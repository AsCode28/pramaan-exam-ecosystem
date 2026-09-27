"""Schema probe for the PRAMAAN screening prototype (dev-only, read-only).

Prints tables, events DDL, FK lists, PK columns, and nullability flags.
Usage (from backend/):  python scripts/check_schema.py
"""

import sqlite3
import sys

DB = sys.argv[1] if len(sys.argv) > 1 else "pramaan.db"

con = sqlite3.connect(DB)
tables = sorted(
    r[0]
    for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
)
print("tables:", tables)

print("--- events DDL ---")
print(con.execute("SELECT sql FROM sqlite_master WHERE name='events'").fetchone()[0])

print("--- responses DDL ---")
print(con.execute("SELECT sql FROM sqlite_master WHERE name='responses'").fetchone()[0])

print("--- sqlite_sequence ---")
try:
    print(con.execute("SELECT name FROM sqlite_sequence").fetchall())
except sqlite3.OperationalError as exc:
    print("NOTE:", exc)

for t in [
    "exams",
    "nodes",
    "candidates",
    "sessions",
    "questions",
    "responses",
    "events",
    "incidents",
    "incident_sessions",
]:
    print(f"--- FK: {t} ---")
    for row in con.execute(f"PRAGMA foreign_key_list({t})").fetchall():
        print(f"  {t}.{row[3]} -> {row[2]}.{row[4]}")

print("--- PK cols: incident_sessions ---")
for _, name, _type, notnull, _dflt, pk in con.execute(
    "PRAGMA table_info(incident_sessions)"
).fetchall():
    print(f"  {name} pk={pk} notnull={notnull}")

print("--- NULLABLE: events.hash/previous_hash, responses.last_event_id ---")
for _, name, _type, notnull, _dflt, _pk in con.execute(
    "PRAGMA table_info(events)"
).fetchall():
    if name in ("previous_hash", "hash", "sequence_no"):
        print(f"  events.{name} notnull={notnull}")
for _, name, _type, notnull, _dflt, _pk in con.execute(
    "PRAGMA table_info(responses)"
).fetchall():
    if name == "last_event_id":
        print(f"  responses.last_event_id notnull={notnull}")
