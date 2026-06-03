import logging
import os
import shutil

from config import get_database_path
from db_backend import (
    IS_POSTGRES,
    IntegrityError,
    add_column_if_missing,
    bool_default_false,
    bool_default_true,
    db_conn,
    db_info_label,
    execute,
    fetchall,
    fetchone,
    fetchval,
    now_minus_days,
    now_plus_days,
    paid_until_active,
    paid_until_expired,
    q,
    serial_pk,
    table_exists,
)

logger = logging.getLogger(__name__)

DB_NAME = db_info_label() if IS_POSTGRES else get_database_path()


def _migrate_legacy_database_if_needed() -> None:
    """Копирует bot_database.db из /app в shared, если после деплоя shared пустой (только SQLite)."""
    global DB_NAME
    if IS_POSTGRES:
        return
    target = get_database_path()
    if os.path.isfile(target) and os.path.getsize(target) > 512:
        DB_NAME = target
        return
    for legacy in ("bot_database.db", "/app/bot_database.db"):
        if os.path.isfile(legacy) and os.path.getsize(legacy) > 512:
            os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
            shutil.copy2(legacy, target)
            logger.info(f"SQLite перенесена: {legacy} → {target}")
            DB_NAME = target
            return
    DB_NAME = target


def init_db():
    if not IS_POSTGRES:
        _migrate_legacy_database_if_needed()

    with db_conn() as conn:
        cur = conn.cursor()

        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS subscribers (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                full_name TEXT,
                age INTEGER,
                phone TEXT,
                photo_file_id TEXT DEFAULT NULL,
                questionnaire TEXT DEFAULT NULL,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_active BOOLEAN {bool_default_true()}
            )
        """)
        add_column_if_missing(
            "subscribers", "photo_file_id",
            "ALTER TABLE subscribers ADD COLUMN photo_file_id TEXT DEFAULT NULL",
        )
        add_column_if_missing(
            "subscribers", "questionnaire",
            "ALTER TABLE subscribers ADD COLUMN questionnaire TEXT DEFAULT NULL",
        )
        add_column_if_missing(
            "subscribers", "plan",
            "ALTER TABLE subscribers ADD COLUMN plan TEXT DEFAULT 'free'",
        )
        add_column_if_missing(
            "subscribers", "paid_until",
            "ALTER TABLE subscribers ADD COLUMN paid_until TIMESTAMP DEFAULT NULL",
        )
        add_column_if_missing(
            "subscribers", "metro_zones",
            "ALTER TABLE subscribers ADD COLUMN metro_zones TEXT DEFAULT NULL",
        )
        add_column_if_missing(
            "subscribers", "trial_used",
            "ALTER TABLE subscribers ADD COLUMN trial_used BOOLEAN DEFAULT 0",
            "ALTER TABLE subscribers ADD COLUMN trial_used BOOLEAN DEFAULT FALSE",
        )

        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS categories (
                id {serial_pk()},
                code TEXT UNIQUE,
                name TEXT,
                emoji TEXT
            )
        """)

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

        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS vacancies (
                id TEXT PRIMARY KEY,
                source_chat TEXT,
                source_chat_title TEXT,
                category_code TEXT,
                message_text TEXT,
                message_link TEXT,
                author_contact TEXT,
                address TEXT DEFAULT NULL,
                is_closed BOOLEAN {bool_default_false()},
                found_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_sent BOOLEAN {bool_default_false()}
            )
        """)
        add_column_if_missing(
            "vacancies", "address",
            "ALTER TABLE vacancies ADD COLUMN address TEXT DEFAULT NULL",
        )
        add_column_if_missing(
            "vacancies", "is_closed",
            f"ALTER TABLE vacancies ADD COLUMN is_closed BOOLEAN {bool_default_false()}",
            f"ALTER TABLE vacancies ADD COLUMN is_closed BOOLEAN {bool_default_false()}",
        )
        add_column_if_missing(
            "vacancies", "dedupe_key",
            "ALTER TABLE vacancies ADD COLUMN dedupe_key TEXT DEFAULT NULL",
        )
        add_column_if_missing(
            "vacancies", "published_at",
            "ALTER TABLE vacancies ADD COLUMN published_at TEXT DEFAULT NULL",
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_vacancies_dedupe_key ON vacancies(dedupe_key)")

        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS sent_vacancies (
                id {serial_pk()},
                user_id INTEGER,
                vacancy_id TEXT,
                sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES subscribers(user_id),
                FOREIGN KEY (vacancy_id) REFERENCES vacancies(id)
            )
        """)
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_sent_vacancies_user_vac "
            "ON sent_vacancies(user_id, vacancy_id)"
        )

        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS responses (
                id {serial_pk()},
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
        add_column_if_missing(
            "responses", "user_photo_file_id",
            "ALTER TABLE responses ADD COLUMN user_photo_file_id TEXT DEFAULT NULL",
        )

        cur.execute("""
            CREATE TABLE IF NOT EXISTS processed_messages (
                message_id TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (message_id, chat_id)
            )
        """)

        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS complaints (
                id {serial_pk()},
                user_id INTEGER,
                vacancy_id TEXT,
                reason TEXT,
                complaint_text TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                resolved BOOLEAN {bool_default_false()},
                FOREIGN KEY (user_id) REFERENCES subscribers(user_id),
                FOREIGN KEY (vacancy_id) REFERENCES vacancies(id)
            )
        """)

        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS support_requests (
                id {serial_pk()},
                user_id INTEGER,
                message_text TEXT,
                user_username TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                answered BOOLEAN {bool_default_false()},
                admin_response TEXT
            )
        """)

        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS target_chats (
                id {serial_pk()},
                chat_link TEXT UNIQUE,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_active BOOLEAN {bool_default_true()}
            )
        """)

        cur.execute("SELECT COUNT(*) FROM target_chats")
        if cur.fetchone()[0] == 0:
            try:
                from config import TARGET_CHATS
                for chat in TARGET_CHATS:
                    cur.execute(
                        q("""
                            INSERT INTO target_chats (chat_link) VALUES (?)
                            ON CONFLICT(chat_link) DO NOTHING
                        """),
                        (chat,),
                    )
                print(f"Импортировано {len(TARGET_CHATS)} чатов из config.py в БД")
            except ImportError:
                print("config.py не найден, чаты не импортированы")

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
                q("INSERT INTO categories (code, name, emoji) VALUES (?, ?, ?)"),
                categories,
            )

        cur.execute("""
            CREATE TABLE IF NOT EXISTS last_processed (
                chat_id TEXT PRIMARY KEY,
                last_message_id INTEGER,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

    logger.info(db_info_label())


def add_subscriber(user_id: int, username: str, first_name: str, last_name: str = None):
    execute("""
        INSERT INTO subscribers (user_id, username, first_name, last_name, is_active)
        VALUES (?, ?, ?, ?, 1)
        ON CONFLICT(user_id) DO UPDATE SET
            username = excluded.username,
            first_name = excluded.first_name,
            last_name = excluded.last_name,
            is_active = 1
    """, (user_id, username, first_name, last_name))


def update_subscriber_profile(user_id: int, full_name: str, age: int, phone: str, photo_file_id: str = None):
    if photo_file_id:
        execute("""
            UPDATE subscribers
            SET full_name = ?, age = ?, phone = ?, photo_file_id = ?
            WHERE user_id = ?
        """, (full_name, age, phone, photo_file_id, user_id))
    else:
        execute("""
            UPDATE subscribers
            SET full_name = ?, age = ?, phone = ?
            WHERE user_id = ?
        """, (full_name, age, phone, user_id))


def update_subscriber_photo(user_id: int, photo_file_id: str):
    execute("UPDATE subscribers SET photo_file_id = ? WHERE user_id = ?", (photo_file_id, user_id))


def update_candidate_questionnaire(user_id: int, questionnaire_text: str):
    execute("UPDATE subscribers SET questionnaire = ? WHERE user_id = ?", (questionnaire_text, user_id))


def get_subscriber_profile(user_id: int) -> dict:
    row = fetchone("""
        SELECT user_id, username, first_name, last_name, full_name, age, phone,
               photo_file_id, questionnaire, is_active, plan, paid_until, metro_zones, trial_used
        FROM subscribers WHERE user_id = ?
    """, (user_id,))
    if row:
        return {
            "user_id": row[0], "username": row[1], "first_name": row[2], "last_name": row[3],
            "full_name": row[4], "age": row[5], "phone": row[6], "photo_file_id": row[7],
            "questionnaire": row[8], "is_active": row[9], "plan": row[10] or "free",
            "paid_until": row[11], "metro_zones": row[12], "trial_used": bool(row[13]),
        }
    return None


def is_user_premium(user_id: int) -> bool:
    return fetchone(
        f"""
        SELECT 1 FROM subscribers
        WHERE user_id = ? AND plan = 'premium'
          AND {paid_until_active()}
        """,
        (user_id,),
    ) is not None


def set_user_plan(user_id: int, plan: str = "premium", days: int = 30):
    if plan == "free":
        execute(
            "UPDATE subscribers SET plan = 'free', paid_until = NULL WHERE user_id = ?",
            (user_id,),
        )
    else:
        execute(
            f"""
            UPDATE subscribers SET plan = 'premium',
            paid_until = {now_plus_days(days)}
            WHERE user_id = ?
            """,
            (user_id,),
        )


def count_premium_subscribers() -> int:
    return fetchval(
        f"""
        SELECT COUNT(*) FROM subscribers
        WHERE is_active = 1 AND plan = 'premium'
          AND {paid_until_active()}
        """,
        default=0,
    )


def set_user_metro_zones(user_id: int, metro_zones: str | None):
    execute("UPDATE subscribers SET metro_zones = ? WHERE user_id = ?", (metro_zones, user_id))


def grant_trial_if_eligible(user_id: int, trial_days: int) -> bool:
    """Выдаёт пробный Premium один раз на user_id. Возвращает True если выдан."""
    if trial_days <= 0:
        return False
    row = fetchone("SELECT trial_used FROM subscribers WHERE user_id = ?", (user_id,))
    if not row or row[0]:
        return False
    execute(
        f"""
        UPDATE subscribers
        SET plan = 'premium', paid_until = {now_plus_days(trial_days)}, trial_used = 1
        WHERE user_id = ?
        """,
        (user_id,),
    )
    return True


def downgrade_expired_premium(user_id: int) -> str | None:
    """Сбрасывает истёкший Premium на free. Возвращает текст уведомления или None."""
    row = fetchone(
        f"""
        SELECT plan, paid_until FROM subscribers
        WHERE user_id = ? AND plan = 'premium' AND paid_until IS NOT NULL
          AND {paid_until_expired()}
        """,
        (user_id,),
    )
    if not row:
        return None
    execute("UPDATE subscribers SET plan = 'free' WHERE user_id = ?", (user_id,))
    return (
        "⏳ *Premium закончился.*\n\n"
        "Моментальные push отключены — новые вакансии в ленте «🔍 Посмотреть новые вакансии».\n"
        "Оформить снова: 💎 Подписка"
    )


def is_profile_complete(user_id: int) -> bool:
    profile = get_subscriber_profile(user_id)
    if not profile:
        return False
    return all([profile.get("full_name"), profile.get("age"), profile.get("phone")])


# ========== КАТЕГОРИИ ==========
def get_all_categories() -> list:
    rows = fetchall("SELECT code, name, emoji FROM categories ORDER BY name")
    return [{"code": r[0], "name": r[1], "emoji": r[2]} for r in rows]


def get_user_categories(user_id: int) -> list:
    rows = fetchall("""
        SELECT c.code, c.name, c.emoji
        FROM user_categories uc
        JOIN categories c ON uc.category_code = c.code
        WHERE uc.user_id = ?
    """, (user_id,))
    return [{"code": r[0], "name": r[1], "emoji": r[2]} for r in rows]


def set_user_categories(user_id: int, category_codes: list):
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(q("DELETE FROM user_categories WHERE user_id = ?"), (user_id,))
        for code in category_codes:
            cur.execute(
                q("INSERT INTO user_categories (user_id, category_code) VALUES (?, ?)"),
                (user_id, code),
            )


def get_subscribers_by_category(category_code: str) -> list:
    rows = fetchall("""
        SELECT s.user_id, s.full_name, s.phone, s.username, s.metro_zones
        FROM subscribers s
        JOIN user_categories uc ON s.user_id = uc.user_id
        WHERE uc.category_code = ? AND s.is_active = 1
    """, (category_code,))
    return [
        {"user_id": r[0], "full_name": r[1], "phone": r[2], "username": r[3], "metro_zones": r[4]}
        for r in rows
    ]


# ========== ВАКАНСИИ ==========
def save_vacancy(vacancy_id: str, source_chat: str, source_chat_title: str,
                 category_code: str, message_text: str, message_link: str,
                 author_contact: str = None, address: str = None, is_closed: bool = False,
                 dedupe_key: str = None, published_at: str = None):
    execute("""
        INSERT INTO vacancies
        (id, source_chat, source_chat_title, category_code, message_text, message_link,
         author_contact, address, is_closed, dedupe_key, published_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            source_chat_title = excluded.source_chat_title,
            category_code = excluded.category_code,
            message_text = excluded.message_text,
            message_link = excluded.message_link,
            author_contact = COALESCE(excluded.author_contact, author_contact),
            address = COALESCE(excluded.address, address),
            dedupe_key = COALESCE(excluded.dedupe_key, dedupe_key),
            published_at = COALESCE(excluded.published_at, published_at)
    """, (vacancy_id, source_chat, source_chat_title, category_code, message_text, message_link,
          author_contact, address, is_closed, dedupe_key, published_at))


def get_vacancy_row(vacancy_id: str):
    return fetchone(
        "SELECT message_text, message_link, source_chat_title, author_contact, address FROM vacancies WHERE id = ?",
        (vacancy_id,),
    )


def migrate_legacy_vacancy_ids() -> int:
    """Переносит id вида {chat_id}_{message_id} на canonical md5 (16 символов)."""
    from parser import make_vacancy_id

    migrated = 0
    ref_tables = ("sent_vacancies", "responses", "complaints")

    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, source_chat, message_link, dedupe_key FROM vacancies WHERE length(id) > 16")
        rows = cur.fetchall()

        for old_id, source_chat, message_link, dedupe_key in rows:
            message_id = None
            if message_link:
                tail = message_link.rstrip("/").split("/")[-1]
                if tail.isdigit():
                    message_id = tail
            if not message_id and "_" in old_id:
                message_id = old_id.rsplit("_", 1)[-1]
            if not message_id or not source_chat:
                continue
            new_id = make_vacancy_id(source_chat, message_id, dedupe_key)
            if new_id == old_id:
                continue

            cur.execute(q("SELECT 1 FROM vacancies WHERE id = ?"), (new_id,))
            if cur.fetchone():
                for table in ref_tables:
                    try:
                        cur.execute(
                            q(f"UPDATE {table} SET vacancy_id = ? WHERE vacancy_id = ?"),
                            (new_id, old_id),
                        )
                    except IntegrityError:
                        pass
                    cur.execute(q(f"DELETE FROM {table} WHERE vacancy_id = ?"), (old_id,))
                cur.execute(q("DELETE FROM vacancies WHERE id = ?"), (old_id,))
            else:
                for table in ref_tables:
                    cur.execute(
                        q(f"UPDATE {table} SET vacancy_id = ? WHERE vacancy_id = ?"),
                        (new_id, old_id),
                    )
                cur.execute(q("UPDATE vacancies SET id = ? WHERE id = ?"), (new_id, old_id))
            migrated += 1

    return migrated


def has_recent_duplicate_vacancy(dedupe_key: str, max_age_days: int = 1) -> bool:
    if not dedupe_key:
        return False
    return fetchone(
        f"""
        SELECT 1
        FROM vacancies
        WHERE dedupe_key = ?
          AND is_closed = 0
          AND found_at >= {now_minus_days(max_age_days)}
        LIMIT 1
        """,
        (dedupe_key,),
    ) is not None


def get_recent_open_vacancies_for_dedupe(max_age_days: int = 1, limit: int = 200) -> list:
    rows = fetchall(
        f"""
        SELECT id, message_text, author_contact, dedupe_key
        FROM vacancies
        WHERE is_closed = 0
          AND found_at >= {now_minus_days(max_age_days)}
        ORDER BY found_at DESC
        LIMIT ?
        """,
        (limit,),
    )
    return [
        {
            "id": row[0],
            "message_text": row[1] or "",
            "author_contact": row[2],
            "dedupe_key": row[3],
        }
        for row in rows
    ]


def get_unsent_vacancies_by_category(category_code: str) -> list:
    rows = fetchall("""
        SELECT id, source_chat_title, message_text, message_link, author_contact, address, found_at, published_at
        FROM vacancies
        WHERE category_code = ? AND is_sent = 0 AND is_closed = 0
        ORDER BY found_at DESC
    """, (category_code,))
    result = []
    for r in rows:
        result.append({
            "id": r[0], "source": r[1], "text": r[2], "link": r[3], "contact": r[4], "address": r[5],
            "found_at": r[6], "published_at": r[7]
        })
    return result


def mark_vacancy_sent(vacancy_id: str):
    execute("UPDATE vacancies SET is_sent = 1 WHERE id = ?", (vacancy_id,))


def mark_vacancy_sent_to_user(vacancy_id: str, user_id: int):
    execute("""
        INSERT INTO sent_vacancies (user_id, vacancy_id) VALUES (?, ?)
        ON CONFLICT(user_id, vacancy_id) DO NOTHING
    """, (user_id, vacancy_id))


def has_user_received_vacancy(user_id: int, vacancy_id: str) -> bool:
    return fetchone(
        "SELECT 1 FROM sent_vacancies WHERE user_id = ? AND vacancy_id = ?",
        (user_id, vacancy_id),
    ) is not None


def get_users_who_received_vacancy(vacancy_id: str) -> list:
    rows = fetchall("SELECT user_id FROM sent_vacancies WHERE vacancy_id = ?", (vacancy_id,))
    return [row[0] for row in rows]


def mark_vacancy_closed(message_id: str, chat_id: str):
    """Помечает вакансию закрытой и возвращает (canonical_id, user_ids для уведомления)."""
    legacy_id = f"{chat_id}_{message_id}"
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            q("""
                SELECT id FROM vacancies
                WHERE id = ? OR (source_chat = ? AND message_link LIKE ?)
                ORDER BY CASE WHEN id = ? THEN 0 ELSE 1 END
                LIMIT 1
            """),
            (legacy_id, chat_id, f"%/{message_id}", legacy_id),
        )
        row = cur.fetchone()
        if not row:
            return None, []
        vacancy_id = row[0]
        cur.execute(q("SELECT user_id FROM sent_vacancies WHERE vacancy_id = ?"), (vacancy_id,))
        users = [r[0] for r in cur.fetchall()]
        cur.execute(
            q("UPDATE vacancies SET is_closed = 1 WHERE id = ? OR id = ?"),
            (vacancy_id, legacy_id),
        )
    return vacancy_id, users


# ========== ОТКЛИКИ ==========
def add_response(user_id: int, vacancy_id: str, vacancy_text: str = None, vacancy_link: str = None, user_photo_file_id: str = None):
    execute("""
        INSERT INTO responses (user_id, vacancy_id, vacancy_text, vacancy_link, user_photo_file_id, status)
        VALUES (?, ?, ?, ?, ?, 'pending')
    """, (user_id, vacancy_id, vacancy_text, vacancy_link, user_photo_file_id))


def is_already_responded(user_id: int, vacancy_id: str) -> bool:
    return fetchone(
        "SELECT 1 FROM responses WHERE user_id = ? AND vacancy_id = ?",
        (user_id, vacancy_id),
    ) is not None


def get_response_photo(user_id: int, vacancy_id: str) -> str:
    row = fetchone(
        "SELECT user_photo_file_id FROM responses WHERE user_id = ? AND vacancy_id = ?",
        (user_id, vacancy_id),
    )
    return row[0] if row else None


# ========== ЖАЛОБЫ ==========
def add_complaint(user_id: int, vacancy_id: str, reason: str, complaint_text: str = None):
    execute("""
        INSERT INTO complaints (user_id, vacancy_id, reason, complaint_text)
        VALUES (?, ?, ?, ?)
    """, (user_id, vacancy_id, reason, complaint_text))


def get_recent_complaints(limit: int = 20):
    return fetchall("""
        SELECT c.id, c.user_id, s.full_name, c.vacancy_id, c.reason, c.complaint_text, c.created_at
        FROM complaints c
        JOIN subscribers s ON c.user_id = s.user_id
        WHERE c.resolved = 0
        ORDER BY c.created_at DESC
        LIMIT ?
    """, (limit,))


def resolve_complaint(complaint_id: int):
    execute("UPDATE complaints SET resolved = 1 WHERE id = ?", (complaint_id,))


# ========== ПОДДЕРЖКА ==========
def add_support_request(user_id: int, message_text: str, username: str = None):
    execute("""
        INSERT INTO support_requests (user_id, message_text, user_username)
        VALUES (?, ?, ?)
    """, (user_id, message_text, username))


def get_unanswered_support_requests(limit: int = 20):
    return fetchall("""
        SELECT id, user_id, user_username, message_text, created_at
        FROM support_requests
        WHERE answered = 0
        ORDER BY created_at ASC
        LIMIT ?
    """, (limit,))


def mark_support_answered(request_id: int, admin_response: str = None):
    execute(
        "UPDATE support_requests SET answered = 1, admin_response = ? WHERE id = ?",
        (admin_response, request_id),
    )


# ========== ДИНАМИЧЕСКИЕ ЧАТЫ ==========
def get_target_chats() -> list:
    if not table_exists("target_chats"):
        return []
    rows = fetchall("SELECT chat_link FROM target_chats WHERE is_active = 1")
    return [row[0] for row in rows]


def list_target_chats() -> list:
    """Все чаты парсинга (включая отключённые) для админки."""
    if not table_exists("target_chats"):
        return []
    rows = fetchall(
        "SELECT chat_link, is_active, added_at FROM target_chats ORDER BY is_active DESC, added_at"
    )
    return [{"chat_link": r[0], "is_active": bool(r[1]), "added_at": r[2]} for r in rows]


def add_target_chat(chat_link: str) -> bool:
    if not table_exists("target_chats"):
        return False
    try:
        execute("INSERT INTO target_chats (chat_link) VALUES (?)", (chat_link,))
        return True
    except IntegrityError:
        return False


def remove_target_chat(chat_link: str):
    if not table_exists("target_chats"):
        return
    execute("UPDATE target_chats SET is_active = 0 WHERE chat_link = ?", (chat_link,))


# ========== АДМИНСКАЯ СТАТИСТИКА ==========
def get_all_subscribers() -> list:
    rows = fetchall("SELECT user_id FROM subscribers WHERE is_active = 1")
    return [row[0] for row in rows]


def get_recent_responses(limit: int = 10) -> list:
    return fetchall("""
        SELECT r.responded_at, r.vacancy_text, s.username, s.first_name
        FROM responses r
        JOIN subscribers s ON r.user_id = s.user_id
        ORDER BY r.responded_at DESC
        LIMIT ?
    """, (limit,))


def get_subscriber_by_id(user_id: int) -> dict:
    row = fetchone("""
        SELECT user_id, username, first_name, last_name, full_name, age, phone, is_active
        FROM subscribers WHERE user_id = ?
    """, (user_id,))
    if row:
        return {
            "user_id": row[0], "username": row[1], "first_name": row[2], "last_name": row[3],
            "full_name": row[4], "age": row[5], "phone": row[6], "is_active": row[7]
        }
    return None


def get_admin_stats() -> dict:
    with db_conn(commit=False) as conn:
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
    return {
        "subscribers": total_subscribers, "full_profiles": full_profiles,
        "responses": total_responses, "pending_vacancies": pending_vacancies,
        "total_vacancies": total_vacancies, "pending_complaints": pending_complaints,
        "pending_support": pending_support
    }


def get_subscriber_cards(limit: int = 20, offset: int = 0) -> list:
    with db_conn(commit=False) as conn:
        cur = conn.cursor()
        cur.execute(
            q("""
                SELECT user_id, username, first_name, last_name, full_name, age, phone, registered_at, is_active
                FROM subscribers
                ORDER BY registered_at DESC
                LIMIT ? OFFSET ?
            """),
            (limit, offset),
        )
        rows = cur.fetchall()
        cards = []
        for row in rows:
            user_id = row[0]
            cur.execute(
                q("""
                    SELECT c.name, c.emoji
                    FROM user_categories uc
                    JOIN categories c ON c.code = uc.category_code
                    WHERE uc.user_id = ?
                    ORDER BY c.name
                """),
                (user_id,),
            )
            cats = [f"{c[1]} {c[0]}" for c in cur.fetchall()]
            cards.append({
                "user_id": user_id,
                "username": row[1],
                "first_name": row[2],
                "last_name": row[3],
                "full_name": row[4],
                "age": row[5],
                "phone": row[6],
                "registered_at": row[7],
                "is_active": bool(row[8]),
                "categories": cats,
            })
    return cards


def get_user_category_mapping() -> list:
    rows = fetchall(
        """
        SELECT c.code, c.name, c.emoji, COUNT(uc.user_id) as subscribers_count
        FROM categories c
        LEFT JOIN user_categories uc ON uc.category_code = c.code
        LEFT JOIN subscribers s ON s.user_id = uc.user_id
        WHERE s.is_active = 1 OR s.is_active IS NULL
        GROUP BY c.code, c.name, c.emoji
        ORDER BY subscribers_count DESC, c.name ASC
        """
    )
    return [
        {
            "code": r[0],
            "name": r[1],
            "emoji": r[2],
            "subscribers_count": r[3] or 0,
        }
        for r in rows
    ]


def is_message_processed(message_id: str, chat_id: str) -> bool:
    return fetchone(
        "SELECT 1 FROM processed_messages WHERE message_id = ? AND chat_id = ?",
        (message_id, chat_id),
    ) is not None


def mark_message_processed(message_id: str, chat_id: str):
    execute("""
        INSERT INTO processed_messages (message_id, chat_id) VALUES (?, ?)
        ON CONFLICT(message_id, chat_id) DO NOTHING
    """, (message_id, chat_id))


def get_last_processed_id(chat_id: str) -> int:
    row = fetchone("SELECT last_message_id FROM last_processed WHERE chat_id = ?", (chat_id,))
    return row[0] if row else 0


def update_last_processed_id(chat_id: str, message_id: int):
    execute("""
        INSERT INTO last_processed (chat_id, last_message_id, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(chat_id) DO UPDATE SET
            last_message_id = excluded.last_message_id,
            updated_at = CURRENT_TIMESTAMP
    """, (chat_id, message_id))
