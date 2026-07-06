"""
Tiny FastAPI app to try the centerline extractor in the browser: upload a PNG of a solid
shape, get back the centerline SVG.

Run:
    pip install -r webapp/requirements.txt
    pip install -e .            # or: pip install centerline-svg
    uvicorn webapp.app:app --reload
    # open http://127.0.0.1:8000
"""

from __future__ import annotations

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, Response

import centerline_svg

app = FastAPI(title="Centerline SVG", version=centerline_svg.__version__)


@app.post("/convert")
async def convert(
    file: UploadFile = File(...),
    threshold: int = Form(128),
    stroke_width: str = Form(""),
) -> Response:
    """Accept a PNG upload, return the centerline as an image/svg+xml document."""
    if file.content_type not in ("image/png", "application/octet-stream") and not (
        file.filename or ""
    ).lower().endswith(".png"):
        raise HTTPException(status_code=415, detail="please upload a PNG file")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    try:
        sw = float(stroke_width) if stroke_width.strip() else None
        svg = centerline_svg.png_to_svg(data, threshold=threshold, stroke_width=sw)
    except Exception as exc:  # bad PNG / unsupported format
        raise HTTPException(status_code=400, detail="could not process image: %s" % exc)
    return Response(content=svg, media_type="image/svg+xml")


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return INDEX_HTML


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Centerline SVG</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 15px/1.5 system-ui, sans-serif; margin: 0; padding: 24px;
         max-width: 1000px; margin-inline: auto; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  p.sub { color: #888; margin: 0 0 20px; }
  .controls { display: flex; flex-wrap: wrap; gap: 16px; align-items: center;
              margin-bottom: 20px; }
  label { display: inline-flex; flex-direction: column; font-size: 12px; color: #888; gap: 4px; }
  input[type=number] { width: 90px; }
  button { font: inherit; padding: 8px 16px; border-radius: 8px; border: 1px solid #999;
           background: #111; color: #fff; cursor: pointer; }
  button:disabled { opacity: .5; cursor: default; }
  .panes { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  .pane { border: 1px solid #8883; border-radius: 12px; padding: 12px; min-height: 260px;
          display: flex; flex-direction: column; }
  .pane h2 { font-size: 12px; color: #888; margin: 0 0 8px; text-transform: uppercase;
             letter-spacing: .05em; }
  .stage { flex: 1; display: grid; place-items: center; overflow: hidden; }
  .stage img, .stage svg { max-width: 100%; max-height: 380px; }
  #drop { border: 2px dashed #8886; border-radius: 12px; padding: 28px; text-align: center;
          color: #888; cursor: pointer; margin-bottom: 20px; }
  #drop.hover { border-color: #4a90e2; color: #4a90e2; }
  a.dl { font-size: 13px; margin-top: 8px; }
  .err { color: #d33; font-size: 13px; }
</style>
</head>
<body>
  <h1>Centerline&nbsp;→&nbsp;SVG</h1>
  <p class="sub">Upload a PNG of a solid dark shape on a light background. It returns the
     medial-axis centerline as a stroked SVG.</p>

  <div id="drop">Drop a PNG here, or click to choose a file</div>
  <input id="file" type="file" accept="image/png" hidden>

  <div class="controls">
    <label>Threshold (0–255)
      <input id="threshold" type="number" min="0" max="255" value="128">
    </label>
    <label>Stroke width (blank = auto)
      <input id="stroke" type="number" min="1" step="1" placeholder="auto">
    </label>
    <button id="go" disabled>Convert</button>
    <span id="msg" class="err"></span>
  </div>

  <div class="panes">
    <div class="pane"><h2>Source PNG</h2><div class="stage" id="src"></div></div>
    <div class="pane"><h2>Centerline SVG</h2>
      <div class="stage" id="out"></div>
      <a id="dl" class="dl" style="display:none" download="centerline.svg">Download SVG</a>
    </div>
  </div>

<script>
const $ = s => document.querySelector(s);
const drop = $('#drop'), fileInput = $('#file'), go = $('#go'), msg = $('#msg');
let currentFile = null;

function pickFile(f) {
  if (!f) return;
  currentFile = f;
  go.disabled = false;
  const reader = new FileReader();
  reader.onload = e => { $('#src').innerHTML = '<img src="' + e.target.result + '">'; };
  reader.readAsDataURL(f);
  $('#out').innerHTML = ''; $('#dl').style.display = 'none'; msg.textContent = '';
}

drop.onclick = () => fileInput.click();
fileInput.onchange = () => pickFile(fileInput.files[0]);
drop.ondragover = e => { e.preventDefault(); drop.classList.add('hover'); };
drop.ondragleave = () => drop.classList.remove('hover');
drop.ondrop = e => { e.preventDefault(); drop.classList.remove('hover'); pickFile(e.dataTransfer.files[0]); };

go.onclick = async () => {
  if (!currentFile) return;
  go.disabled = true; msg.textContent = ''; $('#out').innerHTML = 'converting…';
  const fd = new FormData();
  fd.append('file', currentFile);
  fd.append('threshold', $('#threshold').value || '128');
  fd.append('stroke_width', $('#stroke').value || '');
  try {
    const res = await fetch('/convert', { method: 'POST', body: fd });
    if (!res.ok) { const j = await res.json().catch(()=>({detail:res.statusText}));
                   throw new Error(j.detail || 'error'); }
    const svg = await res.text();
    $('#out').innerHTML = svg;
    const url = URL.createObjectURL(new Blob([svg], { type: 'image/svg+xml' }));
    const dl = $('#dl'); dl.href = url; dl.style.display = 'inline';
  } catch (e) {
    $('#out').innerHTML = ''; msg.textContent = e.message;
  } finally { go.disabled = false; }
};
</script>
</body>
</html>
"""
