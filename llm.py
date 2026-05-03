import os
from pathlib import Path
from anthropic import Anthropic
from dotenv import load_dotenv

import db

load_dotenv()

client = Anthropic()
MODEL = "claude-sonnet-4-6"

PROJECT_DIR = Path(__file__).parent
SOUL = (PROJECT_DIR / "soul.md").read_text()
USER_LORE = (PROJECT_DIR / "user.md").read_text()


def build_household_context(chat_id: int) -> str:
    profiles = db.get_household_profiles(chat_id)
    if not profiles:
        return "No household profiles yet."

    lines = ["Live household state (from current onboarding records):"]
    for p in profiles:
        if not p.get("diet_type"):
            lines.append(f"- {p['name']}: not yet onboarded")
            continue
        loves = ", ".join(p["loved_cuisines"]) if p.get("loved_cuisines") else "none recorded"
        avoids = ", ".join(p["disliked_cuisines"]) if p.get("disliked_cuisines") else "none recorded"
        lines.append(
            f"- {p['name']}: {p['diet_type']}, spice {p['spice_tolerance'] or 'none recorded'}, "
            f"loves {loves}, avoids {avoids}, "
            f"goals: {p.get('health_goals') or 'none recorded'}, "
            f"allergies: {p.get('allergies') or 'none recorded'}, "
            f"notes: {p.get('other_notes') or 'none recorded'}"
        )
    return "\n".join(lines)


HISTORY_TURNS = 20


def _format_user_message(user_name: str, text: str) -> str:
    return f"[{user_name}] {text}"


def _build_messages(chat_id: int, user_name: str, current_message: str):
    history = db.get_recent_messages(chat_id, limit=HISTORY_TURNS)
    messages = []
    for m in history:
        if m["role"] == "user":
            label = m["user_name"] or "user"
            messages.append({"role": "user", "content": f"[{label}] {m['content']}"})
        else:
            messages.append({"role": "assistant", "content": m["content"]})
    messages.append({"role": "user", "content": _format_user_message(user_name, current_message)})

    # Anthropic API requires alternating user/assistant; collapse consecutive same-role
    # messages by joining their content (groups in this app come from the same user firing
    # multiple texts in a row, which is normal in Telegram).
    collapsed = []
    for m in messages:
        if collapsed and collapsed[-1]["role"] == m["role"]:
            collapsed[-1]["content"] += "\n" + m["content"]
        else:
            collapsed.append(m)
    return collapsed


def respond(chat_id: int, user_name: str, message: str) -> str:
    household_context = build_household_context(chat_id)
    messages = _build_messages(chat_id, user_name, message)

    response = client.messages.create(
        model=MODEL,
        max_tokens=512,
        system=[
            {
                "type": "text",
                "text": SOUL,
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": USER_LORE,
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": household_context,
            },
        ],
        messages=messages,
    )

    reply = response.content[0].text.strip()

    # Persist both turns for next call
    db.save_message(chat_id, "user", message, user_name=user_name)
    db.save_message(chat_id, "assistant", reply, user_name=None)

    return reply
