"""Multi-tenant onboarding: /import_tokens and /set_address command flows.

A household (one Telegram chat) becomes usable in two steps:
1) /import_tokens — paste the contents of a .swiggy_tokens.json file
   generated locally by `python swiggy_login.py`. Stores in tenant_tokens.
2) /set_address — bot fetches the Swiggy addresses on file and the user
   picks one. Stores in tenant_settings.default_address_id.
"""

import json
import logging

from telegram import Update
from telegram.ext import (
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import db
import swiggy_mcp

logger = logging.getLogger(__name__)

# Conversation states
WAITING_TOKENS, WAITING_ADDRESS_PICK = range(2)


async def import_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "On a laptop, run `python swiggy_login.py` (browser will open for Swiggy login). "
        "Then open the generated .swiggy_tokens.json file, copy the whole contents, and paste it "
        "as your next message here.\n\n"
        "/cancel to abort."
    )
    return WAITING_TOKENS


async def import_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()
    if text.startswith("```"):
        # Tolerate users pasting inside a fenced code block
        text = text.strip("`").lstrip("json").strip()
    try:
        data = json.loads(text)
        if not isinstance(data, dict) or "tokens" not in data:
            raise ValueError("expected a JSON object with a 'tokens' key")
        tokens = data["tokens"]
        if not isinstance(tokens, dict) or not tokens.get("access_token"):
            raise ValueError("tokens.access_token is missing")
    except Exception as e:
        await update.message.reply_text(
            f"Couldn't parse that as Swiggy tokens ({e}). Paste the JSON again, or /cancel."
        )
        return WAITING_TOKENS

    db.set_tenant_tokens(chat_id, tokens, data.get("client_info"))
    await update.message.reply_text(
        "Tokens imported. Now pick a default delivery address with /set_address."
    )
    return ConversationHandler.END


async def set_address_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    if not db.has_tenant_tokens(chat_id):
        await update.message.reply_text(
            "No Swiggy tokens for this household yet. Run /import_tokens first."
        )
        return ConversationHandler.END
    try:
        payload = await swiggy_mcp.call_tool(chat_id, "get_addresses", {})
    except Exception as e:
        await update.message.reply_text(f"Couldn't fetch addresses: {e}")
        return ConversationHandler.END
    if not payload.get("success"):
        err = (payload.get("error") or {}).get("message", "unknown")
        await update.message.reply_text(f"Swiggy returned an error: {err}")
        return ConversationHandler.END

    addresses = (payload.get("data") or {}).get("addresses") or []
    if not addresses:
        await update.message.reply_text(
            "No saved addresses on your Swiggy account. Add one in the Swiggy app, then try /set_address again."
        )
        return ConversationHandler.END

    context.chat_data["address_options"] = addresses
    lines = ["Pick a default delivery address by replying with its number:"]
    for i, a in enumerate(addresses, 1):
        tag = a.get("addressTag") or a.get("addressCategory") or "?"
        line = a.get("addressLine") or "?"
        lines.append(f"{i}. [{tag}] {line[:120]}")
    lines.append("\n/cancel to abort.")
    await update.message.reply_text("\n".join(lines))
    return WAITING_ADDRESS_PICK


async def set_address_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()
    addrs = context.chat_data.get("address_options") or []
    try:
        idx = int(text) - 1
        chosen = addrs[idx]
    except (ValueError, IndexError):
        await update.message.reply_text(
            f"Reply with a number 1-{len(addrs)} from the list, or /cancel."
        )
        return WAITING_ADDRESS_PICK

    db.set_default_address(chat_id, chosen["id"])
    context.chat_data.pop("address_options", None)
    tag = chosen.get("addressTag") or chosen.get("addressCategory") or "?"
    await update.message.reply_text(
        f"Default address set: [{tag}] {(chosen.get('addressLine') or '')[:120]}\n\n"
        "You're set up. Try /staples or ask me what to cook."
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.chat_data.pop("address_options", None)
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


def build_import_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("import_tokens", import_start)],
        states={
            WAITING_TOKENS: [MessageHandler(filters.TEXT & ~filters.COMMAND, import_receive)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )


def build_set_address_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("set_address", set_address_start)],
        states={
            WAITING_ADDRESS_PICK: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_address_pick)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
