"""
Database layer with dual backend support:
- PostgreSQL via DATABASE_URL (recommended for cloud hosting)
- SQLite fallback via DB_PATH (local development)
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

from url_sanitize import sanitize_url

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "hotel_tracker.db")
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()

try:
    import psycopg
    from psycopg.rows import dict_row
except Exception:  # pragma: no cover
    psycopg = None
    dict_row = None


def _use_postgres() -> bool:
    return DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")


def _ph() -> str:
    return "%s" if _use_postgres() else "?"


def _conn():
    if _use_postgres():
        if psycopg is None:
            raise RuntimeError("DATABASE_URL is set but psycopg is not installed.")
        return psycopg.connect(DATABASE_URL, row_factory=dict_row)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _column_names_sqlite(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def _column_names_pg(conn: Any, table: str) -> set[str]:
    rows = conn.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        """,
        (table,),
    ).fetchall()
    return {r["column_name"] for r in rows}


def _ensure_column_sqlite(conn: sqlite3.Connection, table: str, name: str, ddl: str):
    if name not in _column_names_sqlite(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def init_db():
    if _use_postgres():
        with _conn() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id             TEXT PRIMARY KEY,
                    email          TEXT UNIQUE NOT NULL,
                    password_hash  TEXT NOT NULL,
                    created_at     TIMESTAMPTZ DEFAULT NOW()
                )
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS trackers (
                    id                   TEXT PRIMARY KEY,
                    user_id              TEXT REFERENCES users(id) ON DELETE CASCADE,
                    label                TEXT,
                    url                  TEXT NOT NULL,
                    checkin              TEXT,
                    checkout             TEXT,
                    paid_price           DOUBLE PRECISION,
                    currency             TEXT DEFAULT '₪',
                    price_selector       TEXT,
                    meal_plan            TEXT DEFAULT 'none',
                    require_free_cancel  INTEGER DEFAULT 0,
                    room_keyword         TEXT DEFAULT '',
                    reserve_duty_only    INTEGER DEFAULT 0,
                    club_membership_only INTEGER DEFAULT 0,
                    threshold_pct        DOUBLE PRECISION DEFAULT 0,
                    alert_direction      TEXT DEFAULT 'down',
                    notes                TEXT,
                    purchase_date        TEXT,
                    alternative_urls     TEXT DEFAULT '[]',
                    ntfy_topic           TEXT,
                    active               INTEGER DEFAULT 1,
                    created_at           TIMESTAMPTZ DEFAULT NOW()
                )
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS price_history (
                    id          BIGSERIAL PRIMARY KEY,
                    tracker_id  TEXT NOT NULL REFERENCES trackers(id) ON DELETE CASCADE,
                    url         TEXT,
                    price       DOUBLE PRECISION NOT NULL,
                    checked_at  TIMESTAMPTZ DEFAULT NOW()
                )
                """
            )
            c.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_ph_tracker_url
                ON price_history(tracker_id, url, checked_at DESC)
                """
            )
            # Defensive migrations for existing PG tables
            c.execute("ALTER TABLE trackers ADD COLUMN IF NOT EXISTS user_id TEXT REFERENCES users(id) ON DELETE CASCADE")
            c.execute("ALTER TABLE trackers ADD COLUMN IF NOT EXISTS meal_plan TEXT DEFAULT 'none'")
            c.execute("ALTER TABLE trackers ADD COLUMN IF NOT EXISTS require_free_cancel INTEGER DEFAULT 0")
            c.execute("ALTER TABLE trackers ADD COLUMN IF NOT EXISTS room_keyword TEXT DEFAULT ''")
            c.execute("ALTER TABLE trackers ADD COLUMN IF NOT EXISTS reserve_duty_only INTEGER DEFAULT 0")
            c.execute("ALTER TABLE trackers ADD COLUMN IF NOT EXISTS club_membership_only INTEGER DEFAULT 0")
            c.execute("ALTER TABLE trackers ADD COLUMN IF NOT EXISTS threshold_pct DOUBLE PRECISION DEFAULT 0")
            c.execute("ALTER TABLE trackers ADD COLUMN IF NOT EXISTS alert_direction TEXT DEFAULT 'down'")
            c.execute("ALTER TABLE trackers ADD COLUMN IF NOT EXISTS notes TEXT")
            c.execute("ALTER TABLE trackers ADD COLUMN IF NOT EXISTS purchase_date TEXT")
            c.execute("ALTER TABLE trackers ADD COLUMN IF NOT EXISTS alternative_urls TEXT DEFAULT '[]'")

            cols = _column_names_pg(c, "trackers")
            if "require_breakfast" in cols:
                c.execute(
                    """
                    UPDATE trackers
                    SET meal_plan = 'breakfast'
                    WHERE COALESCE(require_breakfast, 0) = 1
                      AND COALESCE(meal_plan, 'none') = 'none'
                    """
                )
            c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_reset_token TEXT")
            c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_reset_expires TIMESTAMPTZ")
        logger.info("[DB] Initialized PostgreSQL at DATABASE_URL")
        return

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
                id                   TEXT PRIMARY KEY,
                user_id              TEXT REFERENCES users(id) ON DELETE CASCADE,
                label                TEXT,
                url                  TEXT NOT NULL,
                checkin              TEXT,
                checkout             TEXT,
                paid_price           REAL,
                currency             TEXT DEFAULT '₪',
                price_selector       TEXT,
                meal_plan            TEXT DEFAULT 'none',
                require_free_cancel  INTEGER DEFAULT 0,
                room_keyword         TEXT DEFAULT '',
                reserve_duty_only    INTEGER DEFAULT 0,
                club_membership_only INTEGER DEFAULT 0,
                threshold_pct        REAL DEFAULT 0,
                alert_direction      TEXT DEFAULT 'down',
                notes                TEXT,
                purchase_date        TEXT,
                alternative_urls     TEXT DEFAULT '[]',
                ntfy_topic           TEXT,
                active               INTEGER DEFAULT 1,
                created_at           TEXT DEFAULT (datetime('now'))
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
        _ensure_column_sqlite(c, "trackers", "meal_plan", "TEXT DEFAULT 'none'")
        _ensure_column_sqlite(c, "trackers", "require_free_cancel", "INTEGER DEFAULT 0")
        _ensure_column_sqlite(c, "trackers", "room_keyword", "TEXT DEFAULT ''")
        _ensure_column_sqlite(c, "trackers", "reserve_duty_only", "INTEGER DEFAULT 0")
        _ensure_column_sqlite(c, "trackers", "club_membership_only", "INTEGER DEFAULT 0")
        _ensure_column_sqlite(c, "trackers", "threshold_pct", "REAL DEFAULT 0")
        _ensure_column_sqlite(c, "trackers", "alert_direction", "TEXT DEFAULT 'down'")
        _ensure_column_sqlite(c, "trackers", "notes", "TEXT")
        _ensure_column_sqlite(c, "trackers", "purchase_date", "TEXT")
        _ensure_column_sqlite(c, "trackers", "alternative_urls", "TEXT DEFAULT '[]'")
        _ensure_column_sqlite(c, "trackers", "user_id", "TEXT REFERENCES users(id) ON DELETE CASCADE")
        _ensure_column_sqlite(c, "users", "password_reset_token", "TEXT")
        _ensure_column_sqlite(c, "users", "password_reset_expires", "TEXT")

        cols = _column_names_sqlite(c, "trackers")
        if "require_breakfast" in cols:
            c.execute(
                """
                UPDATE trackers
                SET meal_plan = 'breakfast'
                WHERE COALESCE(require_breakfast, 0) = 1
                  AND COALESCE(meal_plan, 'none') = 'none'
                """
            )

    logger.info("[DB] Initialized SQLite at %s", DB_PATH)


def create_user(user_id: str, email: str, password_hash: str):
    q = _ph()
    with _conn() as c:
        c.execute(
            f"INSERT INTO users (id, email, password_hash) VALUES ({q}, {q}, {q})",
            (user_id, email, password_hash),
        )


def get_user_by_email(email: str) -> dict | None:
    q = _ph()
    with _conn() as c:
        row = c.execute(f"SELECT * FROM users WHERE email = {q}", (email.lower().strip(),)).fetchone()
    return dict(row) if row else None


def get_user_by_id(user_id: str) -> dict | None:
    q = _ph()
    with _conn() as c:
        row = c.execute(f"SELECT * FROM users WHERE id = {q}", (user_id,)).fetchone()
    return dict(row) if row else None


def _parse_expires_value(val) -> datetime | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        dt = val
    else:
        s = str(val).replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def set_password_reset_token(user_id: str, token: str, expires_at: datetime) -> None:
    q = _ph()
    exp_val: Any = expires_at
    if not _use_postgres():
        exp_val = expires_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    with _conn() as c:
        c.execute(
            f"UPDATE users SET password_reset_token = {q}, password_reset_expires = {q} WHERE id = {q}",
            (token, exp_val, user_id),
        )


def get_user_by_valid_reset_token(token: str) -> dict | None:
    if not token:
        return None
    q = _ph()
    with _conn() as c:
        row = c.execute(f"SELECT * FROM users WHERE password_reset_token = {q}", (token,)).fetchone()
    if not row:
        return None
    u = dict(row)
    exp = _parse_expires_value(u.get("password_reset_expires"))
    if exp is None or datetime.now(timezone.utc) > exp:
        return None
    return u


def update_user_password_clear_reset(user_id: str, password_hash: str) -> None:
    q = _ph()
    with _conn() as c:
        c.execute(
            f"""
            UPDATE users
            SET password_hash = {q}, password_reset_token = NULL, password_reset_expires = NULL
            WHERE id = {q}
            """,
            (password_hash, user_id),
        )


def _decode_tracker(row: Any) -> dict:
    item = dict(row)
    if item.get("url"):
        item["url"] = sanitize_url(item["url"])
    try:
        raw_alt = json.loads(item.get("alternative_urls") or "[]")
    except json.JSONDecodeError:
        raw_alt = []
    cleaned_alt = []
    for x in raw_alt:
        cleaned_alt.append(sanitize_url(x) if isinstance(x, str) else x)
    item["alternative_urls"] = cleaned_alt
    return item


def get_all_trackers(user_id: str | None = None) -> list[dict]:
    q = _ph()
    with _conn() as c:
        if user_id is not None:
            rows = c.execute(
                f"""
                SELECT * FROM trackers
                WHERE active = 1 AND user_id = {q}
                ORDER BY created_at DESC
                """,
                (user_id,),
            ).fetchall()
        else:
            rows = c.execute("SELECT * FROM trackers WHERE active = 1 ORDER BY created_at DESC").fetchall()
    return [_decode_tracker(r) for r in rows]


def get_tracker(user_id: str, tracker_id: str) -> dict | None:
    q = _ph()
    with _conn() as c:
        row = c.execute(
            f"SELECT * FROM trackers WHERE id = {q} AND user_id = {q}",
            (tracker_id, user_id),
        ).fetchone()
    return _decode_tracker(row) if row else None


def upsert_tracker(data: dict):
    payload = dict(data)
    user_id = payload.get("user_id")
    tid = payload.get("id")
    if not tid:
        raise ValueError("Tracker id is required.")

    if payload.get("meal_plan") in (None, ""):
        payload["meal_plan"] = "breakfast" if payload.get("require_breakfast") else "none"

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
    q = _ph()

    with _conn() as c:
        existing = c.execute(f"SELECT user_id FROM trackers WHERE id = {q}", (tid,)).fetchone()
        if existing:
            ex_uid = existing["user_id"]
            if ex_uid and user_id and ex_uid != user_id:
                raise PermissionError("Tracker belongs to another account.")
            payload["user_id"] = user_id or ex_uid or payload.get("user_id")
        else:
            default_uid = os.getenv("IMPORT_USER_ID")
            if not payload.get("user_id"):
                payload["user_id"] = user_id or default_uid
            if not payload.get("user_id"):
                raise ValueError("user_id is required for new trackers.")

        cols = [f for f in fields if f in payload]
        vals = [payload[c] for c in cols]
        placeholders = ", ".join([q] * len(cols))
        updates = ", ".join([f"{c} = excluded.{c}" for c in cols if c != "id"])

        sql = f"""
            INSERT INTO trackers ({', '.join(cols)}) VALUES ({placeholders})
            ON CONFLICT(id) DO UPDATE SET {updates}
        """
        c.execute(sql, vals)


def delete_tracker(tid: str, user_id: str | None = None) -> bool:
    q = _ph()
    with _conn() as c:
        if user_id is not None:
            cur = c.execute(f"DELETE FROM trackers WHERE id = {q} AND user_id = {q}", (tid, user_id))
        else:
            cur = c.execute(f"DELETE FROM trackers WHERE id = {q}", (tid,))
        return cur.rowcount > 0


def save_price(tracker_id: str, price: float, url: str = ""):
    q = _ph()
    with _conn() as c:
        c.execute(
            f"INSERT INTO price_history (tracker_id, url, price) VALUES ({q}, {q}, {q})",
            (tracker_id, url or "", price),
        )


def get_price_history(tracker_id: str, limit: int = 30) -> list[dict]:
    q = _ph()
    with _conn() as c:
        rows = c.execute(
            f"""
            SELECT price, checked_at, url
            FROM price_history
            WHERE tracker_id = {q}
            ORDER BY checked_at DESC
            LIMIT {q}
            """,
            (tracker_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_last_price(tracker_id: str, url: str = "") -> float | None:
    q = _ph()
    with _conn() as c:
        if url:
            row = c.execute(
                f"""
                SELECT price
                FROM price_history
                WHERE tracker_id = {q} AND url = {q}
                ORDER BY checked_at DESC
                LIMIT 1
                """,
                (tracker_id, url),
            ).fetchone()
        else:
            row = c.execute(
                f"""
                SELECT price
                FROM price_history
                WHERE tracker_id = {q}
                ORDER BY checked_at DESC
                LIMIT 1
                """,
                (tracker_id,),
            ).fetchone()
    return row["price"] if row else None
