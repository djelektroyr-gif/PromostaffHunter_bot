# Аудит стабилизации PromostaffHunter_bot

> Зафиксировано: **2026-06-07** · репозиторий `PromostaffHunter_bot`  
> Тесты на момент аудита: **113 passed** · прод: `origin/main` @ `91fe302`

Живой план исправлений. Обновлять при закрытии волн.

---

## Суть проблемы

Бот **работает**, но архитектура («всё в `main.py` ~5800 строк + in-memory FSM + check-then-act в БД») даёт:

- зависания парсера и «⏳ в процессе»;
- двойные отклики и дубли при быстрых нажатиях;
- «Нет доступа» / тишина / лишние сообщения;
- фичи в коде, но **не видны** (typing — один ping до handler).

Тесты хорошо покрывают **фильтр парсера, ленту, premium downgrade, категории**. Слабо: **отклики, модерация, channel post, race conditions, lifecycle парсера**.

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

### Волна 2 — защита от двойных нажатий

Premium approve / moderation atomic UPDATE; disable кнопку после respond; broadcast lock; лента «сессия истекла».

### Волна 3 — hardening

Stars/push/channel idempotent; FSM escape helper; RedisStorage; PG smoke tests.

---

## Покрытие тестами

| ✅ Хорошо | ❌ Дыры |
|-----------|--------|
| Фильтр, dedupe, quality gate | `handle_response`, double-tap |
| Feed, freshness | `add_response` UNIQUE |
| Premium extend, downgrade | Moderation → push |
| Category toggle atomic | Channel post E2E |
| Response card format | Incremental cursor |

---

## Changelog волн

| Дата | Волна | Что сделано |
|------|-------|-------------|
| 2026-06-07 | — | Аудит зафиксирован |
| 2026-06-07 | 1 | Parser cursor + mark scanned; UNIQUE отклики; admin card; `/start` clear FSM; parser lock/typing UX |
