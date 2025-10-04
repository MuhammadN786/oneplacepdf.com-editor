# app.py — Mini PDF Editor (Flask + PyMuPDF)
# Fixes:
#  - Annotate always returns JSON (no HTML error pages)
#  - Highlight / Strikeout have safe fallbacks for image-only pages
#  - Client robustly parses JSON (or shows server text) to avoid "Unexpected token <"
#  - Signature place-after-save UX preserved

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
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # upload cap

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

class Storage:
    @staticmethod
    def save(file_bytes: bytes, key: str) -> str:
        if USE_S3:
            s3.put_object(Bucket=S3_BUCKET, Key=key, Body=file_bytes, ContentType="application/pdf")
            return key
        path = WORK_DIR / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(file_bytes)
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

# ─── helpers ─────────────────────────────────────────────────────
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

@app.after_request
def add_no_cache(resp):
    if request.path.startswith(("/thumb/", "/page/")):
        resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp

# ─── UI (inline) ─────────────────────────────────────────────────
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
    stack: [], redo: [],
    drawing: false, stroke: [],
    signatureDataURL: null,
  };

  const el = (id) => document.getElementById(id);
  const toolbarBtns = [...document.querySelectorAll('[data-tool]')];
  const pageImg = el('pageImg');
  const overlay = el('overlay');

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
    const btn = document.querySelector(`[data-tool="${map[k]}"]`);
    if (btn) btn.click();
    if ((e.ctrlKey||e.metaKey) && k==='z') el('btnUndo').click();
    if ((e.ctrlKey||e.metaKey) && k==='y') el('btnRedo').click();
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
      await loadThumbs();
      await renderPage(0);
    } catch (err) { alert('Upload error: ' + err.message); console.error(err); }
  });

  // Download / Rollback
  el('btnDownload').addEventListener('click', () => { if (docId) window.location.href = '/download/' + docId; });
  el('btnUndoServer').addEventListener('click', async () => {
    if (!docId) return;
    try {
      const r = await fetch('/revert/' + docId, { method: 'POST' });
      const j = await parseMaybeJSON(r);
      if (!j.ok) throw new Error(j.error || 'Rollback failed');
      state.stack = []; state.redo = [];
      await renderPage();
    } catch (err) { alert('Rollback error: ' + err.message); console.error(err); }
  });

  // Local undo/redo
  el('btnUndo').addEventListener('click', () => { if (state.stack.length) { state.redo.push(state.stack.pop()); redrawOverlay(); } });
  el('btnRedo').addEventListener('click', () => { if (state.redo.length) { state.stack.push(state.redo.pop()); redrawOverlay(); } });

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
      state.stack = []; state.redo = [];
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
    overlay.style.left = '0px';
    overlay.style.top  = '0px';
  }
  window.addEventListener('resize', syncOverlaySize);

  async function renderPage(p = currentPage) {
    if (docId == null) return;
    currentPage = p;
    pageImg.src = `/page/${docId}/${currentPage}?zoom=${zoom}`;
    await new Promise((res, rej) => { pageImg.onload = res; pageImg.onerror = () => rej(new Error('Failed to load page image')); });
    syncOverlaySize(); redrawOverlay();
  }

  function hexToRgb(hex) { const n = parseInt(hex.slice(1), 16); return [(n>>16)&255, (n>>8)&255, n&255]; }

  function redrawOverlay() {
    const ctx = overlay.getContext('2d');
    ctx.clearRect(0,0,overlay.width, overlay.height);
    for (const a of state.stack) if (a.page === currentPage) drawLocal(ctx, a);
    if (state.drawing && state.tool === 'ink' && state.stroke.length) {
      ctx.lineWidth = state.thickness; ctx.strokeStyle = state.color;
      ctx.beginPath(); ctx.moveTo(state.stroke[0][0], state.stroke[0][1]);
      for (let i=1; i<state.stroke.length; i++) ctx.lineTo(state.stroke[i][0], state.stroke[i][1]);
      ctx.stroke();
    }
  }

  function drawLocal(ctx, a) {
    ctx.save(); ctx.strokeStyle = a.colorHex || '#000'; ctx.lineWidth = a.thickness || 2;
    if (a.type === 'highlight' || a.type === 'strikeout') {
      const r = a.rect; ctx.globalAlpha = a.opacity || (a.type==='highlight' ? 0.35 : 0.25);
      ctx.fillStyle = a.colorHex || '#ff0'; ctx.fillRect(r[0], r[1], r[2]-r[0], r[3]-r[1]); ctx.globalAlpha = 1.0;
    } else if (a.type === 'shape_rect') {
      const r = a.rect; ctx.strokeRect(r[0], r[1], r[2]-r[0], r[3]-r[1]);
    } else if (a.type === 'shape_circle') {
      const r = a.rect; const cx=(r[0]+r[2])/2, cy=(r[1]+r[3])/2; const rx=(r[2]-r[0])/2, ry=(r[3]-r[1])/2;
      ctx.beginPath(); ctx.ellipse(cx,cy,rx,ry,0,0,Math.PI*2); ctx.stroke();
    } else if (a.type === 'line' || a.type === 'arrow') {
      const [p1,p2] = a.points; ctx.beginPath(); ctx.moveTo(p1[0],p1[1]); ctx.lineTo(p2[0],p2[1]); ctx.stroke();
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

  // Interactions
  let start = null;
  overlay.addEventListener('mousedown', (e) => { if (!state.tool) return; state.drawing = true; const {x,y}=rel(e); start=[x,y]; if (state.tool==='ink') state.stroke=[[x,y]]; });
  overlay.addEventListener('mousemove', (e) => {
    if (!state.drawing) return;
    const {x,y} = rel(e);
    if (state.tool === 'ink') { state.stroke.push([x,y]); redrawOverlay(); return; }
    redrawOverlay(); const ctx = overlay.getContext('2d'); drawLocal(ctx, previewAction(start,[x,y],true));
  });
  overlay.addEventListener('mouseup', (e) => {
    if (!state.drawing) return; state.drawing=false; const {x,y}=rel(e);
    if (state.tool === 'ink') { state.stack.push({ type:'ink', page: currentPage, points:[state.stroke], color:hexToRgb(state.color), colorHex:state.color, thickness:state.thickness }); state.redo=[]; redrawOverlay(); state.stroke=[]; return; }
    const a = previewAction(start,[x,y],false); if (!a) return; state.stack.push(a); state.redo=[]; redrawOverlay();
  });
  function rel(e){ const r=overlay.getBoundingClientRect(); return {x:e.clientX-r.left, y:e.clientY-r.top}; }

  function previewAction(p1,p2,forPreview){
    const color = hexToRgb(state.color);
    const base = { page: currentPage, color, colorHex: state.color, thickness: state.thickness };
    const rect = [Math.min(p1[0],p2[0]), Math.min(p1[1],p2[1]), Math.max(p1[0],p2[0]), Math.max(p1[1],p2[1])];
    if (state.tool==='highlight') return { ...base, type:'highlight', rect, opacity:0.35 };
    if (state.tool==='strikeout') return { ...base, type:'strikeout', rect, opacity:0.25 };
    if (state.tool==='rect') return { ...base, type:'shape_rect', rect };
    if (state.tool==='circle') return { ...base, type:'shape_circle', rect };
    if (state.tool==='textbox'){ const text = forPreview ? '' : (prompt('Text content?') || ''); return { ...base, type:'textbox', rect, font_size:14, text }; }
    if (state.tool==='line' || state.tool==='arrow') return { ...base, type: state.tool, points:[p1,p2] };
    if (state.tool==='signature'){ if (!state.signatureDataURL){ if(!forPreview) alert('Open Signature and draw your signature first.'); return null; } return { ...base, type:'signature', rect, previewDataURL: state.signatureDataURL, image_data_url: state.signatureDataURL }; }
    return base;
  }

  // Signature pad
  const sigPad = document.getElementById('sigPad'); const sigCtx = sigPad.getContext('2d');
  sigCtx.lineWidth=2; sigCtx.lineCap='round'; sigCtx.strokeStyle='#111';
  let sigDraw=false, last=null;
  function sigPos(e){ const r=sigPad.getBoundingClientRect(); const t=e.touches?.[0]||e; return {x:t.clientX-r.left,y:t.clientY-r.top}; }
  function sigStart(e){ sigDraw=true; last=sigPos(e); e.preventDefault(); }
  function sigMove(e){ if(!sigDraw) return; const p=sigPos(e); sigCtx.beginPath(); sigCtx.moveTo(last.x,last.y); sigCtx.lineTo(p.x,p.y); sigCtx.stroke(); last=p; e.preventDefault(); }
  function sigEnd(){ sigDraw=false; }
  sigPad.addEventListener('mousedown',sigStart); sigPad.addEventListener('mousemove',sigMove); window.addEventListener('mouseup',sigEnd);
  sigPad.addEventListener('touchstart',sigStart,{passive:false}); sigPad.addEventListener('touchmove',sigMove,{passive:false}); sigPad.addEventListener('touchend',sigEnd);
  document.getElementById('sigClear').addEventListener('click',()=>{ sigCtx.clearRect(0,0,sigPad.width,sigPad.height); document.getElementById('sigStatus').textContent='Not set'; state.signatureDataURL=null; });
  document.getElementById('sigUse').addEventListener('click',()=>{ state.signatureDataURL=sigPad.toDataURL('image/png'); document.getElementById('sigStatus').textContent='Signature saved'; state.tool='signature'; toolbarBtns.forEach(x=>x.classList.remove('tool-active')); document.querySelector('[data-tool="signature"]')?.classList.add('tool-active');});

  function syncOverlaySize(){ overlay.width=Math.max(pageImg.clientWidth||1,1); overlay.height=Math.max(pageImg.clientHeight||1,1); overlay.style.width=overlay.width+'px'; overlay.style.height=overlay.height+'px'; }
  window.addEventListener('resize',syncOverlaySize);
  async function renderPage(p=currentPage){ if(docId==null) return; currentPage=p; pageImg.src=`/page/${docId}/${currentPage}?zoom=${zoom}`; await new Promise((res,rej)=>{ pageImg.onload=res; pageImg.onerror=()=>rej(new Error('Failed to load page image'));}); syncOverlaySize(); redrawOverlay(); }

})();
</script>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
"""

# ─── routes ──────────────────────────────────────────────────────
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
    key_original = f"{doc_id}/original.pdf"
    Storage.save(f.read(), key_original)
    key_working = f"{doc_id}/working.pdf"
    Storage.save(Storage.get(key_original), key_working)
    DOCS[doc_id] = {
        "name": filename, "original": key_original, "working": key_working,
        "versions": [key_working], "created": datetime.utcnow().isoformat(),
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

            if t == "highlight":
                rect = _scale_rect(a["rect"], page_rect, viewport)
                try:
                    annot = page.add_highlight_annot(rect)  # may fail on image-only PDFs
                    annot.set_colors(stroke=_color_tuple(a.get("color")))
                    if "opacity" in a: annot.set_opacity(float(a["opacity"]))
                    annot.update()
                except Exception:
                    # fallback: translucent filled rectangle
                    annot = page.add_rect_annot(rect)
                    annot.set_colors(stroke=None, fill=_color_tuple(a.get("color")))
                    annot.set_opacity(float(a.get("opacity", 0.35)))
                    annot.update()

            elif t == "strikeout":
                rect = _scale_rect(a["rect"], page_rect, viewport)
                try:
                    annot = page.add_strikeout_annot(rect)  # may fail on image-only PDFs
                    annot.set_colors(stroke=_color_tuple(a.get("color")))
                    if "opacity" in a: annot.set_opacity(float(a["opacity"]))
                    annot.update()
                except Exception:
                    # fallback: center line
                    y = (rect.y0 + rect.y1) / 2
                    p1 = fitz.Point(rect.x0, y)
                    p2 = fitz.Point(rect.x1, y)
                    annot = page.add_line_annot(p1, p2)
                    annot.set_border(width=float(a.get("thickness", 2)))
                    annot.set_colors(stroke=_color_tuple(a.get("color")))
                    annot.update()

            elif t == "shape_rect":
                rect = _scale_rect(a["rect"], page_rect, viewport)
                annot = page.add_rect_annot(rect)
                annot.set_border(width=float(a.get("thickness", 2)))
                annot.set_colors(stroke=_color_tuple(a.get("color")))
                annot.update()

            elif t == "shape_circle":
                rect = _scale_rect(a["rect"], page_rect, viewport)
                annot = page.add_circle_annot(rect)
                annot.set_border(width=float(a.get("thickness", 2)))
                annot.set_colors(stroke=_color_tuple(a.get("color")))
                annot.update()

            elif t in ("line", "arrow"):
                p1 = _scale_point(a["points"][0], page_rect, viewport)
                p2 = _scale_point(a["points"][1], page_rect, viewport)
                annot = page.add_line_annot(p1, p2)
                annot.set_border(width=float(a.get("thickness", 2)))
                annot.set_colors(stroke=_color_tuple(a.get("color")))
                if t == "arrow":
                    try:
                        annot.set_line_ends(("OpenArrow", "None"))
                    except Exception:
                        pass
                annot.update()

            elif t == "ink":
                strokes = [[_scale_point(pt, page_rect, viewport) for pt in stroke] for stroke in a["points"]]
                annot = page.add_ink_annot(strokes)
                annot.set_colors(stroke=_color_tuple(a.get("color")))
                annot.set_border(width=float(a.get("thickness", 2)))
                annot.update()

            elif t == "textbox":
                rect = _scale_rect(a["rect"], page_rect, viewport)
                content = a.get("text", "")
                annot = page.add_freetext_annot(rect, content)
                annot.set_colors(stroke=_color_tuple(a.get("color", [0,0,0])))
                try:
                    annot.set_font("helv", float(a.get("font_size", 14)))
                except Exception:
                    pass
                annot.update()

            elif t == "signature":
                rect = _scale_rect(a["rect"], page_rect, viewport)
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
        # Always return JSON on failure
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
