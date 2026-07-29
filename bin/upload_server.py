#!/usr/bin/env python
"""
Meeting upload server — private to your Tailscale network.

  iPhone Safari  ->  http://<tailscale-ip>:8765/?k=<token>
                 ->  chunked upload (no size limit)
                 ->  local transcription pipeline
                 ->  Telegram ping via `hermes send`

Nothing leaves this machine except the final Telegram notification.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import unicodedata
from datetime import date
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import CONFIG, ROOT, hermes_bin  # noqa: E402

INBOX = ROOT / "inbox"
JOBS = ROOT / "jobs"
ARCHIVE = ROOT / "archive"
TMP = ROOT / ".uploads"
LOGS = ROOT / "logs"
TOKEN_FILE = ROOT / ".token"
VENV_PY = ROOT / ".venv" / "bin" / "python"
PIPELINE = ROOT / "bin" / "process_meeting.py"
HERMES = hermes_bin()

for d in (INBOX, JOBS, ARCHIVE, TMP, LOGS):
    d.mkdir(parents=True, exist_ok=True)

if TOKEN_FILE.exists():
    TOKEN = TOKEN_FILE.read_text().strip()
else:
    TOKEN = secrets.token_urlsafe(18)
    TOKEN_FILE.write_text(TOKEN)
    TOKEN_FILE.chmod(0o600)

app = FastAPI(title="Meeting Uploader")


def check(k: str | None):
    if not k or not secrets.compare_digest(k, TOKEN):
        raise HTTPException(401, "bad token")


VALID_TYPES = {"general", "client", "interview"}
VALID_FORMATS = ("pdf", "md", "docx")
DEFAULT_FORMATS = ("pdf", "md")


def normalize_outputs(meta: dict) -> dict:
    """Fill in / sanitise the deliverable options so downstream never guesses.

    Old clients that post no options at all get the historical default:
    a `general` meeting note as both PDF and Markdown, no cleaned transcript.
    `formats` is global — it applies to every document produced by the job.
    """
    mt = str(meta.get("meeting_type") or "").strip()
    meta["meeting_type"] = mt if mt in VALID_TYPES else "general"

    meta["want_note"] = bool(meta.get("want_note", True))
    meta["want_transcript"] = bool(meta.get("want_transcript", False))
    if not meta["want_note"] and not meta["want_transcript"]:
        meta["want_note"] = True                      # never produce nothing

    fmts = [f for f in VALID_FORMATS if f in (meta.get("formats") or [])]
    meta["formats"] = fmts or list(DEFAULT_FORMATS)
    return meta


def slug(s: str, fallback: str = "會議") -> str:
    s = (s or "").strip() or fallback
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"[\\/:*?\"<>|\s]+", "-", s)
    return s.strip("-.")[:40] or fallback


# ------------------------------------------------------------------ HTML ----
PAGE = r"""<!doctype html>
<html lang="zh-Hant"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>會議上傳</title>
<style>
  :root{
    --bg:#fbfbfd; --card:#fff; --ink:#1d1d1f; --sub:#6e6e73;
    --line:#e5e5ea; --accent:#1A2E4A; --ok:#0a7c42; --err:#c0392b;
  }
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
  body{margin:0;background:var(--bg);color:var(--ink);
    font:16px/1.55 -apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang TC",sans-serif;
    padding:max(20px,env(safe-area-inset-top)) 20px calc(40px + env(safe-area-inset-bottom));}
  .wrap{max-width:520px;margin:0 auto}
  h1{font-size:26px;letter-spacing:-.02em;margin:8px 0 4px;font-weight:600}
  .sub{color:var(--sub);font-size:14px;margin-bottom:26px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:16px;
    padding:20px;margin-bottom:16px}
  label{display:block;font-size:13px;color:var(--sub);margin:14px 0 6px;font-weight:500;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  label:first-child{margin-top:0}
  input,select,textarea{width:100%;padding:12px 14px;font-size:16px;font-family:inherit;
    border:1px solid var(--line);border-radius:11px;background:#fff;color:var(--ink);
    -webkit-appearance:none;appearance:none}
  input:focus,select:focus,textarea:focus{outline:2px solid var(--accent);outline-offset:-1px}
  textarea{min-height:66px;resize:vertical}
  .drop{border:1.5px dashed #c7c7cc;border-radius:14px;padding:30px 16px;text-align:center;
    color:var(--sub);font-size:15px;background:#fcfcfe;transition:.15s}
  .drop.has{border-color:var(--accent);color:var(--ink);background:#f4f7fb}
  .fname{font-weight:600;word-break:break-all;font-size:15px}
  .meta{font-size:13px;color:var(--sub);margin-top:4px}
  button{width:100%;padding:15px;font-size:17px;font-weight:600;font-family:inherit;
    border:0;border-radius:13px;background:var(--accent);color:#fff;margin-top:20px}
  button:disabled{opacity:.4}
  .bar{height:7px;background:var(--line);border-radius:99px;overflow:hidden;margin:14px 0 8px}
  .bar>i{display:block;height:100%;width:0;background:var(--accent);transition:width .25s}
  .status{font-size:14px;color:var(--sub);white-space:pre-line}
  .done{color:var(--ok);font-weight:600}.fail{color:var(--err);font-weight:600}
  .hide{display:none}
  .row{display:flex;gap:12px}.row>div{flex:1}
  .hint{color:#a1a1a6;font-weight:400}
  code{background:#f0f0f4;padding:2px 6px;border-radius:5px;font-size:13px}
  .opts{display:flex;flex-direction:column;gap:8px;margin-bottom:2px}
  /* the numbered sections sit between tall checkbox cards — they need more
     air above them than a plain field label, or they read as part of the
     card above. */
  label.sec{margin-top:26px;color:var(--ink);font-size:13.5px;font-weight:600;
    letter-spacing:.01em}
  label.sec .hint{font-weight:400}
  #noteOpts{margin-bottom:2px}
  .sep{border:0;border-top:1px solid var(--line);margin:26px 0 18px}
  .chk{display:flex;align-items:flex-start;gap:11px;margin:0;padding:12px 14px;
    border:1px solid var(--line);border-radius:11px;background:#fff;cursor:pointer;
    font-size:16px;color:var(--ink);font-weight:500;white-space:normal;overflow:visible;
    transition:.15s}
  .chk.on{border-color:var(--accent);background:#f4f7fb}
  .chk input{width:21px;height:21px;flex:0 0 21px;margin:1px 0 0;accent-color:var(--accent);
    -webkit-appearance:auto;appearance:auto;padding:0;border:0}
  .chk small{display:block;font-size:12.5px;color:var(--sub);font-weight:400;margin-top:3px;
    line-height:1.4}
  .warn{color:var(--err);font-size:13px;margin-top:10px}
</style></head><body><div class="wrap">
<h1>會議上傳</h1>
<div class="sub">本機轉寫 · 音檔不離開這台 Mac</div>

<form id="f" class="card">
  <label>選擇錄音檔</label>
  <div class="drop" id="drop">
    <div id="dropTxt">點這裡選擇檔案<br><span class="meta">語音備忘錄 · m4a / mp3 / wav / mp4 · 無大小限制</span></div>
    <input id="file" type="file" accept="audio/*,video/*,.m4a,.mp3,.wav,.mp4,.mov,.aac,.caf" class="hide">
  </div>

  <label class="sec">1 · 輸出內容<span class="hint"> (可複選)</span></label>
  <div class="opts">
    <label class="chk" id="wrapTr">
      <input type="checkbox" id="wantTranscript">
      <span>逐字稿<small>整理過的定稿：修正 ASR 錯字、同音字、專有名詞、統一人名，繁體化並保留時間戳</small></span>
    </label>
    <label class="chk on" id="wrapNote">
      <input type="checkbox" id="wantNote" checked>
      <span>會議記錄<small>結構化整理：重點、議題、決議、待辦、風險</small></span>
    </label>
  </div>

  <div id="noteOpts">
    <label class="sec">2 · 會議類型</label>
    <select id="mtype">
      <option value="general" selected>一般討論會議 — 1 份會議記錄（預設）</option>
      <option value="client">顧問／客戶會議 — 3 份（客戶版／內部覆盤／夥伴版）</option>
      <option value="interview">訪談／研究 — 訪談紀要＋重點語錄</option>
    </select>
  </div>

  <label class="sec">3 · 輸出檔案型態<span class="hint"> (可複選)</span></label>
  <div class="opts">
    <label class="chk on" id="wrapPdf"><input type="checkbox" id="fmtPdf" checked>
      <span>PDF<small>排版定稿，適合外發、存檔</small></span></label>
    <label class="chk on" id="wrapMd"><input type="checkbox" id="fmtMd" checked>
      <span>Markdown<small>純文字，適合 Obsidian、貼進其他工具</small></span></label>
    <label class="chk" id="wrapDocx"><input type="checkbox" id="fmtDocx">
      <span>Word (.docx)<small>可直接改後外發</small></span></label>
  </div>
  <div class="warn hide" id="warn"></div>

  <hr class="sep">
  <label class="sec" style="margin-top:0">會議資訊</label>
  <div class="row">
    <div><label>客戶／對象</label><input id="client" placeholder="例：某某科技 / 內部產品會"></div>
    <div><label>日期</label><input id="date" type="date"></div>
  </div>
  <label>會議主題</label>
  <input id="title" placeholder="例：數位轉型第二階段規劃">
  <label>與會者（用頓號或逗號分隔）</label>
  <input id="participants" placeholder="__PARTICIPANTS_HINT__">
  <div class="row">
    <div><label>現場約幾人<span class="hint"> (參考用)</span></label>
      <select id="nspk">
        <option value="-1">不確定</option>
        <option>2</option><option>3</option><option>4</option>
        <option>5</option><option>6</option><option>7</option><option>8</option>
      </select></div>
    <div><label>語言</label>
      <select id="lang"><option value="zh">中文</option><option value="en">English</option>
      <option value="">自動</option></select></div>
  </div>
  <label>背景／專有名詞（會餵給辨識引擎，提高準確率）</label>
  <textarea id="context" placeholder="例：這場會議會提到 ERP、對帳、SaaS，客戶是某某科技，窗口是王經理"></textarea>

  <button id="go" type="submit" disabled>上傳並開始轉寫</button>
</form>

<div class="card hide" id="prog">
  <div class="bar"><i id="pb"></i></div>
  <div class="status" id="st">準備中…</div>
</div>
</div>

<script>
const K = new URLSearchParams(location.search).get('k') || localStorage.getItem('mk') || '';
if (K) localStorage.setItem('mk', K);
const $ = id => document.getElementById(id);
let FILE = null;
$('date').value = new Date().toISOString().slice(0,10);

// ---- output-option state -------------------------------------------------
const PAIRS = [['wantNote','wrapNote'],['wantTranscript','wrapTr'],
               ['fmtPdf','wrapPdf'],['fmtMd','wrapMd'],['fmtDocx','wrapDocx']];
const FMTS = ['fmtPdf','fmtMd','fmtDocx'];
function outputsValid(){
  if (!$('wantNote').checked && !$('wantTranscript').checked)
    return '請至少勾選一項輸出內容（逐字稿或會議記錄）';
  if (!FMTS.some(f => $(f).checked))
    return '請至少勾選一種輸出檔案型態（PDF / Markdown / Word）';
  return '';
}
function sync(){
  PAIRS.forEach(([c,w]) => $(w).classList.toggle('on', $(c).checked));
  $('noteOpts').classList.toggle('hide', !$('wantNote').checked);
  const msg = outputsValid();
  $('warn').textContent = msg;
  $('warn').classList.toggle('hide', !msg);
  $('go').disabled = !FILE || !!msg;
}
PAIRS.forEach(([c]) => $(c).addEventListener('change', sync));
// remember last choice
try{
  const saved = JSON.parse(localStorage.getItem('mopts')||'null');
  if (saved){ PAIRS.forEach(([c])=>{ if(c in saved) $(c).checked = saved[c]; });
              if (saved.mtype) $('mtype').value = saved.mtype; }
}catch(_){}

$('drop').onclick = () => $('file').click();
$('file').onchange = e => {
  FILE = e.target.files[0]; if(!FILE) return;
  $('drop').classList.add('has');
  $('dropTxt').innerHTML = `<div class="fname">${FILE.name}</div>
    <div class="meta">${(FILE.size/1048576).toFixed(1)} MB</div>`;
  sync();
};
sync();
['dragover','dragenter'].forEach(t=>$('drop').addEventListener(t,e=>{e.preventDefault();}));
$('drop').addEventListener('drop', e=>{e.preventDefault();
  FILE=e.dataTransfer.files[0]; if(FILE){$('file').files=e.dataTransfer.files;
  $('file').dispatchEvent(new Event('change'));}});

const CHUNK = 4*1024*1024;

$('f').onsubmit = async e => {
  e.preventDefault();
  if (!FILE) return;
  const bad = outputsValid(); if (bad){ sync(); return; }
  const formats = [];
  if ($('fmtPdf').checked)  formats.push('pdf');
  if ($('fmtMd').checked)   formats.push('md');
  if ($('fmtDocx').checked) formats.push('docx');
  try{ localStorage.setItem('mopts', JSON.stringify({
    wantNote:$('wantNote').checked, wantTranscript:$('wantTranscript').checked,
    fmtPdf:$('fmtPdf').checked, fmtMd:$('fmtMd').checked,
    fmtDocx:$('fmtDocx').checked, mtype:$('mtype').value})); }catch(_){}
  $('go').disabled = true; $('prog').classList.remove('hide');
  const setP = (p,t,cls) => { $('pb').style.width = (p*100).toFixed(1)+'%';
    $('st').textContent = t; $('st').className = 'status '+(cls||''); };

  try {
    setP(0,'建立上傳工作…');
    let r = await fetch('/api/upload/init?k='+K, {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({filename: FILE.name, size: FILE.size,
        client:$('client').value, title:$('title').value, date:$('date').value,
        participants:$('participants').value, num_speakers:$('nspk').value,
        language:$('lang').value, context:$('context').value,
        meeting_type:$('mtype').value,
        want_note:$('wantNote').checked,
        want_transcript:$('wantTranscript').checked,
        formats:formats})});
    if (!r.ok) throw new Error('init failed: '+await r.text());
    const {upload_id} = await r.json();

    const total = Math.ceil(FILE.size/CHUNK);
    for (let i=0;i<total;i++){
      const blob = FILE.slice(i*CHUNK, Math.min((i+1)*CHUNK, FILE.size));
      let ok=false;
      for (let attempt=0; attempt<4 && !ok; attempt++){
        try{
          const fd = new FormData(); fd.append('chunk', blob, 'c');
          const rr = await fetch(`/api/upload/chunk?k=${K}&upload_id=${upload_id}&index=${i}`,
                                 {method:'POST', body:fd});
          ok = rr.ok;
        }catch(_){ }
        if(!ok) await new Promise(s=>setTimeout(s, 800*(attempt+1)));
      }
      if(!ok) throw new Error(`第 ${i+1}/${total} 段上傳失敗`);
      setP(0.5*(i+1)/total, `上傳中 ${Math.round(100*(i+1)/total)}%  (${i+1}/${total} 段)`);
    }

    setP(0.5,'上傳完成，啟動本機轉寫…');
    r = await fetch(`/api/upload/complete?k=${K}&upload_id=${upload_id}`, {method:'POST'});
    if (!r.ok) throw new Error('complete failed: '+await r.text());
    const {job_id} = await r.json();

    while (true){
      await new Promise(s=>setTimeout(s,2500));
      const s = await (await fetch(`/api/job/${job_id}?k=${K}`)).json();
      if (s.state === 'done'){
        setP(1, `完成 ✓\n${s.result.num_speakers} 位講者 · ${Math.round(s.result.duration/60)} 分鐘 · 耗時 ${s.result.elapsed_sec}s (${s.result.realtime_factor}× 實時)\n\n已傳訊息到 Telegram，回覆即可開始整理會議記錄。`, 'done');
        break;
      }
      if (s.state === 'error'){ setP(1, '失敗：\n'+(s.error||'').slice(0,400), 'fail'); break; }
      setP(0.5 + 0.5*(s.progress||0), (s.step_label||'處理中') + ' — ' + (s.message||''));
    }
  } catch(err){ setP(1, '錯誤：'+err.message, 'fail'); $('go').disabled=false; }
};
</script></body></html>"""

PAGE = PAGE.replace("__PARTICIPANTS_HINT__", CONFIG["branding"]["participants_hint"])


@app.get("/", response_class=HTMLResponse)
def index(k: str = ""):
    return PAGE  # token is checked per-API-call, page itself is harmless


@app.post("/api/upload/init")
async def upload_init(req: Request, k: str = ""):
    check(k)
    body = await req.json()
    uid = secrets.token_hex(8)
    d = TMP / uid
    d.mkdir(parents=True, exist_ok=True)
    body["_orig"] = body.get("filename", "audio.m4a")
    (d / "meta.json").write_text(json.dumps(normalize_outputs(body), ensure_ascii=False))
    return {"upload_id": uid}


@app.post("/api/upload/chunk")
async def upload_chunk(k: str = "", upload_id: str = "", index: int = 0,
                       chunk: UploadFile = File(...)):
    check(k)
    d = TMP / upload_id
    if not d.is_dir():
        raise HTTPException(404, "unknown upload")
    # Idempotent: re-uploading a chunk just rewrites it.
    (d / f"{index:06d}.part").write_bytes(await chunk.read())
    return {"ok": True, "index": index}


@app.post("/api/upload/complete")
def upload_complete(k: str = "", upload_id: str = ""):
    check(k)
    d = TMP / upload_id
    if not d.is_dir():
        raise HTTPException(404, "unknown upload")
    meta = json.loads((d / "meta.json").read_text())

    ext = Path(meta["_orig"]).suffix.lower() or ".m4a"
    day = meta.get("date") or date.today().isoformat()
    job_id = f"{day}_{slug(meta.get('client'), '未命名客戶')}_{upload_id[:6]}"
    outdir = ARCHIVE / job_id
    outdir.mkdir(parents=True, exist_ok=True)

    audio = outdir / f"source{ext}"
    parts = sorted(d.glob("*.part"))
    if not parts:
        raise HTTPException(400, "no chunks received")
    with audio.open("wb") as out:
        for p in parts:
            out.write(p.read_bytes())
    shutil.rmtree(d, ignore_errors=True)

    (outdir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))

    ctx = (meta.get("context") or "").strip()
    prompt = "以下是繁體中文商務會議錄音。"
    if ctx:
        prompt += f"背景與專有名詞：{ctx}"

    cmd = [str(VENV_PY), str(PIPELINE), str(audio),
           "--outdir", str(outdir),
           "--language", meta.get("language") or "zh",
           "--num-speakers", str(meta.get("num_speakers") or -1),
           "--title", meta.get("title") or "",
           "--client", meta.get("client") or "",
           "--participants", meta.get("participants") or "",
           "--date", day, "--initial-prompt", prompt]

    log = (LOGS / f"{job_id}.log").open("wb")
    runner = ROOT / "bin" / "run_job.sh"
    subprocess.Popen([str(runner), job_id, str(outdir)] + cmd,
                     stdout=log, stderr=subprocess.STDOUT, start_new_session=True)

    (JOBS / f"{job_id}.json").write_text(json.dumps(
        {"job_id": job_id, "outdir": str(outdir), "meta": meta,
         "created_at": time.time()}, ensure_ascii=False, indent=2))
    return {"job_id": job_id, "outdir": str(outdir)}


@app.get("/api/job/{job_id}")
def job_status(job_id: str, k: str = ""):
    check(k)
    p = ARCHIVE / job_id / "status.json"
    if not p.exists():
        return JSONResponse({"state": "running", "step_label": "啟動中",
                             "progress": 0, "message": ""})
    return JSONResponse(json.loads(p.read_text()))


@app.get("/api/jobs")
def list_jobs(k: str = ""):
    check(k)
    out = []
    for f in sorted(JOBS.glob("*.json"), reverse=True)[:30]:
        j = json.loads(f.read_text())
        sp = ARCHIVE / j["job_id"] / "status.json"
        j["status"] = json.loads(sp.read_text()) if sp.exists() else None
        out.append(j)
    return out


if __name__ == "__main__":
    port = int(os.environ.get("MEETING_PORT") or CONFIG["port"])
    print(f"token: {TOKEN}", file=sys.stderr)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
