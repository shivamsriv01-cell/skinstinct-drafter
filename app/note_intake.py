"""Shared note-storage logic for both places a note can arrive from:
app/collector.py (the notes channel) and app/review.py (Meera's own private
DM with the bot, when she's not replying to a pending Revise/Reject prompt).

Kept in its own module, rather than in collector.py or review.py, so that
neither of those needs to import the other - app/instant.py already imports
app/review.py (to send the finished draft back), and collector.py needs
app/instant.py, so collector importing review directly would create an
import cycle.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from telegram import Message
from telegram.ext import ContextTypes

from app import db

ROOT = Path(__file__).resolve().parent.parent
# Vercel's filesystem is read-only except /tmp. Voice notes are transcribed
# in the same request they arrive in (app/instant.py), so /tmp not
# persisting between requests doesn't lose anything.
AUDIO_DIR = Path("/tmp/audio") if os.environ.get("VERCEL") else ROOT / "data" / "audio"


def _stored_audio_path(dest: Path) -> str:
    # Relative to the project when possible (portable between machines);
    # absolute otherwise (e.g. /tmp on Vercel). Readers do ROOT / path,
    # which leaves an absolute path unchanged.
    try:
        return str(dest.relative_to(ROOT))
    except ValueError:
        return str(dest)


async def store_note(message: Message, context: ContextTypes.DEFAULT_TYPE, conn: sqlite3.Connection) -> int | None:
    """Stores a text or voice message as a note. Returns the new note id, or
    None if the message had nothing to store (e.g. a sticker, a photo with
    no caption).
    """
    created_at = message.date.isoformat() if message.date else db.now_iso()

    if message.voice is not None or message.audio is not None:
        media = message.voice or message.audio
        AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        file = await context.bot.get_file(media.file_id)
        dest = AUDIO_DIR / f"{message.message_id}_{media.file_unique_id}.ogg"
        await file.download_to_drive(custom_path=str(dest))
        note_id = db.insert_note(
            conn,
            source="telegram",
            type_="voice",
            created_at=created_at,
            tg_message_id=message.message_id,
            audio_path=_stored_audio_path(dest),
        )
        db.log(conn, "info", "collector", "voice_note_stored", note_id=note_id)
        conn.commit()
        return note_id

    text = message.text or message.caption
    if text:
        note_id = db.insert_note(
            conn,
            source="telegram",
            type_="text",
            created_at=created_at,
            tg_message_id=message.message_id,
            raw_text=text,
        )
        db.log(conn, "info", "collector", "text_note_stored", note_id=note_id)
        conn.commit()
        return note_id

    return None
