import sqlite3

DB_NAME = "sent_messages.db"


def _migrate_if_needed(conn: sqlite3.Connection) -> None:
    """
    Старые версии создавали PK только по message_id.
    Это ломает парсинг, т.к. в разных чатах message_id повторяются.
    Нужен составной ключ (message_id, chat_id).
    """
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(sent_messages)")
    cols = cur.fetchall()
    if not cols:
        return
    # Формат row: cid, name, type, notnull, dflt_value, pk
    pk_cols = [r[1] for r in cols if int(r[5] or 0) > 0]
    if pk_cols == ["message_id", "chat_id"]:
        return

    cur.execute("ALTER TABLE sent_messages RENAME TO sent_messages_old")
    cur.execute(
        """
        CREATE TABLE sent_messages (
            message_id TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (message_id, chat_id)
        )
        """
    )
    cur.execute(
        """
        INSERT OR IGNORE INTO sent_messages (message_id, chat_id, sent_at)
        SELECT message_id, COALESCE(chat_id, ''), sent_at
        FROM sent_messages_old
        """
    )
    cur.execute("DROP TABLE sent_messages_old")


def init_db():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sent_messages (
            message_id TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (message_id, chat_id)
        )
        """
    )
    _migrate_if_needed(conn)
    conn.commit()
    conn.close()

def is_message_sent(message_id, chat_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM sent_messages WHERE message_id = ? AND chat_id = ?", (message_id, chat_id))
    exists = cur.fetchone() is not None
    conn.close()
    return exists

def mark_message_sent(message_id, chat_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO sent_messages (message_id, chat_id) VALUES (?, ?)",
        (message_id, chat_id),
    )
    conn.commit()
    conn.close()