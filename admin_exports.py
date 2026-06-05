"""Выгрузки Excel для админки (openpyxl)."""

from __future__ import annotations

from datetime import datetime
from io import BytesIO

import openpyxl
from openpyxl.utils import get_column_letter


def _autosize_columns(ws, max_width: int = 48):
    for col_idx, column_cells in enumerate(ws.columns, 1):
        length = 0
        for cell in column_cells:
            if cell.value is not None:
                length = max(length, min(len(str(cell.value)), max_width))
        ws.column_dimensions[get_column_letter(col_idx)].width = max(length + 2, 10)


def _workbook_to_bytes(wb: openpyxl.Workbook) -> bytes:
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _write_sheet(ws, headers: list[str], rows: list[list]):
    ws.append(headers)
    for row in rows:
        ws.append(row)
    _autosize_columns(ws)


def build_subscribers_xlsx(rows: list[dict]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "subscribers"
    headers = [
        "user_id", "username", "full_name", "first_name", "last_name", "phone", "age",
        "birth_date", "user_role", "plan", "paid_until", "trial_used", "metro_zones",
        "categories", "registered_at", "is_active", "has_photo", "resume_extra",
    ]
    data = []
    for r in rows:
        data.append([
            r.get("user_id"), r.get("username"), r.get("full_name"), r.get("first_name"),
            r.get("last_name"), r.get("phone"), r.get("age"), r.get("birth_date"),
            r.get("user_role"), r.get("plan"), r.get("paid_until"), r.get("trial_used"),
            r.get("metro_zones"), r.get("categories"), r.get("registered_at"),
            r.get("is_active"), r.get("has_photo"), r.get("resume_extra"),
        ])
    _write_sheet(ws, headers, data)
    return _workbook_to_bytes(wb)


def build_vacancies_xlsx(rows: list[dict]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "vacancies"
    headers = [
        "id", "category", "source_chat_title", "author_contact", "contact_source",
        "poster_user_id", "poster_username", "poster_display_name", "employer_id",
        "posted_by_bot_user_id", "address", "published_at", "found_at", "is_closed",
        "message_link", "message_text",
    ]
    data = []
    for r in rows:
        data.append([
            r.get("id"), r.get("category_code"), r.get("source_chat_title"),
            r.get("author_contact"), r.get("contact_source"), r.get("poster_user_id"),
            r.get("poster_username"), r.get("poster_display_name"), r.get("employer_id"),
            r.get("posted_by_bot_user_id"), r.get("address"), r.get("published_at"),
            r.get("found_at"), r.get("is_closed"), r.get("message_link"), r.get("message_text"),
        ])
    _write_sheet(ws, headers, data)
    return _workbook_to_bytes(wb)


NOTFIT_REASON_LABELS = {
    "wrong_category": "Не та категория / роль",
    "low_pay": "Мало платят",
    "wrong_area": "Не мой район / далеко",
    "spam": "Спам или не вакансия",
    "duplicate": "Уже видел / повтор",
    "other": "Другое",
}


def build_notfit_xlsx(rows: list[dict]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "notfit"
    headers = [
        "id", "created_at", "user_id", "username", "full_name",
        "reason_code", "reason_label", "reason_text",
        "vacancy_id", "vacancy_category", "user_categories",
        "source_chat", "message_link", "message_text",
    ]
    data = []
    for r in rows:
        code = r.get("reason_code") or ""
        data.append([
            r.get("id"), r.get("created_at"), r.get("user_id"), r.get("username"),
            r.get("full_name") or r.get("first_name"),
            code, NOTFIT_REASON_LABELS.get(code, code), r.get("reason_text"),
            r.get("vacancy_id"), r.get("vacancy_category") or r.get("vacancy_category_live"),
            r.get("user_categories"), r.get("source_chat_title"), r.get("message_link"),
            r.get("message_text"),
        ])
    _write_sheet(ws, headers, data)
    return _workbook_to_bytes(wb)


def build_employers_xlsx(rows: list[dict]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "employers"
    headers = [
        "id", "telegram_user_id", "username", "display_name", "contact_text", "contact_source",
        "vacancies_count", "categories_csv", "bot_user_id", "first_seen_at", "last_seen_at",
    ]
    data = []
    for r in rows:
        data.append([
            r.get("id"), r.get("telegram_user_id"), r.get("username"), r.get("display_name"),
            r.get("contact_text"), r.get("contact_source"), r.get("vacancies_count"),
            r.get("categories_csv"), r.get("bot_user_id"), r.get("first_seen_at"),
            r.get("last_seen_at"),
        ])
    _write_sheet(ws, headers, data)
    return _workbook_to_bytes(wb)


def export_filename(prefix: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    return f"{prefix}_{stamp}.xlsx"
