# app.py — Single-file Flask Mini PDF Editor with Annotations (PyMuPDF + Bootstrap)
# Features: upload, per-page PNG render (zoom), thumbnails, highlight, strikeout, shapes (rect/circle/line/arrow*),
# freehand ink, text boxes, client undo/redo (pre-save), server rollback (version history), download, optional S3 storage.
# *Arrowheads best-effort (PyMuPDF line ends; safely ignored if not supported in your version).

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
    # Expect [r,g,b] 0-255; default yellow for highlights
    if not rgb_list:
        return (1.0, 1.0, 0.0)
    r, g, b = [max(0, min(255, int(c))) / 255.0 for c in rgb_list[:3]]
    return (r, g, b)


# ──────────────────────────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────────────────────────
INDEX_HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Mini PDF Editor — Annotations</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet" />
  <style>
    body { background:#0b1020; color:#e7ecff; }
    .toolbar .btn { border-radius:999px; }
    #thumbs { max-height:80vh; overflow:auto; }
    #canvasWrap { position:relative; background:#fff; padding:0; }
    #overlay { position:absolute; left:0; top:0; pointer-events:none; }
    .tool-active { outline:2px solid #6ea8fe; }
    img.page { display:block; max-width:100%; height:auto; }
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
        <img id="pageImg" class="page" src="" />
        <canvas id="overlay"></canvas>
      </div>
    </div>
  </div>
</div>

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
  };

  const el = (id) => document.getElementById(id);
  const toolbarBtns = [...document.querySelectorAll('[data-tool]')];

  toolbarBtns.forEach((b) => {
    b.addEventListener('click', () => {
      toolbarBtns.forEach((x) => x.classList.remove('tool-active'));
      b.classList.add('tool-active');
      state.tool = b.getAttribute('data-tool');
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
    const payload = { actions: state.stack };
    const r = await fetch('/annotate/' + docId, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
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
    state.drawing = true;
    const {x,y} = rel(e);
    start = [x,y];
    if (state.tool === 'ink') state.stroke = [[x,y]];
    // enable drawing through pointer events
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
    // preview
    redrawOverlay();
    const ctx = overlay.getContext('2d');
    drawLocal(ctx, previewAction(start, [x,y]));
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
    // Special case: textbox asks once
    if (state.tool === 'textbox') {
      a.text = prompt('Text content?') || '';
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
    if (state.tool === 'highlight' || state.tool === 'strikeout' || state.tool === 'rect' || state.tool === 'circle' || state.tool === 'textbox') {
      const rect = [Math.min(p1[0],p2[0]), Math.min(p1[1],p2[1]), Math.max(p1[0],p2[0]), Math.max(p1[1],p2[1])];
      if (state.tool === 'highlight') return { ...base, type:'highlight', rect, opacity:0.35 };
      if (state.tool === 'strikeout') return { ...base, type:'strikeout', rect, opacity:0.25 };
      if (state.tool === 'rect') return { ...base, type:'shape_rect', rect };
      if (state.tool === 'circle') return { ...base, type:'shape_circle', rect };
      if (state.tool === 'textbox') return { ...base, type:'textbox', rect, font_size:14, text:'' };
    }
    if (state.tool === 'arrow' || state.tool === 'line') {
      return { ...base, type: state.tool, points: [p1, p2] };
    }
    return base;
  }

})();
</script>
</body>
</html>
"""

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
        "name": filename,
        "original": key_original,
        "working": key_working,
        "versions": [key_working],
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
    bio = io.BytesIO(pix.tobytes("png"))
    bio.seek(0)
    return send_file(bio, mimetype="image/png")

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
    bio = io.BytesIO(pix.tobytes("png"))
    bio.seek(0)
    return send_file(bio, mimetype="image/png")

@app.post("/annotate/<doc_id>")
def annotate(doc_id):
    if doc_id not in DOCS:
        return jsonify({"error": "doc not found"}), 404
    data = request.get_json(force=True) or {}
    actions = data.get("actions", [])
    if not actions:
        return jsonify({"ok": True, "message": "nothing to do"})
    pdf = fitz.open(stream=Storage.get(DOCS[doc_id]["working"]), filetype="pdf")

    for a in actions:
        t = a.get("type")
        page = pdf[a["page"]]
        if t == "highlight":
            rect = fitz.Rect(*a["rect"])
            annot = page.add_highlight_annot(rect)
            annot.set_colors(stroke=_color_tuple(a.get("color")))
            if "opacity" in a: annot.set_opacity(float(a["opacity"]))
            annot.update()

        elif t == "strikeout":
            rect = fitz.Rect(*a["rect"])
            annot = page.add_strikeout_annot(rect)
            annot.set_colors(stroke=_color_tuple(a.get("color")))
            if "opacity" in a: annot.set_opacity(float(a["opacity"]))
            annot.update()

        elif t == "shape_rect":
            rect = fitz.Rect(*a["rect"])
            annot = page.add_rect_annot(rect)
            annot.set_border(width=float(a.get("thickness", 2)))
            annot.set_colors(stroke=_color_tuple(a.get("color")))
            annot.update()

        elif t == "shape_circle":
            rect = fitz.Rect(*a["rect"])
            annot = page.add_circle_annot(rect)
            annot.set_border(width=float(a.get("thickness", 2)))
            annot.set_colors(stroke=_color_tuple(a.get("color")))
            annot.update()

        elif t in ("line", "arrow"):
            p1, p2 = a["points"][0], a["points"][1]
            annot = page.add_line_annot(fitz.Point(*p1), fitz.Point(*p2))
            annot.set_border(width=float(a.get("thickness", 2)))
            annot.set_colors(stroke=_color_tuple(a.get("color")))
            # Try arrowheads if available; ignore if not supported in current PyMuPDF
            if t == "arrow":
                try:
                    # Newer PyMuPDF: pass names; older: may require integer codes
                    annot.set_line_ends(("OpenArrow", "None"))
                except Exception:
                    pass
            annot.update()

        elif t == "ink":
            # points: list of strokes; each stroke is list of [x,y]
            strokes = []
            for stroke in a["points"]:
                strokes.append([fitz.Point(*pt) for pt in stroke])
            annot = page.add_ink_annot(strokes)
            annot.set_colors(stroke=_color_tuple(a.get("color")))
            annot.set_border(width=float(a.get("thickness", 2)))
            annot.update()

        elif t == "textbox":
            rect = fitz.Rect(*a["rect"])
            content = a.get("text", "")
            annot = page.add_freetext_annot(rect, content)
            annot.set_colors(stroke=_color_tuple(a.get("color", [0,0,0])))
            try:
                annot.set_font("helv", float(a.get("font_size", 14)))
            except Exception:
                pass
            annot.update()

        # You can add more types here (watermark_text, stamp_image, form_fill) later.

    out = io.BytesIO()
    pdf.save(out)
    out.seek(0)
    new_key = f"{doc_id}/{uuid.uuid4().hex}.pdf"
    Storage.save(out.read(), new_key)
    DOCS[doc_id]["working"] = new_key
    DOCS[doc_id]["versions"].append(new_key)
    return jsonify({"ok": True, "version": len(DOCS[doc_id]['versions'])})

@app.post("/revert/<doc_id>")
def revert(doc_id):
    if doc_id not in DOCS:
        return jsonify({"error": "doc not found"}), 404
    vers = DOCS[doc_id]["versions"]
    if len(vers) < 2:
        return jsonify({"error": "no previous version"}), 400
    vers.pop()  # drop current
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

# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Run: python app.py
    # Then open http://localhost:8000
    app.run(host="0.0.0.0", port=8000, debug=True)
