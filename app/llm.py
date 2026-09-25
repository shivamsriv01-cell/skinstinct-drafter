"""Thin wrapper around the Gemini API (google-genai SDK): retries,
JSON-schema output, voice-note transcription, and logging.

The response schemas in app/drafting.py are written in Gemini's OpenAPI
subset (e.g. `"nullable": true`), so they're passed straight through as
response_schema. Voice notes are transcribed by the same Gemini model
(audio sent inline), so there's no separate transcription service.

Temperature is left at the model default unless a caller passes one:
Google strongly recommends the default (1.0) for all Gemini 3 models -
lower values can cause looping or degraded output.

Every call is logged (component + event + short detail), but request/
response bodies containing note text are not dumped into logs.detail
beyond a short excerpt, and the API key itself is never logged.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional, Sequence

from google import genai
from google.genai import types

from app import db

MAX_RETRIES = 5
RETRY_BACKOFF_SECONDS = 3.0
# 429 (quota/rate limit) and 5xx ("model is overloaded") need longer backoff
# than a generic transient error - short retries just hit the same window.
OVERLOAD_BACKOFF_SECONDS = 8.0

DEFAULT_MODEL = "gemini-3.8-flash"
# Used, in order, when the main model returns an overload error.
DEFAULT_FALLBACK_MODELS = ("gemini-3.6-flash", "gemini-3.5-flash-lite")
MODEL_SWITCH_PAUSE_SECONDS = 1.0

TRANSCRIBE_PROMPT = (
    "Transcribe this voice note verbatim, in the language it is spoken. "
    "Return only the transcript text - no preamble, no timestamps, no speaker labels."
)

_AUDIO_MIME_TYPES = {
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".mp3": "audio/mp3",
    ".wav": "audio/wav",
    ".m4a": "audio/aac",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
}


class LLMError(RuntimeError):
    pass


def _is_overloaded(exc: Exception) -> bool:
    text = str(exc)
    return any(
        marker in text
        for marker in ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE", "overloaded", "500", "INTERNAL", "try again")
    )


def _backoff_seconds(attempt: int, exc: Exception) -> float:
    base = OVERLOAD_BACKOFF_SECONDS if _is_overloaded(exc) else RETRY_BACKOFF_SECONDS
    return base * attempt


def _audio_mime_type(path: Path) -> str:
    # Telegram voice notes are Opus-in-Ogg; that's also the fallback.
    return _AUDIO_MIME_TYPES.get(path.suffix.lower(), "audio/ogg")


class LLMClient:
    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        conn: Optional[sqlite3.Connection] = None,
        *,
        timeout_seconds: Optional[float] = None,
        fallback_models: Sequence[str] = (),
    ):
        http_options = (
            types.HttpOptions(timeout=int(timeout_seconds * 1000)) if timeout_seconds else None
        )
        self._client = genai.Client(api_key=api_key, http_options=http_options)
        self.model = model
        # Tried in order when the current model is overloaded (429/5xx) -
        # Gemini's "high demand" 503s are per-model, so switching usually
        # gets an answer in seconds where waiting on the same model doesn't.
        self._models = [model] + [m for m in fallback_models if m and m != model]
        self._conn = conn

    def _log(self, event: str, **detail: Any) -> None:
        if self._conn is not None:
            db.log(self._conn, "info", "llm", event, **detail)

    def _call_with_retries(self, event: str, retries: int, make_call) -> Any:
        """Runs make_call(model) up to `retries` times. On an overload error
        it moves to the next model in the chain (short pause); on any other
        error it retries the same model with the usual backoff.
        """
        idx = 0
        last_error: Optional[Exception] = None
        for attempt in range(1, retries + 1):
            model = self._models[idx]
            try:
                result = make_call(model)
                self._log(f"{event}.ok", model=model, attempt=attempt)
                return result
            except Exception as exc:  # noqa: BLE001 - we want to retry broadly and log
                last_error = exc
                self._log(f"{event}.error", model=model, attempt=attempt, error=str(exc)[:300])
                if attempt == retries:
                    break
                if _is_overloaded(exc) and len(self._models) > 1:
                    idx = (idx + 1) % len(self._models)
                    time.sleep(MODEL_SWITCH_PAUSE_SECONDS)
                else:
                    time.sleep(_backoff_seconds(attempt, exc))
        raise LLMError(f"Gemini call failed after {retries} attempts: {last_error}")

    def generate_json(
        self,
        prompt: str,
        *,
        response_schema: dict,
        temperature: Optional[float] = None,
        schema_name: str = "response",
        max_retries: Optional[int] = None,
    ) -> dict:
        """Calls the model asking for JSON matching response_schema. Retries
        on transient failures and on invalid JSON.

        schema_name only labels the call in the logs. max_retries overrides
        the module default (MAX_RETRIES) for calls with their own tighter
        retry budget - e.g. the news-keyword extraction step
        (app/drafting.extract_news_keywords), which is specified to give up
        after a single retry and skip the news step rather than hold up
        drafting.
        """
        retries = max_retries if max_retries is not None else MAX_RETRIES
        config = types.GenerateContentConfig(
            temperature=temperature,
            response_mime_type="application/json",
            response_schema=response_schema,
        )

        def call(model: str) -> dict:
            response = self._client.models.generate_content(model=model, contents=prompt, config=config)
            if not response.text:
                raise LLMError("empty response from Gemini")
            return json.loads(response.text)

        return self._call_with_retries(f"generate_json[{schema_name}]", retries, call)

    def transcribe_audio(self, audio_path: Path) -> str:
        """Transcribes a voice note by sending the audio inline to the same
        Gemini model chain. Retries on transient failures.
        """
        audio_part = types.Part.from_bytes(
            data=Path(audio_path).read_bytes(), mime_type=_audio_mime_type(Path(audio_path)),
        )
        config = types.GenerateContentConfig()

        def call(model: str) -> str:
            response = self._client.models.generate_content(
                model=model, contents=[audio_part, TRANSCRIBE_PROMPT], config=config,
            )
            text = (response.text or "").strip()
            if not text:
                raise LLMError("empty transcript from Gemini")
            return text

        return self._call_with_retries("transcribe", MAX_RETRIES, call)
