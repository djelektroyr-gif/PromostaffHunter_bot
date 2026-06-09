"""Тесты rate-limit алертов админу."""

from services.admin_ops_alerts import _should_alert_error


def test_error_alert_rate_limit():
    assert _should_alert_error("handler_a:err1") is True
    assert _should_alert_error("handler_a:err1") is False
    assert _should_alert_error("handler_b:err2") is True
