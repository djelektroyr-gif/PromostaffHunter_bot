"""Архив stats парсера и отчёты после аудита / incremental."""

from parser import (
    _mark_stats_finished,
    _new_stats,
    _publish_debug_stats,
    format_chat_noise_report,
    format_reject_samples_report,
    format_scan_finished_summary,
    get_stats_for_filter_reports,
    reset_parser_stats_cache,
)


def setup_function():
    reset_parser_stats_cache()


def test_finished_audit_survives_periodic_publish():
    audit = _new_stats("audit")
    audit["messages_scanned"] = 1168
    audit["matched"] = 211
    audit["non_relevant"] = 957
    audit["by_chat"] = {"Event Family": {"scanned": 20, "matched": 3, "rejected": 17, "reasons": {}}}
    audit["reject_samples"] = [
        {"chat": "Event Family", "reason": "no_hiring", "preview": "шум"},
    ]
    _mark_stats_finished(audit)

    periodic = _new_stats("periodic")
    _publish_debug_stats(periodic)
    _mark_stats_finished(periodic)

    report_stats = get_stats_for_filter_reports()
    assert report_stats["run_kind"] == "audit"
    assert report_stats["messages_scanned"] == 1168
    assert "Event Family" in format_chat_noise_report()
    assert "Примеры отсева" in format_reject_samples_report()
    assert "шум" in format_reject_samples_report()


def test_manual_summary_shows_chats_and_zero_hint():
    stats = _new_stats("manual")
    stats["chats_ok"] = 36
    stats["chats_total"] = 36
    stats["messages_scanned"] = 0
    _mark_stats_finished(stats)

    text = format_scan_finished_summary(stats)
    assert "Ручная проверка" in text
    assert "36/36" in text
    assert "realtime" in text.lower() or "⚡" in text


def test_get_stats_for_filter_reports_prefers_audit_with_samples():
    manual = _new_stats("manual")
    manual["messages_scanned"] = 5
    _mark_stats_finished(manual)

    audit = _new_stats("audit")
    audit["reject_samples"] = [{"chat": "X", "reason": "casting", "preview": "test"}]
    _mark_stats_finished(audit)

    assert get_stats_for_filter_reports()["run_kind"] == "audit"
