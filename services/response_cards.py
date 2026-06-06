"""Карточки откликов: текст, статус доставки черновика."""

from __future__ import annotations

from html import escape as escape_html

DRAFT_STATUS_LABELS = {
    "delivered": "✅ черновик отправлен",
    "manual": "✋ контакт вручную",
    "failed": "⚠️ сбой доставки",
    "pending": "⏳ в обработке",
}


def draft_status_label(status: str | None) -> str:
    return DRAFT_STATUS_LABELS.get(status or "pending", status or "—")


def _preview(text: str | None, limit: int = 200) -> str:
    raw = (text or "—").strip()
    if len(raw) > limit:
        return raw[:limit] + "…"
    return raw


def format_user_response_card(resp: dict, *, idx: int | None = None) -> str:
    prefix = f"<b>{idx}.</b> " if idx is not None else ""
    return (
        f"{prefix}<b>📨 Отклик</b> · {escape_html(draft_status_label(resp.get('draft_status')))}\n"
        f"🕐 {escape_html(str(resp.get('responded_at') or '—'))}\n"
        f"📢 {escape_html(resp.get('source_chat_title') or '—')}\n"
        f"👨‍💼 {escape_html(resp.get('employer_contact') or resp.get('author_contact') or '—')}\n"
        f"📋 {escape_html(_response_status(resp))}\n\n"
        f"{escape_html(_preview(resp.get('vacancy_text')))}"
    )


def format_admin_response_card(resp: dict, user_label: str) -> str:
    return (
        f"<b>📨 Отклик #{resp.get('id')}</b>\n"
        f"👤 {escape_html(user_label)} · <code>{resp.get('user_id')}</code>\n"
        f"🕐 {escape_html(str(resp.get('responded_at') or '—'))}\n"
        f"📢 {escape_html(resp.get('source_chat_title') or '—')}\n"
        f"👨‍💼 {escape_html(resp.get('employer_contact') or resp.get('author_contact') or '—')}\n"
        f"📋 {escape_html(_response_status(resp))} · {escape_html(draft_status_label(resp.get('draft_status')))}\n"
        f"🆔 <code>{escape_html(str(resp.get('vacancy_id') or ''))}</code>\n\n"
        f"{escape_html(_preview(resp.get('vacancy_text'), 400))}"
    )


def format_response_list_row(resp: dict, idx: int) -> str:
    source = resp.get("source_chat_title") or "—"
    if len(source) > 28:
        source = source[:28] + "…"
    status = draft_status_label(resp.get("draft_status"))
    closed = "🔒" if resp.get("is_closed") else "🟢"
    return f"{idx}. {closed} {source} · {status}"


def _response_status(resp: dict) -> str:
    return "Вакансия закрыта" if resp.get("is_closed") else "Вакансия активна"
