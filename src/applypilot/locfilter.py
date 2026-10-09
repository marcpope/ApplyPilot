"""Canonical job-location filtering shared by all discovery sources.

Historically each discovery module carried its own copy of this logic and read
config keys (``location_accept`` / ``location_reject_non_remote``) that nothing
ever wrote -- the shipped config uses ``location.accept_patterns`` /
``location.reject_patterns``. With empty lists the old code rejected every
non-remote job, silently discarding almost all results. This module reads both
schemas and treats an empty accept list as "accept everything not rejected".
"""
from __future__ import annotations

import re

_REMOTE_MARKERS = ("remote", "anywhere", "work from home", "wfh", "distributed")


def load_location_filter(search_cfg: dict | None) -> tuple[list[str], list[str]]:
    """Read accept/reject location patterns from a search config dict.

    Supports the current ``location: {accept_patterns, reject_patterns}`` schema
    and the legacy flat ``location_accept`` / ``location_reject_non_remote`` keys.
    """
    cfg = search_cfg or {}
    loc = cfg.get("location", {}) or {}
    accept = loc.get("accept_patterns") or cfg.get("location_accept", []) or []
    reject = loc.get("reject_patterns") or cfg.get("location_reject_non_remote", []) or []
    return accept, reject


def location_ok(location: str | None, accept: list[str], reject: list[str]) -> bool:
    """Decide whether a job location passes the filter.

    Remote locations are always accepted. A reject-pattern match always fails.
    Empty accept list = accept everything not explicitly rejected. A non-empty
    accept list is exclusive: a non-remote location must match it to pass.
    """
    if not location:
        return True  # unknown location -- keep it, let the scorer decide

    loc = location.lower()

    if any(marker in loc for marker in _REMOTE_MARKERS):
        return True

    for r in reject:
        if r.lower() in loc:
            return False

    if not accept:
        return True  # nothing explicitly rejected and no accept list to enforce

    for a in accept:
        if a.lower() in loc:
            return True

    return False


# -- Title exclusion ----------------------------------------------------------

_title_excludes: list[str] | None = None


def title_excludes() -> list[str]:
    """Lowercased ``exclude_titles`` patterns from the user's search config.

    Loaded once per process: every discovery source checks titles at insert
    time, and re-reading YAML per job would be wasteful.
    """
    global _title_excludes
    if _title_excludes is None:
        from applypilot import config
        cfg = config.load_search_config() or {}
        _title_excludes = [str(t).lower() for t in cfg.get("exclude_titles") or [] if str(t).strip()]
    return _title_excludes


def title_ok(title: str | None, excludes: list[str] | None = None) -> bool:
    """False when the job title contains an ``exclude_titles`` pattern.

    Patterns match whole words, case-insensitively: "intern" excludes
    "Software Intern" but not "Internal Tools Engineer", and "VP" doesn't
    fire inside "MVP".
    """
    if not title:
        return True
    excludes = title_excludes() if excludes is None else excludes
    low = title.lower()
    return not any(
        re.search(rf"(?<!\w){re.escape(pattern.strip())}(?!\w)", low)
        for pattern in excludes if pattern.strip()
    )
