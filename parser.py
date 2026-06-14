import re
import os
import logging
import hashlib
from html import escape as escape_html
from difflib import SequenceMatcher
from telethon import TelegramClient, events
from telethon import errors
from telethon.errors.common import TypeNotFoundError
import asyncio
from datetime import datetime, timezone, timedelta
from config import (
    API_ID, API_HASH, get_telegram_session_name, describe_session_search,
    HELPER_KEYWORDS, EXCLUDE_CATEGORIES, STOP_PHRASES,
    HIRING_VERBS, ONE_TIME_JOB_KEYWORDS, PAYMENT_INDICATORS, VACANCY_MAX_AGE_HOURS,
)
from db import (
    get_target_chats, is_message_processed, mark_vacancy_closed,
    get_last_processed_id, update_last_processed_id,
    save_vacancy, mark_message_processed, has_recent_duplicate_vacancy,
    get_recent_open_vacancies_for_dedupe
)
from db_backend import run_db

logger = logging.getLogger(__name__)

_realtime_client = None
_chat_expected_roles: dict[str, set[str]] = {}
_monitored_chat_ids = set()
_resolved_chats_count = 0
_parser_lock = asyncio.Lock()
_last_health_alert = {}
_background_tasks: set[asyncio.Task] = set()
PARSER_POLL_INTERVAL_SEC = 300
PARSER_HEALTH_INTERVAL_SEC = 600
PARSER_RECONNECT_DELAY_SEC = 30
PARSER_SESSION_MISSING_BACKOFF_SEC = 1800
PER_CHAT_SCAN_LIMIT = 120
PARSER_INCREMENTAL_LOOKBACK = max(0, int(os.getenv("PARSER_INCREMENTAL_LOOKBACK", "30")))
AUDIT_SCAN_LIMIT = 20
REJECT_SAMPLES_MAX = 12
PARSER_SCAN_TIMEOUT_SEC = 1200
ENTITY_RESOLVE_TIMEOUT_SEC = 45
PARSER_LABEL = "Парсер групп (Telethon)"
MOSCOW_TZ = timezone(timedelta(hours=3))
_session_config_alert_sent = False


def spawn_background_task(coro) -> asyncio.Task:
    """create_task + ссылка, иначе GC убивает задачу (asyncio docs)."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


class SessionNotConfiguredError(Exception):
    """Сессия Telethon отсутствует или не авторизована — на сервере нельзя вводить телефон интерактивно."""


def session_file_path() -> str:
    return f"{get_telegram_session_name()}.session"


def is_session_file_present() -> bool:
    return os.path.isfile(session_file_path())


async def create_authorized_client() -> TelegramClient:
    """Подключение без input() — только если .session уже авторизован."""
    session_name = get_telegram_session_name()
    path = f"{session_name}.session"
    if not os.path.isfile(path):
        raise SessionNotConfiguredError(
            f"Файл {path} не найден. {describe_session_search()}\n"
            "Bothost: включите «Общее хранилище» и положите *.session в /app/shared "
            "(или один раз в /app — перенесётся автоматически)."
        )
    if not API_ID or not API_HASH:
        raise SessionNotConfiguredError("Задайте API_ID и API_HASH в переменных окружения.")

    logger.info(f"Telethon session: {path}")
    client = TelegramClient(session_name, API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise SessionNotConfiguredError(
            f"Файл {path} есть, но сессия не авторизована. "
            "Пересоздайте локально (интерактивный вход) и снова загрузите на сервер."
        )
    return client

def make_vacancy_id(chat_id: str, message_id: str, dedupe_key: str = None) -> str:
    unique_str = dedupe_key or f"{chat_id}_{message_id}"
    return hashlib.md5(unique_str.encode()).hexdigest()[:16]


def chat_id_aliases(chat_id) -> set:
    """Telethon отдаёт -100…, в difference иногда голый id канала — храним все формы."""
    raw = str(chat_id).strip()
    aliases = {raw}
    if raw.startswith("-100") and len(raw) > 4:
        aliases.add(raw[4:])
    elif raw.isdigit():
        aliases.add(f"-100{raw}")
    return aliases


async def refresh_monitored_chat_ids(client) -> set:
    """Резолвит ссылки из БД в numeric chat_id — без этого Telethon-парсер не видит группы."""
    global _monitored_chat_ids, _resolved_chats_count
    ids = set()
    resolved = 0
    links = await run_db(get_target_chats)
    for link in links:
        try:
            entity = await asyncio.wait_for(
                client.get_entity(link),
                timeout=ENTITY_RESOLVE_TIMEOUT_SEC,
            )
            ids.update(chat_id_aliases(entity.id))
            resolved += 1
            await asyncio.sleep(0.4)
        except asyncio.TimeoutError:
            logger.warning("Таймаут резолва чата %s (%ss)", link, ENTITY_RESOLVE_TIMEOUT_SEC)
        except Exception as e:
            logger.warning(f"Не удалось резолвить чат {link}: {e}")
    _monitored_chat_ids = ids
    _resolved_chats_count = resolved
    logger.info(
        "📡 Мониторинг %s чатов (%s id-алиасов, резолв %s/%s)",
        len(links),
        len(_monitored_chat_ids),
        resolved,
        len(links),
    )
    return ids


def is_chat_monitored(chat_id) -> bool:
    if not _monitored_chat_ids:
        return False
    return bool(chat_id_aliases(chat_id) & _monitored_chat_ids)


# Маркеры закрытой вакансии в тексте поста (не reply).
_CLOSED_LINE_PATTERNS = [
    re.compile(r"^\s*закрыто\b", re.I | re.M),
    re.compile(r"\bзакрыто\s*❌", re.I),
    re.compile(r"\bвакансия\s+закрыт", re.I),
    re.compile(r"\bнабор\s+заверш", re.I),
    re.compile(r"\bне\s+актуальн", re.I),
    re.compile(r"\bкомплект\b", re.I),
    re.compile(r"\bмест\s+нет\b", re.I),
    re.compile(r"\bуже\s+нашли\b", re.I),
    re.compile(r"\bнашли\s+всех\b", re.I),
]
# Короткий reply «закрыто ❌» — отдельно (там ❌ уместен).
_REPLY_CLOSE_MARKERS = [
    "закрыт", "закрыта", "закрыто", "❌", "набор завершён", "вакансия закрыта", "не актуально",
]
_STRIKE_CLOSE_HINT = re.compile(
    r"работа|смена|нужно\s+\d|утра\s+до|\d{1,2}\.\d{1,2}\.\d{2,4}",
    re.I,
)


def is_vacancy_closed_text(text: str) -> bool:
    """Пост с «ЗАКРЫТО» в том же сообщении (частый паттерн в HelpersTeam)."""
    if not text or not text.strip():
        return False
    for pat in _CLOSED_LINE_PATTERNS:
        if pat.search(text):
            return True
    tail = "\n".join(text.strip().splitlines()[-5:])
    if re.search(r"^\s*закрыт[ао]?\s*❌*\s*$", tail, re.I | re.M):
        return True
    return False


def _is_reply_close_text(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    return any(marker in low for marker in _REPLY_CLOSE_MARKERS)


def _entity_is_strike(ent) -> bool:
    if type(ent).__name__ == "MessageEntityStrike":
        return True
    return bool(getattr(ent, "_test_strike", False))


def _extract_strikethrough_text(text: str, entities) -> str:
    if not text or not entities:
        return ""
    chunks = []
    for ent in entities:
        if not _entity_is_strike(ent):
            continue
        start, end = ent.offset, ent.offset + ent.length
        if 0 <= start < len(text):
            chunks.append(text[start:min(end, len(text))])
    return "\n".join(chunks)


def is_strikethrough_closure(text: str, entities=None) -> bool:
    """Закрытие через зачёркивание заголовка/даты (редактирование поста)."""
    struck = _extract_strikethrough_text(text or "", entities)
    if not struck or len(struck.strip()) < 6:
        return False
    if _STRIKE_CLOSE_HINT.search(struck):
        return True
    full_len = max(len(text or ""), 1)
    return len(struck) / full_len >= 0.28


async def _close_vacancy_by_message_id(
    message_id: str, chat_id: str, stats: dict = None,
) -> dict | None:
    vacancy_id, users = await run_db(mark_vacancy_closed, message_id, chat_id)
    if vacancy_id and stats is not None:
        stats["closed_vacancies"] += 1
    if vacancy_id:
        return {"type": "closed", "vacancy_id": vacancy_id, "users": users}
    return None


REJECT_REASON_LABELS = {
    "empty": "пустой текст",
    "unpaid": "без оплаты",
    "service_request": "услуга, не найм",
    "casting": "кастинг/модель",
    "no_hiring": "нет признаков найма",
    "no_payment": "нет оплаты в тексте",
    "no_contact": "нет контакта",
    "excluded_hashtag_role": "роль по хештегу вне профиля",
    "excluded_organizer": "организатор/свадьба",
    "permanent_job": "постоянная/вахта",
    "office_staff_job": "офисная работа (не event-staff)",
    "non_event_labor": "не event-staff (производство/ЖКХ/общепит)",
    "remote_office_job": "удалённая офисная работа",
    "staff_job": "персонал (прошёл gate)",
}


def reject_reason_label(reason: str | None) -> str:
    if not reason:
        return "—"
    if reason in REJECT_REASON_LABELS:
        return REJECT_REASON_LABELS[reason]
    if reason.startswith("stop_phrase:"):
        return f"стоп-фраза: {reason.split(':', 1)[1].strip()}"
    if reason.startswith("excluded_category:"):
        return f"искл. категория: {reason.split(':', 1)[1].strip()}"
    if reason.startswith("quality_gate:"):
        return f"качество ({reason.split(':', 1)[1].strip()})"
    if reason == "ambiguous_category":
        return "роль не определена"
    if reason == "digest_split_required":
        return "digest (разбивается на блоки)"
    return reason


def _chat_bucket(stats: dict | None, chat_title: str) -> dict:
    return stats.setdefault("by_chat", {}).setdefault(
        chat_title,
        {
            "scanned": 0,
            "matched": 0,
            "rejected": 0,
            "role_mismatch": 0,
            "already_sent": 0,
            "old": 0,
            "no_text": 0,
            "reasons": {},
        },
    )


def _record_reject_sample(
    stats: dict | None,
    chat_title: str,
    reason: str | None,
    text: str,
    *,
    category: str | None = None,
):
    if stats is None or not text:
        return
    samples = stats.setdefault("reject_samples", [])
    preview = re.sub(r"\s+", " ", text).strip()[:140]
    if any(s.get("preview") == preview and s.get("chat") == chat_title for s in samples):
        return
    samples.append(
        {
            "chat": chat_title,
            "reason": reason or "—",
            "category": category,
            "preview": preview,
        }
    )
    if len(samples) > REJECT_SAMPLES_MAX:
        del samples[0 : len(samples) - REJECT_SAMPLES_MAX]


def _bump_chat_stat(stats: dict | None, chat_title: str, field: str, reason: str | None = None):
    if stats is None or not chat_title:
        return
    bucket = _chat_bucket(stats, chat_title)
    if field in ("scanned", "matched", "role_mismatch", "already_sent", "old", "no_text"):
        bucket[field] = bucket.get(field, 0) + 1
    elif field == "rejected":
        bucket["rejected"] = bucket.get("rejected", 0) + 1
        if reason:
            bucket["reasons"][reason] = bucket["reasons"].get(reason, 0) + 1


async def refresh_chat_expected_roles_cache(client):
    """chat_id alias → set(category codes) из target_chats.expected_roles."""
    global _chat_expected_roles
    from db import list_target_chats

    _chat_expected_roles = {}
    for row in await run_db(list_target_chats):
        if not row.get("is_active"):
            continue
        raw = row.get("expected_roles") or ""
        roles = {p.strip() for p in raw.split(",") if p.strip()}
        if not roles:
            continue
        entity = await safe_get_entity(client, row["chat_link"])
        if not entity:
            continue
        for alias in chat_id_aliases(entity.id):
            _chat_expected_roles[alias] = roles


def format_parser_wait_message(stats: dict | None = None) -> str:
    """Текст ожидания для админ-кнопки «Ручная проверка», пока lock занят."""
    s = stats or LAST_DEBUG_STATS
    kind = s.get("run_kind") or "scan"
    labels = {
        "startup": "стартовая синхронизация",
        "manual": "ручная проверка",
        "periodic": "плановая проверка",
    }
    label = labels.get(kind, kind)
    chats_ok = s.get("chats_ok") or 0
    chats_total = s.get("chats_total") or 0
    scanned = s.get("messages_scanned") or 0
    matched = s.get("matched") or 0
    return (
        "🔍 *Ожидание парсера…*\n\n"
        f"Идёт *{label}*: чаты {chats_ok}/{chats_total}, "
        f"сообщений {scanned}, в ленту {matched}.\n"
        "При ~36 чатах полный прогон обычно *5–15 мин* после перезапуска."
    )


def format_scan_finished_summary(stats: dict | None = None) -> str:
    """Краткий итог завершённого прогона (startup/periodic/manual/audit)."""
    s = stats or LAST_DEBUG_STATS
    kind = s.get("run_kind") or "scan"
    labels = {
        "startup": "Стартовая синхронизация",
        "periodic": "Плановая проверка",
        "manual": "Ручная проверка",
        "audit": "Аудит фильтра",
    }
    label = labels.get(kind, "Прогон парсера")
    err = s.get("error")
    err_line = f"\n❌ Ошибка: {err}" if err else ""
    chats_ok = s.get("chats_ok") or 0
    chats_total = s.get("chats_total") or 0
    scanned = s.get("messages_scanned") or 0
    chats_line = f"Чаты: {chats_ok}/{chats_total}\n" if chats_total else ""
    hint = ""
    if kind in ("manual", "periodic", "startup") and scanned == 0 and chats_total and chats_ok >= chats_total:
        hint = (
            "\n\n_Новых постов после курсора не было — нормально, "
            "если ⚡ realtime уже забрал вакансии._"
        )
    return (
        f"✅ *{label} завершена*{err_line}\n\n"
        f"{chats_line}"
        f"Просмотрено сообщений: {scanned}\n"
        f"В ленту: {s.get('matched', 0)} | отсеяно: {s.get('non_relevant', 0)}\n"
        f"Уже в БД: {s.get('already_sent', 0)} | закрыто: {s.get('closed_vacancies', 0)}"
        f"{hint}\n\n"
        "Детали: «📝 Отчёт парсера» или «📊 Шум по чатам»."
    )


def format_chat_noise_report(stats: dict | None = None) -> str:
    s = stats if stats is not None else get_stats_for_filter_reports()
    by_chat = s.get("by_chat") or {}
    if not by_chat:
        scanned = s.get("messages_scanned") or 0
        if scanned and not s.get("finished_at"):
            chats_ok = s.get("chats_ok") or 0
            chats_total = s.get("chats_total") or 0
            return (
                "📊 *Шум по чатам*\n\n"
                f"Прогон ещё идёт: сообщений {scanned}, чатов {chats_ok}/{chats_total}.\n"
                "Отчёт появится после завершения — или нажмите снова через пару минут."
            )
        if scanned and s.get("finished_at"):
            return (
                "📊 *Шум по чатам*\n\n"
                f"Последний прогон просмотрел {scanned} сообщ., "
                f"но почти все уже были в БД или без текста для фильтра.\n"
                "Шум считается только по сообщениям, прошедшим через фильтр категорий."
            )
        return "📊 *Шум по чатам*\n\nНет данных — запустите «🔍 Ручная проверка» или дождитесь планового прогона."
    lines = ["📊 *Шум по чатам* (последний прогон)", ""]
    ranked = []
    for title, bucket in by_chat.items():
        scanned = bucket.get("scanned") or 0
        matched = bucket.get("matched") or 0
        rejected = bucket.get("rejected") or 0
        total = max(scanned, matched + rejected)
        noise_pct = int(rejected * 100 / total) if total else 0
        ranked.append((noise_pct, title, bucket, total))
    ranked.sort(reverse=True)
    for noise_pct, title, bucket, total in ranked[:12]:
        top_reason = ""
        reasons = bucket.get("reasons") or {}
        if reasons:
            r, c = max(reasons.items(), key=lambda x: x[1])
            top_reason = f", топ: {r} ({c})"
        mismatch = bucket.get("role_mismatch") or 0
        mm = f", вне профиля чата: {mismatch}" if mismatch else ""
        already = bucket.get("already_sent") or 0
        al = f", уже в БД: {already}" if already else ""
        lines.append(
            f"• *{title}* — шум ~{noise_pct}% ({bucket.get('rejected', 0)}/{total}), "
            f"в ленту: {bucket.get('matched', 0)}{mm}{al}{top_reason}"
        )
    lines.append("\nПрофиль чата: `/setchatroles ссылка promoter,helper,loader`")
    return "\n".join(lines)


def format_reject_samples_report(stats: dict | None = None) -> str:
    s = stats if stats is not None else get_stats_for_filter_reports()
    samples = s.get("reject_samples") or []
    if not samples:
        run_kind = s.get("run_kind")
        hint = (
            "Запустите «🔬 Аудит фильтра» — прогонит последние посты из каждого чата "
            "через фильтр *без сохранения* и покажет, что отсеяно."
        )
        if run_kind and run_kind != "audit":
            hint = (
                "В обычном прогоне примеры копятся только по *новым* сообщениям, прошедшим фильтр.\n"
                + hint
            )
        return (
            f"📋 *Примеры отсева*\n\n"
            "Пока нет отсеянных постов в памяти последнего прогона.\n\n"
            "Это *не* вакансии в ленте — только то, что парсер отбросил.\n"
            f"{hint}"
        )
    lines = [
        "📋 *Примеры отсева* (последний прогон)",
        "",
        "Посты из чатов, которые парсер *не сохранил* в ленту. "
        "Причина в названии строки — фильтр считает, что это не подходящая вакансия.",
        "",
        f"Тип прогона: {s.get('run_kind') or '—'} · показано {len(samples)}",
        "",
    ]
    for i, sample in enumerate(reversed(samples), 1):
        label = reject_reason_label(sample.get("reason"))
        cat = sample.get("category")
        cat_part = f" → `{cat}`" if cat else ""
        lines.append(f"{i}. *{sample.get('chat', '—')}* — {label}{cat_part}")
        lines.append(f"   _{sample.get('preview', '')}_")
        lines.append("")
    lines.append("Подсказка: если видите ложный отсев — пришлите номер примера, поправим фильтр.")
    return "\n".join(lines).strip()


def format_channel_coverage_report(stats: dict | None, db_counts: dict[str, int] | None = None) -> str:
    """Сводка: откуда реально идут вакансии (БД) + что дал последний прогон."""
    s = stats if stats is not None else get_stats_for_filter_reports()
    by_chat = s.get("by_chat") or {}
    db_counts = db_counts or {}
    all_titles = set(by_chat.keys()) | set(db_counts.keys())
    if not all_titles and not s.get("started_at"):
        return (
            "📡 *Покрытие каналов*\n\n"
            "Нет данных. Запустите «🔬 Аудит фильтра» или дождитесь прогона."
        )

    rows = []
    for title in all_titles:
        b = by_chat.get(title, {})
        db_n = db_counts.get(title, 0)
        matched = b.get("matched") or 0
        rejected = b.get("rejected") or 0
        scanned = b.get("scanned") or 0
        already = b.get("already_sent") or 0
        rows.append((db_n, matched, rejected, scanned, already, title, b))

    rows.sort(key=lambda x: (x[0], x[1]), reverse=True)

    lines = [
        "📡 *Покрытие каналов*",
        "",
        "*За 7 дней в БД* — сколько вакансий сохранено из чата.",
        "*Последний прогон* — что парсер увидел в *новых* постах (incremental).",
        "Если «прогон 0» — новых сообщений не было; realtime или прошлый прогон уже забрали.",
        "",
    ]

    active_db = [r for r in rows if r[0] > 0]
    if active_db:
        lines.append(f"*Дают вакансии ({len(active_db)} чатов):*")
        for db_n, matched, rejected, scanned, already, title, b in active_db[:14]:
            scan_part = (
                f"прогон: +{matched} в ленту, {rejected} отсеяно, {already} уже в БД"
                if scanned
                else "прогон: новых постов не было"
            )
            lines.append(f"• <b>{escape_html(title)}</b> — БД <b>{db_n}</b>, {escape_html(scan_part)}")
        lines.append("")

    silent_db = [r for r in rows if r[0] == 0 and r[3] == 0]
    if silent_db:
        lines.append(f"*Молчат в БД ({len(silent_db)} чатов, 0 вакансий за 7 д):*")
        for *_, title, _b in silent_db[:8]:
            lines.append(f"  • {title}")
        if len(silent_db) > 8:
            lines.append(f"  … и ещё {len(silent_db) - 8}")
        lines.append("")
        lines.append(
            "Частые причины: чат не про hiring-фильтр, нет доступа (❌ в списке чатов), "
            "или там редко постят подходящие роли. «🔬 Аудит фильтра» покажет отсев по последним постам."
        )

    if s.get("run_kind") == "audit" and by_chat:
        lines.append("")
        lines.append("*Аудит (последние посты, без сохранения):*")
        audit_rows = sorted(
            by_chat.items(),
            key=lambda x: (x[1].get("matched") or 0, x[1].get("rejected") or 0),
            reverse=True,
        )
        for title, b in audit_rows[:10]:
            if not (b.get("scanned") or 0):
                continue
            lines.append(
                f"• {title}: в ленту {b.get('matched', 0)}, "
                f"отсеяно {b.get('rejected', 0)} из {b.get('scanned', 0)}"
            )

    return "\n".join(lines)


def _flatten_parser_result(result):
    """Один order, список orders (digest) или closed — в список для dispatch."""
    if not result:
        return []
    if isinstance(result, list):
        return [r for r in result if r]
    return [result]


async def _save_parsed_vacancy_block(
    *,
    block_text: str,
    message,
    chat,
    chat_id: str,
    chat_title: str,
    message_id: str,
    poster: dict,
    block_index: int | None,
    stats: dict | None,
) -> dict | None:
    """Один блок текста → evaluate, dedupe, save_vacancy, order для push."""
    eval_text = block_text
    if block_index is not None and message.text:
        eval_text = enrich_digest_block(block_text, message.text)
    accepted, category, reason, keywords = evaluate_vacancy(eval_text, poster)
    if stats is not None:
        stats["reasons"][reason] = stats["reasons"].get(reason, 0) + 1
    if not accepted or not category:
        _bump_chat_stat(stats, chat_title, "rejected", reason)
        _record_reject_sample(stats, chat_title, reason, eval_text)
        return None

    expected = _chat_expected_roles.get(str(chat_id)) or _chat_expected_roles.get(chat_id)
    if expected and category not in expected:
        _bump_chat_stat(stats, chat_title, "role_mismatch")

    if stats is not None:
        stats["categories"][category] = stats["categories"].get(category, 0) + 1

    cleaned_text = clean_message_text(eval_text)
    message_link = get_message_link(chat.id, message.id)
    author_contact, contact_source = resolve_vacancy_contact(eval_text, poster)
    if not author_contact and message.text and message.text != eval_text:
        author_contact, contact_source = resolve_vacancy_contact(message.text, poster)
    address = extract_address_from_text(eval_text) or extract_address_from_text(message.text or "")
    full_text = "\n".join(part for part in (eval_text, message.text or "") if part)
    msg_lat, msg_lon = _extract_message_geo(message)
    from services.vacancy_enrichment import enrich_vacancy_text

    enrichment = enrich_vacancy_text(
        full_text,
        legacy_address=address,
        location_lat=msg_lat,
        location_lon=msg_lon,
    )
    if enrichment.address_normalized:
        address = enrichment.address_normalized
    enrich_kwargs = enrichment.to_db_kwargs()

    dedupe_key = build_vacancy_dedupe_key(cleaned_text, author_contact)

    duplicate_type = await run_db(
        detect_duplicate_type,
        cleaned_text,
        author_contact,
        dedupe_key,
        category,
        chat_title,
    )
    if duplicate_type:
        if stats is not None:
            if duplicate_type in ("exact", "order_number"):
                stats["duplicates_exact"] += 1
            else:
                stats["duplicates_fuzzy"] += 1
        return None

    from db import upsert_employer_from_post

    employer_id = await run_db(
        upsert_employer_from_post,
        telegram_user_id=poster.get("user_id"),
        username=poster.get("username"),
        display_name=poster.get("display_name"),
        contact_text=author_contact,
        contact_source=contact_source,
        category_code=category,
    )

    sub_id = f"{message_id}_b{block_index}" if block_index is not None else message_id
    vacancy_id = make_vacancy_id(chat_id, sub_id, dedupe_key)
    await run_db(
        save_vacancy,
        vacancy_id,
        chat_id,
        chat_title,
        category,
        cleaned_text[:2000],
        message_link,
        author_contact,
        address,
        False,
        dedupe_key,
        message.date.strftime("%Y-%m-%d %H:%M:%S"),
        poster.get("user_id"),
        poster.get("username"),
        poster.get("display_name"),
        contact_source,
        employer_id,
        None,
        "approved",
        **enrich_kwargs,
    )
    _bump_chat_stat(stats, chat_title, "matched")
    if stats is not None:
        stats["matched"] += 1

    return {
        "vacancy_id": vacancy_id,
        "chat_title": chat_title,
        "message_text": cleaned_text,
        "message_link": message_link,
        "category": category,
        "chat_id": chat_id,
        "message_id": message_id,
        "keywords": keywords[:5],
        "reason": reason,
        "author_contact": author_contact,
        "address": address,
        "address_normalized": enrichment.address_normalized,
        "location_lat": enrichment.location_lat,
        "location_lon": enrichment.location_lon,
        "category_code": category,
        "geo_tags": enrichment.geo_tags,
        "rate_hourly": enrichment.rate_hourly,
        "rate_shift": enrichment.rate_shift,
        "rate_effective_hourly": enrichment.rate_effective_hourly,
        "shift_date": enrichment.shift_date,
        "shift_time_start": enrichment.shift_time_start,
        "dedupe_key": dedupe_key,
        "published_at": message.date.strftime("%Y-%m-%d %H:%M:%S"),
        "poster_user_id": poster.get("user_id"),
        "poster_username": poster.get("username"),
        "contact_source": contact_source,
    }


async def _mark_scanned_message(
    message,
    chat_id: str,
    *,
    allow_reprocess: bool = False,
) -> None:
    """Помечает сообщение просмотренным и монотонно двигает курсор incremental-скана."""
    if allow_reprocess or message is None:
        return
    message_id = str(message.id)
    await run_db(mark_message_processed, message_id, chat_id)
    await run_db(update_last_processed_id, chat_id, int(message.id))


async def _process_single_message(
    message, chat, chat_id: str, chat_title: str, stats: dict = None, *, allow_reprocess: bool = False,
):
    """Обрабатывает одно сообщение. Возвращает order, list[order] (digest) или closed."""
    if not message.text:
        if stats is not None:
            stats["no_text"] += 1
            _bump_chat_stat(stats, chat_title, "no_text")
        await _mark_scanned_message(message, chat_id, allow_reprocess=allow_reprocess)
        return None
    message_id = str(message.id)
    if not allow_reprocess and await run_db(is_message_processed, message_id, chat_id):
        if stats is not None:
            stats["already_sent"] += 1
            _bump_chat_stat(stats, chat_title, "already_sent")
        return None
    if not allow_reprocess and not is_message_recent(message.date):
        if stats is not None:
            stats["old_messages"] += 1
            _bump_chat_stat(stats, chat_title, "old")
        await _mark_scanned_message(message, chat_id, allow_reprocess=allow_reprocess)
        return None

    entities = getattr(message, "entities", None)

    if message.is_reply and _is_reply_close_text(message.text):
        original_message = await message.get_reply_message()
        if original_message and original_message.id:
            original_id = str(original_message.id)
            closed = await _close_vacancy_by_message_id(original_id, chat_id, stats)
            if closed:
                return closed

    if is_strikethrough_closure(message.text, entities):
        closed = await _close_vacancy_by_message_id(message_id, chat_id, stats)
        if not allow_reprocess:
            await _mark_scanned_message(message, chat_id, allow_reprocess=allow_reprocess)
        return closed

    if is_vacancy_closed_text(message.text):
        closed = await _close_vacancy_by_message_id(message_id, chat_id, stats)
        if not allow_reprocess:
            await _mark_scanned_message(message, chat_id, allow_reprocess=allow_reprocess)
        return closed

    if allow_reprocess:
        return None

    poster = await extract_poster_info(message)

    if should_split_digest(message.text):
        blocks = split_vacancy_blocks(message.text)[:MAX_DIGEST_BLOCKS]
        if stats is not None:
            stats["digest_posts"] = stats.get("digest_posts", 0) + 1
        orders = []
        digest_cluster_keys: set[str] = set()
        for idx, block in enumerate(blocks):
            cluster_key = _preview_digest_cluster_key(block, message, poster, idx)
            if cluster_key and cluster_key in digest_cluster_keys:
                if stats is not None:
                    stats["duplicates_fuzzy"] = stats.get("duplicates_fuzzy", 0) + 1
                continue
            order = await _save_parsed_vacancy_block(
                block_text=block,
                message=message,
                chat=chat,
                chat_id=chat_id,
                chat_title=chat_title,
                message_id=message_id,
                poster=poster,
                block_index=idx,
                stats=stats,
            )
            if order:
                if cluster_key:
                    digest_cluster_keys.add(cluster_key)
                orders.append(order)
        await _mark_scanned_message(message, chat_id, allow_reprocess=allow_reprocess)
        if stats is not None:
            stats["digest_blocks_saved"] = stats.get("digest_blocks_saved", 0) + len(orders)
            if not orders:
                stats["non_relevant"] += 1
        if not orders:
            return None
        return orders if len(orders) > 1 else orders[0]

    order = await _save_parsed_vacancy_block(
        block_text=message.text,
        message=message,
        chat=chat,
        chat_id=chat_id,
        chat_title=chat_title,
        message_id=message_id,
        poster=poster,
        block_index=None,
        stats=stats,
    )
    if not order:
        if stats is not None:
            stats["non_relevant"] += 1
        await _mark_scanned_message(message, chat_id, allow_reprocess=allow_reprocess)
        return None

    await _mark_scanned_message(message, chat_id, allow_reprocess=allow_reprocess)
    return order


def _audit_record_eval(stats: dict | None, chat_title: str, block_text: str, poster: dict | None = None):
    accepted, category, reason, _ = evaluate_vacancy(block_text, poster)
    if stats is not None:
        stats["messages_scanned"] = stats.get("messages_scanned", 0) + 1
    _bump_chat_stat(stats, chat_title, "scanned")
    if accepted and category:
        _bump_chat_stat(stats, chat_title, "matched")
        if stats is not None:
            stats["matched"] += 1
            stats["categories"][category] = stats["categories"].get(category, 0) + 1
            stats["reasons"][reason] = stats["reasons"].get(reason, 0) + 1
    else:
        _bump_chat_stat(stats, chat_title, "rejected", reason)
        if stats is not None:
            stats["reasons"][reason] = stats["reasons"].get(reason, 0) + 1
            stats["non_relevant"] += 1
        _record_reject_sample(stats, chat_title, reason, block_text, category=category)


async def _audit_evaluate_message(message, chat_title: str, stats: dict | None):
    """Прогон текста через фильтр без записи в БД — для админ-аудита."""
    if not message.text:
        if stats is not None:
            stats["no_text"] += 1
        _bump_chat_stat(stats, chat_title, "no_text")
        return
    poster = await extract_poster_info(message)
    if should_split_digest(message.text):
        if stats is not None:
            stats["digest_posts"] = stats.get("digest_posts", 0) + 1
        for block in split_vacancy_blocks(message.text)[:MAX_DIGEST_BLOCKS]:
            enriched = enrich_digest_block(block, message.text)
            _audit_record_eval(stats, chat_title, enriched, poster)
        return
    _audit_record_eval(stats, chat_title, message.text, poster)


async def _scan_all_chats(
    client,
    limit_per_chat: int = PER_CHAT_SCAN_LIMIT,
    stats: dict = None,
    *,
    incremental: bool = False,
    audit_only: bool = False,
):
    all_results = []
    closed_vacancies_users = []
    target_chats = await run_db(get_target_chats)
    if not target_chats:
        return [], []

    if stats is not None:
        stats["phase"] = "scan"

    for i, chat_link in enumerate(target_chats, 1):
        entity = await safe_get_entity(client, chat_link)
        if not entity:
            if stats is not None:
                stats["chats_failed"] += 1
                stats["errors_by_chat"][chat_link] = stats["errors_by_chat"].get(chat_link, 0) + 1
            continue

        chat_title = getattr(entity, 'title', None) or 'Без названия'
        chat_id = str(entity.id)
        if stats is not None:
            stats["chats_ok"] += 1

        iter_kwargs = {"limit": limit_per_chat}
        if incremental and not audit_only:
            last_id = await run_db(get_last_processed_id, chat_id)
            if last_id:
                lookback = PARSER_INCREMENTAL_LOOKBACK
                iter_kwargs["min_id"] = max(0, last_id - lookback) if lookback else last_id

        if stats is not None:
            run_kind = stats.get("run_kind") or "scan"
        else:
            run_kind = "scan"
        logger.info(
            "📡 %s: чат %s/%s «%s» (incremental=%s, audit=%s)",
            run_kind,
            i,
            len(target_chats),
            chat_title,
            incremental and not audit_only,
            audit_only,
        )

        async for message in client.iter_messages(entity, **iter_kwargs):
            if not audit_only:
                if stats is not None:
                    stats["messages_scanned"] += 1
                    _bump_chat_stat(stats, chat_title, "scanned")
                    if stats["messages_scanned"] % 25 == 0:
                        await asyncio.sleep(0)
            try:
                if audit_only:
                    await _audit_evaluate_message(message, chat_title, stats)
                else:
                    result = await _process_single_message(message, entity, chat_id, chat_title, stats)
                    for item in _flatten_parser_result(result):
                        if item.get("type") == "closed":
                            closed_vacancies_users.append((item["vacancy_id"], item["users"]))
                        else:
                            all_results.append(item)
                            await asyncio.sleep(0.05)
            except Exception as e:
                if stats is not None:
                    stats["errors"] += 1
                    stats["errors_by_chat"][chat_title] = stats["errors_by_chat"].get(chat_title, 0) + 1
                logger.warning(f"⚠️ Пропущено сообщение chat={chat_title} id={getattr(message, 'id', '?')}: {e}")

        await asyncio.sleep(0.3)

    return all_results, closed_vacancies_users

async def _periodic_scan_loop(bot_callback, closed_callback=None):
    await asyncio.sleep(90)
    while _realtime_client and _realtime_client.is_connected():
        stats = _new_stats("periodic")
        try:
            async with _parser_lock:
                _publish_debug_stats(stats)
                logger.info("🔄 Плановая проверка новых вакансий (incremental)...")
                orders, closed_data = await asyncio.wait_for(
                    _scan_all_chats(
                        _realtime_client,
                        limit_per_chat=PER_CHAT_SCAN_LIMIT,
                        stats=stats,
                        incremental=True,
                    ),
                    timeout=PARSER_SCAN_TIMEOUT_SEC,
                )
            _mark_stats_finished(stats)
            if closed_data and closed_callback:
                await closed_callback(closed_data)
            for order in orders:
                bot_callback(order)
            logger.info(
                f"🔄 Плановая проверка: новых вакансий {len(orders)}, "
                f"просмотрено сообщений {stats['messages_scanned']}, "
                f"отсеяно {stats['non_relevant']}, дубли exact/fuzzy "
                f"{stats['duplicates_exact']}/{stats['duplicates_fuzzy']}, "
                f"закрыто {len(closed_data or [])}"
            )
        except asyncio.TimeoutError:
            logger.error(
                "Плановая проверка: таймаут %s с (чатов %s/%s)",
                PARSER_SCAN_TIMEOUT_SEC,
                stats.get("chats_ok"),
                stats.get("chats_total"),
            )
            _mark_stats_finished(stats, error="timeout")
        except Exception as e:
            logger.error(f"Ошибка плановой проверки: {e}", exc_info=True)
            _mark_stats_finished(stats, error=str(e))
        await asyncio.sleep(PARSER_POLL_INTERVAL_SEC)


async def _parser_health_loop(health_notify_callback=None):
    """Следит за online/offline и резолвом чатов; алерт админу не чаще раза в час."""
    await asyncio.sleep(180)
    while True:
        try:
            snap = get_parser_status_snapshot()
            issues = []
            if not snap["online"]:
                issues.append("offline")
            elif snap["active_chats"] and snap.get("resolved_chats", 0) < snap["active_chats"]:
                issues.append(f"unresolved:{snap['active_chats'] - snap.get('resolved_chats', 0)}")

            if issues and _realtime_client and _realtime_client.is_connected():
                if snap.get("resolved_chats", 0) < snap["active_chats"]:
                    try:
                        async with _parser_lock:
                            await refresh_monitored_chat_ids(_realtime_client)
                    except Exception as e:
                        logger.warning(f"Health: не удалось обновить chat_id: {e}")

            if issues and health_notify_callback:
                key = ",".join(issues)
                now = datetime.now(timezone.utc)
                last = _last_health_alert.get(key)
                if not last or (now - last).total_seconds() > 3600:
                    if "offline" in key:
                        text = (
                            f"⚠️ *{PARSER_LABEL} offline*\n\n"
                            f"Бот переподключается автоматически. "
                            f"Если алерт повторяется — проверьте `user_session` и логи."
                        )
                    else:
                        unresolved = snap["active_chats"] - snap.get("resolved_chats", 0)
                        text = (
                            f"⚠️ *Парсер не видит {unresolved} чат(ов)*\n\n"
                            f"В БД: {snap['active_chats']}, резолв: {snap.get('resolved_chats', 0)}.\n"
                            f"Откройте «📋 Список чатов парсинга» или `/listchats`."
                        )
                    try:
                        await health_notify_callback(text)
                    except Exception as e:
                        logger.warning(f"Health notify failed: {e}")
                    _last_health_alert[key] = now
        except Exception as e:
            logger.error(f"Health loop error: {e}", exc_info=True)
        await asyncio.sleep(PARSER_HEALTH_INTERVAL_SEC)

# ===================== ПАРСЕР ГРУПП (TELETHON) =====================

async def start_realtime_listener(bot_callback, closed_callback=None, health_notify_callback=None):
    global _realtime_client, _session_config_alert_sent
    from session_lock import acquire_session_lock, SessionLockError

    try:
        acquire_session_lock()
    except SessionLockError as e:
        logger.error(str(e))
        if health_notify_callback:
            try:
                await health_notify_callback(f"❌ *Не запущен {PARSER_LABEL}*\n\n{e}")
            except Exception:
                pass
        return

    spawn_background_task(_parser_health_loop(health_notify_callback))
    reconnect_delay = PARSER_RECONNECT_DELAY_SEC

    while True:
        try:
            _realtime_client = await create_authorized_client()
            reconnect_delay = PARSER_RECONNECT_DELAY_SEC
            logger.info(f"✅ {PARSER_LABEL} подключён")

            await refresh_monitored_chat_ids(_realtime_client)
            await refresh_chat_expected_roles_cache(_realtime_client)

            async def _dispatch_parser_result(result, chat_title: str, *, edited: bool = False):
                for item in _flatten_parser_result(result):
                    if item.get("type") == "closed":
                        if closed_callback and item.get("users"):
                            await closed_callback([(item["vacancy_id"], item["users"])])
                        if item.get("vacancy_id"):
                            logger.info(
                                f"🔒 {PARSER_LABEL}: закрыта вакансия {item['vacancy_id']} "
                                f"({'редактирование' if edited else 'пост'}) «{chat_title}»"
                            )
                    else:
                        bot_callback(item)
                        logger.info(
                            f"⚡ {PARSER_LABEL}: вакансия [{item.get('category')}] из «{chat_title}»"
                        )

            async def on_new_message(event):
                if not is_chat_monitored(event.chat_id):
                    logger.debug(
                        f"{PARSER_LABEL}: сообщение chat_id={event.chat_id} вне мониторинга"
                    )
                    return

                logger.info(f"⚡ {PARSER_LABEL}: новое сообщение chat_id={event.chat_id}")
                message = event.message
                chat = await event.get_chat()
                chat_id = str(chat.id)
                chat_title = chat.title or "Без названия"

                try:
                    result = await _process_single_message(message, chat, chat_id, chat_title)
                    await _dispatch_parser_result(result, chat_title)
                except Exception as e:
                    logger.warning(f"⚠️ {PARSER_LABEL}: ошибка chat={chat_title}: {e}")

            async def on_edited_message(event):
                if not is_chat_monitored(event.chat_id):
                    return
                message = event.message
                if not message.text:
                    return
                closed_signal = (
                    is_vacancy_closed_text(message.text)
                    or is_strikethrough_closure(message.text, getattr(message, "entities", None))
                )
                if not closed_signal:
                    return
                chat = await event.get_chat()
                chat_id = str(chat.id)
                chat_title = chat.title or "Без названия"
                logger.info(f"✏️ {PARSER_LABEL}: редактирование (закрытие) chat_id={event.chat_id}")
                try:
                    result = await _process_single_message(
                        message, chat, chat_id, chat_title, allow_reprocess=True,
                    )
                    await _dispatch_parser_result(result, chat_title, edited=True)
                except Exception as e:
                    logger.warning(f"⚠️ {PARSER_LABEL}: ошибка edit chat={chat_title}: {e}")

            _realtime_client.add_event_handler(on_new_message, events.NewMessage())
            _realtime_client.add_event_handler(on_edited_message, events.MessageEdited())
            spawn_background_task(_startup_sync(bot_callback, closed_callback))
            spawn_background_task(_periodic_scan_loop(bot_callback, closed_callback))
            await _realtime_client.run_until_disconnected()
        except SessionNotConfiguredError as e:
            logger.error(f"{PARSER_LABEL}: {e}")
            reconnect_delay = PARSER_SESSION_MISSING_BACKOFF_SEC
            if health_notify_callback and not _session_config_alert_sent:
                _session_config_alert_sent = True
                try:
                    await health_notify_callback(
                        f"❌ *{PARSER_LABEL} не запущен*\n\n{e}\n\n"
                        f"Бот (aiogram) работает, вакансии не парсятся.\n"
                        f"Повторная попытка через {PARSER_SESSION_MISSING_BACKOFF_SEC // 60} мин."
                    )
                except Exception:
                    pass
        except EOFError:
            msg = (
                "Telethon запросил телефон интерактивно (EOF) — на сервере нет TTY. "
                f"Загрузите авторизованный `{session_file_path()}`."
            )
            logger.error(f"{PARSER_LABEL}: {msg}")
            reconnect_delay = PARSER_SESSION_MISSING_BACKOFF_SEC
            if health_notify_callback and not _session_config_alert_sent:
                _session_config_alert_sent = True
                try:
                    await health_notify_callback(f"❌ *{PARSER_LABEL}*\n\n{msg}")
                except Exception:
                    pass
        except TypeNotFoundError as e:
            logger.error(
                "%s: Telegram прислал тип, неизвестный этой версии Telethon (%s). "
                "Обычно помогает обновление telethon и перезапуск; "
                "если повторяется — не используйте один .session файл в двух процессах.",
                PARSER_LABEL,
                e,
            )
        except Exception as e:
            logger.error(f"Парсер отключился: {e}", exc_info=True)
        finally:
            if _realtime_client and _realtime_client.is_connected():
                try:
                    await _realtime_client.disconnect()
                except Exception:
                    pass
            _realtime_client = None
            _monitored_chat_ids.clear()

        logger.warning(f"Переподключение {PARSER_LABEL} через {reconnect_delay} с...")
        await asyncio.sleep(reconnect_delay)

async def stop_realtime_listener():
    global _realtime_client
    for task in list(_background_tasks):
        if not task.done():
            task.cancel()
    if _background_tasks:
        await asyncio.gather(*list(_background_tasks), return_exceptions=True)
    if _realtime_client and _realtime_client.is_connected():
        await _realtime_client.disconnect()
        logger.info(f"🛑 {PARSER_LABEL} остановлен")

def get_parser_status_snapshot() -> dict:
    """Быстрый снимок для админ-статистики без повторного resolve всех чатов."""
    active = len(get_target_chats())
    online = bool(_realtime_client and _realtime_client.is_connected())
    return {
        "online": online,
        "active_chats": active,
        "resolved_chats": _resolved_chats_count if online else 0,
        "monitored_aliases": len(_monitored_chat_ids) if online else 0,
        "session_file": is_session_file_present(),
        "scan_in_progress": parser_scan_in_progress(),
    }


def format_parser_status_line(snapshot: dict) -> str:
    if not snapshot.get("session_file"):
        return (
            f"❌ {PARSER_LABEL}: нет файла `{session_file_path()}`\n"
            f"   Загрузите авторизованную сессию на сервер (см. docs/DEVELOPMENT.md §11)"
        )
    if snapshot["online"]:
        line = f"✅ {PARSER_LABEL}: подключён"
    else:
        line = f"⏳ {PARSER_LABEL}: не подключён"
    line += f"\n   Чатов в БД: {snapshot['active_chats']}"
    if snapshot["online"]:
        resolved = snapshot.get("resolved_chats", 0)
        aliases = snapshot.get("monitored_aliases", 0)
        line += f" | резолв: {resolved}/{snapshot['active_chats']}"
        if aliases:
            line += f" ({aliases} id-алиасов Telethon)"
        if resolved < snapshot["active_chats"]:
            line += "\n   ⚠️ Не все чаты резолвятся — см. «Список чатов парсинга»"
        if snapshot.get("scan_in_progress"):
            line += "\n   🔄 Сейчас идёт полный прогон чатов (startup/manual/periodic)"
    return line

# ===================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====================

def _iso_now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def _empty_debug_stats() -> dict:
    return {
        "started_at": None,
        "finished_at": None,
        "chats_total": 0,
        "chats_ok": 0,
        "chats_failed": 0,
        "messages_scanned": 0,
        "already_sent": 0,
        "no_text": 0,
        "non_relevant": 0,
        "matched": 0,
        "errors": 0,
        "reasons": {},
        "errors_by_chat": {},
        "categories": {},
        "old_messages": 0,
        "closed_vacancies": 0,
        "duplicates_exact": 0,
        "duplicates_fuzzy": 0,
        "digest_posts": 0,
        "digest_blocks_saved": 0,
        "run_kind": None,
        "reject_samples": [],
        "phase": None,
        "error": None,
    }


_FINISHED_STATS: dict[str, dict] = {}


def _mark_stats_finished(stats: dict, error: str | None = None) -> None:
    if not stats.get("finished_at"):
        stats["finished_at"] = _iso_now()
    stats["phase"] = "done"
    if error:
        stats["error"] = error
    kind = stats.get("run_kind") or "scan"
    _FINISHED_STATS[kind] = stats


def get_last_finished_stats(run_kind: str) -> dict | None:
    s = _FINISHED_STATS.get(run_kind)
    if s and s.get("finished_at"):
        return s
    return None


def get_stats_for_filter_reports() -> dict:
    """Снимок для детальных отчётов — не затирается стартом incremental-прогона."""
    for kind in ("audit", "manual", "startup", "periodic"):
        s = get_last_finished_stats(kind)
        if not s:
            continue
        if s.get("reject_samples") or s.get("by_chat"):
            return s
        if kind == "audit" and (s.get("messages_scanned") or 0) > 0:
            return s
    for kind in ("manual", "startup", "periodic", "audit"):
        s = get_last_finished_stats(kind)
        if s:
            return s
    return LAST_DEBUG_STATS


def reset_parser_stats_cache() -> None:
    """Сброс архива stats (только тесты)."""
    global LAST_DEBUG_STATS, _FINISHED_STATS
    LAST_DEBUG_STATS = _empty_debug_stats()
    _FINISHED_STATS = {}


def _new_stats(run_kind: str = "scan") -> dict:
    stats = _empty_debug_stats()
    stats["started_at"] = _iso_now()
    stats["run_kind"] = run_kind
    stats["chats_total"] = len(get_target_chats())
    return stats


def parser_scan_in_progress() -> bool:
    """True, пока идёт полный проход чатов (startup / manual / periodic)."""
    return _parser_lock.locked()


def _publish_debug_stats(stats: dict) -> None:
    global LAST_DEBUG_STATS
    LAST_DEBUG_STATS = stats


LAST_DEBUG_STATS = _empty_debug_stats()


async def _startup_sync(bot_callback, closed_callback=None):
    """Фоновая синхронизация после перезапуска — не блокирует realtime-слушатель."""
    stats = _new_stats("startup")
    try:
        async with _parser_lock:
            _publish_debug_stats(stats)
            logger.info("🔄 Стартовая синхронизация вакансий (incremental)...")
            orders, closed_data = await asyncio.wait_for(
                _scan_all_chats(
                    _realtime_client,
                    limit_per_chat=PER_CHAT_SCAN_LIMIT,
                    stats=stats,
                    incremental=True,
                ),
                timeout=PARSER_SCAN_TIMEOUT_SEC,
            )
        _mark_stats_finished(stats)
        if closed_data and closed_callback:
            await closed_callback(closed_data)
        for order in orders:
            bot_callback(order)
        logger.info(
            f"✅ Стартовая синхронизация: {len(orders)} вакансий, "
            f"просмотрено {stats['messages_scanned']}, "
            f"отсеяно {stats['non_relevant']}, "
            f"уже в БД {stats['already_sent']}"
        )
    except asyncio.TimeoutError:
        logger.error("Стартовая синхронизация: таймаут %s с", PARSER_SCAN_TIMEOUT_SEC)
        _mark_stats_finished(stats, error="timeout")
    except Exception as e:
        logger.error(f"Стартовая синхронизация: {e}", exc_info=True)
        _mark_stats_finished(stats, error=str(e))


def get_last_debug_report() -> str:
    s = LAST_DEBUG_STATS
    snap = get_parser_status_snapshot()
    parser_line = format_parser_status_line(snap)

    if not s.get("started_at"):
        return (
            "🧪 *Последний прогон парсера*\n\n"
            "Ещё не было завершённого прогона после перезапуска.\n"
            f"{parser_line}\n\n"
            "Запустите `/check_now` или дождитесь плановой проверки (~5 мин)."
        )

    lines = [
        "🧪 *Последний прогон парсера*",
        f"Тип: {s.get('run_kind') or '—'}",
        f"Старт: {s.get('started_at')}",
        f"Финиш: {s.get('finished_at') or '⏳ в процессе…'}",
        parser_line,
        f"Фаза: {s.get('phase') or ('scan' if parser_scan_in_progress() else '—')}",
        f"Чатов: {s.get('chats_ok', 0)}/{s.get('chats_total', 0)} успешно, ошибок: {s.get('chats_failed', 0)}",
        f"Сообщений просмотрено: {s.get('messages_scanned', 0)}",
        f"Совпадений найдено: {s.get('matched', 0)}",
        f"Отсеяно: {s.get('non_relevant', 0)} | без текста: {s.get('no_text', 0)} | "
        f"уже обработано: {s.get('already_sent', 0)} | старых: {s.get('old_messages', 0)} | "
        f"закрыто: {s.get('closed_vacancies', 0)}",
        f"Дубли: exact={s.get('duplicates_exact', 0)} | fuzzy={s.get('duplicates_fuzzy', 0)}",
        f"Digest: постов {s.get('digest_posts', 0)}, сохранено блоков {s.get('digest_blocks_saved', 0)}",
        f"Локальных ошибок: {s.get('errors', 0)}",
    ]

    scanned = s.get("messages_scanned", 0)
    run_kind = s.get("run_kind") or ""
    if scanned == 0 and run_kind in ("periodic", "manual", "startup", "check_now"):
        lines.append(
            "\nℹ️ *Нули в incremental — нормально:* парсер смотрит только посты "
            "после курсора `last_processed` (с lookback "
            f"{PARSER_INCREMENTAL_LOOKBACK} сообщ.). Если новых не было — 0. "
            "Вакансии могли прийти через realtime — в логах `⚡ новое сообщение`."
        )

    if not s.get("finished_at"):
        try:
            started = datetime.strptime(s["started_at"], "%Y-%m-%d %H:%M:%S")
            age_min = (datetime.now() - started).total_seconds() / 60
            if not parser_scan_in_progress() and age_min > 2:
                lines.append(
                    f"\n⚠️ *Прогон без финиша*, но lock свободен ({int(age_min)} мин назад) — "
                    "отчёт мог быть перезаписан новым прогоном. Смотрите логи Bothost."
                )
            elif parser_scan_in_progress() and age_min > 15:
                lines.append(
                    f"\n⚠️ *Прогон «в процессе» уже {int(age_min)} мин* — "
                    "возможно зависание Telethon. После деплоя fix — перезапуск или `/check_now`."
                )
        except ValueError:
            pass
    err = s.get("error")
    if err:
        lines.append(f"\n❌ Ошибка прогона: `{err}`")
    categories = s.get("categories") or {}
    if categories:
        lines.append("\n📊 *Распределение по категориям:*")
        for cat, count in sorted(categories.items(), key=lambda x: x[1], reverse=True):
            lines.append(f"  • {cat}: {count}")
    reasons = s.get("reasons") or {}
    if reasons:
        top = sorted(reasons.items(), key=lambda x: x[1], reverse=True)[:8]
        lines.append("\n📋 *Топ причин фильтра:*")
        for reason, count in top:
            lines.append(f"  • {reject_reason_label(reason)} (`{reason}`): {count}")
    samples = s.get("reject_samples") or []
    if samples:
        lines.append(f"\n📋 Примеры отсева: {len(samples)} шт. — кнопка «📋 Примеры отсева»")
    chat_errors = s.get("errors_by_chat") or {}
    if chat_errors:
        lines.append("\n⚠️ Ошибки по чатам:")
        for chat, count in sorted(chat_errors.items(), key=lambda x: x[1], reverse=True)[:5]:
            lines.append(f"  • {chat}: {count}")
    by_chat = s.get("by_chat") or {}
    if by_chat:
        lines.append("\n📊 *Шум по чатам (топ):*")
        ranked = []
        for title, bucket in by_chat.items():
            total = (bucket.get("scanned") or 0) or (
                (bucket.get("matched") or 0) + (bucket.get("rejected") or 0)
            )
            if not total:
                continue
            noise = int((bucket.get("rejected") or 0) * 100 / total)
            ranked.append((noise, title, bucket))
        for noise, title, bucket in sorted(ranked, reverse=True)[:5]:
            lines.append(
                f"  • {title}: шум {noise}%, в ленту {bucket.get('matched', 0)}"
            )
    return "\n".join(lines)

def _is_maps_url(url: str) -> bool:
    u = (url or "").lower()
    return any(
        x in u
        for x in (
            "yandex.ru/maps", "yandex.com/maps", "google.com/maps", "maps.google",
            "go.yandex", "2gis.ru",
        )
    )


def _is_application_url(url: str) -> bool:
    if not url or _is_maps_url(url):
        return False
    u = url.lower()
    return any(
        d in u
        for d in (
            "airtable.com", "forms.gle", "docs.google.com/forms", "forms.google.com",
            "typeform.com", "jotform.com", "anketolog.ru", "surveymonkey.com",
            "tally.so", "notion.site", "leadplan.ru", "formdesigner.ru",
        )
    )


_APPLY_LABEL_RE = re.compile(
    r"заявк|отклик|анкет|apply|register|регистр|подать",
    re.IGNORECASE,
)
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\((https?://[^)]+)\)", re.IGNORECASE)


def _extract_application_url(text: str) -> str | None:
    """Форма заявки (Airtable, Google Forms, …) — приоритетнее @ канала в footer."""
    if not text:
        return None
    md_links = _MD_LINK_RE.findall(text)
    for label, url in md_links:
        url = url.strip()
        if _is_application_url(url) and _APPLY_LABEL_RE.search(label):
            return url
    for _label, url in md_links:
        url = url.strip()
        if _is_application_url(url):
            return url
    for m in re.finditer(r"https?://[^\s\])<>\"']+", text):
        url = m.group(0).rstrip(".,;")
        if _is_application_url(url):
            return url
    return None


def pick_employer_contact_for_response(saved_contact: str | None, vacancy_text: str | None) -> str | None:
    """Контакт для отклика: перечитываем текст; форма заявки важнее сохранённого @ канала."""
    extracted = extract_contact_from_text(vacancy_text or "")
    saved = (saved_contact or "").strip() or None
    if extracted and _is_application_url(extracted):
        return extracted
    if saved and _is_application_url(saved):
        return saved
    if extracted:
        return extracted
    return saved


def extract_contact_from_text(text: str) -> str:
    if not text:
        return None
    apply_url = _extract_application_url(text)
    if apply_url:
        return apply_url
    md_user = re.search(r"\]\(tg://user\?id=(\d+)\)", text, re.IGNORECASE)
    if md_user:
        return f"tg://user?id={md_user.group(1)}"
    user_link = re.search(r"tg://user\?id=(\d+)", text, re.IGNORECASE)
    if user_link:
        return f"tg://user?id={user_link.group(1)}"
    resolve_match = re.search(r'tg://resolve\?domain=([a-zA-Z0-9_]{5,32})', text, re.IGNORECASE)
    if resolve_match:
        return f"@{resolve_match.group(1)}"
    wa_match = re.search(r'(?:https?://)?wa\.me/(\d{5,15})', text, re.IGNORECASE)
    if wa_match:
        return f"https://wa.me/{wa_match.group(1)}"
    api_wa = re.search(r'(?:https?://)?api\.whatsapp\.com/send\?phone=(\d{5,15})', text, re.IGNORECASE)
    if api_wa:
        return f"https://wa.me/{api_wa.group(1)}"
    username_match = re.search(r'@([a-zA-Z0-9_]{5,32})', text)
    if username_match:
        return username_match.group(0)
    tg_link_match = re.search(r't\.me/([a-zA-Z0-9_]+)', text)
    if tg_link_match:
        return f"@{tg_link_match.group(1)}"
    phone_match = re.search(r'(\+7|8)[\s\-]?\(?(\d{3})\)?[\s\-]?(\d{3})[\s\-]?(\d{2})[\s\-]?(\d{2})', text)
    if phone_match:
        return phone_match.group(0)
    ls_match = re.search(r'[вВ] [лЛ][сС] @?([a-zA-Z0-9_]+)', text)
    if ls_match:
        return f"@{ls_match.group(1)}"
    return None

def _extract_message_geo(message) -> tuple[float | None, float | None]:
    geo = getattr(message, "geo", None)
    if geo is not None:
        try:
            return float(geo.lat), float(geo.long)
        except (TypeError, ValueError, AttributeError):
            pass
    media = getattr(message, "media", None)
    if media is not None:
        point = getattr(media, "geo", None)
        if point is not None:
            try:
                return float(point.lat), float(point.long)
            except (TypeError, ValueError, AttributeError):
                pass
    return None, None

def extract_address_from_text(text: str) -> str:
    if not text:
        return None
    from services.vacancy_enrichment import extract_address_normalized

    return extract_address_normalized(text)


_ORDER_NUMBER_RE = re.compile(
    r"(?:автопост\s*)?[№#]\s*(\d{4,7})\b",
    re.IGNORECASE,
)


def _extract_order_numbers(text: str) -> set[str]:
    if not text:
        return set()
    return {m.group(1) for m in _ORDER_NUMBER_RE.finditer(text)}


def _extract_telegram_usernames(text: str, author_contact: str | None = None) -> set[str]:
    names = {m.lower() for m in re.findall(r"@([a-zA-Z0-9_]{4,32})", text or "")}
    contact = (author_contact or "").strip().lower()
    if contact.startswith("@"):
        names.add(contact[1:])
    return names


def _normalize_for_dedupe(text: str) -> str:
    if not text:
        return ""
    normalized = text.lower()
    normalized = re.sub(r"❌\s*закрыто", " ", normalized)
    normalized = re.sub(r"(?:авто)?post\s*№?\s*\d+", " ", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"[№#]\s*\d{4,7}", " ", normalized)
    normalized = re.sub(r"создано\s+заказов\s*:\s*\d+", " ", normalized)
    normalized = re.sub(r"зарегистрирован\s*:\s*\d+", " ", normalized)
    normalized = re.sub(r"⭐️+vip⭐️+", " ", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r'https?://\S+|t\.me/\S+', ' ', normalized)
    normalized = re.sub(r'@\w+', ' ', normalized)
    normalized = re.sub(r'[\W_]+', ' ', normalized, flags=re.UNICODE)
    return re.sub(r'\s+', ' ', normalized).strip()


def _normalize_for_fuzzy_dedupe(text: str) -> str:
    """Убирает дату/адрес — ловит повторы одной кампании с разными локациями."""
    normalized = _normalize_for_dedupe(text)
    normalized = re.sub(
        r"\b\d{1,2}[\.\-/]\d{1,2}(?:[\.\-/]\d{2,4})?\b",
        " ",
        normalized,
    )
    normalized = re.sub(
        r"\b(завтра|сегодня|послезавтра|метро|м\.|ул\.|улица|проспект|пр\.)\b",
        " ",
        normalized,
    )
    normalized = re.sub(
        r"\b(москва|мо|подмосков|лобня|немчиновка|калужская|русаковская|победы)\b",
        " ",
        normalized,
    )
    return re.sub(r"\s+", " ", normalized).strip()


def build_vacancy_dedupe_key(text: str, author_contact: str) -> str:
    normalized_text = _normalize_for_dedupe(text)[:280]
    normalized_contact = (author_contact or "").strip().lower()
    payload = f"{normalized_contact}|{normalized_text}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:24]

def _extract_phone_digits(text: str) -> str:
    if not text:
        return None
    match = re.search(r'(\+7|8)[\s\-]?\(?(\d{3})\)?[\s\-]?(\d{3})[\s\-]?(\d{2})[\s\-]?(\d{2})', text)
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(0))
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return digits if len(digits) == 11 else None

def _extract_campaign_fingerprint(text: str) -> str | None:
    from services.vacancy_dedupe import extract_campaign_fingerprint

    return extract_campaign_fingerprint(text)


def _extract_headline_fingerprint(text: str) -> str | None:
    from services.vacancy_dedupe import extract_headline_fingerprint

    return extract_headline_fingerprint(text)


def find_cluster_vacancy_ids(
    text: str,
    author_contact: str | None,
    category_code: str | None,
    *,
    exclude_id: str | None = None,
    max_age_days: int = 3,
) -> list[str]:
    """Открытые вакансии того же кластера (cross-channel headline/campaign/fuzzy)."""
    from services.vacancy_dedupe import find_cluster_vacancy_ids as _find_ids

    recent = get_recent_open_vacancies_for_dedupe(max_age_days=max_age_days, limit=400)
    return _find_ids(
        text,
        author_contact,
        category_code,
        recent,
        exclude_id=exclude_id,
        normalize_for_dedupe=_normalize_for_dedupe,
        normalize_for_fuzzy_dedupe=_normalize_for_fuzzy_dedupe,
        extract_order_numbers=_extract_order_numbers,
        extract_phone_digits=_extract_phone_digits,
        extract_telegram_usernames=_extract_telegram_usernames,
    )


def detect_duplicate_type(
    text: str,
    author_contact: str,
    dedupe_key: str,
    category_code: str | None = None,
    source_chat_title: str | None = None,
) -> str | None:
    if has_recent_duplicate_vacancy(dedupe_key, max_age_days=1):
        return "exact"
    normalized_text = _normalize_for_dedupe(text)
    fuzzy_text = _normalize_for_fuzzy_dedupe(text)
    if not normalized_text:
        return None
    from services.vacancy_dedupe import cluster_duplicate_reason, extract_campaign_fingerprint

    order_numbers = _extract_order_numbers(text)
    phone_digits = _extract_phone_digits(text)
    usernames = _extract_telegram_usernames(text, author_contact)
    campaign_fp = extract_campaign_fingerprint(text)
    headline_fp = _extract_headline_fingerprint(text)
    recent = get_recent_open_vacancies_for_dedupe(max_age_days=1, limit=250)
    for row in recent:
        reason = cluster_duplicate_reason(
            text,
            author_contact,
            category_code,
            row,
            normalized_text=normalized_text,
            fuzzy_text=fuzzy_text,
            order_numbers=order_numbers,
            phone_digits=phone_digits,
            usernames=usernames,
            campaign_fp=campaign_fp,
            headline_fp=headline_fp,
            extract_order_numbers=_extract_order_numbers,
            extract_phone_digits=_extract_phone_digits,
            extract_telegram_usernames=_extract_telegram_usernames,
            normalize_for_dedupe=_normalize_for_dedupe,
            normalize_for_fuzzy_dedupe=_normalize_for_fuzzy_dedupe,
        )
        if reason:
            return reason
    return None

def is_message_recent(message_dt: datetime, max_age_hours: int = None) -> bool:
    """Вакансия не старше max_age_hours (по умолчанию VACANCY_MAX_AGE_HOURS)."""
    if not message_dt:
        return False
    hours = max_age_hours if max_age_hours is not None else VACANCY_MAX_AGE_HOURS
    if message_dt.tzinfo is None:
        message_dt = message_dt.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - message_dt.astimezone(timezone.utc)
    return age <= timedelta(hours=hours)


def is_message_for_today(message_dt: datetime) -> bool:
    """Обратная совместимость — делегирует в is_message_recent."""
    return is_message_recent(message_dt)

def _normalize_metro_token(value: str) -> str:
    if not value:
        return ""
    cleaned = re.sub(r"[^\w\s\-]", "", value.lower())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    for prefix in ("станция ", "м ", "м."):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):].strip()
    return cleaned


def extract_metro_tokens(text: str) -> list:
    """Станции метро из текста вакансии (нормализованные)."""
    if not text:
        return []
    tokens = set()
    addr = extract_address_from_text(text)
    if addr:
        metro_in_addr = re.search(r"метро\s+([^\n,]{2,50})", addr, re.IGNORECASE)
        if metro_in_addr:
            tokens.add(_normalize_metro_token(metro_in_addr.group(1)))
    for match in re.finditer(
        r"(?:Ⓜ️\s*|м\.|метро)\s*[:\-]?\s*(?:🚇\s*)?([А-Яа-яёЁ\-A-Za-z\s]{2,40})",
        text,
        re.IGNORECASE,
    ):
        token = _normalize_metro_token(match.group(1))
        if token and len(token) >= 3:
            tokens.add(token)
    return sorted(tokens)


def vacancy_matches_user_metro(vacancy_text: str, address: str, user_metro_csv: str) -> bool:
    """True если у пользователя нет фильтра, в вакансии нет метро, или есть пересечение."""
    if not user_metro_csv or not user_metro_csv.strip():
        return True
    user_zones = [_normalize_metro_token(z) for z in user_metro_csv.split(",") if z.strip()]
    if not user_zones:
        return True
    combined = f"{vacancy_text or ''} {address or ''}"
    vac_tokens = extract_metro_tokens(combined)
    if not vac_tokens:
        return True
    for vt in vac_tokens:
        for uz in user_zones:
            if uz in vt or vt in uz:
                return True
    return False


async def inspect_parser_chats() -> tuple:
    """Проверка доступа Telethon к чатам из БД. Возвращает (список, статус парсера)."""
    from db import list_target_chats

    chats_db = await run_db(list_target_chats)
    if not chats_db:
        return [], "empty"

    if not (_realtime_client and _realtime_client.is_connected()):
        offline = []
        for row in chats_db:
            offline.append({
                **row,
                "status": "parser_offline",
                "title": None,
                "chat_id": None,
                "monitored": False,
            })
        return offline, "offline"

    await refresh_monitored_chat_ids(_realtime_client)
    results = []
    for row in chats_db:
        link = row["chat_link"]
        if not row["is_active"]:
            results.append({**row, "status": "disabled", "title": None, "chat_id": None, "monitored": False})
            continue
        entity = await safe_get_entity(_realtime_client, link)
        if entity:
            chat_id = str(entity.id)
            title = getattr(entity, "title", None) or getattr(entity, "username", None) or link
            results.append({
                **row,
                "status": "ok",
                "title": title,
                "chat_id": chat_id,
                "monitored": is_chat_monitored(entity.id),
            })
        else:
            results.append({**row, "status": "no_access", "title": None, "chat_id": None, "monitored": False})
    return results, "online"

def format_parser_chats_report(chats: list, parser_status: str) -> str:
    status_labels = {
        "online": "✅ подключён",
        "offline": "⏳ ещё не подключён (перезапустите или подождите)",
        "empty": "📭 чатов нет",
    }
    lines = [
        f"💬 <b>Чаты парсинга</b> ({escape_html(PARSER_LABEL)})",
        f"Статус: {escape_html(status_labels.get(parser_status, parser_status))}",
        "",
    ]
    if not chats:
        lines.append("Добавьте чат: <code>/addchat @channel</code>")
        return "\n".join(lines)

    ok = sum(1 for c in chats if c.get("status") == "ok")
    bad = sum(1 for c in chats if c.get("status") in ("no_access", "parser_offline"))
    lines.append(f"Всего: {len(chats)} | ✅ доступ: {ok} | ⚠️ проблемы: {bad}")
    lines.append("")

    icons = {"ok": "✅", "no_access": "❌", "parser_offline": "⏳", "disabled": "🚫"}
    for i, chat in enumerate(chats, 1):
        icon = icons.get(chat.get("status"), "❓")
        title = escape_html(chat.get("title") or "—")
        link = escape_html(chat["chat_link"])
        cid = escape_html(str(chat.get("chat_id") or "—"))
        monitored = "📡" if chat.get("monitored") else "—"
        if chat.get("status") == "disabled":
            lines.append(f"{i}. {icon} <code>{link}</code> (отключён)")
        else:
            lines.append(f"{i}. {icon} <b>{title}</b> {monitored}")
            lines.append(f"   <code>{link}</code> → id <code>{cid}</code>")
    lines.append("")
    lines.append("Добавить: <code>/addchat @channel</code> · Удалить: <code>/removechat</code>")
    return "\n".join(lines)

def _keyword_in_text(keyword: str, text_lower: str) -> bool:
    """Проверка ключевого слова с границами — «паковщик» не в упаковщик, «staff» отдельным словом."""
    kw = keyword.lower()
    if kw in ("промо", "склад", "сервис", "staff"):
        pattern = rf'(?<![a-zа-яё0-9]){re.escape(kw)}(?![a-zа-яё0-9])'
        return bool(re.search(pattern, text_lower, re.IGNORECASE))
    if len(kw) <= 5 or kw in _BOUNDARY_KEYWORDS:
        pattern = rf'(?<![a-zа-яё0-9]){re.escape(kw)}(?![a-zа-яё0-9])'
        return bool(re.search(pattern, text_lower, re.IGNORECASE))
    return kw in text_lower


_BOUNDARY_KEYWORDS = frozenset({
    "водитель", "водители", "курьер", "экспедитор",
})

_CATEGORY_TIEBREAK = (
    "loader", "handyman", "promoter", "hostess", "waiter", "animator", "wardrobe",
    "driver", "security", "parking", "supervisor", "helper",
)

_CATEGORY_KEYWORDS = {
    "loader": [
        "грузчик", "грузчики", "подсобник", "подсобный рабочий",
        "погрузка", "разгрузка", "выгрузка", "выгрузк", "разгрузк", "погрузк",
        "такелаж", "такелажник",
        "помощь мастерам", "работа на лесах",
        "выгрузить", "загрузить", "разгрузить", "перемещение фур", "фасовочн", "конвейер",
        "упаковщик", "фасовщик", "комплектовщик", "комплектовка", "упаковка на склад",
        "складской работник", "на склад", "рохл", "паллет", "складирован",
        "фасовоч", "кладовщик",
        "уборка",
        "разбирать", "посадка растений", "декоративные работы", "прораб",
        "погруз", "глины", "тележк",
    ],
    "promoter": [
        "промоутер", "промоутеры", "промоутерша", "промоутером", "промо персонал", "промо",
        "раздача листовок", "раздача визиток", "визиток", "промо-акция", "промоакция", "листовки", "анкетирован",
        "опрос людей", "опрос на улице",
        "привлекать внимание", "приглашать клиентов", "распространение листовок",
        "промо на", "промо в", "позиция: промо", "позиция промо",
    ],
    "hostess": ["хостес", "встреча гостей", "приветствие", "встречать гостей", "администратор ресепшн"],
    "wardrobe": ["гардеробщик", "гардеробщица", "гардероб", "раздевалка", "прием верхней одежды", "выдача номерков"],
    "animator": [
        "аниматор", "аниматоры", "аниматорша", "анимация", "детский праздник", "клоун",
        "ростовые куклы",
    ],
    "waiter": ["официант", "официантка", "официанты", "бармен", "обслуживание гостей", "ресторан", "кафе", "банкет"],
    "driver": ["водитель", "водители", "курьер", "экспедитор"],
    "security": ["охранник", "контролёр", "контролер", "охрана", "секьюрити", "контроль доступа", "пропускной режим"],
    "parking": ["парковщик", "парковка vip", "паркинг", "парковочный"],
    "supervisor": [
        "супервайзер", "супервизор", "тимлид", "старший смены",
        "координатор промо", "координатор проекта", "координатор мероприят",
        "контроль промо-персонала", "контроль промо персонала",
    ],
    "helper": [
        "хелпер", "хэлпер", "хелперы", "хэлперы",
        "требуются хелпер", "требуются хэлпер",
        "хелперский функционал", "хелперские задачи",
        "помощь на монтаже", "на монтаже",
        "парни хелпер", "парня хелпер", "парней хелпер",
        "#техник", "техник прокат", "техник оборудован", "техник концерт",
        "техник на площадке", "техник на монтаже",
        # «мероприят» — намеренный стем: мероприятие / мероприятия / мероприятий
        "хелпер на мероприят", "хэлпер на мероприят",
        "хелпер на мероприятие", "хэлпер на мероприятие",
        "хелперов", "хэлперов", "хелперск", "хэлперск",
        "демонтаж", "монтажник",
        "помощник на мероприятие", "помощник организатора",
        "помощь на площадке", "помощники на площадке",
        "к мероприятию", "деловое мероприятие", "приготовить площадку",
        "бекфотограф", "бэкстейдж", "бэкстейж",
        "ассистент по акт", "ассистент на площадке",
        "волонтер", "волонтёр",
    ],
    "handyman": [
        "разнорабочий", "разнорабочие", "разнорабоч",
        "клининг", "уборщик", "уборщица", "уборщиц", "уборщ",
        "посудомой", "посудомойщик", "посудомойщица", "мойка посуды",
        "котломой", "дворник", "уборка помещен", "генеральная уборка",
        "клинер", "cleaning",
    ],
}

_HELPER_EVENT_HINTS = (
    "мероприят", "площадк", "хелпер", "хэлпер", "регистрац", "конференц",
    "саммит", "summit", "к мероприятию", "демонтаж", "монтажник", "монтажн",
    "на такси на площадку", "монтаж",
)
_HELPER_HEADCOUNT_RE = re.compile(
    r"(?:нужн\w*|требу\w*)\s+\d+\s+х[еэ]лпер|\d+\s+х[еэ]лпер",
    re.I,
)

_LABOR_HINTS = (
    "грузчик", "упаковщик", "фасовщик", "комплектовщик", "разгруз", "погруз", "выгруз",
    "склад", "рохл", "паллет", "фасовоч", "конвейер", "50 кг",
    "подсобн", "посадка", "глины",
)
_HANDYMAN_HINTS = (
    "разнорабоч", "клининг", "уборщ", "посудомой", "котломой", "мойка посуды", "уборк",
)
# В gate грузчика не отсекаем общую «уборку» — её ловит отдельная ветка ниже.
_HANDYMAN_LOADER_REJECT = (
    "разнорабоч", "клининг", "уборщ", "посудомой", "котломой", "мойка посуды",
)
_LOADER_CORE_HINTS = (
    "грузчик", "грузчики", "разгруз", "погруз", "выгруз", "такелаж",
    "склад", "фура", "рохл", "паллет", "комплектовщик", "кладовщик",
    "подсобник", "упаковщик", "фасовщик", "50 кг", "тележк",
    "помощь мастерам", "на лесах", "лесах", "конвейер", "фасовоч",
)
_PROMO_HINTS = (
    "промоутер", "листовок", "визиток", "промо-акция", "раздача листовок",
    "раздача визиток", "промо персонал", "промо", "анкетирован",
)
_NON_SUPERVISOR_COORDINATOR = (
    "организатор", "координатор свад", "свадеб", "#организатора", "координатора",
    "event hunter", "ведущий", "фотограф", "видеограф",
)


MAX_DIGEST_BLOCKS = 12


def split_vacancy_blocks(text: str) -> list:
    """Digest «1. … 2. …» / буллеты — отдельные блоки."""
    if not text:
        return []
    parts = re.split(
        r"(?:(?<=\n)|(?<=^))\s*(?:\d+[\.\)]|[•▪–—\-])\s+",
        text,
    )
    blocks = [p.strip() for p in parts if p.strip()]
    if len(blocks) > 1:
        return blocks
    # «**1. текст» без переноса перед номером
    parts2 = re.split(r"\s*\d+[\.\)]\s+", text, maxsplit=0)
    blocks2 = [p.strip() for p in parts2 if p.strip()]
    if len(blocks2) > 1:
        return blocks2
    return [text]


def enrich_digest_block(block_text: str, full_text: str) -> str:
    """Подмешивает роль/ставку/контакт из шапки digest в блок без своих."""
    from services.post_header_context import enrich_block_with_header_context

    return enrich_block_with_header_context(
        block_text,
        full_text,
        category_scorer=_detect_category_scored,
        payment_checker=has_payment_signal,
        has_payment=has_payment_signal,
        has_contact=lambda t: bool(extract_contact_from_text(t)),
        extract_contact=extract_contact_from_text,
        has_ls_contact=has_ls_contact_phrase,
    )


def _numbered_vacancy_count(text: str) -> int:
    if not text:
        return 0
    if re.search(r"(?:^|\n)\s*2[\.\)]\s", text):
        markers = re.findall(
            r"(?:^|\n|\*{1,4})\s*(\d+)[\.\)]\s+(?!\d{1,2}[\./]\d)",
            text,
        )
        return max(len(markers), 2)
    return len(re.findall(r"(?:^|\n)\s*3[\.\)]\s", text))


def _normalize_hashtag_text(text: str) -> str:
    return re.sub(r"[\*_\s]", "", (text or "").lower())


def _has_technician_role(text_lower: str) -> bool:
    norm = _normalize_hashtag_text(text_lower)
    if "#техник" in norm:
        return True
    return bool(re.search(r"(?:требу|нужен|ищем)\w*\s+.{0,20}техник", text_lower))


def _is_driver_role(text_lower: str) -> bool:
    """Водитель/курьер как основная роль — не «права в плюс» у техника."""
    norm = _normalize_hashtag_text(text_lower)
    if "#водител" in norm or "#курьер" in norm or "#экспедитор" in norm:
        return True
    if re.search(r"(?:требу|нужен|ищем|ищу|набор|ваканс)\w*\s+.{0,25}(?:водител|курьер|экспедитор)", text_lower):
        return True
    if re.search(r"ищ\w+\s+.{0,20}водител", text_lower):
        return True
    if _has_technician_role(text_lower):
        if re.search(
            r"(?:если\s+есть|наличие|есть).{0,25}водительск|водительск\w*\s+прав\w*.{0,35}(?:плюс|желательн)",
            text_lower,
        ):
            return False
    return False


def _score_categories(text_lower: str) -> dict:
    scores = {}
    for category, keywords in _CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if category == "driver" and kw.startswith("водител"):
                if re.search(r"водител\w*\s+забер", text_lower) and "грузчик" in text_lower:
                    continue
            if category == "loader" and kw == "уборка":
                if not any(m in text_lower for m in _SHORT_TERM_SHIFT_MARKERS):
                    continue
                if any(m in text_lower for m in _NON_EVENT_LABOR_MARKERS):
                    continue
            if _keyword_in_text(kw, text_lower):
                scores[category] = scores.get(category, 0) + len(kw)
    if any(marker in text_lower for marker in _NON_SUPERVISOR_COORDINATOR):
        scores.pop("supervisor", None)
    return scores


def _has_explicit_helper_hiring(text_lower: str) -> bool:
    return bool(_HELPER_HEADCOUNT_RE.search(text_lower))


def _pick_category_from_scores(scores: dict, text_lower: str) -> str | None:
    if not scores:
        return None
    if _has_explicit_helper_hiring(text_lower):
        return "helper"
    if _has_technician_role(text_lower) and scores.get("helper"):
        if not _is_driver_role(text_lower):
            return "helper"
    if scores.get("loader") and scores.get("helper"):
        if any(w in text_lower for w in ("хелпер", "хэлпер", "хелперы", "хэлперы")):
            if re.search(r"хелпер\w*\s*[-–—]\s*грузчик", text_lower):
                return "helper"
            if any(w in text_lower for w in _HELPER_EVENT_HINTS):
                return "helper"
        if any(w in text_lower for w in ("хелперский функционал", "хелперские задачи", "хэлперские задачи", "помощь на монтаже", "демонтаж")):
            return "helper"
        if any(w in text_lower for w in _LABOR_HINTS):
            return "loader"
    if scores.get("promoter") and scores.get("helper") and any(w in text_lower for w in _PROMO_HINTS):
        return "promoter"
    if scores.get("loader") and scores.get("parking") and any(w in text_lower for w in _LABOR_HINTS):
        return "loader"
    if scores.get("driver") and scores.get("loader") and "грузчик" in text_lower:
        if re.search(r"водител\w*\s+забер", text_lower) or re.search(
            r"\d+\s*грузчик", text_lower,
        ):
            return "loader"
    if scores.get("driver") and scores.get("helper") and "водител" in text_lower:
        return "driver"
    if scores.get("security") and scores.get("loader"):
        if any(w in text_lower for w in _LOADER_CORE_HINTS) and not any(
            w in text_lower for w in ("охранник", "охрана", "контрол", "секьюрити")
        ):
            return "loader"
    if scores.get("handyman") and scores.get("loader"):
        if any(w in text_lower for w in ("грузчик", "разгруз", "погруз", "фура", "склад", "такелаж")):
            return "loader"
        if any(w in text_lower for w in _HANDYMAN_HINTS):
            return "handyman"
    if scores.get("security") and any(w in text_lower for w in _HANDYMAN_HINTS):
        if not any(w in text_lower for w in ("охранник", "охрана", "контрол", "секьюрити")):
            return "handyman"
    if scores.get("handyman") and any(w in text_lower for w in _HANDYMAN_HINTS):
        return "handyman"
    if scores.get("loader") and scores.get("helper"):
        if any(w in text_lower for w in _HELPER_EVENT_HINTS) and any(
            w in text_lower for w in ("хелпер", "хэлпер", "помощник на мероприят", "к мероприятию")
        ):
            return "helper"

    max_score = max(scores.values())
    winners = [cat for cat, score in scores.items() if score == max_score]
    if len(winners) == 1:
        return winners[0]
    for preferred in _CATEGORY_TIEBREAK:
        if preferred in winners:
            return preferred
    return winners[0]


_STAFF_HIRING_EXTRA = (
    "набор", "на мероприятие", "ищем", "персонал", "комплект", "staff", "бригада",
    "еще 1 рабочий", "ещё 1 рабочий",
)
_PAYMENT_RATE_RE = re.compile(
    r"(?:"
    r"\d[\d\s.,]*\s*(?:руб\.?|₽|р\.?\/?\s*ч)|"
    r"\d{3,4}\s*р/ч|"
    r"\d{3,4}\s*/\s*р\s*/?\s*ч|"
    r"\d{3,5}\s*р(?:\.|\b)(?:\s*\+|\s*за|\s*/|\s|$)|"
    r"₽\/?\s*ч|р\/\s*ч|руб\.?\s*/\s*ч|"
    r"ставка\s*[:\s]?\s*\d|минималка|"
    r"оплат\w*\s*[:\s].*\d|\d[\d\s.,]*\s*(?:₽|руб)|"
    r"\d{2,5}\s*/\s*\d+\s*/\s*\d{2,5}|"
    r"\d{3,5}\s*-\s*\d{3,5}\s*(?:₽|руб|р\b|$)|"
    r"(?:заработок|доход)\s*(?:от\s*)?\d[\d\s–—\-]*(?:до|–|-|—)\s*\d+\s*(?:тыс|тысяч)|"
    r"\d[\d\s.,]*\s*(?:тыс|тысяч)\w*\s*(?:руб|₽)?\s*/\s*день|"
    r"оплат\w*[^\n]{0,40}?\d[\d\s.,]*\s*к\b|"
    r"(?:по\s+)?договор[её]нност"
    r")",
    re.I,
)
_LS_CONTACT_RE = re.compile(
    r"пиш\w*\s+(?:telegram|телеграм|tg|в\s+)?(?:лс|личк)|"
    r"пишите\s+(?:telegram|телеграм|tg|мне\s+)?(?:в\s+)?лс|"
    r"заявки?\s+в\s+лс|"
    r"напишите\s+(?:мне\s+)?(?:в\s+)?лс|"
    r"писать\s+в\s+лс|"
    r"обращайтесь\s+в\s+лс|"
    r"пишите\s+мне|"
    r"в\s+лс\s+пиш|"
    r"личн\w*\s+сообщен|"
    r"для\s+запис\w*|"
    r"записаться\s*:?\s*$",
    re.I | re.M,
)
_SERVICE_REQUEST_RES = (
    re.compile(r"ищу\s+(?:#)?(?:зомби|квест|анимационн|программ|диджей|музыкант|фотограф|ведущ)", re.I),
    re.compile(r"присылайте\s+(?:программу|видео|описание|варианты|прайс|цены)", re.I),
    re.compile(r"бюджет\s+\d+.*(?:ищу|нужен\s+#)", re.I),
    re.compile(r"ищу\s+#\w+\s+на\s+\d", re.I),
    re.compile(r"ищ[ую]\s+#\w", re.I),
    re.compile(
        r"ищ[ую]\s+(?:контактный\s+зоопарк|фотозон|дидже[яй]|видеограф|рилсмейк|мастер-класс\s+для\s+заказчика)",
        re.I,
    ),
    re.compile(r"промо\s+и\s+цены\s+жду", re.I),
    re.compile(r"фото,\s*описание\s+и\s+цену", re.I),
)

_PERMANENT_JOB_MARKERS = (
    "на постоянную работу", "на постоянку", "постоянная работа",
    "на постоянную основу", "постоянную основу",
    "вахта 30", "вахта 60", "вахта 90", "вахта от", "на вахту",
    "оформление по тк", "оформление по тк рф", "по тк рф",
    "официальное оформление", "оплачиваемый отпуск", "больничн",
    "полный пакет документов", "трудовой договор",
    "ежемесячн", "два раза в месяц", "график 5/2", "график 6/1", "6/1 по",
    "набор на вахту", "проживание и питание бесплатно",
    "с заселением", "заселени",
    " тр в месяц", "тр/мес",
)
_NON_EVENT_LABOR_MARKERS = (
    "пекарн", "посудомой", "котломой", "мойщик котл", "мойщик кото",
    "уборщиц", "уборщик", "прачечн", "жкх", "клининг",
    "на производств", "пищевом производств", "общепит",
    "мойка-уборка", "мойка уборка",
)
_MASSOVKA_FILM_MARKERS = (
    "массовк", "на съёмках", "на съемках", "съемки клипа", "съёмки клипа",
    "съемки фильма", "съёмки фильма", "статист", "эпизодник", "на съемки клипа",
)
_DIGITAL_WORK_MARKERS = (
    "удаленн", "удалённ", "со смартфона", "с телефона", "из дома",
    "публиковать материал", "готовый контент", "готовым контентом",
    "работа с телефона", "подработка: помощник",
)
_SHIFT_JOB_MARKERS = (
    "на сегодня", "на завтра", "на сейчас", "срочно",
    "заказ наряд", "разово", "подработк",
)
_SHORT_TERM_SHIFT_MARKERS = _SHIFT_JOB_MARKERS

_PRO_CASTING_MARKERS = (
    "спектакл", "гастрол", "проф. артист", "профессиональный акт",
    "актер в ", "актёр в ", "командировка в ",
)

_ACADEMIC_WRITING_MARKERS = (
    "курсовая", "курсовые", "курсовой", "курсовую",
    "дипломная", "дипломные", "дипломную", "диплом ",
    "диссертац", "магистерск",
    "отчёт по практике", "отчет по практике",
    "типографическ", "переплёт", "переплет", "прошивка", "ламинция",
    "заказать работу", "написание работ", "научная работа",
)

_REMOTE_OFFICE_MARKERS = (
    "#удаленка", "#удалёнка", "удаленка", "удалёнка",
    "дистанционная работа", "полностью дистанцион", "работа удалённо",
    "работать удалённо", "удалённо без постоянного", "из дома",
)
_REMOTE_OFFICE_ROLE_MARKERS = (
    "etsy", "оператор", "ассистент ии", "ии-ассистент", "нейросет",
    "chatgpt", "claude", "маркетплейс", "wildberries", "ozon",
    "оператор-", "оператор ",
)
_EVENT_STAFF_CONTEXT = (
    "мероприят", "хелпер", "хэлпер", "грузчик", "промоутер", "аниматор",
    "официант", "хостес", "на смену", "на сегодня", "на завтра", "разов",
    "смена ", "event staff", "площадке",
)


def is_remote_office_job_spam(text: str) -> bool:
    """Удалённая офисная/online-работа — не event-staff для Hunter."""
    if not text:
        return False
    tl = text.lower()
    has_remote = any(m.replace("#", "") in tl for m in _REMOTE_OFFICE_MARKERS)
    if not has_remote and not any(w in tl for w in ("удален", "удалён", "дистанцион")):
        return False
    has_event = any(w in tl for w in _EVENT_STAFF_CONTEXT)
    if has_event:
        return False
    if any(w in tl for w in _REMOTE_OFFICE_ROLE_MARKERS):
        return True
    if "зарплата фикс" in tl or "з/п" in tl or "зарплат" in tl:
        if has_remote or "дистанцион" in tl:
            return True
    return False


def is_academic_writing_spam(text: str) -> bool:
    """Реклама написания курсовых/дипломов — не вакансия персонала."""
    if not text:
        return False
    tl = text.lower()
    hits = sum(1 for m in _ACADEMIC_WRITING_MARKERS if m in tl)
    if hits >= 2:
        return True
    if hits >= 1 and any(w in tl for w in ("скидка", "новых клиентов", "реферальн", "под ключ")):
        return True
    if "helpers team" in tl.replace(" ", "") or "helpersteam" in tl.replace(" ", ""):
        if hits >= 1 or any(w in tl for w in ("сессия", "зачёт", "экзамен", "дедлайн")):
            return True
    return False


def is_closed_vacancy_post(text: str) -> bool:
    if not text:
        return False
    head = text.lstrip()[:80].lower()
    return head.startswith("❌") and "закрыто" in head


def is_massovka_or_film_extras(text: str) -> bool:
    """Массовка / съёмки — не event-staff аниматор."""
    if not text:
        return False
    tl = text.lower()
    if not any(m in tl for m in _MASSOVKA_FILM_MARKERS):
        return False
    if "аниматор" in tl and any(w in tl for w in ("детск", "праздник", "ростовые")):
        return False
    return True


_OFFICE_STAFF_MARKERS = (
    "личный помощник", "личный #помощник", "#помощник в офис", "помощник в офис",
    "в офис", "документооборот", "делопроизвод", "настольные игры", "зоны отдыха",
    "соц. сет", "собеседован", "разбор заявок", "ведение документ", "выкладка рекламы",
)


def is_office_staff_spam(text: str) -> bool:
    """Офисный помощник / админ — не хелпер на мероприятии."""
    if not text:
        return False
    tl = text.lower()
    norm = _normalize_hashtag_text(tl)
    if "#помощник" in norm:
        if any(w in tl for w in _HELPER_EVENT_HINTS + ("хелпер", "хэлпер", "на площадке", "монтаж", "демонтаж")):
            return False
        return True
    if any(m in tl for m in _OFFICE_STAFF_MARKERS):
        if any(w in tl for w in ("хелпер", "хэлпер", "мероприят", "площадк", "монтаж", "демонтаж")):
            return False
        return True
    return False


def is_digital_work_spam(text: str) -> bool:
    """Удалённая «подработка с телефона» — не хелпер на площадке."""
    if not text:
        return False
    tl = text.lower()
    if not any(m in tl for m in _DIGITAL_WORK_MARKERS):
        return False
    if any(w in tl for w in ("мероприят", "площадк", "на смену", "хелпер", "хэлпер")):
        if not any(m in tl for m in _DIGITAL_WORK_MARKERS[:4]):
            return False
    return True


def is_non_event_labor_spam(text: str) -> bool:
    """Пекарня, ЖКХ, посудомой — не грузчик/хелпер на мероприятии."""
    if not text:
        return False
    tl = text.lower()
    if not any(m in tl for m in _NON_EVENT_LABOR_MARKERS):
        return False
    if any(w in tl for w in _LOADER_CORE_HINTS):
        return False
    if any(w in tl for w in (
        "грузчик", "разгруз", "выгруз", "мероприят", "event staff",
        "фура", "склад", "такелаж", "хелпер", "хэлпер", "конвейер", "фасовоч", "рохл",
    )):
        return False
    return True


def is_permanent_job_spam(text: str) -> bool:
    """Вахта/ТК — не разовая смена для бота."""
    if not text:
        return False
    tl = text.lower()
    if re.search(r"\d{2,3}[\s\d]*000\s*(?:-|–|—)?\s*\d{0,3}[\s\d]*000\s*(?:руб|₽|р)?\s*(?:в\s+)?месяц", tl):
        if not any(m in tl for m in _SHORT_TERM_SHIFT_MARKERS):
            return True
    if re.search(r"\d{2,3}[.\s]\d{3}.*(?:в\s+)?месяц", tl):
        if not any(m in tl for m in _SHORT_TERM_SHIFT_MARKERS):
            return True
    if re.search(r"(?:от\s*)?\d{2,3}\s*тр\s*(?:в\s+)?месяц", tl):
        if not any(m in tl for m in _SHORT_TERM_SHIFT_MARKERS):
            return True
    if "на руки" in tl and "месяц" in tl and re.search(r"\d{2,3}[.\s]\d{2,3}", tl):
        if not any(m in tl for m in _SHORT_TERM_SHIFT_MARKERS):
            return True
    if re.search(r"(?:от\s*)?\+\s*\d{2,3}[\s.]*000.*месяц", tl):
        if not any(m in tl for m in _SHORT_TERM_SHIFT_MARKERS):
            return True
    if not any(m in tl for m in _PERMANENT_JOB_MARKERS):
        return False
    if any(m in tl for m in _SHORT_TERM_SHIFT_MARKERS):
        return False
    if re.search(r"к\s*\d{1,2}[:.\s]", tl):
        return False
    if re.search(r"№\s*\d{4,7}", tl):
        return False
    return True


def is_professional_casting_spam(text: str) -> bool:
    if not text:
        return False
    tl = text.lower()
    if not any(m in tl for m in _PRO_CASTING_MARKERS):
        return False
    if any(w in tl for w in ("аниматор", "хелпер", "промоутер", "грузчик", "раздача листовок")):
        return False
    return True


def is_chat_listing_noise(text: str) -> bool:
    if not text:
        return False
    tl = text.lower()
    if "бот для отправки объявлений" in tl:
        return True
    if "подписаться на max" in tl and "заявк" not in tl:
        return True
    return False


def _score_categories_weighted(text: str) -> dict:
    """Scoring по всему тексту + усиление шапки (первые строки / блок до «1.»)."""
    if not text:
        return {}
    text_lower = text.lower()
    scores = _score_categories(text_lower)

    from services.post_header_context import extract_leading_header_lines, extract_shared_header

    boost_chunks: list[str] = []
    leading = "\n".join(extract_leading_header_lines(text)[:3])
    if leading.strip():
        boost_chunks.append(leading)
    shared = extract_shared_header(text)
    if shared.strip() and shared.strip() != leading.strip():
        boost_chunks.append(shared)

    for chunk in boost_chunks:
        for cat, pts in _score_categories(chunk.lower()).items():
            scores[cat] = scores.get(cat, 0) + pts * 2

    if any(marker in text_lower for marker in _NON_SUPERVISOR_COORDINATOR):
        scores.pop("supervisor", None)
    return scores


def _detect_category_scored(text: str) -> str | None:
    if not text:
        return None
    text_lower = text.lower()
    if _is_driver_role(text_lower):
        return "driver"
    return _pick_category_from_scores(_score_categories_weighted(text), text_lower)


def detect_category(text: str) -> str | None:
    """Категория по тексту; без уверенного scoring — None (не fallback)."""
    if not text:
        return None
    blocks = split_vacancy_blocks(text)
    for block in blocks:
        cat = _detect_category_scored(block)
        if cat:
            return cat
    return _detect_category_scored(text)


def is_casting_call(text: str) -> bool:
    """Кастинг моделей/съёмок — не event-staff."""
    if not text:
        return False
    tl = text.lower()
    markers = (
        "кастинг", "casting", "фотомодел", "видеомодел", "модель на съём",
        "модель на съем", "моделей на реклам", "open call",
    )
    if not any(m in tl for m in markers):
        return False
    event_staff = (
        "промоутер", "хостес", "хелпер", "хэлпер", "грузчик", "аниматор",
        "мероприят", "на площадке", "смен", "персонал", "официант",
    )
    return not any(w in tl for w in event_staff)


def is_service_request(text: str) -> bool:
    if not text:
        return False
    for pat in _SERVICE_REQUEST_RES:
        if pat.search(text):
            return True
    return False


def has_hiring_signal(text: str) -> bool:
    if not text:
        return False
    tl = text.lower()
    for hv in (*HIRING_VERBS, *_STAFF_HIRING_EXTRA):
        if hv.lower() in tl:
            return True
    if re.search(r"\d+\s*(?:чел|человек|чел\b|рабоч|сотрудник)", tl):
        return True
    if re.search(r"позиция\s*[:\s]", tl):
        return True
    if _detect_category_scored(text):
        return True
    return False


def has_payment_signal(text: str) -> bool:
    if not text or is_unpaid_vacancy(text):
        return False
    from services.vacancy_rate import has_payment_or_negotiated_rate

    return has_payment_or_negotiated_rate(text)


def has_ls_contact_phrase(text: str) -> bool:
    if not text:
        return False
    return bool(_LS_CONTACT_RE.search(text))


def has_contact_signal(text: str, poster: dict | None = None) -> bool:
    if extract_contact_from_text(text or ""):
        return True
    if has_ls_contact_phrase(text):
        return True
    if _extract_phone_digits(text or ""):
        return True
    if poster and (poster.get("username") or poster.get("user_id")):
        return True
    return False


async def extract_poster_info(message) -> dict:
    """Автор поста из Telethon (не список участников группы)."""
    info: dict = {}
    try:
        sender = await message.get_sender()
        if sender and getattr(sender, "id", None):
            info["user_id"] = int(sender.id)
            uname = getattr(sender, "username", None)
            if uname:
                info["username"] = uname
            fn = (getattr(sender, "first_name", None) or "").strip()
            ln = (getattr(sender, "last_name", None) or "").strip()
            info["display_name"] = f"{fn} {ln}".strip() or None
    except Exception as e:
        logger.debug("extract_poster_info sender: %s", e)
    post_author = getattr(message, "post_author", None)
    if post_author and not info.get("display_name"):
        info["display_name"] = str(post_author).strip()
    return info


def resolve_vacancy_contact(text: str, poster: dict | None = None) -> tuple[str | None, str | None]:
    from_text = extract_contact_from_text(text or "")
    if from_text:
        return from_text, "text"
    if poster:
        if poster.get("username"):
            return f"@{poster['username']}", "sender"
        if poster.get("user_id"):
            return f"tg://user?id={poster['user_id']}", "sender"
    if has_ls_contact_phrase(text):
        return None, "ls_intent"
    return None, None


_DIGEST_HIRING_MARKERS = (
    "требу", "нужен", "нужны", "ищем", "ищу", "ваканс", "набор",
    "промоутер", "грузчик", "хостес", "официант", "аниматор", "водитель",
)


def _is_numbered_annotation_list(text: str) -> bool:
    """«1. 16+ 2. СРОЧНО» под одной шапкой — одна вакансия, не digest."""
    if _numbered_vacancy_count(text) < 2:
        return False
    blocks = split_vacancy_blocks(text)
    if len(blocks) < 3:
        return False
    header_block = blocks[0]
    numbered = blocks[1:]
    if not all(len(b.strip()) < 72 for b in numbered):
        return False
    header_lower = header_block.lower()
    if not (
        extract_address_from_text(header_block)
        or has_payment_signal(header_block)
        or "смена" in header_lower
        or "адрес" in header_lower
    ):
        return False
    for nb in numbered:
        nl = nb.lower()
        if any(m in nl for m in _DIGEST_HIRING_MARKERS):
            return False
    return True


def _digest_cluster_key(order: dict) -> str | None:
    """Ключ кластера внутри одного digest-поста (адрес + смена + категория)."""
    addr = order.get("address_normalized") or order.get("address")
    if not addr and not order.get("shift_date"):
        return None
    parts = [
        order.get("category_code") or order.get("category") or "",
        (addr or "").strip().lower(),
        str(order.get("shift_date") or ""),
        str(order.get("shift_time_start") or ""),
    ]
    if not any(parts):
        return None
    return "|".join(parts)


def _preview_digest_cluster_key(
    block_text: str,
    message,
    poster: dict,
    block_index: int,
) -> str | None:
    """Ключ кластера до save — чтобы не плодить дубли из одного digest."""
    eval_text = block_text
    if block_index is not None and message.text:
        eval_text = enrich_digest_block(block_text, message.text)
    accepted, category, _, _ = evaluate_vacancy(eval_text, poster)
    if not accepted or not category:
        return None
    address = extract_address_from_text(eval_text) or extract_address_from_text(message.text or "")
    full_text = "\n".join(part for part in (eval_text, message.text or "") if part)
    msg_lat, msg_lon = _extract_message_geo(message)
    from services.vacancy_enrichment import enrich_vacancy_text

    enrichment = enrich_vacancy_text(
        full_text,
        legacy_address=address,
        location_lat=msg_lat,
        location_lon=msg_lon,
    )
    if enrichment.address_normalized:
        address = enrichment.address_normalized
    return _digest_cluster_key({
        "category_code": category,
        "address": address,
        "address_normalized": enrichment.address_normalized,
        "shift_date": enrichment.shift_date,
        "shift_time_start": enrichment.shift_time_start,
    })


def should_split_digest(text: str) -> bool:
    """Нумерованный digest «1. … 2. …» — разбираем по блокам, не одной вакансией."""
    if not text:
        return False
    if _is_numbered_annotation_list(text):
        return False
    if _numbered_vacancy_count(text) < 2:
        return False
    return len(split_vacancy_blocks(text)) > 1


def evaluate_digest_blocks(text: str, poster: dict | None = None) -> list[tuple[str, str]]:
    """Принятые блоки digest: [(category, block_text), …]."""
    accepted = []
    for block in split_vacancy_blocks(text)[:MAX_DIGEST_BLOCKS]:
        enriched = enrich_digest_block(block, text)
        ok, cat, _, _ = evaluate_vacancy(enriched, poster)
        if ok and cat:
            accepted.append((cat, block))
    return accepted


def is_mixed_digest_post(text: str) -> bool:
    """Обратная совместимость тестов — то же, что should_split_digest."""
    return should_split_digest(text)


def is_job_post_for_staff(text: str, poster: dict | None = None) -> tuple[bool, str, list]:
    """Общий gate: найм персонала на смену (все категории)."""
    if not text:
        return False, "empty", []
    tl = text.lower()
    if is_closed_vacancy_post(text):
        return False, "closed_vacancy", []
    if is_chat_listing_noise(text):
        return False, "chat_noise", []
    if is_massovka_or_film_extras(text):
        return False, "massovka_film", []
    if is_permanent_job_spam(text):
        return False, "permanent_job", []
    if is_office_staff_spam(text):
        return False, "office_staff_job", []
    if is_non_event_labor_spam(text):
        return False, "non_event_labor", []
    if is_unpaid_vacancy(text):
        return False, "unpaid", []
    if is_academic_writing_spam(text):
        return False, "academic_writing_spam", []
    if is_digital_work_spam(text):
        return False, "digital_work_spam", []
    if is_remote_office_job_spam(text):
        return False, "remote_office_job", []
    if is_professional_casting_spam(text):
        return False, "professional_casting", []
    for phrase in STOP_PHRASES:
        if phrase.lower() in tl:
            return False, f"stop_phrase: {phrase}", []
    if is_service_request(text):
        return False, "service_request", []
    if is_casting_call(text):
        return False, "casting", []
    if re.search(r"#\s*(аниматор|квест|диджей|музыкант|фотограф)\b", tl):
        if not has_hiring_signal(text) and not any(
            w in tl for w in ("хелпер", "хэлпер", "грузчик", "разнорабоч", "промоутер", "нужен", "нужны", "требу")
        ):
            return False, "excluded_hashtag_role", []
    if any(p in tl for p in ("организатор", "координатор свад", "свадеб")) and "супервайзер" not in tl:
        if not any(w in tl for w in ("хелпер", "хэлпер", "промоутер", "аниматор", "грузчик", "промо", "нужны", "требу")):
            return False, "excluded_organizer", []
    for category in EXCLUDE_CATEGORIES:
        if category.lower() in tl:
            if not any(hw in tl for hw in ("хелпер", "хэлпер", "промоутер", "аниматор", "грузчик", "нужны", "требу")):
                return False, f"excluded_category: {category}", []
    if not has_hiring_signal(text):
        return False, "no_hiring", []
    if not has_payment_signal(text):
        return False, "no_payment", []
    if not has_contact_signal(text, poster):
        return False, "no_contact", []
    keywords = []
    cat = detect_category(text)
    if cat:
        keywords.append(cat)
    return True, "staff_job", keywords


def passes_quality_gate(category: str, text: str) -> bool:
    """Per-category gate: роль в тексте совпадает и нет явного конфликта."""
    if not text or not category:
        return False
    if is_unpaid_vacancy(text):
        return False
    tl = text.lower()
    if category == "driver" and _is_driver_role(tl):
        if "грузчик" in tl and not re.search(r"\b(?:водител|курьер|экспедитор)\w*\b", tl):
            return False
        return True
    scores = _score_categories_weighted(text)
    picked = _pick_category_from_scores(scores, tl)
    if picked != category:
        return False
    if scores.get(category, 0) <= 0:
        return False

    if category == "helper":
        helper_markers = (
            "хелпер", "хэлпер", "хелперы", "хэлперы",
            "требуются хелпер", "требуются хэлпер",
            "хелперский функционал", "хелперские задачи", "хэлперские задачи",
            "помощь на монтаже", "демонтаж", "монтажник",
            "#техник", "техник прокат", "техник оборудован",
            "хелпер на мероприят", "хэлпер на мероприят",
            "хелпер на мероприятие", "хэлпер на мероприятие",
            "помощник на мероприят", "помощник организатора",
            "помощь на площадке", "помощники на площадке",
            "к мероприятию", "деловое мероприятие", "приготовить площадку",
            "расставить", "площадку к мероприятию",
            "бекфотограф", "бэкстейдж", "бэкстейж", "ассистент по акт", "ассистент на площадке",
        )
        if not any(m in tl for m in helper_markers):
            return False
        if any(w in tl for w in _PROMO_HINTS) and "промоутер" not in tl and "позиция: промо" not in tl:
            if not any(m in tl for m in ("хелпер", "хэлпер")):
                return False
        if any(w in tl for w in _LABOR_HINTS) and not any(
            m in tl for m in (
                "хелпер", "хэлпер", "хелперский", "хелперские", "хэлперск",
                "помощник на мероприят", "помощник организатора",
                "помощь на монтаже", "демонтаж",
            )
        ):
            return False
    elif category == "loader":
        if any(w in tl for w in _HANDYMAN_LOADER_REJECT) and not any(w in tl for w in _LOADER_CORE_HINTS):
            return False
        if any(w in tl for w in _LOADER_CORE_HINTS):
            return True
        if (
            "уборка" in tl
            and any(m in tl for m in _SHORT_TERM_SHIFT_MARKERS)
            and not any(m in tl for m in _NON_EVENT_LABOR_MARKERS)
        ):
            return True
        return False
    elif category == "handyman":
        if not any(w in tl for w in _HANDYMAN_HINTS):
            return False
    elif category == "promoter":
        if not any(w in tl for w in _PROMO_HINTS):
            return False
        if re.search(r"позиция\s*[:\s].*хелпер", tl) and "промо" not in tl and "промоутер" not in tl:
            return False
    elif category == "animator":
        if is_massovka_or_film_extras(text):
            return False
        if not any(w in tl for w in ("аниматор", "анимац", "клоун", "ростовые")):
            return False
        if is_service_request(text):
            return False
    elif category == "hostess":
        if not any(w in tl for w in ("хостес", "ресепшн", "встреча гостей", "встречать гостей")):
            return False
    elif category == "wardrobe":
        if not any(w in tl for w in ("гардероб", "гардеробщ", "раздевалка", "номерков")):
            return False
    elif category == "waiter":
        if not any(w in tl for w in ("официант", "бармен", "обслуживание гостей")):
            return False
    elif category == "driver":
        if "грузчик" in tl and re.search(r"водител\w*\s+забер", tl):
            return False
        if not _is_driver_role(tl):
            return False
        if "грузчик" in tl and not re.search(r"\b(?:водител|курьер|экспедитор)\w*\b", tl):
            return False
    elif category == "security":
        if any(w in tl for w in ("охранник", "охрана", "контрол", "секьюрити", "контроль доступа")):
            return True
        if "пропускной" in tl and any(w in tl for w in ("охранник", "охрана", "контрол", "секьюрити")):
            return True
        return False
    elif category == "parking":
        if not any(w in tl for w in ("парковщик", "паркинг", "парковоч")):
            return False
    elif category == "supervisor":
        if not any(w in tl for w in (
            "супервайзер", "супервизор", "старший смены", "контроль промо", "координатор промо",
            "координатор проекта", "координатор мероприят",
        )):
            return False
    return True


def evaluate_vacancy(
    text: str, poster: dict | None = None, *, force_category: str | None = None,
) -> tuple[bool, str | None, str, list]:
    """Полный P0-pipeline: staff gate → category → per-category gate."""
    if should_split_digest(text):
        return False, None, "digest_split_required", []
    ok, reason, keywords = is_job_post_for_staff(text, poster)
    if not ok:
        return False, None, reason, keywords
    category = force_category or detect_category(text)
    if not category:
        return False, None, "ambiguous_category", keywords
    if not passes_quality_gate(category, text):
        return False, None, f"quality_gate:{category}", keywords
    return True, category, "accepted", keywords


def vacancy_matches_category(text: str, category_code: str) -> bool:
    """Перепроверка для ленты и push — тот же pipeline, что при парсинге."""
    accepted, category, _, _ = evaluate_vacancy(text)
    return accepted and category == category_code


def is_unpaid_vacancy(text: str) -> bool:
    if not text:
        return False
    text_lower = text.lower()
    if re.search(r"оплат\w*\s*[💵:]?\s*нет\b", text_lower):
        return True
    if re.search(r"💵\s*нет\b", text_lower):
        return True
    if re.search(r"оплата\s*[:\s]*нет\b", text_lower):
        return True
    if any(p in text_lower for p in ("безмерную благодарность", "без оплаты", "бесплатно", "волонтер")):
        return True
    return False


def is_helper_message(text: str):
    """Обратная совместимость: делегирует в evaluate_vacancy / is_job_post_for_staff."""
    accepted, category, reason, keywords = evaluate_vacancy(text)
    if accepted:
        return True, reason, keywords
    ok, staff_reason, staff_kw = is_job_post_for_staff(text)
    if ok:
        return False, reason, keywords
    return False, staff_reason, staff_kw

def clean_message_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

def get_message_link(chat_id: int, message_id: int) -> str:
    str_id = str(chat_id)
    if str_id.startswith('-100'):
        clean_id = str_id[4:]
        return f"https://t.me/c/{clean_id}/{message_id}"
    elif str_id.startswith('-'):
        clean_id = str_id[1:]
        return f"https://t.me/c/{clean_id}/{message_id}"
    return f"https://t.me/c/{chat_id}/{message_id}"

async def safe_get_entity(client, chat_link: str):
    try:
        entity = await asyncio.wait_for(client.get_entity(chat_link), timeout=ENTITY_RESOLVE_TIMEOUT_SEC)
        await asyncio.sleep(0.3)
        return entity
    except errors.rpcerrorlist.ChannelPrivateError:
        logger.warning(f"⚠️ Приватный канал (нет доступа): {chat_link}")
        return None
    except asyncio.TimeoutError:
        logger.warning(f"⚠️ Таймаут доступа к {chat_link} ({ENTITY_RESOLVE_TIMEOUT_SEC}s)")
        return None
    except errors.rpcerrorlist.UsernameNotOccupiedError:
        logger.warning(f"⚠️ Канал не найден: {chat_link}")
        return None
    except Exception as e:
        logger.warning(f"⚠️ Ошибка доступа к {chat_link}: {type(e).__name__}")
        return None

async def run_parser_audit() -> dict:
    """Админ: последние посты из каждого чата через фильтр, без сохранения."""
    stats = _new_stats("audit")
    stats["reject_samples"] = []
    try:
        async with _parser_lock:
            _publish_debug_stats(stats)
            if _realtime_client and _realtime_client.is_connected():
                logger.info(
                    "🔬 Аудит фильтра: последние %s постов из каждого чата (без сохранения)",
                    AUDIT_SCAN_LIMIT,
                )
                await asyncio.wait_for(
                    _scan_all_chats(
                        _realtime_client,
                        limit_per_chat=AUDIT_SCAN_LIMIT,
                        stats=stats,
                        incremental=False,
                        audit_only=True,
                    ),
                    timeout=PARSER_SCAN_TIMEOUT_SEC,
                )
                _mark_stats_finished(stats)
                return stats
            logger.error(f"❌ {PARSER_LABEL} offline — аудит пропущен")
            _mark_stats_finished(stats, error="offline")
            return stats
    except asyncio.TimeoutError:
        logger.error("Аудит фильтра: таймаут %s с", PARSER_SCAN_TIMEOUT_SEC)
        _mark_stats_finished(stats, error="timeout")
        return stats
    except Exception as e:
        logger.error(f"❌ Ошибка аудита фильтра: {e}", exc_info=True)
        _mark_stats_finished(stats, error=str(e))
        return stats


async def get_new_messages(limit_per_chat: int = PER_CHAT_SCAN_LIMIT):
    stats = _new_stats("manual")
    try:
        async with _parser_lock:
            _publish_debug_stats(stats)
            if _realtime_client and _realtime_client.is_connected():
                logger.info("🔍 Ручная проверка через shared Telethon client")
                result = await asyncio.wait_for(
                    _scan_all_chats(
                        _realtime_client, limit_per_chat=limit_per_chat, stats=stats,
                        incremental=True,
                    ),
                    timeout=PARSER_SCAN_TIMEOUT_SEC,
                )
                _mark_stats_finished(stats)
                orders, closed_data = result
                return orders, closed_data, stats

            logger.error(
                f"❌ {PARSER_LABEL} offline — ручная проверка пропущена (не создаём второй user_session)"
            )
            _mark_stats_finished(stats, error="offline")
            return [], [], stats
    except asyncio.TimeoutError:
        logger.error("Ручная проверка: таймаут %s с", PARSER_SCAN_TIMEOUT_SEC)
        _mark_stats_finished(stats, error="timeout")
        return [], [], stats
    except Exception as e:
        logger.error(f"❌ Критическая ошибка в парсере: {e}", exc_info=True)
        _mark_stats_finished(stats, error=str(e))
        return [], [], stats

async def run_parser():
    return await get_new_messages()

async def test_filter(chat_link: str, limit: int = 30):
    try:
        client = await create_authorized_client()
    except SessionNotConfiguredError as e:
        logger.error(str(e))
        return
    try:
        logger.info(f"\n{'='*60}")
        logger.info(f"🧪 ТЕСТ ФИЛЬТРА: {chat_link}")
        logger.info(f"{'='*60}\n")
        entity = await safe_get_entity(client, chat_link)
        if not entity:
            logger.error("❌ Не удалось получить доступ к каналу")
            return
        passed = 0
        blocked = 0
        category_stats = {}
        async for message in client.iter_messages(entity, limit=limit):
            if not message.text:
                continue
            accepted, category, reason, keywords = evaluate_vacancy(message.text)
            if accepted and category:
                category_stats[category] = category_stats.get(category, 0) + 1
                passed += 1
                logger.info(f"✅ [{category}] [{reason}] {message.text[:80]}...")
            else:
                blocked += 1
                logger.debug(f"⛔ [{reason}] {message.text[:60]}...")
        logger.info(f"\n{'='*60}")
        logger.info(f"📊 ПРОПУЩЕНО: {passed} | ОТСЕЯНО: {blocked}")
        logger.info(f"📊 Категории: {category_stats}")
        logger.info(f"{'='*60}")
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}")
    finally:
        if client.is_connected():
            await client.disconnect()
