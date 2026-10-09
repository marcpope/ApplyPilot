"""F16: re-running init must not destroy existing .env keys."""
from applypilot.wizard.init import _merge_env


def test_merge_preserves_unknown_keys():
    out = _merge_env("CAPSOLVER_API_KEY=abc\nGEMINI_API_KEY=old", {"GEMINI_API_KEY": "new"})
    assert "CAPSOLVER_API_KEY=abc" in out
    assert "GEMINI_API_KEY=new" in out
    assert "GEMINI_API_KEY=old" not in out
    # Each key appears exactly once.
    assert out.count("CAPSOLVER_API_KEY=") == 1
    assert out.count("GEMINI_API_KEY=") == 1


def test_merge_appends_new_keys():
    out = _merge_env("CHROME_PATH=/usr/bin/chrome", {"GEMINI_API_KEY": "k"})
    assert "CHROME_PATH=/usr/bin/chrome" in out
    assert "GEMINI_API_KEY=k" in out


def test_merge_from_empty():
    out = _merge_env("", {"GEMINI_API_KEY": "k", "LLM_MODEL": "m"})
    assert "GEMINI_API_KEY=k" in out
    assert "LLM_MODEL=m" in out


def test_merge_keeps_comments():
    out = _merge_env("# my notes\nCAPSOLVER_API_KEY=x", {"LLM_MODEL": "m"})
    assert "# my notes" in out
    assert "CAPSOLVER_API_KEY=x" in out


def test_switching_provider_drops_old_provider_keys():
    from applypilot.wizard.init import _PROVIDER_KEYS
    existing = "GEMINI_API_KEY=g\nLLM_MODEL=gemini-2.0-flash\nCAPSOLVER_API_KEY=c\n"
    out = _merge_env(existing, {"LLM_URL": "https://api.deepseek.com/v1", "LLM_MODEL": "deepseek-chat",
                                "LLM_API_KEY": "sk"}, remove=_PROVIDER_KEYS)
    assert "GEMINI_API_KEY" not in out
    assert "LLM_MODEL=deepseek-chat" in out
    assert "CAPSOLVER_API_KEY=c" in out
