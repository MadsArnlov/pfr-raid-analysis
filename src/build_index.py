#!/usr/bin/env python3
"""
Build the landing page listing every raid night (linking to its report) plus
a link to the full-history dashboard.

Reads only pulls.csv. For each raid night present, computes the same
lightweight summary fields shown in the per-night report's hero (pull count,
kills, best HP% remaining, furthest phase) via a groupby on the derived
`date` column — independent of build_night_report.py so each build script
only depends on dmu_common, never on another build script.

USAGE
-----
    python src/build_index.py --pulls-csv ./dmu_data/pulls.csv

Optional flags:
    --template ./templates/index_template.html   # HTML template to inject data into
    --out ./output/index.html                    # output file
    --nights-dir nights                          # relative dir the per-night
                                                  # reports live in, used to
                                                  # build links
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from dmu_common import all_nights, inject_template, load_pulls


def compute_night_summaries(df) -> list:
    summaries = []
    for date in all_nights(df):
        night_df = df[df["date"] == date]
        real = night_df[night_df["fight_percentage"].notna()]
        summaries.append({
            "date": date,
            "total_pulls": int(len(night_df)),
            "kills": int(night_df["kill"].sum()),
            "best_pct_remaining": float(real["fight_percentage"].min()) if len(real) else None,
            "furthest_phase": int(night_df["last_phase"].max()) if len(night_df) else None,
        })
    return summaries


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pulls-csv", type=str, default="./dmu_data/pulls.csv",
                         help="Path to pulls.csv from main.py")
    parser.add_argument("--template", type=str, default="templates/index_template.html",
                         help="Path to the HTML template file")
    parser.add_argument("--out", type=str, default="output/index.html",
                         help="Path to write the built index page")
    parser.add_argument("--nights-dir", type=str, default="nights",
                         help="Relative directory the per-night reports live "
                              "in, used to build links (default: nights)")
    args = parser.parse_args()

    df = load_pulls(Path(args.pulls_csv))
    summaries = compute_night_summaries(df)

    date_range = f"{summaries[0]['date']} – {summaries[-1]['date']}" if summaries else "No raid nights yet"

    inject_template(Path(args.template), Path(args.out), {
        "__NIGHTS_JSON__": json.dumps(summaries, separators=(",", ":")),
        "__NIGHTS_DIR__": args.nights_dir,
        "__DATE_RANGE__": date_range,
        "__GENERATED_AT__": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    })

    print(f"Built index page: {args.out}")
    print(f"  {len(summaries)} raid nights listed")


if __name__ == "__main__":
    main()
