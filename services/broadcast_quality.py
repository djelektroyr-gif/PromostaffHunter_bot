"""Кому и когда слать push / кросс-пост: только уверенные вакансии."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BroadcastDecision:
    eligible: bool
    category_code: str | None
    reason: str
    confidence: str  # accepted | soft | wide | rejected


def _poster_from_order(order: dict[str, Any]) -> dict | None:
    uid = order.get("poster_user_id")
    username = order.get("poster_username")
    if uid or username:
        return {"user_id": uid, "username": username}
    return None


def assess_vacancy_broadcast(order: dict[str, Any]) -> BroadcastDecision:
    """
    Решение перед рассылкой: пересчёт evaluate_vacancy на тексте карточки.

    - reject (постоянка, спам, quality) → не пушим;
    - accepted → пушим;
    - soft/wide/misc → в БД могли сохранить, но в канал/бот не шлём
      (типичный источник «кривых» карточек, которые владелец удаляет вручную).
    - from_bot_employer после модерации — всегда пушим, если evaluate не reject.
    """
    if order.get("from_bot_employer"):
        from parser import evaluate_vacancy

        msg_text = (order.get("message_text") or "").strip()
        force = order.get("category") or order.get("category_code")
        accepted, cat, reason, _ = evaluate_vacancy(
            msg_text, _poster_from_order(order), force_category=force,
        )
        if accepted and cat:
            return BroadcastDecision(True, cat, reason, "accepted")
        return BroadcastDecision(False, None, reason, "rejected")

    msg_text = (order.get("message_text") or "").strip()
    if not msg_text:
        return BroadcastDecision(False, None, "empty", "rejected")

    from parser import evaluate_vacancy, passes_quality_gate

    accepted, cat, eval_reason, _ = evaluate_vacancy(msg_text, _poster_from_order(order))
    if not accepted or not cat:
        return BroadcastDecision(False, None, eval_reason, "rejected")

    if eval_reason == "accepted":
        return BroadcastDecision(True, cat, eval_reason, "accepted")

    if eval_reason.startswith("soft_accept:"):
        if passes_quality_gate(cat, msg_text):
            return BroadcastDecision(True, cat, "accepted", "accepted")
        return BroadcastDecision(
            False, cat, f"broadcast_soft_skip:{eval_reason}", "soft",
        )

    if eval_reason.startswith("wide_accept:") or cat == "misc":
        return BroadcastDecision(
            False, cat, f"broadcast_wide_skip:{eval_reason}", "wide",
        )

    if passes_quality_gate(cat, msg_text):
        return BroadcastDecision(True, cat, "accepted", "accepted")

    return BroadcastDecision(False, cat, f"broadcast_gate:{eval_reason}", "soft")
