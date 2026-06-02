import asyncio
import re
import logging
import hashlib
import sqlite3
from datetime import datetime
from urllib.parse import quote
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from db import *
from parser import (
    run_parser, get_last_debug_report, detect_category, extract_contact_from_text,
    start_realtime_listener, stop_realtime_listener, get_new_messages, extract_address_from_text
)
from config import BOT_TOKEN, YOUR_USER_ID

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Flood control для real-time (необязательно, но оставим паузу между отправками)
SEND_DELAY = 1  # секунда между отправками одному пользователю

# Пагинация
user_pages = {}

# Состояния FSM
class RegistrationState(StatesGroup):
    waiting_for_name = State()
    waiting_for_birthdate = State()
    waiting_for_phone = State()
    waiting_for_photo = State()

class ComplaintState(StatesGroup):
    waiting_for_reason = State()
    waiting_for_text = State()

class SupportState(StatesGroup):
    waiting_for_question = State()

class AddChatState(StatesGroup):
    waiting_for_link = State()

class PostVacancyState(StatesGroup):
    waiting_for_category = State()
    waiting_for_text = State()
    waiting_for_photo = State()

class RespondWithPhotoState(StatesGroup):
    waiting_for_photo = State()

class ResponseDraftState(StatesGroup):
    waiting_for_comment = State()

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========

def escape_markdown(text: str) -> str:
    if not text:
        return ""
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

def generate_vacancy_id(chat_id: str, message_id: str, dedupe_key: str = None) -> str:
    unique_str = dedupe_key or f"{chat_id}_{message_id}"
    return hashlib.md5(unique_str.encode()).hexdigest()[:16]

def get_category_emoji(category_code: str) -> str:
    emojis = {
        "promoter": "📢", "hostess": "👩‍💼", "wardrobe": "🧥", "animator": "🎭",
        "helper": "👷", "loader": "📦", "waiter": "🍽️", "driver": "🚐",
        "security": "🛡️", "parking": "🚗", "supervisor": "👨‍💼"
    }
    return emojis.get(category_code, "📌")

def calculate_age(birth_date_str: str) -> int:
    try:
        birth_date = datetime.strptime(birth_date_str, "%d.%m.%Y")
        today = datetime.now()
        age = today.year - birth_date.year
        if (today.month, today.day) < (birth_date.month, birth_date.day):
            age -= 1
        return age
    except ValueError:
        return None

def format_user_card(card: dict, idx: int = None) -> str:
    prefix = f"{idx}. " if idx is not None else ""
    name = card.get("full_name") or "—"
    username = f"@{card['username']}" if card.get("username") else "нет"
    cats = ", ".join(card.get("categories") or []) or "не выбраны"
    status = "активен" if card.get("is_active") else "неактивен"
    return (
        f"{prefix}👤 *{escape_markdown(name)}*\n"
        f"ID: `{card['user_id']}`\n"
        f"Username: {escape_markdown(username)}\n"
        f"Телефон: {escape_markdown(card.get('phone') or '—')}\n"
        f"Возраст: {escape_markdown(str(card.get('age') or '—'))}\n"
        f"Статус: {escape_markdown(status)}\n"
        f"Категории: {escape_markdown(cats)}"
    )

def format_publication_time(raw_dt: str) -> str:
    if not raw_dt:
        return "сейчас"
    try:
        dt = datetime.strptime(raw_dt[:19], "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return raw_dt

def get_freshness_label(raw_dt: str) -> str:
    if not raw_dt:
        return "🟢 Актуальна"
    try:
        published = datetime.strptime(raw_dt[:19], "%Y-%m-%d %H:%M:%S")
        delta_minutes = (datetime.now() - published).total_seconds() / 60
        if delta_minutes <= 30:
            return "🟢 Актуальна: только что"
        if delta_minutes <= 180:
            return "🟢 Актуальна: сегодня"
        return "🟡 Актуальна: ранее сегодня"
    except Exception:
        return "🟢 Актуальна"

def normalize_chat_link(raw: str) -> str:
    if not raw:
        return None
    link = raw.strip()
    if link.startswith("@"):
        return f"https://t.me/{link[1:]}"
    if link.startswith("https://t.me/"):
        return link.rstrip("/")
    if link.startswith("http://t.me/"):
        return "https://" + link[len("http://") :].rstrip("/")
    if link.startswith("t.me/"):
        return f"https://{link}".rstrip("/")
    if re.fullmatch(r"[a-zA-Z0-9_]{5,32}", link):
        return f"https://t.me/{link}"
    return None

def extract_required_fields_from_vacancy(vacancy_text: str) -> list:
    if not vacancy_text:
        return []
    txt = vacancy_text.lower()
    required = []
    checks = [
        ("Возраст", ["возраст", "лет"]),
        ("Гражданство", ["гражданств", "рф", "снг"]),
        ("Опыт", ["опыт", "стаж"]),
        ("Размер одежды", ["размер одежды", "размер"]),
        ("Паспорт", ["паспорт"]),
        ("Медкнижка", ["медкниж", "мед книж"]),
        ("Самозанятость", ["самозанят", "самозанятость"]),
    ]
    for label, keys in checks:
        if any(k in txt for k in keys):
            required.append(label)
    return required

def build_candidate_profile_text(profile: dict, extra_comment: str = None) -> str:
    lines = [
        "Здравствуйте! Откликаюсь на вашу вакансию.",
        "",
        "Анкета кандидата:",
        f"• ФИО: {profile.get('full_name') or '—'}",
        f"• Возраст: {profile.get('age') or '—'}",
        f"• Телефон: {profile.get('phone') or '—'}",
        f"• Telegram: @{profile.get('username') if profile.get('username') else 'нет'}",
    ]
    if extra_comment:
        lines.extend(["", "Дополнительно:", extra_comment.strip()])
    return "\n".join(lines).strip()

def build_contact_link(contact: str, text: str) -> str:
    if not contact:
        return None
    contact = contact.strip()
    if contact.startswith("@"):
        username = contact[1:]
        return f"https://t.me/{username}?text={quote(text)}"
    digits = re.sub(r"\D", "", contact)
    if digits:
        if len(digits) == 11 and digits.startswith("8"):
            digits = "7" + digits[1:]
        if len(digits) == 11 and digits.startswith("7"):
            return f"tel:+{digits}"
    return None

# ========== РАССЫЛКА ВАКАНСИЙ ПОДПИСЧИКАМ (без глобальных счётчиков) ==========

async def send_vacancy_to_subscribers(order: dict):
    category_code = order.get('category', detect_category(order['message_text']))
    dedupe_key = order.get("dedupe_key")
    vacancy_id = generate_vacancy_id(order.get('chat_id', ''), order.get('message_id', ''), dedupe_key=dedupe_key)
    subscribers = get_subscribers_by_category(category_code)
    if not subscribers:
        logger.info(f"Нет подписчиков на категорию {category_code}")
        return

    published_raw = order.get("published_at")
    published_at = format_publication_time(published_raw)
    freshness = get_freshness_label(published_raw)
    text = (
        f"{get_category_emoji(category_code)} *Вакансия:*\n\n"
        f"{escape_markdown(freshness)}\n"
        f"🕒 Опубликовано: {escape_markdown(published_at)}\n\n"
        f"{escape_markdown(order['message_text'][:500])}\n\n"
        f"📢 Источник: {escape_markdown(order['chat_title'])}"
    )

    buttons = [[InlineKeyboardButton(text="✋ Откликнуться", callback_data=f"respond_{vacancy_id}")]]
    if order.get('address'):
        address = order['address']
        maps_url = f"https://yandex.ru/maps/?text={address.replace(' ', '%20')}"
        buttons.append([InlineKeyboardButton(text="🗺️ Показать на карте", url=maps_url)])
    buttons.append([InlineKeyboardButton(text="⚠️ Пожаловаться", callback_data=f"complain_{vacancy_id}")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    save_vacancy(
        vacancy_id=vacancy_id,
        source_chat=order.get('chat_id', ''),
        source_chat_title=order['chat_title'],
        category_code=category_code,
        message_text=order['message_text'][:1000],
        message_link=order['message_link'],
        author_contact=order.get('author_contact'),
        address=order.get('address'),
        dedupe_key=dedupe_key,
        published_at=order.get("published_at")
    )

    sent_count = 0
    for subscriber in subscribers:
        if has_user_received_vacancy(subscriber['user_id'], vacancy_id):
            continue
        try:
            await bot.send_message(
                subscriber['user_id'],
                text,
                parse_mode="MarkdownV2",
                disable_web_page_preview=True,
                reply_markup=keyboard
            )
            mark_vacancy_sent_to_user(vacancy_id, subscriber['user_id'])
            sent_count += 1
            await asyncio.sleep(SEND_DELAY)  # небольшая пауза, чтобы не флудить
        except Exception as e:
            if "bot was blocked by the user" in str(e):
                logger.info(f"Пользователь {subscriber['user_id']} заблокировал бота")
            else:
                logger.error(f"Ошибка отправки {subscriber['user_id']}: {e}")

    logger.info(f"Вакансия {vacancy_id} (категория {category_code}) отправлена {sent_count} подписчикам")
    mark_vacancy_sent(vacancy_id)

# ========== УВЕДОМЛЕНИЕ О ЗАКРЫТИИ ВАКАНСИЙ ==========

async def notify_closed_vacancies(closed_data: list):
    for vacancy_id, user_ids in closed_data:
        for uid in user_ids:
            try:
                await bot.send_message(uid, f"🔒 *Вакансия, на которую вы откликались или получали, больше не актуальна (закрыта).*\nID вакансии: `{vacancy_id}`", parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Не удалось уведомить пользователя {uid}: {e}")

# ========== КЛАВИАТУРЫ ==========

def get_categories_keyboard():
    categories = get_all_categories()
    buttons, row = [], []
    for i, cat in enumerate(categories):
        row.append(InlineKeyboardButton(text=f"{cat['emoji']} {cat['name']}", callback_data=f"cat_{cat['code']}"))
        if len(row) == 2 or i == len(categories) - 1:
            buttons.append(row)
            row = []
    buttons.append([InlineKeyboardButton(text="✅ Завершить выбор", callback_data="finish_categories")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_main_keyboard(user_id: int):
    categories = get_user_categories(user_id)
    if categories:
        categories_text = ", ".join([f"{c['emoji']}{c['name']}" for c in categories])
        status_text = f"📌 Ваши категории: {categories_text}"
    else:
        status_text = "⚠️ Вы ещё не выбрали категории вакансий"
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔍 Посмотреть новые вакансии")],
            [KeyboardButton(text="📋 Мои категории"), KeyboardButton(text="✏️ Изменить категории")],
            [KeyboardButton(text="📞 Мои контакты"), KeyboardButton(text="❌ Отписаться")],
            [KeyboardButton(text="❓ Поддержка")]
        ],
        resize_keyboard=True
    )
    return keyboard, status_text

def get_admin_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="🔍 Ручная проверка")],
            [KeyboardButton(text="📋 Список откликов"), KeyboardButton(text="📝 Отчёт парсера")],
            [KeyboardButton(text="👥 Список подписчиков"), KeyboardButton(text="📢 Рассылка")],
            [KeyboardButton(text="🗂️ Карточки пользователей"), KeyboardButton(text="🧭 Маппинг категорий")],
            [KeyboardButton(text="⚠️ Жалобы"), KeyboardButton(text="❓ Поддержка (админ)")],
            [KeyboardButton(text="➕ Добавить чат"), KeyboardButton(text="📤 Отправить вакансию")],
            [KeyboardButton(text="❌ Закрыть меню")]
        ],
        resize_keyboard=True
    )

# ========== КОМАНДЫ И ОБРАБОТЧИКИ ==========

@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name
    last_name = message.from_user.last_name

    if user_id == YOUR_USER_ID:
        await message.answer(
            f"👋 Здравствуйте, Администратор {first_name}!\n\n"
            f"📊 *Бот работает в штатном режиме.*\n\n"
            f"Используйте кнопки для управления:",
            parse_mode="Markdown",
            reply_markup=get_admin_keyboard()
        )
        return

    add_subscriber(user_id, username, first_name, last_name)
    profile = get_subscriber_profile(user_id)
    if profile and profile.get("full_name") and profile.get("phone"):
        categories = get_user_categories(user_id)
        if categories:
            keyboard, status_text = get_main_keyboard(user_id)
            await message.answer(
                f"👋 С возвращением, {first_name}!\n\n{status_text}\n\n"
                f"Используйте кнопки для управления:",
                reply_markup=keyboard
            )
            return
        else:
            await message.answer(
                f"👋 С возвращением, {first_name}!\n\n"
                f"Ваш профиль уже заполнен, но вы ещё не выбрали категории вакансий.\n\n"
                f"📋 *Выберите категории вакансий:*",
                parse_mode="Markdown",
                reply_markup=get_categories_keyboard()
            )
            return

    await message.answer(
        "👋 *Добро пожаловать в бот поиска работы!*\n\n"
        "Я помогу вам найти подходящие вакансии.\n\n"
        "📝 *Давайте заполним ваш профиль*\n\n"
        "Как вас зовут? (ФИО полностью)\n\nПример: *Иван Петров*",
        parse_mode="Markdown"
    )
    await state.set_state(RegistrationState.waiting_for_name)


@dp.message(RegistrationState.waiting_for_name)
async def process_name(message: types.Message, state: FSMContext):
    full_name = message.text.strip()
    if len(full_name.split()) < 2:
        await message.answer("❌ Пожалуйста, введите полное имя и фамилию (минимум 2 слова).")
        return
    if not re.match(r'^[a-zA-Zа-яА-ЯёЁ\s\-\.]+$', full_name):
        await message.answer("❌ Имя может содержать только буквы, пробелы, дефисы и точки.")
        return
    await state.update_data(full_name=full_name)
    await message.answer(
        "🎂 *Дата рождения*\n\n"
        "Введите вашу дату рождения в формате: **ДД.ММ.ГГГГ**\n\n"
        "Пример: `25.12.1990`\n\n"
        "Я автоматически рассчитаю ваш возраст.",
        parse_mode="Markdown"
    )
    await state.set_state(RegistrationState.waiting_for_birthdate)


@dp.message(RegistrationState.waiting_for_birthdate)
async def process_birthdate(message: types.Message, state: FSMContext):
    birth_date_str = message.text.strip()
    if not re.match(r'^\d{2}\.\d{2}\.\d{4}$', birth_date_str):
        await message.answer(
            "❌ Неверный формат!\n\n"
            "Пожалуйста, введите дату в формате: **ДД.ММ.ГГГГ**\n\n"
            "Пример: `25.12.1990`",
            parse_mode="Markdown"
        )
        return
    age = calculate_age(birth_date_str)
    if age is None:
        await message.answer(
            "❌ Неверная дата! Проверьте, что:\n"
            "- День от 1 до 31\n"
            "- Месяц от 1 до 12\n"
            "- Год реальный\n\n"
            "Пример: `25.12.1990`",
            parse_mode="Markdown"
        )
        return
    if age < 16 or age > 100:
        await message.answer(f"❌ Возраст должен быть от 16 до 100 лет. Ваш возраст: {age}.", parse_mode="Markdown")
        return
    await state.update_data(birth_date=birth_date_str, age=age)
    
    phone_keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить мой номер телефона", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await message.answer(
        f"✅ Возраст: {age} лет\n\n"
        "📞 *Контактный телефон*\n\n"
        "Нажмите на кнопку ниже, чтобы отправить ваш номер телефона.\n"
        "Он будет передан работодателю при отклике на вакансию.",
        parse_mode="Markdown",
        reply_markup=phone_keyboard
    )
    await state.set_state(RegistrationState.waiting_for_phone)


@dp.message(RegistrationState.waiting_for_phone)
async def process_phone(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if message.contact:
        phone = message.contact.phone_number
    else:
        phone = message.text.strip()
        digits_only = re.sub(r'\D', '', phone)
        if len(digits_only) < 10 or len(digits_only) > 15:
            await message.answer(
                "❌ Пожалуйста, введите корректный номер телефона.\n\n"
                "Примеры: +7 999 123-45-67 или 89991234567\n\n"
                "Или нажмите кнопку «Отправить мой номер телефона»",
                reply_markup=ReplyKeyboardMarkup(
                    keyboard=[[KeyboardButton(text="📱 Отправить мой номер телефона", request_contact=True)]],
                    resize_keyboard=True,
                    one_time_keyboard=True
                )
            )
            return

    data = await state.get_data()
    await state.update_data(phone=phone)   # <-- СОХРАНЯЕМ ТЕЛЕФОН В СОСТОЯНИЕ
    update_subscriber_profile(user_id, data['full_name'], data['age'], phone)
    
    # Анкета
    questionnaire = f"""📝 *АНКЕТА КАНДИДАТА*

👤 *ФИО:* {data['full_name']}
🎂 *Дата рождения:* {data['birth_date']}
📊 *Возраст:* {data['age']} лет
📞 *Телефон:* {phone}
🆔 *Telegram:* @{message.from_user.username if message.from_user.username else 'нет'}
"""
    update_candidate_questionnaire(user_id, questionnaire)
    
    # Запрос фото (необязательно)
    await message.answer(
        "📸 *Фото для отклика*\n\n"
        "При отклике на вакансию вы можете приложить своё фото.\n"
        "Это повышает шансы на положительный ответ.\n\n"
        "Отправьте фото сейчас или нажмите «Пропустить»",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="⏩ Пропустить")]],
            resize_keyboard=True
        )
    )
    await state.set_state(RegistrationState.waiting_for_photo)


@dp.message(RegistrationState.waiting_for_photo)
async def process_photo(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if message.text == "⏩ Пропустить":
        photo_file_id = None
    elif message.photo:
        photo_file_id = message.photo[-1].file_id
        update_subscriber_photo(user_id, photo_file_id)
    else:
        await message.answer("Пожалуйста, отправьте фото или нажмите «Пропустить»")
        return

    # Профиль уже полностью сохранён в БД, просто завершаем регистрацию
    data = await state.get_data()
    await message.answer(
        "✅ *Профиль успешно создан!*\n\n"
        f"📝 ФИО: {data['full_name']}\n"
        f"🎂 Возраст: {data['age']} лет\n"
        f"📞 Телефон: {data['phone']}\n\n"
        "Теперь выберите категории вакансий, которые вас интересуют.\n\n"
        "Вы можете выбрать несколько:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove()
    )
    await message.answer(
        "📋 *Выберите категории вакансий:*",
        parse_mode="Markdown",
        reply_markup=get_categories_keyboard()
    )
    await state.clear()


# ========== ОБРАБОТКА КАТЕГОРИЙ (с отметками) ==========

@dp.callback_query(lambda c: c.data and c.data.startswith("cat_"))
async def select_category(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    category_code = callback.data.replace("cat_", "")
    current_codes = [c['code'] for c in get_user_categories(user_id)]
    if category_code in current_codes:
        current_codes.remove(category_code)
        await callback.answer(f"❌ Категория удалена", show_alert=False)
    else:
        current_codes.append(category_code)
        await callback.answer(f"✅ Категория добавлена", show_alert=False)
    set_user_categories(user_id, current_codes)

    updated_codes = [c['code'] for c in get_user_categories(user_id)]
    all_cats = get_all_categories()
    buttons = []
    row = []
    for i, cat in enumerate(all_cats):
        prefix = "✅" if cat['code'] in updated_codes else "⬜"
        row.append(InlineKeyboardButton(text=f"{prefix} {cat['emoji']} {cat['name']}", callback_data=f"cat_{cat['code']}"))
        if len(row) == 2 or i == len(all_cats) - 1:
            buttons.append(row)
            row = []
    buttons.append([InlineKeyboardButton(text="✅ Завершить выбор", callback_data="finish_categories")])
    new_markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    try:
        await callback.message.edit_text(
            "📋 *Выберите категории вакансий:*\n\n"
            "✅ — уже выбраны\n"
            "⬜ — можно добавить\n\n"
            "Когда закончите, нажмите «Завершить выбор»",
            parse_mode="Markdown",
            reply_markup=new_markup
        )
    except Exception as e:
        if "message is not modified" not in str(e):
            logger.error(f"Ошибка: {e}")


@dp.callback_query(lambda c: c.data == "finish_categories")
async def finish_categories(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    categories = get_user_categories(user_id)
    if not categories:
        await callback.answer("⚠️ Вы не выбрали ни одной категории!", show_alert=True)
        return
    categories_text = "\n".join([f"• {c['emoji']} {c['name']}" for c in categories])
    keyboard, status_text = get_main_keyboard(user_id)
    await callback.message.delete()
    await callback.message.answer(
        f"✅ *Вы подписались на вакансии!*\n\n"
        f"📌 Ваши категории:\n{categories_text}\n\n"
        f"Теперь я буду присылать вам новые вакансии по мере их появления.\n\n"
        f"Используйте кнопки для управления:",
        parse_mode="Markdown",
        reply_markup=keyboard
    )
    await callback.answer()

# ========== ПАГИНАЦИЯ ДЛЯ ПРОСМОТРА ВАКАНСИЙ ==========

@dp.message(lambda m: m.text == "🔍 Посмотреть новые вакансии")
async def show_new_vacancies(message: types.Message):
    user_id = message.from_user.id
    user_categories = get_user_categories(user_id)
    if not user_categories:
        await message.answer("⚠️ Вы ещё не выбрали категории вакансий. Используйте кнопку «✏️ Изменить категории»")
        return

    all_vacancies = []
    for cat in user_categories:
        vacancies = get_unsent_vacancies_by_category(cat['code'])
        for vac in vacancies:
            vac['category'] = cat
            all_vacancies.append(vac)

    if not all_vacancies:
        await message.answer("🔍 Новых вакансий по вашим категориям пока нет.\n\nЯ продолжаю мониторинг и сообщу, когда появятся!")
        return

    user_pages[user_id] = {
        "vacancies": all_vacancies,
        "page": 0,
        "total": len(all_vacancies)
    }
    await send_vacancy_page(message, user_id, 0)


async def send_vacancy_page(message: types.Message, user_id: int, page: int):
    data = user_pages.get(user_id)
    if not data:
        return
    vacancies = data["vacancies"]
    total = data["total"]
    start = page * 10
    end = min(start + 10, total)
    if start >= total:
        await message.answer("📭 Это последняя страница.")
        return

    await message.answer(f"📬 *Вакансии (страница {page+1} из {(total-1)//10 + 1})*", parse_mode="Markdown")
    for vac in vacancies[start:end]:
        raw_pub = vac.get('published_at') or vac.get('found_at')
        text = (
            f"{vac['category']['emoji']} *{vac['category']['name']}*\n"
            f"{escape_markdown(get_freshness_label(raw_pub))}\n"
            f"🕒 Опубликовано: {escape_markdown(format_publication_time(raw_pub))}\n"
            f"📢 Из чата: {escape_markdown(vac['source'])}\n\n"
            f"{escape_markdown(vac['text'][:400])}\n\n"
            f"🔗 [Ссылка на сообщение]({vac['link']})"
        )
        buttons = [[InlineKeyboardButton(text="✋ Откликнуться", callback_data=f"respond_{vac['id']}")]]
        if vac.get('address'):
            address = vac['address']
            maps_url = f"https://yandex.ru/maps/?text={address.replace(' ', '%20')}"
            buttons.append([InlineKeyboardButton(text="🗺️ Показать на карте", url=maps_url)])
        buttons.append([InlineKeyboardButton(text="⚠️ Пожаловаться", callback_data=f"complain_{vac['id']}")])
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        try:
            await message.answer(text, parse_mode="MarkdownV2", reply_markup=keyboard, disable_web_page_preview=True)
            await asyncio.sleep(0.3)
        except Exception as e:
            logger.error(f"Ошибка отправки вакансии: {e}")

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"vac_page_{page-1}"))
    if end < total:
        nav_buttons.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"vac_page_{page+1}"))
    if nav_buttons:
        nav_markup = InlineKeyboardMarkup(inline_keyboard=[nav_buttons])
        await message.answer("📄 *Навигация*", parse_mode="Markdown", reply_markup=nav_markup)


@dp.callback_query(lambda c: c.data and c.data.startswith("vac_page_"))
async def vacancy_page_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    page = int(callback.data.split("_")[2])
    await send_vacancy_page(callback.message, user_id, page)
    await callback.answer()


# ========== ОСНОВНЫЕ КОМАНДЫ ПОЛЬЗОВАТЕЛЯ ==========

@dp.message(lambda m: m.text == "📋 Мои категории")
async def show_my_categories(message: types.Message):
    categories = get_user_categories(message.from_user.id)
    if categories:
        text = "📌 *Ваши категории:*\n\n" + "\n".join([f"{c['emoji']} {c['name']}" for c in categories])
    else:
        text = "⚠️ Вы ещё не выбрали категории вакансий.\n\nИспользуйте кнопку «✏️ Изменить категории»"
    await message.answer(text, parse_mode="Markdown")

@dp.message(lambda m: m.text == "✏️ Изменить категории")
async def edit_categories(message: types.Message):
    await message.answer(
        "📋 *Выберите категории вакансий:*\n\n"
        "Когда закончите, нажмите «Завершить выбор»",
        parse_mode="Markdown",
        reply_markup=get_categories_keyboard()
    )

@dp.message(lambda m: m.text == "📞 Мои контакты")
async def show_my_contacts(message: types.Message):
    profile = get_subscriber_profile(message.from_user.id)
    if profile and profile.get("full_name") and profile.get("phone"):
        await message.answer(
            f"📞 *Ваши контактные данные:*\n\n"
            f"👤 ФИО: {profile['full_name']}\n"
            f"🎂 Возраст: {profile['age']} лет\n"
            f"📱 Телефон: {profile['phone']}\n\n"
            f"Эти данные будут переданы работодателю при отклике на вакансию.",
            parse_mode="Markdown"
        )
    else:
        await message.answer("⚠️ Ваш профиль не заполнен. Нажмите /start для заполнения.")

@dp.message(lambda m: m.text == "❌ Отписаться")
async def unsubscribe_user(message: types.Message):
    user_id = message.from_user.id
    set_user_categories(user_id, [])
    await message.answer(
        "❌ *Вы отписались от рассылки вакансий.*\n\n"
        "Если передумаете, просто нажмите /start и заполните профиль заново.",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove()
    )

@dp.message(lambda m: m.text == "❓ Поддержка")
async def support_menu(message: types.Message, state: FSMContext):
    await message.answer(
        "📞 *Поддержка*\n\n"
        "Опишите вашу проблему или вопрос, и администратор ответит вам в ближайшее время.\n\n"
        "Напишите ваше сообщение:",
        parse_mode="Markdown"
    )
    await state.set_state(SupportState.waiting_for_question)

@dp.message(SupportState.waiting_for_question)
async def process_support_question(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username
    add_support_request(user_id, message.text, username)
    await message.answer("✅ Ваш вопрос отправлен администратору. Ответ придёт сюда.")
    await state.clear()


# ========== ЖАЛОБЫ НА ВАКАНСИИ ==========

@dp.callback_query(lambda c: c.data and c.data.startswith("complain_"))
async def start_complaint(callback: types.CallbackQuery, state: FSMContext):
    vacancy_id = callback.data.replace("complain_", "")
    await state.update_data(vacancy_id=vacancy_id)
    await callback.message.answer(
        "⚠️ *Пожаловаться на вакансию*\n\n"
        "Выберите причину:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Вакансия неактуальна/закрыта", callback_data="complaint_reason_closed")],
            [InlineKeyboardButton(text="🤬 Грубость/неуважение", callback_data="complaint_reason_rude")],
            [InlineKeyboardButton(text="📛 Мошенничество/обман", callback_data="complaint_reason_scam")],
            [InlineKeyboardButton(text="📝 Другое (напишу)", callback_data="complaint_reason_other")]
        ])
    )
    await state.set_state(ComplaintState.waiting_for_reason)
    await callback.answer()

@dp.callback_query(lambda c: c.data and c.data.startswith("complaint_reason_"))
async def complaint_reason(callback: types.CallbackQuery, state: FSMContext):
    reason_map = {
        "complaint_reason_closed": "Вакансия неактуальна/закрыта",
        "complaint_reason_rude": "Грубость/неуважение",
        "complaint_reason_scam": "Мошенничество/обман",
        "complaint_reason_other": "Другое"
    }
    reason = reason_map.get(callback.data, "Другое")
    await state.update_data(reason=reason)
    if callback.data == "complaint_reason_other":
        await callback.message.answer("Напишите подробности жалобы (текст):")
        await state.set_state(ComplaintState.waiting_for_text)
    else:
        data = await state.get_data()
        vacancy_id = data.get("vacancy_id")
        user_id = callback.from_user.id
        add_complaint(user_id, vacancy_id, reason)
        await callback.message.answer("✅ Жалоба отправлена администратору. Спасибо, что помогаете улучшить сервис!")
        await state.clear()
    await callback.answer()

@dp.message(ComplaintState.waiting_for_text)
async def complaint_text(message: types.Message, state: FSMContext):
    data = await state.get_data()
    vacancy_id = data.get("vacancy_id")
    reason = data.get("reason")
    user_id = message.from_user.id
    add_complaint(user_id, vacancy_id, reason, message.text)
    await message.answer("✅ Жалоба отправлена администратору. Спасибо, что помогаете улучшить сервис!")
    await state.clear()


# ========== ОТКЛИКИ НА ВАКАНСИИ С ВОЗМОЖНОСТЬЮ ПРИКРЕПИТЬ ФОТО ==========

@dp.callback_query(lambda c: c.data and c.data.startswith("respond_"))
async def handle_response(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    vacancy_id = callback.data.replace("respond_", "")
    if is_already_responded(user_id, vacancy_id):
        await callback.answer("❌ Вы уже откликались на эту вакансию!", show_alert=True)
        return
    profile = get_subscriber_profile(user_id)
    if not profile or not profile.get("full_name") or not profile.get("phone"):
        await callback.answer("⚠️ Сначала заполните профиль! Нажмите /start", show_alert=True)
        return
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT message_text, message_link, source_chat_title, author_contact, address FROM vacancies WHERE id = ?", (vacancy_id,))
    vacancy_row = cur.fetchone()
    conn.close()
    if not vacancy_row:
        await callback.answer("❌ Вакансия не найдена", show_alert=True)
        return
    vacancy_text, vacancy_link, source_chat, saved_contact, address = vacancy_row
    employer_contact = saved_contact or extract_contact_from_text(vacancy_text or "")
    if not employer_contact:
        await callback.answer("⚠️ Контакт заказчика не найден. Отправлю отклик администратору.", show_alert=True)
        await send_to_admin(callback, profile, vacancy_row, build_candidate_profile_text(profile), profile.get('photo_file_id'))
        return
    required_fields = extract_required_fields_from_vacancy(vacancy_text or "")
    draft_text = build_candidate_profile_text(profile)
    contact_link = build_contact_link(employer_contact, draft_text)
    await state.update_data(vacancy_id=vacancy_id, contact=employer_contact, draft_text=draft_text)
    req_line = ", ".join(required_fields) if required_fields else "явных требований не найдено"
    msg = (
        "📨 *Черновик отклика готов*\n\n"
        f"👨‍💼 Контакт заказчика: `{escape_markdown(employer_contact)}`\n"
        f"📌 Источник: {escape_markdown(source_chat or '—')}\n"
        f"🧾 Что просит вакансия: {escape_markdown(req_line)}\n\n"
        "Нажмите кнопку ниже, откроется личный чат с заказчиком и готовым текстом анкеты.\n"
        "Перед отправкой можно отредактировать сообщение вручную."
    )
    buttons = []
    if contact_link:
        buttons.append([InlineKeyboardButton(text="✅ Открыть чат и отправить", url=contact_link)])
    buttons.append([InlineKeyboardButton(text="✏️ Добавить комментарий", callback_data=f"respond_add_{vacancy_id}")])
    buttons.append([InlineKeyboardButton(text="🚫 Отмена", callback_data="respond_cancel")])
    await callback.message.answer(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()

@dp.callback_query(lambda c: c.data == "admin_vacancy")
async def handle_admin_vacancy_response(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    vacancy_text = callback.message.caption or callback.message.text
    vacancy_id = f"admin_{callback.message.message_id}"
    if is_already_responded(user_id, vacancy_id):
        await callback.answer("❌ Вы уже откликались на эту вакансию!", show_alert=True)
        return
    profile = get_subscriber_profile(user_id)
    if not profile or not profile.get("full_name") or not profile.get("phone"):
        await callback.answer("⚠️ Сначала заполните профиль! Нажмите /start", show_alert=True)
        return
    add_response(user_id, vacancy_id, vacancy_text[:200] if vacancy_text else None, None, profile.get('photo_file_id'))
    candidate_questionnaire = profile.get('questionnaire') or f"""📝 *АНКЕТА КАНДИДАТА*

👤 *ФИО:* {profile['full_name']}
🎂 *Возраст:* {profile['age']} лет
📞 *Телефон:* {profile['phone']}
🆔 *Telegram:* @{profile['username'] if profile['username'] else 'нет'}
"""
    admin_message = (
        f"🔔 *НОВЫЙ ОТКЛИК НА АДМИНСКУЮ ВАКАНСИЮ!*\n\n"
        f"📝 *Текст вакансии:*\n{vacancy_text}\n\n"
        f"{candidate_questionnaire}\n"
        f"👤 *Ссылка на кандидата:* [Написать](tg://user?id={user_id})"
    )
    photo_file_id = profile.get('photo_file_id')
    if photo_file_id:
        await bot.send_photo(YOUR_USER_ID, photo_file_id, caption=admin_message, parse_mode="Markdown")
    else:
        await bot.send_message(YOUR_USER_ID, admin_message, parse_mode="Markdown")
    await callback.answer("✅ Ваш отклик отправлен администратору!", show_alert=True)
    await callback.message.edit_reply_markup(reply_markup=None)

@dp.callback_query(lambda c: c.data == "skip_photo_respond")
async def skip_photo_respond(callback: types.CallbackQuery, state: FSMContext):
    await send_application(callback, callback.from_user.id, (await state.get_data()).get("vacancy_id"), None)
    await state.clear()
    await callback.answer()

@dp.callback_query(lambda c: c.data and c.data.startswith("respond_add_"))
async def respond_add_comment(callback: types.CallbackQuery, state: FSMContext):
    vacancy_id = callback.data.replace("respond_add_", "")
    await state.update_data(vacancy_id=vacancy_id)
    await callback.message.answer("✏️ Напишите, что добавить в отклик одним сообщением:")
    await state.set_state(ResponseDraftState.waiting_for_comment)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "respond_cancel")
async def respond_cancel(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer("Отклик отменён", show_alert=False)

@dp.message(ResponseDraftState.waiting_for_comment)
async def respond_comment_received(message: types.Message, state: FSMContext):
    data = await state.get_data()
    vacancy_id = data.get("vacancy_id")
    profile = get_subscriber_profile(message.from_user.id)
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT message_text, author_contact FROM vacancies WHERE id = ?", (vacancy_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        await message.answer("❌ Вакансия не найдена.")
        await state.clear()
        return
    vacancy_text, saved_contact = row
    contact = saved_contact or extract_contact_from_text(vacancy_text or "")
    draft_text = build_candidate_profile_text(profile, extra_comment=message.text)
    link = build_contact_link(contact, draft_text)
    if not link:
        await message.answer(
            f"⚠️ Не удалось собрать ссылку для чата. Контакт: {contact or 'не найден'}\n\n"
            f"Скопируйте и отправьте вручную:\n\n{draft_text}"
        )
        await state.clear()
        return
    await message.answer(
        "✅ Обновил черновик. Откройте чат с заказчиком:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="✅ Открыть чат и отправить", url=link)]]
        )
    )
    await state.clear()

@dp.message(RespondWithPhotoState.waiting_for_photo)
async def respond_photo_received(message: types.Message, state: FSMContext):
    if message.photo:
        photo_file_id = message.photo[-1].file_id
        data = await state.get_data()
        vacancy_id = data.get("vacancy_id")
        await send_application(message, message.from_user.id, vacancy_id, photo_file_id)
    else:
        await message.answer("Пожалуйста, отправьте фото или нажмите «Пропустить»")
        return
    await state.clear()

async def send_application(target, user_id: int, vacancy_id: str, photo_file_id: str = None):
    profile = get_subscriber_profile(user_id)
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT message_text, message_link, source_chat_title, author_contact, address FROM vacancies WHERE id = ?", (vacancy_id,))
    vacancy_row = cur.fetchone()
    conn.close()
    if not vacancy_row:
        await target.answer("❌ Вакансия не найдена")
        return
    vacancy_text, vacancy_link, source_chat, saved_contact, address = vacancy_row
    add_response(user_id, vacancy_id, vacancy_text[:200] if vacancy_text else None, vacancy_link, photo_file_id)
    candidate_questionnaire = profile.get('questionnaire') or f"""📝 *АНКЕТА КАНДИДАТА*

👤 *ФИО:* {profile['full_name']}
🎂 *Возраст:* {profile['age']} лет
📞 *Телефон:* {profile['phone']}
🆔 *Telegram:* @{profile['username'] if profile['username'] else 'нет'}
"""
    employer_contact = saved_contact
    if not employer_contact:
        employer_contact = extract_contact_from_text(vacancy_text)
    if employer_contact:
        try:
            msg = f"🔔 *Новый отклик на вакансию!*\n\n📢 Вакансия из канала: {source_chat}\n\n{candidate_questionnaire}\n\n🔗 Ссылка на сообщение: {vacancy_link}"
            if photo_file_id:
                await bot.send_photo(employer_contact, photo_file_id, caption=msg, parse_mode="Markdown")
            else:
                await bot.send_message(employer_contact, msg, parse_mode="Markdown", disable_web_page_preview=True)
            await target.answer("✅ Ваша анкета отправлена работодателю!", show_alert=True)
            await bot.send_message(YOUR_USER_ID, f"✅ *Анкета отправлена заказчику!*\n\n👤 Кандидат: {profile['full_name']}\n📢 Вакансия: {source_chat}\n👨‍💼 Контакт: {employer_contact}", parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Не удалось отправить заказчику {employer_contact}: {e}")
            await send_to_admin(target, profile, vacancy_row, candidate_questionnaire, photo_file_id)
    else:
        await send_to_admin(target, profile, vacancy_row, candidate_questionnaire, photo_file_id)

async def send_to_admin(target, profile: dict, vacancy_row: tuple, candidate_questionnaire: str, photo_file_id: str = None):
    vacancy_text, vacancy_link, source_chat, _, address = vacancy_row
    user_link = f"[{profile['full_name']}](tg://user?id={profile['user_id']})"
    admin_message = (
        f"🔔 *НОВЫЙ ОТКЛИК НА ВАКАНСИЮ!*\n\n"
        f"📢 Источник: {source_chat}\n"
        f"🔗 Ссылка: {vacancy_link}\n\n"
        f"👤 *Кандидат:*\n"
        f"• ФИО: {profile['full_name']}\n"
        f"• Возраст: {profile['age']} лет\n"
        f"• Телефон: {profile['phone']}\n"
        f"• Username: @{profile['username'] if profile['username'] else 'нет'}\n\n"
        f"📞 Свяжитесь с кандидатом: {user_link}\n\n"
        f"⚠️ *Контакт заказчика не найден!* Перешлите анкету вручную."
    )
    if photo_file_id:
        await bot.send_photo(YOUR_USER_ID, photo_file_id, caption=admin_message, parse_mode="MarkdownV2", disable_web_page_preview=True)
    else:
        await bot.send_message(YOUR_USER_ID, admin_message, parse_mode="MarkdownV2", disable_web_page_preview=True)
    await target.answer("✅ Отклик отправлен администратору! Он свяжется с вами.", show_alert=True)

@dp.callback_query(lambda c: c.data == "already_responded")
async def already_responded(callback: types.CallbackQuery):
    await callback.answer("Вы уже откликались на эту вакансию", show_alert=True)


# ========== АДМИНСКИЕ КОМАНДЫ ==========

@dp.message(Command("admin"))
async def admin_menu(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    stats = get_admin_stats()
    await message.answer(
        f"👑 *Панель администратора*\n\n"
        f"📊 Статистика:\n"
        f"• Подписчиков: {stats['subscribers']}\n"
        f"• Полных профилей: {stats['full_profiles']}\n"
        f"• Откликов: {stats['responses']}\n"
        f"• Вакансий в очереди: {stats['pending_vacancies']}\n"
        f"• Всего вакансий: {stats['total_vacancies']}\n"
        f"⚠️ Жалоб: {stats['pending_complaints']}\n"
        f"❓ Вопросов в поддержку: {stats['pending_support']}",
        parse_mode="Markdown"
    )

@dp.message(Command("status"))
async def status_cmd(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    stats = get_admin_stats()
    await message.answer(
        f"📊 *Статус бота*\n\n✅ Активен\n📡 Режим: real‑time (мгновенные уведомления)\n👥 Подписчиков: {stats['subscribers']}\n💬 Откликов: {stats['responses']}",
        parse_mode="Markdown"
    )

@dp.message(Command("check_now"))
async def check_now_cmd(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    status_msg = await message.answer("🔍 Начинаю проверку...")
    try:
        orders, closed_data = await run_parser()
        if not orders and not closed_data:
            await status_msg.edit_text("✅ Новых вакансий не найдено.")
            return
        if closed_data:
            await notify_closed_vacancies(closed_data)
        for order in orders:
            await send_vacancy_to_subscribers(order)
        await status_msg.edit_text(f"✅ Проверка завершена. Найдено вакансий: {len(orders)}. Уведомлений о закрытии: {len(closed_data)}")
    except Exception as e:
        logger.error(f"Ошибка в check_now_cmd: {e}")
        await status_msg.edit_text(f"❌ Ошибка: {str(e)[:100]}")

@dp.message(Command("debug_last"))
async def debug_last_cmd(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    await message.answer(get_last_debug_report())

@dp.message(Command("usercards"))
async def usercards_cmd(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    await show_admin_user_cards(message, page=0)

@dp.message(Command("catmap"))
async def catmap_cmd(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    mapping = get_user_category_mapping()
    if not mapping:
        await message.answer("📭 Маппинг пуст.")
        return
    lines = ["🧭 *Маппинг категорий (активные подписчики):*"]
    for row in mapping:
        lines.append(f"• {row['emoji']} {row['name']} (`{row['code']}`): *{row['subscribers_count']}*")
    await message.answer("\n".join(lines), parse_mode="Markdown")

@dp.message(Command("myid"))
async def show_my_id(message: types.Message):
    await message.answer(
        f"📌 Ваш Telegram ID: `{message.from_user.id}`\n"
        f"ID в .env: `{YOUR_USER_ID}`\n"
        f"{'✅ Совпадает' if message.from_user.id == YOUR_USER_ID else '❌ НЕ СОВПАДАЕТ! Обновите .env'}"
    )

@dp.message(Command("broadcast"))
async def broadcast_cmd(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("📢 *Отправить рассылку*\nИспользование: `/broadcast Текст`", parse_mode="Markdown")
        return
    text = parts[1]
    subscribers = get_all_subscribers()
    if not subscribers:
        await message.answer("Нет подписчиков.")
        return
    status_msg = await message.answer(f"📢 Рассылка {len(subscribers)} подписчикам...")
    sent = 0
    for uid in subscribers:
        try:
            await bot.send_message(uid, f"📢 *Рассылка от администратора:*\n\n{text}", parse_mode="Markdown")
            sent += 1
            await asyncio.sleep(0.05)
        except:
            pass
    await status_msg.edit_text(f"✅ Отправлено {sent} из {len(subscribers)}")

@dp.message(Command("clean_old"))
async def clean_old_vacancies(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("DELETE FROM vacancies WHERE found_at < datetime('now', '-3 days')")
    cur.execute("DELETE FROM processed_messages WHERE processed_at < datetime('now', '-3 days')")
    conn.commit()
    conn.close()
    await message.answer("✅ Удалены вакансии и обработанные сообщения старше 3 дней.")

@dp.message(Command("addchat"))
async def add_chat_cmd(message: types.Message, state: FSMContext):
    if message.from_user.id != YOUR_USER_ID:
        return
    await message.answer("➕ *Добавление нового чата для парсинга*\n\nВведите ссылку на чат (например, https://t.me/chatname):", parse_mode="Markdown")
    await state.set_state(AddChatState.waiting_for_link)

@dp.message(AddChatState.waiting_for_link)
async def process_add_chat(message: types.Message, state: FSMContext):
    chat_link = normalize_chat_link(message.text)
    if not chat_link:
        await message.answer("❌ Неверный формат. Отправьте @username, username или ссылку t.me/...")
        return
    if add_target_chat(chat_link):
        await message.answer(
            f"✅ Чат {chat_link} добавлен для парсинга.\n"
            "Новые сообщения будут подхватываться автоматически."
        )
    else:
        await message.answer(f"⚠️ Чат {chat_link} уже существует.")
    await state.clear()

@dp.message(Command("removechat"))
async def remove_chat_cmd(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("❌ Использование: `/removechat https://t.me/chatname`", parse_mode="Markdown")
        return
    chat_link = parts[1]
    remove_target_chat(chat_link)
    await message.answer(f"🗑️ Чат {chat_link} удалён из парсинга.")

@dp.message(Command("postvacancy"))
async def post_vacancy_cmd(message: types.Message, state: FSMContext):
    if message.from_user.id != YOUR_USER_ID:
        return
    await message.answer("📤 *Отправка вакансии подписчикам*\n\nВыберите категорию:", parse_mode="Markdown", reply_markup=get_categories_keyboard())
    await state.set_state(PostVacancyState.waiting_for_category)

@dp.callback_query(lambda c: c.data and c.data.startswith("cat_"))
async def post_vacancy_category(callback: types.CallbackQuery, state: FSMContext):
    # Проверяем, что мы действительно в состоянии ожидания категории
    current_state = await state.get_state()
    if current_state != PostVacancyState.waiting_for_category:
        await callback.answer("❌ Сначала выполните команду /postvacancy", show_alert=True)
        return
    
    category_code = callback.data.replace("cat_", "")
    all_cats = get_all_categories()
    cat_name = next((cat['name'] for cat in all_cats if cat['code'] == category_code), category_code)
    await state.update_data(category_code=category_code, category_name=cat_name)
    await callback.message.answer(f"📝 Введите текст вакансии (категория: {cat_name}):")
    await state.set_state(PostVacancyState.waiting_for_text)
    await callback.answer()

@dp.message(PostVacancyState.waiting_for_text)
async def post_vacancy_text(message: types.Message, state: FSMContext):
    await state.update_data(message_text=message.text)
    await message.answer("🖼️ Отправьте фото для вакансии (необязательно) или нажмите «Пропустить»:", reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="⏩ Пропустить")]], resize_keyboard=True))
    await state.set_state(PostVacancyState.waiting_for_photo)

@dp.message(PostVacancyState.waiting_for_photo)
async def post_vacancy_photo(message: types.Message, state: FSMContext):
    data = await state.get_data()
    category_code = data['category_code']
    category_name = data['category_name']
    vacancy_text = data['message_text']
    photo_file_id = None
    if message.photo:
        photo_file_id = message.photo[-1].file_id
    elif message.text == "⏩ Пропустить":
        pass
    else:
        await message.answer("Пожалуйста, отправьте фото или нажмите «Пропустить»")
        return
    subscribers = get_subscribers_by_category(category_code)
    if not subscribers:
        await message.answer(f"⚠️ Нет подписчиков на категорию {category_name}. Вакансия не отправлена.")
        await state.clear()
        return
    text = f"{get_category_emoji(category_code)} *Вакансия от администратора:*\n\n{escape_markdown(vacancy_text[:500])}"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✋ Откликнуться", callback_data="admin_vacancy")]])
    sent = 0
    for sub in subscribers:
        try:
            if photo_file_id:
                await bot.send_photo(sub['user_id'], photo_file_id, caption=text, parse_mode="MarkdownV2", reply_markup=keyboard)
            else:
                await bot.send_message(sub['user_id'], text, parse_mode="MarkdownV2", reply_markup=keyboard)
            sent += 1
            await asyncio.sleep(0.2)
        except Exception as e:
            logger.error(f"Ошибка отправки админ-вакансии {sub['user_id']}: {e}")
    await message.answer(f"✅ Вакансия отправлена {sent} подписчикам категории {category_name}.", reply_markup=ReplyKeyboardRemove())
    await state.clear()


# ========== КНОПКИ АДМИН-МЕНЮ ==========

@dp.message(lambda m: m.text == "📊 Статистика")
async def admin_stats_button(message: types.Message):
    if message.from_user.id == YOUR_USER_ID:
        await admin_menu(message)

@dp.message(lambda m: m.text == "🔍 Ручная проверка")
async def admin_check_button(message: types.Message):
    if message.from_user.id == YOUR_USER_ID:
        await check_now_cmd(message)

@dp.message(lambda m: m.text == "📋 Список откликов")
async def admin_responses_button(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    recent = get_recent_responses(10)
    if not recent:
        await message.answer("📭 Нет откликов.")
        return
    text = "📋 *Последние отклики:*\n\n"
    for resp in recent:
        time = resp[0][:16] if resp[0] else "—"
        name = resp[3] or "Пользователь"
        preview = (resp[1][:50] + "...") if resp[1] and len(resp[1]) > 50 else (resp[1] or "—")
        text += f"• {time} — {name}: {preview}\n"
    await message.answer(text, parse_mode="Markdown")

@dp.message(lambda m: m.text == "📝 Отчёт парсера")
async def admin_debug_button(message: types.Message):
    if message.from_user.id == YOUR_USER_ID:
        await debug_last_cmd(message)

@dp.message(lambda m: m.text == "👥 Список подписчиков")
async def admin_subscribers_button(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    subs = get_all_subscribers()
    if not subs:
        await message.answer("📭 Нет подписчиков.")
        return
    text = "👥 *Список подписчиков:*\n\n"
    for i, uid in enumerate(subs[:20], 1):
        prof = get_subscriber_profile(uid)
        name = prof.get('full_name') or prof.get('first_name') or f"ID:{uid}" if prof else f"ID:{uid}"
        text += f"{i}. {name}\n"
    if len(subs) > 20:
        text += f"\n... и ещё {len(subs)-20}"
    await message.answer(text, parse_mode="Markdown")

@dp.message(lambda m: m.text == "📢 Рассылка")
async def admin_broadcast_button(message: types.Message):
    if message.from_user.id == YOUR_USER_ID:
        await message.answer("📢 Используйте команду `/broadcast Текст`", parse_mode="Markdown")

async def show_admin_user_cards(message: types.Message, page: int = 0):
    limit = 5
    offset = page * limit
    cards = get_subscriber_cards(limit=limit, offset=offset)
    if not cards:
        await message.answer("📭 Карточек больше нет.")
        return
    total_subs = len(get_all_subscribers())
    pages_total = max(1, (total_subs + limit - 1) // limit)
    lines = [f"🗂️ *Карточки пользователей* (страница {page + 1}/{pages_total})"]
    for i, card in enumerate(cards, 1):
        lines.append("")
        lines.append(format_user_card(card, idx=offset + i))
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin_cards_{page-1}"))
    if page + 1 < pages_total:
        nav.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"admin_cards_{page+1}"))
    markup = InlineKeyboardMarkup(inline_keyboard=[nav]) if nav else None
    await message.answer("\n".join(lines), parse_mode="Markdown", reply_markup=markup)

@dp.message(lambda m: m.text == "🗂️ Карточки пользователей")
async def admin_user_cards_button(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    await show_admin_user_cards(message, page=0)

@dp.message(lambda m: m.text == "🧭 Маппинг категорий")
async def admin_category_mapping_button(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    await catmap_cmd(message)

@dp.callback_query(lambda c: c.data and c.data.startswith("admin_cards_"))
async def admin_cards_page_callback(callback: types.CallbackQuery):
    if callback.from_user.id != YOUR_USER_ID:
        await callback.answer("Недоступно", show_alert=True)
        return
    page = int(callback.data.split("_")[2])
    await show_admin_user_cards(callback.message, page=page)
    await callback.answer()

@dp.message(lambda m: m.text == "⚠️ Жалобы")
async def admin_complaints_button(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    complaints = get_recent_complaints(20)
    if not complaints:
        await message.answer("📭 Нет жалоб.")
        return
    text = "⚠️ *Новые жалобы:*\n\n"
    for c in complaints:
        text += f"ID: {c[0]}, от {c[2]} (user {c[1]})\nВакансия: {c[3]}\nПричина: {c[4]}\nТекст: {c[5] or '—'}\nДата: {c[6]}\n\n"
    await message.answer(text, parse_mode="Markdown")

@dp.message(lambda m: m.text == "❓ Поддержка (админ)")
async def admin_support_button(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    requests = get_unanswered_support_requests(20)
    if not requests:
        await message.answer("📭 Нет новых обращений в поддержку.")
        return
    text = "❓ *Новые обращения:*\n\n"
    for req in requests:
        text += f"ID: {req[0]}, от {req[2] or req[1]} (user {req[1]})\nСообщение: {req[3]}\nДата: {req[4]}\n\n"
    await message.answer(text, parse_mode="Markdown")
    await message.answer("Чтобы ответить, используйте команду `/answer ID_обращения ответ`")

@dp.message(Command("answer"))
async def answer_support(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("❌ Использование: `/answer ID_обращения текст_ответа`", parse_mode="Markdown")
        return
    try:
        req_id = int(parts[1])
        answer_text = parts[2]
    except:
        await message.answer("❌ Неверный ID обращения")
        return
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM support_requests WHERE id = ? AND answered = 0", (req_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        await message.answer("❌ Обращение не найдено или уже отвечено.")
        return
    user_id = row[0]
    mark_support_answered(req_id, answer_text)
    conn.close()
    try:
        await bot.send_message(user_id, f"📞 *Ответ от администратора:*\n\n{answer_text}", parse_mode="Markdown")
        await message.answer(f"✅ Ответ отправлен пользователю {user_id}")
    except Exception as e:
        await message.answer(f"❌ Не удалось отправить ответ: {e}")

@dp.message(lambda m: m.text == "➕ Добавить чат")
async def admin_add_chat_button(message: types.Message, state: FSMContext):
    if message.from_user.id == YOUR_USER_ID:
        await add_chat_cmd(message, state)

@dp.message(lambda m: m.text == "📤 Отправить вакансию")
async def admin_post_vacancy_button(message: types.Message, state: FSMContext):
    if message.from_user.id == YOUR_USER_ID:
        await post_vacancy_cmd(message, state)

@dp.message(lambda m: m.text == "❌ Закрыть меню")
async def admin_close_menu(message: types.Message):
    if message.from_user.id == YOUR_USER_ID:
        await message.answer("Меню закрыто. Для открытия напишите /start", reply_markup=ReplyKeyboardRemove())


# ========== ЗАПУСК И ОСТАНОВКА ==========

async def on_startup():
    logger.info("🚀 Запуск бота...")
    init_db()
    logger.info("📁 База данных инициализирована")

    # Один раз прогоняем парсер, чтобы подхватить сообщения за последние несколько минут (гарантия)
    logger.info("🔄 Однократная проверка новых сообщений (для синхронизации)...")
    orders, closed = await get_new_messages(limit_per_chat=200)
    if closed:
        await notify_closed_vacancies(closed)
    for order in orders:
        await send_vacancy_to_subscribers(order)
    logger.info("✅ Однократная проверка завершена")

    # Запускаем real-time listener
    asyncio.create_task(start_realtime_listener(send_vacancy_to_subscribers))
    logger.info("📡 Real-time listener запущен")

    # Запускаем polling для бота
    logger.info("📡 Запуск polling...")
    await dp.start_polling(bot)

async def on_shutdown():
    logger.info("🛑 Остановка бота...")
    await stop_realtime_listener()
    await bot.session.close()
    logger.info("👋 Бот остановлен")

async def main():
    try:
        await on_startup()
    except KeyboardInterrupt:
        logger.info("⚠️ Получен сигнал остановки")
    except Exception as e:
        logger.error(f"❌ Критическая ошибка: {e}", exc_info=True)
    finally:
        await on_shutdown()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Бот остановлен пользователем")
