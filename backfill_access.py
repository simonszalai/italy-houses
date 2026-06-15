"""Backfill listings.access for EVERY row already in Postgres (both batches) by classifying
the locally-stored idealista description. Lets the car-access chip show on listings rated
before access scoring existed. Run: DATABASE_URL=... python3 webapp/backfill_access.py
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".claude/skills/house-search/lib"))
import db as sq, access
import psycopg

def main():
    c = sq.connect()
    desc = {r["listing_id"]: r["description"] for r in
            c.execute("SELECT listing_id, description FROM detail_view WHERE site='idealista'")}
    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=30) as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM listings").fetchall()]
        updates = [(access.classify(desc.get(i, "")), i) for i in ids]
        with conn.cursor() as cur:
            cur.executemany("UPDATE listings SET access=%s WHERE id=%s", updates)
        conn.commit()
        from collections import Counter
        cn = Counter(a for a, _ in updates)
    print(f"backfilled access on {len(updates)} listings: {dict(cn)}")

if __name__ == "__main__":
    main()
