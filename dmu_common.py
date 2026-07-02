"""Shared data logic for the DMU dashboard and night-report builders.

Both build_dashboard.py and build_night_report.py start from the same
pulls.csv and need the same primitives: loading/typing the CSV, local-time
hour bucketing, phase-to-phase conversion rates, the per-phase wipe
histograms ("pitfalls"), and template injection. They live here so the two
scripts can't drift apart.

A "real" pull is one with a non-null fight_percentage. Pulls with a null
fight_percentage / last_phase == 0 are short practice/reset starts and are
excluded from progress stats (but still counted as pulls).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd


def add_raid_time_args(parser) -> None:
    """CLI flags shared by both builders for mapping UTC timestamps onto the
    static's fixed local-time raid block."""
    parser.add_argument("--utc-offset", type=int, default=2,
                        help="Hours to add to UTC to get your local raid "
                             "time (default 2 = CEST). Use 1 for CET in "
                             "winter.")
    parser.add_argument("--raid-start-hour", type=int, default=20,
                        help="Local hour (0-23) your raid block starts, "
                             "default 20 (8pm)")
    parser.add_argument("--raid-length-hours", type=int, default=3,
                        help="Length of your raid block in hours, default 3")


def load_pulls(csv_path: Path) -> pd.DataFrame:
    """Read pulls.csv and add the derived columns everything downstream uses."""
    if not csv_path.exists():
        raise SystemExit(f"ERROR: {csv_path} not found. Run main.py first.")
    df = pd.read_csv(csv_path)
    if df.empty:
        raise SystemExit(f"ERROR: {csv_path} has no rows.")
    df["start_time_utc"] = pd.to_datetime(df["start_time_utc"])
    df["end_time_utc"] = pd.to_datetime(df["end_time_utc"])
    df["date"] = df["start_time_utc"].dt.date.astype(str)
    df["kill"] = df["kill"].eq(True)  # null kill flag counts as a wipe
    return df


def real_pulls(df: pd.DataFrame) -> pd.DataFrame:
    """Pulls with a recorded fight_percentage (excludes practice/reset starts)."""
    return df[df["fight_percentage"].notna()].copy()


def all_nights(df: pd.DataFrame) -> list:
    """All distinct raid-night dates present in pulls.csv, oldest first."""
    return sorted(df["date"].unique())


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
        labels[str(h)] = f"{h % 24:02d}:00–{(h + 1) % 24:02d}:00"
    return order, labels


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
    kills = int(real.loc[real["last_phase"] >= max_phase, "kill"].sum())
    conversion.append({
        "from_phase": max_phase,
        "to_phase": "kill",
        "entered": entered_final,
        "converted": kills,
        "rate_pct": round(kills / entered_final * 100, 1) if entered_final else None,
    })
    return conversion


def compute_phase_stats(real: pd.DataFrame) -> dict:
    """Distribution of boss HP% remaining at pull end, grouped by the phase
    each pull reached — the "pitfalls" histograms. A tall bucket means many
    pulls die at nearly the same HP, i.e. one specific mechanic."""
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


def inject_template(template_path: Path, out_path: Path, replacements: dict) -> None:
    """Fill the template's __PLACEHOLDER__ slots and write the final HTML."""
    if not template_path.exists():
        raise SystemExit(f"ERROR: template not found at {template_path}")
    html = template_path.read_text(encoding="utf-8")
    for placeholder, value in replacements.items():
        html = html.replace(placeholder, value)
    out_path.write_text(html, encoding="utf-8")
