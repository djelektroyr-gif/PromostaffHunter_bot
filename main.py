import asyncio
import re
import logging
import hashlib
import sqlite3
from datetime import datetime
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from db import *
from parser import get_new_messages, get_last_debug_report, detect_category
from config import BOT_TOKEN, YOUR_USER_ID

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
periodic_task = None  # Глобальная переменная для периодической задачи

# Состояния для FSM
class RegistrationState(StatesGroup):
    waiting_for_name = State()
    waiting_for_birthdate = State()
    waiting_for_phone = State()


# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========

def escape_markdown(text: str) -> str:
    """Экранирует спецсимволы Markdown"""
    if not text:
        return ""
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)


def generate_vacancy_id(chat_id: str, message_id: str) -> str:
    """Генерирует уникальный ID вакансии"""
    unique_str = f"{chat_id}_{message_id}"
    return hashlib.md5(unique_str.encode()).hexdigest()[:16]


def get_category_emoji(category_code: str) -> str:
    """Возвращает эмодзи для категории"""
    emojis = {
        "promoter": "📢",
        "hostess": "👩‍💼",
        "wardrobe": "🧥",
        "animator": "🎭",
        "helper": "👷",
        "loader": "📦",
        "waiter": "🍽️",
        "driver": "🚐",
        "security": "🛡️",
        "parking": "🚗",
        "supervisor": "👨‍💼"
    }
    return emojis.get(category_code, "📌")


def calculate_age(birth_date_str: str) -> int:
    """Вычисляет возраст по дате рождения (формат ДД.ММ.ГГГГ)"""
    try:
        birth_date = datetime.strptime(birth_date_str, "%d.%m.%Y")
        today = datetime.now()
        age = today.year - birth_date.year
        # Если день рождения ещё не наступил в этом году
        if (today.month, today.day) < (birth_date.month, birth_date.day):
            age -= 1
        return age
    except ValueError:
        return None


# ========== РАССЫЛКА ВАКАНСИЙ ПОДПИСЧИКАМ ==========

async def send_vacancy_to_subscribers(order: dict):
    """Отправляет вакансию всем подписчикам, которые выбрали соответствующую категорию"""
    category_code = order.get('category', detect_category(order['message_text']))
    vacancy_id = generate_vacancy_id(order.get('chat_id', ''), order.get('message_id', ''))
    
    # Получаем подписчиков на эту категорию
    subscribers = get_subscribers_by_category(category_code)
    
    if not subscribers:
        logger.info(f"Нет подписчиков на категорию {category_code}, вакансия не отправлена")
        return
    
    # Формируем сообщение
    text = (
        f"{get_category_emoji(category_code)} *Вакансия:*\n\n"
        f"{escape_markdown(order['message_text'][:500])}\n\n"
        f"📢 Источник: {escape_markdown(order['chat_title'])}\n"
        f"🔗 [Перейти к сообщению]({order['message_link']})"
    )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✋ Откликнуться на вакансию", callback_data=f"respond_{vacancy_id}")]
    ])
    
    # Сохраняем вакансию в БД
    save_vacancy(
        vacancy_id=vacancy_id,
        source_chat=order.get('chat_id', ''),
        source_chat_title=order['chat_title'],
        category_code=category_code,
        message_text=order['message_text'][:1000],
        message_link=order['message_link']
    )
    
    # Отправляем подписчикам
    sent_count = 0
    for subscriber in subscribers:
        try:
            # Проверяем, не получал ли уже эту вакансию
            if has_user_received_vacancy(subscriber['user_id'], vacancy_id):
                continue
                
            await bot.send_message(
                subscriber['user_id'],
                text,
                parse_mode="MarkdownV2",
                disable_web_page_preview=True,
                reply_markup=keyboard
            )
            mark_vacancy_sent_to_user(vacancy_id, subscriber['user_id'])
            sent_count += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            if "bot was blocked by the user" in str(e):
                logger.info(f"Пользователь {subscriber['user_id']} заблокировал бота")
            else:
                logger.error(f"Ошибка отправки {subscriber['user_id']}: {e}")
    
    logger.info(f"Вакансия {vacancy_id} (категория {category_code}) отправлена {sent_count} подписчикам")
    mark_vacancy_sent(vacancy_id)


# ========== КЛАВИАТУРЫ ==========

def get_categories_keyboard():
    """Создаёт инлайн-клавиатуру с категориями вакансий"""
    categories = get_all_categories()
    buttons = []
    row = []
    for i, cat in enumerate(categories):
        row.append(InlineKeyboardButton(
            text=f"{cat['emoji']} {cat['name']}", 
            callback_data=f"cat_{cat['code']}"
        ))
        if len(row) == 2 or i == len(categories) - 1:
            buttons.append(row)
            row = []
    
    buttons.append([InlineKeyboardButton(text="✅ Завершить выбор", callback_data="finish_categories")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_main_keyboard(user_id: int):
    """Главная клавиатура пользователя"""
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
            [KeyboardButton(text="📞 Мои контакты"), KeyboardButton(text="❌ Отписаться")]
        ],
        resize_keyboard=True
    )
    return keyboard, status_text
def get_admin_keyboard():
    """Создаёт Reply-клавиатуру для администратора"""
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="🔍 Ручная проверка")],
            [KeyboardButton(text="📋 Список откликов"), KeyboardButton(text="📝 Отчёт парсера")],
            [KeyboardButton(text="👥 Список подписчиков"), KeyboardButton(text="📢 Рассылка")],
            [KeyboardButton(text="❌ Закрыть меню")]
        ],
        resize_keyboard=True
    )
    return keyboard


# ========== КОМАНДЫ И ОБРАБОТЧИКИ ==========

@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name
    last_name = message.from_user.last_name
    
    # ========== ДЛЯ АДМИНИСТРАТОРА ==========
    if user_id == YOUR_USER_ID:
        admin_keyboard = get_admin_keyboard()
        await message.answer(
            f"👋 Здравствуйте, Администратор {first_name}!\n\n"
            f"📊 *Бот работает в штатном режиме.*\n\n"
            f"Используйте кнопки для управления ботом:",
            parse_mode="Markdown",
            reply_markup=admin_keyboard
        )
        return
    
    # ========== ДЛЯ ОБЫЧНЫХ ПОЛЬЗОВАТЕЛЕЙ ==========
    # Добавляем пользователя в БД
    add_subscriber(user_id, username, first_name, last_name)
    profile = get_subscriber_profile(user_id)
    
    # Если профиль уже заполнен
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
            # Профиль есть, но категории не выбраны
            await message.answer(
                f"👋 С возвращением, {first_name}!\n\n"
                f"Ваш профиль уже заполнен, но вы ещё не выбрали категории вакансий.\n\n"
                f"📋 *Выберите категории вакансий:*",
                parse_mode="Markdown",
                reply_markup=get_categories_keyboard()
            )
            return
    
    # Если данные не заполнены — начинаем регистрацию
    await message.answer(
        "👋 *Добро пожаловать в бот поиска работы!*\n\n"
        "Я помогу вам найти подходящие вакансии.\n\n"
        "📝 *Давайте заполним ваш профиль*\n\n"
        "Как вас зовут? (ФИО полностью)\n\n"
        "Пример: *Иван Петров*",
        parse_mode="Markdown"
    )
    await state.set_state(RegistrationState.waiting_for_name)


@dp.message(RegistrationState.waiting_for_name)
async def process_name(message: types.Message, state: FSMContext):
    full_name = message.text.strip()
    
    # Валидация ФИО: минимум 2 слова, только буквы, дефисы и пробелы
    name_parts = full_name.split()
    if len(name_parts) < 2:
        await message.answer(
            "❌ Пожалуйста, введите полное имя и фамилию (минимум 2 слова).\n\n"
            "Пример: *Иван Петров*",
            parse_mode="Markdown"
        )
        return
    
    # Проверяем, что только буквы, пробелы, дефисы и точки
    if not re.match(r'^[a-zA-Zа-яА-ЯёЁ\s\-\.]+$', full_name):
        await message.answer(
            "❌ Имя может содержать только буквы, пробелы, дефисы и точки.\n\n"
            "Пример: *Иван Петров* или *Иван-Петр Сидоров*",
            parse_mode="Markdown"
        )
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
    
    # Проверяем формат ДД.ММ.ГГГГ
    if not re.match(r'^\d{2}\.\d{2}\.\d{4}$', birth_date_str):
        await message.answer(
            "❌ Неверный формат!\n\n"
            "Пожалуйста, введите дату в формате: **ДД.ММ.ГГГГ**\n\n"
            "Пример: `25.12.1990`",
            parse_mode="Markdown"
        )
        return
    
    # Вычисляем возраст
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
    
    if age < 16:
        await message.answer(
            "❌ К сожалению, мы принимаем заявки только от кандидатов старше 16 лет.\n\n"
            f"Ваш возраст: {age} лет.",
            parse_mode="Markdown"
        )
        return
    
    if age > 100:
        await message.answer(
            "❌ Пожалуйста, проверьте правильность введённой даты.",
            parse_mode="Markdown"
        )
        return
    
    await state.update_data(birth_date=birth_date_str, age=age)
    
    # Клавиатура с кнопкой отправки контакта
    phone_keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Отправить мой номер телефона", request_contact=True)]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    
    await message.answer(
        f"✅ Возраст: {age} лет\n\n"
        f"📞 *Контактный телефон*\n\n"
        f"Нажмите на кнопку ниже, чтобы отправить ваш номер телефона.\n"
        f"Он будет передан работодателю при отклике на вакансию.",
        parse_mode="Markdown",
        reply_markup=phone_keyboard
    )
    await state.set_state(RegistrationState.waiting_for_phone)


@dp.message(RegistrationState.waiting_for_phone)
async def process_phone(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    
    # Получаем номер телефона (из контакта или из текста)
    if message.contact:
        phone = message.contact.phone_number
    else:
        phone = message.text.strip()
        # Валидация номера телефона
        digits_only = re.sub(r'\D', '', phone)
        if len(digits_only) < 10 or len(digits_only) > 15:
            await message.answer(
                "❌ Пожалуйста, введите корректный номер телефона.\n\n"
                "Примеры:\n"
                "+7 999 123-45-67\n"
                "89991234567\n\n"
                "Или нажмите кнопку «Отправить мой номер телефона»",
                parse_mode="Markdown",
                reply_markup=ReplyKeyboardMarkup(
                    keyboard=[[KeyboardButton(text="📱 Отправить мой номер телефона", request_contact=True)]],
                    resize_keyboard=True,
                    one_time_keyboard=True
                )
            )
            return
    
    data = await state.get_data()
    
    # Сохраняем профиль
    update_subscriber_profile(
        user_id, 
        data['full_name'], 
        data['age'], 
        phone
    )
    
    # Убираем клавиатуру
    await message.answer(
        "✅ *Профиль успешно создан!*\n\n"
        f"📝 ФИО: {data['full_name']}\n"
        f"🎂 Дата рождения: {data['birth_date']}\n"
        f"📊 Возраст: {data['age']} лет\n"
        f"📞 Телефон: {phone}\n\n"
        "Теперь выберите категории вакансий, которые вас интересуют.\n\n"
        "Вы можете выбрать несколько:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove()
    )
    
    # Показываем выбор категорий
    await message.answer(
        "📋 *Выберите категории вакансий:*",
        parse_mode="Markdown",
        reply_markup=get_categories_keyboard()
    )
    await state.clear()


# ========== ОБРАБОТКА КАТЕГОРИЙ ==========

@dp.callback_query(lambda c: c.data and c.data.startswith("cat_"))
async def select_category(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    category_code = callback.data.replace("cat_", "")
    
    current_categories = get_user_categories(user_id)
    current_codes = [c['code'] for c in current_categories]
    
    if category_code in current_codes:
        current_codes.remove(category_code)
        await callback.answer(f"❌ Категория удалена", show_alert=False)
    else:
        current_codes.append(category_code)
        await callback.answer(f"✅ Категория добавлена", show_alert=False)
    
    set_user_categories(user_id, current_codes)
    
    # Получаем обновлённый список категорий
    updated_categories = get_user_categories(user_id)
    updated_codes = [c['code'] for c in updated_categories]
    
    # Создаём новую клавиатуру с отметками
    all_categories = get_all_categories()
    buttons = []
    row = []
    for i, cat in enumerate(all_categories):
        # Добавляем галочку к выбранным категориям
        prefix = "✅" if cat['code'] in updated_codes else "⬜"
        row.append(InlineKeyboardButton(
            text=f"{prefix} {cat['emoji']} {cat['name']}", 
            callback_data=f"cat_{cat['code']}"
        ))
        if len(row) == 2 or i == len(all_categories) - 1:
            buttons.append(row)
            row = []
    
    buttons.append([InlineKeyboardButton(text="✅ Завершить выбор", callback_data="finish_categories")])
    new_markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    # Обновляем сообщение с обработкой ошибки
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
            logger.error(f"Ошибка при редактировании: {e}")


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


# ========== ОСНОВНЫЕ КОМАНДЫ ПОЛЬЗОВАТЕЛЯ ==========

@dp.message(lambda m: m.text == "🔍 Посмотреть новые вакансии")
async def show_new_vacancies(message: types.Message):
    user_id = message.from_user.id
    user_categories = get_user_categories(user_id)
    
    if not user_categories:
        await message.answer(
            "⚠️ Вы ещё не выбрали категории вакансий.\n"
            "Используйте кнопку «✏️ Изменить категории»"
        )
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
    
    await message.answer(f"📬 Найдено {len(all_vacancies)} новых вакансий.")
    
    for vac in all_vacancies[:10]:
        # Экранируем текст вакансии!
        escaped_text = escape_markdown(vac['text'][:400])
        
        text = (
            f"{vac['category']['emoji']} *{vac['category']['name']}*\n"
            f"📢 Из чата: {escape_markdown(vac['source'])}\n\n"
            f"{escaped_text}\n\n"
            f"🔗 [Ссылка на сообщение]({vac['link']})"
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✋ Откликнуться", callback_data=f"respond_{vac['id']}")]
        ])
        
        try:
            await message.answer(text, parse_mode="MarkdownV2", reply_markup=keyboard, disable_web_page_preview=True)
            await asyncio.sleep(0.3)
        except Exception as e:
            logger.error(f"Ошибка отправки вакансии: {e}")
            # Пробуем отправить без Markdown форматирования
            try:
                plain_text = f"{vac['category']['emoji']} {vac['category']['name']}\n\n{vac['text'][:400]}\n\n{vac['link']}"
                await message.answer(plain_text, reply_markup=keyboard)
            except Exception as e2:
                logger.error(f"Не удалось отправить даже plain текст: {e2}")


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


# ========== ОБРАБОТКА ОТКЛИКОВ ==========

@dp.callback_query(lambda c: c.data and c.data.startswith("respond_"))
async def handle_response(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    vacancy_id = callback.data.replace("respond_", "")
    
    if is_already_responded(user_id, vacancy_id):
        await callback.answer("❌ Вы уже откликались на эту вакансию!", show_alert=True)
        return
    
    profile = get_subscriber_profile(user_id)
    if not profile or not profile.get("full_name") or not profile.get("phone"):
        await callback.answer("⚠️ Сначала заполните профиль! Нажмите /start", show_alert=True)
        return
    
    # Получаем информацию о вакансии
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT message_text, message_link, source_chat_title FROM vacancies WHERE id = ?", (vacancy_id,))
    vacancy = cur.fetchone()
    conn.close()
    
    if not vacancy:
        await callback.answer("❌ Вакансия не найдена", show_alert=True)
        return
    
    add_response(user_id, vacancy_id)
    
    user_link = f"[{profile['full_name']}](tg://user?id={user_id})"
    
    admin_message = (
        f"🔔 *НОВЫЙ ОТКЛИК НА ВАКАНСИЮ!*\n\n"
        f"📌 ID вакансии: `{vacancy_id}`\n"
        f"📢 Источник: {vacancy[2]}\n"
        f"🔗 Ссылка: {vacancy[1]}\n\n"
        f"👤 *Кандидат:*\n"
        f"• ФИО: {profile['full_name']}\n"
        f"• Возраст: {profile['age']} лет\n"
        f"• Телефон: {profile['phone']}\n"
        f"• Username: @{profile['username'] if profile['username'] else 'нет'}\n\n"
        f"📞 Свяжитесь с кандидатом: {user_link}"
    )
    
    await bot.send_message(YOUR_USER_ID, admin_message, parse_mode="MarkdownV2", disable_web_page_preview=True)
    
    await callback.answer(
        "✅ Ваш отклик отправлен!\nРаботодатель свяжется с вами.",
        show_alert=True
    )
    
    await callback.message.edit_reply_markup(
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Отклик отправлен", callback_data="already_responded")]
        ])
    )


@dp.callback_query(lambda c: c.data == "already_responded")
async def already_responded(callback: types.CallbackQuery):
    await callback.answer("Вы уже откликались на эту вакансию", show_alert=True)


# ========== АДМИНСКИЕ КОМАНДЫ ==========

@dp.message(Command("admin"))
async def admin_menu(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        await message.answer("⛔ У вас нет прав.")
        return
    
    stats = get_admin_stats()
    
    text = (
        "👑 *Панель администратора*\n\n"
        f"📊 *Статистика:*\n"
        f"• Всего подписчиков: {stats['subscribers']}\n"
        f"• С полным профилем: {stats['full_profiles']}\n"
        f"• Откликов: {stats['responses']}\n"
        f"• Вакансий в обработке: {stats['pending_vacancies']}\n"
        f"• Всего вакансий: {stats['total_vacancies']}"
    )
    
    await message.answer(text, parse_mode="Markdown")


@dp.message(Command("status"))
async def status_cmd(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        await message.answer("⛔ У вас нет прав.")
        return
    
    stats = get_admin_stats()
    
    status_text = (
        "📊 *Статус бота:*\n\n"
        f"✅ Бот активен\n"
        f"🔄 Периодическая проверка: {'запущена' if periodic_task and not periodic_task.done() else 'остановлена'}\n"
        f"⏱️ Интервал проверки: 5 минут\n\n"
        f"👥 *Подписчики:* {stats['subscribers']}\n"
        f"📝 *Всего откликов:* {stats['responses']}"
    )
    
    await message.answer(status_text, parse_mode="Markdown")


@dp.message(Command("check_now"))
async def check_now_cmd(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        await message.answer("⛔ У вас нет прав.")
        return
    
    status_msg = await message.answer("🔍 Начинаю проверку...")
    
    try:
        orders = await get_new_messages()
        
        if not orders:
            await status_msg.edit_text("✅ Новых вакансий не найдено.")
            return
        
        # Отправляем подписчикам
        for order in orders:
            await send_vacancy_to_subscribers(order)
        
        await status_msg.edit_text(
            f"✅ Проверка завершена.\n"
            f"📬 Найдено вакансий: {len(orders)}\n"
            f"👥 Отправлено подписчикам"
        )
        
    except Exception as e:
        logger.error(f"Ошибка в check_now_cmd: {e}")
        await status_msg.edit_text(f"❌ Ошибка при проверке: {str(e)[:100]}")


@dp.message(Command("debug_last"))
async def debug_last_cmd(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        await message.answer("⛔ У вас нет прав.")
        return
    await message.answer(get_last_debug_report())
@dp.message(Command("myid"))
async def show_my_id(message: types.Message):
    await message.answer(
        f"📌 Ваш Telegram ID: `{message.from_user.id}`\n\n"
        f"ID в .env: `{YOUR_USER_ID}`\n\n"
        f"{'✅ Совпадает' if message.from_user.id == YOUR_USER_ID else '❌ НЕ СОВПАДАЕТ! Обновите .env'}"
    )
@dp.message(Command("broadcast"))
async def broadcast_cmd(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        await message.answer("⛔ У вас нет прав.")
        return
    
    # Ожидаем текст рассылки после команды
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "📢 *Отправить рассылку подписчикам*\n\n"
            "Использование: `/broadcast Текст сообщения`\n\n"
            "Пример: `/broadcast Внимание! Новые вакансии скоро появятся!`",
            parse_mode="Markdown"
        )
        return
    
    broadcast_text = parts[1]
    subscribers = get_all_subscribers()
    
    if not subscribers:
        await message.answer("Нет подписчиков для рассылки.")
        return
    
    status_msg = await message.answer(f"📢 Начинаю рассылку {len(subscribers)} подписчикам...")
    
    sent = 0
    for user_id in subscribers:
        try:
            await bot.send_message(
                user_id, 
                f"📢 *Рассылка от администратора:*\n\n{broadcast_text}", 
                parse_mode="Markdown"
            )
            sent += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            if "bot was blocked by the user" in str(e):
                logger.info(f"Пользователь {user_id} заблокировал бота")
    
    await status_msg.edit_text(f"✅ Рассылка отправлена {sent} из {len(subscribers)} подписчиков.")
@dp.message(lambda m: m.text == "📊 Статистика")
async def admin_stats_button(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        await message.answer("⛔ У вас нет прав.")
        return
    
    stats = get_admin_stats()
    text = (
        "📊 *Статистика бота*\n\n"
        f"👥 Подписчиков: {stats['subscribers']}\n"
        f"📝 Полных профилей: {stats['full_profiles']}\n"
        f"💬 Откликов: {stats['responses']}\n"
        f"📦 Вакансий в очереди: {stats['pending_vacancies']}\n"
        f"🏁 Всего вакансий: {stats['total_vacancies']}"
    )
    await message.answer(text, parse_mode="Markdown")


@dp.message(lambda m: m.text == "🔍 Ручная проверка")
async def admin_check_button(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        await message.answer("⛔ У вас нет прав.")
        return
    
    status_msg = await message.answer("🔍 Начинаю проверку...")
    
    try:
        orders = await get_new_messages()
        
        if not orders:
            await status_msg.edit_text("✅ Новых вакансий не найдено.")
            return
        
        for order in orders:
            await send_vacancy_to_subscribers(order)
        
        await status_msg.edit_text(
            f"✅ Проверка завершена.\n"
            f"📬 Найдено вакансий: {len(orders)}\n"
            f"👥 Отправлено подписчикам"
        )
        
    except Exception as e:
        logger.error(f"Ошибка при проверке: {e}")
        await status_msg.edit_text(f"❌ Ошибка: {str(e)[:100]}")


@dp.message(lambda m: m.text == "📋 Список откликов")
async def admin_responses_button(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        await message.answer("⛔ У вас нет прав.")
        return
    
    recent = get_recent_responses(10)
    if not recent:
        await message.answer("📭 Пока нет ни одного отклика.")
        return
    
    text = "📋 *Последние отклики:*\n\n"
    for resp in recent:
        time = resp[0][:16] if resp[0] else "—"
        first_name = resp[3] or "Пользователь"
        vacancy_preview = (resp[1][:50] + "...") if resp[1] and len(resp[1]) > 50 else (resp[1] or "—")
        text += f"• {time} — {first_name}: {vacancy_preview}\n"
    
    await message.answer(text, parse_mode="Markdown")


@dp.message(lambda m: m.text == "📝 Отчёт парсера")
async def admin_debug_button(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        await message.answer("⛔ У вас нет прав.")
        return
    
    await message.answer(get_last_debug_report())


@dp.message(lambda m: m.text == "👥 Список подписчиков")
async def admin_subscribers_button(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        await message.answer("⛔ У вас нет прав.")
        return
    
    subscribers = get_all_subscribers()
    if not subscribers:
        await message.answer("📭 Нет активных подписчиков.")
        return
    
    text = "👥 *Список подписчиков:*\n\n"
    for i, user_id in enumerate(subscribers[:20], 1):
        profile = get_subscriber_profile(user_id)
        if profile:
            name = profile.get('full_name') or profile.get('first_name') or f"ID:{user_id}"
            text += f"{i}. {name}\n"
    
    if len(subscribers) > 20:
        text += f"\n... и ещё {len(subscribers) - 20} подписчиков"
    
    await message.answer(text, parse_mode="Markdown")


@dp.message(lambda m: m.text == "📢 Рассылка")
async def admin_broadcast_button(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        await message.answer("⛔ У вас нет прав.")
        return
    
    await message.answer(
        "📢 *Отправить рассылку*\n\n"
        "Используйте команду:\n"
        "`/broadcast Текст сообщения`\n\n"
        "Пример: `/broadcast Внимание! Новые вакансии скоро появятся!`",
        parse_mode="Markdown"
    )


@dp.message(lambda m: m.text == "❌ Закрыть меню")
async def admin_close_menu(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        await message.answer("⛔ У вас нет прав.")
        return
    
    await message.answer(
        "Меню закрыто. Чтобы открыть снова, напишите /start",
        reply_markup=ReplyKeyboardRemove()
    )


# ========== ПЕРИОДИЧЕСКАЯ ПРОВЕРКА ==========

async def periodic_check():
    """Автоматическая проверка каждые 5 минут"""
    logger.info("🔄 Запущена периодическая проверка")
    
    while True:
        try:
            logger.info("🔍 Выполняю периодическую проверку...")
            orders = await get_new_messages()
            
            if orders:
                logger.info(f"📬 Найдено {len(orders)} новых вакансий")
                for order in orders:
                    await send_vacancy_to_subscribers(order)
            else:
                logger.info("✅ Нет новых вакансий")
                
        except Exception as e:
            logger.error(f"Ошибка в periodic_check: {e}", exc_info=True)
        
        await asyncio.sleep(300)  # 5 минут


# ========== ЗАПУСК И ОСТАНОВКА ==========

async def on_startup():
    """Действия при запуске бота"""
    logger.info("🚀 Запуск бота...")
    
    # Инициализируем БД
    init_db()
    logger.info("📁 База данных инициализирована")
    
    # Запускаем периодическую проверку
    global periodic_task
    periodic_task = asyncio.create_task(periodic_check())
    logger.info("🔄 Периодическая проверка запущена (интервал 5 минут)")
    
    # Логируем ID админа (без отправки сообщения)
    logger.info(f"👑 Администратор ID: {YOUR_USER_ID}")


async def on_shutdown():
    """Действия при остановке бота"""
    logger.info("🛑 Остановка бота...")
    
    # Отменяем периодическую задачу
    global periodic_task
    if periodic_task and not periodic_task.done():
        periodic_task.cancel()
        try:
            await periodic_task
        except asyncio.CancelledError:
            logger.info("✅ Периодическая задача отменена")
    
    # Закрываем сессию бота
    await bot.session.close()
    
    logger.info("👋 Бот остановлен")


async def main():
    """Главная функция запуска"""
    try:
        await on_startup()
        logger.info("📡 Запуск polling...")
        await dp.start_polling(bot)
        
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