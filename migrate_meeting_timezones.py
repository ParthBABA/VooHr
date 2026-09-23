"""Report-only migration helper for meetings whose scheduled_at was stored
with the wrong wall-clock time due to a timezone bug.

Context: the Meeting Tracker's schedule form previously sent the raw value of
an <input type="datetime-local"> (e.g. "2026-09-22T16:15") straight to the
backend. That string carries no timezone, but meetings.create_meeting treated
any naive datetime as UTC — so a meeting scheduled for 4:15 PM in a UTC+5:30
timezone was stored as 4:15 PM UTC (a 5.5-hour silent shift).

This script does NOT auto-correct anything, because the original timezone of
each affected record is unknowable (it varies per user). Instead it prints a
report of every meeting with status "scheduled" or "missed" along with its
current stored scheduled_at, so a human can verify whether each record is
right and fix or delete the ones that matter (usually just test meetings from
today, which can simply be recreated after the fix).

Usage:
    python migrate_meeting_timezones.py [--org-id <mongo_oid>]

Requires MONGODB_URI (and optionally MONGODB_DB) in the environment / .env.
"""

import os
import sys

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()


def list_affected(db, org_id=None, statuses=("scheduled", "missed")):
    query = {"status": {"$in": list(statuses)}}
    if org_id:
        query["org_id"] = org_id
    return list(db.meetings.find(query).sort("scheduled_at", 1))


def fmt(dt):
    if dt is None:
        return "(none)"
    return dt.isoformat()


def print_report(meetings):
    if not meetings:
        print("No meetings found with status scheduled/missed.")
        return 0

    print(f"Found {len(meetings)} meeting(s) with status scheduled/missed.")
    print("=" * 80)
    for m in meetings:
        print(f"  _id:           {m['_id']}")
        print(f"  title:         {m.get('title', '')}")
        print(f"  status:        {m.get('status')}")
        print(f"  org_id:        {m.get('org_id')}")
        print(f"  employee_id:   {m.get('employee_id')}")
        print(f"  scheduled_at:  {fmt(m.get('scheduled_at'))}")
        print("  -" * 26)
    print("=" * 80)
    print("")
    print("IMPORTANT: records from before this fix may be shifted by each")
    print("user's UTC offset (e.g. +05:30 for India Standard Time) because the")
    print("stored naive datetime was treated as UTC. The original timezone is")
    print("NOT recorded anywhere, so it cannot be recovered automatically.")
    print("")
    print("Next steps (manual):")
    print("  1. Identify which meetings above actually matter.")
    print("  2. Delete test/stale meetings (e.g. via the Meeting Tracker UI or")
    print("     a direct db.meetings.delete_one) and recreate them now that the")
    print("     fix ships — their scheduled_at will then be stored correctly.")
    print("  3. For any real meeting you must keep, decide the correct local")
    print("     time in the user's timezone and PATCH it with a UTC-offset ISO")
    print("     string (e.g. '2026-09-22T10:45:00Z' for a 4:15 PM IST meeting),")
    print("     so the stored instant is the true scheduled moment.")
    return len(meetings)


def main():
    args = [a for a in sys.argv[1:]]
    org_id = None
    if args and args[0] == "--org-id" and len(args) > 1:
        org_id = args[1]

    uri = os.environ.get("MONGODB_URI")
    db_name = os.environ.get("MONGODB_DB", "voohr")
    if not uri:
        print("Error: MONGODB_URI environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    print(f"Connecting to MongoDB...")
    client = MongoClient(uri)
    db = client[db_name]

    try:
        affected = list_affected(db, org_id=org_id)
        count = print_report(affected)
        sys.exit(1 if count > 0 else 0)
    finally:
        client.close()


if __name__ == "__main__":
    main()