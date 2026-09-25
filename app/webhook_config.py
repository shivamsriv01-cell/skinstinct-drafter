"""Environment configuration for the Vercel webhook entrypoint (api/webhook.py).

Wraps the same Config the long-polling bot uses (app/config.py - so the
same TELEGRAM_BOT_TOKEN / MEERA_USER_ID / SUPABASE_DB_URL / GEMINI_API_KEY
variables), plus the two webhook-only secrets:

  TELEGRAM_WEBHOOK_SECRET  checked against Telegram's
                           X-Telegram-Bot-Api-Secret-Token header.
  CRON_SECRET              Vercel Cron sends it as "Authorization: Bearer
                           <CRON_SECRET>" on the scheduled batch request.

Allowed chats are derived from MEERA_USER_ID (her private chat) and
NOTES_CHANNEL_ID (if set) - no separate allowlist to keep in sync.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from app.config import Config, ConfigError, load_config


class WebhookConfigError(RuntimeError):
    pass


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise WebhookConfigError(
            f"{name} is not set. Add it in your Vercel project's Environment Variables."
        )
    return value


@dataclass(frozen=True)
class WebhookConfig:
    app: Config
    webhook_secret: str
    cron_secret: str  # "" = cron endpoint disabled
    allowed_chat_ids: frozenset[int]

    @property
    def db_path(self):
        return self.app.db_path


def load_webhook_config() -> WebhookConfig:
    try:
        app_config = load_config()
    except ConfigError as exc:
        raise WebhookConfigError(str(exc)) from exc

    allowed = {app_config.meera_user_id}
    if app_config.notes_channel_id:
        allowed.add(app_config.notes_channel_id)

    return WebhookConfig(
        app=app_config,
        webhook_secret=_require("TELEGRAM_WEBHOOK_SECRET"),
        cron_secret=os.environ.get("CRON_SECRET", "").strip(),
        allowed_chat_ids=frozenset(allowed),
    )
