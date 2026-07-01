# Dancing Mad (Ultimate) Log Fetcher & Dashboard

Pulls your raid group's Dancing Mad (Ultimate) log data from FFLogs and turns
it into a self-updating HTML progress dashboard (pull stats, kills/wipes,
phase reached, pitfalls, hour-by-hour performance, etc.).

## Files in this kit

| File | Purpose |
|---|---|
| `fetch_dmu_logs.py` | Pulls raw pull data from FFLogs → `pulls.csv` |
| `build_dashboard.py` | Turns `pulls.csv` into the HTML dashboard |
| `dashboard_template.html` | Visual shell used by `build_dashboard.py` — keep it alongside the script |
| `requirements.txt` | Python dependencies for both scripts |

Keep all four in the same folder. Re-running the two scripts in sequence is
the entire refresh workflow — see step 6 below.

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
# from the folder containing fetch_dmu_logs.py
pip install -r requirements.txt
```

Create a file named `.env` in the same folder (do **not** share this file or
paste its contents anywhere):

```
FFLOGS_CLIENT_ID=your_client_id_here
FFLOGS_CLIENT_SECRET=your_client_secret_here
```

---

## 3. Find your guild ID

It's the number in your guild's FFLogs reports-list URL:

```
https://www.fflogs.com/guild/reports-list/102435
                                          ^^^^^^ this is the guild ID
```

---

## 4. Run it

```bash
python fetch_dmu_logs.py --guild-id 102435
```

By default this:
- Looks up the FFLogs zone ID for **"Dancing Mad"** (matches "Dancing Mad
  (Ultimate)") and uses it to filter reports server-side, so only DMU logs
  are fetched.
- Additionally filters individual fights whose name contains **"Dancing
  Mad"** as a safety net. Note: in FFLogs, all 5 DMU phases (Kefka, God
  Kefka, Exdeath & Chaos, Kefka again, Ultima Kefka) show up as a single
  fight named "Dancing Mad" with a `lastPhase` field (1–5) indicating how
  far the pull got — they are not separate fight names.
- Writes output into `./dmu_data/` (see below).

### Useful flags

| Flag | Default | Purpose |
|---|---|---|
| `--guild-id` | *(required)* | Numeric guild ID from the FFLogs URL |
| `--zone-name` | `Dancing Mad` | Substring match for the raid zone. Set to `""` to disable zone filtering (fetches all reports, slower) |
| `--encounter-name` | `Dancing Mad` | Substring match for individual fight names |
| `--out-dir` | `./dmu_data` | Where output files are written |
| `--max-reports` | `200` | Safety cap on number of reports fetched |

Example — widen the fetch if the zone lookup doesn't match anything:

```bash
python fetch_dmu_logs.py --guild-id 102435 --zone-name "" --encounter-name "Dancing Mad"
```

---

## 5. Output files

All written into `--out-dir` (default `./dmu_data/`):

- **`pulls.csv`** — one row per pull, with:
  - date/time (UTC), kill or wipe, duration
  - `fight_percentage` (boss HP % remaining when the pull ended)
  - `last_phase` (1–5, how far into the fight's phases the pull got)
  - report code/title it came from
- **`session_summary.json`** — per-raid-day aggregates:
  - total pulls, kills, best % of the day, furthest phase reached
  - hour-by-hour breakdown (pull count, kills, best/avg % per hour of the
    session)
- **`reports_raw.json`** — full raw data backup, useful if something needs
  re-processing later or if you want to double check fight names/zones.

---

## 6. Build (or rebuild) the dashboard

Once you have `pulls.csv`, generate the HTML dashboard yourself — no need to
come back and ask for it to be rebuilt by hand:

```bash
python build_dashboard.py --pulls-csv ./dmu_data/pulls.csv
```

This writes `dmu_raid_dashboard.html` in the current folder. Open it in any
browser.

`build_dashboard.py` reads only `pulls.csv` — it derives session boundaries,
raid-night hours, phase stats, and the hour-by-hour breakdown all from that
one file, so `session_summary.json` isn't needed for this step.

### Refreshing after new raid nights

Whenever you've raided again, just re-run both scripts:

```bash
python fetch_dmu_logs.py --guild-id 102435
python build_dashboard.py --pulls-csv ./dmu_data/pulls.csv
```

That's the entire refresh loop — no manual data wrangling, and the charts,
KPI numbers, and pitfall analysis all update automatically as new pulls and
phases show up.

### `build_dashboard.py` flags

| Flag | Default | Purpose |
|---|---|---|
| `--pulls-csv` | *(required)* | Path to `pulls.csv` |
| `--template` | `dashboard_template.html` | HTML template to inject data into |
| `--out` | `dmu_raid_dashboard.html` | Output file path |
| `--utc-offset` | `2` | Hours added to UTC for local raid time (2 = CEST/summer, 1 = CET/winter) |
| `--raid-start-hour` | `20` | Local hour your raid block starts (24h, e.g. 20 = 8pm) |
| `--raid-length-hours` | `3` | Length of your raid block in hours |

Example for a winter (CET) raid night starting at 19:00 for 4 hours:

```bash
python build_dashboard.py --pulls-csv ./dmu_data/pulls.csv \
  --utc-offset 1 --raid-start-hour 19 --raid-length-hours 4
```

The dashboard automatically adapts to further progress too — the phase
breakdown, pitfalls section, and progress chart pick up phases 4 and 5 as
soon as they start appearing in `pulls.csv`, no template changes needed.

**Keep `dashboard_template.html` next to `build_dashboard.py`** — it's the
visual shell the script injects data into. Editing the template lets you
restyle the dashboard without touching the data logic at all.

---

## Troubleshooting

- **"No zone found matching 'Dancing Mad'"** — the console output will print
  a sample of available zone names; check for a naming difference (e.g. a
  different region's translation) or rerun with `--zone-name ""` and rely on
  `--encounter-name` filtering instead.
- **`401` / auth errors** — double check `.env` values and that there are no
  extra spaces or quote characters around the ID/secret.
- **Rate limiting** — the script already pauses briefly between requests; if
  you have a very large number of reports, consider lowering `--max-reports`
  for a first test run.
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
- **`build_dashboard.py` can't find the template** — make sure
  `dashboard_template.html` is in the same folder, or pass its path
  explicitly with `--template`.
