# app.py — Single-file Flask Mini PDF Editor (PyMuPDF + Bootstrap)
# Now includes: Signature tool (draw/upload), Highlight, Strikeout, Shapes, Arrow/Line,
# Freehand Ink, Text Box, Per-page Zoom, Thumbnails, Client Undo/Redo, Server Rollback,
# Download, Optional S3 storage. Ready for Render / Gunicorn.
#
# Start locally:  python app.py  -> http://localhost:8000
# Render Start Command (recommended): gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120

import io, os, uuid
from datetime import datetime
from pathlib import Path
from flask import Flask, request, jsonify, send_file, render_template_string
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
import fitz  # PyMuPDF

# ──────────────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────────────
load_dotenv()
app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET", "dev-secret")
# app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # optional: 100MB

BASE_DIR = Path(__file__).resolve().parent
WORK_DIR = Path(os.getenv("WORK_DIR", BASE_DIR / "work"))
WORK_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_EXT = {".pdf"}

# Optional S3
USE_S3 = os.getenv("USE_S3", "false").lower() == "true"
if USE_S3:
    import boto3
    S3_BUCKET = os.getenv("S3_BUCKET")
    S3_REGION = os.getenv("S3_REGION")
    s3 = boto3.client("s3", region_name=S3_REGION)
else:
    s3 = None
    S3_BUCKET = None

# In-memory doc index (swap for DB in production)
DOCS = {}  # {doc_id: {name, original, working, versions[], created}}


# ──────────────────────────────────────────────────────────────────────────────
# Storage Abstraction
# ──────────────────────────────────────────────────────────────────────────────
class Storage:
    @staticmethod
    def save(file_bytes: bytes, key: str):
        if USE_S3:
            s3.put_object(Bucket=S3_BUCKET, Key=key, Body=file_bytes, ContentType="application/pdf")
            return key
        path = WORK_DIR / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(file_bytes)
        return str(path)

    @staticmethod
    def get(key: str) -> bytes:
        if USE_S3:
            obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
            return obj["Body"].read()
        return (WORK_DIR / key).read_bytes()


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────
def _allowed(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_EXT

def _color_tuple(rgb_list):
    # Expect [r,g,b] 0-255; default yellow (highlight)
    if not rgb_list:
        return (1.0, 1.0, 0.0)
    r, g, b = [max(0, min(255, int(c))) / 255.0 for c in rgb_list[:3]]
    return (r, g, b)


# ──────────────────────────────────────────────────────────────────────────────
# Inline Frontend (Bootstrap + Canvas overlay + Signature modal)
# ──────────────────────────────────────────────────────────────────────────────
INDEX_HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Mini PDF Editor — Annotations + Signature</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet" />
  <style>
    body { background:#0b1020; color:#e7ecff; }
    .toolbar .btn { border-radius:999px; }
    #thumbs { max-height:80vh; overflow:auto; }
    #canvasWrap { position:relative; background:#fff; }
    #overlay { position:absolute; left:0; top:0; pointer-events:none; }
    .tool-active { outline:2px solid #6ea8fe; }
    img.page { display:block; max-width:100%; height:auto; }
    /* Signature Modal */
    .sig-canvas { background:#fff; border:1px dashed #999; border-radius:8px; touch-action:none; }
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
    </div>

    <div class="col-9">
      <div class="card bg-dark border-secondary mb-3">
        <div class="card-body toolbar d-flex flex-wrap gap-2 align-items-center">
          <button class="btn btn-light" data-tool="highlight">Highlight</button>
          <button class="btn btn-light" data-tool="strikeout">Strikeout</button>
          <button class="btn btn-light" data-tool="rect">Rect</button>
          <button class="btn btn-light" data-tool="circle">Circle</button>
          <button class="btn btn-light" data-tool="arrow">Arrow</button>
          <button class="btn btn-light" data-tool="line">Line</button>
          <button class="btn btn-light" data-tool="ink">Freehand</button>
          <button class="btn btn-light" data-tool="textbox">Text Box</button>
          <button class="btn btn-primary" data-tool="signature">Signature</button>

          <div class="vr"></div>
          <label class="text-nowrap">Color</label>
          <input id="color" type="color" value="#ffeb3b" class="form-control form-control-color" />
          <label class="text-nowrap ms-2">Thickness</label>
          <input id="thickness" type="range" min="1" max="12" value="2" class="form-range w-25" />
          <div class="vr"></div>
          <button class="btn btn-success" id="btnSave">Save Edits</button>
          <button class="btn btn-outline-light" id="btnUndo">Undo</button>
          <button class="btn btn-outline-light" id="btnRedo">Redo</button>
          <div class="vr"></div>
          <label class="text-nowrap">Zoom</label>
          <input id="zoom" type="range" min="0.6" max="2.5" step="0.1" value="1.2" class="form-range w-25" />
        </div>
      </div>

      <div id="canvasWrap" class="rounded-3 shadow">
        <img id="pageImg" class="page" src="" />
        <canvas id="overlay"></canvas>
      </div>
    </div>
  </div>
</div>

<!-- Signature Modal -->
<div class="modal fade" id="sigModal" tabindex="-1" aria-labelledby="sigModalLabel" aria-hidden="true">
  <div class="modal-dialog modal-dialog-centered" style="max-width:720px;">
    <div class="modal-content bg-dark text-light border-secondary">
      <div class="modal-header">
        <h5 class="modal-title" id="sigModalLabel">Create Your Signature</h5>
        <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="Close"></button>
      </div>
      <div class="modal-body">
        <p class="mb-2">Draw your signature, or upload an image (PNG/JPG with transparent/white background).</p>
        <div class="d-flex gap-3 align-items-start">
          <canvas id="sigCanvas" width="600" height="220" class="sig-canvas"></canvas>
          <div class="d-flex flex-column gap-2" style="min-width: 120px;">
            <button id="sigClear" class="btn btn-outline-light">Clear</button>
            <input id="sigUpload" type="file" class="form-control" accept="image/png,image/jpeg" />
          </div>
        </div>
      </div>
      <div class="modal-footer">
        <small class="text-secondary me-auto">Tip: Use a trackpad or phone to draw for best results.</small>
        <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
        <button id="sigSave" type="button" class="btn btn-primary" data-bs-dismiss="modal">Save Signature</button>
      </div>
    </div>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
<script>
(() => {
  let docId = null;
  let currentPage = 0;
  let totalPages = 0;
  let zoom = 1.2;

  const state = {
    tool: null,
    color: '#ffeb3b',
    thickness: 2,
    stack: [],  // local actions (pending save)
    redo: [],
    drawing: false,
    stroke: [],
    signatureDataURL: null,     // saved signature image (dataURL)
  };

  const el = (id) => document.getElementById(id);
  const toolbarBtns = [...document.querySelectorAll('[data-tool]')];

  toolbarBtns.forEach((b) => {
    b.addEventListener('click', () => {
      toolbarBtns.forEach((x) => x.classList.remove('tool-active'));
      b.classList.add('tool-active');
      state.tool = b.getAttribute('data-tool');
      if (state.tool === 'signature' && !state.signatureDataURL) {
        openSignatureModal();
      }
    });
  });

  el('color').addEventListener('input', (e) => state.color = e.target.value);
  el('thickness').addEventListener('input', (e) => state.thickness = parseInt(e.target.value));
  el('zoom').addEventListener('input', (e) => { zoom = parseFloat(e.target.value); renderPage(); });

  el('file').addEventListener('change', async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    const fd = new FormData();
    fd.append('file', f);
    const r = await fetch('/upload', { method: 'POST', body: fd });
    const j = await r.json();
    if (!r.ok) return alert(j.error || 'Upload failed');
    docId = j.doc_id;
    await loadThumbs();
    await renderPage(0);
  });

  el('btnDownload').addEventListener('click', () => {
    if (!docId) return;
    window.location.href = '/download/' + docId;
  });

  el('btnUndoServer').addEventListener('click', async () => {
    if (!docId) return;
    const r = await fetch('/revert/' + docId, { method: 'POST' });
    const j = await r.json();
    if (!j.ok) return alert(j.error || 'Rollback failed');
    state.stack = [];
    state.redo = [];
    await renderPage();
  });

  el('btnUndo').addEventListener('click', () => {
    if (!state.stack.length) return;
    state.redo.push(state.stack.pop());
    redrawOverlay();
  });

  el('btnRedo').addEventListener('click', () => {
    if (!state.redo.length) return;
    state.stack.push(state.redo.pop());
    redrawOverlay();
  });

  el('btnSave').addEventListener('click', async () => {
    if (!docId || !state.stack.length) return;
    // Prepare payload: for stamp_image actions, send hex instead of dataURL
    const actions = state.stack.map(a => {
      if (a.type === 'stamp_image' && a.imageDataURL) {
        return { ...a, image_hex: dataURLToHex(a.imageDataURL), imageDataURL: undefined };
      }
      return a;
    });
    const r = await fetch('/annotate/' + docId, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ actions }),
    });
    const j = await r.json();
    if (!j.ok) return alert(j.error || 'Save failed');
    state.stack = [];
    state.redo = [];
    await renderPage();
  });

  async function loadThumbs() {
    const r = await fetch('/thumbs/' + docId);
    const j = await r.json();
    if (!r.ok) return alert(j.error || 'Thumbs failed');
    totalPages = j.pages;
    const wrap = el('thumbs');
    wrap.innerHTML = '';
    for (let i = 0; i < totalPages; i++) {
      const img = document.createElement('img');
      img.src = `/thumb/${docId}/${i}`;
      img.className = 'img-fluid mb-2 rounded';
      img.style.cursor = 'pointer';
      img.addEventListener('click', () => renderPage(i));
      wrap.appendChild(img);
    }
  }

  const pageImg = el('pageImg');
  const overlay = el('overlay');

  function syncOverlaySize() {
    overlay.width = pageImg.clientWidth;
    overlay.height = pageImg.clientHeight;
    overlay.style.width = pageImg.clientWidth + 'px';
    overlay.style.height = pageImg.clientHeight + 'px';
  }
  window.addEventListener('resize', syncOverlaySize);

  async function renderPage(p = currentPage) {
    if (docId == null) return;
    currentPage = p;
    pageImg.src = `/page/${docId}/${currentPage}?zoom=${zoom}`;
    await new Promise((res) => pageImg.onload = res);
    syncOverlaySize();
    redrawOverlay();
  }

  function hexToRgb(hex) {
    const n = parseInt(hex.slice(1), 16);
    return [(n>>16)&255, (n>>8)&255, n&255];
  }

  // Convert dataURL -> hex string (for server)
  function dataURLToHex(dataURL) {
    const binStr = atob(dataURL.split(',')[1]);
    let hex = '';
    for (let i = 0; i < binStr.length; i++) {
      const h = binStr.charCodeAt(i).toString(16).padStart(2, '0');
      hex += h;
    }
    return hex;
  }

  function redrawOverlay() {
    const ctx = overlay.getContext('2d');
    ctx.clearRect(0,0,overlay.width, overlay.height);
    for (const a of state.stack) {
      if (a.page !== currentPage) continue;
      drawLocal(ctx, a);
    }
    if (state.drawing && state.tool === 'ink' && state.stroke.length) {
      ctx.lineWidth = state.thickness;
      ctx.strokeStyle = state.color;
      ctx.beginPath();
      ctx.moveTo(state.stroke[0][0], state.stroke[0][1]);
      for (let i=1; i<state.stroke.length; i++) ctx.lineTo(state.stroke[i][0], state.stroke[i][1]);
      ctx.stroke();
    }
  }

  // cache images for local preview of stamps/signatures
  const imgCache = new Map();
  function drawStamp(ctx, a) {
    const r = a.rect;
    const w = r[2]-r[0], h = r[3]-r[1];
    if (a.imageDataURL) {
      let img = imgCache.get(a.imageDataURL);
      if (!img) {
        img = new Image();
        img.onload = () => { redrawOverlay(); };
        img.src = a.imageDataURL;
        imgCache.set(a.imageDataURL, img);
      }
      if (img.complete) {
        ctx.drawImage(img, r[0], r[1], w, h);
        return;
      }
    }
    // fallback: draw a placeholder box
    ctx.save();
    ctx.strokeStyle = '#999';
    ctx.setLineDash([6,4]);
    ctx.strokeRect(r[0], r[1], w, h);
    ctx.restore();
  }

  function drawLocal(ctx, a) {
    ctx.save();
    ctx.strokeStyle = a.colorHex || '#000';
    ctx.lineWidth = a.thickness || 2;
    if (a.type === 'highlight' || a.type === 'strikeout') {
      const r = a.rect;
      ctx.globalAlpha = a.opacity || (a.type==='highlight' ? 0.35 : 0.25);
      ctx.fillStyle = a.colorHex || '#ff0';
      ctx.fillRect(r[0], r[1], r[2]-r[0], r[3]-r[1]);
      ctx.globalAlpha = 1.0;
    } else if (a.type === 'shape_rect') {
      const r = a.rect; ctx.strokeRect(r[0], r[1], r[2]-r[0], r[3]-r[1]);
    } else if (a.type === 'shape_circle') {
      const r = a.rect; const cx=(r[0]+r[2])/2, cy=(r[1]+r[3])/2; const rx=(r[2]-r[0])/2, ry=(r[3]-r[1])/2;
      ctx.beginPath(); ctx.ellipse(cx,cy,rx,ry,0,0,Math.PI*2); ctx.stroke();
    } else if (a.type === 'arrow' || a.type === 'line') {
      const [p1,p2] = a.points; ctx.beginPath(); ctx.moveTo(p1[0],p1[1]); ctx.lineTo(p2[0],p2[1]); ctx.stroke();
      if (a.type === 'arrow') {
        const ang = Math.atan2(p2[1]-p1[1], p2[0]-p1[0]);
        const len = 10 + (a.thickness||2)*1.5;
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
    } else if (a.type === 'stamp_image') {
      drawStamp(ctx, a);
    }
    ctx.restore();
  }

  function wrapText(ctx, text, x, y, maxWidth, lineHeight) {
    const words = text.split(' ');
    let line = '';
    for (let n = 0; n < words.length; n++) {
      const testLine = line + words[n] + ' ';
      const testWidth = ctx.measureText(testLine).width;
      if (testWidth > maxWidth && n > 0) {
        ctx.fillText(line, x, y);
        line = words[n] + ' ';
        y += lineHeight;
      } else {
        line = testLine;
      }
    }
    ctx.fillText(line, x, y);
  }

  // Mouse interactions on overlay
  let start = null;
  overlay.addEventListener('mousedown', (e) => {
    if (!state.tool) return;
    if (state.tool === 'signature' && !state.signatureDataURL) {
      openSignatureModal();
      return;
    }
    state.drawing = true;
    const {x,y} = rel(e);
    start = [x,y];
    if (state.tool === 'ink') state.stroke = [[x,y]];
    overlay.style.pointerEvents = 'auto';
  });

  overlay.addEventListener('mousemove', (e) => {
    if (!state.drawing) return;
    const {x,y} = rel(e);
    if (state.tool === 'ink') {
      state.stroke.push([x,y]);
      redrawOverlay();
      return;
    }
    redrawOverlay();
    const ctx = overlay.getContext('2d');
    const preview = previewAction(start, [x,y]);
    // for stamp preview, draw a dashed box
    if (preview.type === 'stamp_image') {
      ctx.save();
      ctx.setLineDash([6,4]);
      const r = preview.rect; ctx.strokeStyle = '#bbb';
      ctx.strokeRect(r[0], r[1], r[2]-r[0], r[3]-r[1]);
      ctx.restore();
    } else {
      drawLocal(ctx, preview);
    }
  });

  overlay.addEventListener('mouseup', (e) => {
    if (!state.drawing) return;
    state.drawing = false;
    overlay.style.pointerEvents = 'none';
    const {x,y} = rel(e);
    if (state.tool === 'ink') {
      const action = {
        type: 'ink', page: currentPage,
        points: [state.stroke],
        color: hexToRgb(state.color), colorHex: state.color,
        thickness: state.thickness,
      };
      state.stack.push(action); state.redo = [];
      redrawOverlay();
      state.stroke = [];
      return;
    }
    const a = previewAction(start, [x,y]);
    if (state.tool === 'textbox') {
      a.text = prompt('Text content?') || '';
    }
    // For signature / stamp_image, attach dataURL for local preview
    if (a.type === 'stamp_image') {
      a.imageDataURL = state.signatureDataURL;
    }
    state.stack.push(a); state.redo = [];
    redrawOverlay();
  });

  function rel(e) {
    const r = overlay.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  }

  function previewAction(p1, p2) {
    const color = hexToRgb(state.color);
    const base = { page: currentPage, color, colorHex: state.color, thickness: state.thickness };
    if (state.tool === 'highlight' || state.tool === 'strikeout' || state.tool === 'rect' || state.tool ===
