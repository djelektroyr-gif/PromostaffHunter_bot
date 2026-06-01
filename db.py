import sqlite3

DB_NAME = "bot_database.db"


def init_db():
    """Инициализация всех таблиц"""
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    
    # 1. Таблица подписчиков (расширенная с анкетой)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS subscribers (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            full_name TEXT,
            age INTEGER,
            phone TEXT,
            questionnaire TEXT DEFAULT NULL,
            registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_active BOOLEAN DEFAULT 1
        )
    """)
    
    # 2. Таблица категорий вакансий
    cur.execute("""
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE,
            name TEXT,
            emoji TEXT
        )
    """)
    
    # 3. Таблица подписок (какие категории выбрал пользователь)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_categories (
            user_id INTEGER,
            category_code TEXT,
            subscribed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, category_code),
            FOREIGN KEY (user_id) REFERENCES subscribers(user_id),
            FOREIGN KEY (category_code) REFERENCES categories(code)
        )
    """)
    
    # 4. Таблица найденных вакансий
    cur.execute("""
        CREATE TABLE IF NOT EXISTS vacancies (
            id TEXT PRIMARY KEY,
            source_chat TEXT,
            source_chat_title TEXT,
            category_code TEXT,
            message_text TEXT,
            message_link TEXT,
            author_contact TEXT,
            found_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_sent BOOLEAN DEFAULT 0
        )
    """)
    
    # 5. Таблица отправленных вакансий (кому и что отправили)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sent_vacancies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            vacancy_id TEXT,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES subscribers(user_id),
            FOREIGN KEY (vacancy_id) REFERENCES vacancies(id)
        )
    """)
    
    # 6. Таблица откликов
    cur.execute("""
        CREATE TABLE IF NOT EXISTS responses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            vacancy_id TEXT,
            vacancy_text TEXT,
            vacancy_link TEXT,
            status TEXT DEFAULT 'pending',
            responded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES subscribers(user_id),
            FOREIGN KEY (vacancy_id) REFERENCES vacancies(id)
        )
    """)
    
    # 7. Таблица для уже обработанных сообщений (из парсера)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS processed_messages (
            message_id TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (message_id, chat_id)
        )
    """)
    
    # Заполняем категории, если их нет
    cur.execute("SELECT COUNT(*) FROM categories")
    if cur.fetchone()[0] == 0:
        categories = [
            ("promoter", "Промоутер", "📢"),
            ("hostess", "Хостес", "👩‍💼"),
            ("wardrobe", "Гардеробщик", "🧥"),
            ("animator", "Аниматор", "🎭"),
            ("helper", "Хелпер", "👷"),
            ("loader", "Грузчик", "📦"),
            ("waiter", "Официант", "🍽️"),
            ("driver", "Водитель", "🚐"),
            ("security", "Охранник", "🛡️"),
            ("parking", "Парковщик", "🚗"),
            ("supervisor", "Супервайзер", "👨‍💼"),
        ]
        cur.executemany(
            "INSERT INTO categories (code, name, emoji) VALUES (?, ?, ?)",
            categories
        )
    
    conn.commit()
    conn.close()


# ========== ФУНКЦИИ ДЛЯ РАБОТЫ С ПОДПИСЧИКАМИ ==========

def add_subscriber(user_id: int, username: str, first_name: str, last_name: str = None):
    """Добавляет или обновляет подписчика (начальная регистрация)"""
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO subscribers (user_id, username, first_name, last_name, is_active)
        VALUES (?, ?, ?, ?, 1)
    """, (user_id, username, first_name, last_name))
    conn.commit()
    conn.close()


def update_subscriber_profile(user_id: int, full_name: str, age: int, phone: str):
    """Обновляет профиль подписчика (ФИО, возраст, телефон)"""
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        UPDATE subscribers 
        SET full_name = ?, age = ?, phone = ?
        WHERE user_id = ?
    """, (full_name, age, phone, user_id))
    conn.commit()
    conn.close()


def update_candidate_questionnaire(user_id: int, questionnaire_text: str):
    """Сохраняет готовую анкету кандидата"""
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        UPDATE subscribers 
        SET questionnaire = ?
        WHERE user_id = ?
    """, (questionnaire_text, user_id))
    conn.commit()
    conn.close()


def get_subscriber_profile(user_id: int) -> dict:
    """Получает профиль подписчика"""
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        SELECT user_id, username, first_name, last_name, full_name, age, phone, questionnaire, is_active
        FROM subscribers WHERE user_id = ?
    """, (user_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        return {
            "user_id": row[0],
            "username": row[1],
            "first_name": row[2],
            "last_name": row[3],
            "full_name": row[4],
            "age": row[5],
            "phone": row[6],
            "questionnaire": row[7],
            "is_active": row[8]
        }
    return None


def is_profile_complete(user_id: int) -> bool:
    """Проверяет, заполнил ли пользователь все данные"""
    profile = get_subscriber_profile(user_id)
    if not profile:
        return False
    return all([profile.get("full_name"), profile.get("age"), profile.get("phone")])


# ========== ФУНКЦИИ ДЛЯ КАТЕГОРИЙ ==========

def get_all_categories() -> list:
    """Возвращает все категории вакансий"""
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT code, name, emoji FROM categories ORDER BY name")
    rows = cur.fetchall()
    conn.close()
    return [{"code": r[0], "name": r[1], "emoji": r[2]} for r in rows]


def get_user_categories(user_id: int) -> list:
    """Возвращает категории, на которые подписан пользователь"""
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        SELECT c.code, c.name, c.emoji 
        FROM user_categories uc
        JOIN categories c ON uc.category_code = c.code
        WHERE uc.user_id = ?
    """, (user_id,))
    rows = cur.fetchall()
    conn.close()
    return [{"code": r[0], "name": r[1], "emoji": r[2]} for r in rows]


def set_user_categories(user_id: int, category_codes: list):
    """Устанавливает категории для пользователя (сначала удаляет старые)"""
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    # Удаляем старые
    cur.execute("DELETE FROM user_categories WHERE user_id = ?", (user_id,))
    # Добавляем новые
    for code in category_codes:
        cur.execute(
            "INSERT INTO user_categories (user_id, category_code) VALUES (?, ?)",
            (user_id, code)
        )
    conn.commit()
    conn.close()


def get_subscribers_by_category(category_code: str) -> list:
    """Возвращает список подписчиков на конкретную категорию"""
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        SELECT s.user_id, s.full_name, s.phone, s.username
        FROM subscribers s
        JOIN user_categories uc ON s.user_id = uc.user_id
        WHERE uc.category_code = ? AND s.is_active = 1 AND s.full_name IS NOT NULL
    """, (category_code,))
    rows = cur.fetchall()
    conn.close()
    return [{"user_id": r[0], "full_name": r[1], "phone": r[2], "username": r[3]} for r in rows]


# ========== ФУНКЦИИ ДЛЯ ВАКАНСИЙ ==========

def save_vacancy(vacancy_id: str, source_chat: str, source_chat_title: str, 
                 category_code: str, message_text: str, message_link: str, 
                 author_contact: str = None):
    """Сохраняет найденную вакансию"""
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        INSERT OR IGNORE INTO vacancies 
        (id, source_chat, source_chat_title, category_code, message_text, message_link, author_contact)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (vacancy_id, source_chat, source_chat_title, category_code, message_text, message_link, author_contact))
    conn.commit()
    conn.close()


def get_unsent_vacancies_by_category(category_code: str) -> list:
    """Получает неотправленные вакансии по категории"""
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        SELECT id, source_chat_title, message_text, message_link, author_contact
        FROM vacancies
        WHERE category_code = ? AND is_sent = 0
        ORDER BY found_at DESC
    """, (category_code,))
    rows = cur.fetchall()
    conn.close()
    return [{
        "id": r[0],
        "source": r[1],
        "text": r[2],
        "link": r[3],
        "contact": r[4]
    } for r in rows]


def mark_vacancy_sent(vacancy_id: str):
    """Отмечает вакансию как отправленную"""
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("UPDATE vacancies SET is_sent = 1 WHERE id = ?", (vacancy_id,))
    conn.commit()
    conn.close()


def mark_vacancy_sent_to_user(vacancy_id: str, user_id: int):
    """Отмечает, что вакансия была отправлена конкретному пользователю"""
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        INSERT OR IGNORE INTO sent_vacancies (user_id, vacancy_id)
        VALUES (?, ?)
    """, (user_id, vacancy_id))
    conn.commit()
    conn.close()


def has_user_received_vacancy(user_id: int, vacancy_id: str) -> bool:
    """Проверяет, получал ли пользователь эту вакансию"""
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        SELECT 1 FROM sent_vacancies WHERE user_id = ? AND vacancy_id = ?
    """, (user_id, vacancy_id))
    result = cur.fetchone() is not None
    conn.close()
    return result


# ========== ФУНКЦИИ ДЛЯ ОТКЛИКОВ ==========

def add_response(user_id: int, vacancy_id: str, vacancy_text: str = None, vacancy_link: str = None):
    """Добавляет отклик на вакансию с сохранением текста и ссылки"""
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO responses (user_id, vacancy_id, vacancy_text, vacancy_link, status)
        VALUES (?, ?, ?, ?, 'pending')
    """, (user_id, vacancy_id, vacancy_text, vacancy_link))
    conn.commit()
    conn.close()


def is_already_responded(user_id: int, vacancy_id: str) -> bool:
    """Проверяет, откликался ли уже пользователь на эту вакансию"""
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        SELECT 1 FROM responses WHERE user_id = ? AND vacancy_id = ?
    """, (user_id, vacancy_id))
    result = cur.fetchone() is not None
    conn.close()
    return result


# ========== ФУНКЦИИ ДЛЯ ПАРСЕРА (обработанные сообщения) ==========

def is_message_processed(message_id: str, chat_id: str) -> bool:
    """Проверяет, обработано ли уже сообщение"""
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        SELECT 1 FROM processed_messages WHERE message_id = ? AND chat_id = ?
    """, (message_id, chat_id))
    result = cur.fetchone() is not None
    conn.close()
    return result


def mark_message_processed(message_id: str, chat_id: str):
    """Отмечает сообщение как обработанное"""
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        INSERT OR IGNORE INTO processed_messages (message_id, chat_id)
        VALUES (?, ?)
    """, (message_id, chat_id))
    conn.commit()
    conn.close()


# ========== АДМИНСКАЯ СТАТИСТИКА ==========

def get_all_subscribers() -> list:
    """Возвращает список всех подписчиков (user_id)"""
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM subscribers WHERE is_active = 1")
    rows = cur.fetchall()
    conn.close()
    return [row[0] for row in rows]


def get_recent_responses(limit: int = 10) -> list:
    """Возвращает последние отклики"""
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        SELECT responded_at, vacancy_text, username, first_name 
        FROM responses 
        ORDER BY responded_at DESC 
        LIMIT ?
    """, (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows


def get_subscriber_by_id(user_id: int) -> dict:
    """Получает подписчика по ID"""
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        SELECT user_id, username, first_name, last_name, full_name, age, phone, is_active
        FROM subscribers WHERE user_id = ?
    """, (user_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        return {
            "user_id": row[0],
            "username": row[1],
            "first_name": row[2],
            "last_name": row[3],
            "full_name": row[4],
            "age": row[5],
            "phone": row[6],
            "is_active": row[7]
        }
    return None


def get_admin_stats() -> dict:
    """Собирает статистику для администратора"""
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    
    cur.execute("SELECT COUNT(*) FROM subscribers WHERE is_active = 1")
    total_subscribers = cur.fetchone()[0]
    
    cur.execute("SELECT COUNT(*) FROM subscribers WHERE full_name IS NOT NULL")
    full_profiles = cur.fetchone()[0]
    
    cur.execute("SELECT COUNT(*) FROM responses")
    total_responses = cur.fetchone()[0]
    
    cur.execute("SELECT COUNT(*) FROM vacancies WHERE is_sent = 0")
    pending_vacancies = cur.fetchone()[0]
    
    cur.execute("SELECT COUNT(*) FROM vacancies")
    total_vacancies = cur.fetchone()[0]
    
    conn.close()
    
    return {
        "subscribers": total_subscribers,
        "full_profiles": full_profiles,
        "responses": total_responses,
        "pending_vacancies": pending_vacancies,
        "total_vacancies": total_vacancies
    }