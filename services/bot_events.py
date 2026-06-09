"""События активности бота для дайджеста и алертов."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from db import count_reg_validation_fails_since, log_bot_event

# Ключевые события (имя → человекочитаемая метка в дайджесте)
EVENT_START = "start"
EVENT_REG_ROLE_CANDIDATE = "reg_role_candidate"
EVENT_REG_ROLE_EMPLOYER = "reg_role_employer"
EVENT_REG_NAME_OK = "reg_name_ok"
EVENT_REG_BIRTHDATE_OK = "reg_birthdate_ok"
EVENT_REG_PHONE_OK = "reg_phone_ok"
EVENT_REG_COMPLETE = "reg_complete"
EVENT_REG_EMPLOYER_COMPLETE = "reg_employer_complete"
EVENT_REG_CATEGORIES_DONE = "reg_categories_done"
EVENT_REG_VALIDATION_FAIL = "reg_validation_fail"
EVENT_FEED_OPEN = "feed_open"
EVENT_VAC_OPEN = "vac_open"
EVENT_RESPONSE_SENT = "response_sent"
EVENT_HANDLER_ERROR = "handler_error"

DIGEST_EVENT_LABELS = {
    EVENT_START: "/start",
    EVENT_REG_ROLE_CANDIDATE: "роль исполнитель",
    EVENT_REG_ROLE_EMPLOYER: "роль заказчик",
    EVENT_REG_COMPLETE: "анкета готова",
    EVENT_REG_EMPLOYER_COMPLETE: "заказчик зарегистрирован",
    EVENT_REG_CATEGORIES_DONE: "категории выбраны",
    EVENT_FEED_OPEN: "лента",
    EVENT_VAC_OPEN: "открыли вакансию",
    EVENT_RESPONSE_SENT: "отклики",
    EVENT_HANDLER_ERROR: "ошибки handler",
}


def record_bot_event(user_id: int | None, event: str, meta: dict | None = None) -> None:
    try:
        log_bot_event(user_id, event, meta)
    except Exception:
        pass


def record_reg_validation_fail(user_id: int, step: str, detail: str = "") -> int:
    """Логирует ошибку валидации; возвращает число таких событий за последний час."""
    record_bot_event(user_id, EVENT_REG_VALIDATION_FAIL, {"step": step, "detail": detail[:120]})
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    return count_reg_validation_fails_since(user_id, since)
