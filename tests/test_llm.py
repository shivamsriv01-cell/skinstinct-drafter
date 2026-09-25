"""Model-fallback behaviour of app/llm.py, with a fake Gemini client (no
network)."""
from types import SimpleNamespace

import pytest

from app import llm


class FakeModels:
    def __init__(self, script):
        self.script = script  # {model: [exception-or-text, ...]}
        self.calls = []

    def generate_content(self, *, model, contents, config):
        self.calls.append(model)
        outcome = self.script[model].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(text=outcome)


def _client(script, fallbacks=("backup-a", "backup-b")):
    client = llm.LLMClient("key", "main", fallback_models=fallbacks)
    client._client = SimpleNamespace(models=FakeModels(script))
    return client


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)


OVERLOADED = RuntimeError("503 UNAVAILABLE. This model is currently experiencing high demand.")


def test_overload_switches_to_next_model():
    client = _client({"main": [OVERLOADED], "backup-a": ['{"ok": true}']})
    assert client.generate_json("p", response_schema={"type": "object"}) == {"ok": True}
    assert client._client.models.calls == ["main", "backup-a"]


def test_overload_walks_whole_chain():
    client = _client({"main": [OVERLOADED], "backup-a": [OVERLOADED], "backup-b": ['{"n": 1}']})
    assert client.generate_json("p", response_schema={"type": "object"}) == {"n": 1}
    assert client._client.models.calls == ["main", "backup-a", "backup-b"]


def test_non_overload_error_retries_same_model():
    client = _client({"main": [ValueError("bad json"), '{"n": 2}']})
    assert client.generate_json("p", response_schema={"type": "object"}) == {"n": 2}
    assert client._client.models.calls == ["main", "main"]


def test_gives_up_after_retry_budget():
    client = _client({"main": [OVERLOADED, OVERLOADED], "backup-a": [OVERLOADED], "backup-b": [OVERLOADED]})
    with pytest.raises(llm.LLMError):
        client.generate_json("p", response_schema={"type": "object"}, max_retries=4)
    assert client._client.models.calls == ["main", "backup-a", "backup-b", "main"]


def test_no_fallbacks_keeps_retrying_main():
    client = _client({"main": [OVERLOADED, '{"n": 3}']}, fallbacks=())
    assert client.generate_json("p", response_schema={"type": "object"}) == {"n": 3}
    assert client._client.models.calls == ["main", "main"]
