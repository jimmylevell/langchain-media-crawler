"""Small web front end for local use and Azure App Service.

A scan takes minutes, longer than App Service's 230 s request limit, so a scan
is started as a background job and the page polls for the result.

    uv run uvicorn scanner.api:app --port 8000
"""
from __future__ import annotations

import threading
import uuid
from datetime import date

import markdown as md
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .config import RunConfig
from .graph import run_scan

app = FastAPI(title="Medien-Scanner für Akquiseimpulse")
jobs: dict[str, dict] = {}  # in memory: fine for one instance, lost on restart


class ScanRequest(BaseModel):
    firma: str = "Treuhand Firma"
    niederlassung: str = "Stans"
    region: str = "Nidwalden, Obwalden und angrenzende Gemeinden"
    kantone: str = "NW,OW"
    tage: int = 7
    kontaktentwurf: bool = False


def _work(job_id: str, cfg: RunConfig) -> None:
    job = jobs[job_id]
    try:
        result = run_scan(cfg, on_event=job["log"].append)
        job.update(status="fertig", **result, html=md.markdown(result["markdown"], extensions=["tables"]))
    except BaseException as e:  # incl. SystemExit from missing configuration
        job.update(status="fehler", error=str(e))


@app.post("/api/scan")
def start_scan(req: ScanRequest) -> dict:
    d = ScanRequest()
    cfg = RunConfig(
        firma=req.firma, niederlassung=req.niederlassung, region=req.region,
        kantone=[k.strip() for k in req.kantone.upper().split(",") if k.strip()],
        tage=req.tage, kontaktentwurf=req.kontaktentwurf, laufdatum=date.today(),
        geplant=(req.niederlassung, req.region, req.tage) == (d.niederlassung, d.region, d.tage),
    )
    job_id = uuid.uuid4().hex
    jobs[job_id] = {"status": "läuft", "log": []}
    threading.Thread(target=_work, args=(job_id, cfg), daemon=True).start()
    return {"id": job_id}


@app.get("/api/scan/{job_id}")
def get_scan(job_id: str) -> dict:
    if job_id not in jobs:
        raise HTTPException(404, "Unbekannter Lauf")
    return jobs[job_id]


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return PAGE


PAGE = """<!doctype html><html lang="de"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Medien-Scanner</title>
<style>
 body{font:15px/1.5 system-ui,sans-serif;max-width:1200px;margin:2rem auto;padding:0 1rem;color:#1c1c1c}
 form{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:.75rem;align-items:end}
 label{display:block;font-size:.85rem;color:#555} input[type=text],input[type=number]{width:100%;padding:.45rem;box-sizing:border-box}
 button{padding:.55rem 1rem;cursor:pointer} pre{background:#f4f4f4;padding:.75rem;white-space:pre-wrap;font-size:.85rem}
 table{border-collapse:collapse;font-size:.88rem} th,td{border:1px solid #ccc;padding:.4rem .5rem;vertical-align:top;text-align:left} th{background:#f0f0f0}
 #out{overflow-x:auto;margin-top:1rem}
</style>
<h1>Medien-Scanner für Akquiseimpulse</h1>
<form id="f">
 <div><label>Firma</label><input type="text" name="firma" value="Treuhand Firma"></div>
 <div><label>Niederlassung</label><input type="text" name="niederlassung" value="Stans"></div>
 <div><label>Region</label><input type="text" name="region" value="Nidwalden, Obwalden und angrenzende Gemeinden"></div>
 <div><label>Kantone (SHAB)</label><input type="text" name="kantone" value="NW,OW"></div>
 <div><label>Zeitraum (Tage)</label><input type="number" name="tage" value="7" min="1" max="31"></div>
 <div><label><input type="checkbox" name="kontaktentwurf"> Kontaktentwurf</label></div>
 <div><button>Scan starten</button></div>
</form>
<pre id="log" hidden></pre><div id="out"></div>
<script>
const f=document.getElementById('f'),log=document.getElementById('log'),out=document.getElementById('out');
f.onsubmit=async e=>{e.preventDefault();out.innerHTML='';log.hidden=false;log.textContent='Gestartet …';
 const d=Object.fromEntries(new FormData(f));d.tage=+d.tage;d.kontaktentwurf=f.kontaktentwurf.checked;
 const {id}=await (await fetch('/api/scan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)})).json();
 const t=setInterval(async()=>{const j=await (await fetch('/api/scan/'+id)).json();
  log.textContent=j.log.join('\\n')||'Gestartet …';
  if(j.status==='fertig'){clearInterval(t);out.innerHTML=j.html;log.textContent+=`\\n${j.searches} Suchen, ${j.pages_read} Seiten, ${j.usage.input} Input- / ${j.usage.output} Output-Tokens`}
  if(j.status==='fehler'){clearInterval(t);log.textContent+='\\nFehler: '+j.error}},3000)};
</script></html>"""
