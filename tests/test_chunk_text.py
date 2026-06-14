# -*- coding: utf-8 -*-
"""Безопасная разбивка длинных HTML-отчётов для Telegram."""

from parser import format_parser_chats_report
from services.telegram_chunks import chunk_text_for_telegram


def test_chunk_text_splits_by_lines_not_mid_tag():
    report = format_parser_chats_report(
        [
            {
                "status": "ok",
                "title": f"Chat {i}",
                "chat_link": f"@ch{i}",
                "chat_id": f"-100{i}",
                "monitored": True,
            }
            for i in range(80)
        ],
        "online",
    )
    chunks = chunk_text_for_telegram(report, max_len=500)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 500
        assert chunk.count("<code>") == chunk.count("</code>")
        assert chunk.count("<b>") == chunk.count("</b>")


def test_chunk_text_short_passthrough():
    assert chunk_text_for_telegram("ok", max_len=100) == ["ok"]
