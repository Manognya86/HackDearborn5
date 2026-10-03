"""A slow or rate-limited Gemini must fail fast with a readable 503, never hang or 500. No network needed."""
from unittest import mock

import httpx
import pytest
from google.genai import errors

from lifelog import config, gem


@pytest.fixture(autouse=True)
def fake_key(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(gem, "_client", None)


@pytest.mark.parametrize("exc, text", [
    (httpx.ReadTimeout("slow"), "didn't answer"),
    (errors.ClientError(429, {"error": {"message": "quota"}}), "rate-limited"),
    (errors.ServerError(503, {"error": {"message": "overloaded"}}), "error (503)"),
])
def test_gemini_failures_become_unavailable(exc, text):
    with mock.patch.object(gem.genai.models.Models, "generate_content", side_effect=exc):
        with pytest.raises(gem.GeminiUnavailable, match=text.replace("(", r"\(").replace(")", r"\)")):
            gem.generate(model="m", contents="hi")


def test_client_has_a_timeout():
    assert gem.client()._api_client._http_options.timeout == int(config.GEMINI_TIMEOUT_S * 1000)
