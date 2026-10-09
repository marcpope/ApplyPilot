"""Keep tests away from the developer's real ~/.applypilot and LLM settings."""
import os
import tempfile

import pytest

# Must happen before applypilot.config is imported: APP_DIR is read at import.
os.environ["APPLYPILOT_DIR"] = tempfile.mkdtemp(prefix="applypilot-test-")

_LLM_ENV_PREFIXES = ("LLM_", "GEMINI_", "OPENAI_", "ANTHROPIC_", "CODEX_", "APPLY_EMAIL", "APPLYPILOT_CLAUDE")


@pytest.fixture(autouse=True)
def _clean_llm_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith(_LLM_ENV_PREFIXES):
            monkeypatch.delenv(key, raising=False)
