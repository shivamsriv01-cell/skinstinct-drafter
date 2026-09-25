"""CLI entrypoint: python -m app <command>.

Commands:
  check                       Verifies .env end to end before you start the
                               bot: config loads, the database (Supabase or
                               SQLite) connects and the tables exist, the
                               Telegram bot token works, and the Gemini key
                               works. Makes no changes beyond creating the
                               tables if they're missing.
  news-test --note "<text>"   Runs just the news step (keyword extraction +
                               ranked fetch) against a note, without
                               drafting or touching the database - prints
                               the keyword JSON, the queries/URLs tried, how
                               many items survived the filter at each step,
                               and the final top 3 (or none).
"""
from __future__ import annotations

import argparse
import json
import sys

from app.config import load_config
from app.drafting import extract_news_keywords
from app.llm import LLMClient
from app.news import fetch_ranked_news
from app.news_config import INDIA_CATEGORIES


def _news_test(note_text: str) -> None:
    config = load_config()
    client = LLMClient(config.gemini_api_key, config.gemini_model, timeout_seconds=15)

    keywords = extract_news_keywords(client, note_text=note_text)
    print("=== keyword extraction ===")
    print(json.dumps(
        {
            "newsworthy": keywords.newsworthy,
            "category": keywords.category,
            "keywords": keywords.keywords,
            "specific_query": keywords.specific_query,
            "broader_query": keywords.broader_query,
        },
        indent=2,
    ))

    if not keywords.newsworthy:
        print("\nnot newsworthy - no requests made")
        return

    add_india = keywords.category in INDIA_CATEGORIES
    items, log = fetch_ranked_news(
        specific_query=keywords.specific_query,
        broader_query=keywords.broader_query,
        category=keywords.category,
        keywords=keywords.keywords,
        add_india=add_india,
    )

    print(f"\n=== fetch (add_india={add_india}) ===")
    for i, (query, url) in enumerate(zip(log.queries_tried, log.urls)):
        kept = log.items_kept_per_step[i] if i < len(log.items_kept_per_step) else 0
        print(f"  [{i}] {query}")
        print(f"      url: {url}")
        print(f"      kept: {kept}")
    print(f"  stopped_at: {log.stopped_at}")
    print(f"  elapsed_seconds: {log.elapsed_seconds:.2f}")

    print(f"\n=== top {len(items)} (of up to 3) ===")
    for i, item in enumerate(items):
        print(f"  [{i}] \"{item.title}\" - {item.source} ({item.published_at[:10]})")
        print(f"      {item.link}")
    if not items:
        print("  none")


def _check() -> bool:
    import httpx
    from google import genai
    from google.genai import types

    from app import db
    from app.config import ConfigError

    ok = True

    def report(name: str, passed: bool, detail: str) -> None:
        nonlocal ok
        ok = ok and passed
        print(f"[{'OK' if passed else 'FAIL'}] {name}: {detail}")

    try:
        config = load_config()
    except ConfigError as exc:
        report(".env", False, str(exc))
        return False
    report(".env", True, "all required values present")

    backend = "Supabase (Postgres)" if db.is_postgres(config.db_path) else f"local SQLite at {config.db_path}"
    try:
        db.init_db(config.db_path)
        with db.connect(config.db_path) as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM notes").fetchone()
        report("database", True, f"{backend} - tables ready, {row['n']} notes stored")
    except Exception as exc:  # noqa: BLE001 - report, don't crash
        report("database", False, f"{backend} - {exc}")

    try:
        resp = httpx.get(f"https://api.telegram.org/bot{config.telegram_bot_token}/getMe", timeout=15)
        data = resp.json()
        if data.get("ok"):
            report("telegram", True, f"bot @{data['result']['username']} is reachable")
        else:
            report("telegram", False, f"Telegram rejected the token: {data.get('description')}")
    except Exception as exc:  # noqa: BLE001
        report("telegram", False, str(exc))

    channel = str(config.notes_channel_id) if config.notes_channel_id else "none (send notes to the bot by DM)"
    print(f"       owner user id: {config.meera_user_id}, notes channel: {channel}")

    try:
        client = genai.Client(api_key=config.gemini_api_key, http_options=types.HttpOptions(timeout=15000))
        client.models.get(model=config.gemini_model)
        report("gemini", True, f"key works, model {config.gemini_model} available")
    except Exception as exc:  # noqa: BLE001
        report("gemini", False, str(exc))

    print("\nAll good - start the bot with run.bat" if ok else "\nFix the FAIL lines above in .env, then run this again.")
    return ok


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("check", help="Verify .env: database, Telegram and Gemini")

    news_test = subparsers.add_parser("news-test", help="Run just the news step against a note")
    news_test.add_argument("--note", required=True, help="The note text to test")

    args = parser.parse_args(argv)

    if args.command == "check":
        sys.exit(0 if _check() else 1)
    elif args.command == "news-test":
        _news_test(args.note)


if __name__ == "__main__":
    main(sys.argv[1:])
