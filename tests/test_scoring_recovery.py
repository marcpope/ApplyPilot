"""Scoring: legacy zero scores are re-queued; repeated failures stop the stage."""
import applypilot.database as db
import applypilot.scoring.scorer as scorer


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(scorer, "RESUME_PATH", tmp_path / "resume.txt")
    (tmp_path / "resume.txt").write_text("my resume")
    return db.init_db()


def _seed(conn, url, score=None):
    conn.execute(
        "INSERT INTO jobs (url, title, full_description, fit_score) VALUES (?,?,?,?)",
        (url, "Engineer", "description " * 30, score),
    )
    conn.commit()


def test_legacy_zero_scores_are_rescored(tmp_path, monkeypatch):
    conn = _setup(tmp_path, monkeypatch)
    _seed(conn, "https://example.com/zero", score=0)
    _seed(conn, "https://example.com/real", score=6)
    monkeypatch.setattr(scorer, "score_job",
                        lambda r, j: {"score": 8, "keywords": "", "reasoning": "ok"})

    out = scorer.run_scoring()

    assert out["scored"] == 1
    rows = dict(conn.execute("SELECT url, fit_score FROM jobs").fetchall())
    assert rows["https://example.com/zero"] == 8
    assert rows["https://example.com/real"] == 6


def test_consecutive_failures_stop_scoring(tmp_path, monkeypatch):
    conn = _setup(tmp_path, monkeypatch)
    for i in range(10):
        _seed(conn, f"https://example.com/{i}")
    calls = []

    def failing(resume, job):
        calls.append(job["url"])
        return {"score": None, "keywords": "", "reasoning": "LLM error: 429"}

    monkeypatch.setattr(scorer, "score_job", failing)
    out = scorer.run_scoring()

    assert len(calls) == scorer.MAX_CONSECUTIVE_ERRORS
    assert out["errors"] == scorer.MAX_CONSECUTIVE_ERRORS
    pending = conn.execute("SELECT COUNT(*) FROM jobs WHERE fit_score IS NULL").fetchone()[0]
    assert pending == 10
