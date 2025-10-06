# app.py — OnePlacePDF (Flask + PyMuPDF)
# Final: solid Undo/Redo, on-canvas Delete/Duplicate UI, textbox fixes, signature stability.
#
# requirements.txt
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
    label.small-label { font-size:.85rem; color:#aeb7d6; }
    .brand { user-select:none }
    #selToolbar {
      position:absolute; display:none; z-index:3;
      background:#0f1428; border:1px solid #2a355d; border-radius:10px; padding:6px;
      box-shadow:0 6px 16px rgba(0,0,0,.35);
    }
    #selToolbar .btn { padding:.15rem .5rem; }
  </style>
</head>
<body>
<div class="container-fluid py-3">
  <div class="d-flex align-items-center gap-2 mb-3">
    <h3 class="m-0 brand">Mini PDF Editor</h3>
    <input id="file" type="file" class="form-control w-auto" accept="application/pdf" />
    <button id="btnDownload" class="btn btn-success">Download</button>
    <button id="btnUndoServer" class="btn btn-outline-warning">Rollback (server)</button>
    <a href="/help" target="_blank" class="btn btn-outline-info">How to use</a>
    <a href="/shortcuts" target="_blank" class="btn btn-outline-secondary">Shortcuts</a>
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
        — Click an annotation to move/resize. ⌫/Del = delete. Double-click a textbox to edit.
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
          <button class="btn btn-light" data-tool="tick">Tick ✓</button>
          <button class="btn btn-light" data-tool="cross">Cross ✗</button>
          <button class="btn btn-warning" data-tool="signature" id="btnSignature" data-bs-toggle="modal" data-bs-target="#signatureModal">Signature</button>

          <div class="vr"></div>
          <label class="small-label">Color</label>
          <input id="color" type="color" value="#ffeb3b" class="form-control form-control-color" />
          <label class="small-label ms-2">Thickness</label>
          <input id="thickness" type="range" min="1" max="12" value="2" class="form-range w-25" />

          <div class="vr"></div>
          <label class="small-label">Font</label>
          <select id="fontFamily" class="form-select form-select-sm w-auto">
            <option value="helv" selected>Helvetica</option>
            <option value="times">Times</option>
            <option value="cour">Courier</option>
          </select>
          <label class="small-label ms-2">Size</label>
          <input id="fontSize" type="number" min="8" max="72" step="1" value="14" class="form-control form-control-sm" style="width:80px;" />

          <div class="vr"></div>
          <button class="btn btn-primary" id="btnSave">Save Edits</button>
          <button class="btn btn-outline-light" id="btnUndo">Undo</button>
          <button class="btn btn-outline-light" id="btnRedo">Redo</button>
          <div class="vr"></div>
          <label class="small-label">Zoom</label>
          <input id="zoom" type="range" min="0.6" max="2.5" step="0.1" value="1.2" class="form-range w-25" />
        </div>
      </div>

      <div id="canvasWrap" class="rounded-3 shadow">
        <img id="pageImg" src="" />
        <canvas id="overlay"></canvas>

        <!-- selection mini-toolbar -->
        <div id="selToolbar" class="btn-group">
          <button id="btnDup" class="btn btn-sm btn-outline-light" title="Duplicate">Duplicate</button>
          <button id="btnDel" class="btn btn-sm btn-danger" title="Delete">Delete</button>
        </div>
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
    fontFamily: 'helv', fontSize: 14,
    stack: [], history: [], historyIdx: -1,
    drawing: false, stroke: [],
    signatureDataURL: null,
    sel: null, draggingSel: false
  };

  const el = (id) => document.getElementById(id);
  const toolbarBtns = [...document.querySelectorAll('[data-tool]')];
  const pageImg = el('pageImg');
  const overlay = el('overlay');
  const canvasWrap = document.getElementById('canvasWrap');
  const selToolbar = el('selToolbar'), btnDup = el('btnDup'), btnDel = el('btnDel');

  const imgCache = new Map();
  function getImageCached(src) {
    if (!src) return null;
    let img = imgCache.get(src);
    if (!img) {
      img = new Image();
      img.decoding = 'async'; img.loading = 'eager'; img.src = src;
      imgCache.set(src, img);
    }
    return img;
  }

  const MIN_PIX = 3, HANDLE = 8, HIT_PAD = 6;

  toolbarBtns.forEach((b) => {
    b.addEventListener('click', () => {
      toolbarBtns.forEach((x) => x.classList.remove('tool-active'));
      b.classList.add('tool-active');
      state.tool = b.getAttribute('data-tool');
    });
  });

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
    if (k === 'escape') { clearSelection(); }
  });

  el('color').addEventListener('input', e => { state.color = e.target.value; if (state.sel) { applyColorToSelection(); }});
  el('thickness').addEventListener('input', e => { state.thickness = +e.target.value; if (state.sel) { applyThicknessToSelection(); }});
  el('fontFamily').addEventListener('change', e => { state.fontFamily = e.target.value; if (state.sel) applyTextStyleToSelection(); });
  el('fontSize').addEventListener('input', e => { state.fontSize = +e.target.value; if (state.sel) applyTextStyleToSelection(); });

  function applyColorToSelection(){
    const s = state.sel; if (!s) return; const a = state.stack[s.index];
    if (!a) return; a.colorHex = state.color; a.color = hexToRgb(state.color); snapshot(); redrawOverlay();
  }
  function applyThicknessToSelection(){
    const s = state.sel; if (!s) return; const a = state.stack[s.index];
    if (!a) return; a.thickness = state.thickness; snapshot(); redrawOverlay();
  }
  function applyTextStyleToSelection(){
    const s = state.sel; if (!s) return; const a = state.stack[s.index];
    if (a && a.type === 'textbox') { a.font = state.fontFamily; a.font_size = state.fontSize; snapshot(); redrawOverlay(); }
  }

  el('zoom').addEventListener('input', e => { zoom = +e.target.value; renderPage(); });

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
    state.stack = []; state.history = []; state.historyIdx = -1; snapshot();
    clearSelection();
  }

  el('btnDownload').addEventListener('click', () => { if (docId) window.location.href = '/download/' + docId; });
  el('btnUndoServer').addEventListener('click', async () => {
    if (!docId) return;
    try {
      const r = await fetch('/revert/' + docId, { method: 'POST' });
      const j = await parseMaybeJSON(r);
      if (!j.ok) throw new Error(j.error || 'Rollback failed');
      resetStacks(); await renderPage();
    } catch (err) { alert('Rollback error: ' + err.message); console.error(err); }
  });

  el('btnUndo').addEventListener('click', () => undo());
  el('btnRedo').addEventListener('click', () => redo());
  function snapshot() {
    state.history = state.history.slice(0, state.historyIdx + 1);
    state.history.push(JSON.stringify(state.stack));
    state.historyIdx = state.history.length - 1;
  }
  function undo() {
    if (state.historyIdx > 0) {
      state.historyIdx--; state.stack = JSON.parse(state.history[state.historyIdx]);
      clearSelection(); redrawOverlay();
    }
  }
  function redo() {
    if (state.historyIdx < state.history.length - 1) {
      state.historyIdx++; state.stack = JSON.parse(state.history[state.historyIdx]);
      clearSelection(); redrawOverlay();
    }
  }

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
      resetStacks(); await renderPage();
    } catch (err) { alert('Save error: ' + err.message); console.error(err); }
  });

  async function parseMaybeJSON(response) {
    const ct = response.headers.get('content-type') || '';
    if (ct.includes('application/json')) return await response.json();
    const text = await response.text();
    return { ok: false, error: text.slice(0, 300) || 'Non-JSON response', raw: text };
  }

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
  const dist = (a,b)=>Math.hypot(b[0]-a[0], b[1]-a[1]);

  overlay.addEventListener('mousedown', (e) => {
    const pt = rel(e);
    const hit = hitTestAt(pt.x, pt.y);
    if (hit) {
      state.sel = hit; state.draggingSel = true; state.sel.startMouse = [pt.x, pt.y];
      snapshotStartForSel(); updateSelToolbar(); return;
    }
    clearSelection();
    if (!state.tool) return;
    state.drawing = true; state.start = [pt.x, pt.y];
    if (state.tool === 'ink') state.stroke = [[pt.x, pt.y]];
  });

  overlay.addEventListener('mousemove', (e) => {
    const pt = rel(e);
    if (state.draggingSel && state.sel) { applyDragToSelection(pt); requestAnimationFrame(redrawOverlay); return; }
    if (!state.drawing) { redrawOverlay(); return; }
    if (state.tool === 'ink') { state.stroke.push([pt.x, pt.y]); requestAnimationFrame(redrawOverlay); return; }
    redrawOverlay();
    const ctx = overlay.getContext('2d');
    const preview = buildActionFromDrag(state.start, [pt.x, pt.y], true);
    if (preview) drawLocal(ctx, preview, true);
  });

  overlay.addEventListener('mouseup', (e) => {
    const pt = rel(e);
    if (state.draggingSel && state.sel) { state.draggingSel = false; snapshot(); updateSelToolbar(); return; }
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
    for (let i=0; i<state.stack.length; i++) {
      const a = state.stack[i]; if (a.page !== currentPage) continue;
      drawLocal(ctx, a, false);
    }
    if (state.drawing && state.tool === 'ink' && state.stroke.length) {
      ctx.lineWidth = state.thickness; ctx.strokeStyle = state.color;
      ctx.beginPath(); ctx.moveTo(state.stroke[0][0], state.stroke[0][1]);
      for (let i=1; i<state.stroke.length; i++) ctx.lineTo(state.stroke[i][0], state.stroke[i][1]);
      ctx.stroke();
    }
    if (state.sel && state.sel.page === currentPage) drawSelection(ctx, state.stack[state.sel.index], state.sel);
    updateSelToolbar();
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

    } else if (a.type === 'signature') {
      const r = a.rect; const img = getImageCached(a.previewDataURL);
      if (img && img.complete && img.naturalWidth) {
        ctx.drawImage(img, r[0], r[1], r[2]-r[0], r[3]-r[1]);
      } else if (img) { img.onload = () => requestAnimationFrame(redrawOverlay); }

    } else if (a.type === 'tick' || a.type === 'cross') {
      const r = a.rect;
      ctx.beginPath();
      if (a.type === 'tick') {
        const x0=r[0], y0=r[1], x1=r[2], y1=r[3];
        ctx.moveTo(x0 + (x1-x0)*0.1, y0 + (y1-y0)*0.6);
        ctx.lineTo(x0 + (x1-x0)*0.4, y1 - (y1-y0)*0.1);
        ctx.lineTo(x1 - (x1-x0)*0.1, y0 + (y1-y0)*0.15);
      } else {
        ctx.moveTo(r[0], r[1]); ctx.lineTo(r[2], r[3]);
        ctx.moveTo(r[2], r[1]); ctx.lineTo(r[0], r[3]);
      }
      ctx.stroke();
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
      for (const c of rectHandles(r)) drawHandle(ctx, c[0], c[1]);
    }
    ctx.restore();
  }
  function drawHandle(ctx, x, y) { ctx.save(); ctx.setLineDash([]); ctx.fillStyle = '#6ea8fe'; ctx.fillRect(x - 8/2, y - 8/2, 8, 8); ctx.restore(); }
  function rectHandles(r){ const x0=r[0],y0=r[1],x1=r[2],y1=r[3]; return [[x0,y0],[x1,y0],[x1,y1],[x0,y1]]; }

  function hitTestAt(x, y) {
    for (let i=state.stack.length-1; i>=0; i--) {
      const a = state.stack[i]; if (a.page !== currentPage) continue;
      if (a.type === 'line' || a.type === 'arrow') {
        const [p1,p2] = a.points;
        if (dist([x,y],p1) <= 8) return {index:i,page:currentPage,handle:'p1'};
        if (dist([x,y],p2) <= 8) return {index:i,page:currentPage,handle:'p2'};
        if (pointToSegment([x,y], p1, p2) <= 6) return {index:i,page:currentPage,handle:'move'};
      } else {
        const r = a.rect;
        const hs = rectHandles(r);
        for (let h=0; h<hs.length; h++) {
          if (Math.abs(x - hs[h][0]) <= 8 && Math.abs(y - hs[h][1]) <= 8)
            return {index:i,page:currentPage,handle:['nw','ne','se','sw'][h]};
        }
        if (x>=r[0]-6 && x<=r[2]+6 && y>=r[1]-6 && y<=r[3]+6)
          return {index:i,page:currentPage,handle:'move'};
      }
    }
    return null;
  }
  function pointToSegment(p, a, b){
    const vx=b[0]-a[0], vy=b[1]-a[1]; const wx=p[0]-a[0], wy=p[1]-a[1];
    const c1 = vx*wx + vy*wy; if (c1 <= 0) return dist(p,a);
    const c2 = vx*vx + vy*vy; if (c2 <= c1) return dist(p,b);
    const t = c1 / c2; const proj=[a[0]+t*vx, a[1]+t*vy]; return dist(p, proj);
  }
  let snapshotBeforeSel = null;
  function snapshotStartForSel() { snapshotBeforeSel = JSON.stringify(state.stack); }
  function clampX(v){ return Math.max(0, Math.min(overlay.width, v)); }
  function clampY(v){ return Math.max(0, Math.min(overlay.height, v)); }
  function enforceMinRect(a){
    const r=a.rect,w=r[2]-r[0],h=r[3]-r[1];
    if (w < 3) { const c=(r[0]+r[2])/2; a.rect=[c-3/2, r[1], c+3/2, r[3]]; }
    if (h < 3) { const c=(r[1]+r[3])/2; a.rect=[r[0], c-3/2, r[2], c+3/2]; }
  }
  function applyDragToSelection(pt) {
    const s = state.sel; if (!s) return;
    const a = state.stack[s.index]; const dx = pt.x - s.startMouse[0], dy = pt.y - s.startMouse[1];
    if (a.type === 'line' || a.type === 'arrow') {
      if (!s.startPoints) s.startPoints = JSON.parse(JSON.stringify(a.points));
      if (s.handle === 'p1') a.points[0] = [clampX(s.startPoints[0][0]+dx), clampY(s.startPoints[0][1]+dy)];
      else if (s.handle === 'p2') a.points[1] = [clampX(s.startPoints[1][0]+dx), clampY(s.startPoints[1][1]+dy)];
      else { a.points[0] = [clampX(s.startPoints[0][0]+dx), clampY(s.startPoints[0][1]+dy)];
             a.points[1] = [clampX(s.startPoints[1][0]+dx), clampY(s.startPoints[1][1]+dy)]; }
      return;
    }
    if (!s.startRect) s.startRect = [...a.rect];
    let [x0,y0,x1,y1] = s.startRect;
    if (s.handle === 'move') { x0=clampX(x0+dx); x1=clampX(x1+dx); y0=clampY(y0+dy); y1=clampY(y1+dy); a.rect=[x0,y0,x1,y1]; enforceMinRect(a); return; }
    if (s.handle === 'nw') { x0=clampX(x0+dx); y0=clampY(y0+dy); }
    if (s.handle === 'ne') { x1=clampX(x1+dx); y0=clampY(y0+dy); }
    if (s.handle === 'se') { x1=clampX(x1+dx); y1=clampY(y1+dy); }
    if (s.handle === 'sw') { x0=clampX(x0+dx); y1=clampY(y1+dy); }
    const nx0=Math.min(x0,x1), ny0=Math.min(y0,y1), nx1=Math.max(x0,x1), ny1=Math.max(y0,y1);
    a.rect=[nx0,ny0,nx1,ny1]; enforceMinRect(a);
  }

  function clearSelection(){ state.sel=null; selToolbar.style.display='none'; redrawOverlay(); }

  function deleteSelected(){
    if (!state.sel) return;
    state.stack.splice(state.sel.index,1); state.sel=null; snapshot(); redrawOverlay();
  }

  btnDel.addEventListener('click', (e)=>{ e.stopPropagation(); deleteSelected(); });
  btnDup.addEventListener('click', (e)=>{
    e.stopPropagation();
    if (!state.sel) return;
    const a = JSON.parse(JSON.stringify(state.stack[state.sel.index]));
    if (a.rect) a.rect = [a.rect[0]+8, a.rect[1]+8, a.rect[2]+8, a.rect[3]+8];
    if (a.points) a.points = a.points.map(p=>[p[0]+8,p[1]+8]);
    state.stack.push(a); snapshot(); state.sel={index:state.stack.length-1,page:currentPage,handle:'move'}; redrawOverlay();
  });

  function updateSelToolbar(){
    if (!state.sel || state.sel.page !== currentPage) { selToolbar.style.display='none'; return; }
    const a = state.stack[state.sel.index]; if (!a) { selToolbar.style.display='none'; return; }
    const r = a.rect ? a.rect : [ Math.min(a.points[0][0], a.points[1][0]), Math.min(a.points[0][1], a.points[1][1]),
                                  Math.max(a.points[0][0], a.points[1][0]), Math.max(a.points[0][1], a.points[1][1]) ];
    const left = Math.max(0, Math.min(overlay.width - selToolbar.offsetWidth, r[0]));
    const top  = Math.max(0, r[1] - 36);
    selToolbar.style.left = left + 'px';
    selToolbar.style.top  = top + 'px';
    selToolbar.style.display = 'block';
  }

  function buildActionFromDrag(p1, p2, preview) {
    const color = hexToRgb(state.color);
    const base = { page: currentPage, color, colorHex: state.color, thickness: state.thickness };
    const rect = [Math.min(p1[0],p2[0]), Math.min(p1[1],p2[1]), Math.max(p1[0],p2[0]), Math.max(p1[1],p2[1])];
    const w = rect[2]-rect[0], h = rect[3]-rect[1];

    if (state.tool === 'signature') {
      let rw=w, rh=h; if (rw < MIN_PIX || rh < MIN_PIX) { rw=180; rh=60; }
      if (!state.signatureDataURL) { if(!preview) alert('Open Signature and draw your signature first.'); return null; }
      getImageCached(state.signatureDataURL);
      return { ...base, type:'signature', rect:[rect[0],rect[1],rect[0]+rw,rect[1]+rh], previewDataURL: state.signatureDataURL, image_data_url: state.signatureDataURL };
    }
    if (state.tool === 'textbox') {
      if (w < MIN_PIX || h < MIN_PIX) return null;
      const text = preview ? '' : (prompt('Text content?') || '');
      return { ...base, type:'textbox', rect, font_size: state.fontSize, font: state.fontFamily, text };
    }
    if (state.tool === 'highlight') return (w < MIN_PIX || h < MIN_PIX) ? null : { ...base, type:'highlight', rect, opacity:0.35 };
    if (state.tool === 'strikeout') return (w < MIN_PIX || h < MIN_PIX) ? null : { ...base, type:'strikeout', rect, opacity:0.25 };
    if (state.tool === 'rect') return (w < MIN_PIX || h < MIN_PIX) ? null : { ...base, type:'shape_rect', rect };
    if (state.tool === 'circle') return (w < MIN_PIX || h < MIN_PIX) ? null : { ...base, type:'shape_circle', rect };
    if (state.tool === 'tick') return (w < MIN_PIX || h < MIN_PIX) ? null : { ...base, type:'tick', rect };
    if (state.tool === 'cross') return (w < MIN_PIX || h < MIN_PIX) ? null : { ...base, type:'cross', rect };
    if (state.tool === 'line' || state.tool === 'arrow') {
      if (Math.hypot(p2[0]-p1[0], p2[1]-p1[1]) < MIN_PIX) return null;
      return { ...base, type: state.tool, points: [p1, p2] };
    }
    return null;
  }

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
  document.getElementById('sigUse').addEventListener('click',()=>{ state.signatureDataURL=sigPad.toDataURL('image/png'); getImageCached(state.signatureDataURL); document.getElementById('sigStatus').textContent='Signature saved'; state.tool='signature'; toolbarBtns.forEach(x=>x.classList.remove('tool-active')); document.querySelector('[data-tool="signature"]')?.classList.add('tool-active');});

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

HELP_HTML = r"""<!doctype html><title>Mini PDF Editor — Help</title><body style="background:#0b1020;color:#e7ecff;font-family:system-ui,Segoe UI,Arial;padding:24px"><h2>How to use</h2><ol><li>Upload a PDF.</li><li>Choose a tool then drag on the page.</li><li>Click an item to select. Use the small <b>Delete / Duplicate</b> toolbar by the selection, or press <b>Delete</b>.</li><li>Undo/Redo at any time. Save to write into the PDF. Download.</li><li>Server rollback restores the previous saved version.</li></ol><p><a href="/" style="color:#9cf">Back to editor</a></p></body>"""

SHORTCUTS_HTML = r"""<!doctype html><title>Shortcuts</title><body style="background:#0b1020;color:#e7ecff;font-family:system-ui,Segoe UI,Arial;padding:24px"><h2>Keyboard shortcuts</h2><ul><li>H/S/R/C/L/A/I/T/G — select tool</li><li>Ctrl/⌘+Z Undo, Ctrl/⌘+Y Redo</li><li>Delete/Backspace — delete selected</li><li>Esc — clear selection</li></ul><p><a href="/" style="color:#9cf">Back to editor</a></p></body>"""

@app.get("/help")
def help_page():
    return render_template_string(HELP_HTML)

@app.get("/shortcuts")
def shortcuts_page():
    return render_template_string(SHORTCUTS_HTML)

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
    DOCS[doc_id] = {
        "name": filename,
        "original": original,
        "working": working,
        "versions": [working],
        "created": datetime.utcnow().isoformat(),
    }
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

            if t in ("highlight", "strikeout", "shape_rect", "shape_circle", "textbox", "signature", "tick", "cross"):
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
                font = a.get("font", "helv")
                size = float(a.get("font_size", 14))

                min_text_height = max(16.0, size * 1.6)
                if rect.height < min_text_height:
                    rect = fitz.Rect(rect.x0, rect.y0, rect.x1, min(page_rect.y1, rect.y0 + min_text_height))

                text_rgb = _color_tuple(a.get("color", [0, 0, 0]))
                # Use text_color when creating the annotation (avoid set_colors(text=...))
                annot = page.add_freetext_annot(rect, content, fontsize=size, fontname=font, text_color=text_rgb)
                annot.set_border(width=0)
                annot.update()

            elif t == "signature":
                img_bytes = _decode_data_url(a.get("image_data_url"))
                if img_bytes:
                    page.insert_image(rect, stream=img_bytes, keep_proportion=True)

            elif t == "tick":
                x0,y0,x1,y1 = rect.x0, rect.y0, rect.x1, rect.y1
                pA = fitz.Point(x0 + (x1-x0)*0.1, y0 + (y1-y0)*0.6)
                pB = fitz.Point(x0 + (x1-x0)*0.4, y1 - (y1-y0)*0.1)
                pC = fitz.Point(x1 - (x1-x0)*0.1, y0 + (y1-y0)*0.15)
                annot = page.add_polyline_annot([pA,pB,pC])
                annot.set_colors(stroke=_color_tuple(a.get("color")))
                annot.set_border(width=float(a.get("thickness", 2)))
                annot.update()

            elif t == "cross":
                p1 = fitz.Point(rect.x0, rect.y0); p2 = fitz.Point(rect.x1, rect.y1)
                p3 = fitz.Point(rect.x1, rect.y0); p4 = fitz.Point(rect.x0, rect.y1)
                ann1 = page.add_line_annot(p1, p2); ann2 = page.add_line_annot(p3, p4)
                for ann in (ann1, ann2):
                    ann.set_colors(stroke=_color_tuple(a.get("color")))
                    ann.set_border(width=float(a.get("thickness", 2)))
                    ann.update()

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

