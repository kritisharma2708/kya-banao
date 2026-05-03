import os
from datetime import date
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

HISTORY_TURNS = 20

TOOLS = [
    {
        "name": "log_cook_leave",
        "description": (
            "Record that the cook is on leave for a specific date or date range. "
            "Call this whenever the user mentions cook absence (e.g., 'didi off tomorrow', "
            "'bhaiya on leave till the 5th', 'cook is gone Monday to Friday'). "
            "Always confirm the inferred dates back to the user in your reply so they can correct you. "
            "If the user says 'till the 5th', use today as start_date and the 5th as end_date. "
            "If the user only mentions a single day, set start_date == end_date. "
            "If the user says the cook is BACK or RETURNING, do NOT call this tool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {
                    "type": "string",
                    "description": "First day of leave in ISO YYYY-MM-DD format.",
                },
                "end_date": {
                    "type": "string",
                    "description": "Last day of leave (inclusive) in ISO YYYY-MM-DD format. Use the same date as start_date for a single-day leave.",
                },
                "reason": {
                    "type": "string",
                    "description": "Brief reason if mentioned (e.g., 'wedding', 'sick'). Empty string if not mentioned.",
                },
            },
            "required": ["start_date", "end_date"],
        },
    },
    {
        "name": "remember",
        "description": (
            "Save a durable, household-level fact that should be remembered forever, not just for this conversation. "
            "Use for things like 'cook is bhaiya, not didi', 'Kriti dislikes paneer', 'we order on Friday nights', "
            "'partner is allergic to mushrooms'. "
            "Do NOT use for one-off context like 'I'm tired tonight' or 'we just ate biryani'. "
            "Use sparingly, only for stable truths. Never duplicate an existing fact."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fact": {
                    "type": "string",
                    "description": "The fact to remember, written as a complete short sentence in third person.",
                },
            },
            "required": ["fact"],
        },
    },
]


def execute_tool(chat_id: int, name: str, args: dict) -> str:
    if name == "log_cook_leave":
        try:
            db.log_cook_leave(
                chat_id,
                args["start_date"],
                args["end_date"],
                args.get("reason", ""),
            )
            return f"Saved: cook on leave from {args['start_date']} to {args['end_date']}."
        except Exception as e:
            return f"Failed to save leave: {e}"
    if name == "remember":
        try:
            db.add_household_fact(chat_id, args["fact"])
            return f"Saved fact: {args['fact']}"
        except Exception as e:
            return f"Failed to save fact: {e}"
    return f"Unknown tool: {name}"


def build_household_context(chat_id: int) -> str:
    profiles = db.get_household_profiles(chat_id)
    facts = db.get_household_facts(chat_id)
    upcoming = db.get_upcoming_cook_events(chat_id)

    parts = [f"Today is {date.today().isoformat()}."]

    if profiles:
        lines = ["Household members:"]
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
        parts.append("\n".join(lines))
    else:
        parts.append("No household profiles yet.")

    if facts:
        parts.append("Remembered facts about this household (always honour these):\n" + "\n".join(f"- {f}" for f in facts))

    if upcoming:
        leave_lines = [f"- {e['event_date']}: {e['status']}" + (f" ({e['notes']})" if e.get("notes") else "") for e in upcoming]
        parts.append("Upcoming cook schedule (next 3 weeks):\n" + "\n".join(leave_lines))
    else:
        parts.append("No cook leaves logged for the next 3 weeks. Cook is presumed present on weekdays unless told otherwise.")

    return "\n\n".join(parts)


def _build_messages(chat_id: int, user_name: str, current_message: str):
    history = db.get_recent_messages(chat_id, limit=HISTORY_TURNS)
    messages = []
    for m in history:
        if m["role"] == "user":
            label = m["user_name"] or "user"
            messages.append({"role": "user", "content": f"[{label}] {m['content']}"})
        else:
            messages.append({"role": "assistant", "content": m["content"]})
    messages.append({"role": "user", "content": f"[{user_name}] {current_message}"})

    collapsed = []
    for m in messages:
        if collapsed and collapsed[-1]["role"] == m["role"] and isinstance(collapsed[-1]["content"], str) and isinstance(m["content"], str):
            collapsed[-1]["content"] += "\n" + m["content"]
        else:
            collapsed.append(m)
    return collapsed


def respond(chat_id: int, user_name: str, message: str) -> str:
    messages = _build_messages(chat_id, user_name, message)

    response = None
    for _ in range(5):  # tool-use loop, max 5 iterations
        household_context = build_household_context(chat_id)
        response = client.messages.create(
            model=MODEL,
            max_tokens=512,
            system=[
                {"type": "text", "text": SOUL, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": USER_LORE, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": household_context},
            ],
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason != "tool_use":
            break

        # Append assistant turn (with tool_use blocks) and tool results
        messages.append({"role": "assistant", "content": [block.model_dump() for block in response.content]})

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = execute_tool(chat_id, block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })
        messages.append({"role": "user", "content": tool_results})

    reply_text = "".join(b.text for b in response.content if b.type == "text").strip()

    db.save_message(chat_id, "user", message, user_name=user_name)
    db.save_message(chat_id, "assistant", reply_text, user_name=None)

    return reply_text or "(silence)"
