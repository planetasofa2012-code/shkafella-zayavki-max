"""
Интеграция с Google Sheets и Google Drive.
Загрузка файлов на Диск, запись заявок в таблицу.
"""

import os
import logging
import tempfile
import asyncio

import aiohttp
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from config import GOOGLE_CREDENTIALS_FILE, GOOGLE_SHEET_ID, GOOGLE_DRIVE_FOLDER_ID

logger = logging.getLogger(__name__)

# Области доступа Google API
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Глобальные клиенты (инициализируются один раз)
_sheets_client = None
_drive_service = None


def _init_google():
    """Инициализация Google API клиентов."""
    global _sheets_client, _drive_service

    if not os.path.exists(GOOGLE_CREDENTIALS_FILE):
        logger.warning(f"Файл {GOOGLE_CREDENTIALS_FILE} не найден. Google API отключён.")
        return False

    try:
        creds = Credentials.from_service_account_file(GOOGLE_CREDENTIALS_FILE, scopes=SCOPES)
        _sheets_client = gspread.authorize(creds)
        _drive_service = build("drive", "v3", credentials=creds)
        logger.info("Google API подключён")
        return True
    except Exception as e:
        logger.error(f"Ошибка инициализации Google API: {e}")
        return False


def _ensure_init():
    """Проверка что Google API инициализирован."""
    if _sheets_client is None or _drive_service is None:
        return _init_google()
    return True


async def download_max_file(session: aiohttp.ClientSession, file_url: str) -> bytes:
    """Скачивает файл из MAX по URL."""
    async with session.get(file_url) as resp:
        return await resp.read()


async def upload_files_to_drive(session: aiohttp.ClientSession, files_data: list, folder_name: str) -> tuple[str, list]:
    """
    Загружает файлы заявки на Google Drive в расшаренную папку.
    Создаёт подпапку для каждой заявки.
    Возвращает (id_папки, список_файлов: [{"id": ..., "name": ..., "type": ...}, ...])
    """
    if not _ensure_init() or not GOOGLE_DRIVE_FOLDER_ID:
        logger.warning("Drive не настроен — файлы не загружены")
        return "", []

    try:
        # Создаём подпапку для этой заявки внутри расшаренной папки
        subfolder_meta = {
            "name": folder_name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [GOOGLE_DRIVE_FOLDER_ID],
        }
        subfolder = _drive_service.files().create(
            body=subfolder_meta, fields="id"
        ).execute()
        subfolder_id = subfolder["id"]

        # Делаем папку доступной всем, у кого есть ссылка (для чтения)
        try:
            permission = {"type": "anyone", "role": "reader"}
            _drive_service.permissions().create(
                fileId=subfolder_id,
                body=permission,
                fields="id"
            ).execute()
            logger.info(f"Папка {folder_name} стала доступна по ссылке.")
        except Exception as perm_err:
            logger.warning(f"Не удалось выдать права anyone/reader на папку: {perm_err}")

        uploaded_files = []

        # Загружаем каждый файл
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
                uploaded = _drive_service.files().create(
                    body=file_meta, media_body=media, fields="id"
                ).execute()

                uploaded_files.append({
                    "id": uploaded["id"],
                    "name": filename,
                    "type": "file",
                })

                os.unlink(tmp_path)
                logger.info(f"Файл загружен на Drive: {filename}")

            except Exception as e:
                logger.error(f"Ошибка загрузки файла {f.get('name')}: {e}")

        return subfolder_id, uploaded_files

    except Exception as e:
        logger.error(f"Ошибка загрузки на Drive (папка {folder_name}): {e}")
        return "", []


def append_application_to_sheet(row_data: dict):
    """
    Добавить строку заявки в Google Таблицу.
    """
    if not _ensure_init():
        logger.warning("Google Sheets недоступен — заявка не записана")
        return

    try:
        sheet = _sheets_client.open_by_key(GOOGLE_SHEET_ID).sheet1

        # Проверяем заголовки (создаём если таблица пустая)
        headers = [
            "Дата", "Компания", "Заказ/Клиент", "Услуга",
            "Файлы", "Комментарий", "Дата готовности",
            "MAX User", "MAX User ID",
        ]
        try:
            existing_headers = sheet.row_values(1)
            if not existing_headers:
                sheet.append_row(headers)
        except Exception:
            sheet.append_row(headers)

        # Добавляем данные
        row = [
            row_data.get("max_user", ""),
            row_data.get("date", ""),
            row_data.get("company", ""),
            row_data.get("order_name", ""),
            row_data.get("service", ""),
            row_data.get("files", ""),
            row_data.get("comment", ""),
            row_data.get("deadline", ""),
            row_data.get("max_user_id", ""),
        ]
        sheet.append_row(row, value_input_option="USER_ENTERED")
        logger.info(f"Заявка записана в таблицу: {row_data.get('company')}")

    except Exception as e:
        logger.error(f"Ошибка записи в Google Sheets: {e}")
        raise
