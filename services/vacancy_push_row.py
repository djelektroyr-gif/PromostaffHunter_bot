"""Индексы колонок get_vacancy_push_row() — держать в sync с db.get_vacancy_push_row SELECT."""

from __future__ import annotations

# message_text, message_link, source_chat_title, author_contact, address,
# category_code, source_chat, dedupe_key, published_at, poster_user_id,
# poster_username, moderation_status, posted_by_bot_user_id,
# address_normalized, location_lat, location_lon,
# geo_tags, rate_hourly, rate_shift, min_hours, rate_effective_hourly,
# shift_date, shift_time_start
PUSH_IDX_MODERATION = 11
PUSH_IDX_ADDRESS_NORM = 13
PUSH_IDX_LAT = 14
PUSH_IDX_LON = 15
PUSH_IDX_GEO_TAGS = 16
PUSH_IDX_RATE_HOURLY = 17
PUSH_IDX_RATE_SHIFT = 18
PUSH_IDX_MIN_HOURS = 19
PUSH_IDX_RATE_EFFECTIVE = 20
PUSH_IDX_SHIFT_DATE = 21
PUSH_IDX_SHIFT_TIME_START = 22


def push_field(row, index: int, default=None):
    if not row or len(row) <= index:
        return default
    return row[index]
