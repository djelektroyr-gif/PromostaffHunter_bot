"""Обогащение вакансии: адрес, geo_tags, ставка, координаты, время смены."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import quote

from services.channel_rate import extract_hourly_rate_rub, extract_min_hours, extract_shift_rate_rub

ENRICHMENT_VERSION = 3

_CITY_STREET_RE = re.compile(
    r"\b("
    r"Москва|МО|Подольск|Химки|Мытищи|Красногорск|Люберцы|Балашиха|"
    r"Корол[её]в|Одинцово|Домодедово|Железнодорожный|Видное|Щ[её]лково|"
    r"Электросталь|Коломна|Серпухов|Реутов|Долгопрудный|Пушкино|Лобня"
    r")\b[\s,]*"
    r"((?:ул\.|улица|пр-т|проспект|пер\.|переулок|шоссе|наб\.|набережная|"
    r"бульвар|б-р|проезд|аллея)\s+[А-Яа-яёЁ0-9\-\.\s]{3,80}(?:,\s*\d+[А-Яа-яёЁA-Za-z0-9\/-]*)?)",
    re.IGNORECASE,
)
_VENUE_RE = re.compile(
    r"\b((?:ТЦ|ТРЦ|ТК|МФК|БЦ|ТРК)\s+[«\"]?[А-Яа-яёЁ0-9\- ]{2,60}[»\"]?)",
    re.IGNORECASE,
)
_LOCATION_LABELS = (
    r"адрес|локация|место(?:\s+работы)?|"
    r"где|точка(?:\s+сбора)?|район|объект|площадка|выход"
)
_EXPLICIT_ADDR_RE = re.compile(
    rf"(?:{_LOCATION_LABELS})\s*[:\-]\s*([^\n]{{4,140}})",
    re.IGNORECASE,
)
_METRO_ADDR_RE = re.compile(
    r"(?:м\.|метро)\s*[:\-]?\s*(?:🚇\s*)?([А-Яа-яёЁA-Za-z\- ]{2,40})",
    re.IGNORECASE,
)
_LOCATION_HEADER_RE = re.compile(
    rf"^(?:[📍🗺]\s*)?(?:\*\*)?(?:{_LOCATION_LABELS}|м\.|метро)(?:\*\*)?\s*[:\-]?\s*(.*?)\s*$",
    re.IGNORECASE,
)
_PIN_INLINE_RE = re.compile(
    r"^[📍🗺]\s*(.+)$",
)
_STREET_FRAGMENT_RE = re.compile(
    r"\b((?:ул\.|улица|пр-т|проспект|пер\.|переулок|шоссе|наб\.|набережная|"
    r"бульвар|б-р|проезд|аллея)\s+[^\n,]{3,80}(?:,\s*\d+[А-Яа-яёЁA-Za-z0-9\/-]*)?)",
    re.IGNORECASE,
)
_NEXT_SECTION_RE = re.compile(
    r"^(?:[📋💰📲⏰📅👷🔒✅❌📢🕐📍🚇]|\*\*(?:ТРЕБОВАН|ОПЛАТ|ДАТА|ВРЕМЯ|ФУНКЦ))",
    re.IGNORECASE,
)
_BOULEVARD_ONLY_RE = re.compile(
    r"\b([А-Яа-яёЁ][А-Яа-яёЁ\-]{1,40}\s+бульвар)\b",
    re.IGNORECASE,
)
_YANDEX_LL_RE = re.compile(
    r"yandex\.(?:ru|com)/maps[^\s?]*\?(?:[^\s]*&)?ll=(\d{1,3}\.\d+)[,\s%2C]+(\d{1,3}\.\d+)",
    re.IGNORECASE,
)
_GOOGLE_COORD_RE = re.compile(
    r"(?:maps\.google\.com|google\.com/maps)[^\s]*?(?:@|q=|\?)(-?\d{1,2}\.\d+)[,\s]+(-?\d{1,3}\.\d+)",
    re.IGNORECASE,
)
_GEO_URI_RE = re.compile(
    r"geo:(-?\d{1,2}\.\d+),(-?\d{1,3}\.\d+)",
    re.IGNORECASE,
)
_RAW_COORD_RE = re.compile(
    r"(?<!\d)(-?\d{1,2}\.\d{4,})[,\s]+(-?\d{1,3}\.\d{4,})(?!\d)",
)
_SHIFT_TIME_RE = re.compile(
    r"(?:^|[\s,])(?:к|с)\s*(\d{1,2})[:.](\d{2})(?:\s|$|[,\.])",
    re.IGNORECASE | re.MULTILINE,
)
_TODAY_TOMORROW_RE = (
    (re.compile(r"\bна\s+сегодня\b", re.I), "today"),
    (re.compile(r"\bна\s+завтра\b", re.I), "tomorrow"),
    (re.compile(r"\bсегодня\b", re.I), "today"),
    (re.compile(r"\bзавтра\b", re.I), "tomorrow"),
)


@dataclass
class VacancyEnrichment:
    address_normalized: str | None = None
    geo_tags: list[str] | None = None
    rate_hourly: int | None = None
    rate_shift: int | None = None
    min_hours: int | None = None
    rate_effective_hourly: int | None = None
    shift_date: str | None = None
    shift_time_start: str | None = None
    location_lat: float | None = None
    location_lon: float | None = None
    enrichment_version: int = ENRICHMENT_VERSION

    def to_db_kwargs(self) -> dict[str, Any]:
        data = asdict(self)
        tags = data.pop("geo_tags", None)
        data["geo_tags"] = json.dumps(tags, ensure_ascii=False) if tags else None
        return data


@lru_cache(maxsize=1)
def _load_city_catalog() -> list[tuple[str, tuple[str, ...]]]:
    path = Path(__file__).resolve().parent.parent / "assets" / "mo_city_catalog.json"
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    out: list[tuple[str, tuple[str, ...]]] = []
    for item in raw:
        slug = item.get("slug")
        names = item.get("names") or []
        if slug and names:
            out.append((slug, tuple(n.lower() for n in names)))
    return out


def extract_coordinates_from_text(text: str) -> tuple[float | None, float | None]:
    if not text:
        return None, None
    for pattern in (_YANDEX_LL_RE, _GEO_URI_RE, _GOOGLE_COORD_RE, _RAW_COORD_RE):
        match = pattern.search(text)
        if not match:
            continue
        try:
            a, b = float(match.group(1)), float(match.group(2))
        except (ValueError, IndexError):
            continue
        if pattern is _YANDEX_LL_RE:
            lon, lat = a, b
        else:
            lat, lon = a, b
        if 40.0 <= lat <= 60.0 and 30.0 <= lon <= 50.0:
            return lat, lon
    return None, None


def _compose_location_address(parts: list[str]) -> str:
    """Склеивает строки блока «ЛОКАЦИЯ» в адрес для карты."""
    cleaned = [p.strip(" .,*#") for p in parts if p and len(p.strip()) >= 2]
    if not cleaned:
        return ""

    blob = " ".join(cleaned)
    has_city = bool(re.search(r"\b(?:москва|мо)\b", blob, re.I))
    if not has_city:
        for slug, names in _load_city_catalog():
            if slug in ("moscow", "mo"):
                continue
            for name in names:
                if re.search(rf"\b{re.escape(name)}\b", blob, re.I):
                    has_city = True
                    break
            if has_city:
                break

    streets: list[str] = []
    metro: str | None = None

    for p in cleaned:
        p = re.sub(r"^🚇\s*", "", p).strip()
        pl = p.lower()
        if re.match(r"^(?:м\.|метро)\b", pl):
            station = re.sub(r"^(?:м\.|метро)\s*", "", p, flags=re.I).strip()
            if station:
                metro = f"метро {station}"
            continue
        if re.search(
            r"бульвар|ул\.|улица|пр-т|проспект|пер\.|шоссе|наб\.|набережная|пр\.|б-р",
            pl,
        ):
            streets.append(p)
            continue
        if streets and len(p.split()) <= 2 and not re.search(r"\d", p):
            metro = f"метро {p}"
            continue
        streets.append(p)

    out: list[str] = []
    if not has_city and streets and re.search(r"бульвар|проспект|пр-т", " ".join(streets), re.I):
        out.append("Москва")
    out.extend(streets)
    if metro:
        out.append(metro)
    return ", ".join(out)


def _extract_location_block(text: str) -> str | None:
    """Блок «📍 ЛОКАЦИЯ» с адресом на следующих строках (частый формат постов)."""
    if not text:
        return None
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = _LOCATION_HEADER_RE.match(line.strip())
        if not m:
            continue
        inline = m.group(1).strip().strip("*")
        parts = _collect_lines_after_header(lines, i, inline)
        composed = _compose_location_address(parts)
        if composed:
            if re.match(
                r"^(?:[📍🗺]\s*)?(?:\*\*)?(?:м\.|метро)(?:\*\*)?",
                line.strip(),
                re.I,
            ) and "метро" not in composed.lower():
                composed = f"метро {composed}"
            return composed
    return None


_LABEL_ONLY_WORDS = frozenset(
    {
        "локация", "адрес", "место", "где", "точка", "район", "объект",
        "площадка", "выход", "метро", "м", "м.", "точка сбора",
    }
)


def _is_label_only(value: str) -> bool:
    cleaned = value.strip().strip("*").lower().rstrip(":.-")
    return cleaned in _LABEL_ONLY_WORDS or cleaned in ("место работы",)


def _collect_lines_after_header(lines: list[str], start: int, inline: str) -> list[str]:
    parts: list[str] = []
    if inline and len(inline) >= 3 and not _is_label_only(inline):
        parts.append(inline)
    for j in range(start + 1, min(start + 5, len(lines))):
        nxt = lines[j].strip().strip("*").strip()
        if not nxt:
            continue
        if _NEXT_SECTION_RE.match(nxt):
            break
        parts.append(nxt)
        if len(parts) >= 3:
            break
    return parts


def _extract_pin_inline_address(text: str) -> str | None:
    """Строка «📍 Москва, ул. …» или «📍 Страстной бульвар» без заголовка секции."""
    if not text:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        m = _PIN_INLINE_RE.match(stripped)
        if not m:
            continue
        if _LOCATION_HEADER_RE.match(stripped):
            continue
        content = m.group(1).strip().strip("*")
        if len(content) < 4 or _is_label_only(content):
            continue
        composed = _compose_location_address([content])
        return composed or content
    return None


def _extract_freeform_address(text: str) -> str | None:
    """Свободная форма: склеивает улицу/бульвар/ТЦ и метро из разных мест текста."""
    if not text:
        return None

    metros: list[str] = []
    for match in _METRO_ADDR_RE.finditer(text):
        station = match.group(1).strip(" .,")
        if station and len(station) >= 2 and station.lower() not in _LABEL_ONLY_WORDS:
            metros.append(station)

    streets: list[str] = []
    city_street = _CITY_STREET_RE.search(text)
    if city_street:
        streets.append(f"{city_street.group(1).strip()}, {city_street.group(2).strip(' .,')}")
    for match in _BOULEVARD_ONLY_RE.finditer(text):
        frag = match.group(1).strip()
        if frag and frag not in streets:
            streets.append(frag)
    for match in _STREET_FRAGMENT_RE.finditer(text):
        frag = match.group(1).strip(" .,")
        if frag and frag not in streets:
            streets.append(frag)

    venues: list[str] = []
    for match in _VENUE_RE.finditer(text):
        venue = match.group(1).strip()
        if venue and venue not in venues:
            venues.append(venue)

    has_moscow = bool(re.search(r"\b(?:москва|мо)\b", text, re.I))

    if streets and metros:
        out: list[str] = []
        if not has_moscow and re.search(r"бульвар|проспект|пр-т|ул\.|улица", streets[0], re.I):
            out.append("Москва")
        out.append(streets[0])
        out.append(f"метро {metros[0]}")
        return ", ".join(out)

    if venues:
        venue = venues[0]
        city_slug = _match_city_slug(text)
        if city_slug and city_slug not in ("moscow", "mo"):
            for slug, names in _load_city_catalog():
                if slug == city_slug:
                    display = names[0].title()
                    break
            else:
                display = city_slug.title()
            base = f"{display}, {venue}"
        elif has_moscow:
            base = f"Москва, {venue}"
        else:
            base = venue
        if metros:
            return f"{base}, метро {metros[0]}"
        return base

    if streets:
        street = streets[0]
        if not has_moscow and re.search(r"бульвар|проспект|пр-т", street, re.I):
            return f"Москва, {street}"
        return street

    return None


def extract_address_normalized(text: str, legacy_address: str | None = None) -> str | None:
    if not text:
        return legacy_address.strip() if legacy_address else None
    block = _extract_location_block(text)
    if block:
        return block
    pin_line = _extract_pin_inline_address(text)
    if pin_line:
        return pin_line
    explicit = _EXPLICIT_ADDR_RE.search(text)
    if explicit:
        return explicit.group(1).strip(" .,")
    city_street = _CITY_STREET_RE.search(text)
    if city_street:
        city = city_street.group(1).strip()
        street = city_street.group(2).strip(" .,")
        return f"{city}, {street}"
    venue = _VENUE_RE.search(text)
    if venue:
        venue_text = venue.group(1).strip()
        city_match = _match_city_slug(text)
        if city_match and city_match != "moscow":
            display = next(
                (n for slug, names in _load_city_catalog() if slug == city_match for n in names),
                city_match,
            )
            return f"{display.title()}, {venue_text}"
        return venue_text
    freeform = _extract_freeform_address(text)
    if freeform:
        return freeform
    metro = _METRO_ADDR_RE.search(text)
    if metro:
        station = metro.group(1).strip(" .,")
        if re.search(r"\bмосква\b", text, re.I):
            return f"Москва, метро {station}"
        return f"метро {station}"
    boulevard = _BOULEVARD_ONLY_RE.search(text)
    if boulevard:
        return f"Москва, {boulevard.group(1).strip()}"
    if legacy_address and legacy_address.strip():
        return legacy_address.strip()
    city_only = _match_city_slug(text)
    if city_only:
        for slug, names in _load_city_catalog():
            if slug == city_only:
                return names[0].title()
    return None


def _match_city_slug(text: str) -> str | None:
    if not text:
        return None
    tl = text.lower()
    for slug, names in _load_city_catalog():
        for name in names:
            if re.search(rf"\b{re.escape(name)}\b", tl):
                return slug
    return None


def build_geo_tags(text: str, address_normalized: str | None = None) -> list[str]:
    combined = f"{text or ''} {address_normalized or ''}"
    if not combined.strip():
        return []
    tags: set[str] = set()
    tl = combined.lower()
    if re.search(r"\bмосква\b", tl) and not re.search(r"\bмосковск", tl):
        tags.add("moscow")
    if re.search(r"\b(?:мо|московск(?:ая|ой)\s+област)\b", tl):
        tags.add("mo")
    city = _match_city_slug(combined)
    if city and city not in ("moscow", "mo"):
        tags.add(city)
    try:
        from parser import extract_metro_tokens
    except ImportError:
        extract_metro_tokens = None
    if extract_metro_tokens:
        for token in extract_metro_tokens(combined):
            if token:
                tags.add(f"metro:{token}")
    return sorted(tags)


def extract_shift_date_token(text: str) -> str | None:
    if not text:
        return None
    for pattern, token in _TODAY_TOMORROW_RE:
        if pattern.search(text):
            return token
    return None


def extract_shift_time_start(text: str) -> str | None:
    if not text:
        return None
    match = _SHIFT_TIME_RE.search(text)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return f"{hour:02d}:{minute:02d}"
    return None


def _calc_effective_hourly(
    rate_hourly: int | None, rate_shift: int | None, min_hours: int | None,
) -> int | None:
    if rate_hourly is not None:
        return rate_hourly
    if rate_shift is not None and min_hours and min_hours > 0:
        return rate_shift // min_hours
    return None


def enrich_vacancy_text(
    text: str,
    *,
    legacy_address: str | None = None,
    location_lat: float | None = None,
    location_lon: float | None = None,
) -> VacancyEnrichment:
    lat, lon = location_lat, location_lon
    if lat is None or lon is None:
        t_lat, t_lon = extract_coordinates_from_text(text or "")
        lat = lat if lat is not None else t_lat
        lon = lon if lon is not None else t_lon

    address_normalized = extract_address_normalized(text, legacy_address)
    rate_hourly = extract_hourly_rate_rub(text or "")
    rate_shift = extract_shift_rate_rub(text or "")
    min_hours = extract_min_hours(text or "")
    rate_effective = _calc_effective_hourly(rate_hourly, rate_shift, min_hours)

    return VacancyEnrichment(
        address_normalized=address_normalized,
        geo_tags=build_geo_tags(text or "", address_normalized),
        rate_hourly=rate_hourly,
        rate_shift=rate_shift,
        min_hours=min_hours,
        rate_effective_hourly=rate_effective,
        shift_date=extract_shift_date_token(text or ""),
        shift_time_start=extract_shift_time_start(text or ""),
        location_lat=lat,
        location_lon=lon,
        enrichment_version=ENRICHMENT_VERSION,
    )


def build_maps_url(
    *,
    address_normalized: str | None = None,
    address: str | None = None,
    location_lat: float | None = None,
    location_lon: float | None = None,
) -> str | None:
    if location_lat is not None and location_lon is not None:
        url = f"https://yandex.ru/maps/?ll={location_lon},{location_lat}&z=16&pt={location_lon},{location_lat}"
        if len(url) <= 2048:
            return url
    text_addr = (address_normalized or address or "").strip()
    if text_addr:
        url = f"https://yandex.ru/maps/?text={quote(text_addr)}"
        if len(url) <= 2048 and url.startswith("https://"):
            return url
    return None


def enrichment_from_row(row: dict) -> VacancyEnrichment:
    geo_tags = row.get("geo_tags")
    if isinstance(geo_tags, str):
        try:
            geo_tags = json.loads(geo_tags)
        except json.JSONDecodeError:
            geo_tags = None
    return VacancyEnrichment(
        address_normalized=row.get("address_normalized"),
        geo_tags=geo_tags,
        rate_hourly=row.get("rate_hourly"),
        rate_shift=row.get("rate_shift"),
        min_hours=row.get("min_hours"),
        rate_effective_hourly=row.get("rate_effective_hourly"),
        shift_date=row.get("shift_date"),
        shift_time_start=row.get("shift_time_start"),
        location_lat=row.get("location_lat"),
        location_lon=row.get("location_lon"),
        enrichment_version=row.get("enrichment_version") or 0,
    )
