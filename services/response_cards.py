"""Карточки откликов: текст, статус доставки черновика."""

from __future__ import annotations

from html import escape as escape_html

DRAFT_STATUS_LABELS = {
    "delivered": "📝 черновик готов — отправьте заказчику",
    "manual": "✋ отправьте вручную",
    "failed": "⚠️ не удалось показать черновик",
    "pending": "⏳ в обработке",
}

ADMIN_DRAFT_STATUS_LABELS = {
    "delivered": "📝 черновик готов",
    "manual": "✋ вручную",
    "failed": "⚠️ сбой черновика",
    "pending": "⏳ в обработке",
}

_CATEGORY_EMOJI = {
    "promoter": "📢", "hostess": "👩‍💼", "wardrobe": "🧥", "animator": "🎭",
    "helper": "👷", "loader": "📦", "waiter": "🍽️", "driver": "🚐",
    "security": "🛡️", "parking": "🚗", "supervisor": "👨‍💼",
}

_CATEGORY_NAMES = {
    "promoter": "Промоутер", "hostess": "Хостес", "wardrobe": "Гардеробщик",
    "animator": "Аниматор", "helper": "Хелпер", "loader": "Грузчик",
    "waiter": "Официант", "driver": "Водитель", "security": "Охранник",
    "parking": "Парковщик", "supervisor": "Супервайзер",
}


def draft_status_label(status: str | None, *, admin: bool = False) -> str:
    labels = ADMIN_DRAFT_STATUS_LABELS if admin else DRAFT_STATUS_LABELS
    return labels.get(status or "pending", status or "—")


def _preview(text: str | None, limit: int = 200) -> str:
    raw = (text or "—").strip()
    if len(raw) > limit:
        return raw[:limit] + "…"
    return raw


def _response_category_line(category_code: str | None) -> str | None:
    if not category_code:
        return None
    emoji = _CATEGORY_EMOJI.get(category_code, "📌")
    name = _CATEGORY_NAMES.get(category_code, category_code)
    return f"{emoji} {name}"


def response_short_title(resp: dict, *, max_len: int = 28) -> str:
    cat = _response_category_line(resp.get("category_code"))
    if cat:
        return cat if len(cat) <= max_len else cat[: max_len - 1] + "…"
    preview = (resp.get("vacancy_text") or "Отклик").strip()
    if len(preview) > max_len:
        return preview[: max_len - 1] + "…"
    return preview or "Отклик"


def format_user_response_card(resp: dict, *, idx: int | None = None) -> str:
    prefix = f"<b>{idx}.</b> " if idx is not None else ""
    cat_line = _response_category_line(resp.get("category_code"))
    lines = [
        f"{prefix}<b>📨 Отклик</b> · {escape_html(draft_status_label(resp.get('draft_status')))}",
        f"🕐 {escape_html(str(resp.get('responded_at') or '—'))}",
    ]
    if cat_line:
        lines.append(escape_html(cat_line))
    lines.extend([
        f"👨‍💼 {escape_html(resp.get('employer_contact') or resp.get('author_contact') or '—')}",
        f"📋 {escape_html(_response_status(resp))}",
        "",
        escape_html(_preview(resp.get("vacancy_text"))),
    ])
    return "\n".join(lines)


def format_admin_response_card(resp: dict, user_label: str) -> str:
    return (
        f"<b>📨 Отклик #{resp.get('id')}</b>\n"
        f"👤 {escape_html(user_label)} · <code>{resp.get('user_id')}</code>\n"
        f"🕐 {escape_html(str(resp.get('responded_at') or '—'))}\n"
        f"📢 {escape_html(resp.get('source_chat_title') or '—')}\n"
        f"👨‍💼 {escape_html(resp.get('employer_contact') or resp.get('author_contact') or '—')}\n"
        f"📋 {escape_html(_response_status(resp))} · {escape_html(draft_status_label(resp.get('draft_status'), admin=True))}\n"
        f"🆔 <code>{escape_html(str(resp.get('vacancy_id') or ''))}</code>\n\n"
        f"{escape_html(_preview(resp.get('vacancy_text'), 400))}"
    )


def format_response_list_row(resp: dict, idx: int) -> str:
    title = response_short_title(resp, max_len=32)
    status = draft_status_label(resp.get("draft_status"))
    closed = "🔒" if resp.get("is_closed") else "🟢"
    return f"{idx}. {closed} {title} · {status}"


def format_admin_response_list_row(resp: dict, idx: int, user_label: str) -> str:
    source = (resp.get("source_chat_title") or "—").strip()
    if len(source) > 18:
        source = source[:18] + "…"
    name = user_label.strip() or "—"
    if len(name) > 12:
        name = name[:12] + "…"
    status = draft_status_label(resp.get("draft_status"), admin=True)
    closed = "🔒" if resp.get("is_closed") else "🟢"
    return f"{idx}. {closed} {source} · {status} · 👤 {name}"


def _response_status(resp: dict) -> str:
    return "Вакансия закрыта" if resp.get("is_closed") else "Вакансия активна"
