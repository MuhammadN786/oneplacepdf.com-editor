import os, io, uuid, shutil, time, json
from typing import List
from flask import (
    Flask, request, redirect, url_for, send_file, abort,
    render_template_string, jsonify, session
)
import fitz  # PyMuPDF
from PIL import Image

# ----------------- Basic setup -----------------
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")

TMP_DIR = "/tmp/pdfedit"
os.makedirs(TMP_DIR, exist_ok=True)

# In-memory map: doc_id -> file path (ephemeral, fine for 1 worker)
OPEN_DOCS = {}

# ----------------- HTML (Bootstrap) -----------------
BASE_HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>OnePlacePDF Editor</title>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
  <style>
    body{ background:#0b1020; color:#eaf2ff }
    .navbar{ background:#0e1530 }
    .btn-primary{ background:#2563eb; border-color:#2563eb }
    .thumb{ border:1px solid #1f2a44; border-radius:6px; background:#0f172a }
    .thumb img{ width:100%; display:block; border-radius:6px }
    .page-tile{ cursor:pointer; transition:transform .07s }
    .page-tile:hover{ transform:translateY(-2px) }
    .page-tile.selected{ outline:2px solid #22c55e; outline-offset:2px }
    .sidebar{ position:sticky; top:1rem }
    code{ color:#9cd2ff }
  </style>
</head>
<body>

<nav class="navbar navbar-expand-lg mb-4">
  <div class="container">
    <a class="navbar-brand text-white" href="/">OnePlacePDF <span class="text-secondary">Editor</span></a>
  </div>
</nav>

<div class="container">
  {% block body %}{% endblock %}
</div>

<script>
  // helpers
  function qs(sel, root=document){return root.querySelector(sel)}
  function qsa(sel, root=document){return [...root.querySelectorAll(sel)]}
</script>
</body>
</html>
"""

INDEX_HTML = r"""
{% extends "base.html" %}
{% block body %}
<div class="row justify-content-center">
  <div class="col-lg-8">
    <div class="card text-bg-dark border-0 shadow">
      <div class="card-body p-5">
        <h1 class="h3 mb-3">Edit PDF files online</h1>
        <p class="text-secondary mb-4">Rotate, delete, reorder pages, or add quick text overlays. Private – files auto-delete soon after editing.</p>

        <form method="post" action="{{ url_for('upload') }}" enctype="multipart/form-data" class="d-flex gap-3 align-items-center">
          <input class="form-control form-control-lg" type="file" name="file" accept="application/pdf" required>
          <button class="btn btn-primary btn-lg">Upload</button>
        </form>

        <hr class="my-4">
        <p class="small text-secondary mb-0">
          Tip: For large documents, edits happen server-side using <code>PyMuPDF</code> for speed and fidelity.
        </p>
      </div>
    </div>
  </div>
</div>
{% endblock %}
"""

WORK_HTML = r"""
{% extends "base.html" %}
{% block body %}
<div class="row g-4">
  <div class="col-lg-3">
    <div class="sidebar">
      <div class="card text-bg-dark border-0 shadow mb-3">
        <div class="card-body">
          <h2 class="h5 mb-3">Document</h2>
          <div class="small text-secondary">File: {{ filename }}</div>
          <div class="small text-secondary">Pages: <span id="pageCount">{{ page_count }}</span></div>
          <div class="d-grid mt-3 gap-2">
            <a class="btn btn-outline-light" href="{{ url_for('download_pdf', doc_id=doc_id) }}" target="_blank">Open PDF</a>
            <a class="btn btn-success" href="{{ url_for('export_pdf', doc_id=doc_id) }}">Download</a>
          </div>
        </div>
      </div>

      <div class="card text-bg-dark border-0 shadow">
        <div class="card-body">
          <h2 class="h5 mb-3">Actions on selection</h2>
          <div class="btn-group d-flex gap-2 flex-wrap">
            <button class="btn btn-primary flex-fill" onclick="rotate(90)">Rotate 90°</button>
            <button class="btn btn-primary flex-fill" onclick="rotate(270)">Rotate -90°</button>
            <button class="btn btn-warning flex-fill" onclick="deletePages()">Delete</button>
          </div>

          <hr class="my-3">
          <h2 class="h6">Reorder</h2>
          <p class="small text-secondary mb-2">Drag to re-order, then apply.</p>
          <div class="d-flex gap-2">
            <button class="btn btn-outline-light btn-sm" onclick="enableReorder(true)">Start reorder</button>
            <button class="btn btn-success btn-sm" onclick="applyReorder()">Apply</button>
            <button class="btn btn-outline-secondary btn-sm" onclick="enableReorder(false)">Cancel</button>
          </div>

          <hr class="my-3">
          <h2 class="h6">Add quick text</h2>
          <div class="small text-secondary mb-2">Adds text at top-left of each selected page.</div>
          <input id="txtText" class="form-control form-control-sm mb-2" placeholder="Text">
          <div class="row g-2">
            <div class="col-6"><input id="txtSize" type="number" class="form-control form-control-sm" value="14" min="6" max="64"></div>
            <div class="col-6"><input id="txtColor" type="color" class="form-control form-control-sm" value="#000000"></div>
          </div>
          <button class="btn btn-outline-light btn-sm mt-2" onclick="addText()">Add text</button>
        </div>
      </div>

      <div class="card text-bg-dark border-0 shadow mt-3">
        <div class="card-body">
          <h2 class="h6">Merge another PDF</h2>
          <input id="mergeInput" type="file" accept="application/pdf" class="form-control form-control-sm mb-2">
          <button class="btn btn-outline-light btn-sm" onclick="merge()">Merge</button>
        </div>
      </div>

    </div>
  </div>

  <div class="col-lg-9">
    <div class="d-flex justify-content-between align-items-center mb-2">
      <h2 class="h5 mb-0">Pages</h2>
      <div class="small text-secondary">Click to select. Hold Ctrl/Cmd to multi-select.</div>
    </div>

    <div id="grid" class="row g-3"></div>
  </div>
</div>

<script>
const DOC_ID = "{{ doc_id }}";
let pageCount = {{ page_count }};
let reorderMode = false;

function api(url, data){
  return fetch(url, {
    method:"POST", headers:{'Content-Type':'application/json'},
    body: JSON.stringify(data||{})
  }).then(r=>r.json());
}

function selectedPages(){
  return qsa('.page-tile.selected').map(el=>parseInt(el.dataset.page));
}

function buildGrid(){
  const grid = qs('#grid'); grid.innerHTML='';
  for(let p=1;p<=pageCount;p++){
    const col = document.createElement('div');
    col.className = 'col-6 col-md-4 col-lg-3';
    col.innerHTML = `
      <div class="thumb page-tile" data-page="${p}" draggable="${reorderMode}">
        <img loading="lazy" src="/preview/${DOC_ID}/${p}?t=${Date.now()}">
        <div class="p-2 small text-center text-secondary">Page ${p}</div>
      </div>`;
    grid.appendChild(col);
  }
  qsa('.page-tile').forEach(tile=>{
    tile.addEventListener('click', e=>{
      if(!reorderMode){
        if(e.metaKey||e.ctrlKey){ tile.classList.toggle('selected') }
        else{
          qsa('.page-tile.selected').forEach(t=>t.classList.remove('selected'));
          tile.classList.add('selected');
        }
      }
    });

    tile.addEventListener('dragstart', e=>{
      if(!reorderMode) return;
      e.dataTransfer.setData('text/plain', tile.dataset.page);
      tile.classList.add('selected');
    });
    tile.addEventListener('dragover', e=>{ if(reorderMode){ e.preventDefault(); } });
    tile.addEventListener('drop', e=>{
      if(!reorderMode) return;
      e.preventDefault();
      const from = parseInt(e.dataTransfer.getData('text/plain'));
      const to = parseInt(tile.dataset.page);
      reorderDom(from, to);
    });
  });
}

function reorderDom(from, to){
  // re-number the tiles visually
  const tiles = qsa('.page-tile');
  const order = tiles.map(t=>parseInt(t.dataset.page));
  const idxFrom = order.indexOf(from);
  const idxTo = order.indexOf(to);
  order.splice(idxTo,0, order.splice(idxFrom,1)[0]);
  // rebuild grid with new order labels
  pageCount = order.length;
  const grid = qs('#grid'); grid.innerHTML='';
  order.forEach(p=>{
    const col = document.createElement('div');
    col.className = 'col-6 col-md-4 col-lg-3';
    col.innerHTML = `
      <div class="thumb page-tile" data-page="${p}" draggable="${reorderMode}">
        <img loading="lazy" src="/preview/${DOC_ID}/${p}?t=${Date.now()}">
        <div class="p-2 small text-center text-secondary">Page ${p}</div>
      </div>`;
    grid.appendChild(col);
  });
  // reattach handlers
  buildGrid();
}

function enableReorder(on){
  reorderMode = !!on;
  buildGrid();
}

function refresh(){
  return fetch(`/api/info/${DOC_ID}`).then(r=>r.json()).then(j=>{
    pageCount = j.page_count;
    qs('#pageCount').textContent = pageCount;
    buildGrid();
  });
}

async function rotate(angle){
  const pages = selectedPages();
  if(pages.length===0){ alert('Select page(s) first'); return; }
  await api(`/op/rotate/${DOC_ID}`, {pages, angle});
  await refresh();
}

async function deletePages(){
  const pages = selectedPages();
  if(pages.length===0){ alert('Select page(s) first'); return; }
  if(!confirm('Delete selected pages?')) return;
  await api(`/op/delete/${DOC_ID}`, {pages});
  await refresh();
}

async function applyReorder(){
  const order = qsa('.page-tile').map(el=>parseInt(el.dataset.page));
  await api(`/op/reorder/${DOC_ID}`, {order});
  reorderMode=false;
  await refresh();
}

async function addText(){
  const pages = selectedPages();
  if(pages.length===0){ alert('Select page(s) first'); return; }
  const text = qs('#txtText').value.trim();
  if(!text){ alert('Enter some text'); return; }
  const size = parseInt(qs('#txtSize').value||'14');
  const color = qs('#txtColor').value; // "#rrggbb"
  await api(`/op/text/${DOC_ID}`, {pages, text, size, color});
  await refresh();
}

async function merge(){
  const inp = qs('#mergeInput');
  if(!inp.files.length) return alert('Choose a PDF to merge');
  const fd = new FormData();
  fd.append('file', inp.files[0]);
  const r = await fetch(`/op/merge/${DOC_ID}`, {method:'POST', body: fd});
  const j = await r.json();
  if(j.ok) { await refresh(); inp.value=''; }
  else alert(j.error||'Merge failed');
}

buildGrid();
</script>
{% endblock %}
"""

# ----------------- Template registration -----------------
@app.context_processor
def inject_templates():
    return {}

@app.route("/")
def index():
    return render_template_string(INDEX_HTML,)

@app.route("/upload", methods=["POST"])
def upload():
    f = request.files.get("file")
    if not f or not f.filename.lower().endswith(".pdf"):
        return redirect(url_for("index"))
    doc_id = uuid.uuid4().hex
    dest = os.path.join(TMP_DIR, f"{doc_id}.pdf")
    f.save(dest)
    OPEN_DOCS[doc_id] = dest
    session["last_doc"] = doc_id
    return redirect(url_for("work", doc_id=doc_id))

@app.route("/work/<doc_id>")
def work(doc_id):
    path = OPEN_DOCS.get(doc_id)
    if not path or not os.path.exists(path):
        abort(404)
    with fitz.open(path) as doc:
        page_count = doc.page_count
    filename = os.path.basename(path)
    return render_template_string(
        WORK_HTML, doc_id=doc_id, page_count=page_count, filename=filename
    )

# ----------------- Utility functions -----------------
def _path(doc_id: str) -> str:
    p = OPEN_DOCS.get(doc_id)
    if not p or not os.path.exists(p):
        abort(404)
    return p

def _as_list(pages: List[int], page_count: int) -> List[int]:
    # pages are 1-based from UI, validate & convert to 0-based unique ascending
    uniq = sorted({p for p in pages if 1 <= int(p) <= page_count})
    return [p-1 for p in uniq]

def _atomic_save(modifier, src: str):
    """Open PDF, apply modifier(doc), save atomically."""
    tmp_out = f"{src}.new.pdf"
    with fitz.open(src) as doc:
        modifier(doc)
        doc.save(tmp_out, deflate=True)
    os.replace(tmp_out, src)

# ----------------- Info & preview -----------------
@app.route("/api/info/<doc_id>")
def info(doc_id):
    path = _path(doc_id)
    with fitz.open(path) as doc:
        return jsonify({"page_count": doc.page_count})

@app.route("/preview/<doc_id>/<int:page>")
def preview(doc_id, page: int):
    path = _path(doc_id)
    with fitz.open(path) as doc:
        if not (1 <= page <= doc.page_count): abort(404)
        pix = doc[page-1].get_pixmap(dpi=90, alpha=False)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    bio = io.BytesIO()
    img.save(bio, format="PNG", optimize=True)
    bio.seek(0)
    return send_file(bio, mimetype="image/png", max_age=0)

@app.route("/pdf/<doc_id>")
def download_pdf(doc_id):
    path = _path(doc_id)
    return send_file(path, mimetype="application/pdf")

@app.route("/export/<doc_id>")
def export_pdf(doc_id):
    path = _path(doc_id)
    fn = f"oneplacepdf_{doc_id}.pdf"
    return send_file(path, as_attachment=True, download_name=fn, mimetype="application/pdf")

# ----------------- Operations -----------------
@app.route("/op/rotate/<doc_id>", methods=["POST"])
def op_rotate(doc_id):
    path = _path(doc_id)
    body = request.get_json(force=True, silent=True) or {}
    angle = int(body.get("angle", 90)) % 360
    with fitz.open(path) as d: page_count = d.page_count
    pages = _as_list(body.get("pages", []), page_count)

    def modifier(doc):
        for p in pages:
            pg = doc[p]
            new = (pg.rotation + angle) % 360
            pg.set_rotation(new)

    _atomic_save(modifier, path)
    return jsonify(ok=True)

@app.route("/op/delete/<doc_id>", methods=["POST"])
def op_delete(doc_id):
    path = _path(doc_id)
    body = request.get_json(force=True, silent=True) or {}
    with fitz.open(path) as d: page_count = d.page_count
    pages = _as_list(body.get("pages", []), page_count)

    def modifier(doc):
        # delete_pages accepts ranges string or list (1-based)
        keep = [i for i in range(doc.page_count) if i not in pages]
        doc.select(keep)

    _atomic_save(modifier, path)
    return jsonify(ok=True)

@app.route("/op/reorder/<doc_id>", methods=["POST"])
def op_reorder(doc_id):
    path = _path(doc_id)
    body = request.get_json(force=True, silent=True) or {}
    order_1based = body.get("order", [])
    with fitz.open(path) as d: page_count = d.page_count

    # UI sends an order based on current page labels (1..N). Convert to 0-based.
    order = [p-1 for p in order_1based if 1 <= p <= page_count]
    if len(order) != page_count:
        return jsonify(ok=False, error="Invalid order length"), 400

    def modifier(doc):
        doc.select(order)

    _atomic_save(modifier, path)
    return jsonify(ok=True)

def hex_to_rgb(hx: str):
    hx = hx.lstrip("#")
    return tuple(int(hx[i:i+2], 16)/255 for i in (0,2,4))

@app.route("/op/text/<doc_id>", methods=["POST"])
def op_text(doc_id):
    path = _path(doc_id)
    body = request.get_json(force=True, silent=True) or {}
    text = (body.get("text") or "").strip()
    size = int(body.get("size") or 14)
    color = hex_to_rgb(body.get("color") or "#000000")

    with fitz.open(path) as d: page_count = d.page_count
    pages = _as_list(body.get("pages", []), page_count)
    if not text:
        return jsonify(ok=False, error="Empty text"), 400

    def modifier(doc):
        for p in pages:
            pg = doc[p]
            # add at 36pt from top-left; use text insertion (device units)
            pg.insert_text((36, 36+size), text, fontsize=size, color=color)

    _atomic_save(modifier, path)
    return jsonify(ok=True)

@app.route("/op/merge/<doc_id>", methods=["POST"])
def op_merge(doc_id):
    path = _path(doc_id)
    f = request.files.get("file")
    if not f or not f.filename.lower().endswith(".pdf"):
        return jsonify(ok=False, error="Upload a PDF"), 400
    tmp = os.path.join(TMP_DIR, f"merge-{uuid.uuid4().hex}.pdf")
    f.save(tmp)

    def modifier(doc):
        with fitz.open(tmp) as other:
            doc.insert_pdf(other)

    try:
        _atomic_save(modifier, path)
        return jsonify(ok=True)
    finally:
        try: os.remove(tmp)
        except: pass

# ----------------- Jinja base registration -----------------
@app.before_request
def _ensure_templates():
    # Register base once per process
    app.jinja_env.globals["bootstrap_loaded"] = True
    app.jinja_loader = app.create_global_jinja_loader()
    app.jinja_env.from_string(BASE_HTML).stream().dump  # touch to ensure env ok

@app.route("/__templates__/base.html")
def _base_ref():
    return render_template_string(BASE_HTML)

# Tell Jinja how to find base.html from the string
from jinja2 import DictLoader
app.jinja_loader = DictLoader({"base.html": BASE_HTML})

# --------------- Health ---------------
@app.get("/healthz")
def health():
    return "ok", 200

# --------------- Local run ---------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
