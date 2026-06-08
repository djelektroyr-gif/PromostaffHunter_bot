# Промпты для картинок канала @promostaff_agency_job

Единый стиль для вакансий и промо-постов **Promostaff Hunter**.  
Файлы лежат в `assets/channel_images/` (не в `data/` — на Bothost `data/` persistent и не из git). Маппинг — `services/channel_images.py`.

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

**Reference для новых картинок:** `assets/channel_images/vacancy-default.png` (маскот) или ближайшая карточка той же серии.

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

Соответствие текстам в `assets/channel_promo_texts.json` (слот 0 → 09:00, 1 → 14:00, 2 → 20:00).

| index | Файл | Три карточки категорий | Тема поста |
|-------|------|------------------------|------------|
| 0 | `promo-categories.png` | **ПРОМОУТЕР**, **ХОСТЕС**, **АНИМАТОР** | Вакансии под вашу роль |
| 1 | `promo-subscribe.png` | **ОФИЦИАНТ**, **ВОДИТЕЛЬ**, **ОХРАННИК** | Подпишитесь на бота |
| 2 | `promo-premium.png` | **ГАРДЕРОБ**, **ПАРКОВЩИК**, **СУПЕРВАЙЗЕР** | Ищете смену / Premium push |

> **Не дублировать** на всех трёх слотах одну связку «хелпер + грузчик + супервайзер».  
> Хелпер и грузчик — на **вакансийных** карточках `vacancy-helper-*.png`, `vacancy-loader-*.png`.  
> Константа в коде: `PROMO_ROLE_TRIO_BY_VARIANT` в `services/channel_images.py`.

### Промо 0 — категории (09:00)

```
[BASE STYLE — no ОТКРЫТО, no corner circular logo]
Recruiter pointing at three filter cards with checkmarks:
pink ПРОМОУТЕР, white ХОСТЕС, purple АНИМАТОР.
Headline ВАКАНСИИ ПОД ВАШУ РОЛЬ. Subline выберите категорию в боте.
White PROMOSTAFF text patch on cap only — NO extra badge in corner.
```

### Промо 1 — подписка (14:00)

```
[BASE STYLE]
Character pointing at smartphone; three small role chips on screen or beside phone:
blue ОФИЦИАНТ, navy ВОДИТЕЛЬ, grey ОХРАННИК.
Headline ПОДПИШИТЕСЬ НА БОТА. Chat «Настроить подписку».
NO corner circular logo. PROMOSTAFF on cap only.
```

### Промо 2 — Premium (20:00)

```
[BASE STYLE]
Character with PREMIUM diamond badge; three role chips:
teal ГАРДЕРОБ, yellow ПАРКОВЩИК, purple СУПЕРВАЙЗЕР.
Headline ИЩЕТЕ СМЕНУ? Box PREMIUM — МОМЕНТАЛЬНЫЙ PUSH.
NO corner circular logo.
```

### Логотип `promostaff-hunter-logo.png`

| Накладывать склейку | Не накладывать |
|---------------------|----------------|
| `promo-maintenance.png` | все `vacancy-*.png` (PROMOSTAFF на форме) |
| `promo-update-premium-filters.png` | `promo-categories/subscribe/premium` (есть PROMOSTAFF) |

Скрипт: `python scripts/apply_channel_logo.py` (только whitelist). `--force` — на все PNG, не рекомендуется.

---

### Дефолт (fallback, неизвестная категория)

Три варианта `vacancy-default-{1,2,3}.png` — **разные** тройки карточек, не только хелпер/грузчик/супервайзер:

| файл | три карточки |
|------|----------------|
| `-1` | хелпер, грузчик, промоутер |
| `-2` | официант, водитель, охранник |
| `-3` | гардероб, парковщик, аниматор |

---

## Чеклист перед добавлением новой картинки

1. Квадрат 1:1, текст читается на телефоне.
2. Логотип **PROMOSTAFF** на форме (если уместно).
3. Персонаж **в действии** по роли, не абстрактный маскот (кроме дефолта и промо).
4. Без лишнего конфетти; без узких подписей «только на мероприятиях» для универсальных ролей.
5. Сохранить в `assets/channel_images/` и добавить строку в `VACANCY_IMAGE_BY_CATEGORY` или `PROMO_IMAGE_BY_VARIANT` в `services/channel_images.py`.
