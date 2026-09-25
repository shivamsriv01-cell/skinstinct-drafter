"""Entry point. Starts the Telegram bot: registers handlers, schedules the
twice-weekly batch job (Monday and Thursday at BATCH_TIME, BATCH_TIMEZONE),
and runs long-polling.

Run with: python -m app.main
"""
from __future__ import annotations

import datetime as dt
import logging

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app import auth, batch, db, review
from app.collector import handle_channel_post
from app.config import load_config
from app.llm import LLMClient

# JobQueue.run_daily's `days` tuple: 0=Sunday .. 6=Saturday (python-telegram-bot v21+).
MONDAY = 1
THURSDAY = 4

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("skinstinct")
# httpx logs every request URL at INFO, and Telegram URLs contain the bot
# token - keep it out of the console/log files.
logging.getLogger("httpx").setLevel(logging.WARNING)


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Last-resort catch-all: without this, an uncaught exception in any
    handler just vanishes into the logs and Meera sees nothing happen (this
    is exactly what happened before this handler was added - see the fix
    for the silent retry failure in app/llm.py / app/review.py).
    """
    config = context.bot_data.get("config")
    conn = context.bot_data.get("conn")
    error = context.error
    logger.exception("Unhandled exception while processing an update", exc_info=error)
    if conn is not None:
        db.log(conn, "error", "main", "unhandled_exception", error=str(error))
        conn.commit()
    if config is not None:
        try:
            await context.bot.send_message(
                chat_id=config.meera_user_id,
                text=f"Something went wrong on my end: {error}\n\nNothing should be lost - try that again.",
            )
        except Exception:  # noqa: BLE001 - never let the error handler itself crash the bot
            logger.exception("Also failed to notify Meera about the error above.")


async def _on_start(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = context.bot_data["config"]
    if not auth.is_meera_private_chat(update, config):
        return
    await update.effective_message.reply_text(
        "Hi! I'm connected and ready.\n\n"
        "Send me a note - typed or a voice message - and I'll score it and, "
        "if it has enough substance, reply with a LinkedIn draft to "
        "Approve / Revise / Reject."
    )


async def _on_startup(context: ContextTypes.DEFAULT_TYPE) -> None:
    config = context.bot_data["config"]
    conn = context.bot_data["conn"]
    db.log(conn, "info", "main", "started")
    conn.commit()
    logger.info("Skinstinct drafting bot started. Waiting for Meera's channel notes.")


def register_handlers(application: Application) -> None:
    """The collector, review-flow and error handlers. Shared by the
    long-polling bot (below) and the Vercel webhook (app/webhook_app.py), so
    both deployments behave identically.
    """
    # /start: instant confirmation the bot is alive (owner only; everyone
    # else is ignored silently, like every other handler).
    application.add_handler(CommandHandler("start", _on_start))

    # Collector: any message posted in the notes channel.
    application.add_handler(
        MessageHandler(filters.ChatType.CHANNEL & (filters.TEXT | filters.VOICE | filters.AUDIO), handle_channel_post)
    )

    # Review flow: button presses, Meera's free-text/voice replies (revise
    # instructions / reject reasons), and notes dropped directly into her
    # private chat as an alternative to the channel.
    application.add_handler(CallbackQueryHandler(review.handle_callback))
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & (filters.TEXT | filters.VOICE | filters.AUDIO) & ~filters.COMMAND,
            review.handle_private_message,
        )
    )
    application.add_error_handler(_on_error)


def attach_bot_data(application: Application, config, conn) -> None:
    application.bot_data["config"] = config
    application.bot_data["conn"] = conn
    application.bot_data["llm"] = LLMClient(
        config.gemini_api_key, config.gemini_model, conn=conn,
        fallback_models=config.gemini_fallback_models,
    )


def build_application() -> Application:
    config = load_config()
    db.init_db(config.db_path)
    conn = db.open_connection(config.db_path)

    application = (
        Application.builder()
        .token(config.telegram_bot_token)
        # Without this, updates are processed one at a time - a second tap
        # (e.g. Meera retrying "Develop" while the first pick is still
        # waiting on a slow/retrying Gemini call) queues up behind it and
        # its callback query has often expired by the time it's handled,
        # producing a confusing silent "Query is too old" failure.
        .concurrent_updates(True)
        .build()
    )
    attach_bot_data(application, config, conn)
    register_handlers(application)

    hour, minute = config.batch_hour_minute
    run_time = dt.time(hour=hour, minute=minute, tzinfo=dt.timezone.utc)
    # run_daily's tzinfo on `time` controls the wall-clock time interpretation;
    # pass the configured IANA zone via Defaults/tzinfo if available, else UTC
    # with a note - see README for timezone setup detail.
    try:
        from zoneinfo import ZoneInfo

        run_time = dt.time(hour=hour, minute=minute, tzinfo=ZoneInfo(config.batch_timezone))
    except Exception:  # noqa: BLE001
        logger.warning(
            "Could not load timezone %s; batch will run at %02d:%02d UTC instead.",
            config.batch_timezone, hour, minute,
        )

    application.job_queue.run_daily(
        batch.run_batch, time=run_time, days=(MONDAY, THURSDAY), name="twice_weekly_batch"
    )
    # Scheduled at build time but only runs once the app has connected to
    # Telegram, which can take several seconds; without a grace period
    # APScheduler drops it as "missed" (its default grace is 1s).
    application.job_queue.run_once(_on_startup, when=0, job_kwargs={"misfire_grace_time": 120})

    return application


def main() -> None:
    application = build_application()
    application.run_polling(allowed_updates=["message", "channel_post", "callback_query"])


if __name__ == "__main__":
    main()
