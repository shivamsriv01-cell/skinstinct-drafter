"""Tests for the Vercel entrypoint (api/webhook.py): the webhook hardening
and the cron endpoint. The actual bot processing (app/webhook_app.py) is
replaced with a recorder, so no Telegram/Gemini/network calls happen here.
"""
import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ENV = {
    "TELEGRAM_BOT_TOKEN": "123:abc",
    "TELEGRAM_WEBHOOK_SECRET": "test-secret",
    "MEERA_USER_ID": "111",
    "NOTES_CHANNEL_ID": "-100222",
    "GEMINI_API_KEY": "x",
    "CRON_SECRET": "cron-secret",
}


@pytest.fixture()
def calls(tmp_path, monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "webhook_test.db"))
    # A real .env may set this; tests must never touch the live Supabase DB.
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)

    import api.cron as cron_module
    import api.webhook as webhook_module

    importlib.reload(webhook_module)
    importlib.reload(cron_module)
    recorded = {"updates": [], "batches": 0}

    def fake_process(config, update):
        recorded["updates"].append(update)

    def fake_batch(config):
        recorded["batches"] += 1

    monkeypatch.setattr(webhook_module.webhook_app, "process_update", fake_process)
    monkeypatch.setattr(cron_module.webhook_app, "run_batch", fake_batch)
    webhook_module.app.config["TESTING"] = True
    recorded["client"] = webhook_module.app.test_client()
    recorded["cron"] = cron_module.app.test_client()
    return recorded


def _post(calls, body, secret="test-secret"):
    headers = {"X-Telegram-Bot-Api-Secret-Token": secret} if secret is not None else {}
    return calls["client"].post("/api/webhook", json=body, headers=headers)


def _text_update(update_id=1, chat_id=111, text="hello", is_bot=False):
    return {
        "update_id": update_id,
        "message": {
            "message_id": 1,
            "date": 1700000000,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id, "is_bot": is_bot},
            "text": text,
        },
    }


def test_missing_secret_is_401(calls):
    assert _post(calls, _text_update(), secret=None).status_code == 401
    assert calls["updates"] == []


def test_wrong_secret_is_401(calls):
    assert _post(calls, _text_update(), secret="wrong").status_code == 401
    assert calls["updates"] == []


def test_allowed_private_message_is_processed(calls):
    resp = _post(calls, _text_update(update_id=1, text="a real note"))
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    assert [u["message"]["text"] for u in calls["updates"]] == ["a real note"]


def test_disallowed_chat_is_ignored_but_200(calls):
    resp = _post(calls, _text_update(update_id=2, chat_id=999))
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ignored_chat"
    assert calls["updates"] == []


def test_bot_own_message_is_ignored_but_200(calls):
    resp = _post(calls, _text_update(update_id=3, is_bot=True))
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ignored_self"
    assert calls["updates"] == []


def test_duplicate_update_id_is_processed_once(calls):
    assert _post(calls, _text_update(update_id=4)).get_json().get("status") != "duplicate"
    second = _post(calls, _text_update(update_id=4))
    assert second.status_code == 200
    assert second.get_json()["status"] == "duplicate"
    assert len(calls["updates"]) == 1


def test_channel_post_from_notes_channel_is_processed(calls):
    update = {
        "update_id": 5,
        "channel_post": {
            "message_id": 2,
            "date": 1700000000,
            "chat": {"id": -100222, "type": "channel"},
            "text": "a note from the channel",
        },
    }
    assert _post(calls, update).status_code == 200
    assert len(calls["updates"]) == 1


def test_button_tap_is_processed(calls):
    update = {
        "update_id": 6,
        "callback_query": {
            "id": "cb1",
            "from": {"id": 111, "is_bot": False},
            "message": {"message_id": 9, "date": 1700000000, "chat": {"id": 111, "type": "private"}},
            "data": "appr:1",
        },
    }
    assert _post(calls, update).status_code == 200
    assert calls["updates"][0]["callback_query"]["data"] == "appr:1"


def test_processing_error_still_returns_200(calls, monkeypatch):
    import api.webhook as webhook_module

    def boom(config, update):
        raise RuntimeError("gemini down")

    monkeypatch.setattr(webhook_module.webhook_app, "process_update", boom)
    resp = _post(calls, _text_update(update_id=7))
    assert resp.status_code == 200
    assert resp.get_json()["error"] == "internal_error"


def test_bad_json_body_returns_200_not_500(calls):
    headers = {"X-Telegram-Bot-Api-Secret-Token": "test-secret", "Content-Type": "application/json"}
    resp = calls["client"].post("/api/webhook", data="not json", headers=headers)
    assert resp.status_code == 200


def test_cron_requires_secret(calls):
    assert calls["cron"].get("/api/cron").status_code == 401
    assert calls["cron"].get("/api/cron", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert calls["batches"] == 0


def test_cron_runs_batch_with_secret(calls):
    resp = calls["cron"].get("/api/cron", headers={"Authorization": "Bearer cron-secret"})
    assert resp.status_code == 200
    assert calls["batches"] == 1


def test_missing_env_config_returns_200_not_crash(monkeypatch, tmp_path):
    for k in ("TELEGRAM_WEBHOOK_SECRET", "TELEGRAM_BOT_TOKEN", "MEERA_USER_ID", "GEMINI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)

    import api.webhook as webhook_module

    importlib.reload(webhook_module)
    webhook_module.app.config["TESTING"] = True
    c = webhook_module.app.test_client()

    resp = c.post("/api/webhook", json=_text_update(), headers={"X-Telegram-Bot-Api-Secret-Token": "anything"})
    assert resp.status_code == 200
    assert resp.get_json()["error"] == "misconfigured"
