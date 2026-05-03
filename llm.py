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


def respond(chat_id: int, user_name: str, message: str) -> str:
    household_context = build_household_context(chat_id)
    user_message = f"From {user_name}: {message}"

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
        messages=[{"role": "user", "content": user_message}],
    )

    return response.content[0].text.strip()
