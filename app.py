"""Italy house-search web app: serves the SPA, a JSON API, and an image proxy.

Postgres-backed; shortlist state is shared across all clients. Env: DATABASE_URL.
"""
import os, json, httpx
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row

HERE = os.path.dirname(os.path.abspath(__file__))
DSN = os.environ["DATABASE_URL"]
if "sslmode=" not in DSN:
    DSN += ("&" if "?" in DSN else "?") + "sslmode=require"
pool = ConnectionPool(DSN, min_size=1, max_size=5, kwargs={"row_factory": dict_row}, open=True)
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
_imgcache = {}

app = FastAPI()

@app.get("/api/listings")
def listings():
    with pool.connection() as c:
        return c.execute("""SELECT l.*, s.status, s.notes FROM listings l
            LEFT JOIN shortlist s ON s.listing_id=l.id ORDER BY l.score DESC""").fetchall()

@app.get("/api/outline")
def outline():
    return json.load(open(os.path.join(HERE, "static", "italy_outline.json")))

@app.post("/api/shortlist")
async def set_shortlist(req: Request):
    b = await req.json()
    lid = b.get("listing_id"); status = b.get("status"); notes = b.get("notes")
    if not lid: raise HTTPException(400, "listing_id required")
    with pool.connection() as c:
        if status in (None, "", "none"):
            c.execute("DELETE FROM shortlist WHERE listing_id=%s", (lid,))
        else:
            c.execute("""INSERT INTO shortlist(listing_id,status,notes,updated_at)
                VALUES(%s,%s,%s,now())
                ON CONFLICT(listing_id) DO UPDATE SET status=EXCLUDED.status,
                  notes=COALESCE(EXCLUDED.notes,shortlist.notes),updated_at=now()""",
                (lid, status, notes))
        c.commit()
    return {"ok": True}

@app.get("/img/{lid}/{idx}")
def img(lid: str, idx: int):
    with pool.connection() as c:
        r = c.execute("SELECT image_urls FROM listings WHERE id=%s", (lid,)).fetchone()
    if not r or idx >= len(r["image_urls"]): raise HTTPException(404)
    url = r["image_urls"][idx]
    if url in _imgcache:
        return Response(_imgcache[url], media_type="image/jpeg", headers={"Cache-Control": "public,max-age=604800"})
    try:
        resp = httpx.get(url, headers={"User-Agent": UA, "Referer": "https://www.idealista.it/"}, timeout=20, follow_redirects=True)
        data = resp.content
    except Exception:
        raise HTTPException(502)
    if len(_imgcache) < 4000:
        _imgcache[url] = data
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "public,max-age=604800"})

@app.get("/")
def index():
    return FileResponse(os.path.join(HERE, "static", "index.html"))

app.mount("/", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
