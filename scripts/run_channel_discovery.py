#!/usr/bin/env python3
"""Запуск telegram-parser по ключам Hunter и выгрузка Excel для ручного отбора чатов."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from openpyxl import Workbook
from openpyxl.styles import Font

HUNTER_ROOT = Path(__file__).resolve().parents[1]
PARSER_ROOT = Path(
    os.environ.get(
        "TELEGRAM_PARSER_ROOT",
        r"C:\Users\Яр\Documents\GitHub\telegram-parser-main\telegram-parser-main",
    )
)
QUERIES_SRC = Path(__file__).with_name("channel_discovery_queries.txt")
OUTPUT_DIR = HUNTER_ROOT / "data" / "channel_discovery"
ACCOUNTS_DIR = PARSER_ROOT / "Accounts"
SESSION_COPY = ACCOUNTS_DIR / "hunter_telethon.session"
ACCOUNT_JSON = ACCOUNTS_DIR / "hunter.json"

# Названия каналов с такими словами — почти всегда не hiring-чаты
_JUNK_TITLE_RE = re.compile(
    r"wildberries|вайлдберри|ozon|озон|скидк|промокод|кешбэк|крипт|казино|ставк|"
    r"1xbet|forex|mlm|инфобиз|знакомств|dating|эскорт|наркот|betting|"
    r"wb скидки|маркетплейс скидки|купоны|кешбек",
    re.IGNORECASE,
)


def _discovery_min_members() -> int:
    raw = os.environ.get("DISCOVERY_MIN_MEMBERS", "50").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 50


def _load_queries(path: Path) -> list[str]:
    if not path.is_file():
        return []
    queries: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        queries.append(line)
    return queries


def _queries_source() -> Path:
    raw = os.environ.get("DISCOVERY_QUERIES_FILE", "").strip()
    if raw:
        path = Path(raw)
        if not path.is_absolute():
            path = HUNTER_ROOT / path
        return path
    return QUERIES_SRC


def _write_queries_for_parser(queries_dst: Path) -> int:
    src = _queries_source()
    queries = _load_queries(src)
    if not queries:
        raise FileNotFoundError(f"Нет запросов в {src}")
    queries_dst.write_text("\n".join(queries) + "\n", encoding="utf-8")
    return len(queries)


def _filter_discovery_rows(rows: list[dict], *, min_members: int) -> tuple[list[dict], dict[str, int]]:
    kept: list[dict] = []
    stats = {"low_members": 0, "junk_title": 0}
    for row in rows:
        members = row.get("members")
        if members is not None and members < min_members:
            stats["low_members"] += 1
            continue
        title = (row.get("title") or "").strip()
        if title and _JUNK_TITLE_RE.search(title):
            stats["junk_title"] += 1
            continue
        kept.append(row)
    return kept, stats


def _normalize_link(link: str) -> str:
    link = (link or "").strip()
    if link.startswith("https://t.me/"):
        return link.rstrip("/").lower()
    if link.startswith("t.me/"):
        return f"https://{link.rstrip('/').lower()}"
    m = re.match(r"^@?([A-Za-z0-9_]{3,})$", link)
    if m:
        return f"https://t.me/{m.group(1).lower()}"
    return link.lower()


def _existing_bot_links() -> set[str]:
    sys.path.insert(0, str(HUNTER_ROOT))
    from config import TARGET_CHATS  # noqa: WPS433

    links = {_normalize_link(x) for x in TARGET_CHATS}
    links.discard("")
    return links


def _discovery_session_path() -> Path:
    """Сессия для discovery: отдельный аккаунт, не user_session прод-парсера."""
    name = os.environ.get("DISCOVERY_SESSION", "discovery_session.session").strip()
    if not name.endswith(".session"):
        name = f"{name}.session"
    path = HUNTER_ROOT / name
    if path.is_file():
        return path
    legacy = HUNTER_ROOT / "user_session.session"
    if legacy.is_file():
        return legacy
    return path


def _setup_parser_account(api_id: int, api_hash: str) -> None:
    src_session = _discovery_session_path()
    if not src_session.is_file():
        raise FileNotFoundError(
            f"Нет Telethon-сессии: {src_session}. "
            "Создайте discovery_session.session (см. docs/CHANNEL_DISCOVERY.md) "
            "или задайте DISCOVERY_SESSION в окружении."
        )
    ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_session, SESSION_COPY)
    ACCOUNT_JSON.write_text(
        json.dumps(
            {
                "app_id": api_id,
                "app_hash": api_hash,
                "session_file": SESSION_COPY.name,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _write_parser_env(api_id: int, api_hash: str) -> int:
    env_path = PARSER_ROOT / ".env"
    queries_dst = PARSER_ROOT / "queries.txt"
    query_count = _write_queries_for_parser(queries_dst)
    env_path.write_text(
        "\n".join(
            [
                f"TG_API_ID={api_id}",
                f"TG_API_HASH={api_hash}",
                "SEARCH_TYPE=all",
                "LIMIT=400",
                "DEEP_SEARCH=0",
                "ACCOUNTS_DIR=Accounts",
                "DEAD_DIR=Accounts/dead",
                "PROXY_FILE=proxy.txt",
                "QUERIES_FILE=queries.txt",
                "RESULTS_CHANNELS=results_channels.txt",
                "RESULTS_CHATS=results_chats.txt",
                "LOG_LEVEL=INFO",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return query_count


def _parse_result_line(line: str) -> tuple[str, int | None, str] | None:
    line = line.strip()
    if not line or "|" not in line:
        return None
    parts = [p.strip() for p in line.split("|")]
    if len(parts) < 3:
        return None
    title = parts[0]
    members_raw = parts[1]
    link = parts[2]
    try:
        members = int(members_raw)
    except ValueError:
        members = None
    return title, members, link


def _read_results(path: Path, kind: str) -> list[dict]:
    if not path.is_file():
        return []
    rows: list[dict] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_result_line(line)
        if not parsed:
            continue
        title, members, link = parsed
        norm = _normalize_link(link) if link.startswith("http") or link.startswith("t.me") else link
        dedupe_key = norm if norm.startswith("https://") else f"{kind}:{title.lower()}"
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        rows.append(
            {
                "type": kind,
                "title": title,
                "members": members,
                "link": link,
                "normalized_link": norm,
            }
        )
    return rows


def _export_excel(rows: list[dict], existing: set[str], out_path: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "channels"
    headers = [
        "Тип",
        "Название",
        "Участники",
        "Ссылка",
        "Уже в боте",
        "Добавить в бот",
        "Заметки",
    ]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)

    for row in sorted(rows, key=lambda r: (-(r["members"] or 0), r["title"].lower())):
        norm = row["normalized_link"]
        already = "да" if norm in existing else ""
        ws.append(
            [
                row["type"],
                row["title"],
                row["members"] if row["members"] is not None else "",
                row["link"],
                already,
                "",
                "",
            ]
        )

    for col, width in zip("ABCDEFG", (10, 42, 12, 36, 12, 14, 30), strict=False):
        ws.column_dimensions[col].width = width

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)


def main() -> int:
    load_dotenv(HUNTER_ROOT / ".env")
    api_id = int(os.getenv("API_ID", "0") or "0")
    api_hash = (os.getenv("API_HASH") or "").strip()
    if not api_id or not api_hash:
        print("Задайте API_ID и API_HASH в .env Hunter.")
        return 1
    if not PARSER_ROOT.is_dir():
        print(f"Не найден telegram-parser: {PARSER_ROOT}")
        return 1

    print("1/4 Настройка аккаунта и .env…")
    print(
        "⚠️  ВАЖНО: не запускайте discovery, пока Hunter на сервере парсит чаты "
        "той же user_session — иначе Telethon на проде получит TypeNotFoundError."
    )
    _setup_parser_account(api_id, api_hash)
    min_members = _discovery_min_members()
    query_count = _write_parser_env(api_id, api_hash)
    print(f"Запросов для поиска: {query_count} | мин. участников в Excel: {min_members}")

    for fname in ("results_channels.txt", "results_chats.txt"):
        path = PARSER_ROOT / fname
        if path.exists():
            path.unlink()

    print("2/4 Запуск telegram-parser (поиск каналов/чатов)…")
    proc = subprocess.run(
        [sys.executable, "main.py"],
        cwd=str(PARSER_ROOT),
        check=False,
    )
    if proc.returncode != 0:
        print(f"telegram-parser завершился с кодом {proc.returncode}")
        return proc.returncode

    print("3/4 Сбор результатов…")
    channels = _read_results(PARSER_ROOT / "results_channels.txt", "канал")
    chats = _read_results(PARSER_ROOT / "results_chats.txt", "чат")
    raw_rows = channels + chats
    all_rows, filt = _filter_discovery_rows(raw_rows, min_members=min_members)
    existing = _existing_bot_links()

    today = date.today().isoformat()
    out_xlsx = OUTPUT_DIR / f"channel_discovery_{today}.xlsx"
    print("4/4 Excel…")
    _export_excel(all_rows, existing, out_xlsx)

    new_count = sum(1 for r in all_rows if r["normalized_link"] not in existing)
    skipped = len(raw_rows) - len(all_rows)
    print(
        f"Готово: {len(all_rows)} в Excel ({new_count} новых для бота). "
        f"Отфильтровано: {skipped} "
        f"(мало участников: {filt['low_members']}, мусор в названии: {filt['junk_title']})."
    )
    print(f"Файл: {out_xlsx}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
