# Hunter: сегодня → завтра (1 страница)

> Карта переноса архитектуры по образцам: **AdBot**, **teledigest**, **djinni-telegram-bot**, **job-sentinel**, **newslatter-bot** + локальный **telegram-order-parser**.  
> Без big bang: каждый шаг — отдельный деплой, golden-тесты не ломаем.  
> Связано: [`PARSER_INGEST_ROADMAP.md`](PARSER_INGEST_ROADMAP.md).

---

## Целевой контур

```text
ingest/reader (Telethon) → normalize → staff_gate → classify+scores → store (PG)
                                                              ↓
pipeline (оркестратор) ← match subscriber ← filters / seen / digest
                              ↓
bot/writer (aiogram) — карточки, push, отклик, админка
```

**Правило:** новые правила парсера — только в слой **classify** или **staff_gate**, не в `main.py`.

---

## Таблица: файл сегодня → модуль завтра

| Сегодня (Hunter) | Завтра (пакет / модуль) | Образец | Зачем |
|------------------|-------------------------|---------|--------|
| `parser.py` — Telethon, scan, session (~3700 строк) | `ingest/reader.py` — клиент, чаты, `_process_single_message` | **AdBot** collector, **newslatter-bot** `tg_reader.py` | Ingest отдельно от логики «роль/шум» |
| `parser.py` — normalize, strike, footer | `ingest/normalize.py` | **teledigest** (сырой текст до анализа) | Unicode/зачёркивание не смешивать с категориями |
| `parser.py` — `evaluate_vacancy`, gates, reject | `ingest/staff_gate.py` + reason codes | **telegram-order-parser** stats, wikivibe слои | Каждый reject с кодом; отчёт как `LAST_DEBUG_STATS` |
| `parser.py` — `detect_category`, scores, wide ingest | `ingest/classify.py` + `config/categories.yaml` | **teledigest** LLM optional, **agentic-job-scraper** analyze step | Правила снаружи кода; LLM только `uncertain` |
| `parser.py` — stats, noise report, audit | `ingest/observability.py` | **job-sentinel** `/stats`, **tg-channel-digest** debug API | Владелец видит recall до правок |
| `parser.py` — `_save_parsed_vacancy_block`, dedupe | `ingest/persist.py` | **job-sentinel** adapters → DB | Одна точка записи вакансии |
| `services/vacancy_dedupe.py`, enrichment | `ingest/enrich.py` (уже частично `vacancy_enrichment.py`) | **job-sentinel** dedup after notify | Dedupe не в classify |
| `main.py` — `send_vacancy_to_subscribers`, push | `delivery/push.py` | **AdBot** forward layer | Push = доставка, не парсинг |
| `services/subscriber_match.py`, `push_notify.py`, `feed_loader.py` | `delivery/match.py` | **AdBot** keywords, **teledigest** FTS-идея | Матч на **выдаче**, не reject на входе |
| `services/forum_vacancy_pin.py`, `vacancy_card_send.py` | `delivery/writer.py` | **newslatter-bot** `tg_writer.py` | Bot API, FloodWait, General thread_id |
| `db.py` — categories, sent, subscribers (~4500 строк) | `store/` — `vacancies.py`, `subscribers.py`, `delivery_log.py` | **djinni-telegram-bot** per-user tables | `user_seen_jobs` ≠ «скрыть из ленты навсегда» |
| `main.py` — категории, feed, respond (~8700 строк) | `bot/handlers/` — `feed.py`, `respond.py`, `settings.py`, `admin.py` | **djinni-telegram-bot** commands | `main.py` → только `main()` + роутеры |
| `handlers/premium_filters.py` | `bot/handlers/filters.py` | **djinni** quiet/digest/exclude | Продуктовая модель подписчика |
| `services/push_digest_scheduler.py` | `delivery/digest.py` | **djinni** pending + digest | Уже есть — только вынести |
| `services/response_monetization.py` | `bot/monetization.py` | — | Не трогать до стабильного recall |
| `main.py` — `spawn_background_task`, startup loops | `pipeline.py` | **newslatter-bot** `pipeline.py` | Один оркестратор фоновых задач |
| `tests/test_parser_*.py` (много файлов) | `tests/golden/` + `tests/ingest/` | **job-sentinel** e2e mindset | Эталонные посты **до** новых reject-правил |

---

## Что **не** переписываем (оставляем как есть)

| Модуль | Причина |
|--------|---------|
| `services/forum_topics.py`, `user_reply_keyboard.py` | UX Telegram уже отработан |
| `services/vacancy_card*.py`, rich messages | Bot API 10.1, не ingest |
| `services/llm_client.py` | Второй этап — только после classify-слоя |
| `admin_exports.py`, Stars, employer flow | Вне критического пути «канал → лента» |
| PostgreSQL-схема ядра | Миграции через `init_db()`, без лома прод |

---

## Порядок миграции (без остановки бота)

| Этап | Действие | Критерий готовности |
|------|----------|---------------------|
| **P0** | `ingest/observability.py` + golden `tests/golden/` (50–100 постов) | Recall % по эталону в админке |
| **P1** | Вынести `normalize` + `staff_gate` из `parser.py` | Новые gate-правила только там + тест |
| **P2** | `delivery/match.py` ← subscriber_match + feed_loader | Пустая лента = баг match, не «нет в канале» |
| **P3** | `ingest/classify.py` + YAML категорий | Правка категории без edit 3700 строк |
| **P4** | `bot/handlers/*`, `pipeline.py` | `main.py` < 500 строк |
| **P5** | LLM на `uncertain` (опционально) | Только посты с score < порога |

---

## Два процесса (как AdBot / teledigest)

| Процесс | API | Ответственность |
|---------|-----|-----------------|
| **Ingest worker** | Telethon (user session) | Читает каналы, пишет в БД, stats |
| **Bot worker** | aiogram (Bot API) | Пользователи, push, отклики, админ |

Сейчас оба в одном процессе — **допустимо на Bothost**, но граница модулей должна быть как у двух процессов.

---

## Анти-пatterns (как было → не повторять)

- Правка `detect_category` в `main.py` или в ответ на один Excel-notfit без golden-теста  
- «Reject навсегда» на ingest то, что можно отфильтровать на match (AdBot-модель)  
- Новый push-путь без `forum_vacancy_pin` / thread_id General  
- Тарифные эксперименты до recall ≥ согласованного порога на эталоне  

---

*Обновлять при старте каждого этапа P0–P5. Последняя фиксация: 2026-06-11.*
