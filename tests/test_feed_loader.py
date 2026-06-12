import sqlite3
from datetime import datetime, timedelta, timezone

import db_backend
from db import (
    add_subscriber,
    get_feed_vacancies_bulk_for_user,
    get_feed_vacancies_for_user,
    init_db,
    mark_vacancy_sent_to_user,
    save_vacancy,
    set_user_categories,
)
from services.feed_loader import (
    build_feed_snapshot,
    get_feed_snapshot,
    invalidate_feed_cache,
    snapshot_collect,
    snapshot_mode_totals,
    vacancy_in_feed_mode,
)


def _setup_db(monkeypatch, tmp_path):
    db_file = tmp_path / "feed_loader.db"

    def _connect():
        return sqlite3.connect(str(db_file), timeout=10.0)

    monkeypatch.setattr(db_backend, "connect", _connect)
    monkeypatch.setattr(db_backend, "IS_POSTGRES", False)
    init_db()
    uid = 800001
    add_subscriber(uid, "loader", "Feed", "User")
    set_user_categories(uid, ["helper", "promoter"])
    return uid


def test_bulk_feed_excludes_old_by_max_hours(monkeypatch, tmp_path):
    uid = _setup_db(monkeypatch, tmp_path)
    now = datetime.now(timezone.utc)
    fresh = (now - timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")
    stale = (now - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
    save_vacancy(
        "vac_fresh", "c1", "Chat", "helper", "Fresh job", "https://t.me/c/1/1",
        "@boss", None, False, "dk1", fresh,
    )
    save_vacancy(
        "vac_old", "c2", "Chat2", "helper", "Old job", "https://t.me/c/2/2",
        "@boss2", None, False, "dk2", stale,
    )

    all_rows = get_feed_vacancies_for_user(uid, "helper")
    assert len(all_rows) == 2

    week_rows = get_feed_vacancies_for_user(uid, "helper", max_hours=168)
    assert len(week_rows) == 1
    assert week_rows[0]["id"] == "vac_fresh"

    bulk = get_feed_vacancies_bulk_for_user(uid, ["helper", "promoter"], max_hours=168)
    assert len(bulk) == 1
    assert bulk[0]["id"] == "vac_fresh"


def test_snapshot_single_pass_counts_modes(monkeypatch, tmp_path):
    uid = _setup_db(monkeypatch, tmp_path)
    now = datetime.now(timezone.utc)
    fresh_ts = (now - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    archive_ts = (now - timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")
    save_vacancy(
        "vac_f", "c1", "Chat", "helper", "Fresh", "https://t.me/c/1/1",
        "@b1", None, False, "dkf", fresh_ts,
    )
    save_vacancy(
        "vac_a", "c2", "Chat2", "helper", "Archive", "https://t.me/c/2/2",
        "@b2", None, False, "dka", archive_ts,
    )
    save_vacancy(
        "vac_p", "c3", "Chat3", "promoter", "Promo", "https://t.me/c/3/3",
        "@b3", None, False, "dkp", fresh_ts,
    )

    snap = build_feed_snapshot(uid, max_hours=168)
    fresh_total, archive_total, all_total, history_total = snapshot_mode_totals(snap)
    assert fresh_total == 2
    assert archive_total == 1
    assert all_total == 3
    assert history_total == 0

    helper_fresh = snapshot_collect(snap, ["helper"], "fresh")
    assert len(helper_fresh) == 1
    assert helper_fresh[0]["id"] == "vac_f"

    all_helper = snapshot_collect(snap, ["helper"], "all")
    assert {v["id"] for v in all_helper} == {"vac_f", "vac_a"}


def test_snapshot_cache_reused(monkeypatch, tmp_path):
    uid = _setup_db(monkeypatch, tmp_path)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    save_vacancy(
        "vac_c", "c1", "Chat", "helper", "One", "https://t.me/c/1/1",
        "@b", None, False, "dkc", now,
    )
    invalidate_feed_cache(uid)
    snap1 = get_feed_snapshot(uid)
    snap2 = get_feed_snapshot(uid)
    assert snap1 is snap2

    mark_vacancy_sent_to_user("vac_c", uid)
    snap3 = get_feed_snapshot(uid)
    assert snap3.totals["all"] == 1

    invalidate_feed_cache(uid)
    snap4 = get_feed_snapshot(uid)
    assert snap4.totals["all"] == 0


def test_vacancy_in_feed_mode_boundaries():
    now = datetime.now(timezone.utc)
    fresh_vac = {"published_at": (now - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")}
    old_vac = {"published_at": (now - timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")}
    assert vacancy_in_feed_mode(fresh_vac, "fresh", fresh_hours=24, archive_max_hours=168)
    assert not vacancy_in_feed_mode(fresh_vac, "archive", fresh_hours=24, archive_max_hours=168)
    assert vacancy_in_feed_mode(old_vac, "archive", fresh_hours=24, archive_max_hours=168)
    assert vacancy_in_feed_mode(fresh_vac, "all", fresh_hours=24, archive_max_hours=168)
