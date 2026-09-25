"""Vercel Cron entrypoint: GET /api/cron - the twice-weekly batch.

Scheduled in vercel.json (Monday + Thursday 02:30 UTC = 08:00 IST). Vercel
Cron sends "Authorization: Bearer <CRON_SECRET>"; anything without it is a
401, so nobody else can trigger a batch (and its Gemini spend).
"""
from __future__ import annotations

import sys
from pathlib import Path

from flask import Flask, jsonify, request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, webhook_app  # noqa: E402
from app.webhook_config import WebhookConfigError, load_webhook_config  # noqa: E402

app = Flask(__name__)


# Catch-all: Vercel routes /api/cron to this file; matching any path keeps
# it working however the platform presents the request path.
@app.get("/", defaults={"path": ""})
@app.get("/<path:path>")
def cron_batch(path: str):
    try:
        config = load_webhook_config()
    except WebhookConfigError as exc:
        app.logger.error("Cron misconfigured: %s", exc)
        return jsonify(ok=False, error="misconfigured"), 500

    if not config.cron_secret or request.headers.get("Authorization", "") != f"Bearer {config.cron_secret}":
        return jsonify(ok=False, error="unauthorized"), 401

    db.init_db(config.db_path)
    webhook_app.run_batch(config.app)  # failures are reported to Meera by run_batch itself
    return jsonify(ok=True), 200
