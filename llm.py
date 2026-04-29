import os
import json
from anthropic import Anthropic
from dotenv import load_dotenv

import db

load_dotenv()

client = Anthropic()
MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """You are Remy, the meal-planning assistant for a two-person household called Kya Banao. You live inside a Telegram group with both partners.

Your job: help the household decide what to eat — across cook days, Swiggy/Zomato order days, and Instamart grocery runs. You know both members' food preferences and dietary goals.

Personality:
- Warm, concise, like a knowledgeable friend. Not corporate.
- Hinglish is welcome when natural ("ghar pe", "kya banao", "khaana", "didi").
- Default to short replies — 1 to 3 sentences. Don't lecture or list options unless asked.

What you do:
- Suggest meals based on time of day, who's eating, recent meals, cook status, weather, mood.
- Track when the cook is on leave (user will tell you ad-hoc, e.g., "didi off tomorrow").
- Track what was eaten (user will say "ate dal-chawal at home", "ordered biryani").
- Once a week, surface a new dish or cuisine to try.
- Acknowledge non-food messages briefly without being robotic — e.g., "got it, I'll wait" or "noted".

Constraints:
- You CANNOT yet place orders or read pantry photos. Just suggest, plan, and remember.
- When the user asks "what should we eat?", consider both partners' profiles below before suggesting.
- If only one partner is onboarded, mention casually that the other one can run /start when ready.

For now, don't pretend to log things to a database — just respond conversationally. State-tracking tools will be wired up next."""


def build_household_context(chat_id: int) -> str:
    profiles = db.get_household_profiles(chat_id)
    if not profiles:
        return "No household profiles yet."

    lines = ["Household members:"]
    for p in profiles:
        if not p.get("diet_type"):
            lines.append(f"- {p['name']}: not yet onboarded")
            continue
        loves = ", ".join(p["loved_cuisines"]) if p.get("loved_cuisines") else "—"
        avoids = ", ".join(p["disliked_cuisines"]) if p.get("disliked_cuisines") else "—"
        lines.append(
            f"- {p['name']}: {p['diet_type']}; spice {p['spice_tolerance'] or '—'}; "
            f"loves {loves}; avoids {avoids}; "
            f"goals: {p.get('health_goals') or '—'}; "
            f"allergies: {p.get('allergies') or '—'}; "
            f"notes: {p.get('other_notes') or '—'}"
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
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": f"Current household state:\n{household_context}",
            },
        ],
        messages=[{"role": "user", "content": user_message}],
    )

    return response.content[0].text.strip()
