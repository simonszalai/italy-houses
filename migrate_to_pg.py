"""Migrate verified keeps from the local SQLite store into Postgres (listings + shortlist).

Reads the assessment/detail/list data, builds denormalized listing rows (incl. image URLs
and Italy-dot coords), and upserts into Postgres. Idempotent. Run:
  DATABASE_URL=... python3 webapp/migrate_to_pg.py [search]
"""
import os, sys, json
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".claude/skills/house-search/lib"))
import db as sq, seismic, foursides, access
import psycopg

ZLABEL = {"1": "Zona 1 (high)", "2": "Zona 2 (med)", "3": "Zona 3 (low)", "4": "Zona 4 (v.low)"}
# Preference weights learned from the couple's ratings/comments (loved set skews Piemonte,
# alt ~450-800, garden present, drivable). Car access is their #1 stated dealbreaker.
REGION_PREF = {"Piemonte": 2.5, "Lombardia": 1.5, "Toscana": 1.5, "Valle d'Aosta": 1.0, "Marche": 1.0}
DSN = os.environ["DATABASE_URL"]
SEARCH = sys.argv[1] if len(sys.argv) > 1 else "sibillini-and-alps"

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings(
  id TEXT PRIMARY KEY, search TEXT, price INT, area INT, garden_m2 INT, garden_band BOOL,
  rooms INT, fs TEXT, town TEXT, region TEXT, zona TEXT, zlabel TEXT, mtn_km INT,
  lat DOUBLE PRECISION, lng DOUBLE PRECISION, interior TEXT, reason TEXT, score REAL,
  image_urls JSONB, url TEXT, verdict TEXT, setting TEXT, alt INT);
ALTER TABLE listings ADD COLUMN IF NOT EXISTS verdict TEXT;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS setting TEXT;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS alt INT;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS access TEXT;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS isolation TEXT;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS neighbors TEXT;
CREATE TABLE IF NOT EXISTS shortlist(
  listing_id TEXT PRIMARY KEY, status TEXT, notes TEXT, updated_at TIMESTAMPTZ DEFAULT now());
"""

def rows():
    c = sq.connect(); crit = json.load(open(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "searches", SEARCH, "criteria.json")))
    ch = sq.criteria_hash(crit); gmin, gmax = crit["garden_min"], crit["garden_max"]
    q = c.execute("""SELECT a.listing_id, a.scores_json, a.reasons, a.verdict, d.description, d.price, d.area,
        d.garden_m2, d.rooms, d.image_urls_json, lv.town, lv.province, lv.mtn_dist_km, lv.alt
        FROM assessments a JOIN detail_view d ON d.site=a.site AND d.listing_id=a.listing_id
        JOIN list_view lv ON lv.site=a.site AND lv.listing_id=a.listing_id
        WHERE a.criteria_hash=?""", (ch,)).fetchall()
    for r in q:
        sc = json.loads(r["scores_json"] or "{}"); fs = foursides.classify(r["description"])
        if "isolation" not in sc:                  # cheap/rule drop or never vision-screened
            continue
        # LEAST-AGGRESSIVE (v3): exclude ONLY the user's hard rejects — in-town, not free-standing,
        # unsound shell, clearly non-mountainous. Neighbour-proximity (close_neighbors / hamlet) is
        # kept but DEMOTED via score, so isolated homes rank far above without hiding workable ones.
        iso = sc.get("isolation")
        if (iso in ("town", "hamlet") or fs == "no" or sc.get("free_standing_4_sides") == "no"
                or sc.get("mountain_terrain") == "no"
                or sc.get("exterior_sound") == "no" or sc.get("roof_ok") == "no"):
            continue
        g = r["garden_m2"]; band = bool(g and gmin <= g <= gmax)
        s = 0.0
        if fs == "yes": s += 5
        if band: s += 4
        elif not g: s += 0.5
        for k, v in (("exterior_sound", "yes"), ("roof_ok", "yes"), ("mountain_terrain", "yes")):
            if sc.get(k) == v: s += 2
        s += {"isolated": 5, "semi_isolated": 1, "close_neighbors": -4, "hamlet": -5, "town": -6}.get(iso, 0)
        if sc.get("visible_neighbors") == "none": s += 1   # confirmed zero neighbours
        if r["verdict"] == "keep": s += 2
        s += float(sc.get("confidence") or 0) * 2
        gz = c.execute("SELECT lat,lng FROM comune_geo WHERE comune_norm=?", (seismic.norm(r["town"] or ""),)).fetchone()
        sz = seismic.lookup(r["town"], seismic.GEO_TO_SIGLA.get(r["province"]))
        if sz and sz["zona"] in ("3", "4"): s += 1
        if r["price"]: s += (crit["price_max"] - r["price"]) / 80000
        # --- preference signal from ratings/comments ---
        acc = access.classify(r["description"])
        if acc == "foot": s -= 6          # their #1 dealbreaker: can't drive to it
        elif acc == "car": s += 3
        region = sz["region"] if sz else ""
        s += REGION_PREF.get(region, 0)
        a = r["alt"]
        if a is not None:
            if 400 <= a <= 900: s += 2     # loved-altitude band
            elif 300 <= a < 400 or 900 < a <= 1100: s += 1
        if g and 800 <= g <= 3000: s += 1.5   # they love a real garden / land to plant
        imgs = json.loads(r["image_urls_json"] or "[]")[:18]
        yield (r["listing_id"], SEARCH, r["price"], r["area"], g, band, r["rooms"], fs,
               r["town"], region, (sz["zona"] if sz else None),
               (ZLABEL.get(sz["zona"]) if sz else "n/a"), round(r["mtn_dist_km"] or 0),
               (gz["lat"] if gz else None), (gz["lng"] if gz else None), sc.get("interior_state"),
               (r["reasons"] or "")[:300], round(s, 2), json.dumps(imgs),
               f"https://www.idealista.it/immobile/{r['listing_id']}/", r["verdict"], sc.get("setting"), r["alt"], acc,
               iso, sc.get("visible_neighbors"))

def assessed_ids():
    """All listing_ids that got a real (vision) verdict under the current criteria_hash."""
    import json as _j
    c = sq.connect(); ch = sq.criteria_hash(json.load(open(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "searches", SEARCH, "criteria.json"))))
    out = set()
    for r in c.execute("SELECT listing_id, scores_json FROM assessments WHERE criteria_hash=?", (ch,)):
        if "isolation" in _j.loads(r["scores_json"] or "{}"):
            out.add(r["listing_id"])
    return out

def main():
    data = list(rows())
    surfaced = {row[0] for row in data}
    # listings the current vision pass judged but did NOT surface (now town/unsound/etc.) —
    # remove them so stale v2 false positives don't linger. NEVER delete anything voted on.
    excluded = assessed_ids() - surfaced
    with psycopg.connect(DSN, connect_timeout=30) as conn:
        conn.execute(SCHEMA)
        if excluded:
            voted = {r[0] for r in conn.execute("SELECT DISTINCT listing_id FROM votes").fetchall()}
            to_del = [i for i in excluded if i not in voted]
            if to_del:
                conn.execute("DELETE FROM listings WHERE id = ANY(%s)", (to_del,))
                conn.commit()
                print(f"removed {len(to_del)} now-excluded unrated listings")
        with conn.cursor() as cur:
            cur.executemany("""INSERT INTO listings
              (id,search,price,area,garden_m2,garden_band,rooms,fs,town,region,zona,zlabel,mtn_km,lat,lng,interior,reason,score,image_urls,url,verdict,setting,alt,access,isolation,neighbors)
              VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
              ON CONFLICT (id) DO UPDATE SET price=EXCLUDED.price,area=EXCLUDED.area,garden_m2=EXCLUDED.garden_m2,
                garden_band=EXCLUDED.garden_band,rooms=EXCLUDED.rooms,fs=EXCLUDED.fs,town=EXCLUDED.town,
                region=EXCLUDED.region,zona=EXCLUDED.zona,zlabel=EXCLUDED.zlabel,mtn_km=EXCLUDED.mtn_km,
                lat=EXCLUDED.lat,lng=EXCLUDED.lng,interior=EXCLUDED.interior,reason=EXCLUDED.reason,
                score=EXCLUDED.score,image_urls=EXCLUDED.image_urls,url=EXCLUDED.url,
                verdict=EXCLUDED.verdict,setting=EXCLUDED.setting,alt=EXCLUDED.alt,access=EXCLUDED.access,
                isolation=EXCLUDED.isolation,neighbors=EXCLUDED.neighbors""", data)
        conn.commit()
        n = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    print(f"migrated {len(data)} listings; table now has {n}")

if __name__ == "__main__":
    main()
