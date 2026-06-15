"""Кластерный дедуп вакансий: headline, cross-channel campaign, fuzzy."""

from __future__ import annotations

import re
from difflib import SequenceMatcher

_CLUSTER_CAMPAIGN_MARKERS = (
    "раздача листовок",
    "промоутер",
    "промо",
    "супервайзер",
    "требуются",
    "требуется",
    "нужны",
    "нужен",
    "большой проект",
    "длительный проект",
    "хостес",
    "грузчик",
    "грузчики",
    "на стенд",
    "хостес-промо",
)

_HEADLINE_SKIP_PREFIXES = ("📍", "💰", "🗓", "👉", "🕐", "📞", "ℹ️")


def normalize_headline_token(value: str) -> str:
    norm = value.lower()
    norm = re.sub(r"[^\w\sа-яё\-]", " ", norm, flags=re.I)
    return re.sub(r"\s+", " ", norm).strip()


def extract_headline_fingerprint(text: str) -> str | None:
    """Яркий заголовок поста (капс, ‼️, длинная строка) — для cross-channel дедупа."""
    if not text:
        return None
    for line in text.splitlines()[:8]:
        stripped = line.strip().strip("*_").strip()
        if len(stripped) < 14:
            continue
        if any(stripped.startswith(p) for p in _HEADLINE_SKIP_PREFIXES):
            continue
        has_marker = any(m in stripped for m in ("‼", "!!", "❗", "⚡"))
        letters = [c for c in stripped if c.isalpha()]
        caps_ratio = (
            sum(1 for c in letters if c.isupper()) / max(len(letters), 1)
            if letters
            else 0.0
        )
        if not (has_marker or caps_ratio >= 0.32 or len(stripped) >= 26):
            continue
        norm = normalize_headline_token(stripped)
        if len(norm) >= 14:
            return norm[:160]
    return None


def extract_campaign_fingerprint(text: str) -> str | None:
    """Заголовок кампании / проекта (промо, грузчик, хостес…)."""
    if not text:
        return None
    headline = extract_headline_fingerprint(text)
    if headline and len(headline) >= 18:
        tl = headline
        if any(m in tl for m in _CLUSTER_CAMPAIGN_MARKERS) or len(headline) >= 24:
            return headline[:140]
    for line in text.splitlines()[:6]:
        stripped = line.strip().strip("*_").strip()
        if len(stripped) < 18:
            continue
        tl = stripped.lower()
        if not any(w in tl for w in _CLUSTER_CAMPAIGN_MARKERS):
            continue
        norm = normalize_headline_token(stripped)
        if len(norm) >= 18:
            return norm[:140]
    return None


def headline_similarity(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def cluster_duplicate_reason(
    text: str,
    author_contact: str | None,
    category_code: str | None,
    candidate: dict,
    *,
    normalized_text: str,
    fuzzy_text: str,
    order_numbers: set[str],
    phone_digits: str | None,
    usernames: set[str],
    campaign_fp: str | None,
    headline_fp: str | None,
    extract_order_numbers,
    extract_phone_digits,
    extract_telegram_usernames,
    normalize_for_dedupe,
    normalize_for_fuzzy_dedupe,
) -> str | None:
    """Совпадение с уже сохранённой вакансией (кластер)."""
    cand_text = candidate.get("message_text", "")
    if order_numbers and order_numbers & extract_order_numbers(cand_text):
        return "order_number"

    cand_campaign = extract_campaign_fingerprint(cand_text)
    if campaign_fp and cand_campaign:
        if SequenceMatcher(None, campaign_fp, cand_campaign).ratio() >= 0.55:
            return "campaign"

    cand_headline = extract_headline_fingerprint(cand_text)
    if headline_fp and cand_headline and headline_similarity(headline_fp, cand_headline) >= 0.72:
        return "headline"

    candidate_text = normalize_for_dedupe(cand_text)
    if not candidate_text:
        return None

    cand_usernames = extract_telegram_usernames(cand_text, candidate.get("author_contact"))
    normalized_contact = (author_contact or "").strip().lower()
    same_contact = normalized_contact and normalized_contact == (
        (candidate.get("author_contact") or "").strip().lower()
    )
    same_phone = phone_digits and phone_digits == extract_phone_digits(cand_text)
    same_username = bool(usernames & cand_usernames)
    has_contact_link = same_contact or same_phone or same_username

    if headline_fp and cand_headline and headline_similarity(headline_fp, cand_headline) >= 0.62:
        return "headline"

    if not has_contact_link:
        fuzzy_sim = SequenceMatcher(
            None, fuzzy_text, normalize_for_fuzzy_dedupe(cand_text),
        ).ratio()
        text_sim = SequenceMatcher(None, normalized_text, candidate_text).ratio()
        if fuzzy_sim >= 0.68 or text_sim >= 0.72:
            return "fuzzy"
        return None

    if campaign_fp and cand_campaign:
        if SequenceMatcher(None, campaign_fp, cand_campaign).ratio() >= 0.55:
            return "campaign"

    similarity = SequenceMatcher(None, normalized_text, candidate_text).ratio()
    fuzzy_similarity = SequenceMatcher(
        None, fuzzy_text, normalize_for_fuzzy_dedupe(cand_text),
    ).ratio()
    threshold = 0.50 if (same_contact or same_username) else 0.55
    fuzzy_threshold = 0.45 if (same_contact or same_username) else 0.50
    if similarity >= threshold or fuzzy_similarity >= fuzzy_threshold:
        return "fuzzy"
    return None


def find_cluster_vacancy_ids(
    text: str,
    author_contact: str | None,
    category_code: str | None,
    recent_rows: list[dict],
    *,
    exclude_id: str | None = None,
    normalize_for_dedupe,
    normalize_for_fuzzy_dedupe,
    extract_order_numbers,
    extract_phone_digits,
    extract_telegram_usernames,
) -> list[str]:
    """Открытые вакансии из того же кластера (для закрытия по кластеру)."""
    normalized_text = normalize_for_dedupe(text)
    fuzzy_text = normalize_for_fuzzy_dedupe(text)
    if not normalized_text:
        return []
    order_numbers = extract_order_numbers(text)
    phone_digits = extract_phone_digits(text)
    usernames = extract_telegram_usernames(text, author_contact)
    campaign_fp = extract_campaign_fingerprint(text)
    headline_fp = extract_headline_fingerprint(text)

    cluster_ids: list[str] = []
    for row in recent_rows:
        vid = row.get("id")
        if not vid or vid == exclude_id:
            continue
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
            extract_order_numbers=extract_order_numbers,
            extract_phone_digits=extract_phone_digits,
            extract_telegram_usernames=extract_telegram_usernames,
            normalize_for_dedupe=normalize_for_dedupe,
            normalize_for_fuzzy_dedupe=normalize_for_fuzzy_dedupe,
        )
        if reason:
            cluster_ids.append(vid)
    return cluster_ids
