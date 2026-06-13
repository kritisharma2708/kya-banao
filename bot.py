import os
import re
import json
import logging
from datetime import time as dt_time, timezone, timedelta
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import db
import onboarding
import tenant_setup
import llm
import swiggy_mcp
from swiggy_login import FileTokenStorage, TOKENS_FILE

IST = timezone(timedelta(hours=5, minutes=30))

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    await update.message.reply_text(
        f"Chat ID: `{chat.id}`\nChat type: {chat.type}\nYour user ID: `{user.id}`",
        parse_mode="Markdown",
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    profiles = db.get_household_profiles(chat.id)
    if not profiles:
        await update.message.reply_text("No one's onboarded in this chat yet. Send /start.")
        return

    lines = ["Household status:"]
    for p in profiles:
        if p.get("diet_type"):
            lines.append(f"✅ {p['name']}, {p['diet_type']}")
        else:
            lines.append(f"⏳ {p['name']}, onboarding incomplete")
    await update.message.reply_text("\n".join(lines))


async def plan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manual trigger: generate and send a weekly plan to this chat."""
    chat = update.effective_chat
    profiles = db.get_household_profiles(chat.id)
    if not any(p.get("diet_type") for p in profiles):
        await update.message.reply_text("Need at least one onboarded user before I can plan. Send /start.")
        return
    try:
        await context.bot.send_chat_action(chat_id=chat.id, action="typing")
        plan = await llm.generate_weekly_plan(chat.id)
        await update.message.reply_text(plan)
        db.save_message(chat.id, "assistant", plan, user_name=None)
    except Exception as e:
        logger.exception("Weekly plan error")
        await update.message.reply_text(f"(Couldn't generate plan: {e})")


async def discovery_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manual trigger: generate this week's discovery nudge on demand."""
    chat = update.effective_chat
    try:
        await context.bot.send_chat_action(chat_id=chat.id, action="typing")
        nudge = await llm.generate_weekly_discovery(chat.id)
        if not nudge:
            await update.message.reply_text(
                "(Nothing fresh surfaced this week. Could be Swiggy hiccuping or order history too thin. Try again later.)"
            )
            return
        await update.message.reply_text(nudge)
        db.save_message(chat.id, "assistant", nudge, user_name=None)
    except Exception as e:
        logger.exception("Discovery error")
        await update.message.reply_text(f"(Discovery hit an error: {e})")


def _render_staples(payload: dict) -> str:
    products = (payload.get("data") or {}).get("products") or []
    if not products:
        return "No go-to items came back from Instamart yet."

    lines = ["Your Instamart go-to items:"]
    for p in products[:20]:
        name = p.get("displayName", "unknown")
        # Pick the cheapest in-stock variation for a representative size + price
        variants = p.get("variations") or []
        in_stock = [v for v in variants if v.get("isInStockAndAvailable")]
        chosen = min(in_stock, key=lambda v: v["price"]["offerPrice"]) if in_stock else (variants[0] if variants else None)
        qty = chosen.get("quantityDescription") if chosen else None
        price = chosen.get("price", {}).get("offerPrice") if chosen else None
        bits = [name]
        if qty:
            bits.append(f"({qty})")
        if price is not None:
            bits.append(f"₹{price}")
        if not p.get("inStock", True):
            bits.append("[out of stock]")
        lines.append("- " + " ".join(bits))
    if len(products) > 20:
        lines.append(f"...and {len(products) - 20} more")
    return "\n".join(lines)


async def staples(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show Instamart go-to items for this household's default address."""
    chat_id = update.effective_chat.id
    address_id = db.get_default_address(chat_id)
    if not address_id:
        await update.message.reply_text(
            "No delivery address set for this household yet. Run /set_address."
        )
        return
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        payload = await swiggy_mcp.call_tool(
            chat_id,
            "your_go_to_items",
            {"addressId": address_id},
        )
        logger.info(f"your_go_to_items raw payload: {payload}")
        await update.message.reply_text(_render_staples(payload))
    except swiggy_mcp.SwiggyAuthRequired as e:
        await update.message.reply_text(f"Swiggy auth needed: {e}")
    except Exception as e:
        logger.exception("staples error")
        await update.message.reply_text(f"(Couldn't fetch staples: {e})")


async def send_weekly_plan(context: ContextTypes.DEFAULT_TYPE):
    """Cron callback: post the Sunday weekly plan to every household group with onboarded users."""
    chat_ids = db.get_chat_ids_with_onboarded_users()
    logger.info(f"Sunday weekly plan firing for {len(chat_ids)} chat(s)")
    for chat_id in chat_ids:
        try:
            plan = await llm.generate_weekly_plan(chat_id)
            await context.bot.send_message(chat_id=chat_id, text=plan)
            db.save_message(chat_id, "assistant", plan, user_name=None)
        except Exception as e:
            logger.exception(f"Failed to send weekly plan to {chat_id}: {e}")


async def send_daily_menu(context: ContextTypes.DEFAULT_TYPE):
    """Daily 8 AM IST cron: post today's planned meals to each household chat.
    Silent if no weekly plan covers today (e.g., last Sunday's plan expired)."""
    chat_ids = db.get_chat_ids_with_onboarded_users()
    logger.info(f"Daily menu firing for {len(chat_ids)} chat(s)")
    for chat_id in chat_ids:
        try:
            reminder = await llm.format_daily_menu_for_chat(chat_id)
            if not reminder:
                continue
            await context.bot.send_message(chat_id=chat_id, text=reminder)
            db.save_message(chat_id, "assistant", reminder, user_name=None)
        except Exception as e:
            logger.exception(f"Daily menu failed for {chat_id}: {e}")


async def send_weekly_discovery(context: ContextTypes.DEFAULT_TYPE):
    """Wednesday 7 PM IST cron: surface one new thing to try this week,
    grounded in order history, recent plans, season, and any queued friend
    recommendations. Silent on weeks where nothing fresh emerges."""
    chat_ids = db.get_chat_ids_with_onboarded_users()
    logger.info(f"Discovery firing for {len(chat_ids)} chat(s)")
    for chat_id in chat_ids:
        try:
            nudge = await llm.generate_weekly_discovery(chat_id)
            if not nudge:
                continue
            await context.bot.send_message(chat_id=chat_id, text=nudge)
            db.save_message(chat_id, "assistant", nudge, user_name=None)
        except Exception as e:
            logger.exception(f"Discovery failed for {chat_id}: {e}")


async def send_cadence_nudge(context: ContextTypes.DEFAULT_TYPE):
    """Daily 9 AM IST cron: ping each household chat ONLY if something looks
    overdue based on Instamart order cadence. Stays silent otherwise."""
    chat_ids = db.get_chat_ids_with_onboarded_users()
    logger.info(f"Cadence check firing for {len(chat_ids)} chat(s)")
    for chat_id in chat_ids:
        try:
            nudge = await llm.check_cadence_for_chat(chat_id)
            if not nudge:
                continue
            await context.bot.send_message(chat_id=chat_id, text=nudge)
            db.save_message(chat_id, "assistant", nudge, user_name=None)
        except Exception as e:
            logger.exception(f"Cadence nudge failed for {chat_id}: {e}")


def _remy_is_addressed(update: Update, bot_username: str | None) -> bool:
    """In group chats, Remy should stay quiet unless he's actually being
    talked to. Returns True if any of:
      - message starts with "Remy" (case-insensitive)
      - message is a reply to one of Remy's own messages
      - bot is @-mentioned in the message
    In private DMs, always returns True."""
    msg = update.message
    chat = update.effective_chat
    if chat.type == "private":
        return True
    text = (msg.text or "").strip()
    if re.match(r"(?i)^remy\b", text):
        return True
    if bot_username and f"@{bot_username}".lower() in text.lower():
        return True
    reply = msg.reply_to_message
    if reply and reply.from_user and reply.from_user.is_bot:
        return True
    return False


async def chat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    if not _remy_is_addressed(update, context.bot.username):
        return
    chat = update.effective_chat
    user = update.effective_user

    user_record = db.get_user(user.id)
    name = user_record["name"] if user_record else user.first_name

    try:
        await context.bot.send_chat_action(chat_id=chat.id, action="typing")
        reply = await llm.respond(chat.id, name, update.message.text)
        await update.message.reply_text(reply)
    except Exception as e:
        logger.exception("LLM error")
        await update.message.reply_text(f"(Remy hit an error: {e})")


def _migrate_legacy_tenant() -> None:
    """One-time migration of single-tenant env-var state into the per-chat tables.

    If LEGACY_CHAT_ID env var is set and that chat has no tenant_tokens row,
    move the contents of the legacy tokens file (which FileTokenStorage's init
    will seed from SWIGGY_TOKENS_JSON if needed) into tenant_tokens, and copy
    SWIGGY_DEFAULT_ADDRESS_ID into tenant_settings. Idempotent — safe to run
    on every boot. Once a tenant has its own DB row, this no-ops."""
    legacy_id_raw = os.getenv("LEGACY_CHAT_ID")
    if not legacy_id_raw:
        return
    try:
        chat_id = int(legacy_id_raw)
    except ValueError:
        logger.warning(f"LEGACY_CHAT_ID is not a valid integer: {legacy_id_raw!r}")
        return

    if db.has_tenant_tokens(chat_id):
        return

    # Trigger any SWIGGY_TOKENS_JSON -> file seed before reading
    FileTokenStorage(TOKENS_FILE)
    if not TOKENS_FILE.exists():
        logger.info(f"_migrate_legacy_tenant: no tokens file at {TOKENS_FILE}, skipping")
        return
    try:
        data = json.loads(TOKENS_FILE.read_text())
    except Exception as e:
        logger.warning(f"_migrate_legacy_tenant: couldn't read tokens file: {e}")
        return

    tokens = data.get("tokens")
    if not tokens or not tokens.get("access_token"):
        logger.warning("_migrate_legacy_tenant: legacy file has no usable tokens")
        return

    db.set_tenant_tokens(chat_id, tokens, data.get("client_info"))
    logger.info(f"_migrate_legacy_tenant: tokens migrated for chat_id={chat_id}")

    default_addr = os.getenv("SWIGGY_DEFAULT_ADDRESS_ID")
    if default_addr:
        db.set_default_address(chat_id, default_addr)
        logger.info(f"_migrate_legacy_tenant: default address {default_addr} set for chat_id={chat_id}")


def build_app() -> Application:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set in environment")

    db.init_db()
    _migrate_legacy_tenant()
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(onboarding.build_handler())
    app.add_handler(tenant_setup.build_import_handler())
    app.add_handler(tenant_setup.build_set_address_handler())
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("plan", plan_command))
    app.add_handler(CommandHandler("discovery", discovery_command))
    app.add_handler(CommandHandler("staples", staples))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat_handler))

    # Sunday 6 PM IST weekly plan
    app.job_queue.run_daily(
        callback=send_weekly_plan,
        time=dt_time(hour=18, minute=0, tzinfo=IST),
        days=(6,),  # 0=Mon..6=Sun
        name="weekly_plan",
    )
    logger.info("Scheduled weekly_plan cron for Sunday 18:00 IST")

    # Daily 8 AM IST menu reminder — silent if no plan covers today
    app.job_queue.run_daily(
        callback=send_daily_menu,
        time=dt_time(hour=8, minute=0, tzinfo=IST),
        name="daily_menu",
    )
    logger.info("Scheduled daily_menu cron for daily 08:00 IST")

    # Daily 9 AM IST cadence nudge — silent if nothing's due
    app.job_queue.run_daily(
        callback=send_cadence_nudge,
        time=dt_time(hour=9, minute=0, tzinfo=IST),
        name="cadence_nudge",
    )
    logger.info("Scheduled cadence_nudge cron for daily 09:00 IST")

    # Wednesday 7 PM IST discovery nudge — one novel suggestion mid-week
    app.job_queue.run_daily(
        callback=send_weekly_discovery,
        time=dt_time(hour=19, minute=0, tzinfo=IST),
        days=(2,),  # 0=Mon..2=Wed..6=Sun
        name="weekly_discovery",
    )
    logger.info("Scheduled weekly_discovery cron for Wednesday 19:00 IST")

    return app


def main():
    app = build_app()
    logger.info("Kya Banao? bot starting (polling mode)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
