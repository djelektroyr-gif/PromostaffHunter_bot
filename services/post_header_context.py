"""Шапка поста и общий контекст для digest: роль, ставка, локация."""

from __future__ import annotations

import re

_NUMBERED_BLOCK_START_RE = re.compile(r"^\s*\d+[\.\)]\s+")
_ROLE_LINE_RE = re.compile(
    r"(?:"
    r"позиция\s*[:\s]|роль\s*[:\s]|вакансия\s*[:\s]|"
    r"#\s*(?:хелпер|грузчик|промо|аниматор|официант|хостес|водител|"
    r"помощник|мероприят|промоутер|грузч|хост|бармен|"
    r"loader|promoter|helper|animator|waiter|hostess|driver)"
    r")",
    re.I,
)
_ROLE_EMOJI_RE = re.compile(r"[👷🧑‍🔧🧑‍🍳🎭📣🚗🛡️🅿️🦺]")


def extract_shared_header(full_text: str) -> str:
    """Текст до первого нумерованного блока «1. …» / «2. …»."""
    if not full_text:
        return ""
    match = re.search(r"(?:^|\n)\s*\d+[\.\)]\s+", full_text)
    if not match:
        return ""
    return full_text[: match.start()].strip()


def extract_leading_header_lines(full_text: str, *, max_lines: int = 5) -> list[str]:
    """Первые осмысленные строки поста (до нумерации или лимита)."""
    if not full_text:
        return []
    out: list[str] = []
    for line in full_text.splitlines():
        stripped = line.strip()
        if not stripped:
            if out:
                break
            continue
        if _NUMBERED_BLOCK_START_RE.match(stripped):
            break
        out.append(stripped)
        if len(out) >= max_lines:
            break
    return out


def line_has_role_hint(line: str, *, category_scorer) -> bool:
    if not line or not line.strip():
        return False
    if category_scorer(line):
        return True
    tl = line.lower()
    if _ROLE_LINE_RE.search(tl):
        return True
    if _ROLE_EMOJI_RE.search(line):
        return True
    if re.search(r"\*\*[^*]{2,60}\*\*", line) and any(
        w in tl for w in ("хелпер", "грузчик", "промо", "аниматор", "официант", "хостес", "водител", "парн")
    ):
        return True
    return False


def line_has_rate_hint(line: str, *, payment_checker) -> bool:
    if not line or not line.strip():
        return False
    if payment_checker(line):
        return True
    from services.channel_rate import extract_hourly_rate_rub, extract_shift_rate_rub

    return bool(extract_hourly_rate_rub(line) or extract_shift_rate_rub(line))


def collect_context_lines(
    full_text: str,
    *,
    kind: str,
    category_scorer,
    payment_checker,
) -> list[str]:
    """Строки шапки с ролью или ставкой (без дублей)."""
    if not full_text:
        return []
    seen: set[str] = set()
    out: list[str] = []

    def _add_from(text: str) -> None:
        for line in text.splitlines():
            s = line.strip()
            if not s or s in seen:
                continue
            if kind == "role" and line_has_role_hint(s, category_scorer=category_scorer):
                seen.add(s)
                out.append(s)
            elif kind == "rate" and line_has_rate_hint(s, payment_checker=payment_checker):
                seen.add(s)
                out.append(s)

    shared = extract_shared_header(full_text)
    if shared:
        _add_from(shared)
    if not shared:
        _add_from("\n".join(extract_leading_header_lines(full_text)))
    return out


def enrich_block_with_header_context(
    block_text: str,
    full_text: str,
    *,
    category_scorer,
    payment_checker,
    has_payment,
    has_contact,
    extract_contact,
    has_ls_contact,
) -> str:
    """Подмешивает роль/ставку/контакт из шапки digest в блок без своих."""
    prefix: list[str] = []
    block = block_text.strip()

    if not category_scorer(block):
        for line in collect_context_lines(
            full_text,
            kind="role",
            category_scorer=category_scorer,
            payment_checker=payment_checker,
        ):
            if line not in prefix and line not in block:
                prefix.append(line)

    if not has_payment(block):
        for line in collect_context_lines(
            full_text,
            kind="rate",
            category_scorer=category_scorer,
            payment_checker=payment_checker,
        ):
            if line not in prefix and line not in block:
                prefix.append(line)
                break

    parts = prefix + [block]

    if not extract_contact(block) and not has_ls_contact(block):
        header = extract_shared_header(full_text)
        contact = extract_contact(header) if header else None
        if contact:
            parts.append(contact)

    return "\n".join(parts)
