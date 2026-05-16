import json
import os
import re
from datetime import date, timedelta
from pathlib import Path
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

import db
import swiggy_mcp

load_dotenv()

client = AsyncAnthropic()
MODEL = "claude-sonnet-4-6"
SWIGGY_DEFAULT_ADDRESS_ID = os.getenv("SWIGGY_DEFAULT_ADDRESS_ID")

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
    {
        "name": "instamart_go_to_items",
        "description": (
            "Fetch the household's frequent/recent Instamart purchases (curd, paneer, snacks, staples). "
            "Use BEFORE suggesting meals or asking 'what's at home?', to ground suggestions in what they actually buy. "
            "Read-only, safe to call freely."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "instamart_recent_orders",
        "description": (
            "Fetch the household's recent Instamart order history. Use when you need to know what was bought when, "
            "infer cadence (\"milk every 4 days?\"), or check \"have we ordered X this week?\". Read-only."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "instamart_search",
        "description": (
            "Search Instamart for a specific product. Use to confirm availability or get current price for an item "
            "mentioned in conversation (\"is fresh basil available?\", \"what's amul ghee at?\"). Read-only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Product name to search, e.g. 'amul milk', 'fresh ginger'."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "instamart_cart",
        "description": "Get current Instamart cart contents. Use to check what's queued before suggesting additions. Read-only.",
        "input_schema": {"type": "object", "properties": {}},
    },
]


def _summarize_go_to(payload: dict) -> str:
    products = (payload.get("data") or {}).get("products") or []
    items = []
    for p in products[:15]:
        variants = p.get("variations") or []
        in_stock = [v for v in variants if v.get("isInStockAndAvailable")]
        chosen = min(in_stock, key=lambda v: v["price"]["offerPrice"]) if in_stock else (variants[0] if variants else None)
        items.append({
            "name": p.get("displayName"),
            "qty": chosen.get("quantityDescription") if chosen else None,
            "price_inr": chosen.get("price", {}).get("offerPrice") if chosen else None,
            "in_stock": p.get("inStock", True),
        })
    return json.dumps({"go_to_items": items}, ensure_ascii=False)


def _compact(payload: dict, max_chars: int = 2500) -> str:
    s = json.dumps(payload, ensure_ascii=False)
    return s if len(s) <= max_chars else s[:max_chars] + "...[truncated]"


async def execute_tool(chat_id: int, name: str, args: dict) -> str:
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
    if name == "instamart_go_to_items":
        try:
            payload = await swiggy_mcp.call_tool(
                "your_go_to_items", {"addressId": SWIGGY_DEFAULT_ADDRESS_ID}
            )
            return _summarize_go_to(payload)
        except Exception as e:
            return f"Couldn't reach Instamart go-to items: {e}"
    if name == "instamart_recent_orders":
        try:
            payload = await swiggy_mcp.call_tool("get_orders", {})
            return _compact(payload)
        except Exception as e:
            return f"Couldn't reach Instamart order history: {e}"
    if name == "instamart_search":
        try:
            payload = await swiggy_mcp.call_tool(
                "search_products",
                {"addressId": SWIGGY_DEFAULT_ADDRESS_ID, "query": args["query"]},
            )
            return _compact(payload)
        except Exception as e:
            return f"Couldn't search Instamart: {e}"
    if name == "instamart_cart":
        try:
            payload = await swiggy_mcp.call_tool("get_cart", {})
            return _compact(payload)
        except Exception as e:
            return f"Couldn't reach Instamart cart: {e}"
    return f"Unknown tool: {name}"


def build_household_context(chat_id: int) -> str:
    profiles = db.get_household_profiles(chat_id)
    facts = db.get_household_facts(chat_id)
    upcoming = db.get_upcoming_cook_events(chat_id)

    today = date.today()
    today_str = today.strftime("%A, %B %-d, %Y")
    parts = [f"Today is {today_str} ({today.isoformat()})."]

    if profiles:
        # Surface dietary constraints as their own block so they cannot be missed
        constraint_lines = []
        for p in profiles:
            diet = (p.get("diet_type") or "").strip()
            if diet == "Eggetarian":
                constraint_lines.append(f"- {p['name']} is EGGETARIAN. NO chicken, mutton, beef, pork, fish, prawns, or any meat or seafood. Eggs and dairy are fine.")
            elif diet == "Vegetarian":
                constraint_lines.append(f"- {p['name']} is VEGETARIAN. NO meat, fish, seafood, or eggs. Dairy is fine.")
            elif diet == "Vegan":
                constraint_lines.append(f"- {p['name']} is VEGAN. NO meat, fish, seafood, eggs, dairy, or any animal product.")
            elif diet == "Non-vegetarian":
                constraint_lines.append(f"- {p['name']} is non-vegetarian. No diet restriction beyond stated allergies.")
        if constraint_lines:
            parts.append("DIETARY CONSTRAINTS (absolute, non-negotiable, treat like allergies):\n" + "\n".join(constraint_lines))

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


WEEKLY_PLAN_INSTRUCTION = """Time for the weekly plan. The 7 days you are planning for are listed below. Use those dates and day names exactly as written. Do not invent or shift dates.

For each of the 7 days, suggest:
- **Breakfast**, **Lunch**, **Dinner** (3 meals per day, every day)
- For each meal, give one specific dish or meal direction in one short phrase, not a paragraph
- For days where the cook is on leave (check your cook schedule in context), lean toward order or eat-out suggestions; for days the cook is around, suggest things she or he can make at home

End with one discovery suggestion: a single new dish, snack, or cuisine the household should try this week.

Hard formatting rules. These will break the output if violated:
- **NO em-dashes (—) anywhere.** Use commas, periods, or new sentences.
- **NO markdown asterisks** for bold. Don't write **like this**. Just write plain text.
- **NO bullet hyphens** at the start of lines. Use plain prose lines.
- Stay in Remy's voice (sensory, specific, never generic praise words).
- One opening line, then the 7 days, then the discovery, then an optional one-line sign-off. That's it.

DIETARY RULES (absolute):
- Read the DIETARY CONSTRAINTS block in your context. Honour the strictest diet across the household for every shared meal.
- If anyone is Eggetarian, Vegetarian, or Vegan, NEVER suggest chicken, mutton, fish, prawns, beef, pork, or any meat. Don't even pair them as alternatives ("paneer or chicken"). Eliminate the meat option entirely.
- The discovery suggestion at the end must also follow these rules. No "try a Vietnamese chicken salad" if the household is eggetarian.

If only one partner is onboarded, just plan in their voice. Weave the partner-onboarding nudge in casually if at all. Do not append a robotic disclaimer at the end.

Output the exact message to send. Nothing else.
"""


def _format_week_block() -> tuple[str, date]:
    """Return (formatted block, start_date) for the upcoming Mon→Sun week.
    Plan starts tomorrow regardless of when this is called, which gives
    Sunday-evening cron the natural Mon→Sun shape."""
    plan_start = date.today() + timedelta(days=1)
    lines = []
    for i in range(7):
        d = plan_start + timedelta(days=i)
        lines.append(f"  {d.strftime('%A, %B %-d, %Y')} (date {d.isoformat()})")
    return "Plan for these 7 days:\n" + "\n".join(lines), plan_start


def _strip_formatting(text: str) -> str:
    """Belt-and-suspenders: remove em-dashes and markdown asterisks even if the
    model slips. Em-dashes become commas (with surrounding-space cleanup),
    asterisks are removed entirely."""
    # Replace " — " patterns and bare em-dashes with comma-space
    text = re.sub(r"\s*—\s*", ", ", text)
    # Remove bold asterisks (**text** or *text*)
    text = re.sub(r"\*+", "", text)
    # Collapse any double commas the substitution might have created
    text = re.sub(r",\s*,", ",", text)
    # Tidy up multiple blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


async def generate_weekly_plan(chat_id: int) -> str:
    household_context = build_household_context(chat_id)
    week_block, _ = _format_week_block()
    instruction = WEEKLY_PLAN_INSTRUCTION + "\n\n" + week_block
    response = await client.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=[
            {"type": "text", "text": SOUL, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": USER_LORE, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": household_context},
        ],
        messages=[{"role": "user", "content": instruction}],
    )
    raw = "".join(b.text for b in response.content if b.type == "text").strip()
    return _strip_formatting(raw)


async def respond(chat_id: int, user_name: str, message: str) -> str:
    messages = _build_messages(chat_id, user_name, message)

    response = None
    for _ in range(5):  # tool-use loop, max 5 iterations
        household_context = build_household_context(chat_id)
        response = await client.messages.create(
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
                result = await execute_tool(chat_id, block.name, block.input)
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
