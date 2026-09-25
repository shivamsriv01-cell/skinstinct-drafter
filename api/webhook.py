"""Vercel serverless entrypoint: POST /api/webhook, Telegram's webhook.

Runs the full bot for one update (app/webhook_app.py): note intake,
instant score + draft, and the Approve / Revise / Reject buttons. The
twice-weekly batch is a separate function, api/cron.py - Vercel maps each
file under api/ to exactly one path, so it can't live in this file.

Webhook hardening, in order:
  1. Verifies the X-Telegram-Bot-Api-Secret-Token header against
     TELEGRAM_WEBHOOK_SECRET before anything else; a mismatch is a 401 and
     nothing is read from the body.
  2. Reads "message", "channel_post" or "callback_query", and only
     processes chats in the allowlist (MEERA_USER_ID + NOTES_CHANNEL_ID);
     anything else is ignored silently to the sender, but logged.
  3. Ignores anything sent by a bot, so a draft the bot posts can never
     trigger another run.
  4. Claims the update_id (processed_updates table) BEFORE processing.
     Drafting can take a minute or more; if Telegram gives up waiting and
     re-sends the update meanwhile, the retry finds it claimed and is a
     no-op instead of a second draft. The trade-off: an update that fails
     mid-way isn't retried - the bot's error handler tells Meera instead,
     and the note itself is already stored for the next batch.
  5. Always returns 200 for anything handled, ignored, deduped or failed,
     so Telegram stops retrying. Only a bad secret gets a non-200.
"""
from __future__ import annotations

import sys
from pathlib import Path

from flask import Flask, jsonify, request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, webhook_app  # noqa: E402
from app.webhook_config import WebhookConfigError, load_webhook_config  # noqa: E402

app = Flask(__name__)


def _extract_ids(update: dict) -> tuple[int | None, dict | None]:
    """Returns (chat_id, sender) for whichever update shape this is."""
    message = update.get("message") or update.get("channel_post")
    if message is not None:
        return message.get("chat", {}).get("id"), message.get("from")

    callback_query = update.get("callback_query")
    if callback_query is not None:
        chat_id = callback_query.get("message", {}).get("chat", {}).get("id")
        return chat_id, callback_query.get("from")

    return None, None


@app.post("/api/webhook")
def webhook():
    try:
        config = load_webhook_config()
    except WebhookConfigError as exc:
        # A misconfigured deployment isn't something Telegram retrying will
        # fix - log it loudly, but still 200 so it doesn't hammer us.
        app.logger.error("Webhook misconfigured: %s", exc)
        return jsonify(ok=False, error="misconfigured"), 200

    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if secret != config.webhook_secret:
        return jsonify(ok=False, error="unauthorized"), 401

    update = request.get_json(silent=True)
    if not isinstance(update, dict):
        return jsonify(ok=False, error="bad_json"), 200

    update_id = update.get("update_id")
    chat_id, sender = _extract_ids(update)

    db.init_db(config.db_path)  # idempotent; creates tables on a fresh database
    conn = db.open_connection(config.db_path)
    try:
        if update_id is not None and not db.claim_update(conn, update_id):
            return jsonify(ok=True, status="duplicate"), 200

        if chat_id is None or chat_id not in config.allowed_chat_ids:
            db.log(conn, "info", "webhook", "ignored_chat", chat_id=chat_id, update_id=update_id)
            conn.commit()
            return jsonify(ok=True, status="ignored_chat"), 200

        if sender is not None and sender.get("is_bot"):
            db.log(conn, "info", "webhook", "ignored_self", update_id=update_id)
            conn.commit()
            return jsonify(ok=True, status="ignored_self"), 200
        conn.commit()
    finally:
        conn.close()

    try:
        webhook_app.process_update(config.app, update)
    except Exception:  # noqa: BLE001 - Telegram must still get a 200
        app.logger.exception("Unhandled error processing update %s", update_id)
        return jsonify(ok=False, error="internal_error"), 200
    return jsonify(ok=True), 200
