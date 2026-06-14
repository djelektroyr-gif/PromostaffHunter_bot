"""Мгновенные ответы пользователю после отправки в поддержку или жалобы."""

from __future__ import annotations

INBOX_SLA_HOURS = 24


def support_request_ack_text(request_id: int) -> str:
    return (
        f"✅ Ваша заявка №{request_id} принята и на рассмотрении.\n\n"
        "Мы передали её специалисту. Постараемся ответить "
        f"в течение {INBOX_SLA_HOURS} часов.\n\n"
        "Ответ придёт в эту тему."
    )


def complaint_ack_text(complaint_id: int) -> str:
    return (
        f"✅ Ваша жалоба №{complaint_id} принята и на рассмотрении.\n\n"
        "Мы передали её специалисту. Постараемся решить вопрос "
        f"в течение {INBOX_SLA_HOURS} часов.\n\n"
        "Спасибо, что помогаете улучшать сервис!"
    )
