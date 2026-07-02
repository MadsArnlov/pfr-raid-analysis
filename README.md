# Dancing Mad (Ultimate) Log Fetcher & Dashboard

Pulls your raid group's Dancing Mad (Ultimate) log data from FFLogs and turns
it into a self-updating HTML progress dashboard (prog curve, pull stats,
kills/wipes, phase reached, pitfalls, hour-by-hour performance) plus a
single-night deep-dive report.

## Files in this kit

| File | Purpose |
|---|---|
| `main.py` | Pulls raw pull data from FFLogs → `pulls.csv` (incremental — only new/changed reports hit the API) |
| `refresh.py` | One command: fetch + rebuild both HTML reports |
| `dmu_common.py` | Shared data logic imported by both build scripts |
| `build_dashboard.py` | Turns `pulls.csv` into the full-history HTML dashboard |
| `dashboard_template.html` | Visual shell used by `build_dashboard.py` — keep it alongside the script |
| `build_night_report.py` | Turns `pulls.csv` into a deep-dive HTML report for one raid night |
| `night_report_template.html` | Visual shell used by `build_night_report.py` — keep it alongside the script |
| `pyproject.toml` | Python dependencies (managed with `uv`) |

It talks to FFLogs' public API (v2, GraphQL) using the OAuth **client
credentials** flow — no login or password needed, just an API client ID and
secret that you generate yourself.

---

## 1. Get your FFLogs API credentials

1. Log into [fflogs.com](https://www.fflogs.com) with your account.
2. Go to **https://www.fflogs.com/api/clients/**.
3. Click **Create Client** (or similar "New Client" button).
   - **Name**: anything, e.g. `dmu-dashboard`
   - **Redirect URL**: not used by this script, put `http://localhost`
   - Leave "Public Client" unchecked.
4. Save. The client page shows:
   - **Client ID** — safe to view again anytime
   - **Client Secret** — usually shown only once, copy it immediately

If you ever lose the secret, go back to the client page and reset it.

---

## 2. Set up the project

```bash
uv sync
```

Create a file named `.env` in the same folder (do **not** share this file or
paste its contents anywhere):

```
FFLOGS_CLIENT_ID=your_client_id_here
FFLOGS_CLIENT_SECRET=your_client_secret_here
```

---

## 3. Run it

The entire refresh loop after a raid night is one command:

```bash
uv run refresh.py
```

This fetches the latest logs and rebuilds both `dmu_raid_dashboard.html`
(full history) and `dmu_night_report.html` (latest night). Open either in
any browser. Use `--skip-fetch` to rebuild from the existing `pulls.csv`
without hitting the API; other flags pass through to the build scripts.

Each step is also runnable on its own:

```bash
uv run main.py                                # fetch → dmu_data/pulls.csv
uv run build_dashboard.py                     # → dmu_raid_dashboard.html
uv run build_night_report.py                  # → dmu_night_report.html (latest night)
uv run build_night_report.py --date 2026-06-24  # a specific past night
```

The guild ID defaults to this static's (102435). For another guild, pass
`--guild-id` (it's the number in the FFLogs reports-list URL:
`https://www.fflogs.com/guild/reports-list/102435`).

### `main.py` fetch behavior and flags

By default the fetch:

- Looks up the FFLogs zone ID for **"Dancing Mad"** (matches "Dancing Mad
  (Ultimate)") and uses it to filter reports server-side, so only DMU logs
  are fetched.
- Additionally filters individual fights whose name contains **"Dancing
  Mad"** as a safety net. Note: in FFLogs, all 5 DMU phases (Kefka, God
  Kefka, Exdeath & Chaos, Kefka again, Ultima Kefka) show up as a single
  fight named "Dancing Mad" with a `lastPhase` field (1–5) indicating how
  far the pull got — they are not separate fight names.
- Is **incremental**: `reports_raw.json` doubles as a cache, and any report
  whose `endTime` hasn't changed since the last run is reused instead of
  refetched. A routine refresh only hits the API for the newest raid night.

| Flag | Default | Purpose |
|---|---|---|
| `--guild-id` | `102435` | Numeric guild ID from the FFLogs URL |
| `--zone-name` | `Dancing Mad` | Substring match for the raid zone. Set to `""` to disable zone filtering (fetches all reports, slower) |
| `--encounter-name` | `Dancing Mad` | Substring match for individual fight names |
| `--out-dir` | `./dmu_data` | Where output files are written |
| `--max-reports` | `200` | Safety cap on number of reports fetched |
| `--full-refetch` | off | Ignore the cache and refetch every report |

Output files (in `--out-dir`):

- **`pulls.csv`** — one row per pull: date/time (UTC), kill or wipe,
  duration, `fight_percentage` (overall encounter % remaining when the pull
  ended), `last_phase` (1–5), report code/title. This is the single source
  of truth for both build scripts.
- **`reports_raw.json`** — full raw report/fight data; backup and fetch
  cache in one.

---

## 4. The two reports

### Full-history dashboard (`build_dashboard.py`)

Cross-night view: prog curve (every pull + running all-time best), phase
composition per night, pull volume/duration, pitfalls (where pulls die, by
phase), phase-to-phase conversion rates and their night-over-night trend,
progression velocity with wall detection, hour-by-hour performance, and a
per-night log with wall reps and record-night markers.

| Flag | Default | Purpose |
|---|---|---|
| `--pulls-csv` | `./dmu_data/pulls.csv` | Path to `pulls.csv` |
| `--template` | `dashboard_template.html` | HTML template to inject data into |
| `--out` | `dmu_raid_dashboard.html` | Output file path |
| `--utc-offset` | `2` | Hours added to UTC for local raid time (2 = CEST/summer, 1 = CET/winter) |
| `--raid-start-hour` | `20` | Local hour your raid block starts (24h, e.g. 20 = 8pm) |
| `--raid-length-hours` | `3` | Length of your raid block in hours |
| `--wall-min-pulls-since-record` | `15` | Pulls without a new best before a "wall" can be flagged |
| `--wall-max-stdev` | `6.0` | Max spread of recent results for a wall to be flagged |

Example for a winter (CET) raid night starting at 19:00 for 4 hours:

```bash
uv run build_dashboard.py --utc-offset 1 --raid-start-hour 19 --raid-length-hours 4
```

The dashboard automatically adapts to further progress — the phase
breakdown, pitfalls section, and progress chart pick up phases 4 and 5 as
soon as they start appearing in `pulls.csv`, no template changes needed.

### Single-night report (`build_night_report.py`)

One night in depth: pull-by-pull timeline with the night's running best,
trend within the night, hour-by-hour volume and depth, downtime between
pulls (breaks vs wipe-recovery gaps, avg re-pull speed, share of the session
actually spent pulling), phase composition, pitfalls, conversion rates, and
how the night stacks up against the all-time best.

| Flag | Default | Purpose |
|---|---|---|
| `--pulls-csv` | `./dmu_data/pulls.csv` | Path to `pulls.csv` |
| `--date` | most recent night | Raid night to analyze (`YYYY-MM-DD`) |
| `--template` | `night_report_template.html` | HTML template to inject data into |
| `--out` | `dmu_night_report.html` | Output file path |
| `--utc-offset` / `--raid-start-hour` / `--raid-length-hours` | `2` / `20` / `3` | Same as the dashboard |
| `--break-threshold-min` | `10` | Gaps at least this long count as scheduled breaks, not wipe recovery |

Both reports read the same `pulls.csv` — there's nothing to keep in sync
between them, and running one doesn't require the other.

**Keep the template files next to their build scripts** — they're the
visual shells the scripts inject data into. Editing a template restyles a
report without touching the data logic at all.

---

## Troubleshooting

- **"No zone found matching 'Dancing Mad'"** — the console output will print
  a sample of available zone names; check for a naming difference (e.g. a
  different region's translation) or rerun with `--zone-name ""` and rely on
  `--encounter-name` filtering instead.
- **`401` / auth errors** — double check `.env` values and that there are no
  extra spaces or quote characters around the ID/secret.
- **Rate limiting** — the script already pauses briefly between requests and
  reuses cached reports; if you have a very large number of reports,
  consider lowering `--max-reports` for a first test run.
- **Stale or weird fetch results** — rerun with `--full-refetch` to bypass
  the `reports_raw.json` cache.
- **Empty `pulls.csv`** — check `reports_raw.json` to see what fight names
  and zone names actually appear in your reports, then adjust
  `--encounter-name` / `--zone-name` accordingly. In FFLogs, DMU's 5 phases
  are tracked via `lastPhase` on a single fight named "Dancing Mad" rather
  than as separate fight names — if your reports use a different naming
  convention, match on whatever `fights[].name` shows in the raw JSON.
- **Dashboard hours look shifted by an hour** — check `--utc-offset`. It's 2
  for CEST (roughly late March–late October) and 1 for CET (winter). If your
  raid time isn't 20:00–23:00 local, also set `--raid-start-hour` /
  `--raid-length-hours` to match.
- **A build script can't find its template** — make sure the template file
  is in the same folder, or pass its path explicitly with `--template`.
