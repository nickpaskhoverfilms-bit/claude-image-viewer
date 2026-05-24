import os
import io
import json
import hmac
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


def _require_env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise RuntimeError(
            f"{name} environment variable is required but missing or empty. "
            "Set it in Railway (Variables tab) before redeploying."
        )
    return val


# Fail-fast: if either secret is missing, the server refuses to boot so
# we never accidentally serve an unprotected board or MCP endpoint.
BOARD_KEY = _require_env("BOARD_KEY")
MCP_TOKEN = _require_env("MCP_TOKEN")
BOARD_COOKIE = "board_session"
BOARD_COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days


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


def _auto_place(
    project_id: int, w: float = 220.0, h: float = 260.0
) -> tuple[float, float]:
    """Pick the first grid slot that doesn't overlap any existing card.

    Walks left-to-right, top-to-bottom on a 240x280 grid. Tested against
    the actual bounding boxes of active cards, so cards previously
    dragged out of the grid don't free up slots that they still occupy
    on screen, and soft-deletes don't cause collisions either.
    """
    col_w, row_h, margin = 240.0, 280.0, 20.0
    cols_per_row = 8
    with _db() as c:
        rows = c.execute(
            "SELECT x, y, width, height FROM card "
            "WHERE project_id = ? AND deleted_at IS NULL",
            (project_id,),
        ).fetchall()
    boxes = [
        (r["x"], r["y"], r["x"] + r["width"], r["y"] + r["height"])
        for r in rows
    ]

    def overlaps(x: float, y: float) -> bool:
        x2, y2 = x + w, y + h
        for bx1, by1, bx2, by2 in boxes:
            if not (x2 <= bx1 or x >= bx2 or y2 <= by1 or y >= by2):
                return True
        return False

    for i in range(10000):
        col = i % cols_per_row
        row = i // cols_per_row
        x = margin + col * col_w
        y = margin + row * row_h
        if not overlaps(x, y):
            return x, y
    # Pathological fallback: just drop it below everything.
    max_y = max((b[3] for b in boxes), default=margin)
    return margin, max_y + 20.0


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
    --grid-dot: #2a2b33;
    --lineage: rgba(180,180,200,0.55);
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
  header { position: sticky; top: 0; z-index: 10; padding: 10px 16px;
    background: rgba(26,26,31,0.92); border-bottom: 1px solid var(--panel-border);
    display: flex; align-items: center; gap: 12px; }
  header h1 { font-size: 14px; font-weight: 600; margin: 0; }
  header .meta { font-size: 12px; color: var(--muted); }
  #board { position: relative; width: 100%; min-height: calc(100vh - 44px);
    background-image: radial-gradient(circle, var(--grid-dot) 1px, transparent 1px);
    background-size: 24px 24px; background-position: 0 0; }
  #board-svg { position: absolute; top: 0; left: 0; pointer-events: none;
    overflow: visible; }
  .card { position: absolute; background: var(--panel);
    border: 1px solid var(--panel-border); border-radius: 8px;
    overflow: hidden; display: flex; flex-direction: column;
    box-shadow: 0 1px 3px rgba(0,0,0,0.4);
    cursor: grab; user-select: none;
    transition: box-shadow 120ms ease, transform 120ms ease; }
  .card::before { content: ''; position: absolute; left: 0; top: 0; bottom: 0;
    width: 3px; background: var(--kind-color, transparent);
    border-top-left-radius: 8px; border-bottom-left-radius: 8px; }
  .card:hover { transform: translateY(-1px);
    box-shadow: 0 6px 14px rgba(0,0,0,0.55); }
  .card.dragging { cursor: grabbing; z-index: 50;
    box-shadow: 0 14px 32px rgba(0,0,0,0.65);
    transform: none; transition: none; }
  .card .thumb { flex: 1; background: #111216; display: flex;
    align-items: center; justify-content: center; overflow: hidden; }
  .card .thumb img { width: 100%; height: 100%; object-fit: cover;
    display: block; pointer-events: none; }
  .card .thumb .placeholder { color: var(--muted); font-size: 11px;
    text-transform: uppercase; letter-spacing: 0.08em; }
  .card .meta { padding: 8px 10px; display: flex; align-items: center;
    gap: 6px; border-top: 1px solid var(--panel-border); min-height: 36px; }
  .card .title { flex: 1; font-size: 12px; line-height: 1.3; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; }
  .card .kind { font-size: 9px; padding: 2px 6px; border-radius: 4px;
    text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted);
    background: #1d1e24; border: 1px solid var(--panel-border);
    white-space: nowrap; }
  .card .status { font-size: 10px; padding: 2px 6px; border-radius: 999px;
    text-transform: uppercase; letter-spacing: 0.05em; color: white;
    white-space: nowrap; }
  .status.in_progress { background: var(--in_progress); }
  .status.review { background: var(--review); }
  .status.locked { background: var(--locked); }
  .dl-btn { color: var(--muted); text-decoration: none; font-size: 14px;
    padding: 2px 7px; border-radius: 4px; line-height: 1; cursor: pointer;
    border: 1px solid var(--panel-border); background: #1d1e24; }
  .dl-btn:hover { color: var(--text); background: #2a2b33; }
  .empty { position: absolute; top: 40%; left: 50%;
    transform: translate(-50%, -50%); color: var(--muted);
    font-size: 13px; text-align: center; line-height: 1.6; }
  .empty .small { display: block; font-size: 11px; margin-top: 4px;
    color: #6b6c75; }

  /* Edit modal */
  #modal.hidden { display: none; }
  #modal { position: fixed; inset: 0; z-index: 100;
    display: flex; align-items: center; justify-content: center; }
  .modal-backdrop { position: absolute; inset: 0; background: rgba(0,0,0,0.6); }
  .modal-panel { position: relative; background: var(--panel);
    border: 1px solid var(--panel-border); border-radius: 10px;
    width: 440px; max-width: calc(100vw - 32px); padding: 20px;
    display: flex; flex-direction: column; gap: 14px;
    box-shadow: 0 24px 60px rgba(0,0,0,0.6); }
  .modal-panel h2 { margin: 0; font-size: 13px; font-weight: 600;
    color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; }
  .field { display: flex; flex-direction: column; gap: 4px;
    font-size: 11px; color: var(--muted); text-transform: uppercase;
    letter-spacing: 0.05em; }
  .field input, .field select, .field textarea {
    background: #15161a; color: var(--text);
    border: 1px solid var(--panel-border); border-radius: 6px;
    padding: 8px 10px; font-size: 13px; font-family: inherit;
    text-transform: none; letter-spacing: normal; }
  .field input:focus, .field select:focus, .field textarea:focus {
    outline: none; border-color: var(--review); }
  .field textarea { resize: vertical; min-height: 90px; }
  .modal-actions { display: flex; justify-content: flex-end;
    gap: 8px; margin-top: 4px; }
  .modal-actions button { background: #2a2b33; color: var(--text);
    border: 1px solid var(--panel-border); border-radius: 6px;
    padding: 8px 16px; font-size: 12px; cursor: pointer;
    font-family: inherit; }
  .modal-actions button:hover { background: #34353d; }
  .modal-actions button.primary { background: var(--review);
    border-color: var(--review); }
  .modal-actions button.primary:hover { filter: brightness(1.1); }
  .modal-error { color: #f87171; font-size: 12px; min-height: 16px; }

  /* Lightbox */
  #lightbox.hidden { display: none; }
  #lightbox { position: fixed; inset: 0; z-index: 150;
    display: flex; align-items: center; justify-content: center; }
  .lb-backdrop { position: absolute; inset: 0; background: rgba(0,0,0,0.88);
    cursor: zoom-out; }
  #lb-img { position: relative; max-width: 92vw; max-height: 92vh;
    object-fit: contain; box-shadow: 0 30px 80px rgba(0,0,0,0.8);
    border-radius: 4px; }
  #lb-hint { position: absolute; bottom: 16px; left: 50%;
    transform: translateX(-50%); color: var(--muted); font-size: 11px;
    letter-spacing: 0.06em; text-transform: uppercase; opacity: 0.7;
    pointer-events: none; }
</style>
</head>
<body>
<header>
  <h1>Canvas Board</h1>
  <span class="meta" id="meta">loading…</span>
</header>
<div id="board"><div class="empty" id="empty">loading…</div></div>

<div id="modal" class="hidden">
  <div class="modal-backdrop" data-close="1"></div>
  <div class="modal-panel">
    <h2 id="ed-heading">Edit card</h2>
    <label class="field">Title
      <input id="ed-title" type="text" autocomplete="off">
    </label>
    <label class="field">Status
      <select id="ed-status">
        <option value="in_progress">in progress</option>
        <option value="review">review</option>
        <option value="locked">locked</option>
      </select>
    </label>
    <label class="field">Notes
      <textarea id="ed-notes" rows="5"></textarea>
    </label>
    <div class="modal-error" id="ed-error"></div>
    <div class="modal-actions">
      <button id="ed-cancel" data-close="1">Cancel</button>
      <button id="ed-save" class="primary">Save</button>
    </div>
  </div>
</div>

<div id="lightbox" class="hidden">
  <div class="lb-backdrop" data-close="1"></div>
  <img id="lb-img" alt="">
  <div id="lb-hint">click backdrop or press Esc to close</div>
</div>

<script>
  const board = document.getElementById('board');
  const meta = document.getElementById('meta');
  const modal = document.getElementById('modal');
  const edHeading = document.getElementById('ed-heading');
  const edTitle = document.getElementById('ed-title');
  const edStatus = document.getElementById('ed-status');
  const edNotes = document.getElementById('ed-notes');
  const edError = document.getElementById('ed-error');
  const edSave = document.getElementById('ed-save');
  const lightbox = document.getElementById('lightbox');
  const lbImg = document.getElementById('lb-img');
  const SVG_NS = 'http://www.w3.org/2000/svg';
  const PAD = 40;
  const SECTION_PAD = 18;
  const DRAG_THRESHOLD = 4;
  const KIND_COLORS = {
    reference: '#6b7280',
    generation: '#a855f7',
    anchor: '#06b6d4',
    video: '#ec4899',
    deliverable: '#22c55e',
    note: '#eab308',
  };
  const SECTION_COLORS = [
    '#4f46e5', '#0891b2', '#16a34a',
    '#d97706', '#db2777', '#7c3aed',
  ];
  let dragging = null;
  let editing = null;
  let viewing = null;

  function safeFilename(s) {
    return (s || 'image').replace(/[^a-z0-9_-]+/gi, '_').slice(0, 80) || 'image';
  }

  function svgEl(tag, attrs) {
    const el = document.createElementNS(SVG_NS, tag);
    if (attrs) for (const k in attrs) el.setAttribute(k, attrs[k]);
    return el;
  }

  function sectionColor(name) {
    let h = 0;
    for (let i = 0; i < name.length; i++) {
      h = ((h * 31) + name.charCodeAt(i)) | 0;
    }
    return SECTION_COLORS[Math.abs(h) % SECTION_COLORS.length];
  }

  // Point on `rect`'s perimeter along the line from rect's center toward
  // (fromX, fromY). Used to terminate lineage lines at card edges so the
  // arrowhead is visible just outside the child card.
  function edgePoint(rect, fromX, fromY) {
    const cx = rect.x + rect.w / 2;
    const cy = rect.y + rect.h / 2;
    const dx = fromX - cx;
    const dy = fromY - cy;
    if (dx === 0 && dy === 0) return { x: cx, y: cy };
    const halfW = rect.w / 2;
    const halfH = rect.h / 2;
    const sx = dx !== 0 ? halfW / Math.abs(dx) : Infinity;
    const sy = dy !== 0 ? halfH / Math.abs(dy) : Infinity;
    const s = Math.min(sx, sy);
    return { x: cx + dx * s, y: cy + dy * s };
  }

  function render(cards) {
    board.innerHTML = '';
    if (!cards.length) {
      const e = document.createElement('div');
      e.className = 'empty';
      e.innerHTML = 'no cards yet' +
        '<span class="small">add one with the MCP add_card tool</span>';
      board.appendChild(e);
      return;
    }

    let maxX = 0, maxY = 0;
    for (const c of cards) {
      maxX = Math.max(maxX, c.x + c.width);
      maxY = Math.max(maxY, c.y + c.height);
    }
    const boardW = maxX + PAD;
    const boardH = maxY + PAD;
    board.style.minWidth = boardW + 'px';
    board.style.minHeight = boardH + 'px';

    // SVG overlay sits behind the cards. Sections first (lowest), then
    // lineage lines, then the cards themselves get appended on top.
    const svg = svgEl('svg', {
      id: 'board-svg',
      width: boardW, height: boardH,
      viewBox: '0 0 ' + boardW + ' ' + boardH,
    });

    const defs = svgEl('defs');
    const marker = svgEl('marker', {
      id: 'lin-arrow',
      viewBox: '0 0 10 10', refX: '8', refY: '5',
      markerWidth: '8', markerHeight: '8', orient: 'auto',
    });
    marker.appendChild(svgEl('path', {
      d: 'M0,0 L10,5 L0,10 z', fill: 'rgba(180,180,200,0.7)',
    }));
    defs.appendChild(marker);
    svg.appendChild(defs);

    // Sections: group active cards by non-empty section, draw a soft
    // tinted rounded rect that just visualizes where those cards happen
    // to be — it doesn't constrain anything.
    const sectionGroup = svgEl('g', { class: 'sections' });
    const bySection = new Map();
    for (const c of cards) {
      if (!c.section) continue;
      if (!bySection.has(c.section)) bySection.set(c.section, []);
      bySection.get(c.section).push(c);
    }
    for (const [name, members] of bySection) {
      let nx = Infinity, ny = Infinity, fx = 0, fy = 0;
      for (const c of members) {
        nx = Math.min(nx, c.x);
        ny = Math.min(ny, c.y);
        fx = Math.max(fx, c.x + c.width);
        fy = Math.max(fy, c.y + c.height);
      }
      const x = nx - SECTION_PAD;
      const y = ny - SECTION_PAD;
      const w = (fx - nx) + SECTION_PAD * 2;
      const h = (fy - ny) + SECTION_PAD * 2;
      const color = sectionColor(name);
      sectionGroup.appendChild(svgEl('rect', {
        x: x, y: y, width: w, height: h, rx: '12', ry: '12',
        fill: color, 'fill-opacity': '0.10',
        stroke: color, 'stroke-opacity': '0.35', 'stroke-width': '1',
      }));
      const label = svgEl('text', {
        x: x + 12, y: y + 18,
        fill: color, 'fill-opacity': '0.85',
        'font-size': '10', 'font-family': '-apple-system, sans-serif',
        'font-weight': '600', 'letter-spacing': '1.2',
      });
      label.textContent = name.toUpperCase();
      sectionGroup.appendChild(label);
    }
    svg.appendChild(sectionGroup);

    // Lineage lines: parent → child arrows, terminated at each card's
    // edge so the arrowhead lives in the gap between them.
    const cardById = new Map(cards.map((c) => [c.id, c]));
    const lineGroup = svgEl('g', { class: 'lines' });
    for (const child of cards) {
      if (!child.parent_card_id) continue;
      const parent = cardById.get(child.parent_card_id);
      if (!parent || parent.id === child.id) continue;
      const pRect = { x: parent.x, y: parent.y, w: parent.width, h: parent.height };
      const cRect = { x: child.x,  y: child.y,  w: child.width,  h: child.height };
      const pCx = pRect.x + pRect.w / 2, pCy = pRect.y + pRect.h / 2;
      const cCx = cRect.x + cRect.w / 2, cCy = cRect.y + cRect.h / 2;
      const start = edgePoint(pRect, cCx, cCy);
      const end = edgePoint(cRect, pCx, pCy);
      lineGroup.appendChild(svgEl('line', {
        x1: start.x, y1: start.y, x2: end.x, y2: end.y,
        stroke: 'var(--lineage)', 'stroke-width': '1.5',
        'stroke-linecap': 'round', 'marker-end': 'url(#lin-arrow)',
      }));
    }
    svg.appendChild(lineGroup);
    board.appendChild(svg);

    // Cards on top.
    for (const c of cards) {
      const el = document.createElement('div');
      el.className = 'card';
      el.dataset.cardId = c.id;
      el.style.left = c.x + 'px';
      el.style.top = c.y + 'px';
      el.style.width = c.width + 'px';
      el.style.height = c.height + 'px';
      el.style.setProperty('--kind-color',
        KIND_COLORS[c.kind] || 'transparent');

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
      const k = document.createElement('div');
      k.className = 'kind';
      k.textContent = c.kind;
      const s = document.createElement('div');
      s.className = 'status ' + c.status;
      s.textContent = c.status.replace('_', ' ');
      m.appendChild(t);
      m.appendChild(k);
      m.appendChild(s);

      if (c.full_image_url) {
        const dl = document.createElement('a');
        dl.className = 'dl-btn';
        dl.href = c.full_image_url;
        dl.download = safeFilename(c.title || ('card_' + c.id)) + '.jpg';
        dl.title = 'Download full image';
        dl.textContent = '↓';
        dl.addEventListener('mousedown', (e) => e.stopPropagation());
        dl.addEventListener('click', (e) => e.stopPropagation());
        m.appendChild(dl);
      }
      el.appendChild(m);

      el.addEventListener('mousedown', (e) => startDrag(e, c, el));
      board.appendChild(el);
    }
  }

  function startDrag(e, card, el) {
    if (e.button !== 0) return;
    dragging = {
      card, el,
      startX: card.x, startY: card.y,
      mouseStartX: e.clientX, mouseStartY: e.clientY,
      moved: false,
      target: e.target,
    };
    e.preventDefault();
  }

  document.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    const dx = e.clientX - dragging.mouseStartX;
    const dy = e.clientY - dragging.mouseStartY;
    if (!dragging.moved && Math.hypot(dx, dy) > DRAG_THRESHOLD) {
      dragging.moved = true;
      dragging.el.classList.add('dragging');
    }
    if (dragging.moved) {
      const nx = Math.max(0, dragging.startX + dx);
      const ny = Math.max(0, dragging.startY + dy);
      dragging.el.style.left = nx + 'px';
      dragging.el.style.top = ny + 'px';
      dragging.card.x = nx;
      dragging.card.y = ny;
    }
  });

  document.addEventListener('mouseup', async (e) => {
    if (!dragging) return;
    const d = dragging;
    dragging = null;
    d.el.classList.remove('dragging');
    if (d.moved) {
      try {
        await fetch('/api/cards/' + d.card.id, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ x: d.card.x, y: d.card.y }),
        });
      } catch (err) {
        meta.textContent = 'save failed: ' + err.message;
      }
      refresh();
    } else {
      if (d.target.closest('.thumb') && d.card.full_image_url) {
        openLightbox(d.card.full_image_url, d.card.title);
      } else {
        openEditor(d.card);
      }
    }
  });

  function openEditor(card) {
    editing = { id: card.id };
    edHeading.textContent = 'Edit card · ' + (card.title || ('#' + card.id));
    edTitle.value = card.title || '';
    edStatus.value = card.status;
    edNotes.value = card.notes || '';
    edError.textContent = '';
    modal.classList.remove('hidden');
    setTimeout(() => { edTitle.focus(); edTitle.select(); }, 0);
  }

  function closeEditor() {
    editing = null;
    modal.classList.add('hidden');
    refresh();
  }

  function openLightbox(url, title) {
    viewing = url;
    lbImg.src = url;
    lbImg.alt = title || '';
    lightbox.classList.remove('hidden');
  }

  function closeLightbox() {
    viewing = null;
    lightbox.classList.add('hidden');
    lbImg.removeAttribute('src');
    refresh();
  }

  modal.addEventListener('click', (e) => {
    if (e.target.dataset && e.target.dataset.close) closeEditor();
  });
  lightbox.addEventListener('click', (e) => {
    if (e.target.dataset && e.target.dataset.close) closeLightbox();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (viewing) closeLightbox();
    else if (editing) closeEditor();
  });

  edSave.addEventListener('click', async () => {
    if (!editing) return;
    edSave.disabled = true;
    edError.textContent = '';
    try {
      const r = await fetch('/api/cards/' + editing.id, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          title: edTitle.value,
          status: edStatus.value,
          notes: edNotes.value,
        }),
      });
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        throw new Error(j.error || ('HTTP ' + r.status));
      }
      closeEditor();
    } catch (err) {
      edError.textContent = 'save failed: ' + err.message;
    } finally {
      edSave.disabled = false;
    }
  });

  async function refresh() {
    if (dragging || editing || viewing) return;
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


_UNAUTHORIZED_HTML = (
    "<!doctype html><meta charset=utf-8>"
    "<title>Canvas Board</title>"
    "<style>body{background:#1a1a1f;color:#e8e8ea;"
    "font-family:-apple-system,sans-serif;"
    "display:flex;align-items:center;justify-content:center;"
    "min-height:100vh;margin:0;text-align:center;line-height:1.6}"
    "code{background:#24252c;padding:2px 6px;border-radius:4px;"
    "color:#9a9ba3}</style>"
    "<div><h1 style='font-size:16px;margin:0 0 8px'>Missing or invalid key</h1>"
    "<p style='color:#9a9ba3;font-size:13px;margin:0'>"
    "Append <code>?key=YOUR_KEY</code> to the URL.</p></div>"
)


def _board_auth_ok(request: Request) -> bool:
    """True if the request carries a valid BOARD_KEY in either the
    ?key= query param or the board_session cookie."""
    candidate = request.query_params.get("key") or request.cookies.get(BOARD_COOKIE)
    if not candidate:
        return False
    return hmac.compare_digest(candidate, BOARD_KEY)


def _api_unauthorized() -> JSONResponse:
    return JSONResponse({"error": "unauthorized"}, status_code=401)


@mcp.custom_route("/board", methods=["GET"])
async def board_page(request: Request) -> HTMLResponse:
    if not _board_auth_ok(request):
        return HTMLResponse(_UNAUTHORIZED_HTML, status_code=401)
    resp = HTMLResponse(BOARD_HTML)
    # Stash the key in a cookie so same-origin fetches, image loads, and
    # download links work without re-passing ?key= everywhere.
    resp.set_cookie(
        BOARD_COOKIE,
        BOARD_KEY,
        max_age=BOARD_COOKIE_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    # Don't leak the ?key= via Referer if the user clicks an external link.
    resp.headers["Referrer-Policy"] = "same-origin"
    return resp


@mcp.custom_route("/api/cards", methods=["GET"])
async def api_cards(request: Request) -> JSONResponse:
    if not _board_auth_ok(request):
        return _api_unauthorized()
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


_PATCHABLE = ("title", "status", "notes", "x", "y")


@mcp.custom_route("/api/cards/{card_id:int}", methods=["PATCH"])
async def api_patch_card(request: Request) -> JSONResponse:
    if not _board_auth_ok(request):
        return _api_unauthorized()
    card_id = request.path_params["card_id"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
    fields = {k: body[k] for k in _PATCHABLE if k in body}
    if "status" in fields and fields["status"] not in ALLOWED_STATUSES:
        return JSONResponse(
            {"error": f"status must be one of {sorted(ALLOWED_STATUSES)}"},
            status_code=400,
        )
    if not fields:
        return JSONResponse({"error": "no fields to update"}, status_code=400)
    sets = ", ".join(f"{k} = ?" for k in fields) + ", updated_at = ?"
    params = list(fields.values()) + [_now(), card_id]
    with _db() as c:
        c.execute(
            f"UPDATE card SET {sets} WHERE id = ? AND deleted_at IS NULL",
            params,
        )
        row = c.execute(
            "SELECT * FROM card WHERE id = ? AND deleted_at IS NULL",
            (card_id,),
        ).fetchone()
    if not row:
        return JSONResponse({"error": "card not found"}, status_code=404)
    return JSONResponse(_card_to_dict(row))


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
    if not _board_auth_ok(request):
        return _api_unauthorized()
    return _serve_from(THUMBS_DIR, request.path_params["filename"])


@mcp.custom_route("/files/{filename}", methods=["GET"])
async def serve_file(request: Request):
    if not _board_auth_ok(request):
        return _api_unauthorized()
    return _serve_from(FILES_DIR, request.path_params["filename"])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    # MCP_TOKEN is embedded in the path itself: anyone hitting plain
    # /mcp gets a 404 from FastMCP. The token is shared only with the
    # claude.ai connector config.
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=port,
        path=f"/mcp/{MCP_TOKEN}",
    )
