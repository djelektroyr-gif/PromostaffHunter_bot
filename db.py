import logging
import os
import shutil
from datetime import datetime, timedelta, timezone

from config import get_database_path
from db_backend import (
    IS_POSTGRES,
    IntegrityError,
    add_column_if_missing,
    bool_default_false,
    bool_default_true,
    bool_false,
    bool_true,
    column_exists_cur,
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
    pg_column_data_type,
    q,
    serial_pk,
    table_exists,
    vacancy_sort_published_sql,
)

logger = logging.getLogger(__name__)

DB_NAME = db_info_label() if IS_POSTGRES else get_database_path()


def _migrate_pg_telegram_bigint_ids(cur) -> None:
    """Telegram user/message id > 2^31 — INTEGER в PG переполняется."""
    if not IS_POSTGRES:
        return
    targets = [
        ("vacancies", "poster_user_id"),
        ("vacancies", "employer_id"),
        ("vacancies", "posted_by_bot_user_id"),
        ("employers", "telegram_user_id"),
        ("employers", "bot_user_id"),
        ("last_processed", "last_message_id"),
    ]
    for table, column in targets:
        if not column_exists_cur(cur, table, column):
            continue
        if pg_column_data_type(cur, table, column) != "integer":
            continue
        try:
            cur.execute(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE BIGINT")
            logger.info("PostgreSQL: %s.%s → BIGINT", table, column)
        except Exception as e:
            logger.warning("PostgreSQL migrate %s.%s: %s", table, column, e)


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
            cur=cur,
        )
        add_column_if_missing(
            "subscribers", "questionnaire",
            "ALTER TABLE subscribers ADD COLUMN questionnaire TEXT DEFAULT NULL",
            cur=cur,
        )
        add_column_if_missing(
            "subscribers", "plan",
            "ALTER TABLE subscribers ADD COLUMN plan TEXT DEFAULT 'free'",
            cur=cur,
        )
        add_column_if_missing(
            "subscribers", "paid_until",
            "ALTER TABLE subscribers ADD COLUMN paid_until TIMESTAMP DEFAULT NULL",
            cur=cur,
        )
        add_column_if_missing(
            "subscribers", "metro_zones",
            "ALTER TABLE subscribers ADD COLUMN metro_zones TEXT DEFAULT NULL",
            cur=cur,
        )
        add_column_if_missing(
            "subscribers", "trial_used",
            "ALTER TABLE subscribers ADD COLUMN trial_used BOOLEAN DEFAULT 0",
            "ALTER TABLE subscribers ADD COLUMN trial_used BOOLEAN DEFAULT FALSE",
            cur=cur,
        )
        add_column_if_missing(
            "subscribers", "birth_date",
            "ALTER TABLE subscribers ADD COLUMN birth_date TEXT DEFAULT NULL",
            cur=cur,
        )
        add_column_if_missing(
            "subscribers", "resume_extra",
            "ALTER TABLE subscribers ADD COLUMN resume_extra TEXT DEFAULT NULL",
            cur=cur,
        )
        add_column_if_missing(
            "subscribers", "photo_storage_path",
            "ALTER TABLE subscribers ADD COLUMN photo_storage_path TEXT DEFAULT NULL",
            cur=cur,
        )
        add_column_if_missing(
            "subscribers", "photo_updated_at",
            "ALTER TABLE subscribers ADD COLUMN photo_updated_at TIMESTAMP DEFAULT NULL",
            cur=cur,
        )
        add_column_if_missing(
            "subscribers", "user_role",
            "ALTER TABLE subscribers ADD COLUMN user_role TEXT DEFAULT 'candidate'",
            cur=cur,
        )

        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS employers (
                id {serial_pk()},
                telegram_user_id INTEGER UNIQUE,
                username TEXT,
                display_name TEXT,
                contact_text TEXT,
                contact_source TEXT,
                first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                vacancies_count INTEGER DEFAULT 0,
                categories_csv TEXT DEFAULT NULL,
                bot_user_id INTEGER DEFAULT NULL
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_employers_tg_user ON employers(telegram_user_id)"
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
            cur=cur,
        )
        add_column_if_missing(
            "vacancies", "is_closed",
            f"ALTER TABLE vacancies ADD COLUMN is_closed BOOLEAN {bool_default_false()}",
            f"ALTER TABLE vacancies ADD COLUMN is_closed BOOLEAN {bool_default_false()}",
            cur=cur,
        )
        add_column_if_missing(
            "vacancies", "dedupe_key",
            "ALTER TABLE vacancies ADD COLUMN dedupe_key TEXT DEFAULT NULL",
            cur=cur,
        )
        add_column_if_missing(
            "vacancies", "published_at",
            "ALTER TABLE vacancies ADD COLUMN published_at TEXT DEFAULT NULL",
            cur=cur,
        )
        for col, ddl in (
            ("poster_user_id", "ALTER TABLE vacancies ADD COLUMN poster_user_id INTEGER DEFAULT NULL"),
            ("poster_username", "ALTER TABLE vacancies ADD COLUMN poster_username TEXT DEFAULT NULL"),
            ("poster_display_name", "ALTER TABLE vacancies ADD COLUMN poster_display_name TEXT DEFAULT NULL"),
            ("contact_source", "ALTER TABLE vacancies ADD COLUMN contact_source TEXT DEFAULT NULL"),
            ("employer_id", "ALTER TABLE vacancies ADD COLUMN employer_id INTEGER DEFAULT NULL"),
            ("posted_by_bot_user_id", "ALTER TABLE vacancies ADD COLUMN posted_by_bot_user_id INTEGER DEFAULT NULL"),
            ("moderation_status", "ALTER TABLE vacancies ADD COLUMN moderation_status TEXT DEFAULT 'approved'"),
        ):
            add_column_if_missing("vacancies", col, ddl, cur=cur)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_vacancies_dedupe_key ON vacancies(dedupe_key)")

        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS vacancy_notfit_feedback (
                id {serial_pk()},
                user_id INTEGER NOT NULL,
                vacancy_id TEXT NOT NULL,
                vacancy_category TEXT,
                user_categories TEXT,
                reason_code TEXT,
                reason_text TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, vacancy_id)
            )
        """)
        for col, ddl in (
            ("reason_code", "ALTER TABLE vacancy_notfit_feedback ADD COLUMN reason_code TEXT"),
            ("reason_text", "ALTER TABLE vacancy_notfit_feedback ADD COLUMN reason_text TEXT"),
        ):
            add_column_if_missing("vacancy_notfit_feedback", col, ddl, cur=cur)

        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS user_forum_topics (
                id {serial_pk()},
                user_id INTEGER NOT NULL,
                topic_key TEXT NOT NULL,
                thread_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, topic_key)
            )
        """)

        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS vacancy_channel_posts (
                vacancy_id TEXT PRIMARY KEY,
                posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS llm_usage (
                id {serial_pk()},
                user_id INTEGER NOT NULL,
                usage_day TEXT NOT NULL,
                count INTEGER DEFAULT 0,
                UNIQUE(user_id, usage_day)
            )
        """)

        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS star_purchases (
                id {serial_pk()},
                user_id INTEGER NOT NULL,
                vacancy_id TEXT NOT NULL,
                stars_amount INTEGER NOT NULL,
                payload TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, vacancy_id, payload)
            )
        """)

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
            cur=cur,
        )
        add_column_if_missing(
            "responses", "star_boost",
            f"ALTER TABLE responses ADD COLUMN star_boost BOOLEAN {bool_default_false()}",
            cur=cur,
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
        add_column_if_missing(
            "target_chats", "expected_roles",
            "ALTER TABLE target_chats ADD COLUMN expected_roles TEXT DEFAULT NULL",
            cur=cur,
        )

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

        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS premium_requests (
                id {serial_pk()},
                user_id INTEGER NOT NULL,
                username TEXT,
                full_name TEXT,
                phone TEXT,
                category_codes TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        add_column_if_missing(
            "premium_requests", "is_renewal",
            "ALTER TABLE premium_requests ADD COLUMN is_renewal BOOLEAN DEFAULT 0",
            "ALTER TABLE premium_requests ADD COLUMN is_renewal BOOLEAN DEFAULT FALSE",
            cur=cur,
        )
        add_column_if_missing(
            "premium_requests", "receipt_file_id",
            "ALTER TABLE premium_requests ADD COLUMN receipt_file_id TEXT DEFAULT NULL",
            cur=cur,
        )
        add_column_if_missing(
            "premium_requests", "receipt_kind",
            "ALTER TABLE premium_requests ADD COLUMN receipt_kind TEXT DEFAULT NULL",
            cur=cur,
        )

        _migrate_pg_telegram_bigint_ids(cur)

    logger.info(db_info_label())


def add_subscriber(user_id: int, username: str, first_name: str, last_name: str = None):
    execute(f"""
        INSERT INTO subscribers (user_id, username, first_name, last_name, is_active)
        VALUES (?, ?, ?, ?, {bool_true()})
        ON CONFLICT(user_id) DO UPDATE SET
            username = excluded.username,
            first_name = excluded.first_name,
            last_name = excluded.last_name,
            is_active = {bool_true()}
    """, (user_id, username, first_name, last_name))


def update_subscriber_profile(
    user_id: int,
    full_name: str,
    age: int,
    phone: str,
    photo_file_id: str = None,
    birth_date: str = None,
):
    if photo_file_id:
        execute("""
            UPDATE subscribers
            SET full_name = ?, age = ?, phone = ?, photo_file_id = ?, birth_date = COALESCE(?, birth_date)
            WHERE user_id = ?
        """, (full_name, age, phone, photo_file_id, birth_date, user_id))
    else:
        execute("""
            UPDATE subscribers
            SET full_name = ?, age = ?, phone = ?, birth_date = COALESCE(?, birth_date)
            WHERE user_id = ?
        """, (full_name, age, phone, birth_date, user_id))


def update_subscriber_name(user_id: int, full_name: str):
    execute("UPDATE subscribers SET full_name = ? WHERE user_id = ?", (full_name, user_id))


def update_subscriber_age(user_id: int, age: int, birth_date: str = None):
    execute(
        "UPDATE subscribers SET age = ?, birth_date = COALESCE(?, birth_date) WHERE user_id = ?",
        (age, birth_date, user_id),
    )


def update_subscriber_phone(user_id: int, phone: str):
    execute("UPDATE subscribers SET phone = ? WHERE user_id = ?", (phone, user_id))


def update_resume_extra(user_id: int, resume_extra: str | None):
    execute("UPDATE subscribers SET resume_extra = ? WHERE user_id = ?", (resume_extra, user_id))


def update_subscriber_photo(user_id: int, photo_file_id: str):
    execute("UPDATE subscribers SET photo_file_id = ? WHERE user_id = ?", (photo_file_id, user_id))


def update_subscriber_photo_storage(
    user_id: int,
    photo_file_id: str | None,
    photo_storage_path: str | None,
):
    execute(
        """
        UPDATE subscribers
        SET photo_file_id = ?, photo_storage_path = ?, photo_updated_at = CURRENT_TIMESTAMP
        WHERE user_id = ?
        """,
        (photo_file_id, photo_storage_path, user_id),
    )


def clear_subscriber_photo(user_id: int):
    execute(
        """
        UPDATE subscribers
        SET photo_file_id = NULL, photo_storage_path = NULL, photo_updated_at = NULL
        WHERE user_id = ?
        """,
        (user_id,),
    )


def update_candidate_questionnaire(user_id: int, questionnaire_text: str):
    execute("UPDATE subscribers SET questionnaire = ? WHERE user_id = ?", (questionnaire_text, user_id))


def get_subscriber_profile(user_id: int) -> dict:
    row = fetchone("""
        SELECT user_id, username, first_name, last_name, full_name, age, phone,
               photo_file_id, questionnaire, is_active, plan, paid_until, metro_zones, trial_used,
               birth_date, resume_extra, photo_storage_path, photo_updated_at, user_role
        FROM subscribers WHERE user_id = ?
    """, (user_id,))
    if row:
        return {
            "user_id": row[0], "username": row[1], "first_name": row[2], "last_name": row[3],
            "full_name": row[4], "age": row[5], "phone": row[6], "photo_file_id": row[7],
            "questionnaire": row[8], "is_active": row[9], "plan": row[10] or "free",
            "paid_until": row[11], "metro_zones": row[12], "trial_used": bool(row[13]),
            "birth_date": row[14], "resume_extra": row[15],
            "photo_storage_path": row[16], "photo_updated_at": row[17],
            "user_role": row[18] or "candidate",
        }
    return None


def rebuild_candidate_questionnaire(user_id: int) -> str:
    """Пересобрать текст анкеты из полей профиля (после редактирования «Мои данные»)."""
    profile = get_subscriber_profile(user_id)
    if not profile:
        return ""
    username = profile.get("username") or "нет"
    birth_line = ""
    if profile.get("birth_date"):
        birth_line = f"🎂 *Дата рождения:* {profile['birth_date']}\n"
    extra_block = ""
    if profile.get("resume_extra"):
        extra_block = f"\n📋 *Доп. информация:*\n{profile['resume_extra']}\n"
    return (
        "📝 *АНКЕТА КАНДИДАТА*\n\n"
        f"👤 *ФИО:* {profile.get('full_name') or '—'}\n"
        f"{birth_line}"
        f"📊 *Возраст:* {profile.get('age') or '—'} лет\n"
        f"📞 *Телефон:* {profile.get('phone') or '—'}\n"
        f"🆔 *Telegram:* @{username}"
        f"{extra_block}"
    ).strip()


def get_subscribers_with_photos() -> list[dict]:
    rows = fetchall(f"""
        SELECT user_id, photo_file_id, photo_storage_path, photo_updated_at
        FROM subscribers
        WHERE is_active = {bool_true()}
          AND (photo_file_id IS NOT NULL OR photo_storage_path IS NOT NULL)
    """)
    return [
        {
            "user_id": r[0],
            "photo_file_id": r[1],
            "photo_storage_path": r[2],
            "photo_updated_at": r[3],
        }
        for r in rows
    ]


def is_user_premium(user_id: int) -> bool:
    return fetchone(
        f"""
        SELECT 1 FROM subscribers
        WHERE user_id = ? AND plan = 'premium'
          AND {paid_until_active()}
        """,
        (user_id,),
    ) is not None


def _parse_paid_until(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        for fmt, size in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d", 10)):
            try:
                return datetime.strptime(s[:size], fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def set_user_plan(user_id: int, plan: str = "premium", days: int = 30, extend: bool = False):
    if plan == "free":
        execute(
            "UPDATE subscribers SET plan = 'free', paid_until = NULL WHERE user_id = ?",
            (user_id,),
        )
    elif extend:
        row = fetchone(
            "SELECT paid_until, plan FROM subscribers WHERE user_id = ?",
            (user_id,),
        )
        base = datetime.now(timezone.utc)
        if row and row[1] == "premium" and row[0]:
            existing = _parse_paid_until(row[0])
            if existing and existing > base:
                base = existing
        new_until = base + timedelta(days=int(days))
        execute(
            "UPDATE subscribers SET plan = 'premium', paid_until = ? WHERE user_id = ?",
            (new_until, user_id),
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
        WHERE is_active = {bool_true()} AND plan = 'premium'
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
        SET plan = 'premium', paid_until = {now_plus_days(trial_days)}, trial_used = {bool_true()}
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
    rows = fetchall(f"""
        SELECT s.user_id, s.full_name, s.phone, s.username, s.metro_zones
        FROM subscribers s
        JOIN user_categories uc ON s.user_id = uc.user_id
        WHERE uc.category_code = ? AND s.is_active = {bool_true()}
    """, (category_code,))
    return [
        {"user_id": r[0], "full_name": r[1], "phone": r[2], "username": r[3], "metro_zones": r[4]}
        for r in rows
    ]


# ========== ВАКАНСИИ ==========
def save_vacancy(vacancy_id: str, source_chat: str, source_chat_title: str,
                 category_code: str, message_text: str, message_link: str,
                 author_contact: str = None, address: str = None, is_closed: bool = False,
                 dedupe_key: str = None, published_at: str = None,
                 poster_user_id: int = None, poster_username: str = None,
                 poster_display_name: str = None, contact_source: str = None,
                 employer_id: int = None, posted_by_bot_user_id: int = None,
                 moderation_status: str = "approved"):
    # PostgreSQL: в ON CONFLICT нужен префикс vacancies. — иначе ambiguous column
    v = "vacancies." if IS_POSTGRES else ""
    mod = moderation_status or "approved"
    execute(f"""
        INSERT INTO vacancies
        (id, source_chat, source_chat_title, category_code, message_text, message_link,
         author_contact, address, is_closed, dedupe_key, published_at,
         poster_user_id, poster_username, poster_display_name, contact_source,
         employer_id, posted_by_bot_user_id, moderation_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            source_chat_title = excluded.source_chat_title,
            category_code = excluded.category_code,
            message_text = excluded.message_text,
            message_link = excluded.message_link,
            author_contact = COALESCE(excluded.author_contact, {v}author_contact),
            address = COALESCE(excluded.address, {v}address),
            dedupe_key = COALESCE(excluded.dedupe_key, {v}dedupe_key),
            published_at = COALESCE(excluded.published_at, {v}published_at),
            poster_user_id = COALESCE(excluded.poster_user_id, {v}poster_user_id),
            poster_username = COALESCE(excluded.poster_username, {v}poster_username),
            poster_display_name = COALESCE(excluded.poster_display_name, {v}poster_display_name),
            contact_source = COALESCE(excluded.contact_source, {v}contact_source),
            employer_id = COALESCE(excluded.employer_id, {v}employer_id),
            posted_by_bot_user_id = COALESCE(excluded.posted_by_bot_user_id, {v}posted_by_bot_user_id),
            moderation_status = COALESCE(excluded.moderation_status, {v}moderation_status)
    """, (
        vacancy_id, source_chat, source_chat_title, category_code, message_text, message_link,
        author_contact, address, is_closed, dedupe_key, published_at,
        poster_user_id, poster_username, poster_display_name, contact_source,
        employer_id, posted_by_bot_user_id, mod,
    ))


_VACANCY_VISIBLE_SQL = "(moderation_status IS NULL OR moderation_status = 'approved')"


def get_vacancy_row(vacancy_id: str):
    return fetchone(
        "SELECT message_text, message_link, source_chat_title, author_contact, address FROM vacancies WHERE id = ?",
        (vacancy_id,),
    )


def get_vacancy_push_row(vacancy_id: str):
    return fetchone(
        """SELECT message_text, message_link, source_chat_title, author_contact, address,
                  category_code, source_chat, dedupe_key, published_at, poster_user_id,
                  poster_username, moderation_status, posted_by_bot_user_id
           FROM vacancies WHERE id = ?""",
        (vacancy_id,),
    )


def get_pending_moderation_vacancies(limit: int = 15) -> list[dict]:
    rows = fetchall(
        f"""
        SELECT id, category_code, source_chat_title, message_text, author_contact,
               posted_by_bot_user_id, published_at
        FROM vacancies
        WHERE moderation_status = 'pending' AND is_closed = {bool_false()}
        ORDER BY {vacancy_sort_published_sql()} DESC
        LIMIT ?
    """,
        (limit,),
    )
    return [
        {
            "id": r[0], "category_code": r[1], "source_chat_title": r[2], "message_text": r[3],
            "author_contact": r[4], "posted_by_bot_user_id": r[5], "published_at": r[6],
        }
        for r in rows
    ]


def set_vacancy_moderation(vacancy_id: str, status: str):
    execute("UPDATE vacancies SET moderation_status = ? WHERE id = ?", (status, vacancy_id))


def record_vacancy_notfit(
    user_id: int,
    vacancy_id: str,
    vacancy_category: str,
    user_categories: list[str],
    *,
    reason_code: str = "",
    reason_text: str | None = None,
):
    cats = ",".join(user_categories) if user_categories else ""
    execute(
        """
        INSERT INTO vacancy_notfit_feedback
            (user_id, vacancy_id, vacancy_category, user_categories, reason_code, reason_text)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, vacancy_id) DO UPDATE SET
            vacancy_category = excluded.vacancy_category,
            user_categories = excluded.user_categories,
            reason_code = excluded.reason_code,
            reason_text = excluded.reason_text,
            created_at = CURRENT_TIMESTAMP
        """,
        (user_id, vacancy_id, vacancy_category, cats, reason_code or None, reason_text),
    )


def get_notfit_stats(limit: int = 10) -> list[dict]:
    rows = fetchall(
        """
        SELECT vacancy_category, reason_code, COUNT(*) AS cnt
        FROM vacancy_notfit_feedback
        GROUP BY vacancy_category, reason_code
        ORDER BY cnt DESC
        LIMIT ?
        """,
        (limit,),
    )
    return [{"category": r[0], "reason_code": r[1], "count": r[2]} for r in rows]


def get_notfit_export_rows(limit: int = 15000) -> list[dict]:
    rows = fetchall(q(f"""
        SELECT
            f.id, f.user_id, f.vacancy_id, f.vacancy_category, f.user_categories,
            f.reason_code, f.reason_text, f.created_at,
            v.message_text, v.source_chat_title, v.message_link, v.category_code,
            s.username, s.full_name, s.first_name, s.last_name
        FROM vacancy_notfit_feedback f
        LEFT JOIN vacancies v ON v.id = f.vacancy_id
        LEFT JOIN subscribers s ON s.user_id = f.user_id
        ORDER BY f.created_at DESC
        LIMIT ?
    """), (limit,))
    return [
        {
            "id": r[0], "user_id": r[1], "vacancy_id": r[2],
            "vacancy_category": r[3], "user_categories": r[4],
            "reason_code": r[5], "reason_text": r[6], "created_at": r[7],
            "message_text": r[8], "source_chat_title": r[9], "message_link": r[10],
            "vacancy_category_live": r[11],
            "username": r[12], "full_name": r[13],
            "first_name": r[14], "last_name": r[15],
        }
        for r in rows
    ]


def get_user_topic_thread_id(user_id: int, topic_key: str) -> int | None:
    row = fetchone(
        "SELECT thread_id FROM user_forum_topics WHERE user_id = ? AND topic_key = ?",
        (user_id, topic_key),
    )
    return int(row[0]) if row else None


def save_user_topic_thread(user_id: int, topic_key: str, thread_id: int):
    execute(
        """
        INSERT INTO user_forum_topics (user_id, topic_key, thread_id)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id, topic_key) DO UPDATE SET thread_id = excluded.thread_id
        """,
        (user_id, topic_key, thread_id),
    )


def is_vacancy_channel_posted(vacancy_id: str) -> bool:
    row = fetchone("SELECT 1 FROM vacancy_channel_posts WHERE vacancy_id = ?", (vacancy_id,))
    return bool(row)


def mark_vacancy_channel_posted(vacancy_id: str):
    execute(
        """
        INSERT INTO vacancy_channel_posts (vacancy_id) VALUES (?)
        ON CONFLICT(vacancy_id) DO NOTHING
        """,
        (vacancy_id,),
    )


def get_llm_usage_today(user_id: int, usage_day: str) -> int:
    row = fetchone(
        "SELECT count FROM llm_usage WHERE user_id = ? AND usage_day = ?",
        (user_id, usage_day),
    )
    return int(row[0]) if row else 0


def increment_llm_usage(user_id: int, usage_day: str):
    execute(
        """
        INSERT INTO llm_usage (user_id, usage_day, count) VALUES (?, ?, 1)
        ON CONFLICT(user_id, usage_day) DO UPDATE SET count = count + 1
        """,
        (user_id, usage_day),
    )


def create_star_purchase(user_id: int, vacancy_id: str, stars_amount: int, payload: str):
    execute(
        """
        INSERT INTO star_purchases (user_id, vacancy_id, stars_amount, payload, status)
        VALUES (?, ?, ?, ?, 'pending')
        ON CONFLICT(user_id, vacancy_id, payload) DO NOTHING
        """,
        (user_id, vacancy_id, stars_amount, payload),
    )


def complete_star_purchase(payload: str) -> dict | None:
    row = fetchone(
        "SELECT user_id, vacancy_id, stars_amount FROM star_purchases WHERE payload = ? AND status = 'pending'",
        (payload,),
    )
    if not row:
        return None
    execute("UPDATE star_purchases SET status = 'paid' WHERE payload = ?", (payload,))
    return {"user_id": row[0], "vacancy_id": row[1], "stars_amount": row[2]}


def has_star_purchase_for_vacancy(user_id: int, vacancy_id: str) -> bool:
    row = fetchone(
        "SELECT 1 FROM star_purchases WHERE user_id = ? AND vacancy_id = ? AND status = 'paid'",
        (user_id, vacancy_id),
    )
    return bool(row)


def set_response_star_boost(user_id: int, vacancy_id: str):
    execute(
        "UPDATE responses SET star_boost = ? WHERE user_id = ? AND vacancy_id = ?",
        (True, user_id, vacancy_id),
    )


def set_subscriber_role(user_id: int, role: str):
    execute("UPDATE subscribers SET user_role = ? WHERE user_id = ?", (role, user_id))


def get_subscriber_role(user_id: int) -> str:
    row = fetchone("SELECT user_role FROM subscribers WHERE user_id = ?", (user_id,))
    return (row[0] if row and row[0] else "candidate")


def _merge_categories_csv(existing: str | None, category_code: str) -> str:
    parts = [p.strip() for p in (existing or "").split(",") if p.strip()]
    if category_code and category_code not in parts:
        parts.append(category_code)
    return ",".join(sorted(parts))


def upsert_employer_from_post(
    *,
    telegram_user_id: int | None,
    username: str | None,
    display_name: str | None,
    contact_text: str | None,
    contact_source: str | None,
    category_code: str | None,
    bot_user_id: int | None = None,
) -> int | None:
    """CRM заказчиков из парсера или публикации в боте. Возвращает employer.id."""
    if not telegram_user_id and not contact_text and not username:
        return None
    with db_conn() as conn:
        cur = conn.cursor()
        row = None
        if telegram_user_id:
            cur.execute(q("SELECT id, categories_csv FROM employers WHERE telegram_user_id = ?"), (telegram_user_id,))
            row = cur.fetchone()
        if not row and contact_text:
            cur.execute(q("SELECT id, categories_csv FROM employers WHERE contact_text = ?"), (contact_text,))
            row = cur.fetchone()
        cats = _merge_categories_csv(row[1] if row else None, category_code or "")
        if row:
            employer_id = row[0]
            cur.execute(
                q("""
                    UPDATE employers SET
                        username = COALESCE(?, username),
                        display_name = COALESCE(?, display_name),
                        contact_text = COALESCE(?, contact_text),
                        contact_source = COALESCE(?, contact_source),
                        last_seen_at = CURRENT_TIMESTAMP,
                        vacancies_count = vacancies_count + 1,
                        categories_csv = ?,
                        bot_user_id = COALESCE(?, bot_user_id),
                        telegram_user_id = COALESCE(telegram_user_id, ?)
                    WHERE id = ?
                """),
                (username, display_name, contact_text, contact_source, cats, bot_user_id, telegram_user_id, employer_id),
            )
        else:
            cur.execute(
                q("""
                    INSERT INTO employers
                    (telegram_user_id, username, display_name, contact_text, contact_source,
                     vacancies_count, categories_csv, bot_user_id)
                    VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                """),
                (telegram_user_id, username, display_name, contact_text, contact_source, cats, bot_user_id),
            )
            if IS_POSTGRES:
                cur.execute("SELECT lastval()")
                employer_id = cur.fetchone()[0]
            else:
                employer_id = cur.lastrowid
    return employer_id


def link_employer_to_bot_user(telegram_user_id: int, bot_user_id: int):
    execute(
        "UPDATE employers SET bot_user_id = ? WHERE telegram_user_id = ?",
        (bot_user_id, telegram_user_id),
    )


def get_subscribers_export_rows(limit: int = 15000) -> list[dict]:
    rows = fetchall(q(f"""
        SELECT s.user_id, s.username, s.full_name, s.first_name, s.last_name, s.phone, s.age,
               s.birth_date, s.user_role, s.plan, s.paid_until, s.trial_used, s.metro_zones,
               s.registered_at, s.is_active, s.resume_extra,
               CASE WHEN s.photo_file_id IS NOT NULL OR s.photo_storage_path IS NOT NULL THEN 1 ELSE 0 END
        FROM subscribers s
        ORDER BY s.registered_at DESC
        LIMIT ?
    """), (limit,))
    result = []
    for row in rows:
        uid = row[0]
        cat_rows = fetchall(
            "SELECT category_code FROM user_categories WHERE user_id = ? ORDER BY category_code",
            (uid,),
        )
        result.append({
            "user_id": row[0], "username": row[1], "full_name": row[2], "first_name": row[3],
            "last_name": row[4], "phone": row[5], "age": row[6], "birth_date": row[7],
            "user_role": row[8] or "candidate", "plan": row[9], "paid_until": row[10],
            "trial_used": row[11], "metro_zones": row[12], "registered_at": row[13],
            "is_active": row[14], "resume_extra": row[15],
            "has_photo": bool(row[16]),
            "categories": ", ".join(c[0] for c in cat_rows),
        })
    return result


def get_vacancies_export_rows(limit: int = 15000) -> list[dict]:
    rows = fetchall(q(f"""
        SELECT id, category_code, source_chat_title, author_contact, contact_source,
               poster_user_id, poster_username, poster_display_name, employer_id,
               posted_by_bot_user_id, address, published_at, found_at, is_closed,
               message_link, message_text
        FROM vacancies
        ORDER BY {vacancy_sort_published_sql()} DESC
        LIMIT ?
    """), (limit,))
    return [
        {
            "id": r[0], "category_code": r[1], "source_chat_title": r[2], "author_contact": r[3],
            "contact_source": r[4], "poster_user_id": r[5], "poster_username": r[6],
            "poster_display_name": r[7], "employer_id": r[8], "posted_by_bot_user_id": r[9],
            "address": r[10], "published_at": r[11], "found_at": r[12], "is_closed": r[13],
            "message_link": r[14], "message_text": r[15],
        }
        for r in rows
    ]


def get_employers_export_rows(limit: int = 15000) -> list[dict]:
    rows = fetchall(q(f"""
        SELECT id, telegram_user_id, username, display_name, contact_text, contact_source,
               vacancies_count, categories_csv, bot_user_id, first_seen_at, last_seen_at
        FROM employers
        ORDER BY vacancies_count DESC, last_seen_at DESC
        LIMIT ?
    """), (limit,))
    return [
        {
            "id": r[0], "telegram_user_id": r[1], "username": r[2], "display_name": r[3],
            "contact_text": r[4], "contact_source": r[5], "vacancies_count": r[6],
            "categories_csv": r[7], "bot_user_id": r[8], "first_seen_at": r[9], "last_seen_at": r[10],
        }
        for r in rows
    ]


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
          AND is_closed = {bool_false()}
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
        WHERE is_closed = {bool_false()}
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


def get_feed_vacancies_for_user(user_id: int, category_code: str) -> list:
    """Открытые вакансии категории, которые пользователь ещё не получал (push/лента)."""
    rows = fetchall(
        f"""
        SELECT v.id, v.source_chat_title, v.message_text, v.message_link,
               v.author_contact, v.address, v.found_at, v.published_at
        FROM vacancies v
        WHERE v.category_code = ? AND v.is_closed = {bool_false()}
          AND (v.moderation_status IS NULL OR v.moderation_status = 'approved')
          AND NOT EXISTS (
            SELECT 1 FROM sent_vacancies sv
            WHERE sv.user_id = ? AND sv.vacancy_id = v.id
          )
        ORDER BY v.found_at DESC
        """,
        (category_code, user_id),
    )
    return [
        {
            "id": r[0], "source": r[1], "text": r[2], "link": r[3], "contact": r[4], "address": r[5],
            "found_at": r[6], "published_at": r[7],
        }
        for r in rows
    ]


def count_feed_vacancies_for_user(user_id: int, category_code: str) -> int:
    return fetchval(
        f"""
        SELECT COUNT(*) FROM vacancies v
        WHERE v.category_code = ? AND v.is_closed = {bool_false()}
          AND (v.moderation_status IS NULL OR v.moderation_status = 'approved')
          AND NOT EXISTS (
            SELECT 1 FROM sent_vacancies sv
            WHERE sv.user_id = ? AND sv.vacancy_id = v.id
          )
        """,
        (category_code, user_id),
        default=0,
    )


def get_unsent_vacancies_by_category(category_code: str) -> list:
    """Legacy: глобальный is_sent. Для ленты используйте get_feed_vacancies_for_user."""
    rows = fetchall(f"""
        SELECT id, source_chat_title, message_text, message_link, author_contact, address, found_at, published_at
        FROM vacancies
        WHERE category_code = ? AND is_sent = {bool_false()} AND is_closed = {bool_false()}
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
    execute(f"UPDATE vacancies SET is_sent = {bool_true()} WHERE id = ?", (vacancy_id,))


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
            q(f"UPDATE vacancies SET is_closed = {bool_true()} WHERE id = ? OR id = ?"),
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
    return fetchall(f"""
        SELECT c.id, c.user_id, s.full_name, c.vacancy_id, c.reason, c.complaint_text, c.created_at
        FROM complaints c
        JOIN subscribers s ON c.user_id = s.user_id
        WHERE c.resolved = {bool_false()}
        ORDER BY c.created_at DESC
        LIMIT ?
    """, (limit,))


def resolve_complaint(complaint_id: int):
    execute(f"UPDATE complaints SET resolved = {bool_true()} WHERE id = ?", (complaint_id,))


# ========== ПОДДЕРЖКА ==========
def add_support_request(user_id: int, message_text: str, username: str = None):
    execute("""
        INSERT INTO support_requests (user_id, message_text, user_username)
        VALUES (?, ?, ?)
    """, (user_id, message_text, username))


def get_unanswered_support_requests(limit: int = 20):
    return fetchall(f"""
        SELECT id, user_id, user_username, message_text, created_at
        FROM support_requests
        WHERE answered = {bool_false()}
        ORDER BY created_at ASC
        LIMIT ?
    """, (limit,))


def mark_support_answered(request_id: int, admin_response: str = None):
    execute(
        f"UPDATE support_requests SET answered = {bool_true()}, admin_response = ? WHERE id = ?",
        (admin_response, request_id),
    )


# ========== ДИНАМИЧЕСКИЕ ЧАТЫ ==========
def get_target_chats() -> list:
    if not table_exists("target_chats"):
        return []
    rows = fetchall(f"SELECT chat_link FROM target_chats WHERE is_active = {bool_true()}")
    return [row[0] for row in rows]


def list_target_chats() -> list:
    """Все чаты парсинга (включая отключённые) для админки."""
    if not table_exists("target_chats"):
        return []
    rows = fetchall(
        "SELECT chat_link, is_active, added_at, expected_roles FROM target_chats ORDER BY is_active DESC, added_at"
    )
    return [
        {"chat_link": r[0], "is_active": bool(r[1]), "added_at": r[2], "expected_roles": r[3]}
        for r in rows
    ]


def set_target_chat_expected_roles(chat_link: str, roles_csv: str) -> bool:
    if not table_exists("target_chats"):
        return False
    cur = execute(
        "UPDATE target_chats SET expected_roles = ? WHERE chat_link = ?",
        (roles_csv.strip() or None, chat_link),
    )
    return cur is not None


def get_target_chat_expected_roles(chat_link: str) -> set[str]:
    if not table_exists("target_chats"):
        return set()
    row = fetchone("SELECT expected_roles FROM target_chats WHERE chat_link = ?", (chat_link,))
    if not row or not row[0]:
        return set()
    return {p.strip() for p in str(row[0]).split(",") if p.strip()}


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
    execute(f"UPDATE target_chats SET is_active = {bool_false()} WHERE chat_link = ?", (chat_link,))


# ========== АДМИНСКАЯ СТАТИСТИКА ==========
def get_all_subscribers() -> list:
    rows = fetchall(f"SELECT user_id FROM subscribers WHERE is_active = {bool_true()}")
    return [row[0] for row in rows]


def get_recent_responses(limit: int = 10) -> list:
    return fetchall("""
        SELECT r.responded_at, r.vacancy_text, s.username, s.first_name
        FROM responses r
        JOIN subscribers s ON r.user_id = s.user_id
        ORDER BY r.responded_at DESC
        LIMIT ?
    """, (limit,))


def count_user_responses(user_id: int) -> int:
    return fetchval(
        "SELECT COUNT(*) FROM responses WHERE user_id = ?",
        (user_id,),
        default=0,
    )


def get_user_responses(user_id: int, limit: int = 5, offset: int = 0) -> list:
    rows = fetchall(
        """
        SELECT r.responded_at, r.vacancy_id, r.vacancy_text, r.vacancy_link, r.status,
               v.is_closed, v.source_chat_title, v.author_contact, v.message_link
        FROM responses r
        LEFT JOIN vacancies v ON r.vacancy_id = v.id
        WHERE r.user_id = ?
        ORDER BY r.responded_at DESC
        LIMIT ? OFFSET ?
        """,
        (user_id, limit, offset),
    )
    result = []
    for row in rows:
        is_closed = bool(row[5]) if row[5] is not None else False
        link = row[3] or row[8]
        result.append({
            "responded_at": row[0],
            "vacancy_id": row[1],
            "vacancy_text": row[2] or "",
            "vacancy_link": link,
            "status": row[4] or "pending",
            "is_closed": is_closed,
            "source_chat_title": row[6],
            "author_contact": row[7],
        })
    return result


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


def get_subscribers_display(limit: int = 20) -> list:
    rows = fetchall(
        f"""
        SELECT user_id,
               COALESCE(full_name, first_name, username, CAST(user_id AS TEXT)) AS name
        FROM subscribers
        WHERE is_active = {bool_true()}
        ORDER BY registered_at DESC
        LIMIT ?
        """,
        (limit,),
    )
    return [{"user_id": r[0], "name": r[1]} for r in rows]


def get_admin_stats() -> dict:
    with db_conn(commit=False) as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM subscribers WHERE is_active = {bool_true()}")
        total_subscribers = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM subscribers WHERE full_name IS NOT NULL")
        full_profiles = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM responses")
        total_responses = cur.fetchone()[0]
        cur.execute(
            f"SELECT COUNT(*) FROM vacancies WHERE is_closed = {bool_false()}"
        )
        pending_vacancies = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM vacancies")
        total_vacancies = cur.fetchone()[0]
        cur.execute(f"SELECT COUNT(*) FROM complaints WHERE resolved = {bool_false()}")
        pending_complaints = cur.fetchone()[0]
        cur.execute(f"SELECT COUNT(*) FROM support_requests WHERE answered = {bool_false()}")
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
            q(f"""
                SELECT user_id, username, first_name, last_name, full_name, age, phone,
                       registered_at, is_active, plan, paid_until,
                       CASE WHEN plan = 'premium' AND ({paid_until_active()}) THEN 1 ELSE 0 END
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
                "plan": row[9] or "free",
                "paid_until": row[10],
                "is_premium": bool(row[11]),
                "categories": cats,
            })
    return cards


def get_user_category_mapping() -> list:
    rows = fetchall(
        f"""
        SELECT c.code, c.name, c.emoji, COUNT(uc.user_id) as subscribers_count
        FROM categories c
        LEFT JOIN user_categories uc ON uc.category_code = c.code
        LEFT JOIN subscribers s ON s.user_id = uc.user_id
        WHERE s.is_active = {bool_true()} OR s.is_active IS NULL
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


# ========== ЗАПРОСЫ PREMIUM ==========
def _premium_request_row(row) -> dict:
    return {
        "id": row[0],
        "user_id": row[1],
        "username": row[2],
        "full_name": row[3],
        "phone": row[4],
        "category_codes": row[5],
        "created_at": row[6],
        "is_renewal": bool(row[7]) if len(row) > 7 else False,
        "receipt_file_id": row[8] if len(row) > 8 else None,
        "receipt_kind": row[9] if len(row) > 9 else None,
        "status": row[10] if len(row) > 10 else "pending",
    }


_PREMIUM_REQUEST_SELECT = """
    SELECT id, user_id, username, full_name, phone, category_codes, created_at,
           is_renewal, receipt_file_id, receipt_kind, status
    FROM premium_requests
"""


def add_premium_request(
    user_id: int,
    username: str = None,
    full_name: str = None,
    phone: str = None,
    category_codes: str = None,
    is_renewal: bool = False,
) -> int:
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            q(
                "UPDATE premium_requests SET status = 'cancelled' "
                "WHERE user_id = ? AND status IN ('pending', 'awaiting_receipt')"
            ),
            (user_id,),
        )
        renewal_val = bool_true() if is_renewal else bool_false()
        if IS_POSTGRES:
            cur.execute(
                q(f"""
                    INSERT INTO premium_requests
                    (user_id, username, full_name, phone, category_codes, is_renewal, status)
                    VALUES (?, ?, ?, ?, ?, {renewal_val}, 'awaiting_receipt')
                    RETURNING id
                """),
                (user_id, username, full_name, phone, category_codes),
            )
            return int(cur.fetchone()[0])
        cur.execute(
            q(f"""
                INSERT INTO premium_requests
                (user_id, username, full_name, phone, category_codes, is_renewal, status)
                VALUES (?, ?, ?, ?, ?, {renewal_val}, 'awaiting_receipt')
            """),
            (user_id, username, full_name, phone, category_codes),
        )
        return int(cur.lastrowid)


def get_premium_request(request_id: int) -> dict | None:
    row = fetchone(f"{_PREMIUM_REQUEST_SELECT} WHERE id = ?", (request_id,))
    return _premium_request_row(row) if row else None


def attach_premium_request_receipt(
    request_id: int,
    user_id: int,
    file_id: str,
    kind: str,
) -> bool:
    row = fetchone(
        f"{_PREMIUM_REQUEST_SELECT} WHERE id = ? AND user_id = ? AND status = 'awaiting_receipt'",
        (request_id, user_id),
    )
    if not row:
        return False
    execute(
        """
        UPDATE premium_requests
        SET receipt_file_id = ?, receipt_kind = ?, status = 'pending'
        WHERE id = ?
        """,
        (file_id, kind, request_id),
    )
    return True


def cancel_premium_request_awaiting(user_id: int, request_id: int | None = None) -> None:
    if request_id:
        execute(
            "UPDATE premium_requests SET status = 'cancelled' "
            "WHERE id = ? AND user_id = ? AND status = 'awaiting_receipt'",
            (request_id, user_id),
        )
    else:
        execute(
            "UPDATE premium_requests SET status = 'cancelled' "
            "WHERE user_id = ? AND status = 'awaiting_receipt'",
            (user_id,),
        )


def get_pending_premium_requests(limit: int = 20) -> list:
    rows = fetchall(
        f"""
        {_PREMIUM_REQUEST_SELECT}
        WHERE status = 'pending'
        ORDER BY created_at ASC
        LIMIT ?
        """,
        (limit,),
    )
    return [_premium_request_row(r) for r in rows]


def count_pending_premium_requests() -> int:
    return fetchval(
        "SELECT COUNT(*) FROM premium_requests WHERE status = 'pending'",
        default=0,
    )


def resolve_premium_requests(user_id: int):
    execute(
        "UPDATE premium_requests SET status = 'approved' WHERE user_id = ? AND status = 'pending'",
        (user_id,),
    )


def reject_premium_request(request_id: int) -> int | None:
    row = fetchone(
        f"{_PREMIUM_REQUEST_SELECT} WHERE id = ? AND status = 'pending'",
        (request_id,),
    )
    if not row:
        return None
    execute(
        "UPDATE premium_requests SET status = 'rejected' WHERE id = ?",
        (request_id,),
    )
    return row[1]
