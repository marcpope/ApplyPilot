"""LLM client: provider detection, empty/thinking responses, error messages."""
import httpx
import pytest

import applypilot.llm as llm


def _env(monkeypatch, **values):
    for key in ("GEMINI_API_KEY", "OPENAI_API_KEY", "LLM_URL", "LLM_MODEL", "LLM_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_gemini_default_model_is_available_to_new_keys(monkeypatch):
    _env(monkeypatch, GEMINI_API_KEY="k")
    _, model, _ = llm._detect_provider()
    assert model == "gemini-2.5-flash"


def test_bare_ollama_url_gets_v1(monkeypatch):
    _env(monkeypatch, LLM_URL="http://127.0.0.1:11434", LLM_MODEL="llama3")
    base, _, _ = llm._detect_provider()
    assert base == "http://127.0.0.1:11434/v1"


def test_explicit_url_path_left_alone(monkeypatch):
    _env(monkeypatch, LLM_URL="https://api.deepseek.com/v1/", LLM_API_KEY="sk")
    base, _, key = llm._detect_provider()
    assert base == "https://api.deepseek.com/v1"
    assert key == "sk"


def _client_returning(handler):
    client = llm.LLMClient("https://example.test/v1", "some-model", "key")
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def test_think_block_stripped():
    client = _client_returning(lambda req: httpx.Response(200, json={
        "choices": [{"message": {"content": "<think>hmm</think>\nSCORE: 8"}, "finish_reason": "stop"}],
    }))
    assert client.ask("q") == "SCORE: 8"


@pytest.mark.parametrize("content", [None, "", "<think>ran out of budget</think>"])
def test_empty_content_raises(content):
    client = _client_returning(lambda req: httpx.Response(200, json={
        "choices": [{"message": {"content": content}, "finish_reason": "length"}],
    }))
    with pytest.raises(RuntimeError, match="empty response"):
        client.ask("q")


def test_http_error_includes_provider_message():
    body = '{"error": {"message": "This model is no longer available to new users."}}'
    client = _client_returning(lambda req: httpx.Response(404, text=body))
    with pytest.raises(RuntimeError, match="no longer available"):
        client.ask("q")


def test_no_think_reaches_user_message_after_system():
    seen = {}

    def handler(req):
        import json
        seen["messages"] = json.loads(req.content)["messages"]
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = _client_returning(handler)
    client.model = "qwen3:8b"
    client.chat([{"role": "system", "content": "rules"}, {"role": "user", "content": "go"}])
    assert seen["messages"][1]["content"].startswith("/no_think")
    assert seen["messages"][0]["content"] == "rules"
