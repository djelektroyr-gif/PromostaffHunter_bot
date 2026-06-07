# Промпты для картинок канала @promostaff_agency_job

Единый стиль для вакансий и промо-постов **Promostaff Hunter**.  
Файлы лежат в `data/channel_images/`; маппинг — `services/channel_images.py`.

Формат: **1080×1080**, квадрат 1:1 для Telegram.

---

## Базовый стиль (добавлять в каждый промпт)

```
Square 1:1 Telegram channel cover PROMOSTAFF. Chibi cartoon vector illustration:
friendly professional characters with large expressive eyes, thick black outlines,
flat vibrant colors, soft shading. Black PROMOSTAFF uniform (polo + cap) unless
role requires different attire (garderob vest, parking safety vest). White
PROMOSTAFF logo on cap and chest. Gradient background sky-blue at top to warm
peach/orange at bottom. Clean composition, no scattered confetti — at most 2–4
tiny sparkles near a focal object (magnifying glass, phone). Large bold Russian
role title. Small green or role-colored pill button ОТКРЫТО at bottom for vacancy
cards. Marketing quality, readable at phone size.
```

**Reference для новых картинок:** `data/channel_images/vacancy-default.png` (маскот) или ближайшая карточка той же серии.

---

## Вакансии (`category_code` → файл)

| code | Файл | Акцент цвета |
|------|------|--------------|
| `loader` | `vacancy-loader.png` | orange |
| `helper` | `vacancy-helper.png` | green |
| `promoter` | `vacancy-promoter.png` | pink |
| `supervisor` | `vacancy-supervisor.png` | purple |
| `wardrobe` | `vacancy-wardrobe.png` | teal/navy |
| `parking` | `vacancy-parking.png` | yellow |
| *остальные* | `vacancy-default.png` | blue |

### Грузчик (`loader`)

```
[BASE STYLE]
Young male loader in black PROMOSTAFF polo and cap carrying brown cardboard box
with PROMOSTAFF logo. Yellow hand truck (рохля) with stacked boxes beside him.
Background: cargo truck (фура) with open rear doors, warehouse loading area —
NOT event stage, NOT subtitle about events. Large text ГРУЗЧИК. Green ОТКРЫТО.
No orange banner with extra marketing text.
```

### Хелпер (`helper`)

```
[BASE STYLE]
Two different chibi characters (man and woman) in black PROMOSTAFF polos carrying
a sofa together. Event setup background: flight case, stage truss, chairs, mic stand.
Large green header ХЕЛПЕР. Green ОТКРЫТО at bottom.
```

### Промоутер (`promoter`)

```
[BASE STYLE]
Female promoter in black PROMOSTAFF cap and polo at promo stand. Right hand:
colorful flyer «ПОПРОБУЙ НОВИНКУ». Left hand: plate with food samples on toothpicks.
Supermarket aisle blur behind. Large pink text ПРОМОУТЕР. Green ОТКРЫТО.
```

### Супервайзер (`supervisor`)

```
[BASE STYLE]
Male supervisor in black PROMOSTAFF uniform with clipboard «ПЛАН ДНЯ» and checkmarks,
standing next to blue company SUV with PROMOSTAFF on door. Event venue sign in background.
Large purple text СУПЕРВАЙЗЕР. Purple ОТКРЫТО.
```

### Гардеробщик (`wardrobe`)

```
[BASE STYLE]
Young attendant in white shirt, black vest, bow tie, white gloves. Holding coat
hanger and numbered ticket tag «1151». Coat rack with jackets behind, elegant counter.
Large white text ГАРДЕРОБ. Teal ОТКРЫТО with checkmark.
```

### Парковщик (`parking`)

```
[BASE STYLE]
Parking attendant: black PROMOSTAFF cap, blue checkered shirt, neon yellow-green
safety vest with reflective stripes, black tablet in hand. Outdoor parking lot with
cars and P sign. Large yellow text ПАРКОВКА. Yellow ОТКРЫТО.
```

### Дефолт (hostess, driver, security, …)

```
[BASE STYLE]
Recruiter mascot in black PROMOSTAFF uniform holding magnifying glass over green
diamond icon and clipboard ВАКАНСИИ with checklist. Three small role cards left side
(helper green, loader orange, supervisor purple) with ОТКРЫТО buttons. Sparkles
only near magnifying glass.
```

---

## Промо (`variant_index` → файл)

Соответствие текстам в `data/channel_promo_texts.json` (слот 0 → 09:00, 1 → 14:00, 2 → 20:00).

| index | Файл | Тема поста |
|-------|------|------------|
| 0 | `promo-categories.png` | Вакансии под вашу роль |
| 1 | `promo-subscribe.png` | Подпишитесь на бота |
| 2 | `promo-premium.png` | Ищете смену / Premium push |

### Промо 0 — категории

```
[BASE STYLE — no ОТКРЫТО vacancy button]
Recruiter pointing at three filter cards: green ХЕЛПЕР, orange ГРУЗЧИК, pink ПРОМОУТЕР
with checkmarks. Headline ВАКАНСИИ ПОД ВАШУ РОЛЬ. Subline выберите категорию в боте.
Badge PROMOSTAFF HUNTER. Marketing promo, not vacancy card.
```

### Промо 1 — подписка

```
[BASE STYLE]
Character pointing at large smartphone showing PROMOSTAFF HUNTER BOT chat with
vacancy list and «Настроить подписку» button. Headline ПОДПИШИТЕСЬ НА БОТА.
Badge push по категориям и метро. Handle @PromostaffHunter_bot.
```

### Промо 2 — Premium

```
[BASE STYLE]
Character with PREMIUM badge on shirt, phone showing instant push «НОВАЯ СМЕНА»
and metro pin. Headline ИЩЕТЕ СМЕНУ? Box PREMIUM — МОМЕНТАЛЬНЫЙ PUSH. Lightning
near phone, tiny sparkles only there.
```

---

## Чеклист перед добавлением новой картинки

1. Квадрат 1:1, текст читается на телефоне.
2. Логотип **PROMOSTAFF** на форме (если уместно).
3. Персонаж **в действии** по роли, не абстрактный маскот (кроме дефолта и промо).
4. Без лишнего конфетти; без узких подписей «только на мероприятиях» для универсальных ролей.
5. Сохранить в `data/channel_images/` и добавить строку в `VACANCY_IMAGE_BY_CATEGORY` или `PROMO_IMAGE_BY_VARIANT` в `services/channel_images.py`.
