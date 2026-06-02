import sqlite3

DB_NAME = "bot_database.db"


def init_db():
    """Инициализация всех таблиц с проверкой наличия новых колонок"""
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    # 1. Таблица подписчиков
    cur.execute("""
        CREATE TABLE IF NOT EXISTS subscribers (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            full_name TEXT,
            age INTEGER,
            phone TEXT,
            photo_file_id TEXT,
            questionnaire TEXT DEFAULT NULL,
            registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_active BOOLEAN DEFAULT 1
        )
    """)
    cur.execute("PRAGMA table_info(subscribers)")
    cols = [c[1] for c in cur.fetchall()]
    if "photo_file_id" not in cols:
        cur.execute("ALTER TABLE subscribers ADD COLUMN photo_file_id TEXT DEFAULT NULL")
    if "questionnaire" not in cols:
        cur.execute("ALTER TABLE subscribers ADD COLUMN questionnaire TEXT DEFAULT NULL")

    # 2. Таблица категорий вакансий
    cur.execute("""
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE,
            name TEXT,
            emoji TEXT
        )
    """)

    # 3. Таблица подписок
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
            address TEXT DEFAULT NULL,
            is_closed BOOLEAN DEFAULT 0,
            found_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_sent BOOLEAN DEFAULT 0
        )
    """)
    cur.execute("PRAGMA table_info(vacancies)")
    cols = [c[1] for c in cur.fetchall()]
    if "address" not in cols:
        cur.execute("ALTER TABLE vacancies ADD COLUMN address TEXT DEFAULT NULL")
    if "is_closed" not in cols:
        cur.execute("ALTER TABLE vacancies ADD COLUMN is_closed BOOLEAN DEFAULT 0")

    # 5. Таблица отправленных вакансий
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
            user_photo_file_id TEXT,
            status TEXT DEFAULT 'pending',
            responded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES subscribers(user_id),
            FOREIGN KEY (vacancy_id) REFERENCES vacancies(id)
        )
    """)
    cur.execute("PRAGMA table_info(responses)")
    cols = [c[1] for c in cur.fetchall()]
    if "user_photo_file_id" not in cols:
        cur.execute("ALTER TABLE responses ADD COLUMN user_photo_file_id TEXT DEFAULT NULL")

    # 7. Таблица для уже обработанных сообщений
    cur.execute("""
        CREATE TABLE IF NOT EXISTS processed_messages (
            message_id TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (message_id, chat_id)
        )
    """)

    # 8. Таблица жалоб
    cur.execute("""
        CREATE TABLE IF NOT EXISTS complaints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            vacancy_id TEXT,
            reason TEXT,
            complaint_text TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            resolved BOOLEAN DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES subscribers(user_id),
            FOREIGN KEY (vacancy_id) REFERENCES vacancies(id)
        )
    """)

    # 9. Таблица вопросов в поддержку
    cur.execute("""
        CREATE TABLE IF NOT EXISTS support_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            message_text TEXT,
            user_username TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            answered BOOLEAN DEFAULT 0,
            admin_response TEXT
        )
    """)

    # 10. Таблица для хранения динамических чатов (TARGET_CHATS)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS target_chats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_link TEXT UNIQUE,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_active BOOLEAN DEFAULT 1
        )
    """)

    # Импорт чатов из config.py, если таблица пуста
    cur.execute("SELECT COUNT(*) FROM target_chats")
    if cur.fetchone()[0] == 0:
        try:
            from config import TARGET_CHATS
            for chat in TARGET_CHATS:
                try:
                    cur.execute("INSERT INTO target_chats (chat_link) VALUES (?)", (chat,))
                except sqlite3.IntegrityError:
                    pass
            print(f"Импортировано {len(TARGET_CHATS)} чатов из config.py в БД")
        except ImportError:
            print("config.py не найден, чаты не импортированы")

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

    # 11. Таблица для хранения последнего обработанного message_id по чату (для real-time)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS last_processed (
            chat_id TEXT PRIMARY KEY,
            last_message_id INTEGER,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    conn.close()


# ========== ФУНКЦИИ ДЛЯ РАБОТЫ С ПОДПИСЧИКАМИ ==========

def add_subscriber(user_id: int, username: str, first_name: str, last_name: str = None):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    # Пытаемся вставить, если такого user_id ещё нет
    cur.execute("""
        INSERT OR IGNORE INTO subscribers (user_id, username, first_name, last_name, is_active)
        VALUES (?, ?, ?, ?, 1)
    """, (user_id, username, first_name, last_name))
    # Если запись уже была, обновляем только username, first_name, last_name (не трогаем остальное)
    cur.execute("""
        UPDATE subscribers 
        SET username = ?, first_name = ?, last_name = ?, is_active = 1
        WHERE user_id = ?
    """, (username, first_name, last_name, user_id))
    conn.commit()
    conn.close()


def update_subscriber_profile(user_id: int, full_name: str, age: int, phone: str, photo_file_id: str = None):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    if photo_file_id:
        cur.execute("""
            UPDATE subscribers 
            SET full_name = ?, age = ?, phone = ?, photo_file_id = ?
            WHERE user_id = ?
        """, (full_name, age, phone, photo_file_id, user_id))
    else:
        cur.execute("""
            UPDATE subscribers 
            SET full_name = ?, age = ?, phone = ?
            WHERE user_id = ?
        """, (full_name, age, phone, user_id))
    conn.commit()
    conn.close()


def update_subscriber_photo(user_id: int, photo_file_id: str):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("UPDATE subscribers SET photo_file_id = ? WHERE user_id = ?", (photo_file_id, user_id))
    conn.commit()
    conn.close()


def update_candidate_questionnaire(user_id: int, questionnaire_text: str):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("UPDATE subscribers SET questionnaire = ? WHERE user_id = ?", (questionnaire_text, user_id))
    conn.commit()
    conn.close()


def get_subscriber_profile(user_id: int) -> dict:
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        SELECT user_id, username, first_name, last_name, full_name, age, phone, photo_file_id, questionnaire, is_active
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
            "photo_file_id": row[7],
            "questionnaire": row[8],
            "is_active": row[9]
        }
    return None


def is_profile_complete(user_id: int) -> bool:
    profile = get_subscriber_profile(user_id)
    if not profile:
        return False
    return all([profile.get("full_name"), profile.get("age"), profile.get("phone")])


# ========== ФУНКЦИИ ДЛЯ КАТЕГОРИЙ ==========

def get_all_categories() -> list:
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT code, name, emoji FROM categories ORDER BY name")
    rows = cur.fetchall()
    conn.close()
    return [{"code": r[0], "name": r[1], "emoji": r[2]} for r in rows]


def get_user_categories(user_id: int) -> list:
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
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("DELETE FROM user_categories WHERE user_id = ?", (user_id,))
    for code in category_codes:
        cur.execute("INSERT INTO user_categories (user_id, category_code) VALUES (?, ?)", (user_id, code))
    conn.commit()
    conn.close()


def get_subscribers_by_category(category_code: str) -> list:
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
                 author_contact: str = None, address: str = None, is_closed: bool = False):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        INSERT OR IGNORE INTO vacancies 
        (id, source_chat, source_chat_title, category_code, message_text, message_link, author_contact, address, is_closed)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (vacancy_id, source_chat, source_chat_title, category_code, message_text, message_link, author_contact, address, is_closed))
    conn.commit()
    conn.close()


def get_unsent_vacancies_by_category(category_code: str) -> list:
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        SELECT id, source_chat_title, message_text, message_link, author_contact, address
        FROM vacancies
        WHERE category_code = ? AND is_sent = 0 AND is_closed = 0
        ORDER BY found_at DESC
    """, (category_code,))
    rows = cur.fetchall()
    conn.close()
    result = []
    for r in rows:
        result.append({
            "id": r[0],
            "source": r[1],
            "text": r[2],
            "link": r[3],
            "contact": r[4],
            "address": r[5]
        })
    return result


def mark_vacancy_sent(vacancy_id: str):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("UPDATE vacancies SET is_sent = 1 WHERE id = ?", (vacancy_id,))
    conn.commit()
    conn.close()


def mark_vacancy_sent_to_user(vacancy_id: str, user_id: int):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO sent_vacancies (user_id, vacancy_id) VALUES (?, ?)", (user_id, vacancy_id))
    conn.commit()
    conn.close()


def has_user_received_vacancy(user_id: int, vacancy_id: str) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM sent_vacancies WHERE user_id = ? AND vacancy_id = ?", (user_id, vacancy_id))
    result = cur.fetchone() is not None
    conn.close()
    return result


def get_users_who_received_vacancy(vacancy_id: str) -> list:
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM sent_vacancies WHERE vacancy_id = ?", (vacancy_id,))
    rows = cur.fetchall()
    conn.close()
    return [row[0] for row in rows]


def mark_vacancy_closed(message_id: str, chat_id: str):
    """Помечает вакансию как закрытую по её ID в чате и возвращает список пользователей, которые её получали"""
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    vacancy_id = f"{chat_id}_{message_id}"
    cur.execute("SELECT user_id FROM sent_vacancies WHERE vacancy_id = ?", (vacancy_id,))
    users = [row[0] for row in cur.fetchall()]
    cur.execute("UPDATE vacancies SET is_closed = 1 WHERE id = ?", (vacancy_id,))
    conn.commit()
    conn.close()
    return users


# ========== ФУНКЦИИ ДЛЯ ОТКЛИКОВ ==========

def add_response(user_id: int, vacancy_id: str, vacancy_text: str = None, vacancy_link: str = None, user_photo_file_id: str = None):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO responses (user_id, vacancy_id, vacancy_text, vacancy_link, user_photo_file_id, status)
        VALUES (?, ?, ?, ?, ?, 'pending')
    """, (user_id, vacancy_id, vacancy_text, vacancy_link, user_photo_file_id))
    conn.commit()
    conn.close()


def is_already_responded(user_id: int, vacancy_id: str) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM responses WHERE user_id = ? AND vacancy_id = ?", (user_id, vacancy_id))
    result = cur.fetchone() is not None
    conn.close()
    return result


def get_response_photo(user_id: int, vacancy_id: str) -> str:
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT user_photo_file_id FROM responses WHERE user_id = ? AND vacancy_id = ?", (user_id, vacancy_id))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


# ========== ФУНКЦИИ ДЛЯ ЖАЛОБ ==========

def add_complaint(user_id: int, vacancy_id: str, reason: str, complaint_text: str = None):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO complaints (user_id, vacancy_id, reason, complaint_text)
        VALUES (?, ?, ?, ?)
    """, (user_id, vacancy_id, reason, complaint_text))
    conn.commit()
    conn.close()


def get_recent_complaints(limit: int = 20):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        SELECT c.id, c.user_id, s.full_name, c.vacancy_id, c.reason, c.complaint_text, c.created_at
        FROM complaints c
        JOIN subscribers s ON c.user_id = s.user_id
        WHERE c.resolved = 0
        ORDER BY c.created_at DESC
        LIMIT ?
    """, (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows


def resolve_complaint(complaint_id: int):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("UPDATE complaints SET resolved = 1 WHERE id = ?", (complaint_id,))
    conn.commit()
    conn.close()


# ========== ФУНКЦИИ ДЛЯ ПОДДЕРЖКИ ==========

def add_support_request(user_id: int, message_text: str, username: str = None):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO support_requests (user_id, message_text, user_username)
        VALUES (?, ?, ?)
    """, (user_id, message_text, username))
    conn.commit()
    conn.close()


def get_unanswered_support_requests(limit: int = 20):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        SELECT id, user_id, user_username, message_text, created_at
        FROM support_requests
        WHERE answered = 0
        ORDER BY created_at ASC
        LIMIT ?
    """, (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows


def mark_support_answered(request_id: int, admin_response: str = None):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("UPDATE support_requests SET answered = 1, admin_response = ? WHERE id = ?", (admin_response, request_id))
    conn.commit()
    conn.close()


# ========== ФУНКЦИИ ДЛЯ ДИНАМИЧЕСКИХ ЧАТОВ ==========

def get_target_chats() -> list:
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT chat_link FROM target_chats WHERE is_active = 1")
    rows = cur.fetchall()
    conn.close()
    return [row[0] for row in rows]


def add_target_chat(chat_link: str) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO target_chats (chat_link) VALUES (?)", (chat_link,))
        conn.commit()
        ok = True
    except sqlite3.IntegrityError:
        ok = False
    conn.close()
    return ok


def remove_target_chat(chat_link: str):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("UPDATE target_chats SET is_active = 0 WHERE chat_link = ?", (chat_link,))
    conn.commit()
    conn.close()


# ========== АДМИНСКАЯ СТАТИСТИКА ==========

def get_all_subscribers() -> list:
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM subscribers WHERE is_active = 1")
    rows = cur.fetchall()
    conn.close()
    return [row[0] for row in rows]


def get_recent_responses(limit: int = 10) -> list:
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


# ========== ФУНКЦИИ ДЛЯ ПАРСЕРА (обработанные сообщения) ==========

def is_message_processed(message_id: str, chat_id: str) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM processed_messages WHERE message_id = ? AND chat_id = ?", (message_id, chat_id))
    result = cur.fetchone() is not None
    conn.close()
    return result


def mark_message_processed(message_id: str, chat_id: str):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO processed_messages (message_id, chat_id) VALUES (?, ?)", (message_id, chat_id))
    conn.commit()
    conn.close()


def get_admin_stats() -> dict:
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM subscribers WHERE is_active = 1")
    total_subscribers = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM subscribers WHERE full_name IS NOT NULL")
    full_profiles = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM responses")
    total_responses = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM vacancies WHERE is_sent = 0 AND is_closed = 0")
    pending_vacancies = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM vacancies")
    total_vacancies = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM complaints WHERE resolved = 0")
    pending_complaints = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM support_requests WHERE answered = 0")
    pending_support = cur.fetchone()[0]
    conn.close()
    return {
        "subscribers": total_subscribers,
        "full_profiles": full_profiles,
        "responses": total_responses,
        "pending_vacancies": pending_vacancies,
        "total_vacancies": total_vacancies,
        "pending_complaints": pending_complaints,
        "pending_support": pending_support
    }

def get_last_processed_id(chat_id: str) -> int:
    """Возвращает последний обработанный message_id для чата"""
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT last_message_id FROM last_processed WHERE chat_id = ?", (chat_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else 0

def update_last_processed_id(chat_id: str, message_id: int):
    """Обновляет последний обработанный message_id для чата"""
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO last_processed (chat_id, last_message_id, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
    """, (chat_id, message_id))
    conn.commit()
    conn.close()