"""
Бот приёма заявок на мебельный раскрой — мессенджер MAX.
Собирает данные → загружает файлы на Google Drive →
записывает в Google Таблицу.

API: https://platform-api.max.ru (Long Polling)
"""

import logging
import asyncio
from datetime import datetime
import json

import aiohttp

from config import MAX_BOT_TOKEN, API_BASE, SERVICES, MAX_FILES
import google_api
from config import GOOGLE_SHEET_ID

# ─── Логирование ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ─── Обёртка над MAX API ──────────────────────────────────────
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
        # Маппинг user_id -> chat_id (для callback)
        self.user_chats = {}
        self.marker = None

    async def start(self):
        self.session = aiohttp.ClientSession()
        logger.info("MAX бот запущен (long polling)")

        me = await self.get_me()
        if me:
            logger.info(f"Бот: {me.get('name', '?')} (@{me.get('username', '?')})")

        google_api._ensure_init()
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
        attachments = [{
            "type": "inline_keyboard",
            "payload": {"buttons": buttons}
        }]
        return await self.send_message(chat_id, text, attachments=attachments)

    async def answer_callback(self, callback_id, notification="✔"):
        url = f"{API_BASE}/answers"
        params = {"callback_id": callback_id}
        body = {"notification": notification}
        try:
            async with self.session.post(url, params=params, json=body, headers=self.headers) as resp:
                return resp.status == 200
        except Exception as e:
            logger.error(f"answer_callback exception: {e}")
            return False

    async def polling(self):
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
                        await asyncio.sleep(5)

            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"Polling error: {e}")
                await asyncio.sleep(5)

    async def handle_update(self, update):
        update_type = update.get("update_type")
        if update_type == "message_created":
            message = update.get("message", {})
            await self.handle_message(message)
        elif update_type == "message_callback":
            await self.handle_callback(update)

    # ─── FSM Helpers ───
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

    # ─── Message Handling ───
    async def handle_message(self, message):
        body = message.get("body", {})
        text = body.get("text", "").strip()
        sender = message.get("sender", {})
        user_id = sender.get("user_id")
        chat_id = message.get("recipient", {}).get("chat_id")

        if not user_id or not chat_id:
            return

        self.user_chats[user_id] = chat_id

        user_name = sender.get("name", "Пользователь")
        username = sender.get("username", "")
        state = self.get_state(user_id)

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

        # FSM Routing
        if state == "waiting_company":
            self.update_data(user_id, company=text)
            await self.send_message(chat_id,
                "Ваш номер заказа/фамилия клиента\n"
                "(нужно чтобы потом сформировать общую отгрузку)"
            )
            self.set_state(user_id, "waiting_order_name")

        elif state == "waiting_order_name":
            self.update_data(user_id, order_name=text)
            buttons = []
            for i, svc in enumerate(SERVICES):
                buttons.append([{"type": "callback", "text": svc, "payload": f"svc_{i}"}])
            await self.send_inline_keyboard(chat_id, "Что будем делать?", buttons)
            self.set_state(user_id, "waiting_service")

        elif state == "waiting_files":
            if has_files:
                await self.process_files(user_id, chat_id, attachments)
            else:
                end_words = ("конец отправки", "конец", "стоп", "всё", "все", "готово", "далее", "дальше")
                if text.lower() in end_words:
                    files = self.get_data(user_id).get("files", [])
                    if not files:
                        await self.send_message(chat_id, "⚠️ Вы не прикрепили файлов. Отправьте хотя бы один.")
                        return
                    await self.send_message(chat_id, "Комментарий (например клей PUR)")
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
            self.clear_state(user_id)
            await self.send_message(chat_id, "Фамилия/название компании")
            self.set_state(user_id, "waiting_company")

    async def process_files(self, user_id, chat_id, attachments):
        data = self.get_data(user_id)
        files = data.get("files", [])

        for att in attachments:
            if att.get("type") not in ("image", "file", "video"):
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
            f"✅ Файл {count} принят. Отправьте ещё или нажмите кнопку.",
            buttons
        )

    # ─── Callback Handling ───
    async def handle_callback(self, update):
        callback = update.get("callback", {})
        callback_id = callback.get("callback_id", "")
        payload = callback.get("payload", "")
        user_id = callback.get("user", {}).get("user_id")

        chat_id = self.user_chats.get(user_id)
        if not chat_id:
            cb_msg = callback.get("message", {}) or update.get("message", {})
            if cb_msg:
                chat_id = cb_msg.get("recipient", {}).get("chat_id")

        if not user_id or not chat_id:
            return

        state = self.get_state(user_id)

        if payload.startswith("svc_") and state == "waiting_service":
            svc_index = int(payload.replace("svc_", ""))
            service = SERVICES[svc_index]
            self.update_data(user_id, service=service)
            await self.answer_callback(callback_id)
            await self.send_message(chat_id, f"Прикрепите 1-{MAX_FILES} файлов")
            self.set_state(user_id, "waiting_files")

        elif payload == "files_done" and state == "waiting_files":
            files = self.get_data(user_id).get("files", [])
            if not files:
                await self.answer_callback(callback_id)
                await self.send_message(chat_id, "⚠️ Сначала прикрепите хотя бы один файл.")
                return
            await self.answer_callback(callback_id)
            await self.send_message(chat_id, "Комментарий (например клей PUR)")
            self.set_state(user_id, "waiting_comment")
        else:
            await self.answer_callback(callback_id)

    # ─── Finalize Order ───
    async def finalize_order(self, user_id, chat_id, user_name, username):
        data = self.get_data(user_id)
        dt_now = datetime.now().strftime("%d.%m.%Y %H:%M")
        tg_user = f"@{username}" if username else user_name
        company = data.get("company", "—")
        order_name = data.get("order_name", "—")
        service = data.get("service", "—")
        comment = data.get("comment", "—")
        deadline = data.get("deadline", "—")
        files = data.get("files", [])

        await self.send_message(chat_id, "⏳ Загружаю файлы и отправляю заявку...")

        folder_id = ""
        if files:
            folder_name = f"{company}_{order_name}_{dt_now.replace(':', '-')}"
            folder_id, _ = await google_api.upload_files_to_drive(self.session, files, folder_name)

        if google_api._ensure_init() and google_api._sheets_client:
            try:
                sheet = google_api._sheets_client.open_by_key(GOOGLE_SHEET_ID).sheet1
                
                folder_link = f"https://drive.google.com/drive/folders/{folder_id}" if folder_id else "Нет файлов"

                row = [
                    tg_user,           # A. Telegram
                    dt_now,            # B. Время
                    company,           # C. Название/Фамилия
                    order_name,        # D. Заказ/Телефон (или Заказ/Клиент)
                    service,           # E. Услуга
                    folder_link,       # F. Ссылка на саму папку с файлами
                    comment,           # G. Комментарий
                    deadline,          # H. Дата готовности
                ]

                sheet.append_row(row, value_input_option="USER_ENTERED")
                await self.send_message(chat_id, "Ваша заявка отправлена ✅")
            except Exception as e:
                logger.error(f"Google Sheets append error: {e}")
                await self.send_message(chat_id, "Заявка отправлена ✅, но не записалась в Таблицу.")
        else:
            await self.send_message(chat_id, "⚠️ Таблица не подключена. Заявка не записана.")

        self.clear_state(user_id)

    async def close(self):
        if self.session:
            await self.session.close()


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
