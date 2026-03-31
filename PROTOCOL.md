# PROTOCOL.md — Бот заявок Шкафелла (MAX)

> Бот для приёма заявок на мебельный раскрой через мессенджер MAX.
> Репозиторий: https://github.com/planetasofa2012-code/shkafella-zayavki-max

---

## Текущий фокус
✅ Отладка завершена: данные записываются точно в 8 колонок таблицы (симметрия со старым ботом).
✅ Ссылка на Google Drive папки генерируется корректно.

---

## Журнал работы

### 27.03.2026 — Создание и отладка бота MAX

| Время | Задача | Результат |
|-------|--------|-----------|
| 12:30 | Создание проекта | Адаптирован bot.py из Telegram-версии под API MAX (`platform-api.max.ru`) |
| 12:40 | Настройка кнопок | Реализованы inline keyboard через `attachments[type=inline_keyboard]` |
| 12:45 | Деплой на сервер | Docker-контейнер на VPS `5.42.104.62:/opt/shkafella-zayavki-max/` |
| 13:00 | Баг: кнопки не работают | Callback приходил, но `chat_id=None` |
| 13:05 | Фикс #1: `answer_callback` | `callback_id` передаётся через `params={}` для правильного URL-кодирования |
| 13:06 | Фикс #2: `chat_id` extraction | `message` находился на уровне `update`, а не внутри `callback`. Добавлен fallback из 3 источников + маппинг `user_chats` |
| 13:08 | Фикс #3: `answer_callback` body | MAX API требует `notification` или `message` в теле POST `/answers` |
| 13:10 | ✅ Полный flow работает | Кнопки → файлы → комментарий → дедлайн → Google Sheets + Drive |
| 15:15 | Очистка Drive | Создан `cleanup_drive.py` для управления квотой сервисного аккаунта |
| 18:50 | Рефакторинг модулей | Монолитный скрипт разбит на `bot.py`, `config.py`, `google_api.py` (симметрия с эталонной версией) |
| 18:50 | Обновление FSM воронки | Удалён этап запроса телефона для соответствия эталонной структуре вопросов |
| 18:50 | Исправление Google Sheets | Настроен маппинг колонок (разделён order и company). Ссылки на папки формируются через `drive_id` без редиректов |

### 31.03.2026 — Фиксы и допиливание интеграций

| Время | Задача | Результат |
|-------|--------|-----------|
| 18:58 | Фикс импортов | Переменная `_sheets_client` кэшировалась пустой. Исправил импорты, таблица начала писаться корректно. |
| 19:00 | Выравнивание 8 колонок | Подогнал `row` под старый формат Telegram-бота (A: Telegram, B: Время, C: Компания, D: Заказ, E: Услуга, F: Ссылка, G: Комментарий, H: Дедлайн) |
| 19:05 | Настройка .env | Добавлен `GOOGLE_DRIVE_FOLDER_ID` вручную, так как PowerShell ломался на экспорте `ENV=val` команды. |

---

## Архитектура

```
MAX мессенджер
  ↓ (Long Polling)
bot.py (Python + aiohttp)
  ↓
Google Drive (файлы заявок)
Google Sheets (таблица заявок)
```

### Стек
| Компонент | Технология |
|-----------|------------|
| API | MAX Platform API (`platform-api.max.ru`) |
| Язык | Python 3.11 |
| HTTP | aiohttp |
| Google API | google-api-python-client, gspread |
| Деплой | Docker + docker-compose |
| Сервер | VPS `5.42.104.62` |

### Файлы
| Файл | Описание |
|------|----------|
| `bot.py` | Основной бот — FSM, polling, обработка callbacks |
| `credentials.json` | Ключ сервисного аккаунта Google (НЕ в git!) |
| `cleanup_drive.py` | Утилита очистки Google Drive |
| `Dockerfile` | Образ контейнера |
| `docker-compose.yml` | Конфигурация деплоя |
| `requirements.txt` | Python-зависимости |

---

## Полезные находки

| Находка | Детали |
|---------|--------|
| MAX API: callback_id | Передаётся как **query parameter**, не в body |
| MAX API: /answers body | Обязательно `notification` или `message` в JSON-теле |
| MAX API: callback structure | `message` с `recipient.chat_id` находится на уровне `update`, не вложен в `callback` |
| Service Account Drive | Файлы не видны в обычном Drive UI — нужен API для управления |

---

## Деплой

### Команды
```bash
# Локально: пуш в GitHub
git add -A && git commit -m "описание" && git push

# На сервере: редеплой
cd /opt/shkafella-zayavki-max
git pull
docker-compose down
docker-compose up -d --build

# Логи
docker logs shkafella-zayavki-max_bot_1 --tail=50
```

### Переменные окружения (.env)
```
MAX_BOT_TOKEN=***
SHEET_ID=***
GOOGLE_DRIVE_FOLDER_ID=***
```
