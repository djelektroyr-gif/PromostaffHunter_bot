"""Каталог категорий Hunter: группы для UI и коды для парсера/БД."""

from __future__ import annotations

# Порядок групп в настройках подписки
CATEGORY_GROUPS: tuple[tuple[str, str], ...] = (
    ("event", "🎪 Ивент-персонал"),
    ("event_prod", "🔧 Монтаж и площадка"),
    ("creative", "🎤 Ведущие и шоу"),
    ("field", "🛒 Полевые"),
    ("specialists", "⚡ Специалисты"),
    ("other", "📌 Прочее"),
)

GROUP_ORDER = [code for code, _ in CATEGORY_GROUPS]
GROUP_LABELS = dict(CATEGORY_GROUPS)

# group_code для существующих и новых категорий
CATEGORY_GROUP_BY_CODE: dict[str, str] = {
    "promoter": "event",
    "hostess": "event",
    "wardrobe": "event",
    "animator": "event",
    "helper": "event",
    "loader": "event",
    "waiter": "event",
    "driver": "event",
    "security": "event",
    "parking": "event",
    "supervisor": "event",
    "handyman": "event",
    "booth": "event_prod",
    "merchandiser": "field",
    "host_mc": "creative",
    "dj": "creative",
    "electrician": "specialists",
    "misc": "other",
}

# Новые категории (добавляются в БД через _ensure_core_categories)
NEW_CATEGORY_ROWS: tuple[tuple[str, str, str], ...] = (
    ("booth", "Монтаж стендов", "🏗️"),
    ("merchandiser", "Мерчендайзер", "🛒"),
    ("host_mc", "Ведущий", "🎤"),
    ("dj", "DJ", "🎧"),
    ("electrician", "Электромонтаж", "⚡"),
    ("misc", "Другая смена", "📋"),
)

CATEGORY_DISPLAY: dict[str, tuple[str, str]] = {
    "promoter": ("Промоутер", "📢"),
    "hostess": ("Хостес", "👩‍💼"),
    "wardrobe": ("Гардеробщик", "🧥"),
    "animator": ("Аниматор", "🎭"),
    "helper": ("Хелпер", "👷"),
    "loader": ("Грузчик", "📦"),
    "waiter": ("Официант", "🍽️"),
    "driver": ("Водитель", "🚐"),
    "security": ("Охранник", "🛡️"),
    "parking": ("Парковщик", "🚗"),
    "supervisor": ("Супервайзер", "👨‍💼"),
    "handyman": ("Разнорабочий / клининг", "🧹"),
    "booth": ("Монтаж стендов", "🏗️"),
    "merchandiser": ("Мерчендайзер", "🛒"),
    "host_mc": ("Ведущий", "🎤"),
    "dj": ("DJ", "🎧"),
    "electrician": ("Электромонтаж", "⚡"),
    "misc": ("Другая смена", "📋"),
}


def category_group(code: str) -> str:
    return CATEGORY_GROUP_BY_CODE.get(code, "other")


def category_name(code: str) -> str:
    row = CATEGORY_DISPLAY.get(code)
    return row[0] if row else code


def category_emoji(code: str) -> str:
    row = CATEGORY_DISPLAY.get(code)
    return row[1] if row else "📌"


def sort_key_for_category(code: str) -> tuple[int, str]:
    group = category_group(code)
    try:
        g_idx = GROUP_ORDER.index(group)
    except ValueError:
        g_idx = len(GROUP_ORDER)
    return g_idx, category_name(code).lower()


def group_legend_text() -> str:
    return "Группы: " + " · ".join(label for _, label in CATEGORY_GROUPS)


def sort_categories(categories: list[dict]) -> list[dict]:
    return sorted(categories, key=lambda c: sort_key_for_category(c["code"]))
