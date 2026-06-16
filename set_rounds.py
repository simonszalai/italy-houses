"""Assign a search ROUND number to every listing in Postgres, inferred from when its detail
was first fetched locally (each round's fetch run clusters on distinct days). Re-runnable:
edit ROUND_BOUNDS when a new round happens. Round = the LAST bound whose start <= fetched day.

  DATABASE_URL=... python3 webapp/set_rounds.py
"""
import os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".claude/skills/house-search/lib"))
import db as sq
import psycopg

# (round_number, inclusive start date YYYY-MM-DD of that round's fetching)
ROUND_BOUNDS = [(1, "2026-06-01"), (2, "2026-06-15")]

def round_of(fetched_at):
    if not fetched_at:
        return 1
    day = time.strftime("%Y-%m-%d", time.localtime(fetched_at))
    r = ROUND_BOUNDS[0][0]
    for num, start in ROUND_BOUNDS:
        if day >= start:
            r = num
    return r

def round3_ids(c):
    """Round 3 = the privacy-forward additions: private (forest/countryside) maybes that the
    EARLIER surfacing rule (mountain_terrain=yes AND confidence>=0.55) had skipped. These only
    appeared in the app in the privacy pass, so they are their own round regardless of fetch date."""
    import json
    ids = set()
    for r in c.execute("SELECT listing_id, scores_json FROM assessments WHERE criteria_hash='v2-forest' AND verdict='maybe'"):
        sc = json.loads(r["scores_json"] or "{}")
        if sc.get("setting") in ("forest_isolated", "countryside") and not (sc.get("mountain_terrain") == "yes" and (sc.get("confidence") or 0) >= 0.55):
            ids.add(r["listing_id"])
    return ids

def main():
    c = sq.connect()
    fa = {row["listing_id"]: row["fetched_at"] for row in
          c.execute("SELECT listing_id, fetched_at FROM detail_view WHERE site='idealista'")}
    r3 = round3_ids(c)
    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=30) as conn:
        conn.execute("ALTER TABLE listings ADD COLUMN IF NOT EXISTS round INT")
        ids = [r[0] for r in conn.execute("SELECT id FROM listings").fetchall()]
        updates = [(3 if i in r3 else round_of(fa.get(i)), i) for i in ids]
        with conn.cursor() as cur:
            cur.executemany("UPDATE listings SET round=%s WHERE id=%s", updates)
        conn.commit()
        from collections import Counter
        cn = Counter(r for r, _ in updates)
    print(f"set round on {len(updates)} listings: {dict(sorted(cn.items()))}")

if __name__ == "__main__":
    main()
