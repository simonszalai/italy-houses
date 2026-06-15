"""Migrate verified keeps from the local SQLite store into Postgres (listings + shortlist).

Reads the assessment/detail/list data, builds denormalized listing rows (incl. image URLs
and Italy-dot coords), and upserts into Postgres. Idempotent. Run:
  DATABASE_URL=... python3 webapp/migrate_to_pg.py [search]
"""
import os, sys, json
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".claude/skills/house-search/lib"))
import db as sq, seismic, foursides
import psycopg

ZLABEL = {"1": "Zona 1 (high)", "2": "Zona 2 (med)", "3": "Zona 3 (low)", "4": "Zona 4 (v.low)"}
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
        WHERE a.criteria_hash=? AND a.verdict IN ('keep','maybe')""", (ch,)).fetchall()
    for r in q:
        sc = json.loads(r["scores_json"] or "{}"); fs = foursides.classify(r["description"])
        # surface all keeps; for maybes only the genuine ones (mountain confirmed, decent confidence) —
        # skip the photo-sparse maybes that couldn't be judged.
        if r["verdict"] == "maybe" and not (sc.get("mountain_terrain") == "yes" and (sc.get("confidence") or 0) >= 0.55):
            continue
        g = r["garden_m2"]; band = bool(g and gmin <= g <= gmax)
        s = 0.0
        if fs == "yes": s += 5
        if band: s += 4
        elif not g: s += 0.5
        for k, v in (("exterior_sound", "yes"), ("roof_ok", "yes"), ("mountain_terrain", "yes")):
            if sc.get(k) == v: s += 2
        if sc.get("setting") in ("forest_isolated", "countryside"): s += 2
        if r["verdict"] == "keep": s += 2
        s += float(sc.get("confidence") or 0) * 2
        gz = c.execute("SELECT lat,lng FROM comune_geo WHERE comune_norm=?", (seismic.norm(r["town"] or ""),)).fetchone()
        sz = seismic.lookup(r["town"], seismic.GEO_TO_SIGLA.get(r["province"]))
        if sz and sz["zona"] in ("3", "4"): s += 1
        if r["price"]: s += (crit["price_max"] - r["price"]) / 80000
        imgs = json.loads(r["image_urls_json"] or "[]")[:18]
        yield (r["listing_id"], SEARCH, r["price"], r["area"], g, band, r["rooms"], fs,
               r["town"], (sz["region"] if sz else ""), (sz["zona"] if sz else None),
               (ZLABEL.get(sz["zona"]) if sz else "n/a"), round(r["mtn_dist_km"] or 0),
               (gz["lat"] if gz else None), (gz["lng"] if gz else None), sc.get("interior_state"),
               (r["reasons"] or "")[:300], round(s, 2), json.dumps(imgs),
               f"https://www.idealista.it/immobile/{r['listing_id']}/", r["verdict"], sc.get("setting"), r["alt"])

def main():
    data = list(rows())
    with psycopg.connect(DSN, connect_timeout=30) as conn:
        conn.execute(SCHEMA)
        with conn.cursor() as cur:
            cur.executemany("""INSERT INTO listings
              (id,search,price,area,garden_m2,garden_band,rooms,fs,town,region,zona,zlabel,mtn_km,lat,lng,interior,reason,score,image_urls,url,verdict,setting,alt)
              VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
              ON CONFLICT (id) DO UPDATE SET price=EXCLUDED.price,area=EXCLUDED.area,garden_m2=EXCLUDED.garden_m2,
                garden_band=EXCLUDED.garden_band,rooms=EXCLUDED.rooms,fs=EXCLUDED.fs,town=EXCLUDED.town,
                region=EXCLUDED.region,zona=EXCLUDED.zona,zlabel=EXCLUDED.zlabel,mtn_km=EXCLUDED.mtn_km,
                lat=EXCLUDED.lat,lng=EXCLUDED.lng,interior=EXCLUDED.interior,reason=EXCLUDED.reason,
                score=EXCLUDED.score,image_urls=EXCLUDED.image_urls,url=EXCLUDED.url,
                verdict=EXCLUDED.verdict,setting=EXCLUDED.setting,alt=EXCLUDED.alt""", data)
        conn.commit()
        n = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    print(f"migrated {len(data)} listings; table now has {n}")

if __name__ == "__main__":
    main()
