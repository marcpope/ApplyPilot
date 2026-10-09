"""Discovery honors the documented config: boards, distance, country, exclude_titles."""
import pandas as pd
import pytest

import applypilot.config as config
import applypilot.database as db
import applypilot.discovery.jobspy as js
import applypilot.locfilter as locfilter


@pytest.mark.parametrize("cfg, expected", [
    ({}, ["indeed", "linkedin"]),
    ({"boards": ["indeed", "glassdoor"]}, ["indeed", "glassdoor"]),
    ({"sites": ["linkedin"]}, ["linkedin"]),
    ({"boards": ["ZipRecruiter", "nonsense"]}, ["zip_recruiter"]),
    ({"boards": ["nonsense"]}, ["indeed", "linkedin"]),
])
def test_resolve_sites(cfg, expected):
    assert js._resolve_sites(cfg) == expected


def test_default_sites_exclude_ziprecruiter():
    assert "zip_recruiter" not in js.DEFAULT_SITES


@pytest.mark.parametrize("raw", [None, float("nan"), "nan", "None", "", "  "])
def test_clean_null_like_values(raw):
    assert js._clean(raw) is None


def test_title_ok():
    excludes = ["intern", "vp "]
    assert locfilter.title_ok("Backend Engineer", excludes)
    assert not locfilter.title_ok("Software Engineering Intern", excludes)
    assert not locfilter.title_ok("VP Engineering", excludes)
    assert locfilter.title_ok("MVP Developer", excludes)
    assert locfilter.title_ok("Internal Tools Engineer", excludes)
    assert locfilter.title_ok(None, excludes)


def test_config_file_prefers_user_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    assert config.config_file("employers.yaml") == config.CONFIG_DIR / "employers.yaml"
    (tmp_path / "employers.yaml").write_text("employers: {}\n")
    assert config.config_file("employers.yaml") == tmp_path / "employers.yaml"


def test_store_does_not_write_literal_none(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    conn = db.init_db()
    df = pd.DataFrame([{"job_url": "https://example.com/1", "title": "Engineer",
                        "company": None, "location": float("nan"),
                        "job_url_direct": None, "is_remote": float("nan"), "site": "indeed"}])
    js.store_jobspy_results(conn, df, "q")
    row = conn.execute("SELECT company, location, application_url FROM jobs").fetchone()
    assert row["company"] is None
    assert row["location"] is None  # NaN is_remote must not turn this into "Remote"
    assert row["application_url"] is None


def _run_search(tmp_path, monkeypatch, frame, defaults=None, sites=("indeed",),
                remote=False, excludes=()):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    calls = []

    def fake_scrape(kwargs, max_retries=2):
        calls.append(kwargs)
        return frame.copy()

    monkeypatch.setattr(js, "_scrape_with_retry", fake_scrape)
    monkeypatch.setattr(locfilter, "_title_excludes", list(excludes))
    search = {"query": "python developer", "location": "Austin, TX", "remote": remote, "tier": 1}
    result = js._run_one_search(search, list(sites), 10, 72, None, defaults or {}, 0,
                                ["Austin"], [], {})
    return result, calls


def test_distance_and_google_term_passed(tmp_path, monkeypatch):
    frame = pd.DataFrame([{"job_url": "https://e.com/1", "title": "Dev", "location": "Austin, TX"}])
    _, calls = _run_search(tmp_path, monkeypatch, frame, defaults={"distance": 25},
                           sites=("indeed", "google"))
    assert calls[0]["distance"] == 25
    assert calls[0]["google_search_term"] == "python developer jobs near Austin, TX"


def test_remote_search_skips_distance(tmp_path, monkeypatch):
    frame = pd.DataFrame([{"job_url": "https://e.com/1", "title": "Dev", "location": "Remote"}])
    _, calls = _run_search(tmp_path, monkeypatch, frame, defaults={"distance": 25}, remote=True)
    assert "distance" not in calls[0]
    assert calls[0]["is_remote"] is True


def test_excluded_titles_not_stored(tmp_path, monkeypatch):
    frame = pd.DataFrame([
        {"job_url": "https://e.com/1", "title": "Python Developer", "location": "Austin, TX"},
        {"job_url": "https://e.com/2", "title": "Python Developer Intern", "location": "Austin, TX"},
    ])
    result, _ = _run_search(tmp_path, monkeypatch, frame, excludes=["intern"])
    assert result["new"] == 1


def test_everything_filtered_by_location_does_not_crash(tmp_path, monkeypatch):
    frame = pd.DataFrame([{"job_url": "https://e.com/1", "title": "Dev", "location": "Berlin"}])
    result, _ = _run_search(tmp_path, monkeypatch, frame, excludes=["intern"])
    assert result["new"] == 0
    assert result["filtered"] == 1
