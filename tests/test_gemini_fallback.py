"""Gemini resilience: retries, model fallback, readable 503s, and the chatbot's no-AI answer. No network needed."""
from unittest import mock

import httpx
import pytest
from google.genai import errors

from lifelog import config, gem


@pytest.fixture(autouse=True)
def fake_key(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(config, "GEMINI_FALLBACK_MODELS", ["fallback-a", "fallback-b"])
    monkeypatch.setattr(gem, "_client", None)
    monkeypatch.setattr(gem.time, "sleep", lambda s: None)


def overloaded():
    return errors.ServerError(503, {"error": {"message": "This model is currently experiencing high demand.", "status": "UNAVAILABLE"}})


def quota():
    return errors.ClientError(429, {"error": {"message": "You exceeded your current quota.", "status": "RESOURCE_EXHAUSTED"}})


def test_timeout_fails_fast_with_a_clear_message():
    with mock.patch.object(gem.genai.models.Models, "generate_content", side_effect=httpx.ReadTimeout("slow")):
        with pytest.raises(gem.GeminiUnavailable, match="didn't answer within"):
            gem.generate(model="main", contents="hi")


def test_overloaded_model_is_retried_once_then_succeeds():
    calls = []

    def fake(self, model, **kw):
        calls.append(model)
        if len(calls) == 1:
            raise overloaded()
        return "answer"

    with mock.patch.object(gem.genai.models.Models, "generate_content", fake):
        assert gem.generate(model="main", contents="hi") == "answer"
    assert calls == ["main", "main"] and gem.last_model() == "main"


def test_quota_and_overload_fall_back_to_the_next_model():
    calls = []

    def fake(self, model, **kw):
        calls.append(model)
        if model == "main":
            raise quota()
        if model == "fallback-a":
            raise overloaded()
        return "answer"

    with mock.patch.object(gem.genai.models.Models, "generate_content", fake):
        assert gem.generate(model="main", contents="hi") == "answer"
    assert calls == ["main", "fallback-a", "fallback-a", "fallback-b"]
    assert gem.last_model() == "fallback-b"


def test_all_models_down_gives_googles_reason_per_model():
    with mock.patch.object(gem.genai.models.Models, "generate_content", side_effect=quota()):
        with pytest.raises(gem.GeminiUnavailable) as e:
            gem.generate(model="main", contents="hi")
    msg = str(e.value)
    assert "out of quota" in msg and "main" in msg and "fallback-b" in msg and "keep working" in msg


def test_other_api_errors_are_not_masked_by_fallbacks():
    bad = errors.ClientError(400, {"error": {"message": "Invalid argument", "status": "INVALID_ARGUMENT"}})
    with mock.patch.object(gem.genai.models.Models, "generate_content", side_effect=bad) as m:
        with pytest.raises(gem.GeminiUnavailable, match="400"):
            gem.generate(model="main", contents="hi")
    assert m.call_count == 1


def test_daily_free_tier_quota_is_named_with_its_reset_time():
    e = errors.ClientError(429, {"error": {
        "message": "You exceeded your current quota.", "status": "RESOURCE_EXHAUSTED",
        "details": [{"violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "quotaValue": "20"}]},
                    {"retryDelay": "1251s"}]}})
    assert gem._reason(e) == "429 free-tier limit of 20 requests per day reached, resets in ~21 min"
    with mock.patch.object(gem.genai.models.Models, "generate_content", side_effect=e):
        with pytest.raises(gem.GeminiUnavailable, match="enable billing"):
            gem.generate(model="main", contents="hi")


def test_client_has_a_timeout():
    assert gem.client()._api_client._http_options.timeout == int(config.GEMINI_TIMEOUT_S * 1000)


@pytest.mark.skipif(not config.DATABASE_URL, reason="no DATABASE_URL")
def test_chatbot_answers_from_tiger_when_every_model_is_down():
    from lifelog import assistant
    with mock.patch.object(gem.genai.models.Models, "generate_content", side_effect=quota()):
        r = assistant.ask("Which of my medicines is in the worst shape?")
    assert r["offline"] is True and r["model"] is None
    assert "Most urgent" in r["answer"] and "without AI" in r["answer"]
    first = assistant.list_my_medicines()[0]["nickname"]
    assert first in r["answer"]
