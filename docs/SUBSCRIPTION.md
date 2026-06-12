# Подписка Promostaff Hunter — логика и админка

> Живой источник правды по тарифам, оплате и Premium. Обновлять вместе с кодом (`main.py`, `db.py`).

См. также: [DEVELOPMENT.md §7](DEVELOPMENT.md#7-подписка--продуктовое-решение-зафиксировано) (краткая выжимка + env).

---

## Тарифы

| Тариф | Как получить | Категории | Push | Метро | Срок |
|-------|--------------|-----------|------|-------|------|
| **Free** | по умолчанию | **1** | нет | нет | — |
| **Trial** | один раз при **первом отклике** на вакансию (`setup_trial_from_first_response`) | категория вакансии + Premium на срок trial | да | да | `TRIAL_DAYS` (env, default 7) |
| **Premium** | оплата + одобрение админа или `/setplan` | все | да | да | `paid_until` в БД |

Расширенные фильтры (ставка, города, тихие часы) — **[PREMIUM_FILTERS.md](PREMIUM_FILTERS.md)** (согласовано, реализация по фазам).

**Лимит Free:** `FREE_CATEGORY_LIMIT` (env, default **1**). Вторая категория и дальше — только Premium.

**Платные отклики (после trial):** `PAID_RESPONSES_ENABLED=1` — Free без Premium платит за каждый отклик: **3 Stars** (`STARS_RESPONSE_PRICE`) или **пакет 5 за 99 ₽** (`RESPONSE_PACK_*`, чек → админ). Баланс — `subscribers.response_credits`. Premium — безлимит откликов.

**Расширенный отклик (Stars):** отдельно, `ext_resp:` — 35 Stars, приоритетный текст (см. [STARS_EXTENDED_RESPONSE.md](STARS_EXTENDED_RESPONSE.md)).

**Premium активен:** `plan = 'premium'` и `paid_until` в будущем (`is_user_premium()`).

**Истечение:** cron + `/start` — plan → free, **категории и метро сбрасываются** (`reset_premium_feed_settings`); на Free нужно снова выбрать одну категорию.

**Напоминание:** за `PREMIUM_RENEWAL_REMIND_DAYS` дней (default 3) до `paid_until` — push в личку (cron, раз в час) и строка на экране «💎 Подписка»; один раз на период (`premium_renewal_warn_for`).

**Продление:** `/setplan` и кнопка «✅ Активировать» **добавляют** дни к текущему `paid_until` (`set_user_plan(..., extend=True)`), не сбрасывают с «сегодня».

---

## Поля БД

### `subscribers`

- `plan` — `free` | `premium`
- `paid_until` — TIMESTAMP окончания Premium
- `trial_used` — пробный период уже выдавался
- `response_credits` — платные отклики (пакет 99 ₽)
- `metro_zones` — фильтр станций (Premium)

### `premium_requests`

| Колонка | Назначение |
|---------|------------|
| `id` | PK |
| `user_id`, `username`, `full_name`, `phone`, `category_codes` | снимок на момент запроса |
| `status` | см. ниже |
| `is_renewal` | продление vs первичная покупка |
| `receipt_file_id`, `receipt_kind` | чек (`photo` / `document`) |
| `created_at` | время создания |

**Статусы запроса:**

| status | Смысл |
|--------|--------|
| `awaiting_receipt` | пользователь нажал «Запросить Premium», чек ещё не прислал |
| `pending` | чек получен, ждёт решения админа |
| `approved` | Premium выдан |
| `rejected` | админ отклонил |
| `cancelled` | отменён (новый запрос того же user или отмена пользователем) |

Счётчик «💳 Ожидают Premium» в статистике — только `status = 'pending'`.

---

## Пользовательский сценарий (ручная оплата)

```
💎 Подписка
  → тариф, цена (SUBSCRIPTION_PRICE_RUB), реквизиты (SUBSCRIPTION_CARD_HINT)
  → опционально URL (SUBSCRIPTION_PAY_URL)

📩 Запросить Premium (перевод) / 📩 Запросить продление
  → запись premium_requests (awaiting_receipt)
  → FSM: «Пришлите скрин чека (фото или PDF)»
  → пользователь отправляет фото/документ
  → status = pending, админу алерт с чеком и кнопками
  → пользователю: «Чек получен, ждите проверки»

Админ одобряет → Premium + push-уведомление пользователю
Админ отклоняет → сообщение пользователю
```

Автоматического webhook / OCR / Stars **нет**.

---

## Инструменты админа

| Инструмент | Действие |
|------------|----------|
| **📊 Статистика** | число Premium, «💳 Ожидают Premium» (`pending`) |
| **💎 Запросы Premium** | карточки pending-запросов с чеком и кнопками |
| **💎 Подписка** (вид админа) | подсказки по `/setplan` |
| **`/setplan USER_ID premium N`** | выдать / продлить Premium на N дней |
| **`/setplan USER_ID free`** | снять Premium |
| **🗂️ Карточки пользователей** | plan, `paid_until` |
| **❓ Поддержка (админ)** + **`/answer ID текст`** | общая поддержка (не привязана к оплате) |

### Inline-кнопки на запросе

- **✅ Активировать 30 дн.** — `activate_premium_for_user`, запрос → `approved`
- **❌ Отклонить** — `rejected`, уведомление пользователю

Команда `/setplan` по-прежнему работает и закрывает все `pending` запросы пользователя (`resolve_premium_requests`).

---

## Env

```env
SUBSCRIPTION_SUPPORT=@your_support_or_link
SUBSCRIPTION_PRICE_RUB=299
SUBSCRIPTION_CARD_HINT=Сбер •••• 1234, получатель Иван И.
TRIAL_DAYS=7
PREMIUM_RENEWAL_REMIND_DAYS=3
# SUBSCRIPTION_PAY_URL=   # опционально — кнопка «Оплатить Premium»
```

---

## TODO (не в этом документе как «сделано»)

- [x] Cron проверки истёкших Premium (`premium_scheduler_loop`, каждый час)
- [x] Напоминание за 3 дня до конца Premium (`PREMIUM_RENEWAL_REMIND_DAYS`)
- [ ] Telegram Stars / ЮKassa / webhook

---

## История изменений

| Дата | Изменение |
|------|-----------|
| 2026-06-04 | FSM чека оплаты + кнопки одобрения/отклонения в «💎 Запросы Premium» |
| 2026-06-04 | UX: продление, extend `paid_until`, HTML экран подписки |
| 2026-06-03 | Trial, ручная карта, `premium_requests`, `/setplan` |
