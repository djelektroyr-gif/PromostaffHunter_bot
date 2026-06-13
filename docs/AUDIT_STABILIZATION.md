# Аудит стабилизации PromostaffHunter_bot

> Обновлено: **2026-06-13** · репозиторий `PromostaffHunter_bot`  
> Тесты: **329+ passed** · `main.py` ~8300 строк · **прод БД: PostgreSQL** (`DATABASE_URL`)

Живой план исправлений. Обновлять при закрытии волн.

**Источник правды по БД:** прод = **PostgreSQL** (с ~2026-06-05). SQLite — только локальный dev и pytest (в тестах явно `DATABASE_URL=""`). См. `docs/DEVELOPMENT.md` §11.

---

## Суть проблемы

Бот **работает**, но архитектура («всё в `main.py` + in-memory FSM + check-then-act в БД») даёт:

- зависания парсера и «⏳ в процессе»;
- двойные отклики и дубли при быстрых нажатиях;
- «Нет доступа» / тишина / лишние сообщения;
- фичи в коде, но **не видны** без прохода сценария подписчиком (trial, Free-категории, paywall);
- баги **админки и forum topics**, которые **не ловятся** unit-тестами парсера (см. волна 4).

Тесты хорошо покрывают **фильтр парсера, ленту, premium downgrade, monetization**. Слабо: **хендлеры aiogram, админ-экраны, E2E подписчик, PostgreSQL-путь в CI**.

---

## Волна 4 — админ, forum topics, видимость (2026-06-13)

Ошибки всплыли **только из прод-логов** (`notify_admin_handler_error`), не из прежнего аудита.

| ID | Проблема | Где | Эффект | Статус |
|----|----------|-----|--------|--------|
| F1 | Дубль `message_thread_id` | `_send_feed_loading_notice` + `Message.answer()` | `SendMessage() got multiple values…`, лента «зависает» | ✅ fix |
| F2 | `ReplyKeyboardMarkup` в `edit_text` | `send_admin_stats_screen` | «📊 Статистика» падает | ✅ fix |
| F3 | Техрассылка / ответ поддержки | `send_user_message` без fallback топика | Тишина или 0 доставлено | ✅ fix + fallback |
| F4 | Trial / Free / paywall | `response_monetization`, UI-тексты | Пользователь не видит проблему; админ не знает без SQL | ✅ частично (#1–#8) |
| F5 | **Слепая зона** | нет smoke/E2E подписчика | Неизвестно, сколько багов ещё в `main.py` | 🟠 в работе |

### Почему аудит волн 1–3 это не поймал

1. Фокус: парсер, отклики, race, деньги — не проход по **каждой кнопке админки**.
2. Forum topics помечены «✅» по коду, но не по **живому чату** с `FORUM_TOPICS_ENABLED=1`.
3. pytest гоняется на **SQLite**; прод на **PostgreSQL** — расхождения типов/SQL редки, но возможны.
4. Подписчики молчат → нет обратной связи по trial/категориям; баги видны только в БД или при целенаправленном тесте.

---

## Smoke-чеклист админа (после каждого деплоя, ~15 мин)

Выполнять **вторым аккаунтом** или с тестового TG. `FORUM_TOPICS_ENABLED=1` как на проде.

| # | Действие | Ожидание | Ловит |
|---|----------|----------|-------|
| A1 | 📊 Статистика | Дайджест без «🚨 Ошибка в боте» | F2, edit_text |
| A2 | 📣 Техсообщение → текст → **✅ Отправить** | «Доставлено: N», нет 0/N без причины | F3, рассылка |
| A3 | ❓ Поддержка (админ) → ответ на обращение | Текст у подписчика в топике «Поддержка» | deliver_support |
| A4 | 📢 Рассылка (обычная) | ≥1 доставлено на тестового подписчика | broadcast |
| A5 | 📋 Список откликов → карточка | Открывается, нет «Нет доступа» | R4 |
| A6 | Алерт «🚨 Ошибка в боте» за 24 ч | 0 новых после чеклиста | регрессии |

---

## Smoke-чеклист подписчика (живое поведение, ~20 мин)

**Второй телефон / тестовый аккаунт** — иначе вы не видите то, что видит пользователь.

| # | Сценарий | Ожидание | Ловит |
|---|----------|----------|-------|
| S1 | `/start` → регистрация → категории → finish | Меню, темы «Вакансии/Отклики/Поддержка» | forum, FSM |
| S2 | Free: выбрать **2+ категории** | Ограничение или paywall (см. `FREE_CATEGORY_LIMIT`) | Free |
| S3 | 🔍 Посмотреть новые (из топика «Вакансии») | «Собираю ленту…» → карточки, без ошибки | F1 |
| S4 | Первый отклик на вакансию | Trial Premium **или** paywall (не оба молча) | trial/paywall |
| S5 | Второй отклик без Premium | Paywall / Stars / пакет 99₽ | monetization |
| S6 | ❓ Поддержка → вопрос | Подтверждение + ответ админа в топике | support |
| S7 | 💎 Подписка | Тексты совпадают с `docs/SUBSCRIPTION.md` | UX/конфиг |

Записывать скрин + время. Если молчание — смотреть алерт админу и логи Bothost.

---

## Как оценить «сколько ещё таких ошибок»

Точное число **неизвестно** без наблюдаемости. Практичный план:

1. **Smoke A1–A6 + S1–S7** после каждого деплоя — закрывает класс «кнопка падает / тишина».
2. **Счётчик алертов** `notify_admin_handler_error` — цель: 0 за неделю после волны 4.
3. **Метрики в БД (раз в неделю, SQL):**
   - `trial_used=1` и `responses=0` — обход paywall;
   - подписчики с 0 категорий, но `notify=1`;
   - `support_requests` без ответа > 48 ч;
   - отклики `draft_status=failed`.
4. **Тесты-хелперы** `tests/test_forum_send_safety.py` — регрессии merge `message_thread_id` и edit_text.
5. **Рефакторинг `main.py`** (ниже) — уменьшает площадь «непросмотренного» кода.

Полной гарантии без реальных пользователей нет; smoke + алерты + SQL сужают риск с «неизвестно» до «контролируемый список».

---

## Рефакторинг `main.py` — предложение (не откладывать бесконечно)

**Почему не предлагали раньше в каждом таске:** сначала закрывали прод-пожары точечно; большой рефактор во время инцидентов повышает риск новых поломок.

**Почему пора:** ~8300 строк, ~200 хендлеров — любой новый фикс может задеть соседний сценарий; аудит по файлу не масштабируется.

### Порядок (инкрементально, без «переписать всё»)

| Этап | Вынос | Файл | Зачем |
|------|-------|------|-------|
| R1 | Отправка в топики | `services/user_messaging.py` (`send_user_message`, broadcast) | F1–F3, один вход |
| R2 | Админ-меню | `handlers/admin/` (stats, broadcast, support reply) | A1–A3, smoke |
| R3 | Подписчик: лента + отклики | `handlers/feed.py`, `handlers/responses.py` | S3–S5 |
| R4 | Регистрация / категории | `handlers/onboarding.py` | S1–S2 |
| R5 | `main.py` | только `dp`, startup, импорт роутеров | <1500 строк |

Каждый этап = отдельный деплой + smoke A+S. **Не смешивать** с крупными фичами.

---

## 🔴 Критично

### Парсер

| ID | Проблема | Где | Эффект |
|----|----------|-----|--------|
| P1 | Incremental cursor пропускает посты | `parser._process_single_message`, `update_last_processed_id` | Скан с новых к старым: сохранили 105 → `last_id=105`, 104 отфильтровали без mark → `min_id=105` навсегда пропускает 104 |
| P2 | Отсеянные не `mark_message_processed` | `parser.py` ~737–740, дубликаты ~581–588 | Те же посты каждые 5 мин, ложный шум в отчётах |
| P3 | Lock без таймаута на проде | periodic loop `parser.py` | Зависший Telethon → lock навсегда → `/check_now` молчит |
| P4 | «72 vs 36» в статистике | `monitored_aliases` vs `active_chats` | Вводящая подпись + ложные health-алерты |

### Отклики

| ID | Проблема | Где | Эффект |
|----|----------|-----|--------|
| R1 | Нет контакта → отклик не в БД | `handle_response` 4171–4174 | «📨 Мои отклики» пусто, повторы спамят админу |
| R2 | Двойной `callback.answer()` | 4172 + `send_to_admin` 4565 | Второй answer падает, toast ломается |
| R3 | Нет UNIQUE `(user_id, vacancy_id)` | `db.responses` | Два клика → два отклика |
| R4 | Админ: карточка → «Нет доступа» | `response_card_callback` без `for_admin` | Список откликов пользователя не открывается |

### БД / деньги (волна 2+)

| ID | Проблема | Эффект |
|----|----------|--------|
| D1 | `complete_star_purchase` не атомарен | Двойное продление Stars |
| D2 | Push: check → send → mark | Дубль вакансии user |
| D3 | `paid_until IS NULL` = вечный Premium | Расходится с docs |

---

## 🟠 Высокий приоритет (волна 2)

| ID | Область | Проблема |
|----|---------|----------|
| U1 | FSM | `/start` не `state.clear()` → **волна 1** |
| U2 | FSM | Нет escape по кнопкам меню mid-flow |
| U3 | UX | Reply «📱 Отправить номер» не убирается |
| U4 | Лента | `user_pages` in-memory → pagination молча no-op после рестарта |
| U5 | Отклики | Pagination шлёт новые сообщения, мёртвый `resp_page_*` |
| A1 | Админ | Premium approve — двойной клик продлевает дважды |
| A2 | Админ | Модерация без idempotency |
| A3 | Админ | `broadcast_cancel` без admin-check |
| A4 | Парсер | `inspect_parser_chats` monitored flag — **волна 1** |

---

## 🟡 Средний / 🟢 низкий

См. историю чата 2026-06-07: фильтр, канал env vs DB, employer dedupe, metro mid-FSM, complaint FSM, MemoryStorage, legacy `is_sent`.

---

## План волн

### Волна 1 — «бот перестаёт врать» ✅ в работе

- [x] Документ аудита (этот файл)
- [x] Parser: `max(last_id)` + mark всех просмотренных сообщений
- [x] Parser/typing: таймаут lock, stats, 72→36 label (локальные fix)
- [x] Отклики: UNIQUE, no-contact в БД, один answer, admin card
- [x] `/start` → `state.clear()`
- [x] Тесты на cursor + responses

### Волна 2 — защита от двойных нажатий ✅

- [x] Premium approve — `approve_premium_request()` atomic UPDATE
- [x] Moderation approve/reject — `set_vacancy_moderation_if_pending()`
- [x] Respond — кнопка «✅ Отклик отправлен» после первого клика
- [x] Broadcast — `_broadcast_lock` + admin guard на cancel
- [x] Лента — «Сессия истекла» + кнопка «Открыть ленту»
- [x] Тесты `test_wave2_stabilization.py`

### Волна 3 — hardening ✅

- [x] Stars — `complete_star_purchase()` atomic UPDATE до выдачи буста
- [x] Push — `try_reserve_vacancy_sent_to_user()` до send, `unreserve` при ошибке Telegram
- [x] Channel — `try_reserve_vacancy_channel_post()` / `try_reserve_promo_slot()` до send, release при ошибке
- [x] FSM — `UserMenuFsmEscapeMiddleware` + `user_fsm_menu_escape()` для всех `USER_MENU_BUTTONS`
- [x] Feed — `user_feed_sessions` в БД + `_get_user_feed()` после рестарта
- [x] FSM storage — `REDIS_URL` → RedisStorage, иначе MemoryStorage
### Волна 4 — админ + forum + наблюдаемость 🟠

- [x] F1–F3: `message_thread_id`, статистика, fallback техрассылки
- [x] Monetization bugs #1–#8 (trial, paywall, cache)
- [x] `tests/test_forum_send_safety.py`
- [x] Smoke-чеклисты A1–A6, S1–S7 (этот документ)
- [ ] R1: вынести `send_user_message` / broadcast в `services/user_messaging.py`
- [ ] R2: `handlers/admin/`
- [ ] Опционально: pytest с testcontainers PostgreSQL для 5–10 критичных SQL

---

## Покрытие тестами

| ✅ Хорошо | ❌ Дыры |
|-----------|--------|
| Фильтр, dedupe, quality gate | `handle_response` E2E |
| Feed, freshness | Moderation → push E2E |
| Premium extend, downgrade | Channel post E2E (Telegram) |
| Category toggle atomic | Админ-кнопки (до `test_forum_send_safety`) |
| Response card format | **PostgreSQL в CI** (все тесты SQLite) |
| Monetization unit | Подписчик smoke S1–S7 (ручной) |
| forum send merge (`test_forum_send_safety`) | Полный рефактор `main.py` |

---

## Changelog волн

| Дата | Волна | Что сделано |
|------|-------|-------------|
| 2026-06-07 | — | Аудит зафиксирован |
| 2026-06-07 | 1 | Parser cursor + mark scanned; UNIQUE отклики; admin card; `/start` clear FSM; parser lock/typing UX |
| 2026-06-07 | 2 | Atomic premium/moderation; respond keyboard lock; broadcast lock; feed session expired UX |
| 2026-06-07 | 3 | Idempotent stars/push/channel; FSM menu escape; feed persist; RedisStorage optional; wave3 tests |
| 2026-06-13 | 4 | F1–F3 fix; smoke A/S чеклисты; monetization #1–#8; `test_forum_send_safety`; план рефакторинга R1–R5; PG = прод |
