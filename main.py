#!/usr/bin/env python3
"""
Fetch Dancing Mad (Ultimate) pull data from FFLogs for a given guild, using the
FFLogs API v2 (GraphQL) client-credentials flow.

SETUP
-----
1. Create a v2 API client at https://www.fflogs.com/api/clients/
   - Name: whatever you like
   - Redirect URL: not needed for client-credentials, put http://localhost
2. Copy the Client ID and Client Secret.
3. Create a file called `.env` next to this script (NOT committed anywhere, NOT
   pasted into a chat) with:

       FFLOGS_CLIENT_ID=your_client_id
       FFLOGS_CLIENT_SECRET=your_client_secret

4. Run:

       uv run main.py

   Optional flags:
       --guild-id 102435                # numeric guild ID from the fflogs URL
       --zone-name "Dancing Mad"        # server-side zone filter, default below
       --encounter-name "Dancing Mad"   # extra fight-name filter, default below
       --out-dir ./dmu_data             # where JSON/CSV output goes
       --max-reports 200                # safety cap
       --full-refetch                   # ignore the cache and refetch everything

OUTPUT
------
Writes two files into --out-dir:
  - reports_raw.json     Full raw report/fight data as returned by FFLogs.
                          Doubles as the fetch cache: on the next run, reports
                          whose endTime hasn't changed are reused instead of
                          refetched, so a refresh only hits the API for new
                          raid nights.
  - pulls.csv            One row per pull (fight) on the matching encounter(s).
                          This is the single source of truth for
                          build_dashboard.py and build_night_report.py.
"""

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv is optional; env vars can also be set directly

TOKEN_URL = "https://www.fflogs.com/oauth/token"
API_URL = "https://www.fflogs.com/api/v2/client"

ZONES_QUERY = """
query {
  worldData {
    zones {
      id
      name
    }
  }
}
"""

REPORTS_QUERY = """
query ($guildID: Int!, $zoneID: Int, $page: Int!) {
  reportData {
    reports(guildID: $guildID, zoneID: $zoneID, page: $page, limit: 25) {
      data {
        code
        title
        startTime
        endTime
        zone { id name }
      }
      total
      per_page
      current_page
      has_more_pages
    }
  }
}
"""

FIGHTS_QUERY = """
query ($code: String!) {
  reportData {
    report(code: $code) {
      title
      startTime
      zone { name }
      fights {
        id
        name
        encounterID
        kill
        difficulty
        startTime
        endTime
        fightPercentage
        bossPercentage
        lastPhase
      }
    }
  }
}
"""


def get_token(client_id: str, client_secret: str) -> str:
    resp = requests.post(
        TOKEN_URL,
        data={"grant_type": "client_credentials"},
        auth=(client_id, client_secret),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def gql(token: str, query: str, variables: dict) -> dict:
    resp = requests.post(
        API_URL,
        json={"query": query, "variables": variables},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"GraphQL error: {data['errors']}")
    return data["data"]


def find_zone_id(token: str, name_substr: str) -> Optional[int]:
    """Look up a zone's numeric ID by a case-insensitive substring match on
    its name, e.g. 'Dancing Mad' -> the ID for 'Dancing Mad (Ultimate)'."""
    data = gql(token, ZONES_QUERY, {})
    zones = data["worldData"]["zones"]
    matches = [z for z in zones if name_substr.lower() in z["name"].lower()]
    if not matches:
        print(f"  No zone found matching '{name_substr}'. Available zones "
              f"include: {[z['name'] for z in zones[:15]]} ...")
        return None
    if len(matches) > 1:
        print(f"  Multiple zones matched '{name_substr}': "
              f"{[(z['id'], z['name']) for z in matches]}. Using the first one.")
    zone = matches[0]
    print(f"  Resolved zone '{name_substr}' -> id={zone['id']} name='{zone['name']}'")
    return zone["id"]


def fetch_all_reports(token: str, guild_id: int, zone_id, max_reports: int) -> list:
    reports = []
    page = 1
    while True:
        data = gql(token, REPORTS_QUERY,
                    {"guildID": guild_id, "zoneID": zone_id, "page": page})
        block = data["reportData"]["reports"]
        reports.extend(block["data"])
        print(f"  fetched page {page} ({len(block['data'])} reports, "
              f"{len(reports)} total so far)")
        if not block["has_more_pages"] or len(reports) >= max_reports:
            break
        page += 1
        time.sleep(0.3)  # be polite to the API
    return reports[:max_reports]


def fetch_fights_for_report(token: str, code: str) -> dict:
    data = gql(token, FIGHTS_QUERY, {"code": code})
    return data["reportData"]["report"]


def load_cache(raw_path: str) -> dict:
    """Previously fetched report details, keyed by report code. Entries carry
    'report_end_time' (the report-level endTime at fetch time); if the
    current report list shows the same endTime, the report hasn't gained new
    fights and the cached fights can be reused."""
    if not os.path.exists(raw_path):
        return {}
    try:
        with open(raw_path, encoding="utf-8") as f:
            entries = json.load(f)
        return {e["code"]: e for e in entries if "code" in e}
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        print(f"  WARNING: could not read cache {raw_path} ({e}); refetching everything.")
        return {}


def ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--guild-id", type=int, default=102435,
                         help="Numeric guild ID from the fflogs guild URL "
                              "(e.g. 102435 from /guild/reports-list/102435). "
                              "Defaults to this static's guild.")
    parser.add_argument("--zone-name", type=str, default="Dancing Mad",
                         help="Substring to match the FFLogs zone name "
                              "(default: 'Dancing Mad', matches "
                              "'Dancing Mad (Ultimate)'). Set to '' to "
                              "disable server-side zone filtering.")
    parser.add_argument("--encounter-name", type=str, default="Dancing Mad",
                         help="Substring to match fight names against, as a "
                              "belt-and-suspenders filter on top of the zone "
                              "filter (default: 'Dancing Mad', matches the "
                              "fight name used across all DMU phases)")
    parser.add_argument("--out-dir", type=str, default="./dmu_data")
    parser.add_argument("--max-reports", type=int, default=200)
    parser.add_argument("--full-refetch", action="store_true",
                         help="Ignore cached report data in reports_raw.json "
                              "and refetch every report from the API")
    args = parser.parse_args()

    client_id = os.environ.get("FFLOGS_CLIENT_ID")
    client_secret = os.environ.get("FFLOGS_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("ERROR: FFLOGS_CLIENT_ID / FFLOGS_CLIENT_SECRET not set. "
              "Create a .env file (see script header) or export them.",
              file=sys.stderr)
        sys.exit(1)

    os.makedirs(args.out_dir, exist_ok=True)
    raw_path = os.path.join(args.out_dir, "reports_raw.json")

    print("Authenticating with FFLogs...")
    token = get_token(client_id, client_secret)

    zone_id = None
    if args.zone_name:
        print(f"Looking up zone ID for '{args.zone_name}'...")
        zone_id = find_zone_id(token, args.zone_name)
        if zone_id is None:
            print("Proceeding without server-side zone filtering "
                  "(will filter by fight name only, which is slower "
                  "since it fetches every report).")

    print(f"Fetching report list for guild {args.guild_id}"
          f"{' (zone filtered)' if zone_id else ''}...")
    reports = fetch_all_reports(token, args.guild_id, zone_id, args.max_reports)
    print(f"Found {len(reports)} matching reports for this guild.")

    cache = {} if args.full_refetch else load_cache(raw_path)

    all_report_details = []
    pulls = []
    fetched, reused = 0, 0

    for i, rep in enumerate(reports, 1):
        code = rep["code"]
        cached = cache.get(code)
        if cached is not None and cached.get("report_end_time") == rep.get("endTime"):
            detail = cached
            reused += 1
        else:
            print(f"[{i}/{len(reports)}] Fetching fights for report {code} "
                  f"({rep.get('title')})...")
            try:
                detail = fetch_fights_for_report(token, code)
            except Exception as e:
                print(f"  WARNING: failed to fetch {code}: {e}")
                if cached is not None:
                    print("  Falling back to the cached copy of this report.")
                    detail = cached
                else:
                    continue
            else:
                detail = {"code": code, "report_end_time": rep.get("endTime"), **detail}
                fetched += 1
                time.sleep(0.2)  # be polite to the API
        all_report_details.append(detail)

        zone_name = (detail.get("zone") or {}).get("name", "") or ""
        report_title = detail.get("title", "")

        matching_fights = [
            f for f in detail.get("fights", [])
            if args.encounter_name.lower() in (f.get("name") or "").lower()
            or args.encounter_name.lower() in zone_name.lower()
        ]

        for f in matching_fights:
            start_ms = detail["startTime"] + f["startTime"]
            end_ms = detail["startTime"] + f["endTime"]
            pulls.append({
                "report_code": code,
                "report_title": report_title,
                "zone_name": zone_name,
                "fight_id": f["id"],
                "fight_name": f.get("name"),
                "difficulty": f.get("difficulty"),
                "kill": f.get("kill"),
                "fight_percentage": f.get("fightPercentage"),
                "boss_percentage": f.get("bossPercentage"),
                "last_phase": f.get("lastPhase"),
                "duration_seconds": round((f["endTime"] - f["startTime"]) / 1000, 1),
                "start_time_utc": ms_to_iso(start_ms),
                "end_time_utc": ms_to_iso(end_ms),
                "start_time_epoch_ms": start_ms,
            })

    print(f"Report details: {fetched} fetched from the API, {reused} reused from cache.")

    # Sort pulls chronologically
    pulls.sort(key=lambda p: p["start_time_epoch_ms"])

    # Write raw backup / fetch cache
    with open(raw_path, "w", encoding="utf-8") as f:
        json.dump(all_report_details, f, indent=2)
    print(f"Wrote raw report data to {raw_path}")

    if not pulls:
        print("No matching pulls found. Check --encounter-name, or inspect "
              "reports_raw.json to see what fight names/zones actually appear.")
        return

    # Write flat pulls CSV
    csv_path = os.path.join(args.out_dir, "pulls.csv")
    fieldnames = list(pulls[0].keys())
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(pulls)
    print(f"Wrote {len(pulls)} pulls to {csv_path}")

    print("\nDone. Build the dashboards with:\n"
          "  uv run build_dashboard.py\n"
          "  uv run build_night_report.py\n"
          "or both at once with: uv run refresh.py")


if __name__ == "__main__":
    main()
