"""Environment configuration. Fails fast and loudly if anything required is missing.

All secrets and IDs come from the environment (loaded from a .env file in
development). Nothing sensitive is ever hard-coded here or anywhere else in
the app.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Union

from dotenv import load_dotenv

from app import netfix

ROOT = Path(__file__).resolve().parent.parent

load_dotenv(ROOT / ".env")
netfix.install()  # after .env, so PREFER_IPV4=0 there can turn it off


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"{name} is not set. Copy .env.example to .env and fill it in."
        )
    return value


def _require_int(name: str) -> int:
    raw = _require(name)
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _optional_int(name: str) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return 0
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def database_target() -> Union[Path, str]:
    """SUPABASE_DB_URL (a postgres:// connection string) when set, otherwise
    the local SQLite file at DB_PATH. See app/db.py.
    """
    url = os.environ.get("SUPABASE_DB_URL", "").strip()
    if url:
        if not url.startswith(("postgres://", "postgresql://")):
            raise ConfigError(
                "SUPABASE_DB_URL must be the Postgres connection string "
                "(starts with postgresql://), not the project URL or an API key. "
                "In Supabase: Connect -> Session pooler -> URI."
            )
        if "[YOUR-PASSWORD]" in url:
            raise ConfigError(
                "SUPABASE_DB_URL still contains [YOUR-PASSWORD] - replace it with "
                "your database password."
            )
        return url
    db_path = Path(os.environ.get("DB_PATH", "data/skinstinct.db"))
    if not db_path.is_absolute():
        db_path = ROOT / db_path
    return db_path


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    meera_user_id: int
    # 0 = no notes channel configured; notes are then sent by DM to the bot.
    notes_channel_id: int
    gemini_api_key: str
    gemini_model: str  # also transcribes voice notes
    # A SQLite file path, or the Supabase postgres:// URL - see database_target().
    db_path: Union[Path, str]
    batch_timezone: str
    batch_time: str  # "HH:MM"
    # Tried in order when gemini_model is overloaded - see app/llm.py.
    gemini_fallback_models: tuple[str, ...] = ()

    @property
    def batch_hour_minute(self) -> tuple[int, int]:
        hh, mm = self.batch_time.split(":")
        return int(hh), int(mm)


def load_config() -> Config:
    return Config(
        telegram_bot_token=_require("TELEGRAM_BOT_TOKEN"),
        meera_user_id=_require_int("MEERA_USER_ID"),
        notes_channel_id=_optional_int("NOTES_CHANNEL_ID"),
        gemini_api_key=_require("GEMINI_API_KEY"),
        gemini_model=os.environ.get("GEMINI_MODEL", "").strip() or "gemini-3.8-flash",
        db_path=database_target(),
        batch_timezone=os.environ.get("BATCH_TIMEZONE", "Asia/Kolkata").strip(),
        batch_time=os.environ.get("BATCH_TIME", "08:00").strip(),
        gemini_fallback_models=_fallback_models(),
    )


def _fallback_models() -> tuple[str, ...]:
    # Unset = the defaults; set to empty ("GEMINI_FALLBACK_MODELS=") to disable.
    raw = os.environ.get("GEMINI_FALLBACK_MODELS")
    if raw is None:
        from app.llm import DEFAULT_FALLBACK_MODELS

        return tuple(DEFAULT_FALLBACK_MODELS)
    return tuple(m.strip() for m in raw.split(",") if m.strip())
