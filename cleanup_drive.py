"""
Скрипт очистки Google Drive сервисного аккаунта.
Показывает все файлы и папки, позволяет удалить.

Запуск:
  python cleanup_drive.py          — показать файлы и объём
  python cleanup_drive.py --delete  — удалить ВСЕ файлы (осторожно!)
  python cleanup_drive.py --trash   — очистить корзину
"""

import sys
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/drive"
]

credentials = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
drive = build("drive", "v3", credentials=credentials)


def get_about():
    """Показать квоту диска."""
    about = drive.about().get(fields="storageQuota, user").execute()
    quota = about.get("storageQuota", {})
    used = int(quota.get("usageInDrive", 0))
    trash = int(quota.get("usageInDriveTrash", 0))
    limit = int(quota.get("limit", 0))
    print(f"\n📊 Квота Google Drive (сервисный аккаунт):")
    print(f"   Использовано: {used / 1024 / 1024:.1f} MB")
    print(f"   В корзине:    {trash / 1024 / 1024:.1f} MB")
    if limit:
        print(f"   Лимит:        {limit / 1024 / 1024 / 1024:.1f} GB")
    else:
        print(f"   Лимит:        не ограничен")
    print()


def list_all_files():
    """Список всех файлов."""
    files = []
    page_token = None
    while True:
        resp = drive.files().list(
            q="trashed=false",
            fields="nextPageToken, files(id, name, mimeType, size, createdTime)",
            pageSize=100,
            pageToken=page_token
        ).execute()
        files.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    print(f"📁 Всего файлов/папок: {len(files)}\n")
    
    total_size = 0
    folders = []
    regular = []
    
    for f in files:
        if f["mimeType"] == "application/vnd.google-apps.folder":
            folders.append(f)
        else:
            size = int(f.get("size", 0))
            total_size += size
            regular.append((f, size))

    print(f"📂 Папки ({len(folders)}):")
    for f in folders:
        print(f"   {f['name']:<50} {f['createdTime'][:10]}  id={f['id']}")

    print(f"\n📄 Файлы ({len(regular)}):")
    for f, size in sorted(regular, key=lambda x: -x[1])[:30]:
        mb = size / 1024 / 1024
        print(f"   {f['name']:<50} {mb:>8.2f} MB  {f['createdTime'][:10]}")
    
    if len(regular) > 30:
        print(f"   ... и ещё {len(regular) - 30} файлов")

    print(f"\n   Общий размер файлов: {total_size / 1024 / 1024:.1f} MB")
    return files


def empty_trash():
    """Очистить корзину."""
    print("🗑️  Очищаю корзину...")
    drive.files().emptyTrash().execute()
    print("✅ Корзина очищена!")


def delete_all_files():
    """Удалить ВСЕ файлы (кроме корневых папок заявок)."""
    page_token = None
    deleted = 0
    while True:
        resp = drive.files().list(
            q="trashed=false",
            fields="nextPageToken, files(id, name, mimeType)",
            pageSize=100,
            pageToken=page_token
        ).execute()
        files = resp.get("files", [])
        if not files:
            break
        for f in files:
            try:
                drive.files().delete(fileId=f["id"]).execute()
                deleted += 1
                print(f"   ❌ Удалён: {f['name']}")
            except Exception as e:
                print(f"   ⚠️  Ошибка: {f['name']}: {e}")
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    print(f"\n✅ Удалено файлов: {deleted}")


if __name__ == "__main__":
    args = sys.argv[1:]
    
    get_about()
    
    if "--trash" in args:
        empty_trash()
        get_about()
    elif "--delete" in args:
        confirm = input("⚠️  УДАЛИТЬ ВСЕ файлы? (yes/no): ")
        if confirm.lower() == "yes":
            delete_all_files()
            empty_trash()
            get_about()
        else:
            print("Отменено.")
    else:
        list_all_files()
        print("\nДля очистки корзины:  python cleanup_drive.py --trash")
        print("Для удаления ВСЕГО:   python cleanup_drive.py --delete")
