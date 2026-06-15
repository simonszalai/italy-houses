"""Italy house-search web app: SPA + JSON API + image proxy.

Postgres-backed. Two people each vote like/maybe/dislike per listing (no auth — each
device picks a name once); listings carry everyone's votes and a cumulative score.
Env: DATABASE_URL.
"""
import os, json, httpx
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import FileResponse
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row

HERE = os.path.dirname(os.path.abspath(__file__))
DSN = os.environ["DATABASE_URL"]
if "sslmode=" not in DSN:
    DSN += ("&" if "?" in DSN else "?") + "sslmode=require"
pool = ConnectionPool(DSN, min_size=1, max_size=5, kwargs={"row_factory": dict_row}, open=True)
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
_imgcache = {}

with pool.connection() as _c:
    _c.execute("""CREATE TABLE IF NOT EXISTS votes(
        listing_id TEXT, voter TEXT, vote TEXT, comment TEXT, updated_at TIMESTAMPTZ DEFAULT now(),
        PRIMARY KEY (listing_id, voter))""")
    _c.execute("ALTER TABLE votes ADD COLUMN IF NOT EXISTS comment TEXT")
    _c.commit()

app = FastAPI()

@app.get("/api/listings")
def listings():
    with pool.connection() as c:
        return c.execute("""SELECT l.*,
            COALESCE(json_object_agg(v.voter, v.vote) FILTER (WHERE v.vote IS NOT NULL), '{}') AS votes,
            COALESCE(json_object_agg(v.voter, v.comment) FILTER (WHERE v.comment IS NOT NULL AND v.comment<>''), '{}') AS comments
            FROM listings l LEFT JOIN votes v ON v.listing_id=l.id
            GROUP BY l.id ORDER BY l.score DESC""").fetchall()

@app.get("/api/voters")
def voters():
    with pool.connection() as c:
        return [r["voter"] for r in c.execute("SELECT DISTINCT voter FROM votes ORDER BY voter")]

@app.post("/api/vote")
async def vote(req: Request):
    b = await req.json()
    lid, voter, v = b.get("listing_id"), (b.get("voter") or "").strip(), b.get("vote")
    if not lid or not voter: raise HTTPException(400, "listing_id and voter required")
    with pool.connection() as c:
        if v in (None, "", "none"):
            c.execute("UPDATE votes SET vote=NULL, updated_at=now() WHERE listing_id=%s AND voter=%s", (lid, voter))
        else:
            c.execute("""INSERT INTO votes(listing_id,voter,vote,updated_at) VALUES(%s,%s,%s,now())
                ON CONFLICT(listing_id,voter) DO UPDATE SET vote=EXCLUDED.vote,updated_at=now()""",
                (lid, voter, v))
        c.execute("DELETE FROM votes WHERE listing_id=%s AND voter=%s AND vote IS NULL AND (comment IS NULL OR comment='')", (lid, voter))
        c.commit()
    return {"ok": True}

@app.post("/api/comment")
async def comment(req: Request):
    b = await req.json()
    lid, voter = b.get("listing_id"), (b.get("voter") or "").strip()
    cm = (b.get("comment") or "").strip() or None
    if not lid or not voter: raise HTTPException(400, "listing_id and voter required")
    with pool.connection() as c:
        c.execute("""INSERT INTO votes(listing_id,voter,comment,updated_at) VALUES(%s,%s,%s,now())
            ON CONFLICT(listing_id,voter) DO UPDATE SET comment=EXCLUDED.comment,updated_at=now()""",
            (lid, voter, cm))
        c.execute("DELETE FROM votes WHERE listing_id=%s AND voter=%s AND vote IS NULL AND (comment IS NULL OR comment='')", (lid, voter))
        c.commit()
    return {"ok": True}

@app.get("/api/outline")
def outline():
    return json.load(open(os.path.join(HERE, "static", "italy_outline.json")))

@app.get("/img/{lid}/{idx}")
def img(lid: str, idx: int):
    with pool.connection() as c:
        r = c.execute("SELECT image_urls FROM listings WHERE id=%s", (lid,)).fetchone()
    if not r or idx >= len(r["image_urls"]): raise HTTPException(404)
    url = r["image_urls"][idx]
    if url in _imgcache:
        return Response(_imgcache[url], media_type="image/jpeg", headers={"Cache-Control": "public,max-age=604800"})
    data = None
    for attempt in range(2):
        try:
            resp = httpx.get(url, headers={"User-Agent": UA, "Referer": "https://www.idealista.it/"}, timeout=25, follow_redirects=True)
            if resp.status_code == 200 and resp.content:
                data = resp.content; break
        except Exception:
            pass
    if data is None:
        raise HTTPException(502)
    if len(_imgcache) < 4000:
        _imgcache[url] = data
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "public,max-age=604800"})

@app.get("/")
def index():
    return FileResponse(os.path.join(HERE, "static", "index.html"))
