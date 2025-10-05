# app.py — Mini PDF Editor (Flask + PyMuPDF)
# New: Select/Move/Resize/Edit for annotations (signature, textbox, shapes, highlight/strike, line/arrow)
# - Click any existing annotation to select
# - Drag inside to move, drag handles to resize (line/arrow: drag endpoints)
# - Double-click selected textbox to edit text
# - Delete key removes selected item
# - Undo/Redo now snapshot the whole stack (supports edits as well as creates)
#
# Prior fixes kept: JSON errors, safe fallbacks, size clamps, signature UX, zoom, thumbnails, rollback.
#
# requirements.txt (add gunicorn for Render/Heroku):
# Flask==3.0.3
# python-dotenv==1.0.1
# PyMuPDF==1.24.6
# gunicorn==22.0.0
# boto3==1.34.162  # only if USE_S3=true

import io, os, uuid, base64, traceback
from datetime import datetime
from pathlib import Path
from flask import Flask, request, jsonify, send_file, render_template_string
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
import fitz  # PyMuPDF

load_dotenv()
app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET", "dev-secret")
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100 MB

BASE_DIR = Path(__file__).resolve().parent
WORK_DIR = Path(os.getenv("WORK_DIR", BASE_DIR / "work"))
WORK_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_EXT = {".pdf"}

USE_S3 = os.getenv("USE_S3", "false").lower() == "true"
if USE_S3:
    import boto3
    S3_BUCKET = os.getenv("S3_BUCKET")
    S3_REGION = os.getenv("S3_REGION")
    s3 = boto3.client("s3", region_name=S3_REGION)
else:
    s3, S3_BUCKET = None, None

DOCS = {}  # {doc_id: {name, original, working, versions[], created}}

# ───────── Storage ─────────
class Storage:
    @staticmethod
    def save(file_bytes: bytes, key: str) -> str:
        if USE_S3:
            s3.put_object(Bucket=S3_BUCKET, Key=key, Body=file_bytes, ContentType="application/pdf")
            return key
        p = WORK_DIR / key
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(file_bytes)
        return key

    @staticmethod
    def get(key: str) -> bytes:
        if USE_S3:
            obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
            return obj["Body"].read()
        p = Path(key)
        if not p.is_absolute():
            p = WORK_DIR / key
        return p.read_bytes()

# ───────── Helpers ─────────
def _allowed(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_EXT

def _color_tuple(rgb_list):
    if not rgb_list:
        return (1.0, 1.0, 0.0)
    r, g, b = [max(0, min(255, int(c))) / 255.0 for c in rgb_list[:3]]
    return (r, g, b)

def _scale_rect(rect, page_rect, viewport):
    vx, vy = max(1, int(viewport.get("w", 1))), max(1, int(viewport.get("h", 1)))
    sx, sy = page_rect.width / vx, page_rect.height / vy
    x0, y0, x1, y1 = rect
    return fitz.Rect(x0 * sx, y0 * sy, x1 * sx, y1 * sy)

def _scale_point(pt, page_rect, viewport):
    vx, vy = max(1, int(viewport.get("w", 1))), max(1, int(viewport.get("h", 1)))
    sx, sy = page_rect.width / vx, page_rect.height / vy
    return fitz.Point(pt[0] * sx, pt[1] * sy)

def _decode_data_url(data_url: str) -> bytes:
    if not data_url or "," not in data_url:
        return b""
    _, b64 = data_url.split(",", 1)
    return base64.b64decode(b64)

def _clip_rect(r: fitz.Rect, page_rect: fitz.Rect) -> fitz.Rect:
    x0 = max(page_rect.x0, min(page_rect.x1, r.x0))
    y0 = max(page_rect.y0, min(page_rect.y1, r.y0))
    x1 = max(page_rect.x0, min(page_rect.x1, r.x1))
    y1 = max(page_rect.y0, min(page_rect.y1, r.y1))
    return fitz.Rect(x0, y0, x1, y1)

def _ensure_min_rect(r: fitz.Rect, page_rect: fitz.Rect, min_w=2.0, min_h=2.0) -> fitz.Rect:
    cx, cy = (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2
    if r.width < min_w:
        r.x0, r.x1 = cx - min_w / 2, cx + min_w / 2
    if r.height < min_h:
        r.y0, r.y1 = cy - min_h / 2, cy + min_h / 2
    r = _clip_rect(r, page_rect)
    if r.width <= 0 or r.height <= 0:
        r = fitz.Rect(cx - min_w / 2, cy - min_h / 2, cx + min_w / 2, cy + min_h / 2)
        r = _clip_rect(r, page_rect)
    return r

@app.after_request
def add_no_cache(resp):
    if request.path.startswith(("/thumb/", "/page/")):
        resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp

# ───────── UI (inline) ─────────
INDEX_HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Mini PDF Editor</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet" />
  <style>
    body { background:#0b1020; color:#e7ecff; }
    .toolbar .btn { border-radius:999px; }
    #thumbs { max-height:80vh; overflow:auto; }
    #canvasWrap { position:relative; background:#101425; padding:0; min-height:60vh; }
    #pageImg { display:block; max-width:100%; height:auto; position:relative; z-index:1; }
    #overlay { position:absolute; left:0; top:0; pointer-events:auto; z-index:2; }
    .tool-active { outline:2px solid #6ea8fe; }
    .kbd { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; background:#11162a; border:1px solid #2a355d; border-radius:6px; padding:1px 6px; }
    .card.bg-dark { background:#0f1428 !important; }
  </style>
</head>
<body>
<div class="container-fluid py-3">
  <div class="d-flex align-items-center gap-3 mb-3">
    <h3 class="m-0">Mini PDF Editor</h3>
    <input id="file" type="file" class="form-control w-auto" accept="application/pdf" />
    <button id="btnDownload" class="btn btn-success">Download</button>
    <button id="btnUndoServer" class="btn btn-outline-warning">Rollback (server)</button>
  </div>

  <div class="row g-3">
    <div class="col-3">
      <div class="card bg-dark border-secondary">
        <div class="card-header">Pages</div>
        <div class="card-body" id="thumbs"></div>
      </div>
      <div class="small mt-3">
        Shortcuts:
        <span class="kbd">H</span> Highlight,
        <span class="kbd">S</span> Strike,
        <span class="kbd">R</span> Rect,
        <span class="kbd">C</span> Circle,
        <span class="kbd">L</span> Line,
        <span class="kbd">A</span> Arrow,
        <span class="kbd">I</span> Ink,
        <span class="kbd">T</span> Text,
        <span class="kbd">G</span> Signature
        — Click annotations to move/resize. ⌫/Del = delete. Double-click a textbox to edit.
      </div>
    </div>

    <div class="col-9">
      <div class="card bg-dark border-secondary mb-3">
        <div class="card-body toolbar d-flex flex-wrap gap-2 align-items-center">
          <button class="btn btn-light" data-tool="highlight">Highlight</button>
          <button class="btn btn-light" data-tool="strikeout">Strikeout</button>
          <button class="btn btn-light" data-tool="rect">Rect</button>
          <button class="btn btn-light" data-tool="circle">Circle</button>
          <button class="btn btn-light" data-tool="line">Line</button>
          <button class="btn btn-light" data-tool="arrow">Arrow</button>
          <button class="btn btn-light" data-tool="ink">Freehand</button>
          <button class="btn btn-light" data-tool="textbox">Text Box</button>
          <button class="btn btn-warning" data-tool="signature" id="btnSignature" data-bs-toggle="modal" data-bs-target="#signatureModal">Signature</button>

          <div class="vr"></div>
          <label class="text-nowrap">Color</label>
          <input id="color" type="color" value="#ffeb3b" class="form-control form-control-color" />
          <label class="text-nowrap ms-2">Thickness</label>
          <input id="thickness" type="range" min="1" max="12" value="2" class="form-range w-25" />
          <div class="vr"></div>
          <button class="btn btn-primary" id="btnSave">Save Edits</button>
          <button class="btn btn-outline-light" id="btnUndo">Undo</button>
          <button class="btn btn-outline-light" id="btnRedo">Redo</button>
          <div class="vr"></div>
          <label class="text-nowrap">Zoom</label>
          <input id="zoom" type="range" min="0.6" max="2.5" step="0.1" value="1.2" class="form-range w-25" />
        </div>
      </div>

      <div id="canvasWrap" class="rounded-3 shadow">
        <img id="pageImg" src="" />
        <canvas id="overlay"></canvas>
      </div>
    </div>
  </div>
</div>

<!-- Signature Modal -->
<div class="modal fade" id="signatureModal" tabindex="-1" aria-hidden="true">
  <div class="modal-dialog modal-dialog-centered">
    <div class="modal-content bg-dark text-light border-secondary">
      <div class="modal-header">
        <h5 class="modal-title">Draw Your Signature</h5>
        <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="Close"></button>
      </div>
      <div class="modal-body">
        <canvas id="sigPad" width="500" height="180" style="background:#fff;border:1px dashed #888;touch-action:none;"></canvas>
        <div class="d-flex gap-2 mt-2">
          <button id="sigClear" class="btn btn-outline-light">Clear</button>
          <button id="sigUse" class="btn btn-success" data-bs-dismiss="modal">Use Signature</button>
          <div class="ms-auto small text-secondary" id="sigStatus">Not set</div>
        </div>
        <div class="small text-secondary mt-2">After “Use Signature”, drag on the page to place it.</div>
      </div>
    </div>
  </div>
</div>

<script>
(() => {
  let docId = null, currentPage = 0, totalPages = 0, zoom = 1.2;

  const state = {
    tool: null, color: '#ffeb3b', thickness: 2,
    stack: [],                             // pending actions (all pages)
    history: [], historyIdx: -1,           // snapshots for undo/redo
    drawing: false, stroke: [],
    signatureDataURL: null,

    sel: null,         // {index, page, kind, handle, startMouse, startRect, startPoints}
    draggingSel: false
  };

  const el = (id) => document.getElementById(id);
  const toolbarBtns = [...document.querySelectorAll('[data-tool]')];
  const pageImg = el('pageImg');
  const overlay = el('overlay');

  const MIN_PIX = 3;             // tiny drags ignored
  const HANDLE = 8;              // handle size (px)
  const HIT_PAD = 6;             // hit padding (px)

  // Tool buttons
  toolbarBtns.forEach((b) => {
    b.addEventListener('click', () => {
      toolbarBtns.forEach((x) => x.classList.remove('tool-active'));
      b.classList.add('tool-active');
      state.tool = b.getAttribute('data-tool');
      // Leaving selection active is fine; user can still move things
    });
  });

  // Shortcuts
  window.addEventListener('keydown', (e) => {
    const k = e.key.toLowerCase();
    const map = { h:'highlight', s:'strikeout', r:'rect', c:'circle', l:'line', a:'arrow', i:'ink', t:'textbox', g:'signature' };
    if (!e.ctrlKey && !e.metaKey) {
      const btn = document.querySelector(`[data-tool="${map[k]}"]`);
      if (btn) btn.click();
    }
    if ((e.ctrlKey||e.metaKey) && k==='z') { undo(); }
    if ((e.ctrlKey||e.metaKey) && k==='y') { redo(); }
    if ((k === 'delete' || k === 'backspace') && state.sel) { deleteSelected(); e.preventDefault(); }
    if (k === 'escape') { state.sel = null; redrawOverlay(); }
  });

  el('color').addEventListener('input', e => state.color = e.target.value);
  el('thickness').addEventListener('input', e => state.thickness = +e.target.value);
  el('zoom').addEventListener('input', e => { zoom = +e.target.value; renderPage(); });

  // Upload
  el('file').addEventListener('change', async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    try {
      const fd = new FormData(); fd.append('file', f);
      const r = await fetch('/upload', { method: 'POST', body: fd });
      const j = await parseMaybeJSON(r);
      if (!r.ok || j.error) throw new Error(j.error || 'Upload failed');
      docId = j.doc_id;
      resetStacks();
      await loadThumbs();
      await renderPage(0);
    } catch (err) { alert('Upload error: ' + err.message); console.error(err); }
  });

  function resetStacks(){
    state.stack = [];
    state.history = [];
    state.historyIdx = -1;
    snapshot(); // initial empty
    state.sel = null;
  }

  // Download / Rollback
  el('btnDownload').addEventListener('click', () => { if (docId) window.location.href = '/download/' + docId; });
  el('btnUndoServer').addEventListener('click', async () => {
    if (!docId) return;
    try {
      const r = await fetch('/revert/' + docId, { method: 'POST' });
      const j = await parseMaybeJSON(r);
      if (!j.ok) throw new Error(j.error || 'Rollback failed');
      resetStacks();
      await renderPage();
    } catch (err) { alert('Rollback error: ' + err.message); console.error(err); }
  });

  // Undo/Redo via snapshots
  el('btnUndo').addEventListener('click', () => undo());
  el('btnRedo').addEventListener('click', () => redo());
  function snapshot() {
    // store a deep copy of stack
    state.history = state.history.slice(0, state.historyIdx + 1);
    state.history.push(JSON.stringify(state.stack));
    state.historyIdx = state.history.length - 1;
  }
  function undo() {
    if (state.historyIdx > 0) {
      state.historyIdx--;
      state.stack = JSON.parse(state.history[state.historyIdx]);
      state.sel = null;
      redrawOverlay();
    }
  }
  function redo() {
    if (state.historyIdx < state.history.length - 1) {
      state.historyIdx++;
      state.stack = JSON.parse(state.history[state.historyIdx]);
      state.sel = null;
      redrawOverlay();
    }
  }

  // Save
  el('btnSave').addEventListener('click', async () => {
    if (!docId || !state.stack.length) return;
    try {
      const viewport = { w: overlay.width, h: overlay.height };
      const payload = { actions: state.stack.map(a => ({ ...a, viewport })) };
      const r = await fetch('/annotate/' + docId, {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'Accept':'application/json' }, body: JSON.stringify(payload)
      });
      const j = await parseMaybeJSON(r);
      if (!j.ok) throw new Error(j.error || 'Save failed');
      resetStacks();
      await renderPage();
    } catch (err) { alert('Save error: ' + err.message); console.error(err); }
  });

  async function parseMaybeJSON(response) {
    const ct = response.headers.get('content-type') || '';
    if (ct.includes('application/json')) return await response.json();
    const text = await response.text();
    return { ok: false, error: text.slice(0, 300) || 'Non-JSON response', raw: text };
  }

  // Thumbnails
  async function loadThumbs() {
    const r = await fetch('/thumbs/' + docId);
    const j = await parseMaybeJSON(r);
    if (!r.ok || j.error) throw new Error(j.error || 'Thumbs failed');
    totalPages = j.pages;
    const wrap = el('thumbs'); wrap.innerHTML = '';
    for (let i = 0; i < totalPages; i++) {
      const img = document.createElement('img');
      img.src = `/thumb/${docId}/${i}`;
      img.className = 'img-fluid mb-2 rounded';
      img.style.cursor = 'pointer';
      img.addEventListener('click', () => renderPage(i));
      wrap.appendChild(img);
    }
  }

  function syncOverlaySize() {
    overlay.width  = Math.max(pageImg.clientWidth  || 1, 1);
    overlay.height = Math.max(pageImg.clientHeight || 1, 1);
    overlay.style.width  = overlay.width  + 'px';
    overlay.style.height = overlay.height + 'px';
  }
  window.addEventListener('resize', () => { syncOverlaySize(); redrawOverlay(); });

  async function renderPage(p = currentPage) {
    if (docId == null) return;
    currentPage = p;
    pageImg.src = `/page/${docId}/${currentPage}?zoom=${zoom}`;
    await new Promise((res, rej) => { pageImg.onload = res; pageImg.onerror = () => rej(new Error('Failed to load page image')); });
    syncOverlaySize(); redrawOverlay();
  }

  function hexToRgb(hex) { const n = parseInt(hex.slice(1), 16); return [(n>>16)&255, (n>>8)&255, n&255]; }

  // ── Drawing + Selection/Editing ────────────────────────────────
  overlay.addEventListener('mousedown', (e) => {
    const pt = rel(e);

    // First, try select/hit existing item
    const hit = hitTestAt(pt.x, pt.y);
    if (hit) {
      state.sel = hit; state.draggingSel = true; state.sel.startMouse = [pt.x, pt.y];
      snapshotStartForSel(); // prepare snapshot of current stack
      return;
    }

    // Otherwise, start creating with the active tool
    if (!state.tool) return;
    state.drawing = true; state.start = [pt.x, pt.y];
    if (state.tool === 'ink') state.stroke = [[pt.x, pt.y]];
  });

  overlay.addEventListener('mousemove', (e) => {
    const pt = rel(e);

    // Handle dragging selection (move / resize / line endpoints)
    if (state.draggingSel && state.sel) {
      applyDragToSelection(pt);
      redrawOverlay();
      return;
    }

    if (!state.drawing) {
      redrawOverlay(); // ensures cursor/handles refresh
      return;
    }

    if (state.tool === 'ink') { state.stroke.push([pt.x, pt.y]); redrawOverlay(); return; }

    // Show preview while creating
    redrawOverlay();
    const ctx = overlay.getContext('2d');
    const preview = buildActionFromDrag(state.start, [pt.x, pt.y], true);
    if (preview) drawLocal(ctx, preview, true);
  });

  overlay.addEventListener('mouseup', (e) => {
    const pt = rel(e);

    // Finish dragging an existing selection
    if (state.draggingSel && state.sel) {
      state.draggingSel = false;
      snapshot(); // commit new state
      return;
    }

    if (!state.drawing) return;
    state.drawing = false;

    if (state.tool === 'ink') {
      if (state.stroke.length > 1) {
        state.stack.push({ type:'ink', page: currentPage, points:[state.stroke], color:hexToRgb(state.color), colorHex:state.color, thickness:state.thickness });
        snapshot();
      }
      state.stroke = []; redrawOverlay(); return;
    }

    const act = buildActionFromDrag(state.start, [pt.x, pt.y], false);
    if (!act) return;
    state.stack.push(act); snapshot(); redrawOverlay();
  });

  // double-click to edit textbox text if selected
  overlay.addEventListener('dblclick', () => {
    const s = state.sel; if (!s) return;
    const a = state.stack[s.index];
    if (a && a.type === 'textbox' && a.page === currentPage) {
      const newText = prompt('Edit text:', a.text || '');
      if (newText !== null) { a.text = newText; snapshot(); redrawOverlay(); }
    }
  });

  function rel(e) { const r = overlay.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; }

  function redrawOverlay() {
    const ctx = overlay.getContext('2d');
    ctx.clearRect(0,0,overlay.width, overlay.height);

    // existing items
    for (let i=0; i<state.stack.length; i++) {
      const a = state.stack[i]; if (a.page !== currentPage) continue;
      drawLocal(ctx, a, false);
    }

    // preview ink stroke
    if (state.drawing && state.tool === 'ink' && state.stroke.length) {
      ctx.lineWidth = state.thickness; ctx.strokeStyle = state.color;
      ctx.beginPath(); ctx.moveTo(state.stroke[0][0], state.stroke[0][1]);
      for (let i=1; i<state.stroke.length; i++) ctx.lineTo(state.stroke[i][0], state.stroke[i][1]);
      ctx.stroke();
    }

    // selection overlay
    if (state.sel && state.sel.page === currentPage) drawSelection(ctx, state.stack[state.sel.index], state.sel);
  }

  function drawLocal(ctx, a, isPreview=false) {
    ctx.save();
    ctx.strokeStyle = a.colorHex || '#000';
    ctx.lineWidth = a.thickness || 2;

    if (a.type === 'highlight' || a.type === 'strikeout') {
      const r = a.rect; ctx.globalAlpha = a.opacity || (a.type==='highlight' ? 0.35 : 0.25);
      ctx.fillStyle = a.colorHex || '#ff0'; ctx.fillRect(r[0], r[1], r[2]-r[0], r[3]-r[1]); ctx.globalAlpha = 1.0;
    } else if (a.type === 'shape_rect') {
      const r = a.rect; ctx.strokeRect(r[0], r[1], r[2]-r[0], r[3]-r[1]);
    } else if (a.type === 'shape_circle') {
      const r = a.rect; const cx=(r[0]+r[2])/2, cy=(r[1]+r[3])/2; const rx=(r[2]-r[0])/2, ry=(r[3]-r[1])/2;
      ctx.beginPath(); ctx.ellipse(cx,cy,rx,ry,0,0,Math.PI*2); ctx.stroke();
    } else if (a.type === 'line' || a.type === 'arrow') {
      const [p1,p2] = a.points;
      ctx.beginPath(); ctx.moveTo(p1[0],p1[1]); ctx.lineTo(p2[0],p2[1]); ctx.stroke();
      if (a.type === 'arrow') {
        const ang = Math.atan2(p2[1]-p1[1], p2[0]-p1[0]), len = 10 + (a.thickness||2)*1.5;
        ctx.beginPath();
        ctx.moveTo(p2[0], p2[1]);
        ctx.lineTo(p2[0]-len*Math.cos(ang-0.4), p2[1]-len*Math.sin(ang-0.4));
        ctx.moveTo(p2[0], p2[1]);
        ctx.lineTo(p2[0]-len*Math.cos(ang+0.4), p2[1]-len*Math.sin(ang+0.4));
        ctx.stroke();
      }
    } else if (a.type === 'textbox') {
      const r = a.rect; ctx.strokeRect(r[0],r[1],r[2]-r[0],r[3]-r[1]);
      ctx.font = `${a.font_size||14}px Arial`; ctx.fillStyle = a.colorHex || '#000';
      wrapText(ctx, a.text||'', r[0]+4, r[1]+16, (r[2]-r[0])-8, (a.font_size||14)+4);
    } else if (a.type === 'ink') {
      for (const stroke of a.points) {
        if (!stroke.length) continue;
        ctx.beginPath(); ctx.moveTo(stroke[0][0], stroke[0][1]);
        for (let i=1; i<stroke.length; i++) ctx.lineTo(stroke[i][0], stroke[i][1]);
        ctx.stroke();
      }
    } else if (a.type === 'signature' && a.previewDataURL) {
      const r = a.rect; const img = new Image();
      img.onload = () => ctx.drawImage(img, r[0], r[1], r[2]-r[0], r[3]-r[1]);
      img.src = a.previewDataURL;
    }
    ctx.restore();
  }

  function wrapText(ctx, text, x, y, maxWidth, lineHeight) {
    const words = text.split(' '); let line = '';
    for (let n = 0; n < words.length; n++) {
      const testLine = line + words[n] + ' '; const testWidth = ctx.measureText(testLine).width;
      if (testWidth > maxWidth && n > 0) { ctx.fillText(line, x, y); line = words[n] + ' '; y += lineHeight; }
      else { line = testLine; }
    }
    ctx.fillText(line, x, y);
  }

  // ── Selection visuals & editing ────────────────────────────────
  function drawSelection(ctx, a, sel) {
    ctx.save();
    ctx.setLineDash([6,4]); ctx.strokeStyle = '#6ea8fe'; ctx.lineWidth = 1.5;

    if (a.type === 'line' || a.type === 'arrow') {
      const [p1,p2] = a.points;
      ctx.beginPath(); ctx.moveTo(p1[0],p1[1]); ctx.lineTo(p2[0],p2[1]); ctx.stroke();
      drawHandle(ctx, p1[0], p1[1]); drawHandle(ctx, p2[0], p2[1]);
    } else {
      const r = a.rect;
      ctx.strokeRect(r[0], r[1], r[2]-r[0], r[3]-r[1]);
      const corners = rectHandles(r);
      for (const c of corners) drawHandle(ctx, c[0], c[1]);
    }
    ctx.restore();
  }

  function drawHandle(ctx, x, y) {
    ctx.save();
    ctx.setLineDash([]);
    ctx.fillStyle = '#6ea8fe';
    ctx.fillRect(x - HANDLE/2, y - HANDLE/2, HANDLE, HANDLE);
    ctx.restore();
  }

  function rectHandles(r) { // 4 corners
    const x0=r[0], y0=r[1], x1=r[2], y1=r[3];
    return [[x0,y0],[x1,y0],[x1,y1],[x0,y1]];
  }

  function hitTestAt(x, y) {
    // highest on top (last in stack)
    for (let i=state.stack.length-1; i>=0; i--) {
      const a = state.stack[i];
      if (a.page !== currentPage) continue;

      if (a.type === 'line' || a.type === 'arrow') {
        const [p1,p2] = a.points;
        if (distPt(p1,[x,y]) <= HANDLE) return selObj(i,'p1');
        if (distPt(p2,[x,y]) <= HANDLE) return selObj(i,'p2');
        if (pointToSegment([x,y], p1, p2) <= HIT_PAD) return selObj(i,'move');
      } else {
        const r = a.rect, hx = rectHandles(r);
        for (let h=0; h<hx.length; h++) {
          if (Math.abs(x - hx[h][0]) <= HANDLE && Math.abs(y - hx[h][1]) <= HANDLE)
            return selObj(i, ['nw','ne','se','sw'][h]);
        }
        if (x >= r[0]-HIT_PAD && x <= r[2]+HIT_PAD && y >= r[1]-HIT_PAD && y <= r[3]+HIT_PAD)
          return selObj(i,'move');
      }
    }
    return null;
  }

  function selObj(index, handle) { return { index, page: currentPage, handle }; }

  function distPt(a,b){ const dx=a[0]-b[0], dy=a[1]-b[1]; return Math.hypot(dx,dy); }
  function pointToSegment(p, a, b){ // distance of p to segment ab
    const vx=b[0]-a[0], vy=b[1]-a[1]; const wx=p[0]-a[0], wy=p[1]-a[1];
    const c1 = vx*wx + vy*wy; if (c1 <= 0) return distPt(p,a);
    const c2 = vx*vx + vy*vy; if (c2 <= c1) return distPt(p,b);
    const t = c1 / c2; const proj=[a[0]+t*vx, a[1]+t*vy]; return distPt(p, proj);
  }

  let snapshotBeforeSel = null;
  function snapshotStartForSel() {
    snapshotBeforeSel = JSON.stringify(state.stack);
  }

  function applyDragToSelection(pt) {
    const s = state.sel; if (!s) return;
    const a = state.stack[s.index];
    const dx = pt.x - s.startMouse[0], dy = pt.y - s.startMouse[1];

    if (a.type === 'line' || a.type === 'arrow') {
      if (!s.startPoints) s.startPoints = JSON.parse(JSON.stringify(a.points));
      if (s.handle === 'p1') {
        a.points[0] = [ clampX(s.startPoints[0][0] + dx), clampY(s.startPoints[0][1] + dy) ];
      } else if (s.handle === 'p2') {
        a.points[1] = [ clampX(s.startPoints[1][0] + dx), clampY(s.startPoints[1][1] + dy) ];
      } else { // move both
        a.points[0] = [ clampX(s.startPoints[0][0] + dx), clampY(s.startPoints[0][1] + dy) ];
        a.points[1] = [ clampX(s.startPoints[1][0] + dx), clampY(s.startPoints[1][1] + dy) ];
      }
      return;
    }

    // rect-like
    if (!s.startRect) s.startRect = [...a.rect];
    let [x0,y0,x1,y1] = s.startRect;

    if (s.handle === 'move') {
      x0 = clampX(x0 + dx); x1 = clampX(x1 + dx);
      y0 = clampY(y0 + dy); y1 = clampY(y1 + dy);
      a.rect = [x0,y0,x1,y1]; enforceMinRect(a);
      return;
    }

    // resize by corner
    if (s.handle === 'nw') { x0 = clampX(x0 + dx); y0 = clampY(y0 + dy); }
    if (s.handle === 'ne') { x1 = clampX(x1 + dx); y0 = clampY(y0 + dy); }
    if (s.handle === 'se') { x1 = clampX(x1 + dx); y1 = clampY(y1 + dy); }
    if (s.handle === 'sw') { x0 = clampX(x0 + dx); y1 = clampY(y1 + dy); }
    // normalize
    const nx0 = Math.min(x0,x1), ny0 = Math.min(y0,y1), nx1 = Math.max(x0,x1), ny1 = Math.max(y0,y1);
    a.rect = [nx0,ny0,nx1,ny1]; enforceMinRect(a);
  }

  function clampX(v){ return Math.max(0, Math.min(overlay.width, v)); }
  function clampY(v){ return Math.max(0, Math.min(overlay.height, v)); }
  function enforceMinRect(a){
    const r=a.rect, w=r[2]-r[0], h=r[3]-r[1];
    if (w < MIN_PIX) { const c=(r[0]+r[2])/2; a.rect=[c-MIN_PIX/2, r[1], c+MIN_PIX/2, r[3]]; }
    if (h < MIN_PIX) { const c=(r[1]+r[3])/2; a.rect=[r[0], c-MIN_PIX/2, r[2], c+MIN_PIX/2]; }
  }

  function deleteSelected(){
    if (!state.sel) return;
    state.stack.splice(state.sel.index, 1);
    state.sel = null;
    snapshot();
    redrawOverlay();
  }

  // Build action from a creation drag
  function buildActionFromDrag(p1, p2, forPreview) {
    const color = hexToRgb(state.color);
    const base = { page: currentPage, color, colorHex: state.color, thickness: state.thickness };
    const rect = [Math.min(p1[0],p2[0]), Math.min(p1[1],p2[1]), Math.max(p1[0],p2[0]), Math.max(p1[1],p2[1])];
    const w = rect[2]-rect[0], h = rect[3]-rect[1];

    if (state.tool === 'signature') {
      let rx=rect[0], ry=rect[1], rw=w, rh=h;
      if (rw < MIN_PIX || rh < MIN_PIX) { rw = 180; rh = 60; }
      if (!state.signatureDataURL) { if(!forPreview) alert('Open Signature and draw your signature first.'); return null; }
      return { ...base, type:'signature', rect:[rx, ry, rx+rw, ry+rh], previewDataURL: state.signatureDataURL, image_data_url: state.signatureDataURL };
    }

    if (state.tool === 'textbox') {
      if (w < MIN_PIX || h < MIN_PIX) return null;
      const text = forPreview ? '' : (prompt('Text content?') || '');
      return { ...base, type:'textbox', rect, font_size:14, text };
    }

    if (state.tool === 'highlight') return (w < MIN_PIX || h < MIN_PIX) ? null : { ...base, type:'highlight', rect, opacity:0.35 };
    if (state.tool === 'strikeout') return (w < MIN_PIX || h < MIN_PIX) ? null : { ...base, type:'strikeout', rect, opacity:0.25 };
    if (state.tool === 'rect') return (w < MIN_PIX || h < MIN_PIX) ? null : { ...base, type:'shape_rect', rect };
    if (state.tool === 'circle') return (w < MIN_PIX || h < MIN_PIX) ? null : { ...base, type:'shape_circle', rect };

    if (state.tool === 'line' || state.tool === 'arrow') {
      const d = Math.hypot(p2[0]-p1[0], p2[1]-p1[1]);
      return (d < MIN_PIX) ? null : { ...base, type: state.tool, points: [p1, p2] };
    }
    return null;
  }

  // Signature pad
  const sigPad = document.getElementById('sigPad'); const sigCtx = sigPad.getContext('2d');
  sigCtx.lineWidth=2; sigCtx.lineCap='round'; sigCtx.strokeStyle='#111';
  let sigDraw=false, last=null;
  function sigPos(e){ const r=sigPad.getBoundingClientRect(); const t=e.touches?.[0]||e; return { x:t.clientX-r.left, y:t.clientY-r.top }; }
  function sigStart(e){ sigDraw=true; last=sigPos(e); e.preventDefault(); }
  function sigMove(e){ if(!sigDraw) return; const p=sigPos(e); sigCtx.beginPath(); sigCtx.moveTo(last.x,last.y); sigCtx.lineTo(p.x,p.y); sigCtx.stroke(); last=p; e.preventDefault(); }
  function sigEnd(){ sigDraw=false; }
  sigPad.addEventListener('mousedown',sigStart); sigPad.addEventListener('mousemove',sigMove); window.addEventListener('mouseup',sigEnd);
  sigPad.addEventListener('touchstart',sigStart,{passive:false}); sigPad.addEventListener('touchmove',sigMove,{passive:false}); sigPad.addEventListener('touchend',sigEnd);
  document.getElementById('sigClear').addEventListener('click',()=>{ sigCtx.clearRect(0,0,sigPad.width,sigPad.height); document.getElementById('sigStatus').textContent='Not set'; state.signatureDataURL=null; });
  document.getElementById('sigUse').addEventListener('click',()=>{ state.signatureDataURL=sigPad.toDataURL('image/png'); document.getElementById('sigStatus').textContent='Signature saved'; state.tool='signature'; toolbarBtns.forEach(x=>x.classList.remove('tool-active')); document.querySelector('[data-tool="signature"]')?.classList.add('tool-active');});

  function syncOverlaySize(){ overlay.width=Math.max(pageImg.clientWidth||1,1); overlay.height=Math.max(pageImg.clientHeight||1,1); overlay.style.width=overlay.width+'px'; overlay.style.height=overlay.height+'px'; }
  window.addEventListener('resize',syncOverlaySize);
})();
</script>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
"""

# ───────── Routes ─────────
@app.get("/")
def index():
    return render_template_string(INDEX_HTML)

@app.post("/upload")
def upload():
    f = request.files.get("file")
    if not f or not _allowed(f.filename):
        return jsonify({"error": "Please upload a PDF file"}), 400
    filename = secure_filename(f.filename)
    doc_id = str(uuid.uuid4())
    original = f"{doc_id}/original.pdf"
    Storage.save(f.read(), original)
    working = f"{doc_id}/working.pdf"
    Storage.save(Storage.get(original), working)
    DOCS[doc_id] = {"name": filename, "original": original, "working": working, "versions": [working], "created": datetime.utcnow().isoformat()}
    return jsonify({"doc_id": doc_id})

@app.get("/thumbs/<doc_id>")
def thumbs(doc_id):
    if doc_id not in DOCS:
        return jsonify({"error": "doc not found"}), 404
    pdf = fitz.open(stream=Storage.get(DOCS[doc_id]["working"]), filetype="pdf")
    return jsonify({"pages": len(pdf)})

@app.get("/thumb/<doc_id>/<int:page>")
def thumb(doc_id, page):
    if doc_id not in DOCS:
        return jsonify({"error": "doc not found"}), 404
    pdf = fitz.open(stream=Storage.get(DOCS[doc_id]["working"]), filetype="pdf")
    if not (0 <= page < len(pdf)):
        return jsonify({"error": "bad page"}), 400
    pix = pdf[page].get_pixmap(dpi=120)
    return send_file(io.BytesIO(pix.tobytes("png")), mimetype="image/png")

@app.get("/page/<doc_id>/<int:page>")
def page_png(doc_id, page):
    if doc_id not in DOCS:
        return jsonify({"error": "doc not found"}), 404
    zoom = float(request.args.get("zoom", "1.0"))
    dpi = max(72, min(300, int(144 * zoom)))
    pdf = fitz.open(stream=Storage.get(DOCS[doc_id]["working"]), filetype="pdf")
    if not (0 <= page < len(pdf)):
        return jsonify({"error": "bad page"}), 400
    mat = fitz.Matrix(dpi / 72.0, dpi / 72.0)
    pix = pdf[page].get_pixmap(matrix=mat, alpha=False)
    return send_file(io.BytesIO(pix.tobytes("png")), mimetype="image/png")

@app.post("/annotate/<doc_id>")
def annotate(doc_id):
    try:
        if doc_id not in DOCS:
            return jsonify({"ok": False, "error": "doc not found"}), 404
        data = request.get_json(force=True, silent=False) or {}
        actions = data.get("actions", [])
        if not actions:
            return jsonify({"ok": True, "message": "nothing to do"})
        pdf = fitz.open(stream=Storage.get(DOCS[doc_id]["working"]), filetype="pdf")

        for a in actions:
            t = a.get("type")
            page = pdf[a["page"]]
            page_rect = page.rect
            viewport = a.get("viewport", {"w": page_rect.width, "h": page_rect.height})

            if t in ("highlight", "strikeout", "shape_rect", "shape_circle", "textbox", "signature"):
                rect = _scale_rect(a["rect"], page_rect, viewport)
                rect = _ensure_min_rect(_clip_rect(rect, page_rect), page_rect, min_w=2.0, min_h=2.0)

            if t == "highlight":
                try:
                    annot = page.add_highlight_annot(rect)
                    annot.set_colors(stroke=_color_tuple(a.get("color")))
                    if "opacity" in a: annot.set_opacity(float(a["opacity"]))
                    annot.update()
                except Exception:
                    annot = page.add_rect_annot(rect)
                    annot.set_colors(stroke=None, fill=_color_tuple(a.get("color")))
                    annot.set_opacity(float(a.get("opacity", 0.35)))
                    annot.update()

            elif t == "strikeout":
                try:
                    annot = page.add_strikeout_annot(rect)
                    annot.set_colors(stroke=_color_tuple(a.get("color")))
                    if "opacity" in a: annot.set_opacity(float(a["opacity"]))
                    annot.update()
                except Exception:
                    y = (rect.y0 + rect.y1) / 2
                    p1 = fitz.Point(rect.x0, y); p2 = fitz.Point(rect.x1, y)
                    annot = page.add_line_annot(p1, p2)
                    annot.set_border(width=float(a.get("thickness", 2)))
                    annot.set_colors(stroke=_color_tuple(a.get("color")))
                    annot.update()

            elif t == "shape_rect":
                annot = page.add_rect_annot(rect)
                annot.set_border(width=float(a.get("thickness", 2)))
                annot.set_colors(stroke=_color_tuple(a.get("color")))
                annot.update()

            elif t == "shape_circle":
                annot = page.add_circle_annot(rect)
                annot.set_border(width=float(a.get("thickness", 2)))
                annot.set_colors(stroke=_color_tuple(a.get("color")))
                annot.update()

            elif t in ("line", "arrow"):
                p1 = _scale_point(a["points"][0], page_rect, viewport)
                p2 = _scale_point(a["points"][1], page_rect, viewport)
                if p1 == p2:
                    p2 = fitz.Point(min(page_rect.x1, p2.x + 5), min(page_rect.y1, p2.y + 5))
                annot = page.add_line_annot(p1, p2)
                annot.set_border(width=float(a.get("thickness", 2)))
                annot.set_colors(stroke=_color_tuple(a.get("color")))
                if t == "arrow":
                    try: annot.set_line_ends(("OpenArrow", "None"))
                    except Exception: pass
                annot.update()

            elif t == "ink":
                strokes = [[_scale_point(pt, page_rect, viewport) for pt in stroke] for stroke in a["points"]]
                strokes = [s for s in strokes if len(s) > 1]
                if not strokes: continue
                annot = page.add_ink_annot(strokes)
                annot.set_colors(stroke=_color_tuple(a.get("color")))
                annot.set_border(width=float(a.get("thickness", 2)))
                annot.update()

            elif t == "textbox":
                content = a.get("text", "")
                annot = page.add_freetext_annot(rect, content)
                annot.set_colors(stroke=_color_tuple(a.get("color", [0,0,0])))
                try: annot.set_font("helv", float(a.get("font_size", 14)))
                except Exception: pass
                annot.update()

            elif t == "signature":
                img_bytes = _decode_data_url(a.get("image_data_url"))
                if img_bytes:
                    page.insert_image(rect, stream=img_bytes, keep_proportion=True)

        out = io.BytesIO()
        pdf.save(out)
        out.seek(0)
        new_key = f"{doc_id}/{uuid.uuid4().hex}.pdf"
        Storage.save(out.read(), new_key)
        DOCS[doc_id]["working"] = new_key
        DOCS[doc_id]["versions"].append(new_key)
        return jsonify({"ok": True, "version": len(DOCS[doc_id]['versions'])})
    except Exception as e:
        app.logger.error("Annotate failed: %s\n%s", e, traceback.format_exc())
        return jsonify({"ok": False, "error": str(e)}), 400

@app.post("/revert/<doc_id>")
def revert(doc_id):
    if doc_id not in DOCS:
        return jsonify({"ok": False, "error": "doc not found"}), 404
    vers = DOCS[doc_id]["versions"]
    if len(vers) < 2:
        return jsonify({"ok": False, "error": "no previous version"}), 400
    vers.pop()
    DOCS[doc_id]["working"] = vers[-1]
    return jsonify({"ok": True, "version": len(vers)})

@app.get("/download/<doc_id>")
def download(doc_id):
    if doc_id not in DOCS:
        return jsonify({"error": "doc not found"}), 404
    data = Storage.get(DOCS[doc_id]["working"])
    return send_file(io.BytesIO(data), mimetype="application/pdf",
                     as_attachment=True, download_name=DOCS[doc_id]["name"])

@app.get("/health")
def health():
    return jsonify({"ok": True})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, debug=False)
