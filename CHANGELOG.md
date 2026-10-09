# Changelog

All notable changes to ApplyPilot will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security
- The auto-apply agent no longer keeps Bash/Edit/Write/WebFetch/WebSearch — only
  the browser, Read, and the Gmail tools it needs — and the prompt now refuses
  instructions embedded in page content (prompt-injection guard).
- `profile.json`, `.env`, generated prompt logs, MCP configs, and the SQLite DB
  are written `0600` (and `~/.applypilot` is `0700`).

### Fixed
- `apply --dry-run` is now genuinely side-effect-free: it never sends an
  application email and never marks jobs applied (previously it did both).
- Screening answers (age, background check, felony, "previously worked here")
  come from `profile.screening` instead of hardcoded values; unset legally
  significant answers are flagged for the human rather than guessed. The agent
  no longer claims experience with tools the candidate hasn't listed.
- `apply --url` now matches fresh (unapplied) jobs and won't file duplicates.
- Location filter no longer discards nearly every job — it reads the documented
  `location.accept_patterns` schema and treats an empty accept list as "keep".
- The real company name is stored and used in scoring, tailoring, cover letters,
  the apply prompt, and the dashboard (was showing the job board, e.g. "linkedin").
- Scoring failures (rate limits, parse errors) leave jobs pending for retry
  instead of writing a permanent `fit_score=0`; scores commit incrementally so an
  interrupt doesn't discard the run; markdown-decorated scores parse correctly.
- Tailored resumes and cover letters get collision-free filenames, and parallel
  apply workers no longer share one upload path (was: one job's resume could be
  sent to another employer).
- The fabrication watchlist is word-boundary matched and respects the candidate's
  real skills (no more false hits on "scalable"/"guardrails"; legitimate C++/C#
  skills allowed).
- Cover-letter PDFs render the actual letter body (were near-empty).
- Sequential `run` no longer silently caps tailoring/cover letters at 20 jobs.
- Jobs stranded `in_progress` by a crashed run are recovered at apply startup.
- One failing site no longer aborts the whole smart-extract stage.
- Jobs left at `fit_score=0` by earlier failed runs are re-queued for scoring.
  Scoring, tailoring and cover letters stop after 5 consecutive LLM failures
  instead of grinding through the queue, and LLM errors no longer use up a
  job's tailor/cover attempts. Tailor and cover results commit per job.
- Empty or reasoning-only LLM responses raise a clear error instead of being
  parsed as results; `<think>` blocks are stripped; HTTP errors include the
  provider's message (e.g. "model no longer available to new users").
- Token limits raised for reasoning models (tailor 2048 -> 16384; score, judge
  and cover letters -> 4096), which caused `EXHAUSTED_RETRIES`.
- Tailored resumes print skills as text instead of Python lists, accept a
  resume with no projects, and don't flag skills that are on the base resume
  as fabricated. The prompt no longer forces one page at the cost of dropping
  preserved companies.
- Discovery reads `boards:` (and `sites:`), passes `defaults.distance` to
  JobSpy, applies `exclude_titles`, and stores missing fields as NULL instead
  of the string "None".
- `status`, `apply`'s pre-check and the apply queue use one definition of
  "ready to apply", so their counts agree.
- Ctrl+C "skip current job" no longer kills ApplyPilot: the agent ran in
  ApplyPilot's process group, which `killpg` took down with it.
- A per-job timeout (`APPLYPILOT_JOB_TIMEOUT`, default 900s) stops hung apply
  agents. The Claude CLI is found via `PATH` lookup, so `claude.cmd` works on
  Windows.
- Dry runs no longer sign in, create accounts or upload files.
- Smart extract and enrichment no longer abort a site when the page never
  reaches network idle.

### Added
- Run LLM stages on Claude, ChatGPT or Google AI subscriptions through their
  CLIs (`LLM_PROVIDER=claude-cli|codex-cli|gemini-cli`), with per-stage
  overrides (`LLM_PROVIDER_SCORE`, `_TAILOR`, `_COVER`, `_EXTRACT`).
- Applicant mailbox over IMAP/SMTP: an MCP server gives the apply agent
  `wait_for_email`, `search_emails`, `read_email` and `send_email`, with
  verification codes and links extracted. Attachments are limited to the
  generated PDFs.
- `LLM_RPM` request pacing, JSON mode for tailoring, and Gemini 429
  handling that waits the server-specified delay and stops on daily quota.

### Changed
- `applypilot doctor` checks for the Playwright browser; README and the init
  wizard tell you to run `playwright install chromium`.
- Re-running `applypilot init` merges into the existing `.env` (preserving keys
  like `CAPSOLVER_API_KEY`) and prompts before overwriting `profile.json` /
  `searches.yaml`. A plain-text resume is now required (or an explicit skip).
- Default Gemini model is `gemini-2.5-flash`. A bare Ollama URL gets `/v1`
  appended.
- ZipRecruiter is no longer searched by default (it answers JobSpy with HTTP
  403); add it to `boards:` to opt in.
- `employers.yaml` and `sites.yaml` in `~/.applypilot/` override the packaged
  copies.
- `applypilot doctor` sends a test request to the configured LLM and warns
  when `profile.json` has no screening answers.
- `applypilot init` supports any OpenAI-compatible API with an API key and
  clears the previous provider's keys when you switch.

## [0.2.0] - 2026-02-17

### Added
- **Parallel workers for discovery/enrichment** - `applypilot run --workers N` enables
  ThreadPoolExecutor-based parallelism for Workday scraping, smart extract, and detail
  enrichment. Default is sequential (1); power users can scale up.
- **Apply utility modes** - `--gen` (generate prompt for manual debugging), `--mark-applied`,
  `--mark-failed`, `--reset-failed` flags on `applypilot apply`
- **Dry-run mode** - `applypilot apply --dry-run` fills forms without clicking Submit
- **5 new tracking columns** - `agent_id`, `last_attempted_at`, `apply_duration_ms`,
  `apply_task_id`, `verification_confidence` for better apply-stage observability
- **Manual ATS detection** - `manual_ats` list in `config/sites.yaml` skips sites with
  unsolvable CAPTCHAs (e.g. TCS iBegin)
- **Qwen3 `/no_think` optimization** - automatically saves tokens when using Qwen models
- **`config.DEFAULTS`** - centralized dict for magic numbers (`min_score`, `max_apply_attempts`,
  `poll_interval`, `apply_timeout`, `viewport`)

### Fixed
- **Config YAML not found after install** - moved `config/` into the package at
  `src/applypilot/config/` so YAML files (employers, sites, searches) ship with `pip install`
- **Search config format mismatch** - wizard wrote `searches:` key but discovery code
  expected `queries:` with tier support. Aligned wizard output and example config
- **JobSpy install isolation** - removed python-jobspy from package dependencies due to
  broken numpy==1.26.3 exact pin in jobspy metadata. Installed separately with `--no-deps`
- **Scoring batch limit** - default limit of 50 silently left jobs unscored across runs.
  Changed to no limit (scores all pending jobs in one pass)
- **Missing logging output** - added `logging.basicConfig(INFO)` so per-job progress for
  scoring, tailoring, and cover letters is visible during pipeline runs

### Changed
- **Blocked sites externalized** - moved from hardcoded sets in launcher.py to
  `config/sites.yaml` under `blocked:` key
- **Site base URLs externalized** - moved from hardcoded dict in detail.py to
  `config/sites.yaml` under `base_urls:` key
- **SSO domains externalized** - moved from hardcoded list in prompt.py to
  `config/sites.yaml` under `blocked_sso:` key
- **Prompt improvements** - screening context uses `target_role` from profile,
  salary section includes `currency_conversion_note` and dynamic hourly rate examples
- **`acquire_job()` fixed** - writes `agent_id` and `last_attempted_at` to proper columns
  instead of misusing `apply_error`
- **`profile.example.json`** - added `currency_conversion_note` and `target_role` fields

## [0.1.0] - 2026-02-17

### Added
- 6-stage pipeline: discover, enrich, score, tailor, cover letter, apply
- Multi-source job discovery: Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs
- Workday employer portal support (46 preconfigured employers)
- Direct career site scraping (28 preconfigured sites)
- 3-tier job description extraction cascade (JSON-LD, CSS selectors, AI fallback)
- AI-powered job scoring (1-10 fit scale with rationale)
- Resume tailoring with factual preservation (no fabrication)
- Cover letter generation per job
- Autonomous browser-based application submission via Playwright
- Interactive setup wizard (`applypilot init`)
- Cross-platform Chrome/Chromium detection (Windows, macOS, Linux)
- Multi-provider LLM support (Gemini, OpenAI, local models via OpenAI-compatible endpoints)
- Pipeline stats and HTML results dashboard
- YAML-based configuration for employers, career sites, and search queries
- Job deduplication across sources
- Configurable score threshold filtering
- Safety limits for maximum applications per run
- Detailed application results logging
