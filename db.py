import json
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


def _pg_alter_integer_to_bigint(cur, table: str, column: str) -> bool:
    """PostgreSQL: INTEGER → BIGINT для Telegram id > 2^31."""
    if not column_exists_cur(cur, table, column):
        return False
    if pg_column_data_type(cur, table, column) != "integer":
        return False
    try:
        cur.execute(
            f'ALTER TABLE "{table}" ALTER COLUMN "{column}" TYPE BIGINT USING "{column}"::bigint'
        )
        logger.info("PostgreSQL: %s.%s → BIGINT", table, column)
        return True
    except Exception as e:
        logger.warning("PostgreSQL migrate %s.%s: %s", table, column, e)
        return False


def _migrate_pg_telegram_bigint_ids(cur) -> None:
    """Telegram user/message id > 2^31 — INTEGER в PG переполняется."""
    if not IS_POSTGRES:
        return

    dropped_fks: list[tuple[str, str, str]] = []
    cur.execute(
        """
        SELECT c.conname, child.relname AS child_table, pg_get_constraintdef(c.oid) AS condef
        FROM pg_constraint c
        JOIN pg_class child ON child.oid = c.conrelid
        JOIN pg_class parent ON parent.oid = c.confrelid
        WHERE c.contype = 'f' AND parent.relname = 'subscribers'
        """
    )
    for conname, child_table, condef in cur.fetchall():
        try:
            cur.execute(f'ALTER TABLE "{child_table}" DROP CONSTRAINT IF EXISTS "{conname}"')
            dropped_fks.append((conname, child_table, condef))
            logger.info("PostgreSQL: dropped FK %s on %s", conname, child_table)
        except Exception as e:
            logger.warning("PostgreSQL drop FK %s on %s: %s", conname, child_table, e)

    for table in (
        "subscribers",
        "user_categories",
        "vacancy_notfit_feedback",
        "user_forum_topics",
        "channel_member_events",
        "user_feed_sessions",
        "llm_usage",
        "star_purchases",
        "sent_vacancies",
        "responses",
        "complaints",
        "support_requests",
        "premium_requests",
    ):
        _pg_alter_integer_to_bigint(cur, table, "user_id")

    for conname, child_table, condef in dropped_fks:
        try:
            cur.execute(f'ALTER TABLE "{child_table}" ADD CONSTRAINT "{conname}" {condef}')
            logger.info("PostgreSQL: restored FK %s on %s", conname, child_table)
        except Exception as e:
            logger.warning("PostgreSQL restore FK %s on %s: %s", conname, child_table, e)

    for table, column in (
        ("vacancies", "poster_user_id"),
        ("vacancies", "posted_by_bot_user_id"),
        ("employers", "telegram_user_id"),
        ("employers", "bot_user_id"),
        ("last_processed", "last_message_id"),
        ("vacancy_channel_posts", "message_id"),
        ("user_forum_topics", "thread_id"),
    ):
        _pg_alter_integer_to_bigint(cur, table, column)


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
            "subscribers", "response_credits",
            "ALTER TABLE subscribers ADD COLUMN response_credits INTEGER DEFAULT 0",
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
        add_column_if_missing(
            "subscribers", "premium_renewal_warn_for",
            "ALTER TABLE subscribers ADD COLUMN premium_renewal_warn_for TIMESTAMP DEFAULT NULL",
            cur=cur,
        )
        add_column_if_missing(
            "subscribers", "last_seen_at",
            "ALTER TABLE subscribers ADD COLUMN last_seen_at TIMESTAMP DEFAULT NULL",
            cur=cur,
        )
        add_column_if_missing(
            "subscribers", "reg_stuck_notified_at",
            "ALTER TABLE subscribers ADD COLUMN reg_stuck_notified_at TIMESTAMP DEFAULT NULL",
            cur=cur,
        )

        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS bot_events (
                id {serial_pk()},
                user_id INTEGER,
                event TEXT NOT NULL,
                meta_json TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_bot_events_created ON bot_events(created_at)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_bot_events_event_created ON bot_events(event, created_at)"
        )

        cur.execute("""
            CREATE TABLE IF NOT EXISTS app_scheduler_flags (
                flag_key TEXT PRIMARY KEY,
                flag_value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS subscriber_filter_prefs (
                user_id INTEGER PRIMARY KEY,
                prefs_json TEXT NOT NULL DEFAULT '{{}}',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES subscribers(user_id)
            )
        """)

        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS push_digest_pending (
                user_id INTEGER NOT NULL,
                vacancy_id TEXT NOT NULL,
                queued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, vacancy_id)
            )
        """)

        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS closed_notice_pending (
                user_id INTEGER NOT NULL,
                vacancy_id TEXT NOT NULL,
                queued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, vacancy_id)
            )
        """)

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
            ("address_normalized", "ALTER TABLE vacancies ADD COLUMN address_normalized TEXT DEFAULT NULL"),
            ("geo_tags", "ALTER TABLE vacancies ADD COLUMN geo_tags TEXT DEFAULT NULL"),
            ("rate_hourly", "ALTER TABLE vacancies ADD COLUMN rate_hourly INTEGER DEFAULT NULL"),
            ("rate_shift", "ALTER TABLE vacancies ADD COLUMN rate_shift INTEGER DEFAULT NULL"),
            ("min_hours", "ALTER TABLE vacancies ADD COLUMN min_hours INTEGER DEFAULT NULL"),
            ("rate_effective_hourly", "ALTER TABLE vacancies ADD COLUMN rate_effective_hourly INTEGER DEFAULT NULL"),
            ("shift_date", "ALTER TABLE vacancies ADD COLUMN shift_date TEXT DEFAULT NULL"),
            ("shift_time_start", "ALTER TABLE vacancies ADD COLUMN shift_time_start TEXT DEFAULT NULL"),
            ("location_lat", "ALTER TABLE vacancies ADD COLUMN location_lat REAL DEFAULT NULL"),
            ("location_lon", "ALTER TABLE vacancies ADD COLUMN location_lon REAL DEFAULT NULL"),
            ("enrichment_version", "ALTER TABLE vacancies ADD COLUMN enrichment_version INTEGER DEFAULT NULL"),
        ):
            add_column_if_missing("vacancies", col, ddl, cur=cur)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_vacancies_dedupe_key ON vacancies(dedupe_key)")
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_vacancies_feed "
            "ON vacancies(category_code, is_closed, found_at)"
        )

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
            CREATE TABLE IF NOT EXISTS user_general_vacancy_pin (
                user_id INTEGER PRIMARY KEY,
                message_id INTEGER NOT NULL,
                vacancy_id TEXT NOT NULL,
                card_text TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS vacancy_channel_posts (
                vacancy_id TEXT PRIMARY KEY,
                posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        for col, ddl in (
            ("category_code", "ALTER TABLE vacancy_channel_posts ADD COLUMN category_code TEXT"),
            ("post_kind", "ALTER TABLE vacancy_channel_posts ADD COLUMN post_kind TEXT DEFAULT 'vacancy'"),
            ("message_id", "ALTER TABLE vacancy_channel_posts ADD COLUMN message_id INTEGER"),
            ("preview_text", "ALTER TABLE vacancy_channel_posts ADD COLUMN preview_text TEXT"),
        ):
            add_column_if_missing("vacancy_channel_posts", col, ddl, cur=cur)

        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS channel_subscriber_snapshots (
                id {serial_pk()},
                member_count INTEGER NOT NULL,
                recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS channel_member_events (
                id {serial_pk()},
                event_type TEXT NOT NULL,
                user_id INTEGER,
                username TEXT,
                event_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_channel_member_events_at ON channel_member_events(event_at)"
        )

        cur.execute("""
            CREATE TABLE IF NOT EXISTS channel_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS channel_promo_sent (
                promo_slot TEXT NOT NULL,
                sent_date TEXT NOT NULL,
                sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (promo_slot, sent_date)
            )
        """)

        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS user_feed_sessions (
                user_id INTEGER PRIMARY KEY,
                feed_mode TEXT NOT NULL,
                category_codes TEXT,
                vacancy_ids TEXT NOT NULL,
                page INTEGER DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
        add_column_if_missing(
            "responses", "employer_contact",
            "ALTER TABLE responses ADD COLUMN employer_contact TEXT DEFAULT NULL",
            cur=cur,
        )
        add_column_if_missing(
            "responses", "source_chat_title",
            "ALTER TABLE responses ADD COLUMN source_chat_title TEXT DEFAULT NULL",
            cur=cur,
        )
        add_column_if_missing(
            "responses", "draft_status",
            "ALTER TABLE responses ADD COLUMN draft_status TEXT DEFAULT 'delivered'",
            cur=cur,
        )
        cur.execute(
            """
            DELETE FROM responses
            WHERE id NOT IN (
                SELECT MIN(id) FROM responses GROUP BY user_id, vacancy_id
            )
            """
        )
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_responses_user_vac "
            "ON responses(user_id, vacancy_id)"
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
        add_column_if_missing(
            "complaints", "admin_response",
            "ALTER TABLE complaints ADD COLUMN admin_response TEXT DEFAULT NULL",
            cur=cur,
        )

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
            CREATE TABLE IF NOT EXISTS chat_suggestions (
                id {serial_pk()},
                user_id INTEGER NOT NULL,
                user_username TEXT,
                chat_link TEXT NOT NULL,
                chat_title TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                admin_note TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                resolved_at TIMESTAMP DEFAULT NULL
            )
        """)
        cur.execute(
            q(
                "CREATE INDEX IF NOT EXISTS idx_chat_suggestions_pending "
                "ON chat_suggestions (chat_link) WHERE status = 'pending'"
            )
            if IS_POSTGRES
            else "CREATE INDEX IF NOT EXISTS idx_chat_suggestions_pending ON chat_suggestions (chat_link, status)"
        )

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
                ("handyman", "Разнорабочий / клининг", "🧹"),
            ]
            cur.executemany(
                q("INSERT INTO categories (code, name, emoji) VALUES (?, ?, ?)"),
                categories,
            )

        _ensure_core_categories(cur)

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

        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS response_pack_requests (
                id {serial_pk()},
                user_id INTEGER NOT NULL,
                username TEXT,
                full_name TEXT,
                phone TEXT,
                status TEXT DEFAULT 'pending',
                receipt_file_id TEXT DEFAULT NULL,
                receipt_kind TEXT DEFAULT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        _seed_channel_settings(cur)

        _migrate_pg_telegram_bigint_ids(cur)

    logger.info(db_info_label())


CHANNEL_SETTING_DEFAULTS = {
    "crosspost_enabled": "1",
    "promo_enabled": "1",
    "hourly_limit_total": "6",
    "hourly_limit_loader": "1",
    "loader_min_rate": "450",
    "quiet_hour_start": "9",
    "quiet_hour_end": "22",
    "promo_times": "09:00,14:00,20:00",
}


def _ensure_core_categories(cur) -> None:
    """Добавляет категории, появившиеся после первого деплоя."""
    extras = [
        ("handyman", "Разнорабочий / клининг", "🧹"),
    ]
    for code, name, emoji in extras:
        cur.execute(
            q(
                """
                INSERT INTO categories (code, name, emoji) VALUES (?, ?, ?)
                ON CONFLICT(code) DO NOTHING
                """
            ),
            (code, name, emoji),
        )


def _seed_channel_settings(cur):
    for key, value in CHANNEL_SETTING_DEFAULTS.items():
        cur.execute(
            q("""
                INSERT INTO channel_settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO NOTHING
            """),
            (key, value),
        )


def get_channel_setting(key: str, default: str | None = None) -> str | None:
    fallback = default if default is not None else CHANNEL_SETTING_DEFAULTS.get(key)
    try:
        row = fetchone("SELECT value FROM channel_settings WHERE key = ?", (key,))
    except Exception:
        return fallback
    if row:
        return row[0]
    return fallback


def set_channel_setting(key: str, value: str):
    execute(
        """
        INSERT INTO channel_settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, str(value)),
    )


def delete_channel_setting(key: str):
    execute("DELETE FROM channel_settings WHERE key = ?", (key,))


def get_channel_promo_texts_from_db() -> list[str] | None:
    import json

    raw = get_channel_setting("promo_texts")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(data, list):
        variants = [str(x).strip() for x in data if str(x).strip()]
    elif isinstance(data, dict):
        items = data.get("variants")
        variants = [str(x).strip() for x in (items or []) if str(x).strip()] if isinstance(items, list) else []
    else:
        return None
    return variants or None


def set_channel_promo_texts_in_db(variants: list[str]):
    import json

    payload = json.dumps({"variants": variants}, ensure_ascii=False)
    set_channel_setting("promo_texts", payload)


def clear_channel_promo_texts_override():
    delete_channel_setting("promo_texts")


def get_channel_settings_dict() -> dict:
    rows = fetchall("SELECT key, value FROM channel_settings")
    merged = dict(CHANNEL_SETTING_DEFAULTS)
    merged.update({r[0]: r[1] for r in rows})
    return merged


def _parse_channel_int(key: str, default: int) -> int:
    raw = get_channel_setting(key, str(default))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def is_channel_crosspost_enabled() -> bool:
    return get_channel_setting("crosspost_enabled", "1") in ("1", "true", "yes")


def is_channel_promo_enabled() -> bool:
    return get_channel_setting("promo_enabled", "1") in ("1", "true", "yes")


def get_channel_hourly_limit_total() -> int:
    return max(0, _parse_channel_int("hourly_limit_total", 6))


def get_channel_hourly_limit_loader() -> int:
    return max(0, _parse_channel_int("hourly_limit_loader", 1))


def get_channel_loader_min_rate() -> int:
    return max(0, _parse_channel_int("loader_min_rate", 450))


def get_channel_quiet_hours() -> tuple[int, int]:
    return _parse_channel_int("quiet_hour_start", 9), _parse_channel_int("quiet_hour_end", 22)


def get_channel_promo_times() -> list[str]:
    raw = get_channel_setting("promo_times", CHANNEL_SETTING_DEFAULTS["promo_times"]) or ""
    return [p.strip() for p in raw.split(",") if p.strip()]


def mark_vacancy_channel_posted(
    vacancy_id: str,
    category_code: str | None = None,
    post_kind: str = "vacancy",
    message_id: int | None = None,
    preview_text: str | None = None,
):
    execute(
        """
        INSERT INTO vacancy_channel_posts
            (vacancy_id, category_code, post_kind, message_id, preview_text, posted_at)
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(vacancy_id) DO UPDATE SET
            category_code = excluded.category_code,
            post_kind = excluded.post_kind,
            message_id = COALESCE(excluded.message_id, vacancy_channel_posts.message_id),
            preview_text = COALESCE(excluded.preview_text, vacancy_channel_posts.preview_text),
            posted_at = CURRENT_TIMESTAMP
        """,
        (vacancy_id, category_code, post_kind, message_id, preview_text),
    )


def record_channel_post(
    post_id: str,
    *,
    post_kind: str,
    category_code: str | None = None,
    message_id: int | None = None,
    preview_text: str | None = None,
):
    mark_vacancy_channel_posted(
        post_id,
        category_code=category_code,
        post_kind=post_kind,
        message_id=message_id,
        preview_text=preview_text,
    )


def record_subscriber_snapshot(member_count: int):
    execute(
        "INSERT INTO channel_subscriber_snapshots (member_count) VALUES (?)",
        (member_count,),
    )


def record_channel_member_event(event_type: str, user_id: int | None = None, username: str | None = None):
    execute(
        """
        INSERT INTO channel_member_events (event_type, user_id, username)
        VALUES (?, ?, ?)
        """,
        (event_type, user_id, username),
    )


def get_subscriber_snapshot_near(days_ago: int = 7) -> int | None:
    row = fetchone(
        f"""
        SELECT member_count FROM channel_subscriber_snapshots
        WHERE recorded_at <= {now_minus_days(days_ago)}
        ORDER BY recorded_at DESC
        LIMIT 1
        """,
    )
    return int(row[0]) if row else None


def get_latest_subscriber_snapshot() -> int | None:
    row = fetchone(
        "SELECT member_count FROM channel_subscriber_snapshots ORDER BY recorded_at DESC LIMIT 1",
    )
    return int(row[0]) if row else None


def count_channel_member_events(event_type: str, days: int = 7) -> int:
    return fetchval(
        f"""
        SELECT COUNT(*) FROM channel_member_events
        WHERE event_type = ? AND event_at >= {now_minus_days(days)}
        """,
        (event_type,),
        default=0,
    )


def get_channel_posts_summary(days: int = 7) -> dict:
    rows = fetchall(
        f"""
        SELECT COALESCE(post_kind, 'vacancy') AS kind, COUNT(*)
        FROM vacancy_channel_posts
        WHERE posted_at >= {now_minus_days(days)}
        GROUP BY COALESCE(post_kind, 'vacancy')
        """,
    )
    summary = {"vacancy": 0, "promo": 0, "custom": 0, "total": 0}
    for kind, cnt in rows:
        summary[kind] = int(cnt)
        summary["total"] += int(cnt)
    return summary


def get_channel_posts_by_hour_msk(days: int = 7) -> list[tuple[int, int]]:
    """Часы МСК (0–23) → число постов за период (наша публикация через бота)."""
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Europe/Moscow")
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = fetchall(
        "SELECT posted_at FROM vacancy_channel_posts WHERE posted_at IS NOT NULL",
    )
    buckets = [0] * 24
    for (posted_at,) in rows:
        dt = _parse_db_timestamp_msk(posted_at)
        if dt is None:
            continue
        if dt.astimezone(timezone.utc) < cutoff:
            continue
        buckets[dt.hour] += 1
    return list(enumerate(buckets))


def count_channel_vacancy_posts_in_msk_hour(category_code: str | None = None) -> int:
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Europe/Moscow")
    now = datetime.now(tz)
    start = now.replace(minute=0, second=0, microsecond=0)
    end = start + timedelta(hours=1)
    rows = fetchall(
        """
        SELECT category_code, posted_at FROM vacancy_channel_posts
        WHERE COALESCE(post_kind, 'vacancy') = 'vacancy' AND message_id IS NOT NULL
        """
    )
    count = 0
    for cat, posted_at in rows:
        if category_code and cat != category_code:
            continue
        dt = _parse_db_timestamp_msk(posted_at)
        if dt and start <= dt < end:
            count += 1
    return count


def count_published_channel_vacancy_posts(category_code: str) -> int:
    """Сколько вакансий категории уже опубликовано в канале (для ротации обложек)."""
    row = fetchone(
        """
        SELECT COUNT(*) FROM vacancy_channel_posts
        WHERE COALESCE(post_kind, 'vacancy') = 'vacancy'
          AND message_id IS NOT NULL
          AND category_code = ?
        """,
        (category_code,),
    )
    return int(row[0]) if row else 0


def _parse_db_timestamp_msk(raw) -> datetime | None:
    from zoneinfo import ZoneInfo

    if raw is None:
        return None
    tz_msk = ZoneInfo("Europe/Moscow")
    if isinstance(raw, datetime):
        dt = raw
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    else:
        s = str(raw).strip()
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s.replace(" ", "T", 1))
        except ValueError:
            try:
                dt = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz_msk)


def is_promo_sent_for_msk_date(promo_slot: str, sent_date: str) -> bool:
    return fetchone(
        "SELECT 1 FROM channel_promo_sent WHERE promo_slot = ? AND sent_date = ?",
        (promo_slot, sent_date),
    ) is not None


def mark_promo_sent(promo_slot: str, sent_date: str):
    execute(
        """
        INSERT INTO channel_promo_sent (promo_slot, sent_date) VALUES (?, ?)
        ON CONFLICT(promo_slot, sent_date) DO NOTHING
        """,
        (promo_slot, sent_date),
    )


def try_reserve_promo_slot(promo_slot: str, sent_date: str) -> bool:
    """Резервирует слот промо до отправки в канал."""
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            q(
                """
                INSERT INTO channel_promo_sent (promo_slot, sent_date)
                VALUES (?, ?)
                ON CONFLICT(promo_slot, sent_date) DO NOTHING
                """
            ),
            (promo_slot, sent_date),
        )
        return cur.rowcount > 0


def release_promo_slot(promo_slot: str, sent_date: str) -> None:
    execute(
        "DELETE FROM channel_promo_sent WHERE promo_slot = ? AND sent_date = ?",
        (promo_slot, sent_date),
    )


def add_subscriber(user_id: int, username: str, first_name: str, last_name: str = None):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    execute(f"""
        INSERT INTO subscribers (user_id, username, first_name, last_name, is_active, last_seen_at)
        VALUES (?, ?, ?, ?, {bool_true()}, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username = excluded.username,
            first_name = excluded.first_name,
            last_name = excluded.last_name,
            is_active = {bool_true()},
            last_seen_at = excluded.last_seen_at
    """, (user_id, username, first_name, last_name, now))


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
               birth_date, resume_extra, photo_storage_path, photo_updated_at, user_role,
               response_credits
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
            "response_credits": int(row[19] or 0) if len(row) > 19 else 0,
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


def get_subscriber_filter_prefs_raw(user_id: int) -> dict | None:
    import json
    from services.filter_prefs import normalize_prefs

    row = fetchone(
        "SELECT prefs_json FROM subscriber_filter_prefs WHERE user_id = ?",
        (user_id,),
    )
    if not row or not row[0]:
        return None
    try:
        data = json.loads(row[0])
    except json.JSONDecodeError:
        return None
    return normalize_prefs(data) if isinstance(data, dict) else None


def set_subscriber_filter_prefs(user_id: int, prefs: dict) -> None:
    import json
    from services.filter_prefs import normalize_prefs

    payload = json.dumps(normalize_prefs(prefs), ensure_ascii=False)
    execute(
        """
        INSERT INTO subscriber_filter_prefs (user_id, prefs_json, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id) DO UPDATE SET
            prefs_json = excluded.prefs_json,
            updated_at = CURRENT_TIMESTAMP
        """,
        (user_id, payload),
    )


def get_subscriber_filter_prefs_effective(user_id: int) -> dict | None:
    """Prefs с миграцией legacy metro_zones; None если Premium не активен."""
    if not is_user_premium(user_id):
        return None
    from services.filter_prefs import merge_metro_zones_into_prefs, migrate_legacy_metro_zones, normalize_prefs

    prefs = get_subscriber_filter_prefs_raw(user_id)
    profile = get_subscriber_profile(user_id)
    metro = (profile or {}).get("metro_zones")
    if prefs is None:
        if metro:
            prefs = normalize_prefs({})
            prefs["geo"] = migrate_legacy_metro_zones(metro)["geo"]
            prefs["apply_to_feed"] = True
            set_subscriber_filter_prefs(user_id, prefs)
            return prefs
        return normalize_prefs({})
    return merge_metro_zones_into_prefs(prefs, metro)


def clear_subscriber_filter_prefs(user_id: int) -> None:
    execute("DELETE FROM subscriber_filter_prefs WHERE user_id = ?", (user_id,))


def list_active_premium_user_ids() -> list[int]:
    rows = fetchall(
        f"""
        SELECT user_id FROM subscribers
        WHERE is_active = {bool_true()} AND plan = 'premium' AND {paid_until_active()}
        ORDER BY user_id
        """,
    )
    return [r[0] for r in rows]


def add_push_digest_pending(user_id: int, vacancy_id: str) -> bool:
    """Добавляет вакансию в очередь digest. False если уже есть."""
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            q(
                "INSERT INTO push_digest_pending (user_id, vacancy_id) VALUES (?, ?) "
                "ON CONFLICT(user_id, vacancy_id) DO NOTHING"
            ),
            (user_id, vacancy_id),
        )
        return cur.rowcount > 0


def count_push_digest_pending(user_id: int) -> int:
    row = fetchone(
        "SELECT COUNT(*) FROM push_digest_pending WHERE user_id = ?",
        (user_id,),
    )
    return int(row[0]) if row else 0


def clear_push_digest_pending(user_id: int) -> int:
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(q("DELETE FROM push_digest_pending WHERE user_id = ?"), (user_id,))
        return cur.rowcount


def add_closed_notice_pending(user_id: int, vacancy_id: str) -> bool:
    """Отложить уведомление «вакансия закрыта» (тихие часы / занят). False если уже в очереди."""
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            q(
                "INSERT INTO closed_notice_pending (user_id, vacancy_id) VALUES (?, ?) "
                "ON CONFLICT(user_id, vacancy_id) DO NOTHING"
            ),
            (user_id, vacancy_id),
        )
        return cur.rowcount > 0


def list_closed_notice_pending(user_id: int) -> list[str]:
    rows = fetchall(
        "SELECT vacancy_id FROM closed_notice_pending WHERE user_id = ? ORDER BY queued_at",
        (user_id,),
    )
    return [r[0] for r in rows]


def remove_closed_notice_pending(user_id: int, vacancy_id: str) -> bool:
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            q("DELETE FROM closed_notice_pending WHERE user_id = ? AND vacancy_id = ?"),
            (user_id, vacancy_id),
        )
        return cur.rowcount > 0


def clear_closed_notice_pending(user_id: int) -> int:
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(q("DELETE FROM closed_notice_pending WHERE user_id = ?"), (user_id,))
        return cur.rowcount


def patch_subscriber_notify_prefs(user_id: int, patch: dict) -> dict:
    """Обновляет notify-блок prefs; возвращает полные prefs."""
    from services.filter_prefs import normalize_prefs

    prefs = get_subscriber_filter_prefs_effective(user_id) or normalize_prefs({})
    notify = dict(prefs.get("notify") or {})
    notify.update(patch)
    prefs["notify"] = notify
    set_subscriber_filter_prefs(user_id, prefs)
    return prefs


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


PREMIUM_EXPIRED_USER_MESSAGE = (
    "⏳ *Premium закончился.*\n\n"
    "• Push отключён\n"
    "• Категории сброшены — в «⚙️ Настройки» выберите *одну* бесплатную\n"
    "• Несколько категорий и push снова — «💎 Подписка»"
)


def reset_premium_feed_settings(user_id: int) -> int:
    """Сброс категорий и фильтра метро при переходе на Free. Возвращает число снятых категорий."""
    cats = get_user_categories(user_id)
    n = len(cats)
    if n:
        set_user_categories(user_id, [])
    set_user_metro_zones(user_id, None)
    return n


def enforce_free_category_limit(user_id: int, free_limit: int) -> bool:
    """
    Если не Premium — не больше free_limit категорий; метро только у Premium.
    Возвращает True, если что-то изменили.
    """
    if is_user_premium(user_id):
        return False
    changed = False
    cats = get_user_categories(user_id)
    codes = [c["code"] for c in cats]
    if len(codes) > free_limit:
        keep = codes[:free_limit] if free_limit > 0 else []
        set_user_categories(user_id, keep)
        changed = True
    profile = get_subscriber_profile(user_id)
    if profile and profile.get("metro_zones"):
        set_user_metro_zones(user_id, None)
        changed = True
    return changed


def _paid_until_key(value) -> str | None:
    """Ключ периода подписки для dedupe напоминаний."""
    dt = _parse_paid_until(value)
    if not dt:
        return None
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


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
    reset_premium_feed_settings(user_id)
    return PREMIUM_EXPIRED_USER_MESSAGE


def list_expired_premium_user_ids() -> list[int]:
    """Активные подписчики с plan=premium и истёкшим paid_until."""
    rows = fetchall(
        f"""
        SELECT user_id FROM subscribers
        WHERE is_active = {bool_true()} AND plan = 'premium' AND paid_until IS NOT NULL
          AND {paid_until_expired()}
        ORDER BY user_id
        """,
    )
    return [r[0] for r in rows]


def list_premium_renewal_reminder_candidates(within_days: int) -> list[dict]:
    """
    Premium/Trial ещё активен, до конца ≤ within_days, напоминание по этому paid_until ещё не слали.
    """
    if within_days <= 0:
        return []
    rows = fetchall(
        f"""
        SELECT user_id, paid_until, trial_used, premium_renewal_warn_for
        FROM subscribers
        WHERE is_active = {bool_true()} AND plan = 'premium' AND paid_until IS NOT NULL
          AND {paid_until_active()}
        ORDER BY paid_until ASC
        """,
    )
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=within_days)
    out: list[dict] = []
    for user_id, paid_until, trial_used, warn_for in rows:
        until = _parse_paid_until(paid_until)
        if not until or until <= now or until > horizon:
            continue
        period_key = _paid_until_key(paid_until)
        if period_key and _paid_until_key(warn_for) == period_key:
            continue
        days_left = max(0, (until - now).days)
        if (until - now).total_seconds() > 0 and days_left == 0:
            days_left = 1
        out.append({
            "user_id": user_id,
            "paid_until": paid_until,
            "trial_used": bool(trial_used),
            "days_left": days_left,
        })
    return out


def mark_premium_renewal_warned(user_id: int, paid_until) -> None:
    execute(
        "UPDATE subscribers SET premium_renewal_warn_for = ? WHERE user_id = ?",
        (paid_until, user_id),
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
    codes = list(dict.fromkeys(category_codes))
    with db_conn() as conn:
        cur = conn.cursor()
        _lock_user_categories(cur, user_id)
        cur.execute(q("DELETE FROM user_categories WHERE user_id = ?"), (user_id,))
        for code in codes:
            cur.execute(
                q("INSERT INTO user_categories (user_id, category_code) VALUES (?, ?)"),
                (user_id, code),
            )


def _user_category_advisory_lock_key(user_id: int) -> int:
    """Ключ для pg_advisory_xact_lock(bigint): Telegram user_id > int32 не влезает в (int, int)."""
    return (1 << 40) | int(user_id)


def _lock_user_categories(cur, user_id: int) -> None:
    if IS_POSTGRES:
        cur.execute(
            "SELECT pg_advisory_xact_lock(%s::bigint)",
            (_user_category_advisory_lock_key(user_id),),
        )
    else:
        cur.execute("BEGIN IMMEDIATE")


def _is_user_premium_cur(cur, user_id: int) -> bool:
    cur.execute(
        q(f"""
        SELECT 1 FROM subscribers
        WHERE user_id = ? AND plan = 'premium'
          AND {paid_until_active()}
        """),
        (user_id,),
    )
    return cur.fetchone() is not None


def toggle_user_category(
    user_id: int,
    category_code: str,
    *,
    free_limit: int,
) -> tuple[list[str], bool]:
    """Атомарное вкл/выкл категории. Возвращает (коды, blocked_by_free_limit)."""
    with db_conn() as conn:
        cur = conn.cursor()
        _lock_user_categories(cur, user_id)
        cur.execute(
            q("SELECT category_code FROM user_categories WHERE user_id = ? ORDER BY category_code"),
            (user_id,),
        )
        current = [r[0] for r in cur.fetchall()]
        if category_code in current:
            cur.execute(
                q("DELETE FROM user_categories WHERE user_id = ? AND category_code = ?"),
                (user_id, category_code),
            )
            current.remove(category_code)
            return current, False
        if not _is_user_premium_cur(cur, user_id) and len(current) >= free_limit:
            return current, True
        cur.execute(
            q("INSERT INTO user_categories (user_id, category_code) VALUES (?, ?)"),
            (user_id, category_code),
        )
        current.append(category_code)
        return current, False


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
                 moderation_status: str = "approved",
                 *, address_normalized: str = None, geo_tags: str = None,
                 rate_hourly: int = None, rate_shift: int = None, min_hours: int = None,
                 rate_effective_hourly: int = None, shift_date: str = None,
                 shift_time_start: str = None, location_lat: float = None,
                 location_lon: float = None, enrichment_version: int = None):
    # PostgreSQL: в ON CONFLICT нужен префикс vacancies. — иначе ambiguous column
    v = "vacancies." if IS_POSTGRES else ""
    mod = moderation_status or "approved"
    execute(f"""
        INSERT INTO vacancies
        (id, source_chat, source_chat_title, category_code, message_text, message_link,
         author_contact, address, is_closed, dedupe_key, published_at,
         poster_user_id, poster_username, poster_display_name, contact_source,
         employer_id, posted_by_bot_user_id, moderation_status,
         address_normalized, geo_tags, rate_hourly, rate_shift, min_hours,
         rate_effective_hourly, shift_date, shift_time_start, location_lat, location_lon,
         enrichment_version)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            moderation_status = COALESCE(excluded.moderation_status, {v}moderation_status),
            address_normalized = COALESCE(excluded.address_normalized, {v}address_normalized),
            geo_tags = COALESCE(excluded.geo_tags, {v}geo_tags),
            rate_hourly = COALESCE(excluded.rate_hourly, {v}rate_hourly),
            rate_shift = COALESCE(excluded.rate_shift, {v}rate_shift),
            min_hours = COALESCE(excluded.min_hours, {v}min_hours),
            rate_effective_hourly = COALESCE(excluded.rate_effective_hourly, {v}rate_effective_hourly),
            shift_date = COALESCE(excluded.shift_date, {v}shift_date),
            shift_time_start = COALESCE(excluded.shift_time_start, {v}shift_time_start),
            location_lat = COALESCE(excluded.location_lat, {v}location_lat),
            location_lon = COALESCE(excluded.location_lon, {v}location_lon),
            enrichment_version = COALESCE(excluded.enrichment_version, {v}enrichment_version)
    """, (
        vacancy_id, source_chat, source_chat_title, category_code, message_text, message_link,
        author_contact, address, is_closed, dedupe_key, published_at,
        poster_user_id, poster_username, poster_display_name, contact_source,
        employer_id, posted_by_bot_user_id, mod,
        address_normalized, geo_tags, rate_hourly, rate_shift, min_hours,
        rate_effective_hourly, shift_date, shift_time_start, location_lat, location_lon,
        enrichment_version,
    ))


def update_vacancy_enrichment(vacancy_id: str, enrichment_kwargs: dict) -> None:
    if not enrichment_kwargs:
        return
    allowed = (
        "address_normalized", "geo_tags", "rate_hourly", "rate_shift", "min_hours",
        "rate_effective_hourly", "shift_date", "shift_time_start",
        "location_lat", "location_lon", "enrichment_version",
    )
    parts = []
    values = []
    for key in allowed:
        if key in enrichment_kwargs and enrichment_kwargs[key] is not None:
            parts.append(f"{key} = ?")
            values.append(enrichment_kwargs[key])
    if not parts:
        return
    values.append(vacancy_id)
    execute(f"UPDATE vacancies SET {', '.join(parts)} WHERE id = ?", tuple(values))


def backfill_vacancy_enrichment(days: int = 3) -> dict[str, int]:
    from parser import detect_category
    from services.vacancy_enrichment import ENRICHMENT_VERSION, enrich_vacancy_text, is_plausible_map_address

    rows = fetchall(
        f"""
        SELECT id, message_text, address, category_code, enrichment_version
        FROM vacancies
        WHERE is_closed = {bool_false()}
          AND found_at >= {now_minus_days(days)}
        ORDER BY found_at DESC
        """,
    )
    enrichment_updated = 0
    recategorized = 0
    for row in rows:
        vid, message_text, address, category_code, enrichment_version = (
            row[0], row[1] or "", row[2], row[3], row[4]
        )
        new_cat = detect_category(message_text)
        if new_cat and new_cat != category_code:
            execute(
                "UPDATE vacancies SET category_code = ? WHERE id = ?",
                (new_cat, vid),
            )
            recategorized += 1
        if enrichment_version is not None and enrichment_version >= ENRICHMENT_VERSION:
            continue
        enrichment = enrich_vacancy_text(message_text, legacy_address=address)
        kwargs = enrichment.to_db_kwargs()
        if enrichment.address_normalized and is_plausible_map_address(enrichment.address_normalized):
            if not address:
                execute(
                    "UPDATE vacancies SET address = ? WHERE id = ? AND (address IS NULL OR address = '')",
                    (enrichment.address_normalized, vid),
                )
            kwargs["address"] = enrichment.address_normalized
        update_vacancy_enrichment(vid, kwargs)
        enrichment_updated += 1
    return {"enrichment": enrichment_updated, "recategorized": recategorized}


_VACANCY_VISIBLE_SQL = "(moderation_status IS NULL OR moderation_status = 'approved')"


def get_vacancy_row(vacancy_id: str):
    return fetchone(
        """SELECT message_text, message_link, source_chat_title, author_contact, address,
                  address_normalized, location_lat, location_lon
           FROM vacancies WHERE id = ?""",
        (vacancy_id,),
    )


def unpack_vacancy_row_basic(row):
    """Первые 5 полей get_vacancy_row() — для откликов (geo-колонки игнорируются)."""
    if not row:
        return None
    return row[0], row[1], row[2], row[3], row[4]


def get_vacancy_push_row(vacancy_id: str):
    return fetchone(
        """SELECT message_text, message_link, source_chat_title, author_contact, address,
                  category_code, source_chat, dedupe_key, published_at, poster_user_id,
                  poster_username, moderation_status, posted_by_bot_user_id,
                  address_normalized, location_lat, location_lon,
                  geo_tags, rate_hourly, rate_shift, rate_effective_hourly,
                  shift_date, shift_time_start
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


def set_vacancy_moderation_if_pending(vacancy_id: str, status: str) -> bool:
    """Меняет статус только если вакансия ещё pending. False — уже обработана."""
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            q(
                "UPDATE vacancies SET moderation_status = ? "
                "WHERE id = ? AND moderation_status = 'pending'"
            ),
            (status, vacancy_id),
        )
        return cur.rowcount > 0


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


def get_general_vacancy_pin(user_id: int) -> dict | None:
    row = fetchone(
        q("""
            SELECT message_id, vacancy_id, card_text
            FROM user_general_vacancy_pin WHERE user_id = ?
        """),
        (user_id,),
    )
    if not row:
        return None
    return {"message_id": row[0], "vacancy_id": row[1], "card_text": row[2]}


def set_general_vacancy_pin(
    user_id: int,
    message_id: int,
    vacancy_id: str,
    card_text: str,
) -> None:
    execute(
        q("""
            INSERT INTO user_general_vacancy_pin (user_id, message_id, vacancy_id, card_text, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                message_id = excluded.message_id,
                vacancy_id = excluded.vacancy_id,
                card_text = excluded.card_text,
                updated_at = CURRENT_TIMESTAMP
        """),
        (user_id, message_id, vacancy_id, card_text),
    )


def clear_general_vacancy_pin(user_id: int) -> None:
    execute(q("DELETE FROM user_general_vacancy_pin WHERE user_id = ?"), (user_id,))


def is_vacancy_channel_posted(vacancy_id: str) -> bool:
    row = fetchone(
        "SELECT 1 FROM vacancy_channel_posts WHERE vacancy_id = ? AND message_id IS NOT NULL",
        (vacancy_id,),
    )
    return bool(row)


def try_reserve_vacancy_channel_post(vacancy_id: str, category_code: str | None = None) -> bool:
    """Резервирует слот в канале до send_message (message_id пока NULL)."""
    if is_vacancy_channel_posted(vacancy_id):
        return False
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            q(
                """
                INSERT INTO vacancy_channel_posts
                    (vacancy_id, category_code, post_kind, message_id, posted_at)
                VALUES (?, ?, 'vacancy', NULL, CURRENT_TIMESTAMP)
                ON CONFLICT(vacancy_id) DO NOTHING
                """
            ),
            (vacancy_id, category_code),
        )
        return cur.rowcount > 0


def release_vacancy_channel_post(vacancy_id: str) -> None:
    execute(
        "DELETE FROM vacancy_channel_posts WHERE vacancy_id = ? AND message_id IS NULL",
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
    """Атомарно помечает покупку Stars как paid. None — уже обработана."""
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            q("UPDATE star_purchases SET status = 'paid' WHERE payload = ? AND status = 'pending'"),
            (payload,),
        )
        if cur.rowcount == 0:
            return None
        cur.execute(
            q("SELECT user_id, vacancy_id, stars_amount FROM star_purchases WHERE payload = ?"),
            (payload,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {"user_id": row[0], "vacancy_id": row[1], "stars_amount": row[2]}


def has_star_purchase_for_vacancy(user_id: int, vacancy_id: str) -> bool:
    """Оплаченный расширенный отклик (ext_resp:)."""
    return has_star_payload_purchase(user_id, vacancy_id, "ext_resp:")


def has_star_payload_purchase(user_id: int, vacancy_id: str, payload_prefix: str) -> bool:
    row = fetchone(
        """
        SELECT 1 FROM star_purchases
        WHERE user_id = ? AND vacancy_id = ? AND status = 'paid'
          AND payload LIKE ?
        """,
        (user_id, vacancy_id, f"{payload_prefix}%"),
    )
    return bool(row)


def has_paid_response_unlock(user_id: int, vacancy_id: str) -> bool:
    """Разовая оплата отклика Stars (resp_pay:)."""
    return has_star_payload_purchase(user_id, vacancy_id, "resp_pay:")


def get_response_credits(user_id: int) -> int:
    row = fetchone("SELECT response_credits FROM subscribers WHERE user_id = ?", (user_id,))
    return int(row[0] or 0) if row else 0


def add_response_credits(user_id: int, amount: int) -> int:
    execute(
        """
        UPDATE subscribers
        SET response_credits = COALESCE(response_credits, 0) + ?
        WHERE user_id = ?
        """,
        (int(amount), user_id),
    )
    return get_response_credits(user_id)


def consume_response_credit(user_id: int) -> bool:
    """Атомарно списать один платный отклик. False — нет кредитов."""
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            q(
                """
                UPDATE subscribers
                SET response_credits = response_credits - 1
                WHERE user_id = ? AND COALESCE(response_credits, 0) >= 1
                """
            ),
            (user_id,),
        )
        return cur.rowcount > 0


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


def get_responses_export_rows(limit: int = 15000) -> list[dict]:
    rows = fetchall(
        q("""
        SELECT r.id, r.responded_at, r.user_id, s.username, s.full_name, s.phone,
               r.vacancy_id, v.category_code, r.source_chat_title, r.employer_contact,
               r.draft_status, r.status, v.is_closed, r.star_boost, r.vacancy_link,
               r.vacancy_text
        FROM responses r
        LEFT JOIN vacancies v ON r.vacancy_id = v.id
        LEFT JOIN subscribers s ON r.user_id = s.user_id
        ORDER BY r.responded_at DESC
        LIMIT ?
        """),
        (limit,),
    )
    return [
        {
            "id": r[0],
            "responded_at": r[1],
            "user_id": r[2],
            "username": r[3],
            "full_name": r[4],
            "phone": r[5],
            "vacancy_id": r[6],
            "category_code": r[7],
            "source_chat_title": r[8],
            "employer_contact": r[9],
            "draft_status": r[10] or "pending",
            "response_status": r[11] or "pending",
            "vacancy_closed": bool(r[12]) if r[12] is not None else False,
            "star_boost": bool(r[13]) if r[13] is not None else False,
            "vacancy_link": r[14],
            "vacancy_text": r[15],
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
        SELECT id, message_text, author_contact, dedupe_key, source_chat_title, category_code
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
            "source_chat_title": row[4] or "",
            "category_code": row[5] or "",
        }
        for row in rows
    ]


def _feed_since_sql(max_hours: int) -> str:
    days = max(1, (int(max_hours) + 23) // 24)
    return now_minus_days(days)


def _map_feed_vacancy_rows(rows) -> list[dict]:
    return [
        {
            "id": r[0], "source": r[1], "text": r[2], "link": r[3], "contact": r[4], "address": r[5],
            "found_at": r[6], "published_at": r[7],
            "address_normalized": r[8], "location_lat": r[9], "location_lon": r[10],
            "category_code": r[11], "geo_tags": r[12],
            "rate_hourly": r[13], "rate_shift": r[14], "rate_effective_hourly": r[15],
            "shift_date": r[16], "shift_time_start": r[17],
        }
        for r in rows
    ]


def get_feed_vacancies_for_user(
    user_id: int,
    category_code: str,
    *,
    max_hours: int | None = None,
) -> list:
    """Открытые вакансии категории, которые пользователь ещё не получал (push/лента)."""
    age_sql = ""
    if max_hours is not None:
        age_sql = f" AND ({vacancy_sort_published_sql()}) >= {_feed_since_sql(max_hours)}"
    rows = fetchall(
        f"""
        SELECT v.id, v.source_chat_title, v.message_text, v.message_link,
               v.author_contact, v.address, v.found_at, v.published_at,
               v.address_normalized, v.location_lat, v.location_lon,
               v.category_code, v.geo_tags, v.rate_hourly, v.rate_shift, v.rate_effective_hourly,
               v.shift_date, v.shift_time_start
        FROM vacancies v
        WHERE v.category_code = ? AND v.is_closed = {bool_false()}
          AND (v.moderation_status IS NULL OR v.moderation_status = 'approved')
          AND NOT EXISTS (
            SELECT 1 FROM sent_vacancies sv
            WHERE sv.user_id = ? AND sv.vacancy_id = v.id
          )
          {age_sql}
        ORDER BY v.found_at DESC
        """,
        (category_code, user_id),
    )
    return _map_feed_vacancy_rows(rows)


def get_feed_vacancies_bulk_for_user(
    user_id: int,
    category_codes: list[str],
    *,
    max_hours: int,
) -> list[dict]:
    """Все непросмотренные вакансии пользователя по списку категорий — один запрос."""
    if not category_codes:
        return []
    placeholders = ",".join("?" * len(category_codes))
    age_sql = f" AND ({vacancy_sort_published_sql()}) >= {_feed_since_sql(max_hours)}"
    rows = fetchall(
        f"""
        SELECT v.id, v.source_chat_title, v.message_text, v.message_link,
               v.author_contact, v.address, v.found_at, v.published_at,
               v.address_normalized, v.location_lat, v.location_lon,
               v.category_code, v.geo_tags, v.rate_hourly, v.rate_shift, v.rate_effective_hourly,
               v.shift_date, v.shift_time_start
        FROM vacancies v
        WHERE v.category_code IN ({placeholders}) AND v.is_closed = {bool_false()}
          AND (v.moderation_status IS NULL OR v.moderation_status = 'approved')
          AND NOT EXISTS (
            SELECT 1 FROM sent_vacancies sv
            WHERE sv.user_id = ? AND sv.vacancy_id = v.id
          )
          {age_sql}
        ORDER BY v.found_at DESC
        """,
        (*category_codes, user_id),
    )
    return _map_feed_vacancy_rows(rows)


def count_history_vacancies_by_categories(
    user_id: int,
    category_codes: list[str],
    *,
    max_hours: int = 720,
) -> dict[str, int]:
    if not category_codes:
        return {}
    placeholders = ",".join("?" * len(category_codes))
    rows = fetchall(
        f"""
        SELECT v.category_code, COUNT(*) FROM sent_vacancies sv
        JOIN vacancies v ON v.id = sv.vacancy_id
        WHERE sv.user_id = ? AND v.category_code IN ({placeholders})
          AND (v.moderation_status IS NULL OR v.moderation_status = 'approved')
          AND sv.sent_at >= {_history_sent_since_sql(max_hours)}
        GROUP BY v.category_code
        """,
        (user_id, *category_codes),
    )
    return {row[0]: int(row[1]) for row in rows}


def _history_sent_since_sql(hours: int) -> str:
    days = max(1, (hours + 23) // 24)
    return now_minus_days(days)


def get_history_vacancies_for_user(
    user_id: int,
    category_code: str,
    *,
    search: str | None = None,
    max_hours: int = 720,
    limit: int = 300,
) -> list:
    """Вакансии, которые уже доставляли пользователю (push/лента)."""
    params: list = [user_id, category_code]
    search_sql = ""
    if search and search.strip():
        search_sql = " AND LOWER(v.message_text) LIKE LOWER(?)"
        params.append(f"%{search.strip()}%")
    params.append(limit)
    rows = fetchall(
        f"""
        SELECT v.id, v.source_chat_title, v.message_text, v.message_link,
               v.author_contact, v.address, v.found_at, v.published_at,
               v.address_normalized, v.location_lat, v.location_lon,
               v.category_code, v.geo_tags, v.rate_hourly, v.rate_shift, v.rate_effective_hourly,
               v.shift_date, v.shift_time_start, v.is_closed, sv.sent_at
        FROM sent_vacancies sv
        JOIN vacancies v ON v.id = sv.vacancy_id
        WHERE sv.user_id = ? AND v.category_code = ?
          AND (v.moderation_status IS NULL OR v.moderation_status = 'approved')
          AND sv.sent_at >= {_history_sent_since_sql(max_hours)}
          {search_sql}
        ORDER BY sv.sent_at DESC
        LIMIT ?
        """,
        tuple(params),
    )
    return [
        {
            "id": r[0], "source": r[1], "text": r[2], "link": r[3], "contact": r[4], "address": r[5],
            "found_at": r[6], "published_at": r[7],
            "address_normalized": r[8], "location_lat": r[9], "location_lon": r[10],
            "category_code": r[11], "geo_tags": r[12],
            "rate_hourly": r[13], "rate_shift": r[14], "rate_effective_hourly": r[15],
            "shift_date": r[16], "shift_time_start": r[17],
            "is_closed": bool(r[18]) if r[18] is not None else False,
            "sent_at": r[19],
        }
        for r in rows
    ]


def count_history_vacancies_for_user(
    user_id: int,
    category_code: str,
    *,
    search: str | None = None,
    max_hours: int = 720,
) -> int:
    params: list = [user_id, category_code]
    search_sql = ""
    if search and search.strip():
        search_sql = " AND LOWER(v.message_text) LIKE LOWER(?)"
        params.append(f"%{search.strip()}%")
    return fetchval(
        f"""
        SELECT COUNT(*) FROM sent_vacancies sv
        JOIN vacancies v ON v.id = sv.vacancy_id
        WHERE sv.user_id = ? AND v.category_code = ?
          AND (v.moderation_status IS NULL OR v.moderation_status = 'approved')
          AND sv.sent_at >= {_history_sent_since_sql(max_hours)}
          {search_sql}
        """,
        tuple(params),
        default=0,
    )


def get_feed_vacancies_by_ids(vacancy_ids: list[str]) -> list[dict]:
    """Вакансии для восстановления ленты после рестарта (порядок ids сохраняется)."""
    if not vacancy_ids:
        return []
    placeholders = ",".join("?" * len(vacancy_ids))
    rows = fetchall(
        f"""
        SELECT id, source_chat_title, message_text, message_link, author_contact, address,
               found_at, published_at, category_code,
               address_normalized, location_lat, location_lon,
               geo_tags, rate_hourly, rate_shift, rate_effective_hourly,
               shift_date, shift_time_start
        FROM vacancies
        WHERE id IN ({placeholders}) AND is_closed = {bool_false()}
        """,
        tuple(vacancy_ids),
    )
    by_id = {
        r[0]: {
            "id": r[0],
            "source": r[1],
            "text": r[2],
            "link": r[3],
            "contact": r[4],
            "address": r[5],
            "found_at": r[6],
            "published_at": r[7],
            "category_code": r[8],
            "address_normalized": r[9],
            "location_lat": r[10],
            "location_lon": r[11],
            "geo_tags": r[12],
            "rate_hourly": r[13],
            "rate_shift": r[14],
            "rate_effective_hourly": r[15],
            "shift_date": r[16],
            "shift_time_start": r[17],
        }
        for r in rows
    }
    return [by_id[vid] for vid in vacancy_ids if vid in by_id]


def save_user_feed_session(
    user_id: int,
    feed_mode: str,
    category_codes: list[str] | None,
    vacancy_ids: list[str],
    page: int = 0,
) -> None:
    import json

    codes_json = json.dumps(category_codes) if category_codes is not None else None
    ids_json = json.dumps(vacancy_ids)
    execute(
        """
        INSERT INTO user_feed_sessions (user_id, feed_mode, category_codes, vacancy_ids, page, updated_at)
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id) DO UPDATE SET
            feed_mode = excluded.feed_mode,
            category_codes = excluded.category_codes,
            vacancy_ids = excluded.vacancy_ids,
            page = excluded.page,
            updated_at = CURRENT_TIMESTAMP
        """,
        (user_id, feed_mode, codes_json, ids_json, page),
    )


def load_user_feed_session(user_id: int) -> dict | None:
    import json

    row = fetchone(
        """
        SELECT feed_mode, category_codes, vacancy_ids, page
        FROM user_feed_sessions WHERE user_id = ?
        """,
        (user_id,),
    )
    if not row:
        return None
    codes = json.loads(row[1]) if row[1] else None
    ids = json.loads(row[2]) if row[2] else []
    if not ids:
        return None
    return {
        "feed_mode": row[0],
        "feed_filter": codes,
        "vacancy_ids": ids,
        "page": row[3] or 0,
    }


def update_user_feed_session_page(user_id: int, page: int) -> None:
    execute(
        """
        UPDATE user_feed_sessions SET page = ?, updated_at = CURRENT_TIMESTAMP
        WHERE user_id = ?
        """,
        (page, user_id),
    )


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
    try_reserve_vacancy_sent_to_user(vacancy_id, user_id)


def try_reserve_vacancy_sent_to_user(vacancy_id: str, user_id: int) -> bool:
    """Резервирует доставку push/ленты до send_message."""
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            q(
                """
                INSERT INTO sent_vacancies (user_id, vacancy_id)
                VALUES (?, ?)
                ON CONFLICT(user_id, vacancy_id) DO NOTHING
                """
            ),
            (user_id, vacancy_id),
        )
        return cur.rowcount > 0


def unreserve_vacancy_sent_to_user(vacancy_id: str, user_id: int) -> None:
    execute(
        "DELETE FROM sent_vacancies WHERE user_id = ? AND vacancy_id = ?",
        (user_id, vacancy_id),
    )


def has_user_received_vacancy(user_id: int, vacancy_id: str) -> bool:
    return fetchone(
        "SELECT 1 FROM sent_vacancies WHERE user_id = ? AND vacancy_id = ?",
        (user_id, vacancy_id),
    ) is not None


def get_users_who_received_vacancy(vacancy_id: str) -> list:
    rows = fetchall("SELECT user_id FROM sent_vacancies WHERE vacancy_id = ?", (vacancy_id,))
    return [row[0] for row in rows]


def get_vacancy_channel_message_id(vacancy_id: str) -> int | None:
    row = fetchone(
        "SELECT message_id FROM vacancy_channel_posts WHERE vacancy_id = ? AND message_id IS NOT NULL",
        (vacancy_id,),
    )
    if not row or row[0] is None:
        return None
    return int(row[0])


def delete_vacancy_completely(vacancy_id: str) -> dict | None:
    """Удаляет вакансию и все связанные записи (push, отклики, канал, ленты)."""
    import json

    if not fetchone("SELECT id FROM vacancies WHERE id = ?", (vacancy_id,)):
        return None

    channel_message_id = get_vacancy_channel_message_id(vacancy_id)
    push_recipients = len(get_users_who_received_vacancy(vacancy_id))
    stats: dict = {
        "vacancy_id": vacancy_id,
        "channel_message_id": channel_message_id,
        "push_recipients": push_recipients,
    }
    ref_tables = (
        "sent_vacancies",
        "responses",
        "complaints",
        "vacancy_notfit_feedback",
        "star_purchases",
        "vacancy_channel_posts",
    )

    with db_conn() as conn:
        cur = conn.cursor()
        for table in ref_tables:
            cur.execute(q(f"DELETE FROM {table} WHERE vacancy_id = ?"), (vacancy_id,))
            stats[f"deleted_{table}"] = cur.rowcount

        feed_updated = 0
        cur.execute(q("SELECT user_id, vacancy_ids FROM user_feed_sessions"))
        for user_id, ids_json in cur.fetchall():
            try:
                ids = json.loads(ids_json) if ids_json else []
            except json.JSONDecodeError:
                continue
            if vacancy_id not in ids:
                continue
            new_ids = [vid for vid in ids if vid != vacancy_id]
            if new_ids:
                cur.execute(
                    q(
                        "UPDATE user_feed_sessions SET vacancy_ids = ?, updated_at = CURRENT_TIMESTAMP "
                        "WHERE user_id = ?"
                    ),
                    (json.dumps(new_ids), user_id),
                )
            else:
                cur.execute(q("DELETE FROM user_feed_sessions WHERE user_id = ?"), (user_id,))
            feed_updated += 1
        stats["feed_sessions_updated"] = feed_updated

        cur.execute(q("DELETE FROM vacancies WHERE id = ?"), (vacancy_id,))
        stats["deleted_vacancy"] = cur.rowcount

    return stats if stats["deleted_vacancy"] else None


def mark_vacancy_closed(message_id: str, chat_id: str):
    """Помечает вакансию закрытой (и кластер cross-channel) → (canonical_id, user_ids)."""
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
        cur.execute(
            q("SELECT message_text, category_code, author_contact FROM vacancies WHERE id = ?"),
            (vacancy_id,),
        )
        vac_row = cur.fetchone()
        cluster_ids = [vacancy_id]
        if vac_row and vac_row[0]:
            try:
                from parser import find_cluster_vacancy_ids

                extra = find_cluster_vacancy_ids(
                    vac_row[0],
                    vac_row[2],
                    vac_row[1],
                    exclude_id=vacancy_id,
                    max_age_days=3,
                )
                cluster_ids.extend(extra)
            except Exception:
                pass
        users: list[int] = []
        seen_users: set[int] = set()
        for vid in cluster_ids:
            cur.execute(q("SELECT user_id FROM sent_vacancies WHERE vacancy_id = ?"), (vid,))
            for r in cur.fetchall():
                if r[0] not in seen_users:
                    seen_users.add(r[0])
                    users.append(r[0])
            cur.execute(q("SELECT user_id FROM responses WHERE vacancy_id = ?"), (vid,))
            for r in cur.fetchall():
                if r[0] not in seen_users:
                    seen_users.add(r[0])
                    users.append(r[0])
        for vid in cluster_ids:
            cur.execute(
                q(f"UPDATE vacancies SET is_closed = {bool_true()} WHERE id = ?"),
                (vid,),
            )
        if legacy_id not in cluster_ids:
            cur.execute(
                q(f"UPDATE vacancies SET is_closed = {bool_true()} WHERE id = ?"),
                (legacy_id,),
            )
    return vacancy_id, users


# ========== ОТКЛИКИ ==========
def add_response(
    user_id: int,
    vacancy_id: str,
    vacancy_text: str = None,
    vacancy_link: str = None,
    user_photo_file_id: str = None,
    *,
    employer_contact: str | None = None,
    source_chat_title: str | None = None,
    draft_status: str = "delivered",
) -> bool:
    """Вставляет отклик. False — пользователь уже откликался (UNIQUE user_id + vacancy_id)."""
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            q(
                """
                INSERT INTO responses (
                    user_id, vacancy_id, vacancy_text, vacancy_link, user_photo_file_id,
                    status, employer_contact, source_chat_title, draft_status
                )
                VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                ON CONFLICT(user_id, vacancy_id) DO NOTHING
                """
            ),
            (
                user_id,
                vacancy_id,
                vacancy_text,
                vacancy_link,
                user_photo_file_id,
                employer_contact,
                source_chat_title,
                draft_status,
            ),
        )
        return cur.rowcount > 0


def update_response_delivery(
    user_id: int,
    vacancy_id: str,
    *,
    draft_status: str,
    vacancy_link: str | None = None,
    user_photo_file_id: str | None = None,
    employer_contact: str | None = None,
    source_chat_title: str | None = None,
) -> None:
    execute(
        """
        UPDATE responses SET
            draft_status = ?,
            vacancy_link = COALESCE(?, vacancy_link),
            user_photo_file_id = COALESCE(?, user_photo_file_id),
            employer_contact = COALESCE(?, employer_contact),
            source_chat_title = COALESCE(?, source_chat_title)
        WHERE user_id = ? AND vacancy_id = ?
        """,
        (
            draft_status,
            vacancy_link,
            user_photo_file_id,
            employer_contact,
            source_chat_title,
            user_id,
            vacancy_id,
        ),
    )


def delete_response(user_id: int, vacancy_id: str):
    execute(
        "DELETE FROM responses WHERE user_id = ? AND vacancy_id = ?",
        (user_id, vacancy_id),
    )


def get_response_record(user_id: int, vacancy_id: str) -> dict | None:
    row = fetchone(
        """
        SELECT r.id, r.responded_at, r.vacancy_id, r.vacancy_text, r.vacancy_link,
               r.status, r.employer_contact, r.source_chat_title, r.draft_status,
               v.is_closed, v.author_contact, v.message_link, v.source_chat_title,
               v.category_code
        FROM responses r
        LEFT JOIN vacancies v ON r.vacancy_id = v.id
        WHERE r.user_id = ? AND r.vacancy_id = ?
        """,
        (user_id, vacancy_id),
    )
    return _map_response_row(row, user_id=user_id) if row else None


def get_response_by_id(response_id: int) -> dict | None:
    row = fetchone(
        """
        SELECT r.id, r.user_id, r.responded_at, r.vacancy_id, r.vacancy_text, r.vacancy_link,
               r.status, r.employer_contact, r.source_chat_title, r.draft_status,
               v.is_closed, v.author_contact, v.message_link, v.source_chat_title,
               v.category_code,
               s.full_name, s.username, s.first_name
        FROM responses r
        LEFT JOIN vacancies v ON r.vacancy_id = v.id
        LEFT JOIN subscribers s ON r.user_id = s.user_id
        WHERE r.id = ?
        """,
        (response_id,),
    )
    if not row:
        return None
    resp = _map_response_row(row[2:15], user_id=row[1], response_id=row[0])
    resp["user_full_name"] = row[15]
    resp["user_username"] = row[16]
    resp["user_first_name"] = row[17]
    return resp


def _map_response_row(row, *, user_id: int, response_id: int | None = None) -> dict:
    if response_id is None:
        response_id = row[0]
        responded_at = row[1]
        vacancy_id = row[2]
        vacancy_text = row[3]
        vacancy_link = row[4]
        status = row[5]
        employer_contact = row[6]
        source_chat_title = row[7]
        draft_status = row[8]
        is_closed = row[9]
        author_contact = row[10]
        message_link = row[11]
        vac_source = row[12] if len(row) > 12 else None
        category_code = row[13] if len(row) > 13 else None
    else:
        responded_at = row[0]
        vacancy_id = row[1]
        vacancy_text = row[2]
        vacancy_link = row[3]
        status = row[4]
        employer_contact = row[5]
        source_chat_title = row[6]
        draft_status = row[7]
        is_closed = row[8]
        author_contact = row[9]
        message_link = row[10]
        vac_source = row[11] if len(row) > 11 else None
        category_code = row[12] if len(row) > 12 else None
    return {
        "id": response_id,
        "user_id": user_id,
        "responded_at": responded_at,
        "vacancy_id": vacancy_id,
        "vacancy_text": vacancy_text or "",
        "vacancy_link": vacancy_link or message_link,
        "status": status or "pending",
        "employer_contact": employer_contact,
        "source_chat_title": source_chat_title or vac_source,
        "draft_status": draft_status or "pending",
        "is_closed": bool(is_closed) if is_closed is not None else False,
        "author_contact": author_contact,
        "category_code": category_code,
    }


def count_admin_responses() -> int:
    return fetchval("SELECT COUNT(*) FROM responses", default=0)


def get_admin_responses(limit: int = 5, offset: int = 0) -> list:
    rows = fetchall(
        """
        SELECT r.id, r.user_id, r.responded_at, r.vacancy_id, r.vacancy_text, r.vacancy_link,
               r.status, r.employer_contact, r.source_chat_title, r.draft_status,
               v.is_closed, v.author_contact, v.message_link, v.source_chat_title,
               v.category_code,
               s.full_name, s.username, s.first_name
        FROM responses r
        LEFT JOIN vacancies v ON r.vacancy_id = v.id
        LEFT JOIN subscribers s ON r.user_id = s.user_id
        ORDER BY r.responded_at DESC
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    )
    out = []
    for row in rows:
        resp = _map_response_row(row[2:15], user_id=row[1], response_id=row[0])
        resp["user_full_name"] = row[15]
        resp["user_username"] = row[16]
        resp["user_first_name"] = row[17]
        out.append(resp)
    return out


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
def add_complaint(user_id: int, vacancy_id: str, reason: str, complaint_text: str = None) -> int:
    with db_conn() as conn:
        cur = conn.cursor()
        if IS_POSTGRES:
            cur.execute(
                q("""
                    INSERT INTO complaints (user_id, vacancy_id, reason, complaint_text)
                    VALUES (?, ?, ?, ?)
                    RETURNING id
                """),
                (user_id, vacancy_id, reason, complaint_text),
            )
            return int(cur.fetchone()[0])
        cur.execute(
            q("""
                INSERT INTO complaints (user_id, vacancy_id, reason, complaint_text)
                VALUES (?, ?, ?, ?)
            """),
            (user_id, vacancy_id, reason, complaint_text),
        )
        return int(cur.lastrowid)


def get_complaint(complaint_id: int) -> dict | None:
    row = fetchone(
        """
        SELECT c.id, c.user_id, s.full_name, s.username, c.vacancy_id,
               c.reason, c.complaint_text, c.created_at, c.resolved, c.admin_response
        FROM complaints c
        LEFT JOIN subscribers s ON c.user_id = s.user_id
        WHERE c.id = ?
        """,
        (complaint_id,),
    )
    if not row:
        return None
    return {
        "id": row[0],
        "user_id": row[1],
        "full_name": row[2],
        "username": row[3],
        "vacancy_id": row[4],
        "reason": row[5],
        "complaint_text": row[6],
        "created_at": row[7],
        "resolved": bool(row[8]) if row[8] is not None else False,
        "admin_response": row[9],
    }


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


def mark_complaint_answered(complaint_id: int, answer_text: str):
    execute(
        f"UPDATE complaints SET resolved = {bool_true()}, admin_response = ? WHERE id = ?",
        (answer_text, complaint_id),
    )


# ========== ПОДДЕРЖКА ==========
def add_support_request(user_id: int, message_text: str, username: str = None) -> int:
    with db_conn() as conn:
        cur = conn.cursor()
        if IS_POSTGRES:
            cur.execute(
                q("""
                    INSERT INTO support_requests (user_id, message_text, user_username)
                    VALUES (?, ?, ?)
                    RETURNING id
                """),
                (user_id, message_text, username),
            )
            return int(cur.fetchone()[0])
        cur.execute(
            q("""
                INSERT INTO support_requests (user_id, message_text, user_username)
                VALUES (?, ?, ?)
            """),
            (user_id, message_text, username),
        )
        return int(cur.lastrowid)


def get_support_request(request_id: int) -> dict | None:
    row = fetchone(
        f"""
        SELECT id, user_id, user_username, message_text, created_at, answered, admin_response
        FROM support_requests WHERE id = ?
        """,
        (request_id,),
    )
    if not row:
        return None
    return {
        "id": row[0],
        "user_id": row[1],
        "username": row[2],
        "message_text": row[3],
        "created_at": row[4],
        "answered": bool(row[5]) if row[5] is not None else False,
        "admin_response": row[6],
    }


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


def target_chat_is_active(chat_link: str) -> bool:
    if not table_exists("target_chats"):
        return False
    row = fetchone(
        f"SELECT is_active FROM target_chats WHERE chat_link = ?",
        (chat_link,),
    )
    return bool(row and row[0])


def activate_target_chat(chat_link: str) -> bool:
    """Добавить чат в мониторинг или снова включить is_active."""
    if not table_exists("target_chats"):
        return False
    if target_chat_is_active(chat_link):
        return True
    row = fetchone("SELECT id FROM target_chats WHERE chat_link = ?", (chat_link,))
    if row:
        execute(
            f"UPDATE target_chats SET is_active = {bool_true()} WHERE chat_link = ?",
            (chat_link,),
        )
        return True
    try:
        execute("INSERT INTO target_chats (chat_link) VALUES (?)", (chat_link,))
        return True
    except IntegrityError:
        execute(
            f"UPDATE target_chats SET is_active = {bool_true()} WHERE chat_link = ?",
            (chat_link,),
        )
        return True


def _chat_suggestion_row(row) -> dict | None:
    if not row:
        return None
    return {
        "id": row[0],
        "user_id": row[1],
        "user_username": row[2],
        "chat_link": row[3],
        "chat_title": row[4],
        "status": row[5],
        "admin_note": row[6],
        "created_at": row[7],
        "resolved_at": row[8],
    }


def get_chat_suggestion(suggestion_id: int) -> dict | None:
    if not table_exists("chat_suggestions"):
        return None
    row = fetchone(
        """
        SELECT id, user_id, user_username, chat_link, chat_title, status,
               admin_note, created_at, resolved_at
        FROM chat_suggestions WHERE id = ?
        """,
        (suggestion_id,),
    )
    return _chat_suggestion_row(row)


def get_pending_chat_suggestion_for_link(chat_link: str) -> dict | None:
    if not table_exists("chat_suggestions"):
        return None
    row = fetchone(
        """
        SELECT id, user_id, user_username, chat_link, chat_title, status,
               admin_note, created_at, resolved_at
        FROM chat_suggestions
        WHERE chat_link = ? AND status = 'pending'
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (chat_link,),
    )
    return _chat_suggestion_row(row)


def count_user_chat_suggestions_since(user_id: int, hours: int = 24) -> int:
    if not table_exists("chat_suggestions"):
        return 0
    return fetchval(
        f"""
        SELECT COUNT(*) FROM chat_suggestions
        WHERE user_id = ? AND created_at >= {now_minus_days(max(1, (hours + 23) // 24))}
        """,
        (user_id,),
        default=0,
    )


def count_pending_chat_suggestions() -> int:
    if not table_exists("chat_suggestions"):
        return 0
    return fetchval(
        "SELECT COUNT(*) FROM chat_suggestions WHERE status = 'pending'",
        default=0,
    )


def create_chat_suggestion(
    user_id: int,
    chat_link: str,
    *,
    user_username: str | None = None,
    chat_title: str | None = None,
) -> int | None:
    if not table_exists("chat_suggestions"):
        return None
    with db_conn() as conn:
        cur = conn.cursor()
        if IS_POSTGRES:
            cur.execute(
                q("""
                    INSERT INTO chat_suggestions
                    (user_id, user_username, chat_link, chat_title, status)
                    VALUES (?, ?, ?, ?, 'pending')
                    RETURNING id
                """),
                (user_id, user_username, chat_link, chat_title),
            )
            return int(cur.fetchone()[0])
        cur.execute(
            q("""
                INSERT INTO chat_suggestions
                (user_id, user_username, chat_link, chat_title, status)
                VALUES (?, ?, ?, ?, 'pending')
            """),
            (user_id, user_username, chat_link, chat_title),
        )
        return int(cur.lastrowid)


def resolve_chat_suggestion(
    suggestion_id: int,
    status: str,
    *,
    admin_note: str | None = None,
) -> bool:
    if status not in ("approved", "rejected"):
        return False
    if not table_exists("chat_suggestions"):
        return False
    row = get_chat_suggestion(suggestion_id)
    if not row or row["status"] != "pending":
        return False
    execute(
        """
        UPDATE chat_suggestions
        SET status = ?, admin_note = ?, resolved_at = CURRENT_TIMESTAMP
        WHERE id = ? AND status = 'pending'
        """,
        (status, admin_note, suggestion_id),
    )
    return True


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


def count_user_sent_vacancies(user_id: int) -> int:
    return fetchval(
        "SELECT COUNT(*) FROM sent_vacancies WHERE user_id = ?",
        (user_id,),
        default=0,
    )


def count_user_notfit_feedback(user_id: int) -> int:
    return fetchval(
        "SELECT COUNT(*) FROM vacancy_notfit_feedback WHERE user_id = ?",
        (user_id,),
        default=0,
    )


def set_subscriber_active(user_id: int, is_active: bool):
    flag = bool_true() if is_active else bool_false()
    execute(f"UPDATE subscribers SET is_active = {flag} WHERE user_id = ?", (user_id,))


def get_subscriber_registered_at(user_id: int):
    row = fetchone("SELECT registered_at FROM subscribers WHERE user_id = ?", (user_id,))
    return row[0] if row else None


def get_user_responses(user_id: int, limit: int = 5, offset: int = 0) -> list:
    rows = fetchall(
        """
        SELECT r.id, r.responded_at, r.vacancy_id, r.vacancy_text, r.vacancy_link,
               r.status, r.employer_contact, r.source_chat_title, r.draft_status,
               v.is_closed, v.author_contact, v.message_link, v.source_chat_title,
               v.category_code
        FROM responses r
        LEFT JOIN vacancies v ON r.vacancy_id = v.id
        WHERE r.user_id = ?
        ORDER BY r.responded_at DESC
        LIMIT ? OFFSET ?
        """,
        (user_id, limit, offset),
    )
    return [_map_response_row(row, user_id=user_id) for row in rows]


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


def get_vacancy_counts_by_chat(days: int = 7) -> list[dict]:
    """Вакансии по source_chat_title за последние N дней."""
    with db_conn(commit=False) as conn:
        cur = conn.cursor()
        cur.execute(
            q(f"""
                SELECT source_chat_title, COUNT(*) AS cnt
                FROM vacancies
                WHERE source_chat_title IS NOT NULL
                  AND source_chat_title != ''
                  AND found_at >= {now_minus_days(days)}
                GROUP BY source_chat_title
                ORDER BY cnt DESC
            """),
        )
        return [{"source_chat_title": r[0], "count": r[1]} for r in cur.fetchall()]


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


def log_bot_event(user_id: int | None, event: str, meta: dict | None = None) -> None:
    meta_json = json.dumps(meta, ensure_ascii=False) if meta else None
    execute(
        q("INSERT INTO bot_events (user_id, event, meta_json) VALUES (?, ?, ?)"),
        (user_id, event, meta_json),
    )
    if user_id:
        try:
            touch_subscriber_last_seen(user_id)
        except Exception:
            pass


def count_bot_events_since(event: str, since_utc: datetime) -> int:
    row = fetchone(
        q("SELECT COUNT(*) FROM bot_events WHERE event = ? AND created_at >= ?"),
        (event, since_utc),
    )
    return int(row[0]) if row else 0


def count_bot_events_grouped_since(since_utc: datetime) -> dict[str, int]:
    rows = fetchall(
        q("""
            SELECT event, COUNT(*) FROM bot_events
            WHERE created_at >= ?
            GROUP BY event
        """),
        (since_utc,),
    )
    return {r[0]: int(r[1]) for r in rows}


def count_distinct_active_users_since(since_utc: datetime) -> int:
    row = fetchone(
        q("""
            SELECT COUNT(DISTINCT user_id) FROM bot_events
            WHERE user_id IS NOT NULL AND created_at >= ?
        """),
        (since_utc,),
    )
    return int(row[0]) if row and row[0] else 0


def count_reg_validation_fails_since(user_id: int, since_utc: datetime) -> int:
    return count_bot_events_since_for_user(user_id, "reg_validation_fail", since_utc)


def count_bot_events_since_for_user(user_id: int, event: str, since_utc: datetime) -> int:
    row = fetchone(
        q("""
            SELECT COUNT(*) FROM bot_events
            WHERE user_id = ? AND event = ? AND created_at >= ?
        """),
        (user_id, event, since_utc),
    )
    return int(row[0]) if row else 0


def touch_subscriber_last_seen(user_id: int) -> None:
    from datetime import datetime, timezone

    execute(
        q("UPDATE subscribers SET last_seen_at = ? WHERE user_id = ?"),
        (datetime.now(timezone.utc), user_id),
    )


def count_subscribers_last_seen_since(since_utc: datetime) -> int:
    row = fetchone(
        q(f"""
            SELECT COUNT(DISTINCT user_id) FROM (
                SELECT user_id FROM subscribers
                WHERE is_active = {bool_true()} AND last_seen_at >= ?
                UNION
                SELECT user_id FROM bot_events
                WHERE user_id IS NOT NULL AND created_at >= ?
            ) active_users
        """),
        (since_utc, since_utc),
    )
    return int(row[0]) if row else 0


def count_responses_since(since_utc: datetime) -> int:
    row = fetchone(
        q("SELECT COUNT(*) FROM responses WHERE responded_at >= ?"),
        (since_utc,),
    )
    return int(row[0]) if row else 0


def count_support_since(since_utc: datetime) -> int:
    row = fetchone(
        q("SELECT COUNT(*) FROM support_requests WHERE created_at >= ?"),
        (since_utc,),
    )
    return int(row[0]) if row else 0


def count_complaints_since(since_utc: datetime) -> int:
    row = fetchone(
        q("SELECT COUNT(*) FROM complaints WHERE created_at >= ?"),
        (since_utc,),
    )
    return int(row[0]) if row else 0


def get_scheduler_flag(key: str) -> str | None:
    row = fetchone(q("SELECT flag_value FROM app_scheduler_flags WHERE flag_key = ?"), (key,))
    return row[0] if row else None


def set_scheduler_flag(key: str, value: str) -> None:
    if IS_POSTGRES:
        execute(
            """
            INSERT INTO app_scheduler_flags (flag_key, flag_value, updated_at)
            VALUES (%s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (flag_key) DO UPDATE SET
                flag_value = EXCLUDED.flag_value,
                updated_at = CURRENT_TIMESTAMP
            """,
            (key, value),
        )
    else:
        execute(
            q("""
                INSERT INTO app_scheduler_flags (flag_key, flag_value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(flag_key) DO UPDATE SET
                    flag_value = excluded.flag_value,
                    updated_at = CURRENT_TIMESTAMP
            """),
            (key, value),
        )


def mark_reg_stuck_notified(user_id: int) -> None:
    execute(
        q("UPDATE subscribers SET reg_stuck_notified_at = CURRENT_TIMESTAMP WHERE user_id = ?"),
        (user_id,),
    )


def get_stuck_registrations(*, older_than_hours: int = 24) -> list[dict]:
    """Профиль не завершён или нет категорий после анкеты."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=older_than_hours)
    stuck: list[dict] = []
    with db_conn(commit=False) as conn:
        cur = conn.cursor()
        cur.execute(
            q(f"""
                SELECT user_id, username, first_name, user_role, registered_at
                FROM subscribers
                WHERE is_active = {bool_true()}
                  AND (full_name IS NULL OR TRIM(full_name) = '')
                  AND registered_at < ?
                  AND (reg_stuck_notified_at IS NULL OR reg_stuck_notified_at < registered_at)
            """),
            (cutoff,),
        )
        for row in cur.fetchall():
            stuck.append({
                "user_id": row[0],
                "username": row[1],
                "first_name": row[2],
                "user_role": row[3] or "candidate",
                "registered_at": row[4],
                "reason": "не завершил анкету",
            })
        cur.execute(
            q(f"""
                SELECT s.user_id, s.username, s.first_name, s.user_role, s.registered_at
                FROM subscribers s
                LEFT JOIN user_categories uc ON uc.user_id = s.user_id
                WHERE s.is_active = {bool_true()}
                  AND s.user_role = 'candidate'
                  AND s.full_name IS NOT NULL AND TRIM(s.full_name) != ''
                  AND s.phone IS NOT NULL AND TRIM(s.phone) != ''
                  AND (s.reg_stuck_notified_at IS NULL OR s.reg_stuck_notified_at < s.registered_at)
                GROUP BY s.user_id, s.username, s.first_name, s.user_role, s.registered_at
                HAVING COUNT(uc.category_code) = 0
                   AND MAX(s.registered_at) < ?
            """),
            (cutoff,),
        )
        for row in cur.fetchall():
            stuck.append({
                "user_id": row[0],
                "username": row[1],
                "first_name": row[2],
                "user_role": row[3] or "candidate",
                "registered_at": row[4],
                "reason": "не выбрал категории",
            })
    return stuck


def get_activity_digest_data(*, since_utc: datetime) -> dict:
    events = count_bot_events_grouped_since(since_utc)
    return {
        "events": events,
        "active_users_events": count_distinct_active_users_since(since_utc),
        "active_users_seen": count_subscribers_last_seen_since(since_utc),
        "responses": count_responses_since(since_utc),
        "support": count_support_since(since_utc),
        "complaints": count_complaints_since(since_utc),
        "stats": get_admin_stats(),
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
        user_ids = [row[0] for row in rows]
        cats_by_user: dict[int, list[str]] = {uid: [] for uid in user_ids}
        if user_ids:
            placeholders = ",".join("?" * len(user_ids))
            cur.execute(
                q(f"""
                    SELECT uc.user_id, c.emoji, c.name
                    FROM user_categories uc
                    JOIN categories c ON c.code = uc.category_code
                    WHERE uc.user_id IN ({placeholders})
                    ORDER BY c.name
                """),
                tuple(user_ids),
            )
            for uid, emoji, name in cur.fetchall():
                cats_by_user.setdefault(uid, []).append(f"{emoji} {name}")
        cards = []
        for row in rows:
            user_id = row[0]
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
                "categories": cats_by_user.get(user_id, []),
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
    """Монотонно продвигает курсор чата (max), не откатывает при reject старых id."""
    mid = int(message_id)
    if IS_POSTGRES:
        conflict_set = (
            "last_message_id = GREATEST(last_processed.last_message_id, EXCLUDED.last_message_id),"
        )
    else:
        conflict_set = (
            "last_message_id = MAX(last_processed.last_message_id, excluded.last_message_id),"
        )
    execute(
        f"""
        INSERT INTO last_processed (chat_id, last_message_id, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(chat_id) DO UPDATE SET
            {conflict_set}
            updated_at = CURRENT_TIMESTAMP
        """,
        (chat_id, mid),
    )


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


def approve_premium_request(request_id: int) -> dict | None:
    """Атомарно одобряет один pending-запрос. None — уже обработан."""
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            q(
                "UPDATE premium_requests SET status = 'approved' "
                "WHERE id = ? AND status = 'pending'"
            ),
            (request_id,),
        )
        if cur.rowcount == 0:
            return None
        cur.execute(q(f"{_PREMIUM_REQUEST_SELECT} WHERE id = ?"), (request_id,))
        row = cur.fetchone()
        return _premium_request_row(row) if row else None


def reject_premium_request(request_id: int) -> int | None:
    """Атомарно отклоняет pending-запрос. Возвращает user_id или None."""
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            q(
                "UPDATE premium_requests SET status = 'rejected' "
                "WHERE id = ? AND status = 'pending'"
            ),
            (request_id,),
        )
        if cur.rowcount == 0:
            return None
        cur.execute(q("SELECT user_id FROM premium_requests WHERE id = ?"), (request_id,))
        row = cur.fetchone()
        return row[0] if row else None


# ========== ПАКЕТЫ ОТКЛИКОВ (рубли) ==========

def _response_pack_request_row(row) -> dict:
    return {
        "id": row[0],
        "user_id": row[1],
        "username": row[2],
        "full_name": row[3],
        "phone": row[4],
        "status": row[5],
        "receipt_file_id": row[6] if len(row) > 6 else None,
        "receipt_kind": row[7] if len(row) > 7 else None,
        "created_at": row[8] if len(row) > 8 else None,
    }


_RESPONSE_PACK_SELECT = """
    SELECT id, user_id, username, full_name, phone, status,
           receipt_file_id, receipt_kind, created_at
    FROM response_pack_requests
"""


def add_response_pack_request(
    user_id: int,
    username: str | None = None,
    full_name: str | None = None,
    phone: str | None = None,
) -> int:
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            q(
                "UPDATE response_pack_requests SET status = 'cancelled' "
                "WHERE user_id = ? AND status IN ('pending', 'awaiting_receipt')"
            ),
            (user_id,),
        )
        if IS_POSTGRES:
            cur.execute(
                q("""
                    INSERT INTO response_pack_requests
                    (user_id, username, full_name, phone, status)
                    VALUES (?, ?, ?, ?, 'awaiting_receipt')
                    RETURNING id
                """),
                (user_id, username, full_name, phone),
            )
            return int(cur.fetchone()[0])
        cur.execute(
            q("""
                INSERT INTO response_pack_requests
                (user_id, username, full_name, phone, status)
                VALUES (?, ?, ?, ?, 'awaiting_receipt')
            """),
            (user_id, username, full_name, phone),
        )
        return int(cur.lastrowid)


def get_response_pack_request(request_id: int) -> dict | None:
    row = fetchone(f"{_RESPONSE_PACK_SELECT} WHERE id = ?", (request_id,))
    return _response_pack_request_row(row) if row else None


def attach_response_pack_receipt(
    request_id: int,
    user_id: int,
    file_id: str,
    kind: str,
) -> bool:
    row = fetchone(
        f"{_RESPONSE_PACK_SELECT} WHERE id = ? AND user_id = ? AND status = 'awaiting_receipt'",
        (request_id, user_id),
    )
    if not row:
        return False
    execute(
        """
        UPDATE response_pack_requests
        SET receipt_file_id = ?, receipt_kind = ?, status = 'pending'
        WHERE id = ?
        """,
        (file_id, kind, request_id),
    )
    return True


def cancel_response_pack_awaiting(user_id: int, request_id: int | None = None) -> None:
    if request_id:
        execute(
            "UPDATE response_pack_requests SET status = 'cancelled' "
            "WHERE id = ? AND user_id = ? AND status = 'awaiting_receipt'",
            (request_id, user_id),
        )
    else:
        execute(
            "UPDATE response_pack_requests SET status = 'cancelled' "
            "WHERE user_id = ? AND status = 'awaiting_receipt'",
            (user_id,),
        )


def get_pending_response_pack_requests(limit: int = 20) -> list:
    rows = fetchall(
        f"""
        {_RESPONSE_PACK_SELECT}
        WHERE status = 'pending'
        ORDER BY created_at ASC
        LIMIT ?
        """,
        (limit,),
    )
    return [_response_pack_request_row(r) for r in rows]


def count_pending_response_pack_requests() -> int:
    return fetchval(
        "SELECT COUNT(*) FROM response_pack_requests WHERE status = 'pending'",
        default=0,
    )


def approve_response_pack_request(request_id: int, credits: int) -> dict | None:
    """Атомарно одобряет запрос и начисляет кредиты. None — уже обработан."""
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            q(
                "UPDATE response_pack_requests SET status = 'approved' "
                "WHERE id = ? AND status = 'pending'"
            ),
            (request_id,),
        )
        if cur.rowcount == 0:
            return None
        cur.execute(q(f"{_RESPONSE_PACK_SELECT} WHERE id = ?"), (request_id,))
        row = cur.fetchone()
        if not row:
            return None
        req = _response_pack_request_row(row)
        cur.execute(
            q(
                """
                UPDATE subscribers
                SET response_credits = COALESCE(response_credits, 0) + ?
                WHERE user_id = ?
                """
            ),
            (int(credits), req["user_id"]),
        )
        req["credits_added"] = int(credits)
        cur.execute(
            q("SELECT COALESCE(response_credits, 0) FROM subscribers WHERE user_id = ?"),
            (req["user_id"],),
        )
        bal_row = cur.fetchone()
        req["balance"] = int(bal_row[0] or 0) if bal_row else int(credits)
        return req


def reject_response_pack_request(request_id: int) -> int | None:
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            q(
                "UPDATE response_pack_requests SET status = 'rejected' "
                "WHERE id = ? AND status = 'pending'"
            ),
            (request_id,),
        )
        if cur.rowcount == 0:
            return None
        cur.execute(q("SELECT user_id FROM response_pack_requests WHERE id = ?"), (request_id,))
        row = cur.fetchone()
        return row[0] if row else None
