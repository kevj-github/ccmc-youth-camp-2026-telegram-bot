import os
import json
import logging
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from claude_handler import ask_claude

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("logs/bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ALLOWED_CHAT_IDS = list(map(int, os.environ.get("ALLOWED_CHAT_IDS", "").split(",")))


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user

    if ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS:
        logger.warning(f"Unauthorized access attempt from chat_id={chat_id}")
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return

    text = update.message.text or ""
    logger.info(f"Message from {user.first_name} ({chat_id}): {text}")

    thinking_msg = await update.message.reply_text("⏳ Thinking...")

    try:
        reply = ask_claude(text)
    except Exception as e:
        logger.error(f"Error from Claude: {e}", exc_info=True)
        reply = f"❌ Something went wrong: {e}"

    await thinking_msg.delete()
    await update.message.reply_text(reply, parse_mode="Markdown")


async def handle_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "I only understand text messages. Type something like:\n"
        "• _How many campers are registered?_\n"
        "• _Show unpaid registrations_\n"
        "• _Analyze photo john\\_doe.jpg_",
        parse_mode="Markdown"
    )


def main():
    logger.info("Starting Youth Camp Bot...")
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(~filters.TEXT, handle_unknown))
    logger.info("Bot is running. Press Ctrl+C to stop.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
