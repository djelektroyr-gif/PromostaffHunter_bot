# -*- coding: utf-8 -*-
"""Регрессия по golden-набору: роль (evaluate_vacancy) и черновик адреса."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

GOLDEN_JSON = Path(__file__).parent / "golden" / "vacancy_cards_2026_06_23.json"
GOLDEN_STRICT = os.getenv("GOLDEN_STRICT") == "1"

# Зафиксировано 2026-06-23 после прогона парсера (роль + gate)
BASELINE_CATEGORY_PASS = 21
BASELINE_CATEGORY_TOTAL = 21


def _load_cases() -> list[dict]:
    if not GOLDEN_JSON.is_file():
        pytest.skip(f"missing {GOLDEN_JSON.name}")
    data = json.loads(GOLDEN_JSON.read_text(encoding="utf-8"))
    return data["cases"]


def _case_id(case: dict) -> str:
    return case.get("id") or case.get("title", "case")


def _run_category_case(case: dict) -> tuple[bool, str]:
    from parser import evaluate_vacancy

    text = (case.get("text") or "").strip()
    expected_ingest = bool(case.get("expected_ingest"))
    expected_cat = case.get("expected_primary_category")
    accepted, category, reason, _keywords = evaluate_vacancy(text)
    if not expected_ingest:
        ok = not accepted
        return ok, f"expected reject ({case.get('reject_reason')}), got accept {category}/{reason}"
    if not accepted:
        return False, f"expected ingest, got reject {reason}"
    if category != expected_cat:
        return False, f"expected {expected_cat}, got {category} ({reason})"
    return True, "ok"


@pytest.fixture(scope="module")
def golden_cases() -> list[dict]:
    return _load_cases()


def test_golden_catalog_has_expected_count(golden_cases: list[dict]):
    assert len(golden_cases) >= 20


def test_golden_category_baseline_not_regressed(golden_cases: list[dict]):
    """Общий прогон: не хуже baseline (ручной CI)."""
    passed = sum(1 for c in golden_cases if _run_category_case(c)[0])
    assert passed >= BASELINE_CATEGORY_PASS, (
        f"golden category {passed}/{len(golden_cases)} "
        f"(baseline {BASELINE_CATEGORY_PASS}/{BASELINE_CATEGORY_TOTAL})"
    )


@pytest.mark.skipif(not GOLDEN_STRICT, reason="per-case: GOLDEN_STRICT=1")
@pytest.mark.parametrize("case", _load_cases() or [{}], ids=_case_id)
def test_golden_category_matches_expectation(case: dict):
    if not case:
        pytest.skip("no golden json")
    ok, msg = _run_category_case(case)
    assert ok, f"{case['id']}: {msg}"


@pytest.mark.skipif(not GOLDEN_STRICT, reason="per-case: GOLDEN_STRICT=1")
@pytest.mark.parametrize("case", _load_cases() or [{}], ids=_case_id)
def test_golden_address_hint(case: dict):
    if not case or not case.get("expected_ingest"):
        pytest.skip("reject or empty")
    expected = (case.get("expected_address") or "").strip()
    if not expected or "уточн" in expected.lower():
        pytest.skip("address intentionally vague")

    from parser import extract_address_from_text

    raw = extract_address_from_text((case.get("text") or "").strip()) or ""
    tokens = [t for t in expected.replace(",", " ").split() if len(t) >= 4]
    if not tokens:
        pytest.skip("no tokens")
    hit = any(t.lower() in raw.lower() for t in tokens)
    assert hit, (
        f"{case['id']}: expected address ~{expected!r}, extract_address={raw!r}"
    )
