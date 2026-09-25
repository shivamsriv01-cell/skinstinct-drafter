"""Runs the full bot inside one serverless request (Vercel).

The long-polling bot (app/main.py) keeps one Application alive and pulls
updates from Telegram. On Vercel there is no long-lived process, so each
webhook request builds an Application with the very same handlers
(app/main.register_handlers), processes exactly one update through it -
note intake, instant scoring + drafting, Approve/Revise/Reject - and shuts
it down again. The twice-weekly batch runs the same way from a Vercel Cron
request (api/webhook.py's /api/cron/batch).

Budget: a new note makes ~5 Gemini calls plus a news lookup. That fits in
Vercel Hobby's 300s function limit (vercel.json) in normal conditions; if
Gemini is overloaded long enough to blow the limit, the note is already
stored and gets re-scored by the next batch.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from telegram import Update
from telegram.ext import Application

from app import batch, db
from app.config import Config
from app.main import attach_bot_data, register_handlers


def _build(config: Config, conn) -> Application:
    application = Application.builder().token(config.telegram_bot_token).updater(None).build()
    attach_bot_data(application, config, conn)
    return application


async def _process_update(config: Config, update_data: dict) -> None:
    conn = db.open_connection(config.db_path)
    try:
        application = _build(config, conn)
        register_handlers(application)
        async with application:  # initialize() / shutdown()
            await application.process_update(Update.de_json(update_data, application.bot))
    finally:
        conn.close()


async def _run_batch(config: Config) -> None:
    conn = db.open_connection(config.db_path)
    try:
        application = _build(config, conn)
        async with application:
            context = SimpleNamespace(bot=application.bot, bot_data=application.bot_data)
            await batch.run_batch(context)
    finally:
        conn.close()


def process_update(config: Config, update_data: dict) -> None:
    asyncio.run(_process_update(config, update_data))


def run_batch(config: Config) -> None:
    asyncio.run(_run_batch(config))
