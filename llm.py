import json
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from openai import AsyncOpenAI
from dotenv import load_dotenv

import db
import swiggy_mcp

load_dotenv()

logger = logging.getLogger(__name__)

# OpenRouter is OpenAI-compatible; DeepSeek Chat V3 is 20-50x cheaper than
# Claude Sonnet 4.6 with comparable quality for our conversational + tool-use
# workload. Model is env-overridable so we can experiment without a redeploy.
client = AsyncOpenAI(
    base_url=os.getenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1"),
    # Placeholder if unset so imports don't crash; calls fail with a clean
    # auth error instead. Set OPENROUTER_API_KEY in .env / Railway vars.
    api_key=os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENAI_API_KEY") or "MISSING_API_KEY",
    default_headers={
        "HTTP-Referer": "https://github.com/kritisharma2708/kya-banao",
        "X-Title": "Kya Banao (Remy)",
    },
)
MODEL = os.getenv("REMY_MODEL", "deepseek/deepseek-chat")

PROJECT_DIR = Path(__file__).parent
SOUL = (PROJECT_DIR / "soul.md").read_text()
USER_LORE = (PROJECT_DIR / "user.md").read_text()

HISTORY_TURNS = 20


def _openai_tools() -> list[dict]:
    """Transform the internal TOOLS list (Anthropic-style `input_schema`) into
    OpenAI's function-calling shape. Cheap to recompute; not memoized because
    TOOLS is a module-level constant."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in TOOLS
    ]


def _system_content(chat_id: int) -> str:
    """One combined system prompt: soul + user lore + live household context."""
    return "\n\n".join([SOUL, USER_LORE, build_household_context(chat_id)])


async def _one_shot(system_texts: list[str], user_content: str, max_tokens: int = 2048) -> str:
    """One-round LLM call (no tools). Used by the various cron / plan
    generators. Joins system_texts into one system message per OpenAI shape."""
    messages = [
        {"role": "system", "content": "\n\n".join(t for t in system_texts if t)},
        {"role": "user", "content": user_content},
    ]
    response = await client.chat.completions.create(
        model=MODEL,
        messages=messages,
        max_tokens=max_tokens,
    )
    return (response.choices[0].message.content or "").strip()


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
        "name": "add_friend_recommendation",
        "description": (
            "Queue a food/restaurant/dish recommendation from a friend, family member, or coworker so it surfaces "
            "in the household's weekly discovery nudge. Call this when someone in chat mentions an external recommendation "
            "Kriti or Navneet might want to try later. Examples: 'Asif said try the Burmese place in Indiranagar', "
            "'mom recommended Sakshi's matar paneer', 'Dhaval keeps raving about the kombucha at Blue Tokai'. "
            "Always include the source (who recommended) when known — it gives the suggestion context. "
            "Do NOT use for the user's own ideas or things they've already tried."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "item": {
                    "type": "string",
                    "description": "What was recommended: dish, restaurant, snack, cuisine, product, etc.",
                },
                "source": {
                    "type": "string",
                    "description": "Who recommended it (first name or relationship). Empty string if unknown.",
                },
                "notes": {
                    "type": "string",
                    "description": "Brief context: why they liked it, where it's from, when it was mentioned. Empty string if none.",
                },
            },
            "required": ["item"],
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
        "name": "update_prep_status",
        "description": (
            "Record whether a lead-time prep step (soaking, marinating, fermenting, thawing) actually happened. "
            "Call this when the user says they did it ('soaked the rajma', 'batter's fermenting', 'done') or "
            "didn't ('forgot to soak', 'oops, never did it'). The morning brief reads this state to stay "
            "consistent: confirmed prep means the dish goes ahead, missed prep means Remy re-plans around it. "
            "meal_date is the day the DISH is planned for, not the day the prep happens. A 'soaked it' reply "
            "at night refers to tomorrow's dish; 'forgot to soak' in the morning refers to today's dish. "
            "If the reply is about one specific dish, pass a short dish_hint (e.g. 'rajma')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "meal_date": {
                    "type": "string",
                    "description": "ISO YYYY-MM-DD date the dish is planned for.",
                },
                "status": {
                    "type": "string",
                    "enum": ["confirmed", "missed"],
                    "description": "confirmed = prep was done, missed = prep didn't happen.",
                },
                "dish_hint": {
                    "type": "string",
                    "description": "Short substring identifying the dish ('rajma', 'tikka'). Empty string if the reply covers all pending prep for that date.",
                },
            },
            "required": ["meal_date", "status"],
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
    {
        "name": "instamart_stage_cart",
        "description": (
            "Add items to Kriti's Instamart cart for HER to checkout in the Swiggy app. "
            "This is the only write tool you have. You do NOT place orders — you stage; Kriti reviews and pays. "
            "Always close your reply with 'open Swiggy to checkout' so she knows the next step is on her. "
            "\n\n"
            "How to use: first call instamart_search or instamart_go_to_items to find spinIds for the products. "
            "Each variation of a product has its own spinId (e.g., 200g vs 400g of the same item are different spinIds). "
            "Then call this with the spinIds and quantities. "
            "\n\n"
            "Items merge with whatever's already in the cart (additive). If the user says 'scratch that, start over', "
            "call instamart_clear_cart first. "
            "\n\n"
            "NEVER stage anything that violates household dietary constraints. NEVER stage without the user asking for it "
            "(no surprise carts). When in doubt, suggest in voice first and wait for a 'yes, add it'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "description": "Items to add to cart.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "spinId": {"type": "string", "description": "Product variation ID from search results."},
                            "quantity": {"type": "number", "description": "How many of this variation to add."},
                            "name": {"type": "string", "description": "Human-readable item name (e.g. 'NOICE Bhakarwadi 100g'). Used for honest reporting if Swiggy silently drops this item due to out-of-stock."},
                        },
                        "required": ["spinId", "quantity", "name"],
                    },
                },
            },
            "required": ["items"],
        },
    },
    {
        "name": "instamart_clear_cart",
        "description": (
            "Empty the entire Instamart cart. Use only when the user explicitly says scrap it, start over, "
            "or 'clear the cart'. Never call this preemptively. Safe to call when cart is already empty."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "instamart_stage_weekly_groceries",
        "description": (
            "Stage many grocery items at once — designed for building the weekly cart after Kriti approves the meal plan. "
            "For each query, searches Instamart, picks the best in-stock variation, and stages everything in a single cart "
            "update. Returns: 'resolved' (items found and staged), 'not_found' (queries with no in-stock match), and the "
            "stage verify result. ALWAYS read both fields and report not_found items by name — don't pretend they got added."
            "\n\n"
            "Use ONLY after Kriti explicitly approves the weekly plan ('looks good', 'go ahead', 'build the cart'). Never "
            "stage proactively. Before calling, you must construct a clean deduplicated grocery list from the plan: combine "
            "ingredients across meals (paneer mentioned in 3 dinners = 1 entry, not 3), skip pantry staples Kriti already has "
            "(check instamart_recent_orders if helpful), and add size hints to queries when they matter ('paneer 200g', 'milk 1L')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "description": "Deduplicated grocery list to stage in one shot.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Ingredient + optional brand/size, e.g. 'Nandini toned milk 1L', 'Amul ghee 500g', 'paneer 200g'."},
                            "quantity": {"type": "number", "description": "How many packs to add. Defaults to 1."},
                        },
                        "required": ["query"],
                    },
                },
            },
            "required": ["items"],
        },
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
    logger.info(f"tool call: {name} args={json.dumps(args, ensure_ascii=False)[:300]}")
    result = await _execute_tool_inner(chat_id, name, args)
    logger.info(f"tool result: {name} -> {result[:300]}")
    return result


async def _execute_tool_inner(chat_id: int, name: str, args: dict) -> str:
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
    if name == "add_friend_recommendation":
        try:
            db.add_friend_recommendation(
                chat_id,
                args["item"],
                args.get("source", ""),
                args.get("notes", ""),
            )
            who = args.get("source") or "someone"
            return f"Queued rec from {who}: {args['item']}"
        except Exception as e:
            return f"Failed to save recommendation: {e}"
    if name == "update_prep_status":
        try:
            hit = db.set_prep_status(
                chat_id,
                args["meal_date"],
                args["status"],
                args.get("dish_hint", ""),
            )
            if hit == 0:
                return (
                    f"No prep is tracked for {args['meal_date']}"
                    + (f" matching '{args['dish_hint']}'" if args.get("dish_hint") else "")
                    + ". Nothing recorded. Don't pretend it was."
                )
            return f"Recorded: {hit} prep item(s) for {args['meal_date']} marked {args['status']}."
        except Exception as e:
            return f"Failed to update prep status: {e}"
    if name == "instamart_go_to_items":
        try:
            address_id = db.get_default_address(chat_id)
            if not address_id:
                return _NO_ADDRESS_HINT
            payload = await swiggy_mcp.call_tool(
                chat_id, "your_go_to_items", {"addressId": address_id}
            )
            return _summarize_go_to(payload)
        except Exception as e:
            return f"Couldn't reach Instamart go-to items: {e}"
    if name == "instamart_recent_orders":
        try:
            payload = await swiggy_mcp.call_tool(chat_id, "get_orders", {})
            return _compact(payload)
        except Exception as e:
            return f"Couldn't reach Instamart order history: {e}"
    if name == "instamart_search":
        try:
            address_id = db.get_default_address(chat_id)
            if not address_id:
                return _NO_ADDRESS_HINT
            payload = await swiggy_mcp.call_tool(
                chat_id,
                "search_products",
                {"addressId": address_id, "query": args["query"]},
            )
            return _compact(payload)
        except Exception as e:
            return f"Couldn't search Instamart: {e}"
    if name == "instamart_cart":
        try:
            payload = await swiggy_mcp.call_tool(chat_id, "get_cart", {})
            return _compact(payload)
        except Exception as e:
            return f"Couldn't reach Instamart cart: {e}"
    if name == "instamart_stage_cart":
        try:
            return await _stage_cart(chat_id, args.get("items") or [])
        except Exception as e:
            return f"Couldn't stage cart: {e}"
    if name == "instamart_clear_cart":
        try:
            payload = await swiggy_mcp.call_tool(chat_id, "clear_cart", {})
            return _compact(payload)
        except Exception as e:
            return f"Couldn't clear cart: {e}"
    if name == "instamart_stage_weekly_groceries":
        try:
            return await _stage_weekly_groceries(chat_id, args.get("items") or [])
        except Exception as e:
            return f"Couldn't stage weekly groceries: {e}"
    return f"Unknown tool: {name}"


_NO_ADDRESS_HINT = (
    "This household hasn't set a default Instamart delivery address yet. "
    "Tell Kriti to run /set_address — I'll show the addresses on file and she picks one."
)


def _existing_cart_items(get_cart_payload: dict) -> list[dict]:
    """Extract [{spinId, quantity}, ...] from a get_cart payload, or [] if cart is empty.
    Swiggy returns success=false with 'cart not found' when cart is empty — treat that as []."""
    if not get_cart_payload.get("success"):
        return []
    data = get_cart_payload.get("data") or {}
    # Cart item shape varies; defensively probe common keys
    items = data.get("items") or data.get("cartItems") or data.get("products") or []
    extracted = []
    for it in items:
        spin = it.get("spinId") or it.get("spin_id")
        qty = it.get("quantity") or it.get("qty") or 0
        if spin and qty:
            extracted.append({"spinId": spin, "quantity": qty})
    return extracted


async def _stage_cart(chat_id: int, new_items: list[dict]) -> str:
    """Merge new_items with existing cart, call update_cart, verify via the
    update_cart response itself (its data.items is the resulting cart state).
    Resilient when get_cart fails on Swiggy's side — proceeds without merge
    and warns Remy that existing items may have been wiped."""
    if not new_items:
        return json.dumps({"error": "no items provided"})

    address_id = db.get_default_address(chat_id)
    if not address_id:
        return _NO_ADDRESS_HINT

    get_payload = await swiggy_mcp.call_tool(chat_id, "get_cart", {})
    get_cart_ok = bool(get_payload.get("success"))
    existing = _existing_cart_items(get_payload) if get_cart_ok else []

    merged: dict[str, float] = {}
    for it in existing:
        merged[it["spinId"]] = merged.get(it["spinId"], 0) + it["quantity"]
    for it in new_items:
        merged[it["spinId"]] = merged.get(it["spinId"], 0) + it["quantity"]
    merged_list = [{"spinId": s, "quantity": q} for s, q in merged.items()]

    # Workaround for Swiggy bug: update_cart on a non-empty cart succeeds at the
    # MCP layer but the mobile app doesn't show new items. Clearing first makes
    # the app sync properly. Tested 2026-05-21.
    if existing:
        await swiggy_mcp.call_tool(chat_id, "clear_cart", {})

    update_payload = await swiggy_mcp.call_tool(
        chat_id,
        "update_cart",
        {"selectedAddressId": address_id, "items": merged_list},
    )

    # Verify via a follow-up get_cart — update_cart's response is sometimes a
    # lie (claims items landed when they actually didn't). get_cart is the
    # ground truth when it works. Fall back to update_cart's own response if
    # get_cart is failing.
    verify_payload = await swiggy_mcp.call_tool(chat_id, "get_cart", {})
    if verify_payload.get("success"):
        actual = _existing_cart_items(verify_payload)
        verify_source = "get_cart"
    elif update_payload.get("success"):
        actual = _existing_cart_items(update_payload)
        verify_source = "update_cart_response_fallback"
    else:
        actual = []
        verify_source = "both_failed"

    logger.info(
        f"stage_cart: requested={new_items} existing={existing} "
        f"update_success={update_payload.get('success')} verify_source={verify_source} "
        f"actual={actual}"
    )

    actual = actual  # type clarity
    actual_spins = {it["spinId"] for it in actual}
    name_by_spin = {it["spinId"]: it.get("name") or it["spinId"] for it in new_items}

    requested_spins = {it["spinId"] for it in new_items}
    landed_spins = requested_spins & actual_spins
    dropped_spins = requested_spins - actual_spins

    landed = [{"spinId": s, "name": name_by_spin[s]} for s in sorted(landed_spins)]
    dropped = [{"spinId": s, "name": name_by_spin[s]} for s in sorted(dropped_spins)]

    warnings = []
    if verify_source == "both_failed":
        # Don't claim drops since we have no source of truth
        landed = []
        dropped = []
        warnings.append(
            "Both update_cart and get_cart are failing right now (Swiggy backend issue). "
            "I have no confidence anything landed. Tell Kriti to check the app directly."
        )
    else:
        if dropped:
            warnings.append(
                "Swiggy silently rejected the dropped items, almost certainly out of stock at "
                "the fulfillment store. Tell Kriti specifically which items dropped (by name); "
                "suggest a different brand/variation or that she add them manually in the app."
            )
        if verify_source == "update_cart_response_fallback":
            warnings.append(
                "Couldn't double-check the cart with a fresh get_cart, so this verify trusts "
                "update_cart's own claim — which has been known to lie. Tell Kriti to open "
                "Swiggy and confirm the items are actually there before checkout."
            )
    if not get_cart_ok:
        warnings.append(
            "Swiggy's get_cart failed before staging, so I couldn't see what was already in "
            "the cart. Anything Kriti had manually added before this is likely gone — tell "
            "her to check the app and re-add anything missing."
        )

    return json.dumps({
        "requested": new_items,
        "landed_in_cart": landed,
        "dropped_by_swiggy": dropped,
        "final_cart": [{"spinId": i["spinId"], "name": i.get("itemName"), "qty": i.get("quantity"), "price": i.get("discountedFinalPrice")}
                       for i in (update_payload.get("data") or {}).get("items") or []],
        "warning": " ".join(warnings) if warnings else None,
    }, ensure_ascii=False)[:3000]


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


async def _stage_weekly_groceries(chat_id: int, items: list[dict]) -> str:
    """Search-and-stage a deduplicated grocery list in one tool call.
    For each query: search Instamart, pick the first in-stock variation of
    the first in-stock product, collect spinIds, hand to _stage_cart for the
    actual update + verify."""
    if not items:
        return json.dumps({"error": "no items provided"})

    address_id = db.get_default_address(chat_id)
    if not address_id:
        return _NO_ADDRESS_HINT

    resolved: list[dict] = []
    not_found: list[dict] = []

    for it in items:
        query = (it.get("query") or "").strip()
        if not query:
            continue
        qty = it.get("quantity") or 1
        try:
            search_payload = await swiggy_mcp.call_tool(
                chat_id,
                "search_products",
                {"addressId": address_id, "query": query},
            )
        except Exception as e:
            not_found.append({"query": query, "reason": f"search failed: {e}"})
            continue
        if not search_payload.get("success"):
            not_found.append({
                "query": query,
                "reason": (search_payload.get("error") or {}).get("message", "search returned no success"),
            })
            continue

        products = (search_payload.get("data") or {}).get("products") or []
        chosen = None
        for p in products:
            if not p.get("inStock"):
                continue
            for v in p.get("variations") or []:
                if v.get("isInStockAndAvailable"):
                    chosen = (p, v)
                    break
            if chosen:
                break
        if not chosen:
            not_found.append({"query": query, "reason": "no in-stock matches"})
            continue
        p, v = chosen
        resolved.append({
            "spinId": v["spinId"],
            "quantity": qty,
            "name": f"{p.get('displayName')} ({v.get('quantityDescription')})",
        })

    if not resolved:
        return json.dumps({
            "resolved": [],
            "not_found": not_found,
            "stage_result": None,
            "warning": "Nothing on the list was found on Instamart. Tell Kriti by name what's missing.",
        }, ensure_ascii=False)[:4000]

    stage_raw = await _stage_cart(chat_id, resolved)
    stage_result = json.loads(stage_raw)
    return json.dumps({
        "resolved_count": len(resolved),
        "not_found": not_found,
        "stage_result": stage_result,
        "warning": (
            f"{len(not_found)} item(s) couldn't be found on Instamart — name them to Kriti. "
            "Also relay anything in stage_result.dropped_by_swiggy."
        ) if not_found else None,
    }, ensure_ascii=False)[:4000]


PLAN_EXTRACTION_INSTRUCTION = """Below is a 7-day meal plan for an Indian household.
Extract a JSON object with this exact shape, nothing else:

{"dates": {"YYYY-MM-DD": {"breakfast": "...", "lunch": "...", "dinner": "..."}, ...}}

Rules:
- Keys are ISO dates, exactly as they appear in the plan.
- Each meal value is a short phrase (the actual dish), not a paragraph.
- Strip any leading bullets, "Breakfast:" labels, em-dashes, asterisks, etc.
- If a day mentions a discovery suggestion or sign-off, ignore those — only the 3 daily meals.
- Return ONLY the JSON. No prose before or after.

Meal plan text:
"""


async def _extract_structured_plan(plan_text: str) -> dict:
    """Second LLM pass: turn the voiced weekly plan into structured per-day JSON.
    Used to power the daily morning menu reminder."""
    raw = await _one_shot(
        system_texts=[],  # extraction is pure parsing, doesn't need soul/lore
        user_content=PLAN_EXTRACTION_INSTRUCTION + plan_text,
        max_tokens=1024,
    )
    # Strip code fences if the model adds them despite the instruction
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


async def generate_weekly_plan(chat_id: int) -> str:
    week_block, plan_start = _format_week_block()
    instruction = WEEKLY_PLAN_INSTRUCTION + "\n\n" + week_block
    raw = await _one_shot(
        system_texts=[SOUL, USER_LORE, build_household_context(chat_id)],
        user_content=instruction,
        max_tokens=2048,
    )
    voiced = _strip_formatting(raw)

    # Capture structured version for the daily menu reminder. If extraction
    # fails, log and proceed — we don't want to lose the voiced plan over
    # a parsing hiccup.
    try:
        structured = await _extract_structured_plan(voiced)
        db.save_weekly_plan(chat_id, plan_start.isoformat(), structured)
        logger.info(f"Saved structured weekly plan for {chat_id}, week starting {plan_start}")
    except Exception:
        logger.exception("Failed to extract/save structured weekly plan")

    return voiced


# -----------------------------------------------------------------------------
# Look-ahead prep: detect lead-time steps (soak/marinate/ferment/thaw) in
# planned dishes, nudge the night before, and keep the morning brief honest
# about what actually got done.
# -----------------------------------------------------------------------------

def _detect_prep_from_rules(
    meals: dict, rules: list[dict], lead: str
) -> tuple[list[dict], list[dict]]:
    """Match planned dishes against curated prep_rules for one lead class.
    Returns (matched preps, unmatched dishes) — unmatched go to the LLM
    fallback. First matching rule per dish wins."""
    matched: list[dict] = []
    unmatched: list[dict] = []
    for meal_type in ("breakfast", "lunch", "dinner"):
        dish = (meals.get(meal_type) or "").strip()
        if not dish:
            continue
        dish_lower = dish.lower()
        hit = None
        for r in rules:
            if r["lead"] == lead and r["pattern"] in dish_lower:
                hit = r
                break
        if hit:
            matched.append({"meal_type": meal_type, "dish": dish, "action": hit["action"]})
        else:
            unmatched.append({"meal_type": meal_type, "dish": dish})
    return matched, unmatched


PREP_FALLBACK_INSTRUCTION = """You are checking tomorrow's planned dishes for OVERNIGHT lead-time prep that must start tonight: soaking dried beans/lentils/sago, fermenting batter, setting or souring curd, long marination. Ignore anything that can be done day-of (chopping, quick 30-minute marinades, boiling).

Dishes (JSON): """

PREP_FALLBACK_SUFFIX = """

Reply with ONLY a JSON array, no prose. One entry per dish that genuinely needs overnight prep, empty array if none:
[{"meal_type": "...", "dish": "...", "action": "one short imperative sentence saying what to do tonight"}]

Be conservative. When unsure whether a dish needs overnight prep, leave it out. A false nudge erodes trust faster than a missed one."""


async def _detect_prep_llm(unmatched: list[dict]) -> list[dict]:
    """LLM fallback for dishes the curated rules didn't recognize. Conservative
    by instruction; any parse failure returns [] rather than guessing."""
    if not unmatched:
        return []
    raw = await _one_shot(
        system_texts=[],
        user_content=PREP_FALLBACK_INSTRUCTION
        + json.dumps(unmatched, ensure_ascii=False)
        + PREP_FALLBACK_SUFFIX,
        max_tokens=512,
    )
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        preps = json.loads(raw)
        return [
            p for p in preps
            if isinstance(p, dict) and p.get("dish") and p.get("action")
        ]
    except (json.JSONDecodeError, TypeError):
        logger.warning(f"prep fallback returned unparseable output: {raw[:200]}")
        return []


PREP_NUDGE_INSTRUCTION = """It's evening. Tomorrow's plan includes dishes that need prep starting TONIGHT. Compose ONE short message in Remy's voice (3 sentences max) telling the household what to do before bed. Name the dish and the action concretely ("soak the rajma before you sleep, tomorrow's lunch depends on it"). If there are multiple preps, weave them into one flowing message, don't list them.

Close by asking them to say the word once it's done, so you know the dish is safe for tomorrow.

Hard rules:
- NO em-dashes, NO markdown asterisks, NO bullet hyphens.
- Stay in Remy's sensory, specific voice.
- Do not mention anything about tomorrow's plan beyond the prep-dependent dishes.

Prep needed tonight (JSON):
"""


async def check_prep_for_chat(chat_id: int, today: date | None = None) -> str | None:
    """9 PM cron body: look at TOMORROW's planned meals, detect overnight prep
    (curated rules first, conservative LLM fallback for unknown dishes), record
    prep_state, and return a voiced nudge. None = nothing needs prep tonight
    (cron stays silent)."""
    today = today or date.today()
    tomorrow = today + timedelta(days=1)
    meals = db.get_meals_for_date(chat_id, tomorrow.isoformat())
    if not meals:
        return None

    rules = db.get_prep_rules()
    matched, unmatched = _detect_prep_from_rules(meals, rules, lead="night_before")
    try:
        matched += await _detect_prep_llm(unmatched)
    except Exception:
        logger.exception("prep: LLM fallback failed, continuing with rule matches only")
    if not matched:
        return None

    db.record_prep_nudges(chat_id, tomorrow.isoformat(), matched)

    instruction = PREP_NUDGE_INSTRUCTION + json.dumps(matched, ensure_ascii=False)
    raw = await _one_shot(
        system_texts=[SOUL, USER_LORE, build_household_context(chat_id)],
        user_content=instruction,
        max_tokens=512,
    )
    return _strip_formatting(raw)


MORNING_BRIEF_INSTRUCTION = """It's 8 AM. Compose today's brief in Remy's voice: a DECIDED day, not a menu of options. State breakfast, lunch, and dinner as settled facts, with at least one line of reasoning drawn from the real data below (cook's schedule, prep state, what the plan says). Never invent facts you don't have: no weather, no calendar events, nothing outside the JSON below and your context.

Prep-state rules (these keep you honest, follow them exactly):
- A dish whose prep is "confirmed": state it with full confidence, maybe a nod to the prep ("the rajma had its overnight swim, lunch is on").
- A dish whose prep is "missed": the dish CANNOT happen today. Re-plan it out loud: name a no-prep alternative for that meal today, move the original dish to tomorrow, and tell them tonight's soak makes it happen. Do not guilt-trip, one light touch at most.
- A dish whose prep is "nudged" (you asked last night, nobody answered): go with the plan, but add ONE conditional line: if the prep didn't happen, say the word and you'll pivot to [name a concrete no-prep alternative].
- same_day_preps listed below are things to start THIS MORNING for today's meals (marinades, thawing). Weave them in with their timing ("get the tikka into its marinade by noon").

Hard rules:
- NO em-dashes, NO markdown asterisks, NO bullet hyphens at line starts.
- Any alternative dish you name must respect the DIETARY CONSTRAINTS block in your context.
- 4 to 7 short lines total. One thought per line. No headers.
- Stay in Remy's sensory, specific voice.

Today's data (JSON):
"""


async def generate_morning_brief(chat_id: int, today: date | None = None) -> str | None:
    """8 AM cron body: today's meals as a decided day, consistent with last
    night's prep state. Returns None if no plan covers today (cron stays
    silent)."""
    today = today or date.today()
    meals = db.get_meals_for_date(chat_id, today.isoformat())
    if not meals:
        return None

    prep_state = db.get_prep_state(chat_id, today.isoformat())
    rules = db.get_prep_rules()
    same_day_preps, _ = _detect_prep_from_rules(meals, rules, lead="same_day_morning")

    payload = {
        "today": today.isoformat(),
        "day_name": today.strftime("%A"),
        "meals": meals,
        "prep_state": prep_state,
        "same_day_preps": same_day_preps,
    }

    instruction = MORNING_BRIEF_INSTRUCTION + json.dumps(payload, ensure_ascii=False)
    raw = await _one_shot(
        system_texts=[SOUL, USER_LORE, build_household_context(chat_id)],
        user_content=instruction,
        max_tokens=768,
    )
    return _strip_formatting(raw)


async def respond(chat_id: int, user_name: str, message: str) -> str:
    conv = _build_messages(chat_id, user_name, message)
    # OpenAI wants one flat messages list, system first
    messages = [{"role": "system", "content": _system_content(chat_id)}] + conv

    tools = _openai_tools()
    reply_text = ""

    for _ in range(5):  # tool-use loop, max 5 iterations
        # Refresh system in case a tool call mutated household context (e.g. `remember`).
        messages[0] = {"role": "system", "content": _system_content(chat_id)}

        response = await client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=tools,
            max_tokens=2048,
        )
        msg = response.choices[0].message
        finish_reason = response.choices[0].finish_reason
        tool_calls = msg.tool_calls or []
        logger.info(f"finish_reason={finish_reason} tool_calls={len(tool_calls)}")

        reply_text = (msg.content or "").strip()

        if finish_reason != "tool_calls" or not tool_calls:
            break

        # Append assistant turn (with tool_calls) then one tool-role message per call
        messages.append({
            "role": "assistant",
            "content": msg.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in tool_calls
            ],
        })
        for tc in tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError as e:
                args = {}
                logger.warning(f"Tool {tc.function.name} sent malformed JSON args: {e}")
            result = await execute_tool(chat_id, tc.function.name, args)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })

    db.save_message(chat_id, "user", message, user_name=user_name)
    db.save_message(chat_id, "assistant", reply_text, user_name=None)

    return reply_text or "(silence)"


# -----------------------------------------------------------------------------
# Cadence inference: detect items running low based on Instamart order history.
# -----------------------------------------------------------------------------

# Items ordered less frequently than this are skipped — order history is too
# short to estimate cadence reliably for monthly-or-rarer purchases.
MAX_CADENCE_DAYS = 21
# We only flag an item as running low when (days_since_last - cadence) exceeds
# this buffer, to avoid pinging on the exact day something is "due."
CADENCE_BUFFER_DAYS = 1


def _compute_cadence_alerts(orders: list[dict], today: date | None = None) -> list[dict]:
    """Given a list of order objects from get_orders, return items likely
    running low. Each item needs >= 2 orders to estimate cadence."""
    today = today or date.today()
    dates_by_item: dict[str, list[date]] = {}
    name_by_item: dict[str, str] = {}

    for order in orders:
        ts = order.get("createdAt") or order.get("orderTime")
        if not ts:
            continue
        try:
            d = datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
        except (ValueError, AttributeError):
            continue
        for it in order.get("items") or []:
            item_id = it.get("itemId") or it.get("spinId")
            if not item_id:
                continue
            dates_by_item.setdefault(item_id, []).append(d)
            name_by_item[item_id] = it.get("name") or item_id

    alerts = []
    for item_id, dates in dates_by_item.items():
        if len(dates) < 2:
            continue
        dates = sorted(set(dates))
        if len(dates) < 2:
            continue
        intervals = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
        intervals = [iv for iv in intervals if iv > 0]
        if not intervals:
            continue
        cadence = mean(intervals)
        if cadence > MAX_CADENCE_DAYS:
            continue
        days_since_last = (today - dates[-1]).days
        if days_since_last - cadence > CADENCE_BUFFER_DAYS:
            alerts.append({
                "item_id": item_id,
                "name": name_by_item[item_id],
                "last_ordered": dates[-1].isoformat(),
                "days_since_last": days_since_last,
                "typical_cadence_days": round(cadence, 1),
                "order_count": len(dates),
            })
    alerts.sort(key=lambda a: a["days_since_last"] - a["typical_cadence_days"], reverse=True)
    return alerts


CADENCE_NUDGE_INSTRUCTION = """The following Instamart items appear to be running low based on the household's order cadence. Compose ONE short proactive message in Remy's voice (3 sentences max) flagging the most relevant 1-3 items. Mention the specific items and roughly how overdue each is ("milk's been 6 days, usually you order every 4"). End with a soft "want me to draft a cart?" or similar — never assume yes, never stage anything yourself.

Hard rules:
- NO em-dashes, NO markdown asterisks, NO bullet hyphens (same voice rules as elsewhere).
- If more than 3 items are listed, pick the top 1-3 by how overdue they are. Don't dump the whole list.
- One opening line, then the items woven naturally, then the soft ask. Don't write headers.
- Stay in Remy's sensory, specific voice.

Items running low (JSON):
"""


async def check_cadence_for_chat(chat_id: int) -> str | None:
    """Fetch orders, compute cadence alerts, and ask Remy to phrase a nudge.
    Returns None when nothing is due (cron stays silent) or when Swiggy fails."""
    try:
        payload = await swiggy_mcp.call_tool(chat_id, "get_orders", {"count": 20})
    except Exception:
        logger.exception("cadence: get_orders failed")
        return None
    if not payload.get("success"):
        logger.info(f"cadence: get_orders returned error: {(payload.get('error') or {}).get('message')}")
        return None

    orders = (payload.get("data") or {}).get("orders") or []
    alerts = _compute_cadence_alerts(orders)
    if not alerts:
        return None

    instruction = CADENCE_NUDGE_INSTRUCTION + json.dumps(alerts[:5], ensure_ascii=False)
    raw = await _one_shot(
        system_texts=[SOUL, USER_LORE, build_household_context(chat_id)],
        user_content=instruction,
        max_tokens=512,
    )
    return _strip_formatting(raw)


# -----------------------------------------------------------------------------
# Discovery engine: weekly nudge to try something new.
# -----------------------------------------------------------------------------

DISCOVERY_INSTRUCTION = """Time for the weekly discovery nudge. Suggest exactly ONE new thing the household should try this week. Could be a snack, a produce item, a dish to cook, a cuisine to revisit, or a restaurant style. It must be GROUNDED in the data below — don't invent generic suggestions.

Hard rules:
- Must respect household dietary constraints. If anyone is eggetarian/vegetarian/vegan, NEVER suggest meat or fish.
- Must NOT appear in the household's recent orders list below. The whole point is novelty.
- If a friend recommendation is queued, lead with that (mention who recommended it, use the source field).
- Consider Indian seasonality (today's date is in your context). Bangalore in late spring = mangoes (Alphonso/Banganapalli winding down, Kesar arriving), jamun starting, fresh tender coconut. Monsoon = corn, mushrooms, leafy greens. Winter = strawberries, oranges, root veg. Use seasonality when it actually fits.
- Short and specific. 2-4 sentences max. Name the thing concretely, say WHY it fits, and close with a soft offer ("want me to find it on Instamart?" or "want me to weave this into the weekend plan?").

Hard formatting rules:
- NO em-dashes (—) anywhere.
- NO markdown asterisks.
- NO bullet hyphens at the start of lines.
- Stay in Remy's sensory, specific voice.

Below: dietary constraints, last 30 days of order history, the last 2 weekly plans (so you don't repeat dishes), pending friend recommendations, and today's date.
"""


async def generate_weekly_discovery(chat_id: int) -> str | None:
    """Pull recent orders + plans + friend recs, ask Remy to phrase ONE novel
    suggestion. Returns None on failure or if Swiggy is unreachable."""
    pending_recs = db.get_pending_friend_recs(chat_id, limit=3)

    try:
        orders_payload = await swiggy_mcp.call_tool(chat_id, "get_orders", {"count": 20})
    except Exception:
        logger.exception("discovery: get_orders failed")
        return None
    if not orders_payload.get("success"):
        logger.info(f"discovery: get_orders error: {(orders_payload.get('error') or {}).get('message')}")
        return None
    orders = (orders_payload.get("data") or {}).get("orders") or []

    # Compact order history: just dates + item names
    compact_orders = []
    for o in orders[:20]:
        items = [it.get("name") for it in (o.get("items") or []) if it.get("name")]
        compact_orders.append({"date": (o.get("createdAt") or "")[:10], "items": items})

    recent_plans = db.get_recent_weekly_plans(chat_id, limit=2)

    payload = {
        "today": date.today().isoformat(),
        "pending_friend_recommendations": pending_recs,
        "recent_orders_last_15_days": compact_orders,
        "last_two_weekly_plans": recent_plans,
    }

    instruction = DISCOVERY_INSTRUCTION + "\n\n" + json.dumps(payload, ensure_ascii=False, default=str)[:6000]
    raw = await _one_shot(
        system_texts=[SOUL, USER_LORE, build_household_context(chat_id)],
        user_content=instruction,
        max_tokens=512,
    )
    voiced = _strip_formatting(raw)
    if not voiced:
        return None

    # If a pending friend rec was the basis, mark the first as consumed so we
    # don't surface it again next week.
    if pending_recs:
        db.mark_friend_rec_consumed(pending_recs[0]["id"])

    return voiced
