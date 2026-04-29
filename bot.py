import os
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

import db
import onboarding

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
            lines.append(f"✅ {p['name']} — {p['diet_type']}")
        else:
            lines.append(f"⏳ {p['name']} — onboarding incomplete")
    await update.message.reply_text("\n".join(lines))


def build_app() -> Application:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set in environment")

    db.init_db()
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(onboarding.build_handler())
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("status", status))

    return app


def main():
    app = build_app()
    logger.info("Kya Banao? bot starting (polling mode)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
