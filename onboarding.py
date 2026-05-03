from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

import db

DIET, ALLERGIES, GOALS, LOVED, DISLIKED, SPICE, NOTES = range(7)

DIET_OPTIONS = [["Vegetarian"], ["Non-vegetarian"], ["Eggetarian"], ["Vegan"]]
SPICE_OPTIONS = [["Mild"], ["Medium"], ["Spicy"], ["Extra spicy"]]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    chat = update.effective_chat
    db.upsert_user(user.id, user.first_name, chat.id)

    existing = db.get_user(user.id)
    if existing and existing.get("onboarded"):
        await update.message.reply_text(
            f"Hi {user.first_name}, you're already onboarded. "
            "Send /reonboard to update your profile, or just chat with me about food."
        )
        return ConversationHandler.END

    context.user_data["profile"] = {"name": user.first_name}
    await update.message.reply_text(
        f"Hey {user.first_name}, I'm Remy, kitchen-spirit of this group.\n\n"
        "A few quick questions so I can learn your food rhythms. Your partner should run /start too whenever they're around.\n\n"
        "First, what's your diet?",
        reply_markup=ReplyKeyboardMarkup(DIET_OPTIONS, one_time_keyboard=True, resize_keyboard=True),
    )
    return DIET


async def diet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["profile"]["diet_type"] = update.message.text.strip()
    await update.message.reply_text(
        "Any allergies or things you absolutely can't eat? "
        "(e.g., 'lactose, peanuts', or send 'none')",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ALLERGIES


async def allergies(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    context.user_data["profile"]["allergies"] = "" if text.lower() == "none" else text
    await update.message.reply_text(
        "What are your health or diet goals? "
        "(e.g., 'high protein', 'lighter dinners', 'no sugar', or 'none')"
    )
    return GOALS


async def goals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    context.user_data["profile"]["health_goals"] = "" if text.lower() == "none" else text
    await update.message.reply_text(
        "Which cuisines do you LOVE? (comma-separated, e.g., 'South Indian, Italian, Thai')"
    )
    return LOVED


async def loved(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    cuisines = [c.strip() for c in update.message.text.split(",") if c.strip()]
    context.user_data["profile"]["loved_cuisines"] = cuisines
    await update.message.reply_text(
        "Which cuisines do you DISLIKE or want to avoid? (comma-separated, or 'none')"
    )
    return DISLIKED


async def disliked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text.lower() == "none":
        context.user_data["profile"]["disliked_cuisines"] = []
    else:
        context.user_data["profile"]["disliked_cuisines"] = [
            c.strip() for c in text.split(",") if c.strip()
        ]

    await update.message.reply_text(
        "Spice tolerance?",
        reply_markup=ReplyKeyboardMarkup(SPICE_OPTIONS, one_time_keyboard=True, resize_keyboard=True),
    )
    return SPICE


async def spice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["profile"]["spice_tolerance"] = update.message.text.strip()
    await update.message.reply_text(
        "Last one. Anything else I should know? "
        "(e.g., 'I usually skip dinner', 'partner is on a diet', 'we eat early', or 'none')",
        reply_markup=ReplyKeyboardRemove(),
    )
    return NOTES


async def notes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    context.user_data["profile"]["other_notes"] = "" if text.lower() == "none" else text

    user_id = update.effective_user.id
    db.save_profile(user_id, context.user_data["profile"])
    db.mark_onboarded(user_id)

    profile = context.user_data["profile"]
    summary = (
        f"All set, {profile['name']}!\n\n"
        f"Diet: {profile['diet_type']}\n"
        f"Allergies: {profile['allergies'] or 'none'}\n"
        f"Goals: {profile['health_goals'] or 'none'}\n"
        f"Loves: {', '.join(profile['loved_cuisines']) or 'none'}\n"
        f"Avoids: {', '.join(profile['disliked_cuisines']) or 'none'}\n"
        f"Spice: {profile['spice_tolerance']}\n"
        f"Notes: {profile['other_notes'] or 'none'}\n\n"
        "If your partner hasn't onboarded yet, ask them to send /start.\n"
        "Once both of you are in, I can start planning meals."
    )
    await update.message.reply_text(summary)
    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Cancelled. Send /start whenever you want to onboard.",
        reply_markup=ReplyKeyboardRemove(),
    )
    context.user_data.clear()
    return ConversationHandler.END


def build_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("start", start), CommandHandler("reonboard", start)],
        states={
            DIET: [MessageHandler(filters.TEXT & ~filters.COMMAND, diet)],
            ALLERGIES: [MessageHandler(filters.TEXT & ~filters.COMMAND, allergies)],
            GOALS: [MessageHandler(filters.TEXT & ~filters.COMMAND, goals)],
            LOVED: [MessageHandler(filters.TEXT & ~filters.COMMAND, loved)],
            DISLIKED: [MessageHandler(filters.TEXT & ~filters.COMMAND, disliked)],
            SPICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, spice)],
            NOTES: [MessageHandler(filters.TEXT & ~filters.COMMAND, notes)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
