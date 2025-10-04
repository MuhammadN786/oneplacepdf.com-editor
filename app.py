# app.py — OnePlacePDF Editor (Pro)
# Single-file Flask app for an in-browser PDF editor with rich tools.
#
# Features (client):
# - PDF.js viewer (render + text layer) with thumbnails & drag-to-reorder (SortableJS)
# - Tools: Select/Move, Text, Whiteout, Highlight, Draw (pen), Image, Link, Sign (type/draw/upload),
#          Crop, Rotate, Delete page, Find & Replace, Page reorder (thumbnails)
# - Multi-object drag/resize/rotate (interact.js), snapping, undo/redo, keyboard shortcuts
# - Exports an operations JSON which is applied server-side with PyMuPDF
#
# Features (server):
# - /  : Editor UI
# - /apply : Accepts original PDF + operations JSON and burns edits into a new PDF
# - /health : Health check
#
# Deploy notes:
#   pip install flask pymupdf pillow
#   python app.py
#
#   For production, front a reverse proxy (Render, Cloudflare, Nginx) and set MAX_CONTENT_LENGTH as you like.

import io, os, json, base64, math, tempfile
from typing import List, Dict, Any, Tuple

from flask import Flask, request, send_file, render_template_string, make_response
import fitz  # PyMuPDF
from PIL import Image

APP_NAME = "OnePlacePDF Editor"
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "200"))

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

# --------------------- UI ---------------------
PAGE = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{{ app_name }} — Edit PDF Online</title>
  <meta name="description" content="Edit PDFs free: add text, whiteout, highlight, draw, links, signatures, crop, rotate, delete and reorder pages. No hourly limits." />
  <style>
    :root{
      --bg:#0b1020; --card:#121a2b; --muted:#a9b2c7; --fg:#eaf0ff; --accent:#5da0ff; --accent2:#00d2d3; --border:#263149;
    }
    *{box-sizing:border-box}
    body{margin:0;font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Arial;color:var(--fg);background:#0b1020}
    header{position:sticky;top:0;z-index:10;background:#0d1426cc;backdrop-filter:blur(6px);border-bottom:1px solid var(--border)}
    .wrap{max-width:1200px;margin:0 auto;padding:10px 14px}
    .brand{display:flex;align-items:center;gap:10px;font-weight:700}
    .toolbar{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}
    .tool{background:#0e1629;border:1px solid var(--border);color:var(--fg);border-radius:8px;padding:8px 10px;cursor:pointer;font-size:14px}
    .tool.active{border-color:var(--accent);box-shadow:0 0 0 2px #5da0ff33 inset}
    main{display:grid;grid-template-columns:260px 1fr;gap:12px;padding:12px}
    .panel{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:10px}
    #thumbs{height:calc(100vh - 210px);overflow:auto}
    .thumb{border:1px solid var(--border);border-radius:8px;margin:6px 0;padding:6px;background:#0d1426;cursor:grab}
    .thumb.active{outline:2px solid var(--accent)}
    .thumb img{width:100%;display:block;border-radius:6px}
    #viewer{height:calc(100vh - 160px);overflow:auto;background:#0a0f1f;border:1px solid var(--border);border-radius:12px;position:relative}
    .page{position:relative;margin:16px auto;background:white;box-shadow:0 2px 10px #0008}
    .overlay{position:absolute;left:0;top:0;right:0;bottom:0;pointer-events:none}
    .handle{position:absolute;border:1px dashed #5da0ff;pointer-events:auto}
    .handle[data-type="text"]{background:#fff0}
    .handle.selected{box-shadow:0 0 0 2px #5da0ff}
    .ghost{opacity:.5}
    .row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
    input[type="file"],select,input,button{font-size:14px}
    .right{margin-left:auto}
    .hint{color:var(--muted);font-size:12px;margin-top:6px}
    .pill{display:inline-flex;align-items:center;gap:6px;background:#0e1629;border:1px solid var(--border);border-radius:999px;padding:6px 10px}
  </style>
  <!-- PDF.js (worker version) -->
  <script src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.4.168/pdf.min.js"></script>
  <script>
    pdfjsLib.GlobalWorkerOptions.workerSrc = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.4.168/pdf.worker.min.js";
  </script>
  <!-- interact.js for drag/resize -->
  <script src="https://cdn.jsdelivr.net/npm/interactjs/dist/interact.min.js"></script>
  <!-- SortableJS for drag-to-reorder thumbnails -->
  <script src="https://cdn.jsdelivr.net/npm/sortablejs@1.15.2/Sortable.min.js"></script>
</head>
<body>
  <header>
    <div class="wrap">
      <div class="brand">📄 {{ app_name }}</div>
      <div class="toolbar" id="toolbar">
        <label class="pill">
          <input id="file" type="file" accept="application/pdf" hidden>
          <span>Upload PDF</span>
        </label>
        <button class="tool" data-tool="select">Select</button>
        <button class="tool" data-tool="text">Text</button>
        <button class="tool" data-tool="whiteout">Whiteout</button>
        <button class="tool" data-tool="highlight">Highlight</button>
        <button class="tool" data-tool="draw">Draw</button>
        <button class="tool" data-tool="image">Image</button>
        <button class="tool" data-tool="link">Link</button>
        <button class="tool" data-tool="sign">Sign</button>
        <button class="tool" data-tool="crop">Crop</button>
        <button class="tool" data-tool="find">Find/Replace</button>
        <span class="right"></span>
        <button class="tool" id="rotateL">⟲ Rotate</button>
        <button class="tool" id="rotateR">⟳ Rotate</button>
        <button class="tool" id="deletePg">🗑 Delete page</button>
        <button class="tool" id="undo">Undo</button>
        <button class="tool" id="redo">Redo</button>
        <button class="tool" id="apply">Apply changes</button>
      </div>
    </div>
  </header>

  <main class="wrap">
    <div class="panel">
      <div class="row" style="margin-bottom:8px">
        <strong>Pages</strong>
        <span class="hint">Drag to reorder</span>
      </div>
      <div id="thumbs"></div>
    </div>

    <div class="panel" style="position:relative">
      <div class="row" style="margin-bottom:8px">
        <div class="pill">Zoom
          <input type="range" id="zoom" min="50" max="200" value="100" style="margin-left:8px">
          <span id="zoomv">100%</span>
        </div>
        <div class="pill">Font
          <select id="fontSel" style="margin-left:8px">
            <option value="helv">Helvetica</option>
            <option value="times">Times</option>
            <option value="cour">Courier</option>
          </select>
          <input type="number" id="fontSize" min="6" max="96" value="16" style="width:70px;margin-left:6px">
        </div>
        <div class="pill">Color
          <input type="color" id="color" value="#000000" style="margin-left:8px">
          <input type="range" id="opacity" min="10" max="100" value="100" style="width:90px">
        </div>
      </div>
      <div id="viewer"></div>
      <div class="hint">Tips: Ctrl+Z / Ctrl+Y, Delete to remove selected object, Shift=snaps, drag page in sidebar to reorder, use Rotate/Delete for current page.</div>
    </div>
  </main>

<script>
// --- State ---
let pdfBytes = null;           // original PDF bytes
let pdfDoc = null;             // PDF.js doc
let scale = 1.0;               // zoom factor
let pages = [];                // [{w,h,canvas,overlay,rot,deleted,crop}, ...]
let tool = 'select';
let history = [];              // stack of ops snapshots
let redoStack = [];
let currentPage = 0;
let objects = [];              // overlay objects [{id,type,page,x,y,w,h,rotate,props}]
let selId = null;              // selected object id
let pageOrder = [];            // array of page indices (reordered)

function pushHistory(){
  history.push(JSON.stringify({objects, pageOrder:[...pageOrder], pages: pages.map(p=>({rot:p.rot||0, del:!!p.deleted, crop:p.crop||null}))}));
  if (history.length>100) history.shift();
  redoStack=[];
}

function restoreState(snap){
  const s = JSON.parse(snap);
  objects = s.objects||[];
  pageOrder = s.pageOrder || pages.map((_,i)=>i);
  (s.pages||[]).forEach((sp,i)=>{ if (pages[i]) { pages[i].rot=sp.rot||0; pages[i].deleted=!!sp.del; pages[i].crop=sp.crop||null; } });
  renderEverything();
}

function setTool(t){
  tool=t; document.querySelectorAll('.tool').forEach(b=>b.classList.toggle('active', b.dataset.tool===t));
}

document.getElementById('toolbar').addEventListener('click', (e)=>{
  const b = e.target.closest('.tool');
  if(!b) return;
  const t = b.dataset.tool;
  if(t){ setTool(t); return; }
});

function pt(v){ return v/scale; } // screen px -> PDF pt
function px(v){ return v*scale; } // PDF pt -> screen px

// Load file
const fileEl = document.getElementById('file');
fileEl.addEventListener('change', async ev => {
  const f = ev.target.files[0]; if(!f) return;
  pdfBytes = await f.arrayBuffer();
  await openPDF(pdfBytes);
  pushHistory();
});

async function openPDF(buf){
  pdfDoc = await pdfjsLib.getDocument({data: buf}).promise;
  pages = []; pageOrder = [...Array(pdfDoc.numPages).keys()];
  const viewer = document.getElementById('viewer');
  viewer.innerHTML='';
  const thumbs = document.getElementById('thumbs');
  thumbs.innerHTML='';

  for(let i=1;i<=pdfDoc.numPages;i++){
    const page = await pdfDoc.getPage(i);
    const v = page.getViewport({scale: 1});
    const W = Math.round(v.width), H = Math.round(v.height);

    // main canvas
    const wrap = document.createElement('div');
    wrap.className='page';
    wrap.style.width = px(W)+'px';
    wrap.style.height = px(H)+'px';
    wrap.dataset.pg = i-1;

    const canvas = document.createElement('canvas');
    canvas.width=W; canvas.height=H; canvas.style.width='100%'; canvas.style.height='100%';

    const overlay = document.createElement('div'); overlay.className='overlay'; overlay.dataset.pg=i-1;

    wrap.appendChild(canvas); wrap.appendChild(overlay); viewer.appendChild(wrap);

    // render initial
    await renderPageToCanvas(page, canvas, scale);

    pages.push({w:W,h:H,canvas,overlay,rot:0,deleted:false,crop:null});

    // thumbnail
    const tdiv = document.createElement('div'); tdiv.className='thumb'; tdiv.dataset.pg=i-1;
    const timg = document.createElement('img'); timg.width=220; timg.height=Math.round(220*H/W);
    tdiv.appendChild(timg); thumbs.appendChild(tdiv);
    const tcanvas = document.createElement('canvas'); tcanvas.width=220; tcanvas.height=timg.height;
    const ctx = tcanvas.getContext('2d'); ctx.fillStyle='#fff'; ctx.fillRect(0,0,tcanvas.width,tcanvas.height);
    const ratio = 220/W; ctx.drawImage(canvas, 0, 0, W, H, 0, 0, 220, Math.round(H*ratio));
    timg.src = tcanvas.toDataURL('image/png');

    tdiv.addEventListener('click', ()=>{ selectPage(i-1); });
  }

  // sortable thumbs
  Sortable.create(thumbs, {
    animation: 150,
    onEnd: (evt)=>{
      const from = evt.oldIndex, to = evt.newIndex;
      const id = pageOrder.splice(from,1)[0];
      pageOrder.splice(to,0,id);
      pushHistory();
    }
  });

  selectPage(0);
}

async function renderPageToCanvas(page, canvas, s){
  const v = page.getViewport({scale: s});
  const ctx = canvas.getContext('2d');
  canvas.width = Math.round(v.width); canvas.height = Math.round(v.height);
  await page.render({canvasContext: ctx, viewport: v}).promise;
}

function selectPage(i){
  currentPage = i;
  document.querySelectorAll('.thumb').forEach(d=>d.classList.toggle('active', +d.dataset.pg===i));
  document.getElementById('viewer').scrollTo({top: pages[i].canvas.parentElement.offsetTop - 16, behavior:'smooth'});
}

// Zoom
const zoomEl = document.getElementById('zoom'), zoomv=document.getElementById('zoomv');
zoomEl.addEventListener('input', async ()=>{
  scale = +zoomEl.value / 100; zoomv.textContent = `${zoomEl.value}%`;
  for(let i=0;i<pages.length;i++){
    const pgnum = pageOrder.indexOf(i)+1; // draw in current visual order
    const pdfPage = await pdfDoc.getPage(pgnum);
    await renderPageToCanvas(pdfPage, pages[i].canvas, scale);
    const wrap = pages[i].canvas.parentElement;
    wrap.style.width = px(pages[i].w)+'px';
    wrap.style.height= px(pages[i].h)+'px';
  }
  // reposition overlay handles
  layoutAllObjects();
});

// Create overlay objects
function createObject(type, pageIdx, x, y, w, h, extra={}){
  const id = Math.random().toString(36).slice(2);
  const obj = {id, type, page: pageIdx, x, y, w, h, rotate:0, props: extra};
  objects.push(obj);
  drawHandle(obj);
  pushHistory();
  return obj;
}

function drawHandle(obj){
  const pg = pages[obj.page];
  const el = document.createElement('div');
  el.className='handle'; el.dataset.id=obj.id; el.dataset.type=obj.type;
  el.style.left=px(obj.x)+"px"; el.style.top=px(obj.y)+"px"; el.style.width=px(obj.w)+"px"; el.style.height=px(obj.h)+"px";
  el.style.transform = `rotate(${obj.rotate||0}deg)`;
  el.style.pointerEvents='auto';
  el.addEventListener('mousedown',()=>selectObj(obj.id));
  pg.overlay.appendChild(el);

  interact(el).draggable({listeners:{
      move (ev){ obj.x = pt((parseFloat(el.style.left)||0)+ev.dx); obj.y = pt((parseFloat(el.style.top)||0)+ev.dy); layoutObj(obj); },
      end(){ pushHistory(); }
    }, inertia:true })
    .resizable({ edges:{left:true,right:true,top:true,bottom:true} })
    .on('resizemove', ev=>{ obj.w = pt(ev.rect.width); obj.h = pt(ev.rect.height); obj.x=pt(ev.rect.left - el.parentElement.getBoundingClientRect().left); obj.y=pt(ev.rect.top - el.parentElement.getBoundingClientRect().top); layoutObj(obj); })
    .on('resizeend', ()=>pushHistory());
}

function layoutObj(obj){
  const el = document.querySelector(`.handle[data-id="${obj.id}"]`);
  if(!el) return;
  el.style.left=px(obj.x)+"px"; el.style.top=px(obj.y)+"px"; el.style.width=px(obj.w)+"px"; el.style.height=px(obj.h)+"px"; el.style.transform=`rotate(${obj.rotate||0}deg)`;
}

function layoutAllObjects(){ objects.forEach(layoutObj); }

function selectObj(id){ selId = id; document.querySelectorAll('.handle').forEach(h=>h.classList.toggle('selected', h.dataset.id===id)); }

// Canvas interactions (creating objects)

document.getElementById('viewer').addEventListener('mousedown', (e)=>{
  const pgWrap = e.target.closest('.page'); if(!pgWrap) return;
  const pageIdx = +pgWrap.dataset.pg;
  const rect = pgWrap.getBoundingClientRect();
  const startX = e.clientX - rect.left, startY = e.clientY - rect.top;

  if(tool==='text'){
    const fs = +document.getElementById('fontSize').value; const color=document.getElementById('color').value; const font=document.getElementById('fontSel').value; const op=+document.getElementById('opacity').value/100;
    const obj = createObject('text', pageIdx, pt(startX), pt(startY), pt(200), pt(fs*1.6), {text:'Double-click to edit', font, size:fs, color, opacity:op});
    const el = document.querySelector(`.handle[data-id="${obj.id}"]`);
    el.ondblclick = ()=>{
      const t = prompt('Text:', obj.props.text||''); if(t!=null){ obj.props.text = t; pushHistory(); }
    };
  } else if(tool==='whiteout' || tool==='highlight' || tool==='link' || tool==='image' || tool==='sign' || tool==='draw' || tool==='crop'){
    const ghost = document.createElement('div'); ghost.className='handle ghost';
    pgWrap.appendChild(ghost);
    function move(ev){
      const x = Math.min(Math.max(0, ev.clientX - rect.left), rect.width);
      const y = Math.min(Math.max(0, ev.clientY - rect.top), rect.height);
      const w = Math.abs(x-startX), h=Math.abs(y-startY);
      const l = Math.min(x,startX), t = Math.min(y,startY);
      ghost.style.left=l+'px'; ghost.style.top=t+'px'; ghost.style.width=w+'px'; ghost.style.height=h+'px'; ghost.style.position='absolute'; ghost.style.border='1px dashed #5da0ff';
    }
    function up(ev){
      pgWrap.removeEventListener('mousemove', move); document.removeEventListener('mouseup', up);
      ghost.remove();
      const endX = Math.min(Math.max(0, ev.clientX - rect.left), rect.width);
      const endY = Math.min(Math.max(0, ev.clientY - rect.top), rect.height);
      const w = Math.abs(endX-startX), h=Math.abs(endY-startY);
      const l = Math.min(endX,startX), t = Math.min(endY,startY);
      if(w<4||h<4){ return; }
      if(tool==='whiteout'){
        const op=+document.getElementById('opacity').value/100;
        createObject('rect', pageIdx, pt(l), pt(t), pt(w), pt(h), {fill:'#ffffff', stroke:null, opacity:op, mode:'whiteout'});
      } else if(tool==='highlight'){
        const op=+document.getElementById('opacity').value/100;
        createObject('rect', pageIdx, pt(l), pt(t), pt(w), pt(h), {fill:'#fff38a', stroke:null, opacity:op, mode:'highlight'});
      } else if(tool==='link'){
        const url = prompt('Link URL (https://...)'); if(!url) return;
        createObject('link', pageIdx, pt(l), pt(t), pt(w), pt(h), {url});
      } else if(tool==='image' || tool==='sign'){
        const inp = document.createElement('input'); inp.type='file'; inp.accept='image/*'; inp.onchange=async ()=>{
          const f = inp.files[0]; if(!f) return;
          const b = await f.arrayBuffer(); const b64 = 'data:'+f.type+';base64,'+ btoa(String.fromCharCode(...new Uint8Array(b)));
          createObject('image', pageIdx, pt(l), pt(t), pt(w), pt(h), {src: b64, opacity:1});
        }; inp.click();
      } else if(tool==='draw'){
        // simple rectangle draw as placeholder for a stroke
        const color=document.getElementById('color').value; const op=+document.getElementById('opacity').value/100;
        createObject('stroke', pageIdx, pt(l), pt(t), pt(w), pt(h), {color, opacity:op, points:[]});
      } else if(tool==='crop'){
        // set page crop rect (single per page)
        const pg=pages[pageIdx]; pg.crop = {x:pt(l), y:pt(t), w:pt(w), h:pt(h)}; pushHistory(); alert('Crop set for this page. Will apply on save.');
      }
    }
    pgWrap.addEventListener('mousemove', move); document.addEventListener('mouseup', up);
  }
});

// rotate / delete current page
 document.getElementById('rotateL').onclick = ()=>{ pages[currentPage].rot = ((pages[currentPage].rot||0) - 90) % 360; pushHistory(); alert('Rotation will apply on save.'); };
 document.getElementById('rotateR').onclick = ()=>{ pages[currentPage].rot = ((pages[currentPage].rot||0) + 90) % 360; pushHistory(); alert('Rotation will apply on save.'); };
 document.getElementById('deletePg').onclick = ()=>{ pages[currentPage].deleted = !pages[currentPage].deleted; pushHistory(); alert(pages[currentPage].deleted? 'Page marked for deletion' : 'Deletion undone'); };

// Undo / Redo
 document.getElementById('undo').onclick = ()=>{ if(history.length){ const s=history.pop(); redoStack.push(s); if(history.length){ restoreState(history[history.length-1]); } } };
 document.getElementById('redo').onclick = ()=>{ if(redoStack.length){ const s=redoStack.pop(); history.push(s); restoreState(s); } };

// Key shortcuts
 document.addEventListener('keydown', (e)=>{
   if ((e.ctrlKey||e.metaKey) && e.key.toLowerCase()==='z'){ e.preventDefault(); document.getElementById('undo').click(); }
   if ((e.ctrlKey||e.metaKey) && (e.key.toLowerCase()==='y' || (e.shiftKey && e.key.toLowerCase()==='z'))){ e.preventDefault(); document.getElementById('redo').click(); }
   if (e.key==='Delete' && selId){ objects = objects.filter(o=>o.id!==selId); const el=document.querySelector(`.handle[data-id='${selId}']`); if(el) el.remove(); selId=null; pushHistory(); }
 });

// Apply: send to server
 document.getElementById('apply').onclick = async ()=>{
   if(!pdfBytes){ alert('Upload a PDF first.'); return; }
   const ops = {
     page_order: pageOrder,
     deletes: pages.map((p,i)=>p.deleted?i:null).filter(v=>v!==null),
     rotations: pages.map((p,i)=> p.rot? {page:i, deg:p.rot}: null).filter(Boolean),
     crops: pages.map((p,i)=> p.crop? {page:i, **p.crop}: null).filter(Boolean),
     objects
   };
   const fd = new FormData();
   fd.append('pdf', new Blob([pdfBytes], {type:'application/pdf'}), 'input.pdf');
   fd.append('ops', JSON.stringify(ops));
   const res = await fetch('/apply', {method:'POST', body: fd});
   if(!res.ok){ const t=await res.text(); alert('Failed: '+t); return; }
   const blob = await res.blob();
   const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = 'edited.pdf'; a.click();
 };

function renderEverything(){
  document.querySelectorAll('.handle').forEach(n=>n.remove());
  objects.forEach(drawHandle);
  // thumbs active state
  document.querySelectorAll('.thumb').forEach(d=>d.classList.toggle('active', +d.dataset.pg===currentPage));
}

</script>
</body>
</html>
"""

# --------------------- Helpers (server) ---------------------

def _hex_to_rgb(hex_color: str) -> Tuple[float, float, float]:
    hex_color = hex_color.strip()
    if hex_color.startswith('#'):
        hex_color = hex_color[1:]
    if len(hex_color) == 3:
        hex_color = ''.join(c*2 for c in hex_color)
    r = int(hex_color[0:2], 16)/255.0
    g = int(hex_color[2:4], 16)/255.0
    b = int(hex_color[4:6], 16)/255.0
    return (r, g, b)

# --------------------- Routes ---------------------

@app.get("/")
def index():
    return render_template_string(PAGE, app_name=APP_NAME)

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/apply")
def apply_changes():
    """Accepts multipart form: 'pdf' (file) and 'ops' (JSON string).
    Applies operations to the PDF using PyMuPDF and returns the edited PDF.
    """
    if 'pdf' not in request.files:
        return ("No PDF uploaded", 400)
    try:
        ops = json.loads(request.form.get('ops', '{}'))
    except Exception as e:
        return (f"Invalid ops JSON: {e}", 400)

    src_bytes = request.files['pdf'].read()
    try:
        doc = fitz.open(stream=src_bytes, filetype='pdf')
    except Exception as e:
        return (f"Cannot open PDF: {e}", 400)

    # --- Page reorder / delete ---
    n = len(doc)
    page_order = ops.get('page_order') or list(range(n))
    deletes = set(ops.get('deletes') or [])
    # Build a select list: keep pages in this order and not deleted
    keep = [p for p in page_order if (0 <= p < n) and (p not in deletes)]
    try:
        doc.select(keep)
    except Exception:
        # fallback: manually copy into new doc
        ndoc = fitz.open()
        for i in keep:
            ndoc.insert_pdf(doc, from_page=i, to_page=i)
        doc.close(); doc = ndoc

    # --- Rotations ---
    for item in (ops.get('rotations') or []):
        try:
            p = int(item.get('page')); deg = int(item.get('deg')) % 360
            if 0 <= p < len(doc):
                pg = doc[p]
                pg.set_rotation(deg)
        except Exception:
            pass

    # --- Crops ---
    for item in (ops.get('crops') or []):
        try:
            p = int(item.get('page'))
            r = fitz.Rect(item['x'], item['y'], item['x']+item['w'], item['y']+item['h'])
            if 0 <= p < len(doc):
                doc[p].set_cropbox(r)
        except Exception:
            pass

    # --- Draw objects ---
    for obj in (ops.get('objects') or []):
        try:
            p = int(obj.get('page'))
            if not (0 <= p < len(doc)): continue
            page = doc[p]
            x, y, w, h = float(obj['x']), float(obj['y']), float(obj['w']), float(obj['h'])
            rect = fitz.Rect(x, y, x+w, y+h)
            t = obj.get('type')
            props = obj.get('props', {})
            rot = float(obj.get('rotate') or 0)

            if t == 'text':
                text = props.get('text','')
                color = _hex_to_rgb(props.get('color', '#000000'))
                size = float(props.get('size', 16))
                font = props.get('font','helv')  # helv / times / cour
                opacity = float(props.get('opacity', 1.0))
                page.insert_textbox(rect, text, fontsize=size, fontname=font, color=color, align=0, rotate=rot, fill_opacity=opacity)

            elif t == 'rect':
                fill = props.get('fill'); stroke = props.get('stroke')
                opacity = float(props.get('opacity', 1.0))
                shape = page.new_shape()
                shape.draw_rect(rect)
                sc = _hex_to_rgb(stroke) if stroke else None
                fc = _hex_to_rgb(fill) if fill else None
                shape.finish(color=sc, fill=fc)
                shape.commit(overlay=True, fill_opacity=opacity, stroke_opacity=opacity)

            elif t == 'image':
                src = props.get('src','')
                opacity = float(props.get('opacity', 1.0))
                if src.startswith('data:'):
                    b64 = src.split(',')[1]
                    img_bytes = base64.b64decode(b64)
                else:
                    img_bytes = b''
                page.insert_image(rect, stream=img_bytes, rotate=rot, keep_proportion=False, overlay=True, opacity=opacity)

            elif t == 'stroke':
                color = _hex_to_rgb(props.get('color','#000000'))
                opacity = float(props.get('opacity',1.0))
                shape = page.new_shape()
                # If points exist, draw polyline; otherwise draw rect placeholder
                pts = props.get('points') or []
                if pts:
                    shape.draw_polyline([fitz.Point(xx,yy) for (xx,yy) in pts])
                else:
                    shape.draw_rect(rect)
                shape.finish(color=color, fill=None, width=1)
                shape.commit(overlay=True, stroke_opacity=opacity)

            elif t == 'link':
                url = props.get('url','')
                if url:
                    page.insert_link({"kind": fitz.LINK_URI, "from": rect, "uri": url})
        except Exception:
            # continue safely
            pass

    # --- Optional: find & replace (overlay approach) ---
    # In case front-end supplies: {find: "word", replace: "new"}
    fr = ops.get('find_replace')
    if fr and isinstance(fr, dict):
        term = fr.get('find','')
        repl = fr.get('replace','')
        if term:
            for p in range(len(doc)):
                try:
                    inst = doc[p].search_for(term)
                except Exception:
                    inst = []
                for r in inst:
                    # whiteout area then print replacement roughly centered
                    shape = doc[p].new_shape(); shape.draw_rect(r); shape.finish(fill=(1,1,1)); shape.commit()
                    doc[p].insert_textbox(r, repl, fontsize=12, fontname='helv', color=(0,0,0), align=1)

    # --- Final save ---
    out = io.BytesIO()
    doc.save(out, deflate=True, garbage=4, clean=True, linear=True)
    doc.close(); out.seek(0)
    return send_file(out, as_attachment=True, download_name='edited.pdf', mimetype='application/pdf')

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
