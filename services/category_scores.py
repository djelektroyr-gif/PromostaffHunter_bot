"""Скоры категорий по тексту вакансии — для multi-tag в ленте и push."""

from __future__ import annotations

import json

from parser import _score_categories_weighted

MAX_STORED_SCORES = 8


def compute_category_scores(text: str) -> dict[str, int]:
    if not text:
        return {}
    scores = _score_categories_weighted(text)
    if not scores:
        return {}
    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return dict(ordered[:MAX_STORED_SCORES])


def scores_to_json(scores: dict[str, int] | None) -> str | None:
    if not scores:
        return None
    return json.dumps(scores, ensure_ascii=False)


def parse_category_scores_json(raw) -> dict[str, int]:
    if not raw:
        return {}
    if isinstance(raw, dict):
        data = raw
    elif isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    else:
        return {}
    out: dict[str, int] = {}
    for key, value in data.items():
        if not key:
            continue
        try:
            score = int(value)
        except (TypeError, ValueError):
            continue
        if score > 0:
            out[str(key)] = score
    return out
