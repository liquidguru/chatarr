#!/usr/bin/env python3
"""
Telegram frontend. Shares all logic with the web frontend via core.py
(both call core.process_request). Talks to Sonarr + Radarr.
"""

import asyncio
import logging
import os

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from core import (
    ADMIN_ID,
    ALLOWED_USERS,
    is_allowed,
    process_request,
    save_users,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]

# ── Telegram handlers ─────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("You're not authorised. Use /myid and ask the admin to add you.")
        return
    await update.message.reply_text(
        "Hey! Ask me anything about movies and TV shows.\n\n"
        "Examples:\n"
        "  show me Jason Statham movies\n"
        "  what action movies do we have?\n"
        "  TV shows like Breaking Bad\n"
        "  add Severance\n"
        "  what's downloading?"
    )

async def cmd_myid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = update.effective_user.full_name
    await update.message.reply_text(f"{name}, your Telegram ID is: {uid}")

async def cmd_allow(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /allow <user_id>")
        return
    uid = int(ctx.args[0])
    ALLOWED_USERS.add(uid)
    save_users(ALLOWED_USERS)
    await update.message.reply_text(f"Added {uid}.")

async def cmd_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /remove <user_id>")
        return
    uid = int(ctx.args[0])
    ALLOWED_USERS.discard(uid)
    save_users(ALLOWED_USERS)
    await update.message.reply_text(f"Removed {uid}.")

async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text(f"Allowed user IDs: {sorted(ALLOWED_USERS)}")

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("You're not authorised. Use /myid and ask the admin to add you.")
        return
    await ctx.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    user_id = update.effective_user.id
    try:
        reply = await asyncio.to_thread(process_request, user_id, update.message.text)
    except Exception as e:
        log.error(f"Error: {e}")
        reply = "Something went wrong — please try again."
    await update.message.reply_text(reply)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("myid",   cmd_myid))
    app.add_handler(CommandHandler("allow",  cmd_allow))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("users",  cmd_users))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    log.info("media-bot started")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
