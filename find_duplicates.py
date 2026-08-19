"""Safe diagnostic utility to find duplicate email_hash records in the
users collection.

Usage:
    python find_duplicates.py

This script:
  - Finds all duplicate email_hash values using MongoDB aggregation.
  - Reports document IDs, created_at, org_id, and role for each group.
  - Does NOT expose raw passwords, secrets, session tokens, TOTP secrets,
    or encryption keys.
  - Does NOT automatically delete or merge any records.
  - Clearly identifies which records require manual resolution.

Run this before attempting to create the unique index, or after receiving
DuplicateKeyError on startup.
"""

import os
import sys
from datetime import datetime, timezone

from pymongo import MongoClient


def find_duplicates(db):
    """Aggregate users by email_hash and return groups with >1 document.

    Returns a list of dicts, each with:
      - email_hash: the duplicated hash value
      - count: number of documents with this hash
      - documents: list of {id, created_at, org_id, role, has_password,
        last_login} for each document
    """
    pipeline = [
        {"$group": {
            "_id": "$email_hash",
            "count": {"$sum": 1},
            "docs": {"$push": {
                "id": {"$toString": "$_id"},
                "created_at": "$created_at",
                "org_id": {"$toString": "$org_id"},
                "role": "$role",
                "has_password": {"$ne": ["$password_hash", None]},
                "last_login": "$last_login",
            }},
        }},
        {"$match": {"count": {"$gt": 1}}},
        {"$sort": {"count": -1}},
    ]
    return list(db.users.aggregate(pipeline))


def print_report(duplicates):
    """Print a human-readable report of duplicate email_hash groups."""
    if not duplicates:
        print("No duplicate email_hash records found.")
        print("The unique index can be created safely.")
        return 0

    total_groups = len(duplicates)
    total_docs = sum(g["count"] for g in duplicates)
    print(f"Found {total_groups} duplicate group(s) affecting {total_docs} "
          f"document(s).\n")
    print("=" * 72)
    print("ACTION REQUIRED: These records need manual resolution before")
    print("the unique email_hash index can be created.")
    print("=" * 72)

    for i, group in enumerate(duplicates, 1):
        h = group["_id"]
        # Show first and last 8 chars of hash for identification
        short_hash = f"{h[:8]}...{h[-8:]}" if len(h) > 16 else h
        print(f"\n--- Group {i}: email_hash = {short_hash} "
              f"({group['count']} documents) ---")
        for j, doc in enumerate(group["docs"], 1):
            created = doc.get("created_at")
            if created and isinstance(created, datetime):
                created_str = created.strftime("%Y-%m-%d %H:%M:%S UTC")
            else:
                created_str = str(created) if created else "unknown"

            last = doc.get("last_login")
            if last and isinstance(last, datetime):
                last_str = last.strftime("%Y-%m-%d %H:%M:%S UTC")
            else:
                last_str = str(last) if last else "never"

            print(f"  Document {j}:")
            print(f"    _id:          {doc['id']}")
            print(f"    created_at:   {created_str}")
            print(f"    org_id:       {doc['org_id']}")
            print(f"    role:         {doc.get('role', 'unknown')}")
            print(f"    has_password: {doc['has_password']}")
            print(f"    last_login:   {last_str}")

    print("\n" + "=" * 72)
    print("RESOLUTION STEPS:")
    print("  1. For each group above, decide which document to KEEP.")
    print("  2. Manually update any references (sessions, employees,")
    print("     notifications) pointing at the documents you will delete.")
    print("  3. Delete the unwanted duplicate documents from MongoDB.")
    print("  4. Restart the application — the unique index will then be")
    print("     created successfully.")
    print("=" * 72)
    print("\nDO NOT delete records without checking for dependent data")
    print("(sessions, employees, notifications, active_sessions).")
    return total_groups


def main():
    uri = os.environ.get("MONGODB_URI")
    db_name = os.environ.get("MONGODB_DB", "voohr")
    if not uri:
        print("Error: MONGODB_URI environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    print(f"Connecting to MongoDB...")
    client = MongoClient(uri)
    db = client[db_name]

    try:
        print(f"Scanning '{db_name}.users' collection for duplicate email_hash values...\n")
        duplicates = find_duplicates(db)
        count = print_report(duplicates)
        sys.exit(1 if count > 0 else 0)
    finally:
        client.close()


if __name__ == "__main__":
    main()
