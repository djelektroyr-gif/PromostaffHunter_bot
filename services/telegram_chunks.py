"""Разбивка длинного текста для Telegram (без порчи HTML)."""

from __future__ import annotations


def chunk_text_for_telegram(text: str, max_len: int = 3800) -> list[str]:
    """Части для send/edit — по строкам, без разреза HTML-тегов посередине."""
    if not text:
        return [""]
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    buf: list[str] = []
    buf_len = 0
    for line in text.split("\n"):
        piece = line + "\n"
        if len(piece) > max_len:
            if buf:
                chunks.append("\n".join(buf).rstrip("\n"))
                buf, buf_len = [], 0
            start = 0
            while start < len(line):
                chunks.append(line[start:start + max_len])
                start += max_len
            continue
        if buf and buf_len + len(piece) > max_len:
            chunks.append("\n".join(buf).rstrip("\n"))
            buf = [line]
            buf_len = len(piece)
        else:
            buf.append(line)
            buf_len += len(piece)
    if buf:
        chunks.append("\n".join(buf).rstrip("\n"))
    return chunks
