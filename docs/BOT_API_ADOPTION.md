# Внедрение новых возможностей Telegram Bot API в Hunter

Последнее обновление: **2026-06-11**

## Зачем этот документ

Telegram регулярно добавляет методы API (черновик сообщения, живое фото, цветные кнопки и т.д.).  
**Бот сам на проде библиотеки не обновляет** — это осознанный процесс: PR → тесты → ручной деплой Bothost.

Цель: не отставать от aiogram и включать фишки **под флагами**, без поломки старого поведения.

---

## Цикл обновления зависимостей

| Шаг | Кто / что |
|-----|-----------|
| 1 | **Dependabot** (`.github/dependabot.yml`) — раз в неделю PR с новыми версиями из `requirements.txt` |
| 2 | Локально или в CI: `pip install -r requirements.txt` + `python -m pytest tests/ -q` |
| 3 | Если тесты зелёные — merge PR |
| 4 | **Ручной деплой** на Bothost (автосборка по push не используется) |
| 5 | Запись в этот файл или в `DEVELOPMENT.md` — что включили и какие env нужны |

### Что не делать

- Не ставить `pip install -U` прямо на проде без тестов.
- Не включать все новые API сразу без флага — канал и LLM должны иметь fallback.

---

## Текущая версия

| Пакет | Версия | Bot API (ориентир) |
|-------|--------|---------------------|
| **aiogram** | **3.28.2** | ~10.0 в типах; **10.1** — raw `sendRichMessage` |

Проверка методов в рантайме: `services/telegram_api_capabilities.py` (`supports_message_draft`, `supports_live_photo`).

**Rich Messages (10.1):** см. **[`BOT_API_RICH_MESSAGES.md`](BOT_API_RICH_MESSAGES.md)** — карточки вакансий, бэклог.

---

## Включённые фичи Hunter

### 1. Черновики LLM — `sendRichMessageDraft` + `sendMessageDraft`

**Env:**

- `LLM_MESSAGE_DRAFT_ENABLED=1` (по умолчанию **включено**)
- `LLM_RICH_MESSAGE_DRAFT_ENABLED=1` (по умолчанию **включено**, Rich 10.1)

**Где:** кнопка «✨ Улучшить текст» → `services/message_draft.py` → `ask_llm_with_draft`, финал в `respond_llm_enhance`

**Поведение:**

1. Над полем ввода — rich-черновик с `<tg-thinking>⏳ Составляю текст…</tg-thinking>` (или plain draft при fallback)
2. После ответа gateway — превью в том же черновике
3. Итог — `sendRichMessage` с заголовком и footer + inline-кнопки (или Markdown fallback)

**Fallback:** rich draft → plain draft → без черновика; финал rich → Markdown `send_user_message_safe_buttons`; при `LLM_MESSAGE_DRAFT_ENABLED=0` — `typing_keepalive` + `ask_llm`.

**Топики:** учитывается `message_thread_id` темы «📨 Отклики».

### 2. `sendLivePhoto` — живое фото в канале

**Env:** `CHANNEL_LIVE_PHOTO_ENABLED=1` (по умолчанию **выключено** — нужны MP4)

**Где:** `services/channel_images.py` → `send_channel_post`

**Ассеты:** к каждому PNG кладётся **короткое видео** с тем же именем:

```
vacancy-promoter-1.png
vacancy-promoter-1.mp4   ← 1–3 сек, лёгкая анимация (Ken Burns, блик и т.п.)
```

Папка: `assets/channel_images/` или `CHANNEL_IMAGES_DIR` на Bothost.

**Fallback:** нет MP4 или ошибка API → обычный `send_photo`.

### 3. Rich Messages — карточки вакансий (10.1)

**Env:** `RICH_VACANCY_CARDS_ENABLED=1` (по умолчанию **включено**, fallback на HTML)

**Где:** push/лена/deeplink → `services/vacancy_card_send.py`

**Док:** [`BOT_API_RICH_MESSAGES.md`](BOT_API_RICH_MESSAGES.md)

### 4. Уже было (tool-v3/v4)

- Цветные inline-кнопки (`style`) — `services/telegram_buttons.py`
- Forum topics, кросс-пост канала, LLM gateway, Stars

---

## Бэклог API (следующие итерации)

| Метод | Bot API | Статус |
|-------|---------|--------|
| `sendRichMessageDraft` | 10.1 (июнь 2026) | ✅ LLM «Улучшить текст» |
| `sendRichMessage` | 10.1 (июнь 2026) | ✅ карточки вакансий Hunter |
| Стриминг токенов LLM в draft | — | Нужен streaming в gateway `/ask` |
| Live photo на промо-постах | 10.0 | Тот же паттерн PNG+MP4 для `promo-*.png` |

При появлении нового метода: строка в таблицу → флаг в `config.py` → хелпер в `services/` → pytest → env в Bothost.

---

## Env Bothost (дополнение к tool-v4)

```env
# aiogram 3.28.2 после деплоя образа с обновлённым requirements.txt
LLM_MESSAGE_DRAFT_ENABLED=1
CHANNEL_LIVE_PHOTO_ENABLED=0   # 1 когда зальёте MP4 рядом с PNG
```

---

## Связанные документы

- [`DEVELOPMENT.md`](DEVELOPMENT.md) — общий конспект
- [`SPRINT_MODERN_UX_LLM_STARS.md`](SPRINT_MODERN_UX_LLM_STARS.md) — спринт modern UX
- [`CHANNEL_IMAGE_PROMPTS.md`](CHANNEL_IMAGE_PROMPTS.md) — генерация обложек канала
