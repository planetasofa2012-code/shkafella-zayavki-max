import os
from dotenv import load_dotenv

load_dotenv()

# ─── MAX ──────────────────────────────────────────────────────
MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN", "")
API_BASE = "https://platform-api.max.ru"

# ─── Услуги ───────────────────────────────────────────────────
SERVICES = [
    "Распил",
    "Присадка и распил",
    "Проектирование + распил + присадка",
    "Подпил/переделка",
]

MAX_FILES = 10

# ─── Google API ───────────────────────────────────────────────
GOOGLE_CREDENTIALS_FILE = "credentials.json"
GOOGLE_SHEET_ID = os.getenv("SHEET_ID", "")
GOOGLE_DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "")
