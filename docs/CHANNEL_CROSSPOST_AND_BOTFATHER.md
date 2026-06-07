# Канал @promostaff_agency_job + BotFather: настройки и кросс-постинг

> **Канал:** [t.me/promostaff_agency_job](https://t.me/promostaff_agency_job)  
> **Бот Hunter:** `@PromostaffHunter_bot`  
> **Снимки BotFather:** зафиксированы **2026-06-05** (скрины владельца в Cursor assets)

Документ: **зачем канал**, **как кросс-постить превью с кнопками**, **что включить в BotFather** (перевод с английского), **что лишнее выключить**.

**Готовые тексты для закрепа, BotFather, ВП и новостей:** [`CHANNEL_MARKETING_COPY.md`](CHANNEL_MARKETING_COPY.md).

---

## 1. Зачем канал, если есть бот

| Аудитория | Что видит в канале | Куда ведём |
|-----------|-------------------|------------|
| **Исполнитель** | Короткое превью вакансии (категория, оплата, район) | Кнопка **«Подробнее в боте»** → полная картоcc + отклик |
| **Заказчик** | Примеры вакансий, доверие к агентству | Кнопка **«Разместить вакансию»** → `?start=employer` |
| **Оба** | Новости, советы, «ищем промо/хелперов» | Закреп + периодические посты |

**Канал = витрина и трафик.**  
**Бот = анкета, фильтры, push, отклик, Premium, модерация.**

Парсер по-прежнему читает **группы** (Telethon). Канал — **исходящая** публикация от бота, не источник парсинга (если только специально не добавите канал как источник — не рекомендуется для своей витрины).

---

## 2. Кросс-постинг: как это работает технически

### Что постим в канал

Короткая **HTML-карточка** (до ~900 символов) **с обложкой-картинкой** по категории вакансии или промо-варианту:

- **Вакансии:** `send_photo` + caption; файл из `assets/channel_images/` по `category_code` (см. `services/channel_images.py`).
- **Автопромо:** картинка по индексу варианта текста (0 → categories, 1 → subscribe, 2 → premium).
- **Промпты для единого стиля:** [`CHANNEL_IMAGE_PROMPTS.md`](CHANNEL_IMAGE_PROMPTS.md).

```
📢 Промоутер · Москва · от 3500 ₽/смена
🕒 Сегодня · из чата «…»
Кратко: … (2–3 строки)

[ Подробнее в боте ]  [ Разместить вакансию ]
```

### Кнопки — только URL (не callback)

В **канале** надёжный паттерн — **inline-кнопки с `url`**, не `callback_data`:

| Кнопка | URL |
|--------|-----|
| Подробнее в боте | `https://t.me/PromostaffHunter_bot?start=vac_<vacancy_id>` |
| Разместить вакансию | `https://t.me/PromostaffHunter_bot?start=employer` |
| Подписаться на ленту (опц.) | `https://t.me/PromostaffHunter_bot?start=from_channel` |

**Почему URL:** человек без регистрации жмёт → открывается бот → `/start vac_…` → полная вакансия + «Откликнуться». Callback из канала тоже возможен, но часто путает пользователя (ответ приходит в личку бота неочевидно).

### Когда постить

| Триггер | Режим |
|---------|--------|
| Новая вакансия прошла P0 + push подписчикам | Авто-кросс-пост (флаг в config) |
| Вакансия заказчика после модерации ✅ | То же |
| Админ | Ручная кнопка «Опубликовать в канал» |
| Дайджest / топ дня | Планировщик 1× в день |

### Права бота в канале

1. Добавить `@PromostaffHunter_bot` в **@promostaff_agency_job** как **администратора**.
2. Минимум: **Post messages** (публиковать сообщения).
3. Желательно: **Edit messages** (исправить опечатку), **Delete messages** (снять закрытую вакансию).

`chat_id` канала сохранить в env: `HUNTER_CHANNEL_ID=-100…` (узнать через `@RawDataBot` или лог при первом посте).

### Код (план реализации)

- `services/channel_post.py` → `post_vacancy_preview_to_channel()` — **photo + caption**
- `services/channel_promo.py` → `post_channel_promo()` — **photo по variant_index**
- `services/channel_images.py` — маппинг `category_code` / промо-индекс → PNG
- `assets/channel_images/` — ассеты из git; промпты — `docs/CHANNEL_IMAGE_PROMPTS.md`
- Вызов после `send_vacancy_to_subscribers` или из админки
- `main.py`: handler `/start vac_<id>` — показать карточку даже если пользователь не в Premium push

---

## 3. BotFather: карта меню (главный экран бота)

Путь: **@BotFather** → `/mybots` → **PromoStaff_Hunter_bot** → открывается дашборд.

```
┌─────────────────────────────────────┐
│  [аватар] PromoStaff_Hunter_bot     │
│  @PromostaffHunter_bot              │
│  API Token: …  [Copy] [Revoke]      │
├─────────────────────────────────────┤
│  Settings                           │
│    Edit Info          ⓘ  →          │
│    Commands           /  →          │
│    Mini Apps          ⊞  →          │
│    Bot Settings       🤖 →          │
│    Login Widget       🔑 →          │
│    Games              🎮 →          │
├─────────────────────────────────────┤
│  Monetization                       │
│    Payments           $  →          │
│    Telegram Stars     ⭐ →          │
├─────────────────────────────────────┤
│  Actions                            │
│    Transfer Ownership               │
│    Delete Bot                       │
└─────────────────────────────────────┘
```

| Пункт | По-русски | Нужен для канала / Hunter сейчас? |
|-------|-----------|-----------------------------------|
| **Edit Info** | Имя, описание, аватар, welcome | ✅ Да — витрина |
| **Commands** | Меню `/start`, `/help`… | ✅ Да — расширить по ролям |
| **Mini Apps** | Веб-приложение в Telegram | ⏳ Позже (нужен HTTPS на promostaff.pro) |
| **Bot Settings** | Группы, темы, режимы | ✅ Да — см. §4 |
| **Login Widget** | Вход на **сайт** через Telegram | ❌ Не для канала |
| **Games** | HTML5-игры | ❌ Не нужно |
| **Payments** | Оплата **физических** товаров | ❌ Premium в рублях — не сюда |
| **Telegram Stars** | Цифровые покупки в боте | ✅ Пилот «Расширенный отклик» |

---

## 4. Bot Settings — что на ваших скринах и что делать

Путь: **Bot Settings** → два блока: **Mode Settings** и **Threads / Groups**.

### 4.1 Mode Settings (скрин 2026-06-05)

| Переключатель | Было у вас | Рекомендация | Зачем |
|---------------|------------|--------------|-------|
| **Inline Mode** | OFF | **OFF** (пока) | Нужен для `@bot запрос` в любом чате; для **постов в канал** не обязателен |
| **Bot Management Mode** | ON | **OFF** | Создание других ботов — Hunter не использует |
| **Guest Chat Mode** | ON | **OFF** | Ответы без добавления в чат — не нужно, лишняя поверхность |
| **Secretary Mode** | ON | **OFF** | Доступ ко **всем** чатам пользователя — не для вакансий, риск |
| **Bot to Bot Communication** | ON | **OFF** | Общение бот↔бот; риск петель, не нужно |

### 4.2 Threads Settings

| Переключатель | Было у вас | Рекомендация | Зачем |
|---------------|------------|--------------|-------|
| **Threaded Mode** | ON | **ON** ✅ | Темы в **личке** с ботом (см. `SPRINT_MODERN_UX_LLM_STARS.md`); к каналу не относится |

Текст про «fee for Telegram Star purchases» — комиссия Telegram на покупки Stars в threaded-режиме; на обычный Stars-пилот не блокер.

### 4.3 Groups and Channels (скрин 2026-06-05)

| Переключатель | Было у вас | Рекомендация | Зачем |
|---------------|------------|--------------|-------|
| **Allow Groups** | ON | **ON** ✅ | Бот может быть добавлен в группы (парсер — отдельно Telethon) |
| **Group Privacy** | ON | **ON** ✅ | В группах бот видит только команды и упоминания — меньше шума |
| **Group Admin Rights** | ON, 0/13 | **OFF** или 0/13 | Права в **группах** Hunter не модерирует |
| **Channel Admin Rights** | ON, 0/13 | **ON** → настроить права | См. §4.4 |

**Privacy Policy → Enter URL:** пусто — ок для старта (стандартная политика Telegram).

**Restrict bot usage:** OFF — бот публичный, так и нужно.

### 4.4 Channel Admin Rights — что включить (0/13 → галочки)

Нажать **Channel Admin Rights** в BotFather и отметить **только нужное**:

| Право | English | Включить? |
|-------|---------|-----------|
| Публиковать | Post messages | ✅ **Да** |
| Редактировать | Edit messages | ✅ Да |
| Удалять | Delete messages | ✅ Да |
| Добавлять админов | Add administrators | ❌ Нет |
| Остальное | … | ❌ Нет |

После сохранения: при добавлении бота в канал Telegram **предложит** эти права по умолчанию.

---

## 5. Edit Info (скрины профиля)

| Поле | English | У вас | Комментарий |
|------|---------|-------|-------------|
| Имя | Bot Name | PromoStaff_Hunter_bot | Можно короче: «Promostaff Hunter» |
| About | About text | «Бот мониторинга и откликов…» | Видно в **профиле** бота |
| Welcome / Description | What can this bot do? | Текст с ✅ категориями | Видно **до** нажатия Start |
| Картинка welcome | Set Welcome Picture | Логотин 640×360 | ✅ Хорошо для доверия |

Кнопка **Update** внизу — сохранить.

**Дополнить welcome для канала** (1 абзац):

> Подписывайтесь на канал вакансий: [@promostaff_agency_job](https://t.me/promostaff_agency_job)

---

## 6. Commands (скрин 2026-06-05)

Сейчас: `/start`, `/help`.

**Расширить (через Edit или код `set_my_commands`):**

| Команда | Описание |
|---------|----------|
| `/start` | 🏠 Главное меню |
| `/help` | 📖 Как пользоваться |
| `/feed` | 🔍 Новые вакансии |
| `/employer` | 📤 Разместить вакансию |

List: **Default** — для всех; отдельный scope для админа — позже.

---

## 7. Telegram Stars (скрин Monetization)

Путь: **Telegram Stars** → экран «Stars are used for in-app payments…» → **Learn More**.

| Вопрос | Ответ |
|--------|--------|
| Включить сейчас? | ✅ Да, если идём на пилот «Расширенный отклик» |
| Это замена Premium? | **Нет** — Premium в рублях ([SUBSCRIPTION.md](SUBSCRIPTION.md)) |
| Что продавать за Stars | Разовый расширенный отклик, позже — буст вакансии для заказчика |

Настройка приёма Stars — в BotFather + код `sendInvoice` (см. `SPRINT_MODERN_UX_LLM_STARS.md`, дни 7–9).

---

## 8. Что НЕ трогать (по вашим скринам)

| Экран | Почему |
|-------|--------|
| **Login Widget → Enter URL** | Нужен только если сайт логинит через Telegram |
| **Games → Inline Mode** | Только для HTML5-игр, не для вакансий |
| **Payments ($)** | Физические товары; не для Premium в рублях |
| **Secretary / Bot Management / Guest** | Лишние режимы — **выключить** (§4.1) |

---

## 9. Чеклист: канал + бот готовы к кросс-посту

### В Telegram (канал)

- [ ] `@PromostaffHunter_bot` — **админ** канала @promostaff_agency_job
- [ ] Права: Post, Edit, Delete messages
- [ ] Закреп: пост «Как откликаться» + ссылка на бота + `?start=employer` для заказчиков
- [ ] Описание канала со ссылкой на бота

### В BotFather

- [x] Channel Admin Rights: Post + Edit + Delete
- [x] Threaded Mode: ON (личка)
- [x] Secretary / Guest / Bot Management / Bot-to-Bot: **OFF**
- [ ] Welcome picture + текст с ссылкой на канал
- [ ] Stars: готовы к пилоту (когда код готов)

### В коде Hunter (tool-v4)

- [ ] `HUNTER_CHANNEL_ID` в env Bothost
- [x] `post_vacancy_preview()` + `?start=vac_<id>`
- [x] Флаг `CHANNEL_CROSSPOST_ENABLED=1`
- [x] Админ: «Опубликовать в канал» (`📣 В канал`, кнопка в модерации)

---

## 10. Воронка в одной схеме

```
Группы (парсер Telethon)
        ↓
   Hunter БД / модерация
        ↓
   Push подписчикам (личка бота)
        ↓
   Кросс-пост превью → @promostaff_agency_job
        ↓
   [Подробнее] → t.me/PromostaffHunter_bot?start=vac_xxx
        ↓
   Регистрация / отклик / Premium (рубли) / Stars (разово)
```

---

## 11. Связанные документы

- [SPRINT_MODERN_UX_LLM_STARS.md](SPRINT_MODERN_UX_LLM_STARS.md) — темы, LLM, Stars
- [DEVELOPMENT.md](DEVELOPMENT.md) — общий статус Hunter
- **promostaff-agency-bot** → [VACANCY_CHANNEL_DEEP_LINK_AND_JOBS_BOT.md](https://github.com/...) — отдельный jobs-бот агентства (другой продукт, та же идея deep link)

---

## 12. Тексты автопромо (без правок Python)

Расписание 09:00 / 14:00 / 20:00 МСК — тексты **не в коде**:

| Способ | Как |
|--------|-----|
| **Файл** | `assets/channel_promo_texts.json` — массив `variants` (HTML). Редактируется в git; на Bothost `data/` не подтягивается из репозитория |
| **Бот** | **📺 Канал → ✏️ Тексты промо** — правка слотов 1/2/3 (сохраняется в БД, приоритет над файлом) |
| **Сброс** | **🗑 Сброс** — убрать правки из БД, снова файл или встроенные дефолты |

Примеры формулировок: [CHANNEL_MARKETING_COPY.md](CHANNEL_MARKETING_COPY.md).

---

## 13. Журнал

| Дата | Изменение |
|------|-----------|
| 2026-06-06 | Тексты автопромо: `data/channel_promo_texts.json` + админка **✏️ Тексты промо** |
| 2026-06-05 | Первая версия: канал, кросс-пост, BotFather по скринам владельца |
