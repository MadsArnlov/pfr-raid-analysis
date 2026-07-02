#!/usr/bin/env python3
"""
Build (or rebuild) the Dancing Mad (Ultimate) raid dashboard from pulls.csv.

This reads only pulls.csv (produced by fetch_dmu_logs.py) — everything else
(session boundaries, hours, phase stats, the hour-by-raid-night grid) is
derived from it, so re-running the fetch script and then this one is all you
need to do to refresh the dashboard with new raid nights.

USAGE
-----
    python build_dashboard.py --pulls-csv ./dmu_data/pulls.csv

Optional flags:
    --template ./dashboard_template.html   # HTML template to inject data into
    --out ./dmu_raid_dashboard.html        # output file
    --utc-offset 2                         # hours to add to UTC for local
                                            # raid time (2 = CEST/summer,
                                            # 1 = CET/winter)
    --raid-start-hour 20                   # local hour your raid block starts
    --raid-length-hours 3                  # length of the raid block
    --wall-min-pulls-since-record 15       # pulls since the last all-time-best
                                            # pull before a "wall" can be flagged
    --wall-max-stdev 6.0                   # max stdev of fight_percentage in the
                                            # recent window for a wall to be flagged
                                            # (tight cluster = likely a real wall)

Typical refresh workflow:

    python fetch_dmu_logs.py --guild-id 102435
    python build_dashboard.py --pulls-csv ./dmu_data/pulls.csv
"""

import argparse
import json
from pathlib import Path

import pandas as pd


def bucket_hour(local_hour: int, raid_start: int, raid_length: int) -> str:
    """Classify an hour into 'pre', one of the raid-block hours (as a
    string of the block's starting hour), or 'late'."""
    if local_hour < raid_start:
        return "pre"
    offset = local_hour - raid_start
    if offset < raid_length:
        return str(raid_start + offset)
    return "late"


def build_bucket_order_and_labels(raid_start: int, raid_length: int):
    order = ["pre"] + [str(raid_start + i) for i in range(raid_length)] + ["late"]
    labels = {"pre": "Pre-raid", "late": f"{(raid_start + raid_length) % 24:02d}:00+"}
    for i in range(raid_length):
        h = raid_start + i
        labels[str(h)] = f"{h % 24:02d}:00\u2013{(h + 1) % 24:02d}:00"
    return order, labels


def compute_progression(real: pd.DataFrame, wall_min_pulls: int, wall_max_stdev: float) -> dict:
    """Track all-time-best "record" pulls chronologically. Feeds three related
    reads on the same underlying log: progression velocity (pulls between
    records \u2014 is the gap growing normally or has it spiked?), time since the
    last new best, and a "wall" heuristic (long dry spell + tight clustering
    of recent results = likely stuck on one specific mechanic, not bad luck)."""
    real_sorted = real.sort_values("start_time_utc").reset_index(drop=True)

    record_positions = []
    running_best = None
    for i, pct in enumerate(real_sorted["fight_percentage"]):
        if running_best is None or pct < running_best:
            record_positions.append(i)
            running_best = pct

    records = []
    for k, pos in enumerate(record_positions):
        row = real_sorted.iloc[pos]
        if k == 0:
            pulls_since_prior = 0
            nights_since_prior = 0
        else:
            prior_pos = record_positions[k - 1]
            pulls_since_prior = pos - prior_pos
            nights_since_prior = int(real_sorted.iloc[prior_pos + 1:pos + 1]["date"].nunique())
        records.append({
            "date": row["date"],
            "pull_global_index": int(pos) + 1,
            "fight_percentage": float(row["fight_percentage"]),
            "last_phase": int(row["last_phase"]),
            "pulls_since_prior_record": int(pulls_since_prior),
            "nights_since_prior_record": int(nights_since_prior),
        })

    last_pos = record_positions[-1]
    total_real_pulls = len(real_sorted)
    pulls_since_last_record = total_real_pulls - 1 - last_pos
    nights_since_last_record = int(real_sorted.iloc[last_pos + 1:]["date"].nunique())
    current = {
        "pulls_since_last_record": int(pulls_since_last_record),
        "nights_since_last_record": nights_since_last_record,
        "last_record_date": records[-1]["date"],
        "last_record_pct": records[-1]["fight_percentage"],
    }

    # Window for the wall check: whichever is larger of "everything since the
    # last record" or "the last two raid nights" (guards against a long dry
    # spell within a single very-long night looking like a multi-night wall).
    window_by_pulls = real_sorted.iloc[last_pos + 1:]
    two_latest_dates = sorted(real_sorted["date"].unique())[-2:]
    window_by_nights = real_sorted[real_sorted["date"].isin(two_latest_dates)]
    window = window_by_pulls if len(window_by_pulls) >= len(window_by_nights) else window_by_nights

    wall = {"is_walled": False, "phase": None, "band": None, "stdev": None}
    if pulls_since_last_record >= wall_min_pulls and len(window) >= 2:
        stdev = float(window["fight_percentage"].std())
        if stdev <= wall_max_stdev:
            phase_mode = int(window["last_phase"].mode().iloc[0])
            band = f"{int(window['fight_percentage'].min())}-{int(window['fight_percentage'].max())}"
            wall = {"is_walled": True, "phase": phase_mode, "band": band, "stdev": round(stdev, 2)}

    return {"records": records, "current": current, "wall": wall}


def compute_phase_conversion(real: pd.DataFrame) -> list:
    """For each phase reached, what share of pulls that got that far survived
    it and pushed into the next phase (vs. wiped and ended the pull there).
    The final entry compares pulls reaching the deepest phase against actual
    kills — i.e. the clear rate out of the current wall."""
    if real.empty:
        return []
    max_phase = int(real["last_phase"].max())
    conversion = []
    for p in range(1, max_phase):
        entered = int((real["last_phase"] >= p).sum())
        converted = int((real["last_phase"] > p).sum())
        conversion.append({
            "from_phase": p,
            "to_phase": p + 1,
            "entered": entered,
            "converted": converted,
            "rate_pct": round(converted / entered * 100, 1) if entered else None,
        })
    entered_final = int((real["last_phase"] >= max_phase).sum())
    kills = int(real.loc[real["last_phase"] >= max_phase, "kill"].fillna(False).astype(bool).sum())
    conversion.append({
        "from_phase": max_phase,
        "to_phase": "kill",
        "entered": entered_final,
        "converted": kills,
        "rate_pct": round(kills / entered_final * 100, 1) if entered_final else None,
    })
    return conversion


def compute_phase_conversion_by_session(real: pd.DataFrame, dates: list) -> dict:
    """Same phase-to-phase conversion as compute_phase_conversion, but broken
    out per raid night so the dashboard can chart how each transition's
    survival rate is trending over time. Transitions are fixed to the
    all-time deepest phase reached (so every night's line lines up on the
    same x-axis); a night with zero pulls reaching a given phase leaves that
    point null rather than 0, so the chart should use spanGaps."""
    if real.empty:
        return {"transitions": [], "sessions": []}
    max_phase = int(real["last_phase"].max())
    transitions = [{"from_phase": p, "to_phase": p + 1} for p in range(1, max_phase)]
    transitions.append({"from_phase": max_phase, "to_phase": "kill"})

    sessions = []
    for d in dates:
        day = real[real["date"] == d]
        rates = []
        for t in transitions:
            p = t["from_phase"]
            entered = int((day["last_phase"] >= p).sum())
            if t["to_phase"] == "kill":
                converted = int(day.loc[day["last_phase"] >= p, "kill"].fillna(False).astype(bool).sum())
            else:
                converted = int((day["last_phase"] > p).sum())
            rates.append(round(converted / entered * 100, 1) if entered else None)
        sessions.append({"date": d, "rates": rates})

    return {"transitions": transitions, "sessions": sessions}


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pulls-csv", type=str, required=True,
                         help="Path to pulls.csv from fetch_dmu_logs.py")
    parser.add_argument("--template", type=str, default="dashboard_template.html",
                         help="Path to the HTML template file")
    parser.add_argument("--out", type=str, default="dmu_raid_dashboard.html",
                         help="Path to write the built dashboard HTML")
    parser.add_argument("--utc-offset", type=int, default=2,
                         help="Hours to add to UTC to get your local raid "
                              "time (default 2 = CEST). Use 1 for CET in "
                              "winter.")
    parser.add_argument("--raid-start-hour", type=int, default=20,
                         help="Local hour (0-23) your raid block starts, "
                              "default 20 (8pm)")
    parser.add_argument("--raid-length-hours", type=int, default=3,
                         help="Length of your raid block in hours, default 3")
    parser.add_argument("--wall-min-pulls-since-record", type=int, default=15,
                         help="Minimum real pulls since the last all-time-best "
                              "pull before a 'wall' can be flagged, default 15")
    parser.add_argument("--wall-max-stdev", type=float, default=6.0,
                         help="Max stdev of fight_percentage in the recent "
                              "window for a wall to be flagged — a tight "
                              "cluster suggests one specific mechanic rather "
                              "than inconsistent play, default 6.0")
    args = parser.parse_args()

    pulls_path = Path(args.pulls_csv)
    if not pulls_path.exists():
        raise SystemExit(f"ERROR: {pulls_path} not found. Run fetch_dmu_logs.py first.")

    df = pd.read_csv(pulls_path)
    if df.empty:
        raise SystemExit(f"ERROR: {pulls_path} has no rows.")

    df["start_time_utc"] = pd.to_datetime(df["start_time_utc"])
    df["end_time_utc"] = pd.to_datetime(df["end_time_utc"])
    df["date"] = df["start_time_utc"].dt.date.astype(str)
    df["utc_hour"] = df["start_time_utc"].dt.hour
    df["local_hour"] = (df["utc_hour"] + args.utc_offset) % 24

    dates = sorted(df["date"].unique())
    real = df[df["fight_percentage"].notna()].copy()

    # ---------------- overview ----------------
    total_hours = 0.0
    sessions = []
    for d in dates:
        day = df[df["date"] == d].sort_values("start_time_utc")
        day_real = day[day["fight_percentage"].notna()]
        start = day["start_time_utc"].iloc[0]
        end = day["end_time_utc"].iloc[-1]
        session_hours = round((end - start).total_seconds() / 3600, 2)
        total_hours += session_hours
        total_pulls = int(len(day))
        kills = int(day["kill"].fillna(False).astype(bool).sum())
        best_pct = float(day_real["fight_percentage"].min()) if len(day_real) else None
        furthest_phase = int(day["last_phase"].max()) if len(day) else None
        avg_duration = round(float(day["duration_seconds"].mean()), 1) if len(day) else None
        pulls_per_hour = round(total_pulls / session_hours, 1) if session_hours > 0 else None
        pct_stdev = round(float(day_real["fight_percentage"].std()), 2) if len(day_real) >= 2 else None
        sessions.append({
            "date": d,
            "total_pulls": total_pulls,
            "kills": kills,
            "best_pct_remaining_of_day": best_pct,
            "furthest_phase": furthest_phase,
            "avg_pull_duration_seconds": avg_duration,
            "session_hours": session_hours,
            "pulls_per_hour": pulls_per_hour,
            "pct_stdev": pct_stdev,
        })

    overview = {
        "total_sessions": len(dates),
        "total_pulls": int(len(df)),
        "total_hours": round(total_hours, 1),
        "date_start": dates[0],
        "date_end": dates[-1],
        "total_kills": int(df["kill"].fillna(False).astype(bool).sum()),
        "current_best_pct": sessions[-1]["best_pct_remaining_of_day"],
        "furthest_phase_reached": max(s["furthest_phase"] for s in sessions if s["furthest_phase"] is not None),
        "reset_pulls": int(df["fight_percentage"].isna().sum()),
    }

    # ---------------- phase stats (pitfalls) ----------------
    phase_stats = {}
    for phase in sorted(real["last_phase"].unique()):
        sub = real[real["last_phase"] == phase]
        bins = list(range(0, 105, 5))
        hist = pd.cut(sub["fight_percentage"], bins=bins, right=True).value_counts().sort_index()
        hist_list = [{"range": f"{int(iv.left)}-{int(iv.right)}", "count": int(c)}
                     for iv, c in hist.items() if c > 0]
        mode_bin = max(hist_list, key=lambda x: x["count"]) if hist_list else None
        phase_stats[int(phase)] = {
            "n": int(len(sub)),
            "min_pct": float(sub["fight_percentage"].min()),
            "max_pct": float(sub["fight_percentage"].max()),
            "mean_pct": round(float(sub["fight_percentage"].mean()), 2),
            "stdev_pct": round(float(sub["fight_percentage"].std()), 2) if len(sub) >= 2 else None,
            "histogram": hist_list,
            "wall_bucket": mode_bin,
        }

    # ---------------- phase composition per night ----------------
    comp = df.groupby(["date", "last_phase"]).size().unstack(fill_value=0)
    all_phase_cols = sorted(set(df["last_phase"].unique()) | {0, 1})
    comp = comp.reindex(columns=all_phase_cols, fill_value=0)
    phase_composition = []
    for d, row in comp.iterrows():
        entry = {"date": d}
        for p in all_phase_cols:
            key = "p0_reset" if p == 0 else f"p{int(p)}"
            entry[key] = int(row.get(p, 0))
        phase_composition.append(entry)

    # ---------------- hour-by-hour (local time) ----------------
    df["hour_bucket"] = df["local_hour"].apply(
        lambda h: bucket_hour(h, args.raid_start_hour, args.raid_length_hours))
    bucket_order_full, bucket_labels = build_bucket_order_and_labels(
        args.raid_start_hour, args.raid_length_hours)

    grid = []
    for d in dates:
        day = df[df["date"] == d]
        row = {"date": d, "hours": {}}
        for b in bucket_order_full:
            sub = day[day["hour_bucket"] == b]
            if len(sub) == 0:
                continue
            valid = sub[sub["fight_percentage"].notna()]
            row["hours"][b] = {
                "pull_count": int(len(sub)),
                "avg_pct": round(float(valid["fight_percentage"].mean()), 1) if len(valid) else None,
                "best_pct": round(float(valid["fight_percentage"].min()), 2) if len(valid) else None,
            }
        grid.append(row)

    bucket_order_present = [b for b in bucket_order_full if any(b in r["hours"] for r in grid)]

    aggregated = []
    for b in bucket_order_present:
        sub = df[df["hour_bucket"] == b]
        if len(sub) == 0:
            continue
        valid = sub[sub["fight_percentage"].notna()]
        n_nights = sub["date"].nunique()
        aggregated.append({
            "bucket": b,
            "label": bucket_labels[b],
            "nights_present": int(n_nights),
            "total_pulls": int(len(sub)),
            "avg_pulls_per_night": round(len(sub) / n_nights, 1),
            "avg_pct": round(float(valid["fight_percentage"].mean()), 1) if len(valid) else None,
            "best_pct": round(float(valid["fight_percentage"].min()), 2) if len(valid) else None,
        })

    hourly_cest = {
        "bucket_order": bucket_order_present,
        "bucket_labels": bucket_labels,
        "grid": grid,
        "aggregated": aggregated,
    }

    # ---------------- position stats (kept for completeness) ----------------
    from collections import defaultdict
    by_position = defaultdict(list)
    for s_idx, d in enumerate(dates):
        day = df[df["date"] == d].sort_values("start_time_utc")
        start = day["start_time_utc"].iloc[0]
        day = day.copy()
        day["hour_offset"] = ((day["start_time_utc"] - start).dt.total_seconds() // 3600).astype(int) + 1
        for pos, g in day.groupby("hour_offset"):
            valid = g[g["fight_percentage"].notna()]
            by_position[pos].append({
                "pull_count": int(len(g)),
                "avg_pct_remaining": float(valid["fight_percentage"].mean()) if len(valid) else None,
                "best_pct_remaining": float(valid["fight_percentage"].min()) if len(valid) else None,
            })

    position_stats = []
    for pos, hbs in sorted(by_position.items()):
        total_pulls = sum(h["pull_count"] for h in hbs)
        valid = [h for h in hbs if h["avg_pct_remaining"] is not None]
        position_stats.append({
            "position": int(pos),
            "sessions_reaching": len(hbs),
            "total_pulls": total_pulls,
            "avg_pulls_per_session": round(total_pulls / len(hbs), 1),
            "avg_pct_remaining": round(sum(h["avg_pct_remaining"] for h in valid) / len(valid), 1) if valid else None,
            "best_pct_remaining": min((h["best_pct_remaining"] for h in valid), default=None),
        })

    progression = compute_progression(real, args.wall_min_pulls_since_record, args.wall_max_stdev)
    phase_conversion = compute_phase_conversion(real)
    phase_conversion_by_session = compute_phase_conversion_by_session(real, dates)

    data = {
        "overview": overview,
        "sessions": sessions,
        "phase_stats": phase_stats,
        "phase_composition": phase_composition,
        "hourly_cest": hourly_cest,
        "position_stats": position_stats,
        "progression": progression,
        "phase_conversion": phase_conversion,
        "phase_conversion_by_session": phase_conversion_by_session,
    }

    # ---------------- inject into template ----------------
    template_path = Path(args.template)
    if not template_path.exists():
        raise SystemExit(f"ERROR: template not found at {template_path}")
    html = template_path.read_text(encoding="utf-8")

    data_json = json.dumps(data, separators=(",", ":"))
    date_range = f"{overview['date_start']} \u2013 {overview['date_end']}"
    reset_note = f"{overview['reset_pulls']} short practice/reset pulls excluded from phase stats."

    html = html.replace("__DATA_JSON__", data_json)
    html = html.replace("__DATE_RANGE__", date_range)
    html = html.replace("__RESET_NOTE__", reset_note)

    out_path = Path(args.out)
    out_path.write_text(html, encoding="utf-8")

    print(f"Built dashboard: {out_path}")
    print(f"  {overview['total_pulls']} pulls, {overview['total_sessions']} raid nights, "
          f"{overview['total_hours']}h logged, best {overview['current_best_pct']}% "
          f"(phase {overview['furthest_phase_reached']})")


if __name__ == "__main__":
    main()
