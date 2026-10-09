"""Subscription CLI providers: command lines, output parsing, provider routing."""
import json
import subprocess

import pytest

import applypilot.llm as llm


@pytest.fixture
def fake_cli(monkeypatch, tmp_path):
    """Pretend every CLI exists; capture the subprocess call and return canned output."""
    monkeypatch.setattr(llm.shutil, "which", lambda name: f"/bin/{name}")
    calls = []

    def install(stdout="", returncode=0, stderr="", last_message=None):
        def fake_run(cmd, input, cwd, env, **kwargs):
            calls.append({"cmd": cmd, "input": input, "env": env})
            if last_message is not None and "-o" in cmd:
                with open(cmd[cmd.index("-o") + 1], "w") as f:
                    f.write(last_message)
            return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)
        monkeypatch.setattr(llm.subprocess, "run", fake_run)
        return calls

    return install


MESSAGES = [{"role": "system", "content": "Be a scorer."}, {"role": "user", "content": "Job text"}]


def test_claude_cli_uses_system_flag_and_parses_result(fake_cli, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    monkeypatch.delenv("APPLYPILOT_CLAUDE_USE_API_KEY", raising=False)
    calls = fake_cli(stdout=json.dumps({"result": "SCORE: 8", "is_error": False}))
    client = llm.CLIClient("claude-cli", "sonnet")
    assert client.chat(MESSAGES) == "SCORE: 8"
    cmd = calls[0]["cmd"]
    assert cmd[cmd.index("--system-prompt") + 1] == "Be a scorer."
    assert cmd[cmd.index("--setting-sources") + 1] == ""
    assert calls[0]["input"] == "Job text"
    assert "ANTHROPIC_API_KEY" not in calls[0]["env"]


def test_codex_cli_reads_last_message_file(fake_cli, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    calls = fake_cli(stdout="noise", last_message="SCORE: 6\n")
    client = llm.CLIClient("codex-cli", None, reasoning="low")
    assert client.chat(MESSAGES) == "SCORE: 6"
    cmd = calls[0]["cmd"]
    assert 'model_reasoning_effort="low"' in cmd
    assert calls[0]["input"].startswith("Be a scorer.")
    assert "OPENAI_API_KEY" not in calls[0]["env"]


def test_gemini_cli_parses_json_response(fake_cli, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    calls = fake_cli(stdout=json.dumps({"response": "SCORE: 9", "stats": {}}))
    assert llm.CLIClient("gemini-cli", "gemini-2.5-flash").chat(MESSAGES) == "SCORE: 9"
    assert "GEMINI_API_KEY" not in calls[0]["env"]


def test_usage_limit_fails_fast(fake_cli):
    calls = fake_cli(returncode=1, stderr="You've hit your limit. Your limit will reset at 5pm.")
    with pytest.raises(RuntimeError, match="usage limit"):
        llm.CLIClient("codex-cli").chat(MESSAGES)
    assert len(calls) == 1


def test_transient_failure_retries(fake_cli, monkeypatch):
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    calls = fake_cli(returncode=1, stderr="connection reset")
    with pytest.raises(RuntimeError, match="connection reset"):
        llm.CLIClient("claude-cli").chat(MESSAGES)
    assert len(calls) == llm._CLI_RETRIES


def test_per_stage_provider_resolution(monkeypatch):
    for key in ("LLM_PROVIDER", "LLM_MODEL", "LLM_PROVIDER_SCORE", "LLM_MODEL_SCORE", "LLM_PROVIDER_TAILOR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "codex-cli")
    monkeypatch.setenv("LLM_MODEL", "gpt-x")
    monkeypatch.setenv("LLM_PROVIDER_SCORE", "gemini-cli")
    assert llm.resolve_provider("tailor") == ("codex-cli", "gpt-x")
    # The global model belongs to the global provider, not the override.
    assert llm.resolve_provider("score") == ("gemini-cli", "")
    monkeypatch.setenv("LLM_MODEL_SCORE", "gemini-2.5-flash")
    assert llm.resolve_provider("score") == ("gemini-cli", "gemini-2.5-flash")


def test_unknown_provider_rejected(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "chatgpt")
    with pytest.raises(RuntimeError, match="Unknown LLM_PROVIDER"):
        llm.resolve_provider("score")
