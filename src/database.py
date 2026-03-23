"""
SQLite database layer.
Stores trackers with booking context + price history.
"""
import json
import logging
import os
import sqlite3

logger = logging.getLogger(__name__)
DB_PATH = os.getenv("DB_PATH", "hotel_tracker.db")


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def _ensure_column(conn: sqlite3.Connection, table: str, name: str, ddl: str):
    if name not in _column_names(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def init_db():
    with _conn() as c:
        c.execute("PRAGMA foreign_keys = ON")
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id             TEXT PRIMARY KEY,
                email          TEXT UNIQUE NOT NULL,
                password_hash  TEXT NOT NULL,
                created_at     TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS trackers (
                id                  TEXT PRIMARY KEY,
                label               TEXT,
                url                 TEXT NOT NULL,
                checkin             TEXT,
                checkout            TEXT,
                paid_price          REAL,
                currency            TEXT DEFAULT '₪',
                price_selector      TEXT,
                meal_plan           TEXT DEFAULT 'none',
                require_free_cancel INTEGER DEFAULT 0,
                room_keyword        TEXT DEFAULT '',
                reserve_duty_only   INTEGER DEFAULT 0,
                club_membership_only INTEGER DEFAULT 0,
                threshold_pct       REAL DEFAULT 0,
                alert_direction     TEXT DEFAULT 'down',
                notes               TEXT,
                purchase_date       TEXT,
                alternative_urls    TEXT DEFAULT '[]',
                ntfy_topic          TEXT,
                active              INTEGER DEFAULT 1,
                created_at          TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS price_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                tracker_id  TEXT NOT NULL,
                url         TEXT,
                price       REAL NOT NULL,
                checked_at  TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (tracker_id) REFERENCES trackers(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_ph_tracker_url ON price_history(tracker_id, url, checked_at DESC);
            """
        )

        # Backward-compatible migrations for older DB files.
        _ensure_column(c, "trackers", "meal_plan", "TEXT DEFAULT 'none'")
        _ensure_column(c, "trackers", "require_free_cancel", "INTEGER DEFAULT 0")
        _ensure_column(c, "trackers", "room_keyword", "TEXT DEFAULT ''")
        _ensure_column(c, "trackers", "reserve_duty_only", "INTEGER DEFAULT 0")
        _ensure_column(c, "trackers", "club_membership_only", "INTEGER DEFAULT 0")
        _ensure_column(c, "trackers", "threshold_pct", "REAL DEFAULT 0")
        _ensure_column(c, "trackers", "alert_direction", "TEXT DEFAULT 'down'")
        _ensure_column(c, "trackers", "notes", "TEXT")
        _ensure_column(c, "trackers", "purchase_date", "TEXT")
        _ensure_column(c, "trackers", "alternative_urls", "TEXT DEFAULT '[]'")
        _ensure_column(c, "trackers", "user_id", "TEXT REFERENCES users(id) ON DELETE CASCADE")

        # If legacy boolean breakfast column exists, map it to meal_plan once.
        cols = _column_names(c, "trackers")
        if "require_breakfast" in cols:
            c.execute(
                """
                UPDATE trackers
                SET meal_plan = 'breakfast'
                WHERE COALESCE(require_breakfast, 0) = 1
                  AND COALESCE(meal_plan, 'none') = 'none'
                """
            )

    logger.info("[DB] Initialized at %s", DB_PATH)


def create_user(user_id: str, email: str, password_hash: str):
    with _conn() as c:
        c.execute(
            "INSERT INTO users (id, email, password_hash) VALUES (?, ?, ?)",
            (user_id, email, password_hash),
        )


def get_user_by_email(email: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE email = ?", (email.lower().strip(),)).fetchone()
    return dict(row) if row else None


def get_user_by_id(user_id: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def _decode_tracker(row: sqlite3.Row) -> dict:
    item = dict(row)
    try:
        item["alternative_urls"] = json.loads(item.get("alternative_urls") or "[]")
    except json.JSONDecodeError:
        item["alternative_urls"] = []
    return item


def get_all_trackers(user_id: str | None = None) -> list[dict]:
    with _conn() as c:
        if user_id is not None:
            rows = c.execute(
                """
                SELECT * FROM trackers
                WHERE active = 1 AND user_id = ?
                ORDER BY created_at DESC
                """,
                (user_id,),
            ).fetchall()
        else:
            rows = c.execute("SELECT * FROM trackers WHERE active = 1 ORDER BY created_at DESC").fetchall()
    return [_decode_tracker(r) for r in rows]


def get_tracker(user_id: str, tracker_id: str) -> dict | None:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM trackers WHERE id = ? AND user_id = ?",
            (tracker_id, user_id),
        ).fetchone()
    return _decode_tracker(row) if row else None


def upsert_tracker(data: dict):
    payload = dict(data)
    user_id = payload.get("user_id")
    tid = payload.get("id")
    if not tid:
        raise ValueError("Tracker id is required.")

    # Backward compatibility for old clients.
    if payload.get("meal_plan") in (None, ""):
        if payload.get("require_breakfast"):
            payload["meal_plan"] = "breakfast"
        else:
            payload["meal_plan"] = "none"

    if isinstance(payload.get("alternative_urls"), list):
        payload["alternative_urls"] = json.dumps(payload["alternative_urls"], ensure_ascii=False)

    fields = [
        "id",
        "user_id",
        "label",
        "url",
        "checkin",
        "checkout",
        "paid_price",
        "currency",
        "price_selector",
        "meal_plan",
        "require_free_cancel",
        "room_keyword",
        "reserve_duty_only",
        "club_membership_only",
        "threshold_pct",
        "alert_direction",
        "notes",
        "purchase_date",
        "alternative_urls",
        "ntfy_topic",
        "active",
    ]

    with _conn() as c:
        existing = c.execute("SELECT user_id FROM trackers WHERE id = ?", (tid,)).fetchone()
        if existing:
            ex_uid = existing["user_id"]
            if ex_uid and user_id and ex_uid != user_id:
                raise PermissionError("Tracker belongs to another account.")
            # Keep owner on update; allow setting user_id on legacy rows with NULL when caller supplies it.
            payload["user_id"] = user_id or ex_uid or payload.get("user_id")
        else:
            default_uid = os.getenv("IMPORT_USER_ID")
            if not payload.get("user_id"):
                payload["user_id"] = user_id or default_uid
            if not payload.get("user_id"):
                raise ValueError("user_id is required for new trackers.")

        cols = [f for f in fields if f in payload]
        vals = [payload[c] for c in cols]

        placeholders = ", ".join(["?"] * len(cols))
        updates = ", ".join([f"{c} = excluded.{c}" for c in cols if c != "id"])

        sql = f"""
            INSERT INTO trackers ({', '.join(cols)}) VALUES ({placeholders})
            ON CONFLICT(id) DO UPDATE SET {updates}
        """
        c.execute(sql, vals)


def delete_tracker(tid: str, user_id: str | None = None) -> bool:
    """Returns True if a row was deleted."""
    with _conn() as c:
        if user_id is not None:
            cur = c.execute("DELETE FROM trackers WHERE id = ? AND user_id = ?", (tid, user_id))
        else:
            cur = c.execute("DELETE FROM trackers WHERE id = ?", (tid,))
        return cur.rowcount > 0


def save_price(tracker_id: str, price: float, url: str = ""):
    with _conn() as c:
        c.execute(
            "INSERT INTO price_history (tracker_id, url, price) VALUES (?, ?, ?)",
            (tracker_id, url or "", price),
        )


def get_price_history(tracker_id: str, limit: int = 30) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            """
            SELECT price, checked_at, url
            FROM price_history
            WHERE tracker_id = ?
            ORDER BY checked_at DESC
            LIMIT ?
            """,
            (tracker_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_last_price(tracker_id: str, url: str = "") -> float | None:
    with _conn() as c:
        if url:
            row = c.execute(
                """
                SELECT price
                FROM price_history
                WHERE tracker_id = ? AND url = ?
                ORDER BY checked_at DESC
                LIMIT 1
                """,
                (tracker_id, url),
            ).fetchone()
        else:
            row = c.execute(
                """
                SELECT price
                FROM price_history
                WHERE tracker_id = ?
                ORDER BY checked_at DESC
                LIMIT 1
                """,
                (tracker_id,),
            ).fetchone()
    return row["price"] if row else None
