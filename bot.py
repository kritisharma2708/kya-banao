import os
import logging
from datetime import time as dt_time, timezone, timedelta
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import db
import onboarding
import llm

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
        plan = llm.generate_weekly_plan(chat.id)
        await update.message.reply_text(plan)
        db.save_message(chat.id, "assistant", plan, user_name=None)
    except Exception as e:
        logger.exception("Weekly plan error")
        await update.message.reply_text(f"(Couldn't generate plan: {e})")


async def send_weekly_plan(context: ContextTypes.DEFAULT_TYPE):
    """Cron callback: post the Sunday weekly plan to every household group with onboarded users."""
    chat_ids = db.get_chat_ids_with_onboarded_users()
    logger.info(f"Sunday weekly plan firing for {len(chat_ids)} chat(s)")
    for chat_id in chat_ids:
        try:
            plan = llm.generate_weekly_plan(chat_id)
            await context.bot.send_message(chat_id=chat_id, text=plan)
            db.save_message(chat_id, "assistant", plan, user_name=None)
        except Exception as e:
            logger.exception(f"Failed to send weekly plan to {chat_id}: {e}")


async def chat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    chat = update.effective_chat
    user = update.effective_user

    user_record = db.get_user(user.id)
    name = user_record["name"] if user_record else user.first_name

    try:
        await context.bot.send_chat_action(chat_id=chat.id, action="typing")
        reply = llm.respond(chat.id, name, update.message.text)
        await update.message.reply_text(reply)
    except Exception as e:
        logger.exception("LLM error")
        await update.message.reply_text(f"(Remy hit an error: {e})")


def build_app() -> Application:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set in environment")

    db.init_db()
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(onboarding.build_handler())
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("plan", plan_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat_handler))

    # Sunday 6 PM IST weekly plan
    app.job_queue.run_daily(
        callback=send_weekly_plan,
        time=dt_time(hour=18, minute=0, tzinfo=IST),
        days=(6,),  # 0=Mon..6=Sun
        name="weekly_plan",
    )
    logger.info("Scheduled weekly_plan cron for Sunday 18:00 IST")

    return app


def main():
    app = build_app()
    logger.info("Kya Banao? bot starting (polling mode)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
