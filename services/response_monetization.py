"""Платные отклики после trial: Stars, пакет кредитов, Premium безлимит."""
from __future__ import annotations

from dataclasses import dataclass

from config import (
    PAID_RESPONSES_ENABLED,
    RESPONSE_PACK_CREDITS,
    TRIAL_DAYS,
    TRIAL_ON_FIRST_RESPONSE,
)
from services.beta_access import effective_free_category_limit
from db import (
    count_user_responses,
    get_subscriber_profile,
    get_vacancy_push_row,
    grant_trial_if_eligible,
    has_paid_response_unlock,
    is_user_premium,
    consume_response_credit,
    get_response_credits,
    toggle_user_category,
)


@dataclass(frozen=True)
class ResponseAccess:
    allowed: bool
    needs_paywall: bool
    apply_first_trial: bool
    reason: str


def _first_response_trial_eligible(user_id: int) -> bool:
    """Первый отклик + trial ещё не использовали."""
    if not TRIAL_ON_FIRST_RESPONSE:
        return False
    if count_user_responses(user_id) > 0:
        return False
    profile = get_subscriber_profile(user_id)
    if profile and profile.get("trial_used"):
        return False
    return True


def resolve_response_access(user_id: int, vacancy_id: str) -> ResponseAccess:
    """Можно ли отправить отклик без оплаты прямо сейчас."""
    if is_user_premium(user_id):
        return ResponseAccess(True, False, False, "premium")
    if not PAID_RESPONSES_ENABLED:
        return ResponseAccess(True, False, False, "free_legacy")
    if has_paid_response_unlock(user_id, vacancy_id):
        return ResponseAccess(True, False, False, "star_paid")
    if _first_response_trial_eligible(user_id):
        return ResponseAccess(True, False, True, "first_trial")
    if get_response_credits(user_id) > 0:
        return ResponseAccess(True, False, False, "credit")
    return ResponseAccess(False, True, False, "paywall")


def setup_trial_from_first_response(user_id: int, vacancy_id: str) -> dict:
    """Trial + категория вакансии при первом отклике."""
    push = get_vacancy_push_row(vacancy_id)
    category_code = (push[5] if push else None) or None
    trial_granted = grant_trial_if_eligible(user_id, TRIAL_DAYS)
    category_added = False
    if category_code:
        codes, blocked = toggle_user_category(
            user_id,
            category_code,
            free_limit=effective_free_category_limit(),
        )
        category_added = not blocked and category_code in codes
    return {
        "trial_granted": trial_granted,
        "category_code": category_code,
        "category_added": category_added,
    }


def consume_response_slot(user_id: int, vacancy_id: str) -> tuple[bool, dict | None]:
    """
    Списать платный слот после успешного add_response.
    Возвращает (ok, trial_info) — trial_info только при первом отклике.
    """
    if is_user_premium(user_id):
        return True, None
    if not PAID_RESPONSES_ENABLED:
        return True, None
    if has_paid_response_unlock(user_id, vacancy_id):
        return True, None
    if _first_response_trial_eligible(user_id):
        info = setup_trial_from_first_response(user_id, vacancy_id)
        return True, info
    if consume_response_credit(user_id):
        return True, None
    return False, None


def response_paywall_text(*, credits: int, stars_price: int, pack_credits: int, pack_price: int) -> str:
    lines = [
        "💳 *Платный отклик*",
        "",
        "Пробный Premium закончился. Чтобы откликнуться:",
    ]
    if credits > 0:
        lines.append(f"• На балансе *{credits}* платных откликов — нажмите «Откликнуться» снова.")
        lines.append("")
    lines.extend([
        f"• ⭐ *{stars_price} Stars* — один отклик на эту вакансию",
        f"• 📦 *{pack_credits} откликов за {pack_price} ₽* — перевод + чек",
        "• 💎 *Premium* — безлимит откликов и мониторинг вакансий",
    ])
    return "\n".join(lines)
