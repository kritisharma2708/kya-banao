import sqlite3
import json
import os
from datetime import datetime, date, timedelta
from contextlib import contextmanager

DB_PATH = os.getenv("DATABASE_URL", "kya_banao.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True) if os.path.dirname(DB_PATH) else None


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db():
    with conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                name TEXT,
                chat_id INTEGER,
                onboarded INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS profiles (
                user_id INTEGER PRIMARY KEY,
                diet_type TEXT,
                allergies TEXT,
                health_goals TEXT,
                loved_cuisines TEXT,
                disliked_cuisines TEXT,
                spice_tolerance TEXT,
                other_notes TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );

            CREATE TABLE IF NOT EXISTS cook_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_date DATE NOT NULL,
                status TEXT NOT NULL,
                meals_affected TEXT,
                reported_by INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS meals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                meal_date DATE NOT NULL,
                meal_type TEXT,
                source TEXT,
                description TEXT,
                cuisine TEXT,
                reported_by INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS tokens (
                user_id TEXT PRIMARY KEY,
                access_token TEXT,
                refresh_token TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                user_name TEXT,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id);

            CREATE TABLE IF NOT EXISTS household_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                fact TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_facts_chat ON household_facts(chat_id);
        """)

        # Migrations: cook_events originally had no chat_id/notes columns
        cols = {row[1] for row in c.execute("PRAGMA table_info(cook_events)").fetchall()}
        if "chat_id" not in cols:
            c.execute("ALTER TABLE cook_events ADD COLUMN chat_id INTEGER")
        if "notes" not in cols:
            c.execute("ALTER TABLE cook_events ADD COLUMN notes TEXT")


def upsert_user(user_id: int, name: str, chat_id: int):
    with conn() as c:
        c.execute("""
            INSERT INTO users (user_id, name, chat_id)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                name = excluded.name,
                chat_id = excluded.chat_id
        """, (user_id, name, chat_id))


def get_user(user_id: int):
    with conn() as c:
        row = c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def mark_onboarded(user_id: int):
    with conn() as c:
        c.execute("UPDATE users SET onboarded = 1 WHERE user_id = ?", (user_id,))


def save_profile(user_id: int, profile: dict):
    with conn() as c:
        c.execute("""
            INSERT INTO profiles (
                user_id, diet_type, allergies, health_goals,
                loved_cuisines, disliked_cuisines, spice_tolerance, other_notes,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                diet_type = excluded.diet_type,
                allergies = excluded.allergies,
                health_goals = excluded.health_goals,
                loved_cuisines = excluded.loved_cuisines,
                disliked_cuisines = excluded.disliked_cuisines,
                spice_tolerance = excluded.spice_tolerance,
                other_notes = excluded.other_notes,
                updated_at = CURRENT_TIMESTAMP
        """, (
            user_id,
            profile.get("diet_type"),
            profile.get("allergies"),
            profile.get("health_goals"),
            json.dumps(profile.get("loved_cuisines", [])),
            json.dumps(profile.get("disliked_cuisines", [])),
            profile.get("spice_tolerance"),
            profile.get("other_notes"),
        ))


def get_profile(user_id: int):
    with conn() as c:
        row = c.execute("SELECT * FROM profiles WHERE user_id = ?", (user_id,)).fetchone()
        if not row:
            return None
        p = dict(row)
        p["loved_cuisines"] = json.loads(p["loved_cuisines"]) if p["loved_cuisines"] else []
        p["disliked_cuisines"] = json.loads(p["disliked_cuisines"]) if p["disliked_cuisines"] else []
        return p


def get_household_profiles(chat_id: int):
    with conn() as c:
        rows = c.execute("""
            SELECT u.user_id, u.name, p.*
            FROM users u
            LEFT JOIN profiles p ON u.user_id = p.user_id
            WHERE u.chat_id = ?
        """, (chat_id,)).fetchall()
        return [dict(r) for r in rows]


def save_message(chat_id: int, role: str, content: str, user_name: str = None):
    with conn() as c:
        c.execute(
            "INSERT INTO messages (chat_id, role, user_name, content) VALUES (?, ?, ?, ?)",
            (chat_id, role, user_name, content),
        )


def get_recent_messages(chat_id: int, limit: int = 20):
    """Return last N messages for chat in chronological order."""
    with conn() as c:
        rows = c.execute("""
            SELECT role, user_name, content
            FROM messages
            WHERE chat_id = ?
            ORDER BY id DESC
            LIMIT ?
        """, (chat_id, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]


def add_household_fact(chat_id: int, fact: str):
    with conn() as c:
        c.execute(
            "INSERT INTO household_facts (chat_id, fact) VALUES (?, ?)",
            (chat_id, fact),
        )


def get_household_facts(chat_id: int):
    with conn() as c:
        rows = c.execute(
            "SELECT fact FROM household_facts WHERE chat_id = ? ORDER BY id",
            (chat_id,),
        ).fetchall()
        return [r["fact"] for r in rows]


def log_cook_leave(chat_id: int, start_date: str, end_date: str, reason: str = ""):
    """Set cook on leave for each day in the inclusive range. Replaces any existing
    entry for that date so Remy can correct himself if needed."""
    start = datetime.fromisoformat(start_date).date()
    end = datetime.fromisoformat(end_date).date()
    with conn() as c:
        d = start
        while d <= end:
            c.execute(
                "DELETE FROM cook_events WHERE chat_id = ? AND event_date = ?",
                (chat_id, d.isoformat()),
            )
            c.execute(
                "INSERT INTO cook_events (chat_id, event_date, status, notes) VALUES (?, ?, ?, ?)",
                (chat_id, d.isoformat(), "on_leave", reason or None),
            )
            d += timedelta(days=1)


def get_upcoming_cook_events(chat_id: int, days_ahead: int = 21):
    today = date.today().isoformat()
    cutoff = (date.today() + timedelta(days=days_ahead)).isoformat()
    with conn() as c:
        rows = c.execute("""
            SELECT event_date, status, notes
            FROM cook_events
            WHERE chat_id = ? AND event_date >= ? AND event_date <= ?
            ORDER BY event_date
        """, (chat_id, today, cutoff)).fetchall()
        return [dict(r) for r in rows]
