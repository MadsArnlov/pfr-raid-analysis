#!/usr/bin/env python3
"""
Build a deep-dive report for a single Dancing Mad (Ultimate) raid night.

This reads only pulls.csv (produced by main.py) and filters it down to one
raid night — either the most recent night present in the file, or a specific
`--date`. Everything in the report (pull-by-pull timeline, trend within the
night, downtime between pulls, phase composition) is derived from that one
night's rows alone.

The one exception is the "all-time context" section, which deliberately
reads the *full*, unfiltered pulls.csv (not just the target night) so the
report can say how this night's best pull compares to the best pull ever
logged. That's the only place this script looks outside the selected night.

USAGE
-----
    python src/build_night_report.py --pulls-csv ./dmu_data/pulls.csv

Optional flags:
    --date 2026-06-24                                 # analyze a specific night
                                                       # instead of the most recent one
    --template ./templates/night_report_template.html  # HTML template to inject data into
    --out ./output/dmu_night_report.html                # output file
    --utc-offset 2                           # hours to add to UTC for local
                                              # raid time (2 = CEST, 1 = CET)
    --raid-start-hour 20                     # local hour the raid block starts
    --raid-length-hours 3                    # length of the raid block
    --break-threshold-min 10                 # gaps at least this long count as
                                              # scheduled breaks, not wipe recovery
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from dmu_common import (
    add_raid_time_args,
    all_nights,
    bucket_hour,
    build_bucket_order_and_labels,
    compute_phase_conversion,
    compute_phase_stats,
    inject_template,
    load_pulls,
    real_pulls,
)


def select_night(df: pd.DataFrame, date_arg: str | None):
    available = all_nights(df)
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
    session_seconds = (end - start).total_seconds()
    active_seconds = float(night_df["duration_seconds"].sum())
    total_pulls = int(len(night_df))
    deepest = int(night_df["last_phase"].max()) if total_pulls else None
    return {
        "date": night_df["date"].iloc[0],
        "total_pulls": total_pulls,
        "kills": int(night_df["kill"].sum()),
        "session_start_utc": start.isoformat(),
        "session_end_utc": end.isoformat(),
        "session_hours": session_hours,
        "best_pct_remaining": float(real["fight_percentage"].min()) if len(real) else None,
        "furthest_phase": deepest,
        # pulls that reached the night's deepest phase — reps on the wall
        "wall_reps": int((night_df["last_phase"] >= deepest).sum()) if deepest else 0,
        "avg_pull_duration_seconds": round(float(night_df["duration_seconds"].mean()), 1) if total_pulls else None,
        "pulls_per_hour": round(total_pulls / session_hours, 1) if session_hours > 0 else None,
        "active_seconds": round(active_seconds, 1),
        # share of the session actually spent in the instance pulling
        "active_pct": round(active_seconds / session_seconds * 100, 1) if session_seconds > 0 else None,
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
            "kill": bool(row["kill"]),
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
            "kills": int(sub["kill"].sum()),
            "avg_pct": round(float(valid["fight_percentage"].mean()), 1) if len(valid) else None,
            "best_pct": round(float(valid["fight_percentage"].min()), 2) if len(valid) else None,
        })

    return {
        "bucket_order": [b["bucket"] for b in buckets],
        "bucket_labels": bucket_labels,
        "buckets": buckets,
    }


def compute_trend(pull_timeline: list, hourly: dict) -> dict:
    real_pulls_list = [p for p in pull_timeline if not p["is_reset"]]

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

    best_pull = min(real_pulls_list, key=lambda p: p["fight_percentage"]) if real_pulls_list else None
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


def compute_downtime(pull_timeline: list, break_threshold_seconds: float) -> dict:
    """Gaps between pulls, in chronological order, split into scheduled-break
    sized gaps vs normal wipe-recovery gaps. Re-pull speed (avg recovery gap)
    is the number a raid leader can actually act on — breaks would drown it
    out if averaged together."""
    gaps = [
        {
            "after_pull_index": p["index"] - 1,
            "gap_seconds": p["gap_since_previous_seconds"],
            "is_break": p["gap_since_previous_seconds"] >= break_threshold_seconds,
        }
        for p in pull_timeline
        if p["gap_since_previous_seconds"] is not None
    ]
    gap_values = [g["gap_seconds"] for g in gaps]
    breaks = [g for g in gaps if g["is_break"]]
    recoveries = [g["gap_seconds"] for g in gaps if not g["is_break"]]
    return {
        "gaps": gaps,
        "break_threshold_seconds": break_threshold_seconds,
        "total_downtime_seconds": round(sum(gap_values), 1) if gap_values else 0.0,
        "break_count": len(breaks),
        "break_seconds": round(sum(g["gap_seconds"] for g in breaks), 1),
        "longest_gap_seconds": max(gap_values) if gap_values else None,
        "avg_recovery_seconds": round(sum(recoveries) / len(recoveries), 1) if recoveries else None,
    }


def compute_phase_composition_single(night_df: pd.DataFrame) -> dict:
    comp = {}
    for phase, count in night_df["last_phase"].value_counts().items():
        key = "p0_reset" if phase == 0 else f"p{int(phase)}"
        comp[key] = int(count)
    return comp


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


def build_one_night(df: pd.DataFrame, target_date: str, args, out_path: Path) -> dict:
    """Compute and write the report for one raid night. Returns the night
    overview dict (used by callers that build multiple nights in one run)."""
    night_df = df[df["date"] == target_date].sort_values("start_time_utc").reset_index(drop=True)

    night = compute_night_overview(night_df)
    pull_timeline = compute_pull_timeline(night_df)
    hourly = compute_hourly_breakdown(night_df, args.utc_offset, args.raid_start_hour, args.raid_length_hours)
    trend = compute_trend(pull_timeline, hourly)
    downtime = compute_downtime(pull_timeline, args.break_threshold_min * 60)
    real_night = real_pulls(night_df)

    data = {
        "night": night,
        "pulls": pull_timeline,
        "hourly": hourly,
        "trend": trend,
        "downtime": downtime,
        "phase_composition": compute_phase_composition_single(night_df),
        "context": compute_context(df, night, target_date),
        "phase_conversion": compute_phase_conversion(real_night),
        "phase_stats": compute_phase_stats(real_night),
    }

    inject_template(Path(args.template), out_path, {
        "__DATA_JSON__": json.dumps(data, separators=(",", ":")),
        "__DATE_RANGE__": target_date,
    })

    print(f"Built night report: {out_path}")
    print(f"  {target_date} — {night['total_pulls']} pulls, best {night['best_pct_remaining']}% "
          f"(phase {night['furthest_phase']}), trend: {trend['direction']}")
    return night


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pulls-csv", type=str, default="./dmu_data/pulls.csv",
                         help="Path to pulls.csv from main.py")
    parser.add_argument("--date", type=str, default=None,
                         help="Raid night to analyze (YYYY-MM-DD). Defaults to "
                              "the most recent night present in pulls.csv. "
                              "Ignored if --all-nights is set.")
    parser.add_argument("--all-nights", action="store_true",
                         help="Build one report per raid night present in "
                              "pulls.csv, instead of just one night. Each "
                              "report is written to '<out-dir>/<date>.html'.")
    parser.add_argument("--out-dir", type=str, default="output/nights",
                         help="Directory to write per-night reports into "
                              "when --all-nights is set (default: "
                              "output/nights)")
    parser.add_argument("--template", type=str, default="templates/night_report_template.html",
                         help="Path to the HTML template file")
    parser.add_argument("--out", type=str, default="output/dmu_night_report.html",
                         help="Path to write the built report HTML "
                              "(single-night mode only)")
    add_raid_time_args(parser)
    parser.add_argument("--break-threshold-min", type=float, default=10,
                         help="Gaps at least this many minutes long count as "
                              "scheduled breaks rather than wipe recovery, "
                              "default 10")
    args = parser.parse_args()

    df = load_pulls(Path(args.pulls_csv))

    if args.all_nights:
        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        for target_date in all_nights(df):
            build_one_night(df, target_date, args, out_dir / f"{target_date}.html")
        return

    target_date, _ = select_night(df, args.date)
    build_one_night(df, target_date, args, Path(args.out))


if __name__ == "__main__":
    main()
