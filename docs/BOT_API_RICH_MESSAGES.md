# Rich Messages — Bot API 10.1 (Promostaff Hunter)

Последнее обновление: **2026-06-11**

Живой конспект по [Rich Messages](https://core.telegram.org/bots/features#rich-messages) и [Bot API 10.1](https://core.telegram.org/bots/api#june-11-2026). Не терять контекст между сессиями.

---

## Что это

**Rich Message** — не «ещё один `parse_mode`», а отдельный тип сообщения:

- Метод: **`sendRichMessage`** (не `sendMessage`)
- Контент: **`InputRichMessage`** — строка **Rich HTML** или **Rich Markdown**
- Редактирование: **`editMessageText`** + поле `rich_message` (без `text`)
- Стриминг LLM: **`sendRichMessageDraft`** + блок `<tg-thinking>`

В ответе в `Message` появляется поле **`rich_message`** (структура `RichMessage` с блоками).

Демо в Telegram: [@RichTextDemoBot](https://t.me/RichTextDemoBot).

---

## Rich HTML vs обычный HTML в sendMessage

| | `sendMessage` + HTML | `sendRichMessage` + Rich HTML |
|--|----------------------|-------------------------------|
| Заголовки | `<b>` | `<h1>`…`<h6>` |
| Таблица ставки/адреса | нет | `<table>` |
| Длинный текст | полотно | `<details><summary>…</summary>` |
| Карта в теле | только ссылка | `<tg-map>` (v2) |
| LLM стрим | `sendMessageDraft` (plain) | `sendRichMessageDraft` |

Синтаксис: [rich message formatting options](https://core.telegram.org/bots/api#rich-message-formatting-options).

---

## Статус в экосистеме Promostaff

| Компонент | Версия / слой | Rich в коде |
|-----------|---------------|-------------|
| **aiogram** | 3.28.2 → Bot API **10.0** | Нет типов для 10.1 |
| **Hunter** | `sendRichMessage` через raw `TelegramMethod` | ✅ карточки вакансий |
| **agency-bot** | HTML/Markdown | ⏳ бэклог (КП, расчёт) |
| **Канал Hunter** | caption на фото | ⏳ отдельный эпик |

**Env Hunter:**

```env
RICH_VACANCY_CARDS_ENABLED=1   # fallback на HTML при ошибке API
LLM_RICH_MESSAGE_DRAFT_ENABLED=1   # sendRichMessageDraft для «Улучшить текст»
```

---

## Карточка вакансии (реализовано)

**Файлы:**

| Файл | Роль |
|------|------|
| `services/vacancy_card_rich.py` | Rich HTML: превью и полная карточка |
| `services/telegram_rich_message.py` | `sendRichMessage`, `editMessageText+rich_message` |
| `services/vacancy_card_send.py` | отправка/редактирование + fallback |
| `main.py` | `send_vacancy_card(card_input=…)`, `_edit_vacancy_card_message` |

**Превью (push, лента):**

- `<h3>` — категория + свежесть
- `<table>` — адрес, смена, ставка
- `<p>` — заголовок и подсказка
- `<footer>` — PromoStaff Hunter

**Полная карточка («Открыть вакансию»):**

- `<details>` — полный текст поста
- таблица фактов
- контактные подсказки (как в HTML-карточке)

**Не в v1:** `<tg-map>` в rich (кнопка «Карта» в inline остаётся).

---

## LLM «Улучшить текст» (реализовано)

**Файлы:** `services/message_draft.py`, `services/telegram_rich_message.py`, `main.py` → `respond_llm_enhance`.

**Поток:**

1. `sendRichMessageDraft` с `<tg-thinking>⏳ Составляю текст…</tg-thinking>` (эфемерный черновик над полем ввода).
2. После LLM — обновление черновика превью (`build_llm_enhanced_preview_rich_html`).
3. Финал — `sendRichMessage` с заголовком, текстом и footer; inline-кнопка «Открыть чат» при deeplink.

**Fallback:** rich draft → plain `sendMessageDraft` → без черновика; финал rich → Markdown `send_user_message_safe_buttons`.

**Env:** `LLM_RICH_MESSAGE_DRAFT_ENABLED=1` (вместе с `LLM_MESSAGE_DRAFT_ENABLED=1`).

---

## Бэклог Rich Messages

| Приоритет | Сценарий | Блоки API |
|-----------|----------|-----------|
| ~~P1~~ | ~~LLM «Улучшить текст»~~ | ✅ `sendRichMessageDraft`, `<tg-thinking>` |
| P2 | Карта в карточке | `<tg-map>` + координаты из enrichment |
| P3 | Админ-дайджест | `<table>`, `<h2>` |
| P4 | agency-bot КП | таблицы, формулы |
| P5 | Канал | rich в посте (если API позволит с media) |

---

## Ограничения

1. **Клиент Telegram** — нужна актуальная версия приложения; на старых rich может не отрисоваться → **HTML fallback** в коде.
2. **aiogram** — ждём релиз с Bot API 10.1 или raw HTTP (как сейчас в Hunter).
3. **Канал** — кросс-пост сейчас `send_photo` + HTML caption; rich для канала — отдельная задача.

---

## Связанные документы

- [`BOT_API_ADOPTION.md`](BOT_API_ADOPTION.md) — цикл обновлений, 10.0 фичи
- [`DEVELOPMENT.md`](DEVELOPMENT.md) — общий конспект Hunter
