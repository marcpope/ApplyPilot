"""Tailoring: resume rendering, validator leniency, per-job persistence."""
import applypilot.database as db
import applypilot.scoring.tailor as tailor
from applypilot.scoring.tailor import assemble_resume_text
from applypilot.scoring.validator import validate_json_fields

PROFILE = {"personal": {"full_name": "Jane Doe", "email": "j@example.com"}}


def _data(**overrides):
    data = {
        "title": "Engineer",
        "summary": "Builds backend services.",
        "skills": {"Languages": ["Java", "Python"], "Tools": "Docker, Git"},
        "experience": [{"header": "Engineer at Acme", "subtitle": "2020-2024",
                        "bullets": ["Built an API", "Cut latency 40%"]}],
        "projects": [],
        "education": "State U | BS",
    }
    data.update(overrides)
    return data


def test_skill_lists_render_as_text():
    text = assemble_resume_text(_data(), PROFILE)
    assert "Languages: Java, Python" in text
    assert "['" not in text


def test_empty_projects_omits_header():
    assert "PROJECTS" not in assemble_resume_text(_data(), PROFILE)


def test_education_list_renders_one_per_line():
    text = assemble_resume_text(_data(education=["State U | BS", "City College | AS"]), PROFILE)
    assert "State U | BS\nCity College | AS" in text


def test_empty_projects_passes_validation():
    result = validate_json_fields(_data(), {"resume_facts": {}}, mode="normal")
    assert "Missing required field: projects" not in result["errors"]


def test_watchlist_skill_on_base_resume_is_not_fabricated():
    data = _data(skills={"Languages": "C#, Python", "Certs": "AWS Certified Cloud Practitioner"})
    base = "Skills: C#, Python. AWS Certified Cloud Practitioner."
    flagged = validate_json_fields(data, {"resume_facts": {}}, mode="normal")
    allowed = validate_json_fields(data, {"resume_facts": {}}, mode="normal", original_text=base)
    assert any("c#" in e.lower() for e in flagged["errors"])
    assert not any("fabricated" in e.lower() for e in allowed["errors"])


def _seed(conn, url):
    conn.execute(
        "INSERT INTO jobs (url, title, site, fit_score, full_description) VALUES (?,?,?,?,?)",
        (url, "Engineer", "linkedin", 9, "description " * 50),
    )
    conn.commit()


def _run(tmp_path, monkeypatch, fake_tailor):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(tailor, "TAILORED_DIR", tmp_path / "out")
    monkeypatch.setattr(tailor, "RESUME_PATH", tmp_path / "resume.txt")
    monkeypatch.setattr(tailor, "load_profile", lambda: PROFILE)
    monkeypatch.setattr(tailor, "tailor_resume", fake_tailor)
    (tmp_path / "resume.txt").write_text("resume")
    conn = db.init_db()
    return conn


def test_llm_errors_do_not_burn_attempts_and_stop_the_stage(tmp_path, monkeypatch):
    calls = []

    def boom(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("quota exhausted")

    conn = _run(tmp_path, monkeypatch, boom)
    for i in range(8):
        _seed(conn, f"https://example.com/{i}")
    out = tailor.run_tailoring(limit=0)

    assert out["errors"] == tailor.MAX_CONSECUTIVE_ERRORS
    assert len(calls) == tailor.MAX_CONSECUTIVE_ERRORS
    attempts = conn.execute("SELECT MAX(COALESCE(tailor_attempts, 0)) FROM jobs").fetchone()[0]
    assert attempts == 0


def test_exhausted_retries_counted_as_failure(tmp_path, monkeypatch):
    def exhausted(*args, **kwargs):
        return "", {"status": "exhausted_retries", "attempts": 4}

    conn = _run(tmp_path, monkeypatch, exhausted)
    _seed(conn, "https://example.com/a")
    out = tailor.run_tailoring(limit=0)

    assert out["failed"] == 1
    row = conn.execute("SELECT tailor_attempts FROM jobs").fetchone()
    assert row[0] == 1
