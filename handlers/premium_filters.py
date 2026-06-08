"""Premium filter settings UI (Premium-фильтры: geo, ставка, push, смена)."""

from __future__ import annotations

from html import escape as escape_html

from aiogram import F, Router, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db import (
    get_all_categories,
    get_subscriber_filter_prefs_effective,
    get_subscriber_filter_prefs_raw,
    get_user_categories,
    is_user_premium,
    patch_subscriber_notify_prefs,
    set_subscriber_filter_prefs,
    set_user_metro_zones,
)
from services.filter_prefs import (
    city_display_name,
    default_prefs,
    format_prefs_summary,
    load_city_catalog,
    normalize_metro_list,
    normalize_prefs,
)
from services.push_notify import (
    compute_pause_for_hours,
    compute_pause_until_morning,
    format_busy_line,
    format_category_push_label,
    format_quiet_hours_line,
    get_category_push_mode,
    is_user_busy,
    next_category_push_mode,
    parse_quiet_hours_input,
    paused_until_iso,
)

BTN_PREMIUM_FILTERS = "🎯 Фильтры Premium"
PF_PREFIX = "pf:"

router = Router(name="premium_filters")

CITIES_PER_PAGE = 8
_RADIUS_PRESETS: list[int | None] = [None, 10, 15, 20, 25]
_EARLIEST_PRESETS: list[str | None] = [None, "08:00", "09:00", "10:00", "11:00"]
_SELECTABLE_CITY_SLUGS = [
    c["slug"]
    for c in load_city_catalog()
    if c.get("slug") not in ("moscow", "mo")
]


class PremiumFilterState(StatesGroup):
    waiting_metro = State()
    waiting_rate_value = State()
    waiting_quiet_hours = State()


def _premium_required_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Подписка", callback_data="subscription_request")],
    ])


async def _answer_premium_required(message: types.Message) -> None:
    await message.answer(
        "🎯 <b>Фильтры Premium</b> — только для подписчиков Premium.\n\n"
        "География, минимальная ставка и push-фильтры доступны после оформления подписки.",
        parse_mode="HTML",
        reply_markup=_premium_required_keyboard(),
    )


def _load_prefs(user_id: int) -> dict:
    prefs = get_subscriber_filter_prefs_effective(user_id)
    return normalize_prefs(prefs or {})


def _save_prefs(user_id: int, prefs: dict) -> None:
    prefs = normalize_prefs(prefs)
    set_subscriber_filter_prefs(user_id, prefs)
    metro_list = prefs.get("geo", {}).get("metro_stations") or []
    if metro_list:
        set_user_metro_zones(user_id, ", ".join(metro_list))
    elif not prefs.get("geo", {}).get("include_all"):
        set_user_metro_zones(user_id, None)


def _geo_summary_lines(prefs: dict) -> list[str]:
    geo = prefs["geo"]
    lines: list[str] = []
    if geo.get("include_all"):
        lines.append("• Режим: <b>везде</b> (гео не режет push)")
    else:
        lines.append("• Режим: <b>выборочно</b>")
        cities = [city_display_name(s) for s in geo.get("cities") or []]
        if cities:
            lines.append(f"• Города МО: {escape_html(', '.join(cities))}")
        if geo.get("moscow") == "all":
            lines.append("• Москва: <b>любая локация</b>")
        elif geo.get("moscow") == "metro_only":
            lines.append("• Москва: <b>только метро из списка</b>")
        metros = geo.get("metro_stations") or []
        if metros:
            preview = ", ".join(m.title() for m in metros[:5])
            suffix = f" (+{len(metros) - 5})" if len(metros) > 5 else ""
            lines.append(f"• Метро: {escape_html(preview)}{suffix}")
    unk = "да" if geo.get("show_if_location_unknown", True) else "нет"
    lines.append(f"• Без адреса показывать: {unk}")
    if geo.get("radius_km"):
        lines.append(f"• Радиус от города: <b>{geo['radius_km']} км</b> (если есть координаты)")
    return lines


def _shift_summary_lines(prefs: dict) -> list[str]:
    shift = prefs.get("shift") or {}
    if not (shift.get("no_night") or shift.get("only_today_tomorrow") or shift.get("earliest_start")):
        return ["• Без ограничений по времени смены"]
    lines = []
    if shift.get("no_night"):
        lines.append("• <b>Без ночных</b> смен (старт 22:00–06:00)")
    if shift.get("only_today_tomorrow"):
        lines.append("• Только <b>сегодня и завтра</b>")
    if shift.get("earliest_start"):
        lines.append(f"• Не раньше <b>{escape_html(shift['earliest_start'])}</b>")
    return lines


def _rates_summary_lines(prefs: dict, user_id: int) -> list[str]:
    rates = prefs.get("rates") or {}
    if not rates:
        return ["• Пороги не заданы"]
    cat_names = {c["code"]: c for c in get_all_categories()}
    lines = []
    for code, cfg in rates.items():
        if not isinstance(cfg, dict):
            continue
        name = cat_names.get(code, {}).get("name") or code
        emoji = cat_names.get(code, {}).get("emoji") or ""
        if cfg.get("min_hourly"):
            lines.append(f"• {emoji} {escape_html(name)}: от {cfg['min_hourly']} ₽/ч")
        elif cfg.get("min_shift"):
            lines.append(f"• {emoji} {escape_html(name)}: от {cfg['min_shift']} ₽/смена")
    return lines or ["• Пороги не заданы"]


def _notify_summary_lines(prefs: dict) -> list[str]:
    notify = prefs.get("notify") or {}
    lines = [
        f"• Не беспокоить: <b>{escape_html(format_quiet_hours_line(prefs))}</b> (МСК)",
    ]
    busy = format_busy_line(prefs)
    if busy:
        lines.append(f"• Занят до: <b>{escape_html(busy)}</b>")
    else:
        lines.append("• Занят: <b>нет</b>")
    digest_on = notify.get("digest_after_pause", True)
    lines.append(f"• Сводка после паузы: {'да' if digest_on else 'нет'}")
    cat_modes = notify.get("category_push") or {}
    if cat_modes:
        cat_names = {c["code"]: c for c in get_all_categories()}
        for code, mode in list(cat_modes.items())[:4]:
            name = cat_names.get(code, {}).get("name") or code
            emoji = cat_names.get(code, {}).get("emoji") or ""
            lines.append(
                f"• {emoji} {escape_html(name)}: {format_category_push_label(mode)}"
            )
    else:
        lines.append("• Категории push: <b>все — обычный push</b>")
    return lines


def build_main_screen_text(prefs: dict, user_id: int) -> str:
    summary = format_prefs_summary(prefs)
    feed_line = "да" if prefs.get("apply_to_feed") else "нет (только push)"
    return (
        f"🎯 <b>Фильтры Premium</b>\n\n"
        f"<i>{escape_html(summary)}</i>\n\n"
        f"<b>📍 География</b>\n"
        + "\n".join(_geo_summary_lines(prefs))
        + f"\n\n<b>💰 Ставка</b>\n"
        + "\n".join(_rates_summary_lines(prefs, user_id))
        + f"\n\n<b>📅 Смена</b>\n"
        + "\n".join(_shift_summary_lines(prefs))
        + f"\n\n<b>🔔 Push</b>\n"
        + "\n".join(_notify_summary_lines(prefs))
        + f"\n\n<b>📋 Лента</b>\n"
        f"• Применять фильтры в ленте: <b>{feed_line}</b>"
    )


def build_main_keyboard(prefs: dict) -> InlineKeyboardMarkup:
    feed_on = prefs.get("apply_to_feed")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📍 География", callback_data=f"{PF_PREFIX}geo")],
        [InlineKeyboardButton(text="💰 Ставка", callback_data=f"{PF_PREFIX}rates")],
        [InlineKeyboardButton(text="📅 Смена", callback_data=f"{PF_PREFIX}shift")],
        [InlineKeyboardButton(text="🔔 Push и уведомления", callback_data=f"{PF_PREFIX}notify")],
        [InlineKeyboardButton(
            text=f"{'☑' if feed_on else '☐'} Фильтры и в ленте",
            callback_data=f"{PF_PREFIX}feed:{'0' if feed_on else '1'}",
        )],
        [
            InlineKeyboardButton(text="↩️ Сбросить", callback_data=f"{PF_PREFIX}reset"),
            InlineKeyboardButton(text="✅ Готово", callback_data=f"{PF_PREFIX}done"),
        ],
    ])


def build_shift_keyboard(prefs: dict) -> InlineKeyboardMarkup:
    shift = prefs.get("shift") or {}
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{'☑' if shift.get('no_night') else '☐'} Без ночных смен",
            callback_data=f"{PF_PREFIX}sn:{'0' if shift.get('no_night') else '1'}",
        )],
        [InlineKeyboardButton(
            text=f"{'☑' if shift.get('only_today_tomorrow') else '☐'} Только сегодня/завтра",
            callback_data=f"{PF_PREFIX}st:{'0' if shift.get('only_today_tomorrow') else '1'}",
        )],
        [InlineKeyboardButton(
            text=f"⏰ Не раньше: {shift.get('earliest_start') or '—'}",
            callback_data=f"{PF_PREFIX}earliest:cycle",
        )],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"{PF_PREFIX}home")],
    ])


def _next_radius(current: int | None) -> int | None:
    try:
        idx = _RADIUS_PRESETS.index(current)
    except ValueError:
        idx = 0
    return _RADIUS_PRESETS[(idx + 1) % len(_RADIUS_PRESETS)]


def _next_earliest(current: str | None) -> str | None:
    try:
        idx = _EARLIEST_PRESETS.index(current)
    except ValueError:
        idx = 0
    return _EARLIEST_PRESETS[(idx + 1) % len(_EARLIEST_PRESETS)]


def build_notify_keyboard(prefs: dict) -> InlineKeyboardMarkup:
    notify = prefs.get("notify") or {}
    digest_on = notify.get("digest_after_pause", True)
    busy = is_user_busy(prefs)
    rows = [
        [InlineKeyboardButton(
            text=f"🌙 Тихие часы: {format_quiet_hours_line(prefs)}",
            callback_data=f"{PF_PREFIX}quiet:edit",
        )],
        [
            InlineKeyboardButton(text="🔕 Занят 2 ч", callback_data=f"{PF_PREFIX}busy:2"),
            InlineKeyboardButton(text="🌙 До утра", callback_data=f"{PF_PREFIX}busy:morning"),
        ],
    ]
    if busy:
        rows.append([InlineKeyboardButton(
            text="🔔 Включить push",
            callback_data=f"{PF_PREFIX}busy:off",
        )])
    rows.extend([
        [InlineKeyboardButton(
            text=f"{'☑' if digest_on else '☐'} Сводка после паузы",
            callback_data=f"{PF_PREFIX}digest:{'0' if digest_on else '1'}",
        )],
        [InlineKeyboardButton(text="📂 Режим push по категориям", callback_data=f"{PF_PREFIX}catpush")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"{PF_PREFIX}home")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_catpush_keyboard(user_id: int, prefs: dict) -> InlineKeyboardMarkup:
    cats = get_user_categories(user_id) or get_all_categories()
    rows = []
    for cat in cats[:12]:
        mode = get_category_push_mode(prefs, cat["code"])
        label = format_category_push_label(mode)
        rows.append([InlineKeyboardButton(
            text=f"{cat.get('emoji', '')} {cat['name']}: {label}".strip(),
            callback_data=f"{PF_PREFIX}cp:{cat['code']}",
        )])
    rows.append([InlineKeyboardButton(text="◀️ Push", callback_data=f"{PF_PREFIX}notify")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _resume_push_and_digest(user_id: int, bot) -> None:
    from services.push_digest_scheduler import resume_push_notifications

    patch_subscriber_notify_prefs(user_id, {
        "paused_until": None,
        "push_block_was_active": False,
    })
    if bot:
        await resume_push_notifications(bot, user_id)


def build_geo_keyboard(prefs: dict) -> InlineKeyboardMarkup:
    geo = prefs["geo"]
    include_all = geo.get("include_all")
    moscow = geo.get("moscow")
    rows = [
        [InlineKeyboardButton(
            text=f"{'☑' if include_all else '☐'} Везде (без geo-фильтра)",
            callback_data=f"{PF_PREFIX}all:{'0' if include_all else '1'}",
        )],
        [InlineKeyboardButton(text="🏙 Города МО", callback_data=f"{PF_PREFIX}cities:0")],
        [InlineKeyboardButton(
            text=f"{'☑' if moscow == 'all' else '☐'} Москва — любая",
            callback_data=f"{PF_PREFIX}msk:all",
        )],
        [InlineKeyboardButton(
            text=f"{'☑' if moscow == 'metro_only' else '☐'} Москва — только метро",
            callback_data=f"{PF_PREFIX}msk:metro",
        )],
        [InlineKeyboardButton(text="🚇 Станции метро", callback_data=f"{PF_PREFIX}metro")],
        [InlineKeyboardButton(
            text=(
                f"{'☑' if geo.get('show_if_location_unknown', True) else '☐'} "
                "Показывать без адреса"
            ),
            callback_data=f"{PF_PREFIX}unk:{'0' if geo.get('show_if_location_unknown', True) else '1'}",
        )],
        [InlineKeyboardButton(
            text=f"📏 Радиус: {geo.get('radius_km') or '—'} км",
            callback_data=f"{PF_PREFIX}radius:cycle",
        )],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"{PF_PREFIX}home")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_cities_keyboard(prefs: dict, page: int) -> InlineKeyboardMarkup:
    selected = set(prefs["geo"].get("cities") or [])
    start = page * CITIES_PER_PAGE
    chunk = _SELECTABLE_CITY_SLUGS[start : start + CITIES_PER_PAGE]
    rows: list[list[InlineKeyboardButton]] = []
    for slug in chunk:
        mark = "☑" if slug in selected else "☐"
        rows.append([InlineKeyboardButton(
            text=f"{mark} {city_display_name(slug)}",
            callback_data=f"{PF_PREFIX}ct:{slug}:{page}",
        )])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"{PF_PREFIX}cities:{page - 1}"))
    if start + CITIES_PER_PAGE < len(_SELECTABLE_CITY_SLUGS):
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"{PF_PREFIX}cities:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="◀️ География", callback_data=f"{PF_PREFIX}geo")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_rates_keyboard(user_id: int) -> InlineKeyboardMarkup:
    cats = get_user_categories(user_id) or get_all_categories()
    rows = []
    for cat in cats[:12]:
        rows.append([InlineKeyboardButton(
            text=f"{cat.get('emoji', '')} {cat['name']}".strip(),
            callback_data=f"{PF_PREFIX}rate:{cat['code']}",
        )])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"{PF_PREFIX}home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_rate_edit_keyboard(code: str, prefs: dict) -> InlineKeyboardMarkup:
    cfg = (prefs.get("rates") or {}).get(code) or {}
    hourly = cfg.get("min_hourly")
    shift = cfg.get("min_shift")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"₽/ч{' ✓ ' + str(hourly) if hourly else ''}",
            callback_data=f"{PF_PREFIX}rh:{code}",
        )],
        [InlineKeyboardButton(
            text=f"₽/смена{' ✓ ' + str(shift) if shift else ''}",
            callback_data=f"{PF_PREFIX}rs:{code}",
        )],
        [InlineKeyboardButton(text="🗑 Сбросить категорию", callback_data=f"{PF_PREFIX}rc:{code}")],
        [InlineKeyboardButton(text="◀️ Ставки", callback_data=f"{PF_PREFIX}rates")],
    ])


async def _edit_or_send(
    target: types.Message | types.CallbackQuery,
    text: str,
    markup: InlineKeyboardMarkup,
) -> None:
    msg = target.message if isinstance(target, types.CallbackQuery) else target
    try:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=markup)
    except TelegramBadRequest:
        await msg.answer(text, parse_mode="HTML", reply_markup=markup)


async def show_premium_filters_screen(
    target: types.Message | types.CallbackQuery,
    user_id: int,
) -> None:
    if not is_user_premium(user_id):
        if isinstance(target, types.CallbackQuery):
            await target.answer("Нужен Premium", show_alert=True)
        else:
            await _answer_premium_required(target)
        return
    prefs = _load_prefs(user_id)
    text = build_main_screen_text(prefs, user_id)
    markup = build_main_keyboard(prefs)
    if isinstance(target, types.CallbackQuery):
        await _edit_or_send(target, text, markup)
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=markup)


@router.message(F.text == BTN_PREMIUM_FILTERS)
@router.message(F.text == "📍 Станции метро")
@router.message(F.text == "📍 Мои районы")
async def premium_filters_menu_message(message: types.Message, state: FSMContext) -> None:
    await state.clear()
    await show_premium_filters_screen(message, message.from_user.id)


@router.callback_query(F.data.startswith(PF_PREFIX))
async def premium_filters_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id
    data = callback.data or ""
    if not is_user_premium(user_id):
        await callback.answer("Нужен Premium", show_alert=True)
        return

    prefs = _load_prefs(user_id)
    action = data[len(PF_PREFIX):]

    if action == "home":
        await callback.answer()
        await show_premium_filters_screen(callback, user_id)
        return

    if action == "done":
        await callback.answer("Сохранено")
        try:
            await callback.message.delete()
        except TelegramBadRequest:
            pass
        return

    if action == "reset":
        prefs = default_prefs()
        _save_prefs(user_id, prefs)
        await callback.answer("Сброшено")
        await show_premium_filters_screen(callback, user_id)
        return

    if action == "geo":
        await callback.answer()
        await _edit_or_send(
            callback,
            "📍 <b>География</b>\n\nВыберите зоны для push (достаточно одного совпадения):",
            build_geo_keyboard(prefs),
        )
        return

    if action.startswith("cities:"):
        page = int(action.split(":")[1])
        await callback.answer()
        await _edit_or_send(
            callback,
            f"🏙 <b>Города МО</b> (стр. {page + 1})\n\n"
            "Отметьте города из списка. Можно вместе с «Москва — любая».",
            build_cities_keyboard(prefs, page),
        )
        return

    if action.startswith("ct:"):
        _, slug, page_s = action.split(":", 2)
        page = int(page_s)
        cities = list(prefs["geo"].get("cities") or [])
        if slug in cities:
            cities.remove(slug)
        else:
            cities.append(slug)
        prefs["geo"]["cities"] = cities
        prefs["geo"]["include_all"] = False
        _save_prefs(user_id, prefs)
        await callback.answer(city_display_name(slug))
        await _edit_or_send(
            callback,
            f"🏙 <b>Города МО</b> (стр. {page + 1})",
            build_cities_keyboard(_load_prefs(user_id), page),
        )
        return

    if action.startswith("all:"):
        val = action.split(":")[1] == "1"
        prefs["geo"]["include_all"] = val
        if val:
            prefs["geo"]["moscow"] = None
            prefs["geo"]["cities"] = []
            prefs["geo"]["metro_stations"] = []
        _save_prefs(user_id, prefs)
        await callback.answer("Везде" if val else "Выборочно")
        await _edit_or_send(
            callback,
            "📍 <b>География</b>",
            build_geo_keyboard(_load_prefs(user_id)),
        )
        return

    if action == "msk:all":
        geo = prefs["geo"]
        if geo.get("moscow") == "all":
            geo["moscow"] = None
        else:
            geo["moscow"] = "all"
            geo["include_all"] = False
        _save_prefs(user_id, prefs)
        await callback.answer()
        await _edit_or_send(callback, "📍 <b>География</b>", build_geo_keyboard(_load_prefs(user_id)))
        return

    if action == "msk:metro":
        geo = prefs["geo"]
        if geo.get("moscow") == "metro_only":
            geo["moscow"] = None
        else:
            geo["moscow"] = "metro_only"
            geo["include_all"] = False
        _save_prefs(user_id, prefs)
        await callback.answer()
        await _edit_or_send(callback, "📍 <b>География</b>", build_geo_keyboard(_load_prefs(user_id)))
        return

    if action.startswith("unk:"):
        val = action.split(":")[1] == "1"
        prefs["geo"]["show_if_location_unknown"] = val
        _save_prefs(user_id, prefs)
        await callback.answer()
        await _edit_or_send(callback, "📍 <b>География</b>", build_geo_keyboard(_load_prefs(user_id)))
        return

    if action == "metro":
        await callback.answer()
        await state.set_state(PremiumFilterState.waiting_metro)
        await state.update_data(pf_return="geo")
        current = prefs["geo"].get("metro_stations") or []
        cur_line = ", ".join(s.title() for s in current) if current else "не заданы"
        await callback.message.answer(
            f"🚇 <b>Станции метро</b>\n\n"
            f"Сейчас: {escape_html(cur_line)}\n\n"
            "Введите станции через запятую, например:\n"
            "<code>Таганская, Сокол</code>\n\n"
            "Чтобы сбросить — отправьте <code>0</code> или <code>-</code>.",
            parse_mode="HTML",
        )
        return

    if action.startswith("feed:"):
        prefs["apply_to_feed"] = action.split(":")[1] == "1"
        _save_prefs(user_id, prefs)
        await callback.answer("Обновлено")
        await show_premium_filters_screen(callback, user_id)
        return

    if action == "shift":
        await callback.answer()
        await _edit_or_send(
            callback,
            "📅 <b>Фильтр смены</b>\n\n"
            "Применяется к push (и к ленте, если включено «фильтры в ленте»).\n"
            "Если время смены не распознано — вакансия не отсекается.",
            build_shift_keyboard(_load_prefs(user_id)),
        )
        return

    if action.startswith("sn:"):
        prefs["shift"]["no_night"] = action.split(":")[1] == "1"
        _save_prefs(user_id, prefs)
        await callback.answer()
        await _edit_or_send(callback, "📅 <b>Фильтр смены</b>", build_shift_keyboard(_load_prefs(user_id)))
        return

    if action.startswith("st:"):
        prefs["shift"]["only_today_tomorrow"] = action.split(":")[1] == "1"
        _save_prefs(user_id, prefs)
        await callback.answer()
        await _edit_or_send(callback, "📅 <b>Фильтр смены</b>", build_shift_keyboard(_load_prefs(user_id)))
        return

    if action == "earliest:cycle":
        cur = prefs.get("shift", {}).get("earliest_start")
        prefs.setdefault("shift", {})["earliest_start"] = _next_earliest(cur)
        _save_prefs(user_id, prefs)
        await callback.answer()
        await _edit_or_send(callback, "📅 <b>Фильтр смены</b>", build_shift_keyboard(_load_prefs(user_id)))
        return

    if action == "radius:cycle":
        cur = prefs.get("geo", {}).get("radius_km")
        prefs.setdefault("geo", {})["radius_km"] = _next_radius(cur)
        _save_prefs(user_id, prefs)
        await callback.answer()
        await _edit_or_send(callback, "📍 <b>География</b>", build_geo_keyboard(_load_prefs(user_id)))
        return

    if action == "notify":
        await callback.answer()
        await _edit_or_send(
            callback,
            "🔔 <b>Push и уведомления</b>\n\n"
            "Тихие часы и «занят» не блокируют ленту — только моментальный push.",
            build_notify_keyboard(_load_prefs(user_id)),
        )
        return

    if action == "quiet:edit":
        await callback.answer()
        await state.set_state(PremiumFilterState.waiting_quiet_hours)
        await callback.message.answer(
            "🌙 <b>Тихие часы</b> (МСК)\n\n"
            f"Сейчас: <b>{escape_html(format_quiet_hours_line(prefs))}</b>\n\n"
            "Введите интервал, например:\n"
            "<code>23:00-08:00</code>",
            parse_mode="HTML",
        )
        return

    if action.startswith("busy:"):
        sub = action.split(":", 1)[1]
        if sub == "off":
            await _resume_push_and_digest(user_id, callback.bot)
            await callback.answer("Push включён")
        elif sub == "morning":
            until = compute_pause_until_morning(prefs)
            patch_subscriber_notify_prefs(user_id, {
                "paused_until": paused_until_iso(until),
                "push_block_was_active": True,
            })
            await callback.answer("До утра")
        elif sub == "2":
            until = compute_pause_for_hours(2)
            patch_subscriber_notify_prefs(user_id, {
                "paused_until": paused_until_iso(until),
                "push_block_was_active": True,
            })
            await callback.answer("Занят 2 часа")
        await _edit_or_send(
            callback,
            "🔔 <b>Push и уведомления</b>",
            build_notify_keyboard(_load_prefs(user_id)),
        )
        return

    if action.startswith("digest:"):
        val = action.split(":")[1] == "1"
        patch_subscriber_notify_prefs(user_id, {"digest_after_pause": val})
        await callback.answer("Сохранено")
        await _edit_or_send(
            callback,
            "🔔 <b>Push и уведомления</b>",
            build_notify_keyboard(_load_prefs(user_id)),
        )
        return

    if action == "catpush":
        await callback.answer()
        await _edit_or_send(
            callback,
            "📂 <b>Режим push по категориям</b>\n\n"
            "Нажмите категорию, чтобы переключить: 🔥 приоритет → 🔔 push → 📂 только лента.",
            build_catpush_keyboard(user_id, _load_prefs(user_id)),
        )
        return

    if action.startswith("cp:"):
        code = action.split(":", 1)[1]
        prefs = _load_prefs(user_id)
        modes = dict(prefs.get("notify", {}).get("category_push") or {})
        current = modes.get(code)
        new_mode = next_category_push_mode(current)
        if new_mode == "normal" and code in modes:
            modes.pop(code)
        else:
            modes[code] = new_mode
        patch_subscriber_notify_prefs(user_id, {"category_push": modes})
        await callback.answer(format_category_push_label(new_mode))
        await _edit_or_send(
            callback,
            "📂 <b>Режим push по категориям</b>",
            build_catpush_keyboard(user_id, _load_prefs(user_id)),
        )
        return

    if action == "rates":
        await callback.answer()
        await _edit_or_send(
            callback,
            "💰 <b>Минимальная ставка</b>\n\n"
            "Выберите категорию. Пустой порог — ставка не фильтруется.",
            build_rates_keyboard(user_id),
        )
        return

    if action.startswith("rate:"):
        code = action.split(":", 1)[1]
        await callback.answer()
        cat = next((c for c in get_all_categories() if c["code"] == code), None)
        title = f"{cat.get('emoji', '')} {cat['name']}" if cat else code
        await _edit_or_send(
            callback,
            f"💰 <b>{escape_html(title.strip())}</b>\n\nЗадайте порог ₽/ч или ₽/смена:",
            build_rate_edit_keyboard(code, prefs),
        )
        return

    if action.startswith(("rh:", "rs:")):
        kind, code = action.split(":", 1)
        field = "min_hourly" if kind == "rh" else "min_shift"
        await callback.answer()
        await state.set_state(PremiumFilterState.waiting_rate_value)
        await state.update_data(pf_rate_code=code, pf_rate_field=field)
        label = "₽/ч" if field == "min_hourly" else "₽/смена"
        await callback.message.answer(
            f"Введите минимум {label} (число) или <code>-</code> чтобы убрать порог.",
            parse_mode="HTML",
        )
        return

    if action.startswith("rc:"):
        code = action.split(":", 1)[1]
        rates = prefs.get("rates") or {}
        rates.pop(code, None)
        prefs["rates"] = rates
        _save_prefs(user_id, prefs)
        await callback.answer("Сброшено")
        await show_premium_filters_screen(callback, user_id)
        return


@router.message(PremiumFilterState.waiting_metro)
async def premium_filters_metro_input(message: types.Message, state: FSMContext) -> None:
    user_id = message.from_user.id
    text = (message.text or "").strip()
    prefs = _load_prefs(user_id)
    if text in ("0", "-", "—"):
        prefs["geo"]["metro_stations"] = []
        if prefs["geo"].get("moscow") == "metro_only":
            prefs["geo"]["moscow"] = None
    else:
        stations = normalize_metro_list(text)
        if not stations:
            await message.answer("❌ Укажите станции через запятую или <code>0</code> для сброса.", parse_mode="HTML")
            return
        prefs["geo"]["metro_stations"] = stations
        prefs["geo"]["include_all"] = False
        if not prefs["geo"].get("moscow"):
            prefs["geo"]["moscow"] = "metro_only"
    _save_prefs(user_id, prefs)
    await state.clear()
    preview = ", ".join(s.title() for s in prefs["geo"]["metro_stations"]) or "сброшено"
    await message.answer(f"✅ Метро сохранено: {escape_html(preview)}", parse_mode="HTML")
    await show_premium_filters_screen(message, user_id)


@router.message(PremiumFilterState.waiting_rate_value)
async def premium_filters_rate_input(message: types.Message, state: FSMContext) -> None:
    user_id = message.from_user.id
    data = await state.get_data()
    code = data.get("pf_rate_code")
    field = data.get("pf_rate_field")
    if not code or not field:
        await state.clear()
        return
    text = (message.text or "").strip().replace(" ", "")
    prefs = _load_prefs(user_id)
    rates = dict(prefs.get("rates") or {})
    cfg = dict(rates.get(code) or {})
    if text in ("-", "—", "0"):
        cfg[field] = None
        if not cfg.get("min_hourly") and not cfg.get("min_shift"):
            rates.pop(code, None)
        else:
            rates[code] = cfg
    else:
        try:
            value = int(text)
        except ValueError:
            await message.answer("❌ Введите целое число или <code>-</code>.", parse_mode="HTML")
            return
        if value <= 0:
            await message.answer("❌ Сумма должна быть больше нуля.")
            return
        cfg[field] = value
        rates[code] = cfg
    prefs["rates"] = rates
    _save_prefs(user_id, prefs)
    await state.clear()
    await message.answer("✅ Порог сохранён.")
    await show_premium_filters_screen(message, user_id)


@router.message(PremiumFilterState.waiting_quiet_hours)
async def premium_filters_quiet_input(message: types.Message, state: FSMContext) -> None:
    user_id = message.from_user.id
    parsed = parse_quiet_hours_input(message.text or "")
    if not parsed:
        await message.answer(
            "❌ Формат: <code>23:00-08:00</code> (время по МСК).",
            parse_mode="HTML",
        )
        return
    start, end = parsed
    patch_subscriber_notify_prefs(user_id, {
        "quiet_start": start,
        "quiet_end": end,
        "quiet_configured": True,
    })
    await state.clear()
    await message.answer(f"✅ Тихие часы: {start} – {end} (МСК)")
    await show_premium_filters_screen(message, user_id)


def get_saved_filters_hint(user_id: int) -> str | None:
    """Строка для экрана подписки, если prefs сохранены без Premium."""
    if is_user_premium(user_id):
        return None
    if not get_subscriber_filter_prefs_raw(user_id):
        return None
    return "💾 Ваши фильтры сохранены — заработают после продления Premium."
