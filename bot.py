"""
Бот приёма заявок на мебельный раскрой — мессенджер MAX.
Собирает данные → загружает файлы на Google Drive →
записывает в Google Таблицу.

API: https://platform-api.max.ru (Long Polling)
"""

import logging
import os
import asyncio
import tempfile
from datetime import datetime

import aiohttp
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ─── Конфигурация ─────────────────────────────────────────────
MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN", "")
API_BASE = "https://platform-api.max.ru"
SERVICES = ["Распил", "Присадка", "Проектирование", "Подпил"]
MAX_FILES = 20

# Google API
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]
SHEET_ID = os.getenv("SHEET_ID", "")
ROOT_FOLDER_NAME = "Заявки_Шкафелла_Бот_MAX"

# ─── Логирование ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ─── Google API ───────────────────────────────────────────────
worksheet = None
drive_service = None
root_folder_id = None

try:
    if os.path.exists("credentials.json"):
        credentials = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
        gc = gspread.authorize(credentials)
        if SHEET_ID:
            sh = gc.open_by_key(SHEET_ID)
            worksheet = sh.sheet1
            logger.info("Google Sheets подключён!")
        else:
            logger.warning("SHEET_ID не указан в .env")

        drive_service = build("drive", "v3", credentials=credentials)
        logger.info("Google Drive подключён!")
    else:
        logger.warning("Файл credentials.json не найден.")
except Exception as e:
    logger.error(f"Ошибка подключения к Google API: {e}")


# ─── Google Drive функции ─────────────────────────────────────
def get_or_create_root_folder():
    global root_folder_id
    if root_folder_id:
        return root_folder_id
    if not drive_service:
        return None
    try:
        query = (
            f"name='{ROOT_FOLDER_NAME}' and "
            "mimeType='application/vnd.google-apps.folder' and "
            "trashed=false"
        )
        results = drive_service.files().list(q=query, fields="files(id)").execute()
        files = results.get("files", [])
        if files:
            root_folder_id = files[0]["id"]
            logger.info(f"Корневая папка найдена: {root_folder_id}")
        else:
            folder_meta = {
                "name": ROOT_FOLDER_NAME,
                "mimeType": "application/vnd.google-apps.folder",
            }
            folder = drive_service.files().create(body=folder_meta, fields="id").execute()
            root_folder_id = folder["id"]
            drive_service.permissions().create(
                fileId=root_folder_id,
                body={"type": "anyone", "role": "reader"},
            ).execute()
            logger.info(f"Корневая папка создана: {root_folder_id}")
        return root_folder_id
    except Exception as e:
        logger.error(f"Ошибка работы с корневой папкой: {e}")
        return None


async def download_max_file(session: aiohttp.ClientSession, file_url: str) -> bytes:
    """Скачивает файл из MAX по URL."""
    async with session.get(file_url) as resp:
        return await resp.read()


async def upload_files_to_drive_max(session: aiohttp.ClientSession, files_data: list, folder_name: str) -> str:
    """Загружает файлы заявки на Google Drive."""
    if not drive_service:
        return ""
    parent_id = get_or_create_root_folder()
    if not parent_id:
        return ""
    try:
        subfolder_meta = {
            "name": folder_name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        }
        subfolder = drive_service.files().create(
            body=subfolder_meta, fields="id,webViewLink"
        ).execute()
        subfolder_id = subfolder["id"]
        drive_service.permissions().create(
            fileId=subfolder_id,
            body={"type": "anyone", "role": "reader"},
        ).execute()
        folder_link = subfolder.get(
            "webViewLink",
            f"https://drive.google.com/drive/folders/{subfolder_id}"
        )

        for f in files_data:
            try:
                file_bytes = await download_max_file(session, f["url"])
                filename = f.get("name", "file")
                with tempfile.NamedTemporaryFile(delete=False, suffix=f"_{filename}") as tmp:
                    tmp.write(file_bytes)
                    tmp_path = tmp.name
                file_meta = {
                    "name": filename,
                    "parents": [subfolder_id],
                }
                media = MediaFileUpload(tmp_path)
                drive_service.files().create(body=file_meta, media_body=media).execute()
                os.unlink(tmp_path)
                logger.info(f"Файл загружен на Drive: {filename}")
            except Exception as e:
                logger.error(f"Ошибка загрузки файла {f.get('name')}: {e}")
        return folder_link
    except Exception as e:
        logger.error(f"Ошибка загрузки на Drive: {e}")
        return ""


# ─── MAX Bot API клиент ───────────────────────────────────────
class MaxBot:
    """Обёртка над MAX Bot API с long polling и FSM."""

    def __init__(self, token: str):
        self.token = token
        self.headers = {
            "Authorization": token,
            "Content-Type": "application/json"
        }
        self.session: aiohttp.ClientSession = None
        # FSM: user_id -> {"state": ..., "data": {...}}
        self.users = {}
        self.marker = None

    async def start(self):
        self.session = aiohttp.ClientSession()
        logger.info("MAX бот запущен (long polling)")

        # Проверяем подключение
        me = await self.get_me()
        if me:
            logger.info(f"Бот: {me.get('name', '?')} (@{me.get('username', '?')})")

        # Корневая папка Drive
        folder_id = get_or_create_root_folder()
        if folder_id:
            logger.info(f"Корневая папка Drive: https://drive.google.com/drive/folders/{folder_id}")

        await self.polling()

    async def get_me(self):
        url = f"{API_BASE}/me"
        async with self.session.get(url, headers=self.headers) as resp:
            if resp.status == 200:
                return await resp.json()
            else:
                logger.error(f"GET /me failed: {resp.status}")
                return None

    async def send_message(self, chat_id, text, attachments=None, format_type=None):
        """Отправляет сообщение в чат MAX."""
        url = f"{API_BASE}/messages?chat_id={chat_id}"
        payload = {"text": text}
        if attachments:
            payload["attachments"] = attachments
        if format_type:
            payload["format"] = format_type
        async with self.session.post(url, json=payload, headers=self.headers) as resp:
            if resp.status == 200:
                return await resp.json()
            else:
                body = await resp.text()
                logger.error(f"send_message error {resp.status}: {body}")
                return None

    async def send_inline_keyboard(self, chat_id, text, buttons):
        """Кнопки: buttons = [[{"type":"callback","text":"...","payload":"..."}]]"""
        attachments = [{
            "type": "inline_keyboard",
            "payload": {"buttons": buttons}
        }]
        return await self.send_message(chat_id, text, attachments=attachments)

    async def answer_callback(self, callback_id):
        """Ответ на callback-нажатие кнопки (callback_id в query params)."""
        url = f"{API_BASE}/answers"
        params = {"callback_id": callback_id}
        try:
            async with self.session.post(url, params=params, json={}, headers=self.headers) as resp:
                body = await resp.text()
                logger.info(f"answer_callback status={resp.status}, body={body[:200]}")
                return resp.status == 200
        except Exception as e:
            logger.error(f"answer_callback exception: {e}")
            return False

    async def polling(self):
        """Long polling для получения обновлений."""
        url = f"{API_BASE}/updates"
        while True:
            try:
                params = {"timeout": 30, "types": "message_created,message_callback"}
                if self.marker:
                    params["marker"] = self.marker

                async with self.session.get(url, params=params, headers=self.headers, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        updates = data.get("updates", [])
                        self.marker = data.get("marker", self.marker)

                        for update in updates:
                            await self.handle_update(update)
                    else:
                        body = await resp.text()
                        logger.error(f"Polling error {resp.status}: {body}")
                        await asyncio.sleep(5)

            except asyncio.TimeoutError:
                continue
            except aiohttp.ClientError as e:
                logger.error(f"Polling connection error: {e}")
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"Polling unexpected error: {e}")
                await asyncio.sleep(5)

    async def handle_update(self, update):
        """Маршрутизация обновлений."""
        update_type = update.get("update_type")
        logger.info(f"UPDATE [{update_type}]: {update}")

        if update_type == "message_created":
            message = update.get("message", {})
            await self.handle_message(message)
        elif update_type == "message_callback":
            callback = update.get("callback", {})
            await self.handle_callback(callback)
        else:
            logger.warning(f"Неизвестный тип обновления: {update_type}")

    # ─── FSM помощники ────────────────────────────────────────
    def get_user_state(self, user_id):
        if user_id not in self.users:
            self.users[user_id] = {"state": None, "data": {}}
        return self.users[user_id]

    def set_state(self, user_id, state):
        self.get_user_state(user_id)["state"] = state

    def get_state(self, user_id):
        return self.get_user_state(user_id)["state"]

    def update_data(self, user_id, **kwargs):
        self.get_user_state(user_id)["data"].update(kwargs)

    def get_data(self, user_id):
        return self.get_user_state(user_id)["data"]

    def clear_state(self, user_id):
        self.users[user_id] = {"state": None, "data": {}}

    # ─── Обработка сообщений ──────────────────────────────────
    async def handle_message(self, message):
        body = message.get("body", {})
        text = body.get("text", "").strip()
        sender = message.get("sender", {})
        user_id = sender.get("user_id")
        chat_id = message.get("recipient", {}).get("chat_id")

        if not user_id or not chat_id:
            return

        user_name = sender.get("name", "Пользователь")
        username = sender.get("username", "")
        state = self.get_state(user_id)

        # Проверяем вложения (файлы/фото)
        attachments = body.get("attachments", [])
        has_files = any(a.get("type") in ("image", "file", "video") for a in attachments)

        # Команды
        if text.lower() in ("/start", "start", "начать", "/начать", "привет", "старт"):
            self.clear_state(user_id)
            await self.send_message(chat_id, "Фамилия/название компании")
            self.set_state(user_id, "waiting_company")
            return

        if text.lower() in ("/cancel", "отмена", "/отмена"):
            self.clear_state(user_id)
            await self.send_message(chat_id, "❌ Заявка отменена. Напишите /start для новой.")
            return

        # FSM
        if state == "waiting_company":
            self.update_data(user_id, company=text)
            await self.send_message(chat_id,
                "Ваш номер заказа/фамилия клиента\n"
                "(нужно чтобы потом сформировать общую отгрузку)"
            )
            self.set_state(user_id, "waiting_order_name")

        elif state == "waiting_order_name":
            self.update_data(user_id, order_name=text)
            await self.send_message(chat_id, "📞 Укажите ваш номер телефона для связи:")
            self.set_state(user_id, "waiting_phone")

        elif state == "waiting_phone":
            self.update_data(user_id, phone=text)
            buttons = []
            for i, svc in enumerate(SERVICES):
                buttons.append([{"type": "callback", "text": svc, "payload": f"svc_{i}"}])
            await self.send_inline_keyboard(chat_id, "Что будем делать?", buttons)
            self.set_state(user_id, "waiting_service")

        elif state == "waiting_files":
            if has_files:
                # Обработка файлов
                await self.process_files(user_id, chat_id, attachments)
            else:
                end_words = ("конец", "стоп", "всё", "все", "готово", "далее", "дальше")
                if text.lower() in end_words:
                    files = self.get_data(user_id).get("files", [])
                    if not files:
                        await self.send_message(chat_id, "⚠️ Вы не прикрепили файлов. Отправьте хотя бы один.")
                        return
                    await self.send_message(chat_id,
                        "Комментарий (например: кромка другого цвета или что материал оплачен)"
                    )
                    self.set_state(user_id, "waiting_comment")
                else:
                    await self.send_message(chat_id, "Прикрепите файлы или напишите «Далее».")

        elif state == "waiting_comment":
            self.update_data(user_id, comment=text)
            await self.send_message(chat_id, "Планируемая дата готовности")
            self.set_state(user_id, "waiting_deadline")

        elif state == "waiting_deadline":
            self.update_data(user_id, deadline=text)
            await self.finalize_order(user_id, chat_id, user_name, username)

        else:
            # Нет состояния — автоматически начинаем заявку
            self.clear_state(user_id)
            await self.send_message(chat_id,
                "👋 Добро пожаловать! Начинаем оформление заявки.\n\n"
                "Фамилия/название компании"
            )
            self.set_state(user_id, "waiting_company")

    async def process_files(self, user_id, chat_id, attachments):
        """Обработка прикреплённых файлов."""
        data = self.get_data(user_id)
        files = data.get("files", [])

        for att in attachments:
            att_type = att.get("type")
            if att_type not in ("image", "file", "video"):
                continue

            if len(files) >= MAX_FILES:
                buttons = [[{"type": "callback", "text": f"Далее ➡️ ({len(files)} файл.)", "payload": "files_done"}]]
                await self.send_inline_keyboard(chat_id, f"⚠️ Максимум {MAX_FILES} файлов.", buttons)
                return

            payload = att.get("payload", {})
            file_url = payload.get("url", "")
            file_name = payload.get("file_name") or payload.get("name") or f"файл_{len(files)+1}"

            if file_url:
                files.append({"url": file_url, "name": file_name})

        self.update_data(user_id, files=files)
        count = len(files)
        buttons = [[{"type": "callback", "text": f"Далее ➡️ ({count} файл.)", "payload": "files_done"}]]
        await self.send_inline_keyboard(chat_id,
            f"✅ Файл принят (всего: {count}). Отправьте ещё или нажмите кнопку.",
            buttons
        )

    async def handle_callback(self, callback):
        """Обработка нажатий кнопок."""
        callback_id = callback.get("callback_id", "")
        payload = callback.get("payload", "")
        user_id = callback.get("user", {}).get("user_id")
        chat_id = callback.get("message", {}).get("recipient", {}).get("chat_id")

        logger.info(f"CALLBACK: user_id={user_id}, chat_id={chat_id}, payload={payload}")

        if not user_id or not chat_id:
            logger.warning("CALLBACK: user_id или chat_id не найдены!")
            return

        state = self.get_state(user_id)
        logger.info(f"CALLBACK: текущий state={state}")

        if payload.startswith("svc_") and state == "waiting_service":
            svc_index = int(payload.replace("svc_", ""))
            service = SERVICES[svc_index]
            self.update_data(user_id, service=service)
            logger.info(f"CALLBACK: выбран сервис={service}, отвечаю на callback...")
            await self.answer_callback(callback_id)
            logger.info(f"CALLBACK: отправляю сообщение про файлы...")
            result = await self.send_message(chat_id, f"Прикрепите 1-{MAX_FILES} файлов")
            logger.info(f"CALLBACK: send_message result={result}")
            self.set_state(user_id, "waiting_files")

        elif payload == "files_done" and state == "waiting_files":
            files = self.get_data(user_id).get("files", [])
            if not files:
                await self.answer_callback(callback_id)
                return
            await self.answer_callback(callback_id)
            await self.send_message(chat_id,
                "Комментарий (например: кромка другого цвета или что материал оплачен)"
            )
            self.set_state(user_id, "waiting_comment")
        else:
            logger.warning(f"CALLBACK: не подошло — payload={payload}, state={state}")
            await self.answer_callback(callback_id)

    async def finalize_order(self, user_id, chat_id, user_name, username):
        """Финализация заявки — Drive + Google Sheets."""
        data = self.get_data(user_id)
        dt_now = datetime.now().strftime("%d.%m.%Y %H:%M")
        tg_user = f"@{username}" if username else user_name
        company = data.get("company", "—")
        order_name = data.get("order_name", "—")
        phone = data.get("phone", "—")
        service = data.get("service", "—")
        comment = data.get("comment", "—")
        deadline = data.get("deadline", "—")
        files = data.get("files", [])

        await self.send_message(chat_id, "⏳ Загружаю файлы и отправляю заявку...")

        # 1. Google Drive
        drive_link = ""
        if files and drive_service:
            folder_name = f"{company}_{order_name}_{dt_now.replace(':', '-')}"
            drive_link = await upload_files_to_drive_max(self.session, files, folder_name)
            if drive_link:
                logger.info(f"Файлы загружены на Drive: {drive_link}")

        # 2. Google Sheets
        if worksheet:
            try:
                row = [
                    tg_user,
                    dt_now,
                    f"{company} / {order_name}",
                    phone,
                    service,
                    drive_link or "Файлы в MAX",
                    comment,
                    deadline
                ]
                worksheet.append_row(row)
                await self.send_message(chat_id, "Ваша заявка успешно отправлена ✅")
            except Exception as e:
                logger.error(f"Google Sheets error: {e}")
                await self.send_message(chat_id, "Заявка отправлена ✅, но не записалась в Таблицу.")
        else:
            await self.send_message(chat_id, "⚠️ Таблица не подключена. Заявка не записана.")

        self.clear_state(user_id)

    async def close(self):
        if self.session:
            await self.session.close()


# ─── Запуск ───────────────────────────────────────────────────
async def main():
    if not MAX_BOT_TOKEN:
        logger.error("MAX_BOT_TOKEN не задан в .env!")
        return

    bot = MaxBot(MAX_BOT_TOKEN)
    try:
        await bot.start()
    except KeyboardInterrupt:
        pass
    finally:
        await bot.close()


if __name__ == "__main__":
    asyncio.run(main())
