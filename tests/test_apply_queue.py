"""Apply queue: status count matches the queue; hung agents time out; Windows CLI lookup."""
import sys

import applypilot.apply.launcher as launcher
import applypilot.apply.prompt as prompt_mod
import applypilot.config as config
import applypilot.database as db


def _seed(conn, url, **cols):
    row = {"url": url, "title": "Engineer", "site": "linkedin",
           "tailored_resume_path": "/tmp/r.txt", "fit_score": 8}
    row.update(cols)
    keys = ", ".join(row)
    conn.execute(f"INSERT INTO jobs ({keys}) VALUES ({', '.join('?' * len(row))})", list(row.values()))
    conn.commit()


def test_ready_count_matches_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(launcher, "_load_blocked", lambda: ([], []))
    conn = db.init_db()
    _seed(conn, "https://e.com/ready")                                   # no application_url
    _seed(conn, "https://e.com/manual", apply_status="manual")
    _seed(conn, "https://e.com/maxed", apply_status="failed", apply_attempts=99)
    _seed(conn, "https://e.com/low", fit_score=5)
    _seed(conn, "https://e.com/applied", apply_status="applied", applied_at="2026-01-01")

    assert db.get_stats(conn)["ready_to_apply"] == 1
    job = launcher.acquire_job(min_score=7)
    assert job["url"] == "https://e.com/ready"
    assert launcher.acquire_job(min_score=7) is None


def test_watchdog_kills_hung_agent(tmp_path, monkeypatch):
    """A job whose agent never exits is failed as a timeout, not hung forever."""
    monkeypatch.setattr(config, "LOG_DIR", tmp_path)
    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    monkeypatch.setattr(launcher, "JOB_TIMEOUT_S", 1)
    monkeypatch.setattr(launcher, "reset_worker_dir", lambda wid: tmp_path)
    monkeypatch.setattr(prompt_mod, "build_prompt", lambda **kw: "prompt")
    monkeypatch.setattr(launcher, "_build_claude_cmd",
                        lambda *a, **kw: [sys.executable, "-c", "import time; time.sleep(60)"])
    job = {"url": "https://e.com/1", "title": "Engineer", "site": "linkedin",
           "tailored_resume_path": None, "fit_score": 8}

    status, duration_ms = launcher.run_job(job, port=9999, worker_id=0)

    assert status == "failed:timeout"
    assert duration_ms < 20000


def test_claude_cmd_uses_resolved_path(monkeypatch):
    monkeypatch.setattr(launcher.shutil, "which", lambda name: r"C:\npm\claude.cmd")
    assert launcher._build_claude_cmd("sonnet", "mcp.json")[0] == r"C:\npm\claude.cmd"


def test_dry_run_prompt_forbids_accounts(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "load_profile", lambda: {
        "personal": {"full_name": "Jane Doe", "email": "j@e.com", "phone": "1", "city": "X"},
        "work_authorization": {}, "compensation": {"salary_expectation": "1"},
        "experience": {}, "availability": {}, "eeo": {}, "skills_boundary": {},
    })
    monkeypatch.setattr(config, "load_search_config", lambda: {})
    monkeypatch.setattr(config, "APPLY_WORKER_DIR", tmp_path)
    (tmp_path / "x.pdf").write_bytes(b"%PDF-1.4")
    job = {"url": "https://e.com/j", "title": "Engineer", "site": "linkedin",
           "application_url": None, "fit_score": 8, "tailored_resume_path": str(tmp_path / "x.txt")}
    dry = prompt_mod.build_prompt(job=job, tailored_resume="r", dry_run=True)
    real = prompt_mod.build_prompt(job=job, tailored_resume="r", dry_run=False)
    assert "do NOT sign in, create an account" in dry
    assert "do NOT sign in, create an account" not in real


def test_agent_env_drops_anthropic_key_unless_opted_in(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.delenv("APPLYPILOT_CLAUDE_USE_API_KEY", raising=False)
    assert "ANTHROPIC_API_KEY" not in launcher._agent_env()
    monkeypatch.setenv("APPLYPILOT_CLAUDE_USE_API_KEY", "1")
    assert launcher._agent_env()["ANTHROPIC_API_KEY"] == "sk-ant-x"


def test_strict_mcp_config_flag():
    assert "--strict-mcp-config" in launcher._build_claude_cmd("sonnet", "mcp.json")


def test_no_result_diagnosis():
    assert launcher._diagnose_no_result("Invalid API key · Please run /login", 1).startswith("claude_auth")
    assert "Chrome crashed" in launcher._diagnose_no_result("step 1\nstep 2\nChrome crashed", 1)
    assert "no output" in launcher._diagnose_no_result("", 1)


def test_auth_failure_stops_worker_without_burning_attempt(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(launcher, "_load_blocked", lambda: ([], []))
    monkeypatch.setattr(launcher, "launch_chrome", lambda *a, **kw: None)
    monkeypatch.setattr(launcher, "cleanup_worker", lambda *a, **kw: None)
    monkeypatch.setattr(launcher, "run_job", lambda *a, **kw: ("failed:claude_auth:not logged in", 10))
    launcher._stop_event.clear()
    conn = db.init_db()
    _seed(conn, "https://e.com/a")
    _seed(conn, "https://e.com/b")
    try:
        applied, failed = launcher.worker_loop(worker_id=0, limit=0)
    finally:
        stopped = launcher._stop_event.is_set()
        launcher._stop_event.clear()
    assert stopped and applied == 0
    rows = conn.execute("SELECT apply_status, COALESCE(apply_attempts, 0) FROM jobs").fetchall()
    assert all(r[0] is None and r[1] == 0 for r in rows)
