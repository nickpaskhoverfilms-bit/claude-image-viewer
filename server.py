import os
import io
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from PIL import Image as PILImage
from fastmcp import FastMCP
from fastmcp.utilities.types import Image
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
)


# Railway mounts a persistent volume at /data. Locally that directory does
# not exist (or is not writable), so fall back to ./data so the same code
# works in both places.
def _pick_data_dir() -> Path:
    railway = Path("/data")
    if railway.exists() and os.access(railway, os.W_OK):
        return railway
    local = Path("./data").resolve()
    local.mkdir(parents=True, exist_ok=True)
    return local


DATA_DIR = _pick_data_dir()
THUMBS_DIR = DATA_DIR / "thumbs"
FILES_DIR = DATA_DIR / "files"
THUMBS_DIR.mkdir(parents=True, exist_ok=True)
FILES_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "canvas.db"

ALLOWED_KINDS = {"reference", "generation", "anchor", "video", "deliverable", "note"}
ALLOWED_STATUSES = {"in_progress", "review", "locked"}
DEFAULT_PROJECT_ID = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _init_db() -> None:
    with _db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS project (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS card (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL REFERENCES project(id),
                kind TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'in_progress',
                title TEXT NOT NULL DEFAULT '',
                prompt TEXT NOT NULL DEFAULT '',
                source_url TEXT NOT NULL DEFAULT '',
                source_job_id TEXT NOT NULL DEFAULT '',
                thumbnail_url TEXT NOT NULL DEFAULT '',
                full_image_url TEXT NOT NULL DEFAULT '',
                parent_card_id INTEGER,
                section TEXT NOT NULL DEFAULT '',
                x REAL NOT NULL DEFAULT 0,
                y REAL NOT NULL DEFAULT 0,
                width REAL NOT NULL DEFAULT 220,
                height REAL NOT NULL DEFAULT 260,
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                deleted_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_card_project_active
                ON card(project_id, deleted_at);
            """
        )
        row = c.execute(
            "SELECT id FROM project WHERE id = ?", (DEFAULT_PROJECT_ID,)
        ).fetchone()
        if not row:
            c.execute(
                "INSERT INTO project (id, name, created_at) VALUES (?, ?, ?)",
                (DEFAULT_PROJECT_ID, "Default Project", _now()),
            )


_init_db()


def _card_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d.pop("deleted_at", None)
    return d


def _auto_place(project_id: int) -> tuple[float, float]:
    with _db() as c:
        count = c.execute(
            "SELECT COUNT(*) AS n FROM card "
            "WHERE project_id = ? AND deleted_at IS NULL",
            (project_id,),
        ).fetchone()["n"]
    col_w, row_h, margin = 240.0, 280.0, 20.0
    cols_per_row = 8
    col = count % cols_per_row
    row = count // cols_per_row
    return margin + col * col_w, margin + row * row_h


def _download_and_store(card_id: int, source_url: str) -> tuple[str, str]:
    try:
        resp = httpx.get(source_url, timeout=60, follow_redirects=True)
        resp.raise_for_status()
        img = PILImage.open(io.BytesIO(resp.content)).convert("RGB")
        full_path = FILES_DIR / f"{card_id}.jpg"
        img.save(full_path, format="JPEG", quality=95)
        thumb = img.copy()
        thumb.thumbnail((400, 400))
        thumb_path = THUMBS_DIR / f"{card_id}.jpg"
        thumb.save(thumb_path, format="JPEG", quality=85)
        return f"/thumbs/{card_id}.jpg", f"/files/{card_id}.jpg"
    except Exception:
        return "", ""


mcp = FastMCP("Image Viewer")


def shrink(url, max_edge=1568):
    data = httpx.get(url, timeout=60, follow_redirects=True).content
    img = PILImage.open(io.BytesIO(data)).convert("RGB")
    img.thumbnail((max_edge, max_edge))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85)
    return out.getvalue()


@mcp.tool
def view_asset(url: str) -> Image:
    """Download an image from a URL so it can be viewed and described."""
    return Image(data=shrink(url), format="jpeg")


@mcp.tool
def compare_assets(urls: list[str]) -> list[Image]:
    """Download up to 4 images from URLs so they can be compared side by side."""
    return [Image(data=shrink(u, 1024), format="jpeg") for u in urls[:4]]


@mcp.tool
def get_canvas_state(project_id: int = DEFAULT_PROJECT_ID) -> str:
    """Return every active card on the canvas as compact JSON.

    Use this at the start of a session to orient on what's already on the
    board. Soft-deleted cards are omitted.
    """
    with _db() as c:
        rows = c.execute(
            "SELECT * FROM card WHERE project_id = ? AND deleted_at IS NULL "
            "ORDER BY id",
            (project_id,),
        ).fetchall()
    cards = [_card_to_dict(r) for r in rows]
    return json.dumps(
        {"project_id": project_id, "cards": cards}, separators=(",", ":")
    )


@mcp.tool
def add_card(
    kind: str,
    title: str,
    source_url: str = "",
    prompt: str = "",
    source_job_id: str = "",
    parent_card_id: Optional[int] = None,
    section: str = "",
    x: Optional[float] = None,
    y: Optional[float] = None,
    notes: str = "",
    project_id: int = DEFAULT_PROJECT_ID,
) -> str:
    """Create a card on the canvas.

    kind must be one of: reference, generation, anchor, video, deliverable, note.
    If source_url is given, the server downloads the image and saves both a
    thumbnail and a full-resolution copy under /data; the served paths are
    written to thumbnail_url and full_image_url. If x/y are omitted, the card
    is auto-placed on a simple grid. Returns JSON with the new card's id and
    its served image URLs.
    """
    if kind not in ALLOWED_KINDS:
        raise ValueError(f"kind must be one of {sorted(ALLOWED_KINDS)}")
    if x is None or y is None:
        x, y = _auto_place(project_id)
    now = _now()
    with _db() as c:
        cur = c.execute(
            """INSERT INTO card
            (project_id, kind, status, title, prompt, source_url,
             source_job_id, thumbnail_url, full_image_url, parent_card_id,
             section, x, y, notes, created_at, updated_at)
            VALUES (?, ?, 'in_progress', ?, ?, ?, ?, '', '', ?, ?, ?, ?, ?, ?, ?)""",
            (
                project_id, kind, title, prompt, source_url, source_job_id,
                parent_card_id, section, x, y, notes, now, now,
            ),
        )
        card_id = cur.lastrowid
    thumb_url, full_url = "", ""
    if source_url:
        thumb_url, full_url = _download_and_store(card_id, source_url)
        if thumb_url:
            with _db() as c:
                c.execute(
                    "UPDATE card SET thumbnail_url = ?, full_image_url = ?, "
                    "updated_at = ? WHERE id = ?",
                    (thumb_url, full_url, _now(), card_id),
                )
    return json.dumps(
        {
            "card_id": card_id,
            "thumbnail_url": thumb_url,
            "full_image_url": full_url,
        },
        separators=(",", ":"),
    )


@mcp.tool
def update_card(
    card_id: int,
    title: Optional[str] = None,
    status: Optional[str] = None,
    prompt: Optional[str] = None,
    notes: Optional[str] = None,
    section: Optional[str] = None,
    parent_card_id: Optional[int] = None,
    x: Optional[float] = None,
    y: Optional[float] = None,
) -> str:
    """Update one or more fields on a card.

    Set status='locked' to lock a card; allowed statuses are in_progress,
    review, locked. Any argument left at its default (None) is not changed.
    Returns the updated card as JSON.
    """
    if status is not None and status not in ALLOWED_STATUSES:
        raise ValueError(f"status must be one of {sorted(ALLOWED_STATUSES)}")
    fields = {
        "title": title,
        "status": status,
        "prompt": prompt,
        "notes": notes,
        "section": section,
        "parent_card_id": parent_card_id,
        "x": x,
        "y": y,
    }
    fields = {k: v for k, v in fields.items() if v is not None}
    if not fields:
        return json.dumps({"error": "no fields to update"})
    sets = ", ".join(f"{k} = ?" for k in fields) + ", updated_at = ?"
    params = list(fields.values()) + [_now(), card_id]
    with _db() as c:
        c.execute(
            f"UPDATE card SET {sets} WHERE id = ? AND deleted_at IS NULL",
            params,
        )
        row = c.execute(
            "SELECT * FROM card WHERE id = ?", (card_id,)
        ).fetchone()
    if not row:
        return json.dumps({"error": f"card {card_id} not found"})
    return json.dumps(_card_to_dict(row), separators=(",", ":"))


@mcp.tool
def delete_card(card_id: int) -> str:
    """Soft-delete a card. The row is kept but hidden from get_canvas_state
    and the board."""
    now = _now()
    with _db() as c:
        c.execute(
            "UPDATE card SET deleted_at = ?, updated_at = ? "
            "WHERE id = ? AND deleted_at IS NULL",
            (now, now, card_id),
        )
    return json.dumps({"card_id": card_id, "deleted": True})


BOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Canvas Board</title>
<style>
  :root {
    --bg: #1a1a1f;
    --panel: #24252c;
    --panel-border: #34353d;
    --text: #e8e8ea;
    --muted: #9a9ba3;
    --in_progress: #d97706;
    --review: #2563eb;
    --locked: #16a34a;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
  header { position: sticky; top: 0; z-index: 10; padding: 10px 16px;
    background: rgba(26,26,31,0.92); border-bottom: 1px solid var(--panel-border);
    display: flex; align-items: center; gap: 12px; }
  header h1 { font-size: 14px; font-weight: 600; margin: 0; }
  header .meta { font-size: 12px; color: var(--muted); }
  #board { position: relative; width: 100%; min-height: calc(100vh - 44px); }
  .card { position: absolute; background: var(--panel);
    border: 1px solid var(--panel-border); border-radius: 8px;
    overflow: hidden; display: flex; flex-direction: column;
    box-shadow: 0 1px 3px rgba(0,0,0,0.4); }
  .card .thumb { flex: 1; background: #111216; display: flex;
    align-items: center; justify-content: center; overflow: hidden; }
  .card .thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .card .thumb .placeholder { color: var(--muted); font-size: 11px;
    text-transform: uppercase; letter-spacing: 0.08em; }
  .card .meta { padding: 8px 10px; display: flex; align-items: center;
    gap: 8px; border-top: 1px solid var(--panel-border); min-height: 36px; }
  .card .title { flex: 1; font-size: 12px; line-height: 1.3; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; }
  .card .status { font-size: 10px; padding: 2px 6px; border-radius: 999px;
    text-transform: uppercase; letter-spacing: 0.05em; color: white;
    white-space: nowrap; }
  .status.in_progress { background: var(--in_progress); }
  .status.review { background: var(--review); }
  .status.locked { background: var(--locked); }
  .empty { position: absolute; top: 40%; left: 50%; transform: translateX(-50%);
    color: var(--muted); font-size: 13px; }
</style>
</head>
<body>
<header>
  <h1>Canvas Board</h1>
  <span class="meta" id="meta">loading…</span>
</header>
<div id="board"><div class="empty" id="empty">loading…</div></div>
<script>
  const board = document.getElementById('board');
  const meta = document.getElementById('meta');
  const PAD = 40;

  function render(cards) {
    board.innerHTML = '';
    if (!cards.length) {
      const e = document.createElement('div');
      e.className = 'empty';
      e.textContent = 'no cards yet — add one with the MCP add_card tool';
      board.appendChild(e);
      return;
    }
    let maxX = 0, maxY = 0;
    for (const c of cards) {
      const el = document.createElement('div');
      el.className = 'card';
      el.style.left = c.x + 'px';
      el.style.top = c.y + 'px';
      el.style.width = c.width + 'px';
      el.style.height = c.height + 'px';

      const thumb = document.createElement('div');
      thumb.className = 'thumb';
      if (c.thumbnail_url) {
        const img = document.createElement('img');
        img.src = c.thumbnail_url;
        img.alt = c.title || ('card ' + c.id);
        thumb.appendChild(img);
      } else {
        const ph = document.createElement('div');
        ph.className = 'placeholder';
        ph.textContent = c.kind;
        thumb.appendChild(ph);
      }
      el.appendChild(thumb);

      const m = document.createElement('div');
      m.className = 'meta';
      const t = document.createElement('div');
      t.className = 'title';
      t.textContent = c.title || ('card ' + c.id);
      t.title = c.title || '';
      const s = document.createElement('div');
      s.className = 'status ' + c.status;
      s.textContent = c.status.replace('_', ' ');
      m.appendChild(t);
      m.appendChild(s);
      el.appendChild(m);

      board.appendChild(el);
      maxX = Math.max(maxX, c.x + c.width);
      maxY = Math.max(maxY, c.y + c.height);
    }
    board.style.minWidth = (maxX + PAD) + 'px';
    board.style.minHeight = (maxY + PAD) + 'px';
  }

  async function refresh() {
    try {
      const r = await fetch('/api/cards', { cache: 'no-store' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const data = await r.json();
      render(data.cards || []);
      meta.textContent = (data.cards || []).length + ' cards · updated ' +
        new Date().toLocaleTimeString();
    } catch (err) {
      meta.textContent = 'error: ' + err.message;
    }
  }

  refresh();
  setInterval(refresh, 3000);
</script>
</body>
</html>
"""


@mcp.custom_route("/board", methods=["GET"])
async def board_page(request: Request) -> HTMLResponse:
    return HTMLResponse(BOARD_HTML)


@mcp.custom_route("/api/cards", methods=["GET"])
async def api_cards(request: Request) -> JSONResponse:
    try:
        project_id = int(request.query_params.get("project_id", DEFAULT_PROJECT_ID))
    except ValueError:
        project_id = DEFAULT_PROJECT_ID
    with _db() as c:
        rows = c.execute(
            "SELECT * FROM card WHERE project_id = ? AND deleted_at IS NULL "
            "ORDER BY id",
            (project_id,),
        ).fetchall()
    return JSONResponse(
        {"project_id": project_id, "cards": [_card_to_dict(r) for r in rows]}
    )


def _serve_from(directory: Path, filename: str):
    # Resolve and guard against path traversal — the resolved file must sit
    # inside `directory`.
    try:
        path = (directory / filename).resolve()
        path.relative_to(directory.resolve())
    except (ValueError, OSError):
        return PlainTextResponse("not found", status_code=404)
    if not path.is_file():
        return PlainTextResponse("not found", status_code=404)
    return FileResponse(path, media_type="image/jpeg")


@mcp.custom_route("/thumbs/{filename}", methods=["GET"])
async def serve_thumb(request: Request):
    return _serve_from(THUMBS_DIR, request.path_params["filename"])


@mcp.custom_route("/files/{filename}", methods=["GET"])
async def serve_file(request: Request):
    return _serve_from(FILES_DIR, request.path_params["filename"])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    mcp.run(transport="http", host="0.0.0.0", port=port, path="/mcp")
