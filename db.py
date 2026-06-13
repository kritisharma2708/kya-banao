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

            CREATE TABLE IF NOT EXISTS weekly_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                week_start DATE NOT NULL,
                plan_json TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(chat_id, week_start)
            );

            CREATE INDEX IF NOT EXISTS idx_weekly_plans_chat ON weekly_plans(chat_id, week_start);

            CREATE TABLE IF NOT EXISTS friend_recommendations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                item TEXT NOT NULL,
                source TEXT,
                notes TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                consumed_at TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_friend_recs_chat_status
                ON friend_recommendations(chat_id, status);

            -- Multi-tenant: per-chat OAuth tokens, replacing the global
            -- .swiggy_tokens.json file. One row per household chat.
            CREATE TABLE IF NOT EXISTS tenant_tokens (
                chat_id INTEGER PRIMARY KEY,
                tokens_json TEXT,
                client_info_json TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- Multi-tenant: per-chat settings (default Instamart address,
            -- future per-chat config). One row per household chat.
            CREATE TABLE IF NOT EXISTS tenant_settings (
                chat_id INTEGER PRIMARY KEY,
                default_address_id TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
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


def get_chat_ids_with_onboarded_users():
    """Return distinct chat_ids that have at least one onboarded user.
    Used by the weekly plan cron to know which households to send to."""
    with conn() as c:
        rows = c.execute(
            "SELECT DISTINCT chat_id FROM users WHERE onboarded = 1 AND chat_id IS NOT NULL"
        ).fetchall()
        return [r["chat_id"] for r in rows]


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


def save_weekly_plan(chat_id: int, week_start: str, plan: dict):
    """Store the structured weekly plan. week_start is ISO date (YYYY-MM-DD).
    Replaces any existing plan for the same chat+week_start."""
    with conn() as c:
        c.execute("""
            INSERT INTO weekly_plans (chat_id, week_start, plan_json)
            VALUES (?, ?, ?)
            ON CONFLICT(chat_id, week_start) DO UPDATE SET
                plan_json = excluded.plan_json,
                created_at = CURRENT_TIMESTAMP
        """, (chat_id, week_start, json.dumps(plan, ensure_ascii=False)))


def get_meals_for_date(chat_id: int, day: str) -> dict | None:
    """Return {breakfast, lunch, dinner} for a given ISO date, drawing from
    whichever weekly plan covers it. Returns None if no plan covers this day."""
    with conn() as c:
        rows = c.execute("""
            SELECT plan_json FROM weekly_plans
            WHERE chat_id = ?
            ORDER BY week_start DESC
            LIMIT 5
        """, (chat_id,)).fetchall()
    for r in rows:
        plan = json.loads(r["plan_json"])
        meals = (plan.get("dates") or {}).get(day)
        if meals:
            return meals
    return None


def get_latest_weekly_plan(chat_id: int) -> dict | None:
    """Return the most recent stored weekly plan, or None."""
    with conn() as c:
        row = c.execute("""
            SELECT plan_json FROM weekly_plans
            WHERE chat_id = ?
            ORDER BY week_start DESC LIMIT 1
        """, (chat_id,)).fetchone()
    return json.loads(row["plan_json"]) if row else None


def get_recent_weekly_plans(chat_id: int, limit: int = 2) -> list[dict]:
    """Return up to `limit` recent weekly plans, newest first."""
    with conn() as c:
        rows = c.execute("""
            SELECT plan_json FROM weekly_plans
            WHERE chat_id = ?
            ORDER BY week_start DESC LIMIT ?
        """, (chat_id, limit)).fetchall()
    out = []
    for r in rows:
        try:
            out.append(json.loads(r["plan_json"]))
        except Exception:
            continue
    return out


def add_friend_recommendation(chat_id: int, item: str, source: str = "", notes: str = ""):
    """Queue a friend/family recommendation for the weekly discovery nudge."""
    with conn() as c:
        c.execute("""
            INSERT INTO friend_recommendations (chat_id, item, source, notes)
            VALUES (?, ?, ?, ?)
        """, (chat_id, item, source or None, notes or None))


def get_pending_friend_recs(chat_id: int, limit: int = 5):
    with conn() as c:
        rows = c.execute("""
            SELECT id, item, source, notes, created_at
            FROM friend_recommendations
            WHERE chat_id = ? AND status = 'pending'
            ORDER BY id ASC LIMIT ?
        """, (chat_id, limit)).fetchall()
        return [dict(r) for r in rows]


def mark_friend_rec_consumed(rec_id: int):
    with conn() as c:
        c.execute("""
            UPDATE friend_recommendations
            SET status = 'consumed', consumed_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (rec_id,))


# ---------------------------------------------------------------------------
# Multi-tenant token + settings storage
# ---------------------------------------------------------------------------

def set_tenant_tokens(chat_id: int, tokens: dict | None, client_info: dict | None):
    """Upsert OAuth tokens + dynamic-registration client info for one chat."""
    with conn() as c:
        c.execute("""
            INSERT INTO tenant_tokens (chat_id, tokens_json, client_info_json, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(chat_id) DO UPDATE SET
                tokens_json = excluded.tokens_json,
                client_info_json = excluded.client_info_json,
                updated_at = CURRENT_TIMESTAMP
        """, (
            chat_id,
            json.dumps(tokens) if tokens is not None else None,
            json.dumps(client_info) if client_info is not None else None,
        ))


def get_tenant_tokens(chat_id: int) -> tuple[dict | None, dict | None]:
    """Return (tokens, client_info) parsed from the row, or (None, None)."""
    with conn() as c:
        row = c.execute("""
            SELECT tokens_json, client_info_json
            FROM tenant_tokens WHERE chat_id = ?
        """, (chat_id,)).fetchone()
    if not row:
        return None, None
    tokens = json.loads(row["tokens_json"]) if row["tokens_json"] else None
    client_info = json.loads(row["client_info_json"]) if row["client_info_json"] else None
    return tokens, client_info


def has_tenant_tokens(chat_id: int) -> bool:
    tokens, _ = get_tenant_tokens(chat_id)
    return bool(tokens and tokens.get("access_token"))


def set_default_address(chat_id: int, address_id: str):
    with conn() as c:
        c.execute("""
            INSERT INTO tenant_settings (chat_id, default_address_id, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(chat_id) DO UPDATE SET
                default_address_id = excluded.default_address_id,
                updated_at = CURRENT_TIMESTAMP
        """, (chat_id, address_id))


def get_default_address(chat_id: int) -> str | None:
    """Per-chat default delivery address. Falls back to the legacy
    SWIGGY_DEFAULT_ADDRESS_ID env var so existing single-tenant Railway
    deployments keep working without manual data migration."""
    with conn() as c:
        row = c.execute("""
            SELECT default_address_id FROM tenant_settings WHERE chat_id = ?
        """, (chat_id,)).fetchone()
    if row and row["default_address_id"]:
        return row["default_address_id"]
    return os.getenv("SWIGGY_DEFAULT_ADDRESS_ID") or None
