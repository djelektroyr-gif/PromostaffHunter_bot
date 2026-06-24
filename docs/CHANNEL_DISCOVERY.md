# Поиск каналов (Channel Discovery) — схема и запуск

Отдельный инструмент **не заменяет** парсер вакансий Hunter. Он только **находит** каналы и чаты по ключевым словам; в бот чаты добавляются **вручную** после проверки.

## Два разных процесса

| | **Парсер Hunter** (`parser.py`) | **Discovery** (`scripts/run_channel_discovery.py`) |
|---|---|---|
| Задача | Читать **уже добавленные** чаты и вытаскивать вакансии | **Поиск новых** каналов/чатов по словам |
| Где работает | Bothost, 24/7 | **Только локально** на вашем ПК |
| Сессия Telethon | `user_session.session` на сервере | **Другой файл** — `discovery_session.session` |
| Результат | Вакансии в ленту и push | Excel со ссылками для отбора |

**Важно:** один `.session` — один активный процесс. Не запускайте discovery на `user_session`, пока бот на Bothost парсит тот же аккаунт — будет `TypeNotFoundError` и сбои на проде.

---

## Схема (два аккаунта Telegram)

```
┌─────────────────────────────────────────────────────────────────┐
│  Аккаунт 1 (основной)                                           │
│  user_session.session → Bothost → Hunter парсит target_chats    │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│  Аккаунт 2 (для поиска)                                         │
│  discovery_session.session → только локально → run_channel_     │
│  discovery.py → Excel → ручной отбор → ➕ Добавить чат в бот    │
└─────────────────────────────────────────────────────────────────┘

Внешний репозиторий (скачан вручную):
  telegram-parser-main/telegram-parser-main/
  Используется как «поисковик» внутри run_channel_discovery.py
```

---

## Шаг 1. API_ID и API_HASH

Один раз на [my.telegram.org](https://my.telegram.org) → API development tools.

В `.env` в корне Hunter (уже есть для бота):

```
API_ID=...
API_HASH=...
```

Для **второго аккаунта** эти значения **те же** — меняется только номер телефона при входе.

---

## Шаг 2. Создать `discovery_session.session` (второй аккаунт)

В PowerShell:

```powershell
cd C:\Users\Яр\Documents\GitHub\PromostaffHunter_bot\PromostaffHunter_bot

# Не трогать user_session.session прод-парсера — временно убрать из пути
Rename-Item user_session.session user_session_main.session -ErrorAction SilentlyContinue

python create_telethon_session.py
# Введите номер ВТОРОГО аккаунта, код из Telegram, 2FA если есть

Rename-Item user_session.session discovery_session.session
Rename-Item user_session_main.session user_session.session -ErrorAction SilentlyContinue
```

Если `discovery_session.session` уже создан (вход по QR / код), а прод-сессия лежит как `user_session_main.session`:

```powershell
# Вернуть прод-парсеру имя user_session.session
Rename-Item user_session_main.session user_session.session
```

Проверка: `user_session.session` — прод (929…), `discovery_session.session` — поиск каналов (968…).

Файл `discovery_session.session` остаётся в папке проекта (не загружать на Bothost для discovery).

**Если код не приходит (ни в чат Telegram, ни SMS):**

1. Telegram часто шлёт код **только в уже открытый Telegram** (`SentCodeTypeApp`), а не в SMS.
2. После нескольких попыток срабатывает лимит — подождите **1–2 часа**, не жмите скрипт снова.
3. Проверьте на телефоне: **Настройки → Номер** — именно `+7 968 533-73-32`, не прод `929…`.
4. **Вход по QR** (рекомендуется для discovery):

```powershell
Remove-Item user_session.session -ErrorAction SilentlyContinue
pip install qrcode pillow
python create_telethon_session.py --qr --name discovery_session
# Откроется PNG _telethon_qr_login.png — скан только через:
# Настройки → Устройства → Подключить устройство (не камера телефона)
```

Диагностика: `python scripts/diagnose_telethon_code.py +79685337332` — покажет тип доставки кода.

Для **первого** аккаунта прод-парсера сессия создаётся так же, но файл называется `user_session.session` и живёт на Bothost в shared.

---

## Шаг 3. Установить telegram-parser (один раз)

Скачать [4eiz/telegram-parser](https://github.com/4eiz/telegram-parser) в:

```
C:\Users\Яр\Documents\GitHub\telegram-parser-main\telegram-parser-main
```

Или задать свой путь:

```powershell
$env:TELEGRAM_PARSER_ROOT = "C:\путь\telegram-parser-main"
```

В папке parser: `pip install telethon python-dotenv`

---

## Шаг 4. Запуск Discovery

```powershell
cd C:\Users\Яр\Documents\GitHub\PromostaffHunter_bot\PromostaffHunter_bot

# По умолчанию берёт discovery_session.session (или user_session.session если discovery нет)
python scripts/run_channel_discovery.py

# Явно указать файл сессии
$env:DISCOVERY_SESSION = "discovery_session.session"
python scripts/run_channel_discovery.py
```

**Запросы** — править в `scripts/channel_discovery_queries.txt` (строки с `#` — комментарии).

**Фильтр в Excel** (после поиска, до выгрузки):
- меньше `DISCOVERY_MIN_MEMBERS` участников — не попадает в файл (по умолчанию **50**);
- каналы с «скидки WB», «крипто», «казино» и т.п. в названии — отбрасываются.

Свой порог: `$env:DISCOVERY_MIN_MEMBERS = "100"` перед запуском.

**Результат:**

- Excel: `data/channel_discovery/channel_discovery_YYYY-MM-DD.xlsx`
- Колонки: тип, название, участники, ссылка, «Уже в боте», заметки

Добавление в мониторинг: админка / **➕ Добавить чат** или `target_chats` в БД.

---

## Что проверять в Excel (ручной отбор)

Discovery находит **всё похожее по словам**, не только вакансии. Перед добавлением в бот:

- есть ли **реальные посты** с наймом (хелпер, промо, смена, ₽/час);
- нет ли сплошного **спама** (скидки WB, «добро пожаловать в группу», реклама каналов);
- чат **живой** (участники, недавние сообщения).

Примеры **не вакансий**, которые discovery/парсер могут зацепить:

- «Cole Kelly, добро пожаловать в группу…» — системное приветствие;
- посты без оплаты («за еду и ласку», волонтёр) — фильтры ingest должны отсеивать, но чат может быть шумным.

Лучше не добавлять чат, если в ленте больше мусора, чем hiring.

---

## Частые проблемы

| Симптом | Причина | Что делать |
|--------|---------|------------|
| `TypeNotFoundError` на Bothost | Два процесса на одной сессии | Discovery только на `discovery_session`, бот на `user_session` |
| Нет файла сессии | Не создан `discovery_session.session` | Шаг 2 |
| Пустой Excel | Нет аккаунта в `Accounts/` parser | Скрипт копирует сессию сам; проверьте `.env` API_ID/HASH |
| Бот падает на «Настройки» | Устаревший forum topic | Деплой с фиксом reply-keyboard + пересоздание тем |

---

## Связанные файлы в репозитории

| Файл | Назначение |
|------|------------|
| `scripts/run_channel_discovery.py` | Запуск поиска и Excel |
| `scripts/channel_discovery_queries.txt` | Ключевые слова |
| `create_telethon_session.py` | Создание `.session` локально |
| `data/channel_discovery/*.xlsx` | Результаты прогонов |
