# Спринт Hunter: темы в личке + LLM-черновик + пилот Stars

> **Репозиторий:** PromostaffHunter_bot  
> **Сборка после спринта (целевая):** `tool-v4`  
> **Оценка:** **10–12 рабочих дней** (соло + агент в Cursor), без Mini App и без смены polling → webhooks  
> **Канон «современный бот»:** promostaff-agency-bot → [`TELEGRAM_MODERN_BOT_PLAYBOOK.md`](https://github.com/promostaff/promostaff-agency-bot/blob/main/docs/TELEGRAM_MODERN_BOT_PLAYBOOK.md) (локально в воркспейсе: `promostaff-agency-bot/docs/TELEGRAM_MODERN_BOT_PLAYBOOK.md`)

Последнее обновление документа: **2026-06-05**

---

## Прогресс реализации (tool-v4)

| Эпик | Статус |
|------|--------|
| 1 — Forum topics | ✅ код + pytest |
| 1b — Канал | ✅ auto push + admin «📣 В канал» + `?start=vac_*` |
| 2 — LLM черновик | ✅ код (`LLM_ENABLED=0` по умолчанию) |
| 3 — Stars | ✅ код (`STARS_ENABLED=0` по умолчанию) |
| Smoke / деплой | ⏳ Bothost + env |

---

## Цель спринта

Сделать Hunter **современным и автоматизированным без нового домена**:

1. **Темы (forum topics) в личке** — структура чата вместо одного «полотна».
2. **Кросс-пост в канал** [@promostaff_agency_job](https://t.me/promostaff_agency_job) — превью + кнопки в бота.
3. **LLM-черновик отклика** — умный текст под вакансию.
4. **Пилот Telegram Stars** — «Расширенный отклик»; Premium в рублях без изменений.

**Статус:** реализовано в коде **`tool-v4`** (2026-06-05). BotFather — владелец настроил.

### Env Bothost

```env
APP_BUILD=tool-v4
FORUM_TOPICS_ENABLED=1
HUNTER_CHANNEL_ID=-100xxxxxxxxxx
CHANNEL_CROSSPOST_ENABLED=1
LLM_ENABLED=0
STARS_ENABLED=0
```

---

## Что уже есть (база)

| Компонент | Файлы / факт |
|-----------|----------------|
| Отклик + черновик (шаблон) | `main.py` → `build_candidate_profile_text`, `respond_*`, deeplink |
| Цветные inline, notfit с причиной | `tool-v3`, `build_vacancy_keyboard`, `vacancy_notfit_feedback` |
| Premium (рубли, чек) | `docs/SUBSCRIPTION.md`, FSM чека |
| Пуш вакансий | `parser.py` → `send_vacancy_to_subscribers` |
| LLM-инфра в экосистеме | **promostaff-agency-bot** → `deepseek_gateway/` (HTTP `/ask` + `X-Internal-Token`) |

---

## Сводная оценка по дням

| День | Блок | Результат дня |
|------|------|----------------|
| **1** | Темы: подготовка | BotFather, флаги, таблица `user_forum_topics`, хелпер `ensure_user_topics` |
| **2** | Темы: маршрутизация | Push → тема «Вакансии»; отклики/поддержка → свои темы; fallback без topics |
| **3** | Темы: polish + тесты | pytest, `/start` → ensure topics |
| **3b** | **Канал:** кросс-пост | `channel_post.py`, `?start=vac_`, env `HUNTER_CHANNEL_ID` |
| **4** | LLM: клиент | `services/llm_client.py` |
| **5** | LLM: черновик отклика | Промпт + интеграция в `respond_*`, кнопка «✨ Улучшить текст», fallback на шаблон |
| **6** | LLM: UX | `send_chat_action(typing)`, опционально `sendMessageDraft`, лимиты/ошибки |
| **7** | Stars: продукт + БД | Спека «Расширенный отклик», `star_purchases`, цена в Stars |
| **8** | Stars: invoice | `sendInvoice` / `pre_checkout_query`, связка с откликом |
| **9** | Stars: выдача фичи | После оплаты — расширенный текст + пометка в `responses`; админ-стат |
| **10** | Интеграция | Сквозной smoke: push → тема → отклик → LLM → Stars |
| **11** | Доки + деплой | `DEVELOPMENT.md`, env Bothost, ручной деплой `tool-v4` |
| **12** | Буфер | Баги Telethon/Bot API, правки по smoke |

**Итого:** 10 дней план + **2 дня буфер** = **12 календарных рабочих дней** при последовательной работе.  
Параллельно (темы ‖ LLM после дня 3): можно уложиться в **~8–9 дней**, но для одного потока безопаснее **10–12**.

---

## Эпик 1 — Forum topics в личке (3 дня)

### Зачем

Один чат с сотнями push теряется. Темы = «папки»: пользователь видит вакансии отдельно от поддержки и служебных сообщений.

### BotFather / API

1. В [@BotFather](https://t.me/BotFather) → бот → **Bot Settings** → включить **Topics in private chats** (forum mode).
2. Опционально: **запретить пользователю** создавать/удалять темы (бот управляет структурой).
3. Методы: `createForumTopic`, `sendMessage` с `message_thread_id`.

### Темы (канон)

| `message_thread_id` | Название | Что туда шлём |
|---------------------|----------|----------------|
| `general` (или первая созданная) | 💬 Главное | `/start`, меню, настройки |
| создаёт бот | 📬 Вакансии | push, «Посмотреть новые», карточки |
| создаёт бот | 📨 Отклики | подтверждения отклика, «Мои отклики» |
| создаёт бот | ❓ Поддержка | вопросы пользователя и ответы админа |

### Задачи по дням

**День 1**

- [x] `db.py` / `init_db()`: таблица `user_forum_topics (user_id, topic_key, thread_id, created_at)`.
- [x] `services/forum_topics.py`: `ensure_user_topics(bot, user_id) → dict[str, int]`.
- [x] Константы `TOPIC_VACANCIES`, `TOPIC_RESPONSES`, `TOPIC_SUPPORT`, `TOPIC_MAIN`.
- [x] Обработка ошибок API (старый клиент, topics выключены) → **fallback**: всё в обычный чат, лог `warning`.

**День 2**

- [x] Вызов `ensure_user_topics` после успешной регистрации и в `/start` (идемпотентно).
- [x] `send_vacancy_card` / push: `message_thread_id=topics["vacancies"]`.
- [x] Отклик, «Мои отклики» → тема `responses`.
- [x] Поддержка (`SupportState`) → тема `support`.
- [x] Админ-ответ поддержки — в ту же тему пользователя.

**День 3**

- [x] Тесты: mock `createForumTopic`, маршрутизация thread_id.
- [ ] Ручной smoke (чеклист §Приёмка).
- [ ] Строка в `/status` или админ-стат: «Topics: enabled / fallback».

### Критерии приёмки (темы)

- [ ] Новый пользователь после регистрации получает 3–4 темы без ручных действий.
- [ ] Push-вакансия **не** попадает в General, если topics включены.
- [x] При ошибке API бот **не падает**, вакансии уходят как сейчас (без thread_id).
- [x] `python -m pytest tests/ -q` — зелёный.

---

## Эпик 1b — Кросс-пост в канал (1 день)

Канал: [@promostaff_agency_job](https://t.me/promostaff_agency_job). Док: [`CHANNEL_CROSSPOST_AND_BOTFATHER.md`](CHANNEL_CROSSPOST_AND_BOTFATHER.md).

- [x] `services/channel_post.py` — превью HTML + URL-кнопки.
- [x] Deep link `?start=vac_{vacancy_id}` → карточка вакансии.
- [x] Auto cross-post после успешного push.
- [x] Админ: «📣 В канал» + кнопка в модерации.
- [x] BotFather: Threaded Mode ON (владелец, 2026-06-05).
- [ ] `HUNTER_CHANNEL_ID` на Bothost + smoke поста.

---

## Эпик 2 — LLM-черновик отклика (3 дня)

### Зачем

Шаблон «ФИО, телефон, возраст» одинаков для всех вакансий. LLM подстраивает текст под **текст вакансии** и **категорию** — выше конверсия отклика.

### Архитектура

```
respond_* → build_candidate_profile_text (fallback)
         → llm_client.enhance_response_draft(vacancy_text, profile) → черновик
         → build_contact_link → deeplink заказчику
```

**Не путать:** LLM **не** отправляет сообщение заказчику сам — только генерирует текст; отправка по-прежнему через пользователя (deeplink).

### Env (Bothost)

```env
LLM_GATEWAY_URL=https://…/ask          # deepseek_gateway или аналог
LLM_INTERNAL_TOKEN=…                   # X-Internal-Token
LLM_ENABLED=1                          # 0 = только шаблон
LLM_TIMEOUT_SEC=25
LLM_DAILY_LIMIT_FREE=0                 # LLM-черновик только Premium или для всех — решение ниже
LLM_DAILY_LIMIT_PREMIUM=20
```

**Продуктовое решение (зафиксировано для спринта):**

- **Free:** шаблон как сейчас; кнопка «✨ Улучшить текст (Premium)» → экран подписки.
- **Premium:** до 20 LLM-черновиков/сутки; счётчик в БД `llm_usage (user_id, day, count)`.

### Задачи по дням

**День 4**

- [x] `services/llm_client.py`: POST JSON `{ "text": prompt }`, парсинг `reply`, retry 1×, таймаут.
- [x] Промпт в `services/llm_prompts.py`: вакансия + профиль + «кратко, вежливо, по-русски, без выдуманных фактов».
- [ ] Unit-тест с mock HTTP (без реального API).

**День 5**

- [x] В `respond_vacancy`: после шаблона — inline «✨ Улучшить текст» (`respond_llm_{vacancy_id}`).
- [x] Callback: `send_chat_action(typing)` → LLM → показать черновик + «Открыть чат» / «Редактировать».
- [x] При ошибке LLM — сообщение «Используем стандартный текст» + шаблон.

**День 6**

- [x] Лимиты Premium/Free, запись в `llm_usage`.
- [ ] Опционально: **`sendMessageDraft`** для поэтапного появления текста (если aiogram/API на Bothost ок).
- [x] Не логировать телефон/ФИО в prompt logs — только `user_id`, `vacancy_id`.

### Критерии приёмки (LLM)

- [ ] Premium: черновик осмысленно ссылается на вакансию (ручная проверка 3 кейсов).
- [ ] Free: LLM не вызывается без Premium.
- [ ] `LLM_ENABLED=0` — поведение как до спринта.
- [ ] Fallback при timeout/500 — без падения handler.

---

## Эпик 3 — Пилот Telegram Stars «Расширенный отклик» (3 дня)

### Зачем

Разовый доход и тест Stars **без замены** месячного Premium в рублях.

### Продукт (одна фича)

**«Расширенный отклик»** (разово за вакансию):

- LLM-черновик (если ещё не использован).
- Явная пометка в тексте для заказчика: «⭐ Приоритетный отклик через Promostaff Hunter».
- Запись в `responses.is_star_boost = true` для аналитики.

**Цена (черновик, уточнить перед день 7):** 25–49 Stars (~$0.5–1) — проверить в BotFather минимумы.

### Не делаем в этом спринте

- Подписка Premium за Stars.
- Буст вакансии для заказчика (можно спринт 2).
- Вывод Stars в рубли на расчётный счёт (отдельная операционка Telegram).

### BotFather

- Payments → Telegram Stars для бота включены.
- Тест в test environment при первой интеграции (если доступно).

### Задачи по дням

**День 7**

- [x] `docs/STARS_EXTENDED_RESPONSE.md` — одностраничная спека для пользователя.
- [x] `init_db()`: `star_purchases (id, user_id, vacancy_id, stars_amount, payload, status, created_at)`.
- [x] `responses.star_boost` или флаг в purchase + join.
- [x] Константа `STARS_EXTENDED_RESPONSE_PRICE` в `config.py`.

**День 8**

- [x] Handler: кнопка «⭐ Расширенный отклик» на экране отклика (рядом с обычным).
- [x] `sendInvoice` (currency `XTR`) / обработка `pre_checkout_query` → `answerPreCheckoutQuery(ok=True)`.
- [x] `successful_payment` / payload `ext_resp:{vacancy_id}` → статус `paid`.

**День 9**

- [x] После оплаты: сгенерировать расширенный черновик (LLM + префикс), deeplink, запись отклика.
- [ ] Админ: счётчик Star-покупок в «📊 Статистика» или Excel-лист (минимум — SQL/лог).
- [x] Edge: повторная оплата той же вакансии → «Уже куплено».

### Критерии приёмки (Stars)

- [ ] Тестовая покупка Stars проходит end-to-end (test/prod по BotFather).
- [ ] Обычный отклик **без** Stars работает как раньше.
- [ ] Premium в рублях не изменился (`SUBSCRIPTION.md` актуален).

---

## День 10–11 — интеграция и выкат

**День 10 — сквозной smoke**

| # | Шаг |
|---|-----|
| 1 | Новый пользователь → темы созданы |
| 2 | Push → карточка в теме «Вакансии» |
| 3 | Premium → «Улучшить текст» → LLM-черновик |
| 4 | Stars → расширенный отклик → deeplink |
| 5 | Free → только шаблон, upsell Premium/Stars |

**День 11**

- [x] `APP_BUILD=tool-v4` в `main.py`.
- [x] Обновить §0 в `DEVELOPMENT.md`, журнал §13.
- [ ] Env на Bothost: LLM + Stars + канал.
- [ ] **Ручной деплой** Bothost → smoke на проде.

---

## Зависимости и риски

| Риск | Митигация |
|------|-----------|
| Topics не включены в BotFather | Fallback без thread_id; инструкция в деплой-чеклисте |
| LLM gateway недоступен | `LLM_ENABLED=0`, шаблон |
| Stars не проходят на prod | Пилот только после test; кнопку скрыть флагом `STARS_ENABLED` |
| Flood при создании тем 1000+ users | Создавать темы лениво при первом push, не при `/start` массово |
| aiogram без `sendMessageDraft` | Пропустить draft, оставить typing + одно сообщение |

---

## Файлы (ожидаемые изменения)

| Файл | Эпик |
|------|------|
| `services/forum_topics.py`, `services/channel_post.py` | 1, 1b |
| `services/llm_client.py`, `services/llm_prompts.py` | 2 |
| `main.py` (маршрутизация, respond, invoice) | 1–3 |
| `db.py` (`init_db`) | 1–3 |
| `config.py` | 2–3 |
| `tests/test_forum_topics.py`, `tests/test_llm_client.py` | 1–2 |
| `docs/STARS_EXTENDED_RESPONSE.md` | 3 |

---

## После спринта (не входит в 12 дней)

- Scoring по `vacancy_notfit_feedback` (P3+).
- Mini App ленты на `promostaff.pro`.
- Stars: буст вакансии для заказчика.
- Webhooks вместо polling при росте нагрузки.

---

## Связанные документы

- [`DEVELOPMENT.md`](DEVELOPMENT.md) — общий статус Hunter
- [`CHANNEL_CROSSPOST_AND_BOTFATHER.md`](CHANNEL_CROSSPOST_AND_BOTFATHER.md) — канал + BotFather
- [`STARS_EXTENDED_RESPONSE.md`](STARS_EXTENDED_RESPONSE.md) — пилот Stars
- [`SUBSCRIPTION.md`](SUBSCRIPTION.md) — Premium в рублях (не Stars)
- **promostaff-agency-bot** → `docs/TELEGRAM_MODERN_BOT_PLAYBOOK.md`
- **promostaff-agency-bot** → `deepseek_gateway/README.md`
