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

ENRICHMENT_VERSION = 5

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
    r"\b((?:ТЦ|ТРЦ|ТК|МФК|БЦ|ТРК)\s+[«\"]?[А-Яа-яёЁ0-9\- ]{2,60}[»\"]?|"
    r"(?:торговый центр|тц)\s+[«\"]?[А-Яа-яёЁ0-9\- ]{2,60}[»\"]?)",
    re.IGNORECASE,
)
_NAMED_VENUE_RE = re.compile(
    r"\b("
    r"(?:казанский|ленинградский|курский|ярославский|павелецкий|киевский|"
    r"белорусский|савёловский|рижский)\s+вокзал|"
    r"московская\s+консерватория|"
    r"платинум\s+арена"
    r")\b",
    re.IGNORECASE,
)
_MOSCOW_COMMA_LINE_RE = re.compile(
    r"(?:^|\n)\s*Москва\s*,\s*([^\n]{4,100}?)(?:\s*$|\s*(?:задача|оплат|@))",
    re.IGNORECASE | re.MULTILINE,
)
_METRO_TC_LINE_RE = re.compile(
    r"(?:мцк|м\.|метро)\s+([А-Яа-яёЁ\- ]{2,40}).{0,30}?"
    r"(?:торговый центр|тц)\s+([«\"]?[А-Яа-яёЁ0-9\- ]{2,50}[»\"]?)",
    re.IGNORECASE,
)
_CITY_LOCATIVE_RE = re.compile(
    r"\b(?:в|на)\s+([А-Яа-яёЁ][а-яёЁ\-]{2,30}(?:е|у|и|о))\b",
    re.IGNORECASE,
)
_REGIONAL_CITY_LINE_RE = re.compile(
    r"(?:^|\n)\s*([А-ЯЁ]{4,24})\s*(?:\n|$)",
)
_STREET_WITH_NUMBER_RE = re.compile(
    r"(?:"
    r"(?:ул\.?|улиц[аеи])\s+([А-Яа-яёЁ][А-Яа-яёЁa-z\- ]{1,40}\d{1,4}[а-яёa-z]?(?:с\d+)?)|"
    r"([А-Яа-яёЁ][а-яёЁ\-]{1,40}\s+бульвар\s+\d{1,4}[а-яёa-z]?(?:с\d+)?)"
    r")",
    re.IGNORECASE,
)
_ADDR_TASK_NOISE_RE = re.compile(
    r"фото\s*[-–—]\s*\d|"
    r"нужно\s+будет\s+взять|"
    r"взять\s+с\s+собой|"
    r"сюда\s+еще\s+нужн|"
    r"координатор\w*\s+\d|"
    r"требуется\s+на\s+подработку|"
    r"требуется\s+\d+\s*человек|"
    r"^\s*оплат\w*\s+\d",
    re.IGNORECASE,
)
_LANDMARK_RE = re.compile(
    r"\b(вднх|vdnh|выставочн\w*(?:\s+центр\w*)?(?:\s+вднх)?|экспоцентр|сокольники|лужники|"
    r"гостин(?:ый|ого)\s+двор|парк\s+гор(?:ь|и)к(?:ого)?)\b",
    re.IGNORECASE,
)
_GARBAGE_ADDR_RE = re.compile(
    r"кандидат|рассмотрим|опыт\s+работ|активн\w*\s+промо|старше\s+\d|"
    r"только\s+(?:актив|промо|опыт)|отклик|анкет|whatsapp|telegram|"
    r"начинается|работа\s+нач|начало\s*:?\s*\d|"
    r"смена\s+на\s+склад|"
    r"откуда\s+такси|ваш\s+адрес|свой\s+адрес|адрес\s+откуда|"
    r"^фио$|^возраст$|бронь\s+ваканс",
    re.IGNORECASE,
)
_TELEGRAM_MENTION_RE = re.compile(r"@\w+", re.I)
_MAP_WORTHY_RE = re.compile(
    r"вднх|vdnh|выставочн|сокольник|лужник|вокзал|консерватор|арена|экспо|"
    r"метро|м\.|ул\.|улица|проспект|пр-т|наб\.|шоссе|"
    r"бульвар|проезд|тц|трц|москва|област|район|"
    r"\d{1,5}\s*[а-яa-z]?(?:/\d+)?(?:\s|,|$)",
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
    r"(?:Ⓜ️\s*|м\.|метро)\s*[:\-]?\s*(?:🚇\s*)?([А-Яа-яёЁA-Za-z\- ]{2,40})",
    re.IGNORECASE,
)
_BARE_LINE_ADDRESS_RE = re.compile(
    r"(?:^|\n)\s*("
    r"(?:ул\.|улица|пер\.|переулок|наб\.|набережная|шоссе|проспект|бульвар|проезд|аллея)\s+"
    r"[^\n,]{3,70}\d+[^\n]{0,15}|"
    r"(?:маршала|генерала|академика)\s+[А-Яа-яёЁ\-]+\s+\d+[^\n]{0,20}|"
    r"[А-Яа-яёЁ][а-яёЁ\-]+\s+(?:набережная|шоссе|переулок|проспект|бульвар)\s+\d+[^\n]{0,15}|"
    r"[А-Яа-яёЁ][А-Яа-яёЁ\-\s]{2,35}\s+\d+[а-яёa-z]?(?:/?\d*)?(?:с\d+)?"
    r")",
    re.IGNORECASE | re.MULTILINE,
)
_MO_REGION_LINE_RE = re.compile(
    r"московск(?:ая|ой)\s+област[ьи][^\n]{0,120}",
    re.IGNORECASE,
)
_YANDEX_POINT_RE = re.compile(
    r"whatshere(?:%5B|\[)point(?:%5D|\])=(\d{1,3}\.\d+)[,%]2?C(\d{1,3}\.\d+)",
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
_EXPLICIT_DATE_RE = re.compile(
    r"(?:📅\s*)?(?:\*\*)?(?:дата)(?:\*\*)?\s*[:\s]*(\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?)",
    re.I,
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


def _city_display_name(slug: str, names: tuple[str, ...]) -> str:
    if slug == "mo":
        return "Московская область"
    if slug == "moscow":
        return "Москва"
    return names[0].title() if names else slug.title()


def extract_coordinates_from_text(text: str) -> tuple[float | None, float | None]:
    if not text:
        return None, None
    point = _YANDEX_POINT_RE.search(text)
    if point:
        try:
            lon, lat = float(point.group(1)), float(point.group(2))
            if 40.0 <= lat <= 60.0 and 30.0 <= lon <= 50.0:
                return lat, lon
        except (ValueError, IndexError):
            pass
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
        if re.match(r"^(?:Ⓜ️\s*|м\.|метро)\b", pl):
            station = re.sub(r"^(?:Ⓜ️\s*|м\.|метро)\s*", "", p, flags=re.I).strip()
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
        if _is_form_address_placeholder(inline):
            continue
        if re.match(
            r"^(?:[📍🗺]\s*)?(?:\*\*)?(?:м\.|метро)(?:\*\*)?\s*",
            line.strip(),
            re.I,
        ):
            station = inline or re.sub(
                r"^(?:[📍🗺]\s*)?(?:\*\*)?(?:м\.|метро)(?:\*\*)?\s*[:\-]?\s*",
                "",
                line.strip(),
                flags=re.I,
            ).strip("* ")
            station = re.sub(r"^🚇\s*", "", station).strip()
            follow = _collect_lines_after_header(lines, i, "")
            if station and (
                not follow or all(_is_noise_location_followup(p) for p in follow)
            ):
                return f"м. {station}"
        parts = _collect_lines_after_header(lines, i, inline)
        composed = _compose_location_address(parts)
        if composed and not _is_form_address_placeholder(composed):
            if re.match(
                r"^(?:[📍🗺]\s*)?(?:\*\*)?(?:м\.|метро)(?:\*\*)?",
                line.strip(),
                re.I,
            ) and "метро" not in composed.lower():
                composed = f"метро {composed}"
            return composed
    return None


_FORM_ADDRESS_INLINE_RE = re.compile(
    r"^(?:откуда\s+)?такси$|"
    r"^(?:ваш|свой)\s+адрес$|"
    r"^адрес\s+откуда|"
    r"^фио$|^возраст$|"
    r"^укажите\s+адрес",
    re.IGNORECASE,
)

_DATE_OR_PAY_FOLLOWUP_RE = re.compile(
    r"^\d{1,2}\s+(?:январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)|"
    r"^\d{1,2}[./]\d{1,2}|"
    r"оплат|ставк|₽|руб|р/ч",
    re.IGNORECASE,
)


def _is_noise_location_followup(line: str) -> bool:
    if not line:
        return True
    if _ADDR_TASK_NOISE_RE.search(line):
        return True
    if _DATE_OR_PAY_FOLLOWUP_RE.search(line):
        return True
    return False


_LABEL_ONLY_WORDS = frozenset(
    {
        "локация", "адрес", "место", "где", "точка", "район", "объект",
        "площадка", "выход", "метро", "м", "м.", "точка сбора",
    }
)


def _is_form_address_placeholder(value: str) -> bool:
    cleaned = value.strip().strip("*").lower().rstrip(":.-")
    if not cleaned:
        return False
    if _FORM_ADDRESS_INLINE_RE.search(cleaned):
        return True
    if cleaned in _LABEL_ONLY_WORDS or cleaned in ("место работы",):
        return True
    return False


def _is_label_only(value: str) -> bool:
    return _is_form_address_placeholder(value)


def _sanitize_address_fragment(value: str) -> str:
    """Убираем @username и хвостовую пунктуацию — не часть адреса."""
    if not value:
        return value
    return _TELEGRAM_MENTION_RE.sub("", value).strip(" .,—-")


def _finalize_address(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = _sanitize_address_fragment(value.strip())
    return cleaned or None


def is_plausible_map_address(text: str | None) -> bool:
    """Отсекаем мусор вроде «кандидатов старше 18»; пропускаем ВДНХ, улицы, метро."""
    if not text:
        return False
    s = text.strip()
    if len(s) < 4:
        return False
    if _GARBAGE_ADDR_RE.search(s):
        return False
    if re.search(r",\s*МО\b", s):
        return True
    if _MAP_WORTHY_RE.search(s):
        return True
    return len(s) >= 12 and not _GARBAGE_ADDR_RE.search(s)


def _extract_landmark_address(text: str) -> str | None:
    if not text:
        return None
    m = _LANDMARK_RE.search(text)
    if not m:
        return None
    name = m.group(1).strip(" .,!—")
    name_l = name.lower()
    tl = text.lower()
    metro_landmarks = ("сокольники", "лужники", "вднх", "vdnh")
    if any(x in name_l for x in metro_landmarks):
        station = "ВДНХ" if "вднх" in name_l or "vdnh" in name_l else name.title()
        if re.search(r"\bмосква\b", tl):
            return f"Москва, м. {station}"
        return f"м. {station}"
    if "москва" in name.lower():
        return name
    if re.search(r"\bмосква\b", tl) or "вднх" in name_l or "vdnh" in name_l:
        return f"Москва, {name}"
    return name


def _extract_moscow_comma_line(text: str) -> str | None:
    if not text:
        return None
    m = _MOSCOW_COMMA_LINE_RE.search(text)
    if not m:
        return None
    tail = _sanitize_address_fragment(m.group(1).strip(" .,"))
    if not tail or _ADDR_TASK_NOISE_RE.search(tail):
        return None
    if re.search(r"задача\s*:", tail, re.I):
        tail = re.split(r"задача\s*:", tail, maxsplit=1, flags=re.I)[0].strip(" .,")
    return f"Москва, {tail}" if tail else None


def _extract_metro_tc_line(text: str) -> str | None:
    if not text:
        return None
    m = _METRO_TC_LINE_RE.search(text)
    if not m:
        return None
    station = m.group(1).strip(" .,")
    mall = m.group(2).strip(" .,")
    if not station or not mall:
        return None
    return f"м. {station}, ТЦ {mall}"


def _extract_named_venue_address(text: str) -> str | None:
    if not text:
        return None
    m = _NAMED_VENUE_RE.search(text)
    if not m:
        return None
    venue = m.group(1).strip()
    venue_title = venue[0].upper() + venue[1:] if venue else venue
    tail = text[m.end(): m.end() + 120]
    street_m = _STREET_WITH_NUMBER_RE.search(tail)
    if street_m:
        street = (street_m.group(1) or street_m.group(2) or "").strip(" .,")
        if street:
            return f"{venue_title}, ул. {street}"
    city = _match_city_slug(text)
    if city and city not in ("moscow", "mo"):
        names = next(
            (names for slug, names in _load_city_catalog() if slug == city),
            (city,),
        )
        return f"{_city_display_name(city, names)}, {venue_title}"
    if re.search(r"\bмосква\b", text, re.I) or "вокзал" in venue.lower() or "консерватор" in venue.lower():
        return f"Москва, {venue_title}"
    return venue_title


def _extract_city_locative_address(text: str) -> str | None:
    if not text:
        return None
    for m in _CITY_LOCATIVE_RE.finditer(text):
        place = m.group(1).strip()
        place_l = place.lower()
        if _ADDR_TASK_NOISE_RE.search(place):
            continue
        if re.search(r"мероприят|площадк|смен|склад|работ|регистрац", place_l):
            continue
        if place_l in _LABEL_ONLY_WORDS:
            continue
        slug = _match_city_slug(place) or _match_city_slug(f"в {place}")
        if not slug or slug in ("moscow",):
            continue
        names = next(
            (names for s, names in _load_city_catalog() if s == slug),
            (place,),
        )
        display = _city_display_name(slug, names)
        if slug == "mo":
            continue
        return f"{display}, МО"
    return None


def _extract_regional_block_address(text: str) -> str | None:
    """Город капсом на отдельной строке + площадка/улица ниже (регионы)."""
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    city_display: str | None = None
    for i, line in enumerate(lines):
        if _REGIONAL_CITY_LINE_RE.fullmatch(line) and line not in ("ПРОМО", "ХЕЛПЕР"):
            city_display = line.title()
            block = "\n".join(lines[i + 1: i + 4])
            venue_m = _NAMED_VENUE_RE.search(block)
            street_m = _STREET_WITH_NUMBER_RE.search(block)
            if venue_m and street_m:
                street = (street_m.group(1) or street_m.group(2) or "").strip(" .,")
                return f"{city_display}, ул. {street}"
            if venue_m:
                return f"{city_display}, {venue_m.group(1).title()}"
            break
    return None


def _collect_lines_after_header(lines: list[str], start: int, inline: str) -> list[str]:
    parts: list[str] = []
    if inline and len(inline) >= 3 and not _is_label_only(inline):
        parts.append(inline)
    for j in range(start + 1, min(start + 5, len(lines))):
        nxt = lines[j].strip().strip("*").strip()
        if not nxt:
            continue
        if _is_noise_location_followup(nxt):
            break
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


def _extract_bare_line_address(text: str) -> str | None:
    """Строка вида «Маршала Чуйкова 6к1» или «Курьяновская набережная 6с1» без префикса «ул.»."""
    if not text:
        return None
    for match in _BARE_LINE_ADDRESS_RE.finditer(text):
        candidate = _sanitize_address_fragment(match.group(1).strip(" .,-—"))
        if len(candidate) < 8:
            continue
        if _GARBAGE_ADDR_RE.search(candidate):
            continue
        if _ADDR_TASK_NOISE_RE.search(candidate):
            continue
        if re.match(r"^[кс]\s+\d", candidate, re.I):
            continue
        if re.search(r"\d{3,4}\s*/\s*\d", candidate):
            continue
        if re.search(r"(?:оплат|ставк|руб|₽|р/ч|час)", candidate, re.I):
            continue
        if re.search(r"\bмосква\b", text, re.I):
            return f"Москва, {candidate}"
        return candidate
    return None


def _extract_mo_region_address(text: str) -> str | None:
    """«Московская область, Королёв, … Советская улица, 27»."""
    if not text:
        return None
    region = _MO_REGION_LINE_RE.search(text)
    if not region:
        return None
    fragment = region.group(0).strip(" .,")
    fragment = re.split(r"(?:оплат|ставк|☎|телефон)", fragment, maxsplit=1, flags=re.I)[0].strip(" .,")
    if len(fragment) >= 12:
        return fragment[:140]
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
            display = _city_display_name(city_slug, next(
                (names for slug, names in _load_city_catalog() if slug == city_slug),
                (city_slug,),
            ))
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
        leg = _finalize_address(legacy_address)
        return leg if leg and is_plausible_map_address(leg) else None

    def _ok(candidate: str | None) -> str | None:
        cleaned = _finalize_address(candidate)
        if cleaned and is_plausible_map_address(cleaned):
            return cleaned
        return None

    pin_line = _extract_pin_inline_address(text)
    if pin_line:
        ok_pin = _ok(pin_line)
        if ok_pin:
            return ok_pin
    block = _extract_location_block(text)
    if block:
        return _ok(block)
    explicit = _EXPLICIT_ADDR_RE.search(text)
    if explicit:
        return _ok(explicit.group(1).strip(" .,"))
    moscow_line = _extract_moscow_comma_line(text)
    if moscow_line:
        return _ok(moscow_line)
    metro_tc = _extract_metro_tc_line(text)
    if metro_tc:
        return _ok(metro_tc)
    regional = _extract_regional_block_address(text)
    if regional:
        return _ok(regional)
    named_venue = _extract_named_venue_address(text)
    if named_venue:
        return _ok(named_venue)
    city_loc = _extract_city_locative_address(text)
    if city_loc:
        return _ok(city_loc)
    city_street = _CITY_STREET_RE.search(text)
    if city_street:
        city = city_street.group(1).strip()
        street = city_street.group(2).strip(" .,")
        return _ok(f"{city}, {street}")
    venue = _VENUE_RE.search(text)
    if venue:
        venue_text = venue.group(1).strip()
        city_match = _match_city_slug(text)
        if city_match and city_match != "moscow":
            names = next(
                (names for slug, names in _load_city_catalog() if slug == city_match),
                (city_match,),
            )
            return f"{_city_display_name(city_match, names)}, {venue_text}"
        return venue_text
    landmark = _extract_landmark_address(text)
    if landmark:
        return _ok(landmark)
    mo_region = _extract_mo_region_address(text)
    if mo_region:
        return _ok(mo_region)
    bare = _extract_bare_line_address(text)
    if bare:
        return _ok(bare)
    freeform = _extract_freeform_address(text)
    if freeform:
        return _ok(freeform)
    metro = _METRO_ADDR_RE.search(text)
    if metro:
        station = metro.group(1).strip(" .,")
        if re.search(r"\bмосква\b", text, re.I):
            return _ok(f"Москва, метро {station}")
        return _ok(f"метро {station}")
    boulevard = _BOULEVARD_ONLY_RE.search(text)
    if boulevard:
        return _ok(f"Москва, {boulevard.group(1).strip()}")
    if legacy_address and legacy_address.strip():
        return _ok(legacy_address.strip())
    city_only = _match_city_slug(text)
    if city_only:
        for slug, names in _load_city_catalog():
            if slug == city_only:
                return _ok(_city_display_name(slug, names))
    return None


def resolve_map_address(
    *,
    body: str = "",
    address: str | None = None,
    address_normalized: str | None = None,
) -> str | None:
    """Лучший адрес для карты: из текста (enrichment), затем БД — только если правдоподобен."""
    enriched = enrich_vacancy_text(body or "", legacy_address=address)
    for candidate in (enriched.address_normalized, address_normalized, address):
        if candidate and is_plausible_map_address(candidate):
            return candidate.strip()
    return None


def resolve_map_fields_for_vacancy(vac: dict) -> dict:
    """Координаты и адрес для кнопки «На карте» — с учётом текста вакансии."""
    body = vac.get("text") or ""
    address = vac.get("address")
    address_normalized = vac.get("address_normalized")
    enriched = enrich_vacancy_text(body, legacy_address=address)
    lat = vac.get("location_lat")
    lon = vac.get("location_lon")
    if lat is None:
        lat = enriched.location_lat
    if lon is None:
        lon = enriched.location_lon
    best_addr = resolve_map_address(
        body=body,
        address=address,
        address_normalized=address_normalized,
    )
    return {
        "address": address,
        "address_normalized": best_addr,
        "location_lat": lat,
        "location_lon": lon,
    }


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
    explicit = _EXPLICIT_DATE_RE.search(text)
    if explicit:
        return explicit.group(1).replace("/", ".")
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
    if text_addr and is_plausible_map_address(text_addr):
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
