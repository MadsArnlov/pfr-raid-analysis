#!/usr/bin/env python3
"""
Build a deep-dive report for a single Dancing Mad (Ultimate) raid night.

This reads only pulls.csv (produced by fetch_dmu_logs.py / main.py) and
filters it down to one raid night — either the most recent night present in
the file, or a specific `--date`. Everything in the report (pull-by-pull
timeline, trend within the night, downtime between pulls, phase composition)
is derived from that one night's rows alone.

The one exception is the "all-time context" section, which deliberately
reads the *full*, unfiltered pulls.csv (not just the target night) so the
report can say how this night's best pull compares to the best pull ever
logged. That's the only place this script looks outside the selected night.

USAGE
-----
    python build_night_report.py --pulls-csv ./dmu_data/pulls.csv

Optional flags:
    --date 2026-06-24                        # analyze a specific night
                                              # instead of the most recent one
    --template ./night_report_template.html  # HTML template to inject data into
    --out ./dmu_night_report.html            # output file
    --utc-offset 2                           # hours to add to UTC for local
                                              # raid time (2 = CEST, 1 = CET)
    --raid-start-hour 20                     # local hour the raid block starts
    --raid-length-hours 3                    # length of the raid block

Typical "after each raid night" workflow:

    python main.py --guild-id 102435
    python build_night_report.py --pulls-csv ./dmu_data/pulls.csv
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd


def bucket_hour(local_hour: int, raid_start: int, raid_length: int) -> str:
    """Classify an hour into 'pre', one of the raid-block hours (as a
    string of the block's starting hour), or 'late'. Mirrors the same
    bucketing used in build_dashboard.py."""
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
        labels[str(h)] = f"{h % 24:02d}:00–{(h + 1) % 24:02d}:00"
    return order, labels


def load_pulls(csv_path: Path) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    df["start_time_utc"] = pd.to_datetime(df["start_time_utc"])
    df["end_time_utc"] = pd.to_datetime(df["end_time_utc"])
    df["date"] = df["start_time_utc"].dt.date.astype(str)
    return df


def select_night(df: pd.DataFrame, date_arg: str | None):
    available = sorted(df["date"].unique())
    target_date = date_arg or available[-1]
    if target_date not in available:
        raise SystemExit(
            f"ERROR: no pulls found for {target_date}. "
            f"Available dates: {', '.join(available)}"
        )
    night_df = df[df["date"] == target_date].sort_values("start_time_utc").reset_index(drop=True)
    return target_date, night_df


def compute_night_overview(night_df: pd.DataFrame) -> dict:
    real = night_df[night_df["fight_percentage"].notna()]
    start = night_df["start_time_utc"].iloc[0]
    end = night_df["end_time_utc"].iloc[-1]
    session_hours = round((end - start).total_seconds() / 3600, 2)
    total_pulls = int(len(night_df))
    return {
        "date": night_df["date"].iloc[0],
        "total_pulls": total_pulls,
        "kills": int(night_df["kill"].fillna(False).astype(bool).sum()),
        "session_start_utc": start.isoformat(),
        "session_end_utc": end.isoformat(),
        "session_hours": session_hours,
        "best_pct_remaining": float(real["fight_percentage"].min()) if len(real) else None,
        "furthest_phase": int(night_df["last_phase"].max()) if total_pulls else None,
        "avg_pull_duration_seconds": round(float(night_df["duration_seconds"].mean()), 1) if total_pulls else None,
        "pulls_per_hour": round(total_pulls / session_hours, 1) if session_hours > 0 else None,
    }


def compute_pull_timeline(night_df: pd.DataFrame) -> list:
    timeline = []
    running_best = None
    prev_end = None
    for i, row in night_df.iterrows():
        fp = row["fight_percentage"]
        fp = None if pd.isna(fp) else float(fp)
        is_reset = fp is None or int(row["last_phase"]) == 0
        if fp is not None:
            running_best = fp if running_best is None else min(running_best, fp)
        gap = None if prev_end is None else round((row["start_time_utc"] - prev_end).total_seconds(), 1)
        timeline.append({
            "index": i + 1,
            "fight_id": int(row["fight_id"]),
            "start_time_utc": row["start_time_utc"].isoformat(),
            "end_time_utc": row["end_time_utc"].isoformat(),
            "duration_seconds": float(row["duration_seconds"]),
            "gap_since_previous_seconds": gap,
            "kill": bool(row["kill"]) if not pd.isna(row["kill"]) else False,
            "fight_percentage": fp,
            "last_phase": int(row["last_phase"]) if not pd.isna(row["last_phase"]) else 0,
            "is_reset": bool(is_reset),
            "running_best_pct": running_best,
        })
        prev_end = row["end_time_utc"]
    return timeline


def compute_hourly_breakdown(night_df: pd.DataFrame, utc_offset: int,
                              raid_start: int, raid_length: int) -> dict:
    df = night_df.copy()
    df["local_hour"] = (df["start_time_utc"].dt.hour + utc_offset) % 24
    df["hour_bucket"] = df["local_hour"].apply(
        lambda h: bucket_hour(h, raid_start, raid_length))

    bucket_order_full, bucket_labels = build_bucket_order_and_labels(raid_start, raid_length)

    buckets = []
    for b in bucket_order_full:
        sub = df[df["hour_bucket"] == b]
        if len(sub) == 0:
            continue
        valid = sub[sub["fight_percentage"].notna()]
        buckets.append({
            "bucket": b,
            "label": bucket_labels[b],
            "pull_count": int(len(sub)),
            "kills": int(sub["kill"].fillna(False).astype(bool).sum()),
            "avg_pct": round(float(valid["fight_percentage"].mean()), 1) if len(valid) else None,
            "best_pct": round(float(valid["fight_percentage"].min()), 2) if len(valid) else None,
        })

    return {
        "bucket_order": [b["bucket"] for b in buckets],
        "bucket_labels": bucket_labels,
        "buckets": buckets,
    }


def compute_trend(pull_timeline: list, hourly: dict) -> dict:
    real_pulls = [p for p in pull_timeline if not p["is_reset"]]

    scored_buckets = [b for b in hourly["buckets"] if b["avg_pct"] is not None]
    if len(scored_buckets) < 2:
        direction = "insufficient_data"
        first_hour = scored_buckets[0] if scored_buckets else None
        last_hour = scored_buckets[0] if scored_buckets else None
    else:
        first_hour = scored_buckets[0]
        last_hour = scored_buckets[-1]
        delta = first_hour["avg_pct"] - last_hour["avg_pct"]  # positive = improvement
        if delta > 2:
            direction = "improving"
        elif delta < -2:
            direction = "regressing"
        else:
            direction = "plateauing"

    best_pull = min(real_pulls, key=lambda p: p["fight_percentage"]) if real_pulls else None
    best_pull_position = None
    if best_pull is not None:
        n = len(pull_timeline)
        third = max(1, n / 3)
        if best_pull["index"] <= third:
            best_pull_position = "early"
        elif best_pull["index"] <= 2 * third:
            best_pull_position = "mid"
        else:
            best_pull_position = "late"

    return {
        "direction": direction,
        "first_hour": first_hour,
        "last_hour": last_hour,
        "best_pull": best_pull,
        "best_pull_position": best_pull_position,
    }


def compute_downtime(pull_timeline: list) -> dict:
    gaps = [
        {"after_pull_index": p["index"] - 1, "gap_seconds": p["gap_since_previous_seconds"]}
        for p in pull_timeline
        if p["gap_since_previous_seconds"] is not None
    ]
    gaps.sort(key=lambda g: g["gap_seconds"], reverse=True)
    gap_values = [g["gap_seconds"] for g in gaps]
    return {
        "gaps": gaps,
        "total_downtime_seconds": round(sum(gap_values), 1) if gap_values else 0.0,
        "longest_gap_seconds": gap_values[0] if gap_values else None,
        "avg_gap_seconds": round(sum(gap_values) / len(gap_values), 1) if gap_values else None,
    }


def compute_phase_composition_single(night_df: pd.DataFrame) -> dict:
    comp = {}
    for phase, count in night_df["last_phase"].value_counts().items():
        key = "p0_reset" if phase == 0 else f"p{int(phase)}"
        comp[key] = int(count)
    return comp


def compute_phase_conversion(real: pd.DataFrame) -> list:
    """Mirrors build_dashboard.py's phase-to-phase conversion, scoped to one
    night: of the pulls that reached phase N, what share survived it and
    pushed into phase N+1 (or, for the deepest phase reached, into an actual
    kill)."""
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


def compute_phase_stats(real: pd.DataFrame) -> dict:
    """Mirrors build_dashboard.py's phase_stats (pitfalls), scoped to one
    night: distribution of HP% remaining at pull end, grouped by the phase
    each pull reached."""
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
    return phase_stats


def compute_context(full_df: pd.DataFrame, night_overview: dict, target_date: str) -> dict:
    real = full_df[full_df["fight_percentage"].notna()]
    prior = real[real["date"] < target_date]

    all_time_best_pct = float(real["fight_percentage"].min()) if len(real) else None
    all_time_best_date = None
    if len(real):
        all_time_best_date = real.loc[real["fight_percentage"].idxmin(), "date"]

    prior_best_pct = float(prior["fight_percentage"].min()) if len(prior) else None
    night_best = night_overview["best_pct_remaining"]
    is_new_best_night = (
        night_best is not None
        and (prior_best_pct is None or night_best < prior_best_pct)
    )

    return {
        "all_time_best_pct": all_time_best_pct,
        "all_time_best_date": all_time_best_date,
        "all_time_furthest_phase": int(full_df["last_phase"].max()),
        "is_new_best_night": bool(is_new_best_night),
        "nights_before_this_one": int(full_df[full_df["date"] < target_date]["date"].nunique()),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pulls-csv", type=str, required=True,
                         help="Path to pulls.csv from main.py")
    parser.add_argument("--date", type=str, default=None,
                         help="Raid night to analyze (YYYY-MM-DD). Defaults to "
                              "the most recent night present in pulls.csv.")
    parser.add_argument("--template", type=str, default="night_report_template.html",
                         help="Path to the HTML template file")
    parser.add_argument("--out", type=str, default="dmu_night_report.html",
                         help="Path to write the built report HTML")
    parser.add_argument("--utc-offset", type=int, default=2,
                         help="Hours to add to UTC to get your local raid "
                              "time (default 2 = CEST). Use 1 for CET in "
                              "winter.")
    parser.add_argument("--raid-start-hour", type=int, default=20,
                         help="Local hour (0-23) your raid block starts, "
                              "default 20 (8pm)")
    parser.add_argument("--raid-length-hours", type=int, default=3,
                         help="Length of your raid block in hours, default 3")
    args = parser.parse_args()

    pulls_path = Path(args.pulls_csv)
    if not pulls_path.exists():
        raise SystemExit(f"ERROR: {pulls_path} not found. Run main.py first.")

    df = load_pulls(pulls_path)
    if df.empty:
        raise SystemExit(f"ERROR: {pulls_path} has no rows.")

    target_date, night_df = select_night(df, args.date)

    night = compute_night_overview(night_df)
    pull_timeline = compute_pull_timeline(night_df)
    hourly = compute_hourly_breakdown(night_df, args.utc_offset, args.raid_start_hour, args.raid_length_hours)
    trend = compute_trend(pull_timeline, hourly)
    downtime = compute_downtime(pull_timeline)
    phase_composition = compute_phase_composition_single(night_df)
    context = compute_context(df, night, target_date)
    real_night = night_df[night_df["fight_percentage"].notna()]
    phase_conversion = compute_phase_conversion(real_night)
    phase_stats = compute_phase_stats(real_night)

    data = {
        "night": night,
        "pulls": pull_timeline,
        "hourly": hourly,
        "trend": trend,
        "downtime": downtime,
        "phase_composition": phase_composition,
        "context": context,
        "phase_conversion": phase_conversion,
        "phase_stats": phase_stats,
    }

    # ---------------- inject into template ----------------
    template_path = Path(args.template)
    if not template_path.exists():
        raise SystemExit(f"ERROR: template not found at {template_path}")
    html = template_path.read_text(encoding="utf-8")

    data_json = json.dumps(data, separators=(",", ":"))
    html = html.replace("__DATA_JSON__", data_json)
    html = html.replace("__DATE_RANGE__", target_date)

    out_path = Path(args.out)
    out_path.write_text(html, encoding="utf-8")

    print(f"Built night report: {out_path}")
    print(f"  {target_date} — {night['total_pulls']} pulls, best {night['best_pct_remaining']}% "
          f"(phase {night['furthest_phase']}), trend: {trend['direction']}")


if __name__ == "__main__":
    main()
