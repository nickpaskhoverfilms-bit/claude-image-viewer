# claude-image-viewer

A small FastMCP server that does two things:

1. Gives Claude **image-viewing tools** — fetch an image URL, shrink it, and
   hand it back as a visible/comparable image so Claude can describe and
   reason about it.
2. Hosts a **visual canvas** for creative work — a SQLite-backed board of
   cards (references, generations, anchors, video frames, deliverables,
   notes) that Claude manipulates via MCP tools and the user views and
   edits in a browser at `/board`.

The whole server is a single Python file (`server.py`) running on Railway.

## Deployment

- **Hosting:** Railway, single web process. `Procfile` declares `web:
  python server.py`.
- **Python deps:** `fastmcp`, `httpx`, `pillow` (`requirements.txt`).
  Requires Python 3.10+ (FastMCP requirement).
- **System deps:** `ffmpeg` (provides both `ffmpeg` and `ffprobe`),
  installed by `nixpacks.toml` (`aptPkgs = ['ffmpeg']`). Used by the
  video tools to probe duration and extract frames.
- **Persistent state:** Railway mounts a volume at `/data`. `server.py`
  uses `/data` if it exists and is writable, otherwise falls back to
  `./data` so the same code works locally. The SQLite database, full-
  resolution image files, and thumbnails all live there.
- **Port:** read from `$PORT` (Railway sets it), defaults to 8080 locally.

## File layout (runtime, inside the data dir)

```
canvas.db          # SQLite database
files/{id}.jpg     # full-resolution image for card {id}
thumbs/{id}.jpg    # 400px thumbnail for card {id}
```

The directory `data/` is in `.gitignore`.

---

## The MCP server

FastMCP server named `"Image Viewer"`. HTTP transport, served at
`/mcp/<MCP_TOKEN>` (see Security). Exposes eight tools, in three groups.

### Image-viewing tools

These are the original purpose of the server. They download an image
from an arbitrary URL, downscale it with Pillow, and return it as an
MCP `Image` payload that Claude renders inline.

- **`view_asset(url: str) -> Image`** — downloads `url`, downscales to
  max edge 1568px, returns a single JPEG image. Used to "look at"
  any image on the open web.
- **`compare_assets(urls: list[str]) -> list[Image]`** — same idea but
  for up to 4 URLs, downscaled to 1024px each, returned as a list so
  Claude can compare them side by side.

Shrinking is done in-process by the `shrink(url, max_edge)` helper
(`Pillow.thumbnail` + JPEG re-encode at quality 85).

### Video tools

Two tools that stream a video to a temp file, probe it with `ffprobe`,
and extract frames with `ffmpeg` (fast-seek `-ss` before `-i`,
re-encoded to MJPEG and piped to stdout). Both reject anything larger
than `MAX_VIDEO_BYTES` (500 MB) — enforced both by the declared
`Content-Length` and by a streamed byte cap during download. Only
direct video URLs (mp4, mov, webm, etc.) are supported; HLS/DASH
manifests are not. The temp file is always unlinked in a `finally`.

- **`get_video_overview(video_url: str) -> Image`** — returns a single
  4×3 contact-sheet JPEG with `OVERVIEW_FRAMES` (12) frames sampled
  evenly across the clip, each labeled with its timestamp in the
  bottom-left of its tile. Sampling skips a small head/tail margin
  (`min(0.5s, 5% of duration)`) so it doesn't lock onto title/end
  cards. Tile size is derived from the first frame's aspect ratio
  and capped so both sheet dimensions stay within
  `OVERVIEW_LONG_EDGE` (1280 px). Labels are drawn on a translucent
  overlay that is alpha-composited over the sheet, so they stay
  readable on any frame content.
- **`extract_frames(video_url, start_time, end_time) -> list[Image]`**
  — returns up to `EXTRACT_MAX_FRAMES` (8) frames sampled evenly
  across `[start_time, end_time]`, endpoints inclusive. For windows
  ≤ 8 s the count is `round(window_seconds)`; for longer windows the
  full 8 frames are used. Each frame is downscaled so its long edge
  is ≤ `EXTRACT_LONG_EDGE` (1568 px) and gets the same timestamp pill
  in the bottom-left corner (`_draw_timestamp_label`). Time arguments
  accept either seconds (`"42"`, `"42.5"`) or colon strings (`"1:23"`
  = m:ss, `"0:01:23"` = h:mm:ss), parsed by `_parse_time`. `end_time`
  is clamped to the video's duration; `start_time` past the end or
  not strictly before `end_time` raises `ValueError`.

Timestamp labels use DejaVu Sans Bold from the Nixpacks-supplied
Debian font path, falling back to PIL's default font if missing
(`_load_label_font`).

### Canvas tools

Four tools that read and write the SQLite canvas.

- **`get_canvas_state(project_id=1) -> str`** — returns every active
  (non-soft-deleted) card for the project as compact JSON. Call this at
  the start of a session to orient on what's already on the board.
- **`add_card(kind, title, source_url="", prompt="", source_job_id="",
  parent_card_id=None, section="", x=None, y=None, notes="",
  project_id=1) -> str`** — inserts a card. `kind` must be one of the
  ALLOWED_KINDS (see below). If `source_url` is given, the server
  downloads it via httpx, saves both a full JPEG and a 400px thumbnail
  under `files/{id}.jpg` / `thumbs/{id}.jpg`, and writes the served
  paths (`/files/{id}.jpg`, `/thumbs/{id}.jpg`) into `thumbnail_url`
  and `full_image_url`. If `x`/`y` are omitted, the card is auto-placed
  on the next empty grid slot (see Auto-placement).
- **`update_card(card_id, title=None, status=None, prompt=None,
  notes=None, section=None, parent_card_id=None, x=None, y=None) -> str`**
  — partial update. Any argument left at `None` is not changed.
  `status` validated against ALLOWED_STATUSES.
- **`delete_card(card_id) -> str`** — soft delete. Sets `deleted_at`;
  the row is kept but hidden from `get_canvas_state` and the board.

All four return JSON strings.

### Allowed enum values

```python
ALLOWED_KINDS    = {"reference", "generation", "anchor",
                    "video", "deliverable", "note"}
ALLOWED_STATUSES = {"in_progress", "review", "locked"}
```

The web UI uses these names directly (kind chip and status pill).

---

## The canvas (database)

SQLite at `<data dir>/canvas.db`. Single connection per request via the
`_db()` context manager (`foreign_keys=ON`).

### Schema

```sql
CREATE TABLE project (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE card (
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
CREATE INDEX idx_card_project_active ON card(project_id, deleted_at);
```

Timestamps are ISO-8601 UTC strings (`datetime.isoformat(timespec="seconds")`).
Soft delete = `deleted_at IS NOT NULL`; "active" everywhere means
`deleted_at IS NULL`.

A single default project (id=1, name "Default Project") is inserted on
first boot. The schema supports multi-project but nothing in the UI or
tools exposes it yet — everything defaults to `project_id=1`.

### Auto-placement (`_auto_place`)

When `add_card` is called without explicit `x,y`, the server walks a
240×280 grid (20px outer margin, 8 columns) left-to-right then top-to-
bottom, and returns the first slot whose 220×260 bounding box does not
intersect any active card's bounding box. This is the only correct
behavior given that cards can be dragged anywhere and soft-deletes
free up slots in arbitrary order — a count-based formula would
collide.

If 10,000 slots are somehow all full, the fallback drops the card
below everything (`y = max_existing_y + 20`).

---

## The board (`/board`)

A plain HTML/CSS/JS page (no React, no build step) inlined in
`server.py` as the `BOARD_HTML` constant and served by the `/board`
route. The page polls `/api/cards` every 3 seconds and re-renders.

### Layout & rendering

The page is a fixed-size viewport (`#board`, `overflow: hidden`) that
contains a single transformable child (`#stage`, `transform-origin:
0 0`). All cards, the SVG overlay, and the dotted-grid background
live inside `#stage`. Zoom and pan are implemented by writing
`transform: translate(panX, panY) scale(zoom)` to `#stage`; that one
operation moves and scales everything together.

The edit modal and the lightbox are `position: fixed` at the
document root and are unaffected by zoom.

Inside `#stage`:

- Each card is an absolutely-positioned `<div>` at its saved `x,y`
  with its saved `width,height`. The stage's width/height are sized
  to the furthest card so the background grid tiles correctly.
- A single SVG overlay (`pointer-events: none`) is rebuilt on every
  render and holds:
  1. **Section backgrounds** — cards are grouped by their `section`
     field (empty string ignored). Each group gets a rounded rect
     padded 18px around the group's AABB, with low-opacity fill +
     stroke in a color picked by hashing the section name. A small
     uppercase label sits at top-left.
  2. **Lineage lines** — for any card whose `parent_card_id` matches
     another card on the current board, a thin arrow is drawn between
     them. Endpoints are clipped to each card's rectangle edge (via a
     center-to-center / perimeter intersection) so the arrowhead
     lives in the gap between the two cards. Self-loops and missing
     parents are skipped silently.
- Cards are appended *after* the SVG so they naturally render above
  it.

### Card visuals

- Background: dark panel.
- A 3px colored stripe on the left edge, colored by `kind` via a
  `--kind-color` CSS variable and a `::before` pseudo-element. Kind
  palette: reference=gray, generation=purple, anchor=cyan, video=pink,
  deliverable=green, note=yellow.
- A thumbnail area (top) that either shows the card's `thumbnail_url`
  image or, if absent, a placeholder showing the kind name.
- A meta row (bottom): title (truncated), kind chip, status pill
  (orange/blue/green for in_progress/review/locked), and — if the
  card has a `full_image_url` — a "↓" download button.
- Subtle hover lift (1px translate + bigger shadow), suppressed
  during drag and while Space is held (pan mode).
- Faint dotted-grid background on the stage (24px radial gradient).
  Because the grid lives on `#stage` rather than `#board`, the dots
  scale with zoom — content stays visually anchored to the grid at
  every zoom level.

### Zoom and pan

View state is three module-scope JS vars (`zoom`, `panX`, `panY`,
initialized to 1 / 0 / 0). A small `applyTransform()` helper writes
the CSS transform and updates the header's zoom label. The render
function clears and rebuilds `#stage`'s children but never touches
those vars, so the 3-second poll preserves the user's view exactly.

**Range:** zoom is clamped to 10%–400%.

**Toolbar (header, right side):**
- `−` zoom out by 1.2× around the viewport center.
- Current zoom % — click to reset to 100% at origin.
- `+` zoom in by 1.2× around the viewport center.
- `Fit` — compute the AABB of all cards, pick the largest scale that
  fits with 40px padding (capped at 100%), and center it.

**Wheel:**
- `Ctrl/Cmd + wheel` (which also catches trackpad pinch, since pinch
  fires `WheelEvent` with `ctrlKey: true`) zooms around the cursor
  position. The math: compute the stage-space point currently under
  the cursor, change `zoom`, then adjust `panX`/`panY` so that same
  stage point is still under the cursor.
- Plain wheel / two-finger swipe pans by `(deltaX, deltaY)`.

Both branches call `preventDefault()` so the page doesn't scroll.

**Pan input:**
- **Drag the empty background.** Mousedown on `#board` whose target
  isn't a card starts a pan. Card mousedowns call
  `e.stopPropagation()` to prevent the board's pan handler from also
  firing.
- **Hold Space and drag anywhere.** While Space is held the body
  gets a `space-held` class (cursor switches to `grab`, hover lift
  is suppressed) and any mousedown — including over a card — starts
  a pan instead of a card drag.

**Keyboard:** `+` / `−` zoom around viewport center, `0` resets to
100% at origin, `1` fits the board. Bindings are suppressed while
typing in an `INPUT`/`TEXTAREA`/`SELECT`.

**First-load behavior:** the first poll that returns cards checks
whether the content fits inside the viewport at 100%. If yes, the
view stays at 100% / origin. If no, `fitToScreen()` runs once. After
that first paint the flag `didInitialFit` is set and subsequent
renders never auto-fit again, so the user's zoom and pan are
preserved.

**Card drag at zoom != 1:** the mouse delta is in screen pixels but
card `x,y` is in stage pixels, so the drag handler divides the
delta by `zoom` before adding it to the start position. This keeps
the card under the cursor at every zoom level.

### Interactions

- **Drag to reposition.** Mousedown on a card starts tracking. After
  the pointer crosses a 4px threshold, the card follows the cursor
  and gets a `dragging` class (raised z-index, bigger shadow). On
  mouseup, the new `x,y` is sent via `PATCH /api/cards/{id}`.
- **Click to edit.** A release that did not cross the drag threshold
  opens a modal with Title (input) / Status (dropdown) / Notes
  (textarea). Save sends all three via `PATCH /api/cards/{id}`. Esc
  or backdrop click cancels.
- **Click thumbnail to view full image.** A non-drag click on the
  thumbnail opens an in-page lightbox (dimmed backdrop, image fit to
  92vw/92vh). Backdrop click or Esc closes it. The board stays mounted
  behind it.
- **Download.** The "↓" anchor uses the HTML5 `download` attribute
  with a sanitized filename `<title>.jpg`. `mousedown` / `click` are
  `stopPropagation`'d so the button doesn't trigger drag or the
  editor.

Four flags — `dragging`, `editing`, `viewing`, `panning` — pause the
polling refresh while the user is in the middle of any of those
interactions, so the DOM is never rebuilt out from under them.

### HTTP API (used by the board)

All gated by board auth (see Security).

- **`GET /board`** — the HTML page itself. Also sets the board cookie.
- **`GET /api/cards?project_id=1`** — returns all active cards for the
  project as JSON: `{"project_id": int, "cards": [card_dict, ...]}`.
  Same payload shape as `get_canvas_state`.
- **`PATCH /api/cards/{id}`** — JSON body, partial update. Accepts any
  subset of `title`, `status`, `notes`, `x`, `y`. Unknown fields are
  ignored. Returns the updated row as JSON. 404 if the card was
  deleted.
- **`GET /thumbs/{filename}`** and **`GET /files/{filename}`** — stream
  the saved JPEG. Path-traversal guard: the resolved path must be
  inside the directory.

---

## Security model (Checkpoint 5)

The deployed instance is single-tenant (one user). Security is two
shared secrets, both read from environment variables at startup. If
either is missing the server raises `RuntimeError` and exits — this
prevents an accidental deploy without auth.

### Environment variables

- **`BOARD_KEY`** — gates the web UI (everything under `/board`,
  `/api/*`, `/thumbs/*`, `/files/*`).
- **`MCP_TOKEN`** — gates the MCP transport.

Both are required. Generate with `openssl rand -hex 32` or any
long random string.

### Web UI auth

A request to a protected web route is authorized if **either**:

1. `?key=<value>` matches `BOARD_KEY`, or
2. The `board_session` cookie matches `BOARD_KEY`.

Compared with `hmac.compare_digest` (constant-time).

`GET /board` is the only route that *sets* the cookie. On a successful
`?key=` visit it issues `board_session=<BOARD_KEY>` with attributes
`HttpOnly`, `Secure`, `SameSite=Lax`, `Max-Age=30 days`, `Path=/`.
After that first visit, the page's own `fetch` calls to `/api/cards`,
the `<img src="/thumbs/...">` requests, and the download anchors all
carry the cookie automatically, no per-request key needed.

The `/board` response also sets `Referrer-Policy: same-origin` so the
`?key=` in the URL bar will not leak via `Referer` if the user clicks
an external link.

Unauthorized response:
- `/board` → 401 with a tiny HTML page reading "Missing or invalid key".
- API and static routes → 401 with `{"error": "unauthorized"}`.

Bookmark URL: `https://<host>/board?key=<BOARD_KEY>`. The `?key=` stays
in the URL so the bookmark always works even if the cookie expires.

### MCP auth

The MCP HTTP transport is mounted at `/mcp/<MCP_TOKEN>` instead of
`/mcp`. There is no separate auth provider — the URL itself is the
credential. Anyone hitting `/mcp` without the token gets the FastMCP
default 404.

The `claude.ai` connector is configured with that full path. Rotating
`MCP_TOKEN` requires updating the connector URL.

### What this does NOT cover

- No rate limiting.
- No per-user accounts / no audit log.
- No CSRF defense beyond `SameSite=Lax` (acceptable for a single-user
  internal tool).
- No rotation UI — rotate by editing the env vars in Railway and
  redeploying.

---

## Development notes

- **Running locally:** create a venv on Python 3.10+, `pip install -r
  requirements.txt`, then `BOARD_KEY=dev MCP_TOKEN=dev python
  server.py`. Board served at `http://localhost:8080/board?key=dev`.
- **Editing the board UI:** the HTML, CSS, and JS all live in the
  `BOARD_HTML` string in `server.py`. There is no build step. Reload
  the page to pick up changes.
- **Schema changes:** `_init_db()` uses `CREATE TABLE IF NOT EXISTS`,
  so adding a column requires either an explicit `ALTER TABLE` or
  resetting `canvas.db`. There is no migration framework.
- **Adding routes:** use `@mcp.custom_route(path, methods=[...])`.
  Remember to add `_board_auth_ok(request)` to any new web route
  unless it is intentionally public.
