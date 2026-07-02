# CLAUDE.md

Instructions for Claude (or any AI assistant) working in this repository.

## Purpose

This repo tracks one FFXIV raid static's progression through **Dancing Mad
(Ultimate)** (DMU), Final Fantasy XIV's 7th ultimate raid (patch 7.51,
released June 2, 2026). It pulls pull-by-pull log data from FFLogs and turns
it into a static HTML dashboard: fight progress per raid night, recurring
wipe points ("pitfalls"), pull volume/duration stats, and hour-by-hour
performance across the raid's fixed schedule.

The goal is a **repeatable, zero-manual-effort refresh loop**: raid → run one
command → get updated dashboards. Nobody should have to hand-edit data,
recompute stats, or touch chart code to see updated numbers after a raid
night.

## How the pieces fit together

```
FFLogs API  →  src/main.py  →  pulls.csv  →  src/build_dashboard.py     →  output/dmu_raid_dashboard.html
                             →  reports_raw.json (raw backup + fetch cache)
                                pulls.csv  →  src/build_night_report.py →  output/dmu_night_report.html

src/refresh.py = src/main.py + both build scripts, in one command
src/dmu_common.py = shared data logic imported by both build scripts
```

Python build scripts live in `src/`, their HTML shells in `templates/`,
and locally built HTML output goes to `output/` (gitignored except for the
two artifacts described below). All commands are run from the repo
root — every script's default paths (`--pulls-csv`, `--template`, `--out`)
are relative to it, not to `src/`.

There are two independent downstream builds off the same `pulls.csv`:
`build_dashboard.py` for the full multi-night history, and
`build_night_report.py` for a deep dive on one raid night (latest by
default, or a specific `--date`). Neither requires the other to run first —
both start from a fresh `pulls.csv` fetch.

| File | Role |
|---|---|
| `src/main.py` | Authenticates to FFLogs v2 GraphQL API (client-credentials flow), fetches all reports for the guild filtered to the DMU zone, flattens every pull into `pulls.csv`. Incremental: reports whose `endTime` is unchanged in `reports_raw.json` are reused from cache instead of refetched (`--full-refetch` bypasses this). |
| `src/refresh.py` | One-command refresh: runs `main.py`, then both build scripts. `--skip-fetch` rebuilds from the existing `pulls.csv`; other args pass through to the builders. |
| `src/dmu_common.py` | Shared logic used by both build scripts: pulls.csv loading/typing, "real pull" filtering, local-time hour bucketing, phase-to-phase conversion, the pitfalls histograms, and template injection. Anything both reports need belongs here, not copy-pasted. |
| `pulls.csv` | Source of truth. One row per pull: date/time, kill/wipe, `fight_percentage`, `boss_percentage`, `last_phase`, duration. Everything downstream is derived from this file alone. |
| `src/build_dashboard.py` | Reads `pulls.csv`, computes all cross-night aggregates (session boundaries, prog curve, progression records/wall detection, phase histograms, hour-by-raid-night grid), injects them as JSON into `templates/dashboard_template.html`, writes the final HTML. |
| `templates/dashboard_template.html` | The visual shell — HTML/CSS/Chart.js. Contains `__DATA_JSON__`, `__DATE_RANGE__`, `__RESET_NOTE__` placeholders that `build_dashboard.py` fills in. All data access in the JS reads from a single injected `DATA` object. |
| `output/dmu_raid_dashboard.html` | Final build artifact of `build_dashboard.py`. Fully self-contained (Chart.js loaded from cdnjs, fonts from Google Fonts) — can be opened directly in a browser or hosted as a static file. |
| `src/build_night_report.py` | Reads `pulls.csv`, filters to one raid night (latest, or `--date`), computes a pull-by-pull timeline plus trend/downtime (with break detection)/phase-composition for that night and an all-time-best comparison against the rest of `pulls.csv`, injects them into `templates/night_report_template.html`. |
| `templates/night_report_template.html` | The visual shell for the single-night report — same theme/fonts/Chart.js as `dashboard_template.html` (CSS intentionally duplicated, not shared) but built around a pull-level timeline instead of cross-night aggregates. Same `__DATA_JSON__` / `__DATE_RANGE__` placeholder mechanism. |
| `output/dmu_night_report.html` | Final build artifact of `build_night_report.py`. Self-contained the same way as `dmu_raid_dashboard.html`. |

**Rule of thumb:** styling and layout changes go in the relevant template
file (`templates/dashboard_template.html` or
`templates/night_report_template.html`). Data/aggregation changes go in the
matching build script in `src/`. Don't hand-edit
`output/dmu_raid_dashboard.html` or `output/dmu_night_report.html` — they're
generated artifacts and will be overwritten on the next build.

## Domain knowledge specific to this project

These are non-obvious things learned while building this, worth knowing
before changing the aggregation logic:

- **DMU's 5 phases are not separate FFLogs "fights."** All phases (Kefka →
  God Kefka → Exdeath & Chaos → Kefka again → Ultima Kefka) show up as a
  single fight named `"Dancing Mad"` in the API. Progress within the pull is
  tracked via the `lastPhase` field (1–5), not by fight name. Don't filter
  on fight name expecting per-phase fights.

- **`fight_percentage` vs `boss_percentage` are very different things.**
  - `fight_percentage` (used throughout this dashboard) is FFLogs' own
    "overall encounter completion" metric — it decreases monotonically from
    100% (pull start) to 0% (kill) across the *entire* multi-phase encounter.
    This is the correct field for "how far did this pull get."
  - `boss_percentage` is the raw HP% of whatever enemy is currently active.
    It resets close to 100% every time a new phase's boss/add spawns, so it
    is **not** comparable across phases and should not be used for overall
    progress tracking. It's kept in `pulls.csv` for reference but not
    currently charted.

- **~20 pulls have `null` `fight_percentage` / `last_phase == 0`.** These are
  very short (<75s) practice/reset pulls, likely mid-fight practice starts.
  `build_dashboard.py` counts them in `overview.reset_pulls` and in the
  phase-composition chart, but excludes them from the phase histograms
  ("pitfalls" section) since they don't represent a real death point.

- **Raid schedule is fixed local-time, not fixed UTC.** The static raids
  20:00–23:00 CEST. Because CEST/CET shifts relative to UTC across the year,
  the hour-by-hour analysis converts UTC timestamps to local time via
  `--utc-offset` (2 for CEST, 1 for CET) rather than bucketing on raw UTC
  hour. If the static's schedule or timezone changes, pass
  `--utc-offset` / `--raid-start-hour` / `--raid-length-hours` accordingly
  — don't hardcode new values into the script.

- **The dashboard is built to extend to phases 4 and 5 automatically.** The
  progress chart, pitfalls section, and phase legend in
  `dashboard_template.html` derive their phase list from whatever appears in
  the injected data (`phase_composition` / `phase_stats` keys), not from a
  hardcoded "phases 1–3." When the group starts reaching phase 4/5, no
  template changes should be needed — if they are, that's a bug to fix by
  making the relevant section data-driven, not by adding a special case.

- **Zero kills so far is expected, not a bug.** The raid released June 2,
  2026 and this static started raiding June 3. `overview.total_kills` will
  be 0 until the first clear. When a kill does happen, `pulls.csv` will have
  `kill == True` for that row — no schema changes needed, but double check
  the dashboard's presentation still makes sense at that point (e.g. whether
  a "cleared" state deserves its own hero treatment).

- **"Wall reps" and "active time" are raid-leader metrics, keep them honest.**
  Wall reps = pulls whose `last_phase` reached the deepest phase seen
  (all-time deepest on the dashboard, that night's deepest in the night
  report) — it measures how much practice the group actually got on the
  current wall. `active_pct` in the night report is total pull duration over
  session span; its complement is downtime, which the night report splits
  into "breaks" (gaps ≥ `--break-threshold-min`, default 10) and normal
  wipe-recovery gaps so the avg re-pull gap isn't polluted by dinner breaks.

- **No dual-axis charts.** Charts that used to overlay two scales (pulls +
  duration, pulls + HP%) are now side-by-side single-axis pairs
  (`.chart-pair`). Keep it that way when adding charts.

## Running things

```bash
uv sync

# one-time: create an FFLogs API client at https://www.fflogs.com/api/clients/
# and put FFLOGS_CLIENT_ID / FFLOGS_CLIENT_SECRET in a local .env file

# the whole refresh loop (fetch + both reports), from the repo root:
uv run src/refresh.py

# or piecewise (guild id defaults to this static's, 102435):
uv run src/main.py
uv run src/build_dashboard.py
uv run src/build_night_report.py
# a specific past night:
uv run src/build_night_report.py --date 2026-06-24
```

The fetch is incremental — only reports that are new or changed since the
last run are pulled from the API (`reports_raw.json` doubles as the cache;
`--full-refetch` forces a clean fetch).

See `README.md` for full setup and flag documentation.

## Conventions

- Python: stdlib + `requests`, `python-dotenv`, `pandas`. No other
  dependencies without a good reason — this is meant to stay a small,
  easy-to-audit pipeline.
- Logic needed by both build scripts goes in `src/dmu_common.py`; don't
  duplicate aggregation code between them.
- No secrets in the repo. `.env` (repo root) holding `FFLOGS_CLIENT_ID` /
  `FFLOGS_CLIENT_SECRET` must stay out of version control (add to
  `.gitignore` if not already there).
- `templates/dashboard_template.html` has no build step — it's plain
  HTML/CSS/JS with Chart.js from a CDN. Keep it that way; don't introduce a
  bundler or framework for what's currently a single static file.
- When adding new aggregate stats, add them to the `data` dict in
  `src/build_dashboard.py` and consume them in
  `templates/dashboard_template.html` via the global `DATA` object — don't
  invent a second data-passing mechanism.
- Prefer deriving new stats from `pulls.csv` alone over requiring additional
  input files, to keep the "two commands to refresh" workflow intact.
