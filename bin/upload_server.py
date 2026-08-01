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

import fnmatch
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.error
from datetime import date
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jobstate  # noqa: E402
import lmstudio_runtime  # noqa: E402
import config as config_mod  # noqa: E402
from config import CONFIG, ROOT, hermes_bin, sanitize_agent_preset  # noqa: E402

INBOX = ROOT / "inbox"
JOBS = ROOT / "jobs"
ARCHIVE = ROOT / "archive"
TMP = ROOT / ".uploads"
LOGS = ROOT / "logs"
TOKEN_FILE = ROOT / ".token"
VENV_PY = ROOT / ".venv" / "bin" / "python"
PIPELINE = ROOT / "bin" / "process_meeting.py"
REVIEW = ROOT / "bin" / "review.py"
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


@app.exception_handler(StarletteHTTPException)
async def _json_errors(request: Request, exc: StarletteHTTPException):
    """One error shape for the whole API: {"error": "..."} + a real status.

    FastAPI's default is {"detail": ...}; the web UI renders `error` inline,
    and the old upload page only ever reads the body as text, so unifying here
    is safe for both.
    """
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


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

    meta["agent_preset"] = sanitize_agent_preset(meta.get("agent_preset"))

    fmts = [f for f in VALID_FORMATS if f in (meta.get("formats") or [])]
    meta["formats"] = fmts or list(DEFAULT_FORMATS)
    return meta


def slug(s: str, fallback: str = "會議") -> str:
    s = (s or "").strip() or fallback
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"[\\/:*?\"<>|\s]+", "-", s)
    return s.strip("-.")[:40] or fallback


# -------------------------------------------------------------- job helpers --
# Every job-id -> path resolution in this file goes through jobstate.job_dir(),
# which refuses anything that escapes archive/. Nothing here concatenates a
# user-supplied string onto a path.

def jd(job_id: str, must_exist: bool = True) -> Path:
    try:
        d = jobstate.job_dir(job_id)
    except ValueError:
        raise HTTPException(400, "無效的 job id")
    if must_exist and not d.is_dir():
        raise HTTPException(404, f"找不到會議 {job_id}")
    return d


def safe_child(d: Path, name: str) -> Path:
    """Resolve `name` inside `d`, refusing traversal and symlink escapes."""
    if not name or "/" in name or "\\" in name or ".." in name or name.startswith("."):
        raise HTTPException(400, "無效的檔名")
    p = (d / name).resolve()
    if not str(p).startswith(str(d.resolve()) + os.sep):
        raise HTTPException(400, "無效的檔名")
    if not p.is_file():
        raise HTTPException(404, f"找不到檔案 {name}")
    return p


def atomic_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    os.replace(tmp, path)


def read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def lmstudio_status_payload() -> dict:
    try:
        return lmstudio_runtime.status()
    except Exception as exc:  # noqa: BLE001
        return {
            "backend": "openai_compat",
            "model": "",
            "base_url": "",
            "cleanup": lmstudio_runtime.cleanup_cfg(),
            "activity": lmstudio_runtime.read_activity(),
            "loaded_models": [],
            "loaded_count": 0,
            "available_models": [],
            "available_count": 0,
            "selected_model_loaded": False,
            "active_private_jobs": [],
            "active_count": 0,
            "can_unload_now": False,
            "load_error": f"{type(exc).__name__}: {exc}",
            "list_error": f"{type(exc).__name__}: {exc}",
            "log_path": str(lmstudio_runtime.LOG_FILE),
        }


def save_private_model(model_key: str) -> str:
    key = str(model_key or "").strip()
    if not key:
        raise ValueError("缺少模型名稱")
    cfg = config_mod.load()
    agent = cfg.setdefault("agent", {})
    profiles = agent.setdefault("profiles", {})
    private = profiles.setdefault("private", {})
    private["backend"] = private.get("backend") or "openai_compat"
    private["model"] = key
    config_mod.save(cfg)
    return key


def save_private_cleanup(mode: object, idle_minutes: object = None) -> dict:
    mode = str(mode or "").strip().lower()
    if mode not in {"keep_loaded", "idle_eject", "after_job"}:
        raise ValueError("cleanup mode 不合法")
    mins = 15 if idle_minutes in (None, "") else int(str(idle_minutes).strip())
    if mins < 1 or mins > 1440:
        raise ValueError("idle_minutes 必須介於 1 到 1440")
    cfg = config_mod.load()
    agent = cfg.setdefault("agent", {})
    cleanup = agent.setdefault("private_cleanup", {})
    cleanup["mode"] = mode
    cleanup["idle_minutes"] = mins
    config_mod.save(cfg)
    return {"mode": mode, "idle_minutes": mins}


def is_protected(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in jobstate.PROTECTED)


# Scratch directories the review stages build. Cheap to regenerate, and they
# are what actually holds the disk after a long job.
SCRATCH_DIRS = (".chunks", ".review", ".export")


def cleanup_plan(d: Path) -> dict:
    """What "清理產出" would delete, without deleting anything."""
    names: list[str] = []
    seen: set[str] = set()
    freed = 0
    for g in jobstate.OUTPUT_GLOBS:
        for p in sorted(d.glob(g)):
            if not p.is_file() or p.name in seen or is_protected(p.name):
                continue
            seen.add(p.name)
            freed += p.stat().st_size
            names.append(p.name)
    for sub in SCRATCH_DIRS:
        sd = d / sub
        if sd.is_dir():
            freed += jobstate.dir_size(sd)
            names.append(sub + "/")
    audio = next((p for p in d.glob("source.*") if p.is_file()), None)
    return {
        "outputs": {"count": len(names), "bytes": freed, "names": names},
        "total": {"bytes": jobstate.dir_size(d)},
        "audio": {"bytes": audio.stat().st_size if audio else 0},
    }


def spawn_review(job_id: str, d: Path, stage: str) -> None:
    """Start review.py detached, appending to the job's agent log.

    The HTTP request must not wait on a model call that can run for minutes,
    so this is fire-and-forget: the browser learns what happened by polling
    state.json, and the log endpoint is the escape hatch when it goes wrong.
    """
    log_path = LOGS / f"{job_id}-agent.log"
    log = log_path.open("ab")
    try:
        log.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} "
                  f"review.py --stage {stage} (web) ===\n".encode())
        log.flush()
        subprocess.Popen(
            [str(VENV_PY), str(REVIEW), str(d), "--stage", stage, "--deliver"],
            stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True, cwd=str(ROOT))
    finally:
        log.close()


def require_review():
    if not REVIEW.exists():
        raise HTTPException(503, "bin/review.py 尚未安裝，無法啟動撰寫")


def tail_file(path: Path, lines: int) -> str:
    if not path.exists():
        return ""
    size = path.stat().st_size
    with path.open("rb") as f:
        if size > 262_144:                       # only ever read the tail
            f.seek(size - 262_144)
        data = f.read()
    return "\n".join(data.decode("utf-8", "replace").splitlines()[-lines:])


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
  .top{display:flex;align-items:baseline;justify-content:space-between;gap:16px;margin:8px 0 4px}
  .top h1{margin:0}
  a.nav{color:var(--accent);text-decoration:none;font-size:14.5px;font-weight:600;
    white-space:nowrap;padding:8px 0}
  a.jumpto{display:block;margin-top:14px;padding:13px;text-align:center;font-size:15px;
    font-weight:600;color:var(--accent);text-decoration:none;border:1px solid var(--line);
    border-radius:11px;background:#fff}
</style></head><body><div class="wrap">
<div class="top"><h1>會議上傳</h1><a class="nav" id="navJobs" href="/jobs">全部會議 ›</a></div>
<div class="sub">音檔留在這台 Mac · AI 整理可選一般模式或保密模式</div>

<form id="f" class="card">
  <label>選擇錄音檔</label>
  <div class="drop" id="drop">
    <div id="dropTxt">點這裡選擇檔案<br><span class="meta">語音備忘錄 · m4a / mp3 / wav / mp4 · 無大小限制</span></div>
    <input id="file" type="file" accept="audio/*,video/*,.m4a,.mp3,.wav,.mp4,.mov,.aac,.caf" class="hide">
  </div>

  <label class="sec">1 · AI 處理模式</label>
  <div class="opts">
    <label class="chk on" id="wrapAgentGeneral" onclick="setAgentPreset('general')">
      <input type="radio" name="agentPreset" id="agentGeneral" checked>
      <span>一般模式（預設）<small>用 Claude / Codex 整理會議記錄，適合一般日常會議</small></span>
    </label>
    <label class="chk" id="wrapAgentPrivate" onclick="setAgentPreset('private')">
      <input type="radio" name="agentPreset" id="agentPrivate">
      <span>保密模式（LM Studio）<small>把逐字稿送到你自己的 LM Studio 推論機，適合敏感內容</small></span>
    </label>
  </div>

  <label class="sec">2 · 輸出內容<span class="hint"> (可複選)</span></label>
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
    <label class="sec">3 · 會議類型</label>
    <select id="mtype">
      <option value="general" selected>一般討論會議 — 1 份會議記錄（預設）</option>
      <option value="client">顧問／客戶會議 — 3 份（客戶版／內部覆盤／夥伴版）</option>
      <option value="interview">訪談／研究 — 訪談紀要＋重點語錄</option>
    </select>
  </div>

  <label class="sec">4 · 輸出檔案型態<span class="hint"> (可複選)</span></label>
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
  <a class="jumpto hide" id="toJob" href="/jobs">到這場會議 ›</a>
  <a class="jumpto hide" id="toJobs" href="/jobs">全部會議 ›</a>
</div>
</div>

<script>
const K = new URLSearchParams(location.search).get('k') || localStorage.getItem('mk') || '';
if (K) localStorage.setItem('mk', K);
const $ = id => document.getElementById(id);
const kq = p => p + (K ? (p.includes('?') ? '&' : '?') + 'k=' + encodeURIComponent(K) : '');
$('navJobs').href = kq('/jobs');
let FILE = null;
$('date').value = new Date().toISOString().slice(0,10);

// ---- output-option state -------------------------------------------------
const PAIRS = [['wantNote','wrapNote'],['wantTranscript','wrapTr'],
               ['fmtPdf','wrapPdf'],['fmtMd','wrapMd'],['fmtDocx','wrapDocx']];
const AGENT_PAIRS = [['agentGeneral','wrapAgentGeneral'],['agentPrivate','wrapAgentPrivate']];
const FMTS = ['fmtPdf','fmtMd','fmtDocx'];
function syncAgents(){
  AGENT_PAIRS.forEach(([c,w]) => $(w).classList.toggle('on', $(c).checked));
}
function outputsValid(){
  if (!$('wantNote').checked && !$('wantTranscript').checked)
    return '請至少勾選一項輸出內容（逐字稿或會議記錄）';
  if (!FMTS.some(f => $(f).checked))
    return '請至少勾選一種輸出檔案型態（PDF / Markdown / Word）';
  return '';
}
function sync(){
  PAIRS.forEach(([c,w]) => $(w).classList.toggle('on', $(c).checked));
  syncAgents();
  $('noteOpts').classList.toggle('hide', !$('wantNote').checked);
  const msg = outputsValid();
  $('warn').textContent = msg;
  $('warn').classList.toggle('hide', !msg);
  $('go').disabled = !FILE || !!msg;
}
function setAgentPreset(preset){
  $('agentPrivate').checked = preset === 'private';
  $('agentGeneral').checked = preset !== 'private';
  sync();
}
PAIRS.forEach(([c,w]) => {
  $(c).addEventListener('change', sync);
  $(w).addEventListener('click', e => { if (e.target !== $(c)) { $(c).click(); } });
});
AGENT_PAIRS.forEach(([c,w]) => {
  $(c).addEventListener('change', sync);
  $(w).addEventListener('click', e => {
    if (e.target === $(c)) return;
    setAgentPreset(c === 'agentPrivate' ? 'private' : 'general');
  });
});
// remember last choice
try{
  const saved = JSON.parse(localStorage.getItem('mopts')||'null');
  if (saved){
    PAIRS.forEach(([c])=>{ if(c in saved) $(c).checked = saved[c]; });
    if (saved.mtype) $('mtype').value = saved.mtype;
    if (saved.agentPreset === 'private') $('agentPrivate').checked = true;
    else $('agentGeneral').checked = true;
  }
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
    fmtDocx:$('fmtDocx').checked, mtype:$('mtype').value,
    agentPreset:$('agentPrivate').checked ? 'private' : 'general'})); }catch(_){}
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
        agent_preset:$('agentPrivate').checked ? 'private' : 'general',
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
    $('toJob').href = kq('/job/' + encodeURIComponent(job_id));
    $('toJobs').href = kq('/jobs');
    $('toJob').classList.remove('hide');
    $('toJobs').classList.remove('hide');

    while (true){
      await new Promise(s=>setTimeout(s,2500));
      const s = await (await fetch(`/api/job/${job_id}?k=${K}`)).json();
      if (s.state === 'done'){
        setP(1, `轉寫完成 ✓\n${s.result.num_speakers} 位講者 · ${Math.round(s.result.duration/60)} 分鐘 · 耗時 ${s.result.elapsed_sec}s (${s.result.realtime_factor}× 實時)\n\n接著會掃描逐字稿並整理出待確認的問題，到會議頁面回答即可開始撰寫。`, 'done');
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


# ------------------------------------------------------ jobs / detail UI ----
# One stylesheet for both pages, extending the upload form's language: white
# cards on #fbfbfd, hairline borders instead of shadows, the same navy accent,
# and red kept for exactly two things — 待回答 and 刪除.
CSS = r"""
  :root{
    --bg:#fbfbfd; --card:#fff; --ink:#1d1d1f; --sub:#6e6e73;
    --line:#e5e5ea; --hair:#d2d2d7; --accent:#1A2E4A; --ok:#0a7c42;
    --err:#d70015; --soft:#f5f5f7;
  }
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
  html{-webkit-text-size-adjust:100%}
  body{margin:0;background:var(--bg);color:var(--ink);
    font:16px/1.55 -apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang TC",
      "Helvetica Neue",sans-serif;
    padding:max(20px,env(safe-area-inset-top)) 20px calc(56px + env(safe-area-inset-bottom));}
  .wrap{max-width:760px;margin:0 auto}
  a{color:var(--accent);text-decoration:none}
  h1{font-size:26px;letter-spacing:-.022em;margin:0;font-weight:600}
  h2{font-size:14px;font-weight:600;letter-spacing:.02em;margin:0 0 18px;color:var(--sub)}
  .sub{color:var(--sub);font-size:14px}
  .top{display:flex;align-items:center;justify-content:space-between;gap:16px;
    min-height:44px;margin-bottom:14px}
  .nav{font-size:14.5px;font-weight:600;white-space:nowrap;display:inline-flex;
    align-items:center;min-height:44px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:16px;
    padding:20px;margin-bottom:16px}
  .hide{display:none !important}
  .mono{font-family:ui-monospace,SFMono-Regular,"SF Mono",Menlo,monospace}

  /* --- badges ------------------------------------------------------------ */
  .badge{display:inline-flex;align-items:center;font-size:12px;font-weight:600;
    padding:3px 9px;border-radius:99px;border:1px solid var(--hair);color:var(--sub);
    background:#fff;letter-spacing:.02em;white-space:nowrap}
  .badge.s-run{border-color:#ccd8e6;color:var(--accent);background:#f3f7fb}
  .badge.s-ask{border-color:#f0c3c7;color:var(--err);background:#fdf3f4}
  .badge.s-done{border-color:#c2e0d0;color:var(--ok);background:#f2f9f5}
  .badge.s-err{border-color:#f0c3c7;color:var(--err);background:#fdf3f4}
  .badge.s-arch{border-color:var(--hair);color:#8e8e93;background:var(--soft)}

  /* --- buttons ----------------------------------------------------------- */
  .btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;
    min-height:44px;padding:11px 17px;font:600 15px/1.2 inherit;border:1px solid var(--hair);
    border-radius:10px;background:#fff;color:var(--ink);cursor:pointer;
    -webkit-appearance:none;appearance:none}
  .btn:active{background:var(--soft)}
  .btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
  .btn.danger{color:var(--err);border-color:#eec2c6}
  .btn.danger.solid{background:var(--err);border-color:var(--err);color:#fff}
  .btn:disabled{opacity:.4;cursor:default}
  .btn.wide{width:100%}
  .btnrow{display:flex;flex-wrap:wrap;gap:8px}
  .btn.okflash{background:var(--ok);border-color:var(--ok);color:#fff}
  .btn.busy{opacity:.7}

  /* --- feedback + navigation -------------------------------------------- */
  .strip{border:1px solid #eec2c6;background:#fdf3f4;color:var(--err);border-radius:10px;
    padding:11px 14px;font-size:14px;margin-bottom:16px;line-height:1.5}
  .toast{position:fixed;left:50%;bottom:calc(18px + env(safe-area-inset-bottom));
    transform:translate(-50%, 24px);max-width:min(92vw, 560px);width:max-content;
    box-shadow:0 12px 32px rgba(0,0,0,.12);z-index:60;opacity:0;pointer-events:none;
    transition:opacity .2s ease, transform .2s ease;margin:0}
  .toast.show{opacity:1;transform:translate(-50%, 0)}
  .strip.ok{border-color:#c2e0d0;background:#f2f9f5;color:var(--ok)}
  .fab{position:fixed;right:20px;bottom:calc(18px + env(safe-area-inset-bottom));
    width:52px;height:52px;border-radius:999px;border:1px solid var(--hair);background:#fff;
    color:var(--accent);box-shadow:0 10px 28px rgba(0,0,0,.12);z-index:40;display:flex;
    align-items:center;justify-content:center;font-size:22px;font-weight:700;cursor:pointer;
    opacity:0;pointer-events:none;transform:translateY(12px);transition:.2s}
  .fab.show{opacity:1;pointer-events:auto;transform:translateY(0)}
  .fab:active{background:var(--soft)}

  /* --- forms ------------------------------------------------------------- */
  label{display:block;font-size:13px;color:var(--sub);margin:16px 0 6px;font-weight:500}
  label.first{margin-top:0}
  input,select,textarea{width:100%;padding:12px 14px;font-size:16px;font-family:inherit;
    border:1px solid var(--line);border-radius:11px;background:#fff;color:var(--ink);
    -webkit-appearance:none;appearance:none}
  input:focus,select:focus,textarea:focus{outline:2px solid var(--accent);outline-offset:-1px}
  textarea{min-height:76px;resize:vertical;line-height:1.6}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:0 14px}
  .chk{display:flex;align-items:flex-start;gap:11px;margin:0;padding:12px 14px;
    border:1px solid var(--line);border-radius:11px;background:#fff;cursor:pointer;
    font-size:15.5px;font-weight:500;min-height:44px;transition:.15s}
  .chk.on{border-color:var(--accent);background:#f3f7fb}
  .chk input{width:21px;height:21px;flex:0 0 21px;margin:0;accent-color:var(--accent);
    -webkit-appearance:auto;appearance:auto;padding:0;border:0}
  .opts{display:flex;flex-direction:column;gap:8px}
  .opts.row{flex-direction:row;flex-wrap:wrap}
  .opts.row .chk{flex:1 1 30%}

  /* --- modal ------------------------------------------------------------- */
  .veil{position:fixed;inset:0;background:rgba(29,29,31,.34);display:flex;
    align-items:center;justify-content:center;padding:20px;z-index:50;
    -webkit-backdrop-filter:blur(6px);backdrop-filter:blur(6px)}
  .modal{background:#fff;border-radius:16px;max-width:620px;width:100%;
    max-height:82vh;display:flex;flex-direction:column;overflow:hidden;
    border:1px solid var(--line)}
  .modal .mh{padding:18px 20px 0;font-size:17px;font-weight:600}
  .modal .mb{padding:12px 20px;overflow:auto;font-size:14.5px;color:var(--sub);
    line-height:1.6;flex:1}
  .modal .mf{padding:14px 20px 18px;display:flex;gap:8px;justify-content:flex-end;
    border-top:1px solid var(--line)}
  .modal pre{white-space:pre-wrap;word-break:break-word;font-size:13px;color:var(--ink);
    background:var(--soft);border-radius:10px;padding:14px;margin:0;line-height:1.65}
  @media (max-width:520px){ .modal .mf{flex-direction:column-reverse} .modal .mf .btn{width:100%} }
"""


JOBS_PAGE = r"""<!doctype html>
<html lang="zh-Hant"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>會議</title>
<style>__CSS__
  .tools{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:18px}
  .tools input[type=search]{flex:2 1 220px;min-width:0}
  .tools select{flex:1 1 130px;min-width:0;padding-right:34px;
    background-image:linear-gradient(45deg,transparent 50%,var(--sub) 50%),
      linear-gradient(135deg,var(--sub) 50%,transparent 50%);
    background-position:calc(100% - 18px) 21px,calc(100% - 13px) 21px;
    background-size:5px 5px,5px 5px;background-repeat:no-repeat}
  .toggle{display:flex;align-items:center;gap:9px;font-size:14px;color:var(--sub);
    min-height:44px;cursor:pointer;padding:0 2px;white-space:nowrap}
  .toggle input{width:20px;height:20px;flex:0 0 20px;accent-color:var(--accent);
    -webkit-appearance:auto;appearance:auto;padding:0;border:0}
  .list{display:flex;flex-direction:column;gap:10px}
  .job{display:block;background:#fff;border:1px solid var(--line);border-radius:14px;
    padding:16px 18px;color:inherit;transition:.15s}
  .job:active{background:var(--soft)}
  .job.ask{border-color:#eec2c6}
  .job.arch{opacity:.62}
  .jt{display:flex;align-items:center;gap:9px;flex-wrap:wrap}
  .jtitle{font-size:16.5px;font-weight:600;letter-spacing:-.01em;
    overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:100%}
  .jmeta{font-size:13.5px;color:var(--sub);margin-top:5px;
    display:flex;flex-wrap:wrap;gap:4px 10px}
  .cta{display:inline-flex;align-items:center;gap:6px;margin-top:12px;min-height:44px;
    padding:10px 15px;border-radius:10px;background:#fdf3f4;border:1px solid #eec2c6;
    color:var(--err);font-size:14.5px;font-weight:600}
  .pbar{height:5px;background:var(--line);border-radius:99px;overflow:hidden;margin:12px 0 6px}
  .pbar>i{display:block;height:100%;background:var(--accent);transition:width .3s}
  /* Scanning and writing have no measurable percentage — an indeterminate bar
     is honest where a stale 100% from the ASR stage is not. */
  .pbar.indet>i{width:34%;animation:slide 1.5s cubic-bezier(.4,0,.2,1) infinite}
  @keyframes slide{0%{margin-left:-34%}100%{margin-left:100%}}
  .pstep{font-size:13px;color:var(--sub)}
  .empty{text-align:center;color:var(--sub);font-size:14.5px;padding:52px 20px;
    border:1px dashed var(--hair);border-radius:14px;background:#fff}
  .count{font-size:13px;color:#a1a1a6;margin:0 2px 12px}
  .admin{background:#fff;border:1px solid var(--line);border-radius:14px;padding:16px 18px;margin:0 0 16px}
  .admin h2{font-size:17px;margin-bottom:8px}
  .adminmeta{font-size:13.5px;color:var(--sub);display:flex;flex-wrap:wrap;gap:4px 10px;margin:8px 0}
  .adminstats{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin:12px 0}
  .adminstat{border:1px solid var(--line);border-radius:12px;background:#fcfcfd;padding:12px 13px}
  .adminstat .k{font-size:12px;color:var(--sub);margin-bottom:6px}
  .adminstat .v{font-size:14px;color:var(--ink);font-weight:600;line-height:1.45;word-break:break-word}
  .adminline{font-size:14px;color:var(--ink);margin-top:8px;line-height:1.6}
  .admin .btnrow{margin-top:12px}
  .admin .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:12px}
  .admin select{min-width:min(100%,420px);max-width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:12px;background:#fff;font-size:14px;color:var(--ink)}
  .admin .inputunit{display:inline-flex;align-items:center;border:1px solid var(--line);border-radius:12px;background:#fff;overflow:hidden}
  .admin .inputunit input[type="number"]{width:88px;padding:10px 12px;border:0;border-radius:0;background:transparent;font-size:14px;color:var(--ink)}
  .admin .inputunit input[type="number"]:focus{outline:none}
  .admin .inputunit:focus-within{outline:2px solid var(--accent);outline-offset:-1px}
  .admin .inputunit span{padding:0 12px;border-left:1px solid var(--line);font-size:13px;color:var(--sub);white-space:nowrap;background:#fafafc;align-self:stretch;display:flex;align-items:center}
  .adminhint{font-size:12.5px;color:var(--sub);margin-top:8px;line-height:1.6}
  @media (max-width:640px){.adminstats{grid-template-columns:1fr}}
</style></head><body><div class="wrap">

<div class="top">
  <h1>會議</h1>
  <a class="nav" id="navUp" href="/">📤 上傳新錄音</a>
</div>
<div class="sub" style="margin-bottom:20px">本機轉寫 · 音檔不離開這台 Mac</div>

<div id="err" class="strip hide"></div>

<section class="admin">
  <h2>LM Studio 管理</h2>
  <div class="adminmeta" id="lmMeta">讀取中…</div>
  <div class="adminstats">
    <div class="adminstat"><div class="k">目標模型</div><div class="v" id="lmStatTarget">—</div></div>
    <div class="adminstat"><div class="k">目前載入</div><div class="v" id="lmStatLoaded">—</div></div>
    <div class="adminstat"><div class="k">保密任務</div><div class="v" id="lmStatJobs">—</div></div>
    <div class="adminstat"><div class="k">手動釋放</div><div class="v" id="lmStatEject">—</div></div>
  </div>
  <div class="adminline" id="lmLine1"></div>
  <div class="adminline" id="lmLine2"></div>
  <div class="row">
    <select id="lmModel"></select>
    <button class="btn" id="lmLoad">選擇並載入模型</button>
  </div>
  <div class="row">
    <select id="lmCleanupMode">
      <option value="keep_loaded">keep_loaded：持續保留模型</option>
      <option value="idle_eject">idle_eject：閒置後釋放</option>
      <option value="after_job">after_job：每次工作後釋放</option>
    </select>
    <div class="inputunit"><input id="lmIdleMinutes" type="number" min="1" max="1440" step="1" inputmode="numeric" aria-label="閒置分鐘數"><span>分鐘</span></div>
    <button class="btn" id="lmSaveCleanup">儲存 cleanup 設定</button>
  </div>
  <div class="adminhint" id="lmHint">重新整理狀態時會同步抓取 LM Studio 可用模型列表。</div>
  <div class="btnrow">
    <button class="btn" id="lmRefresh">重新整理狀態</button>
    <button class="btn" id="lmEject">立即釋放模型</button>
  </div>
</section>

<div class="tools">
  <input id="q" type="search" placeholder="搜尋主題或客戶" autocomplete="off">
  <select id="fstate">
    <option value="">全部狀態</option>
    <option value="awaiting_answers">待回答</option>
    <option value="transcribing">轉寫中</option>
    <option value="scanning">掃描中</option>
    <option value="writing">撰寫中</option>
    <option value="done">完成</option>
    <option value="error">失敗</option>
  </select>
  <select id="sort">
    <option value="new">最新</option>
    <option value="old">最舊</option>
    <option value="big">檔案最大</option>
    <option value="todo">待處理優先</option>
  </select>
  <label class="toggle"><input type="checkbox" id="arch"><span>顯示已封存</span></label>
</div>

<div class="count" id="count"></div>
<div class="list" id="list"></div>
</div>
<button class="fab" id="toTop" type="button" aria-label="回到最上面">↑</button>

<script>
const K = new URLSearchParams(location.search).get('k') || localStorage.getItem('mk') || '';
if (K) localStorage.setItem('mk', K);
const $ = id => document.getElementById(id);
const kq = p => p + (K ? (p.includes('?') ? '&' : '?') + 'k=' + encodeURIComponent(K) : '');
$('navUp').href = kq('/');

const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const hb = b => { b = +b || 0; if (b < 1024) return b + ' B';
  const u = ['KB','MB','GB','TB']; let i = -1;
  do { b /= 1024; i++; } while (b >= 1024 && i < 3);
  return b.toFixed(b < 10 ? 1 : 0) + ' ' + u[i]; };
const dur = s => { s = Math.round(+s || 0); if (!s) return '';
  const h = Math.floor(s/3600), m = Math.round(s%3600/60);
  return h ? h + ' 小時 ' + m + ' 分' : Math.max(1, m) + ' 分'; };
const MT = {general:'一般討論', client:'顧問／客戶', interview:'訪談／研究'};
const SCLS = {awaiting_answers:'s-ask', error:'s-err', done:'s-done',
              transcribing:'s-run', scanning:'s-run', writing:'s-run'};
const RUNNING = ['transcribing','scanning','writing'];
// status.json only measures the ASR stage, so the later stages get their own
// line rather than a percentage that stopped moving.
const STEP = {scanning:'正在掃描逐字稿，整理待確認的問題…', writing:'正在撰寫文件…'};
function stepText(j){
  if (j.state === 'transcribing')
    return (j.step_label || '轉寫中') + (j.message ? ' — ' + String(j.message).slice(0, 80) : '');
  return STEP[j.state] || j.state_label || '';
}

let JOBS = [], LAST = '', LM = null;

function showErr(m){ $('err').textContent = m; $('err').classList.remove('hide'); }
function clearErr(){ $('err').classList.add('hide'); }
function ago(ts){
  ts = +ts || 0; if (!ts) return '尚未使用';
  const d = Math.max(0, Math.round(Date.now()/1000 - ts));
  if (d < 60) return d + ' 秒前';
  if (d < 3600) return Math.floor(d/60) + ' 分鐘前';
  if (d < 86400) return Math.floor(d/3600) + ' 小時前';
  return Math.floor(d/86400) + ' 天前';
}
async function withBtn(id, busyText, okText, fn){
  const b = $(id), old = b.textContent;
  b.disabled = true; b.textContent = busyText;
  try {
    const out = await fn();
    b.textContent = okText;
    setTimeout(() => { b.textContent = old; }, 1200);
    return out;
  } finally {
    setTimeout(() => { b.disabled = false; if (b.textContent === okText) b.textContent = old; }, 0);
  }
}
function renderModelOptions(){
  const sel = $('lmModel');
  const models = (LM && LM.available_models) || [];
  if (!models.length){
    sel.innerHTML = '<option value="">（目前抓不到可用模型）</option>';
    sel.disabled = true;
    $('lmLoad').disabled = true;
    return;
  }
  sel.disabled = false;
  sel.innerHTML = models.map(m => {
    const label = [m.display_name || m.key, m.params || '', m.loaded ? '已載入' : '', m.key === LM.model ? '目前預設' : '']
      .filter(Boolean).join(' · ');
    return '<option value="' + esc(m.key) + '">' + esc(label) + '</option>';
  }).join('');
  sel.value = models.some(m => m.key === LM.model) ? LM.model : models[0].key;
  $('lmLoad').disabled = false;
}
function syncCleanupForm(){
  const c = (LM && LM.cleanup) || {};
  $('lmCleanupMode').value = c.mode || 'idle_eject';
  $('lmIdleMinutes').value = c.idle_minutes || 15;
  $('lmIdleMinutes').disabled = $('lmCleanupMode').value !== 'idle_eject';
}
function lmLoadedSummary(){
  if (!LM) return '—';
  const selected = (LM.selected_model_instances || []).filter(Boolean);
  const foreign = (LM.foreign_loaded_models || []).filter(Boolean);
  if (selected.length && foreign.length)
    return '目標模型已載入；另有 ' + foreign.join('、');
  if (selected.length)
    return selected.join('、');
  if (foreign.length)
    return foreign.join('、');
  return '目前沒有載入模型';
}
function renderLM(){
  if (!LM){
    $('lmMeta').textContent = '讀取中…'; $('lmLine1').textContent = ''; $('lmLine2').textContent = '';
    $('lmStatTarget').textContent = '—';
    $('lmStatLoaded').textContent = '—';
    $('lmStatJobs').textContent = '—';
    $('lmStatEject').textContent = '—';
    $('lmHint').textContent = '重新整理狀態時會同步抓取 LM Studio 可用模型列表。';
    $('lmModel').innerHTML = '<option value="">讀取中…</option>';
    $('lmModel').disabled = true;
    $('lmLoad').disabled = true;
    $('lmCleanupMode').value = 'idle_eject';
    $('lmIdleMinutes').value = 15;
    $('lmIdleMinutes').disabled = false;
    $('lmSaveCleanup').disabled = true;
    return;
  }
  const c = LM.cleanup || {};
  const act = LM.activity || {};
  const bits = [
    '策略：' + (c.mode || 'idle_eject'),
    c.mode === 'idle_eject' ? '閒置 ' + (c.idle_minutes || 15) + ' 分鐘 eject' : '',
    '可用模型：' + (LM.available_count || 0) + ' 個',
  ].filter(Boolean);
  $('lmMeta').innerHTML = bits.map(esc).join('<span>·</span>');
  $('lmStatTarget').textContent = LM.model || '未設定';
  $('lmStatLoaded').textContent = lmLoadedSummary();
  $('lmStatJobs').textContent = LM.active_count
    ? (LM.active_private_jobs || []).join('、')
    : '目前沒有保密任務';
  $('lmStatEject').textContent = LM.can_unload_now
    ? '可立即釋放'
    : ((LM.foreign_loaded_count || 0)
        ? '不可釋放（目前是外部載入模型）'
        : '暫不可釋放');
  $('lmLine1').textContent = '最後使用：' + ago(act.ts) + (act.job_id ? ' · job ' + act.job_id : '') + (act.stage ? ' · ' + act.stage : '');
  $('lmLine2').textContent = (
    LM.active_count
      ? '目前仍有保密任務執行中：' + (LM.active_private_jobs || []).join('、')
      : LM.can_unload_now
        ? '目前沒有保密任務在跑；可手動釋放模型。'
        : (LM.foreign_loaded_count || 0)
          ? '目前沒有本系統的保密任務在跑，但 LM Studio 仍有其他已載入模型：' + (LM.foreign_loaded_models || []).join('、') + '；先不提供釋放。'
          : '目前沒有保密任務在跑，也沒有偵測到保密模式目標模型已載入。'
  ) + (LM.load_error ? '（狀態讀取警告：' + LM.load_error + '）' : '');
  $('lmHint').textContent = (LM.available_count || 0)
    ? '可選 LLM：' + LM.available_count + ' 個；選擇後會更新保密模式預設模型並立即嘗試載入。'
    : '目前沒有抓到可用模型。';
  $('lmEject').disabled = !LM.can_unload_now;
  $('lmSaveCleanup').disabled = false;
  renderModelOptions();
  syncCleanupForm();
}
async function pollLM(showInlineError=false){
  try {
    LM = await (await fetch(kq('/api/admin/lmstudio'))).json();
    renderLM();
  } catch(e){
    if (showInlineError) showErr('LM Studio 狀態讀取失敗：' + e.message);
    $('lmMeta').textContent = 'LM Studio 狀態讀取失敗';
    $('lmStatTarget').textContent = '—';
    $('lmStatLoaded').textContent = '—';
    $('lmStatJobs').textContent = '—';
    $('lmStatEject').textContent = '—';
    $('lmLine1').textContent = e.message || '';
    $('lmLine2').textContent = '';
  }
}
function syncTop(){ $('toTop').classList.toggle('show', window.scrollY > 600); }
$('toTop').onclick = () => window.scrollTo({top:0, behavior:'smooth'});
window.addEventListener('scroll', syncTop, {passive:true});
syncTop();

function render(){
  const q = ($('q').value || '').trim().toLowerCase();
  const st = $('fstate').value;
  const showArch = $('arch').checked;
  let rows = JOBS.filter(j => showArch || !j.archived);
  if (st) rows = rows.filter(j => j.state === st);
  if (q) rows = rows.filter(j =>
    ((j.title || '') + ' ' + (j.client || '')).toLowerCase().includes(q));

  const key = j => (j.date || '') + ' ' + String(j.updated_at || 0).padStart(16, '0');
  const sort = $('sort').value;
  if (sort === 'new')      rows.sort((a,b) => key(b).localeCompare(key(a)));
  else if (sort === 'old') rows.sort((a,b) => key(a).localeCompare(key(b)));
  else if (sort === 'big') rows.sort((a,b) => (b.size_bytes||0) - (a.size_bytes||0));
  else {
    const rank = j => j.needs_user ? 0 : (j.state === 'error' ? 1
                    : (RUNNING.includes(j.state) ? 2 : 3));
    rows.sort((a,b) => rank(a) - rank(b) || key(b).localeCompare(key(a)));
  }

  $('count').textContent = rows.length
    ? rows.length + ' 場會議' + (rows.length !== JOBS.length ? '（共 ' + JOBS.length + '）' : '')
    : '';

  if (!rows.length){
    $('list').innerHTML = '<div class="empty">' +
      (JOBS.length ? '沒有符合條件的會議' : '還沒有任何會議，先去上傳一段錄音吧') + '</div>';
    return;
  }

  $('list').innerHTML = rows.map(j => {
    const bits = [j.client, j.date, dur(j.duration), MT[j.meeting_type],
                  hb(j.size_bytes)].filter(Boolean);
    const open = +j.questions_open || 0;
    const running = RUNNING.includes(j.state);
    return '<a class="job ' + (open ? 'ask ' : '') + (j.archived ? 'arch' : '') + '" href="' +
      esc(kq('/job/' + encodeURIComponent(j.job_id))) + '">' +
      '<div class="jt">' +
        '<span class="badge ' + (SCLS[j.state] || '') + '">' + esc(j.state_label || j.state) + '</span>' +
        (j.archived ? '<span class="badge s-arch">已封存</span>' : '') +
        '<span class="jtitle">' + esc(j.title || j.job_id) + '</span>' +
      '</div>' +
      (bits.length ? '<div class="jmeta">' +
        bits.map(b => '<span>' + esc(b) + '</span>').join('<span>·</span>') + '</div>' : '') +
      (j.error ? '<div class="jmeta" style="color:var(--err)">' +
        esc(String(j.error).slice(0, 160)) + '</div>' : '') +
      (running ? '<div class="pbar' + (j.state === 'transcribing' ? '' : ' indet') +
        '"><i style="' + (j.state === 'transcribing'
          ? 'width:' + Math.round(100 * (+j.progress || 0)) + '%' : '') +
        '"></i></div><div class="pstep">' + esc(stepText(j)) + '</div>' : '') +
      (open ? '<div class="cta">' + open + ' 題待回答 ›</div>' : '') +
    '</a>';
  }).join('');
}

async function poll(){
  try {
    const r = await fetch(kq('/api/jobs?archived=1'));
    const t = await r.text();
    if (!r.ok){
      let m = 'HTTP ' + r.status;
      try { m = JSON.parse(t).error || m; } catch(_){}
      showErr(r.status === 401 ? '權杖無效，請用含 ?k=… 的網址開啟本頁' : m);
      return;
    }
    clearErr();
    if (t === LAST) return;          // nothing moved: leave the DOM alone
    LAST = t;
    JOBS = JSON.parse(t).jobs || [];
    render();
  } catch(e){ showErr('連線失敗：' + e.message); }
}

['q','fstate','sort','arch'].forEach(id =>
  $(id).addEventListener('input', render));
$('lmCleanupMode').addEventListener('change', () => {
  $('lmIdleMinutes').disabled = $('lmCleanupMode').value !== 'idle_eject';
});
$('lmRefresh').onclick = () => pollLM(true);
$('lmLoad').onclick = () => withBtn('lmLoad', '載入中…', '已更新 ✓', async () => {
  const model = $('lmModel').value;
  if (!model) throw new Error('請先選擇模型');
  const r = await fetch(kq('/api/admin/lmstudio/select-load'), {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({model})
  });
  const t = await r.text();
  let j = {}; try { j = t ? JSON.parse(t) : {}; } catch(_){ }
  if (!r.ok) throw new Error((j && j.error) || ('HTTP ' + r.status));
  LM = j.status || LM;
  renderLM();
});
$('lmSaveCleanup').onclick = () => withBtn('lmSaveCleanup', '儲存中…', '已儲存 ✓', async () => {
  const mode = $('lmCleanupMode').value;
  const idle_minutes = $('lmIdleMinutes').value;
  const r = await fetch(kq('/api/admin/lmstudio/cleanup'), {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({mode, idle_minutes})
  });
  const t = await r.text();
  let j = {}; try { j = t ? JSON.parse(t) : {}; } catch(_){ }
  if (!r.ok) throw new Error((j && j.error) || ('HTTP ' + r.status));
  LM = j.status || LM;
  renderLM();
});
$('lmEject').onclick = () => withBtn('lmEject', '釋放中…', '已送出 ✓', async () => {
  const r = await fetch(kq('/api/admin/lmstudio/unload'), {method:'POST'});
  const t = await r.text();
  let j = {}; try { j = t ? JSON.parse(t) : {}; } catch(_){ }
  if (!r.ok) throw new Error((j && j.error) || ('HTTP ' + r.status));
  LM = j.status || LM;
  renderLM();
});
poll();
pollLM();
setInterval(poll, 5000);
setInterval(pollLM, 15000);
</script></body></html>"""


DETAIL_PAGE = r"""<!doctype html>
<html lang="zh-Hant"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>會議</title>
<style>__CSS__
  .hero{margin-bottom:22px}
  .hero .jt{display:flex;align-items:center;gap:9px;flex-wrap:wrap;margin-bottom:8px}
  .hero h1{font-size:24px}
  .prog{margin-top:14px}
  .pbar{height:5px;background:var(--line);border-radius:99px;overflow:hidden;margin-bottom:6px}
  .pbar>i{display:block;height:100%;background:var(--accent);transition:width .3s}
  .pbar.indet>i{width:34%;animation:slide 1.5s cubic-bezier(.4,0,.2,1) infinite}
  @keyframes slide{0%{margin-left:-34%}100%{margin-left:100%}}
  .stack{display:flex;flex-direction:column}
  .ord-meta{order:10}.ord-files{order:20}.ord-ops{order:30}.ord-q{order:40}
  body.mode-awaiting .ord-q{order:15}
  body.mode-awaiting .ord-files{order:20}
  body.mode-awaiting .ord-meta{order:30}
  body.mode-awaiting .ord-ops{order:40}
  .dirty{font-size:13px;color:var(--accent);margin-top:10px}

  /* --- question cards ---------------------------------------------------- */
  .qcard{border:1px solid var(--line);border-radius:14px;padding:18px;margin-bottom:12px}
  .qcard.ans{border-color:#c2e0d0}
  .qhead{display:flex;align-items:center;justify-content:space-between;gap:10px;
    margin-bottom:10px}
  .qn{font-size:12px;color:#a1a1a6;font-variant-numeric:tabular-nums}
  .qq{font-size:16.5px;font-weight:600;line-height:1.5;margin-bottom:12px;
    letter-spacing:-.01em}
  .ev{background:var(--soft);border-radius:10px;padding:12px 14px;margin-bottom:14px}
  .evrow{display:flex;gap:10px;font-size:13.5px;line-height:1.6;color:var(--ink)}
  .evrow+.evrow{margin-top:8px;padding-top:8px;border-top:1px solid var(--line)}
  .ts{flex:0 0 auto;color:var(--sub);font-variant-numeric:tabular-nums;
    font-family:ui-monospace,SFMono-Regular,"SF Mono",Menlo,monospace;font-size:12.5px;
    padding-top:1px}
  .qopts{display:flex;flex-wrap:wrap;gap:8px}
  .opt{display:inline-flex;align-items:center;gap:7px;min-height:44px;padding:10px 15px;
    border:1px solid var(--hair);border-radius:10px;background:#fff;color:var(--ink);
    font:500 15px/1.2 inherit;cursor:pointer;-webkit-appearance:none;appearance:none;
    text-align:left}
  .opt.sel{border-color:var(--accent);background:#f3f7fb;font-weight:600;
    box-shadow:inset 0 0 0 1px var(--accent)}
  .opt em{font-style:normal;font-size:11.5px;font-weight:600;color:var(--sub);
    border:1px solid var(--hair);border-radius:99px;padding:1px 7px;background:#fff}
  .opt.sel em{color:var(--accent);border-color:#ccd8e6}
  .mini{font-size:12.5px;color:var(--sub);margin:14px 0 6px}
  .rep{display:flex;align-items:center;gap:8px;margin-bottom:8px}
  .rep .arrow{color:var(--sub);flex:0 0 auto;font-size:14px}
  .rep .rx{flex:0 0 44px;height:44px;border:1px solid var(--line);border-radius:10px;
    background:#fff;color:var(--sub);font-size:15px;cursor:pointer}
  .addrep{font-size:14px;font-weight:600;color:var(--accent);background:none;border:0;
    padding:10px 0;min-height:44px;cursor:pointer}
  .found{font-size:13.5px;color:var(--sub);line-height:1.7;background:var(--soft);
    border-radius:10px;padding:12px 14px;margin-bottom:12px}
  .qsubmit{display:flex;flex-direction:column;gap:8px;margin-top:22px;
    padding-top:20px;border-top:1px solid var(--line)}
  .carddet{padding:0;overflow:hidden}
  .carddet>summary{padding:18px 20px;margin:0}
  .carddet>.inside{padding:0 20px 20px}
  .qsum{font-size:13px;color:var(--sub);font-weight:500;margin-left:8px}

  /* --- files ------------------------------------------------------------- */
  table{width:100%;border-collapse:collapse;font-size:14.5px}
  td{padding:12px 0;border-top:1px solid var(--line);vertical-align:middle}
  tr:first-child td{border-top:0}
  td.fn{font-weight:500;word-break:break-all;padding-right:12px}
  td.fs{color:var(--sub);white-space:nowrap;font-variant-numeric:tabular-nums;
    text-align:right;padding-right:12px}
  td.fa{white-space:nowrap;text-align:right;width:1%}
  .lnk{display:inline-block;min-height:44px;line-height:44px;padding:0 10px;
    font-size:14.5px;font-weight:600;background:none;border:0;color:var(--accent);
    cursor:pointer;font-family:inherit}
  .fmt{font-size:11.5px;color:var(--sub);border:1px solid var(--hair);border-radius:5px;
    padding:1px 6px;margin-left:7px;text-transform:uppercase;font-weight:600}

  /* --- ops --------------------------------------------------------------- */
  .opsgrp{margin-bottom:18px}
  .opsgrp:last-of-type{margin-bottom:0}
  .opslabel{font-size:12.5px;color:var(--sub);margin-bottom:8px}
  details{border-top:1px solid var(--line);margin-top:20px;padding-top:6px}
  summary{cursor:pointer;font-size:14.5px;font-weight:600;padding:12px 0;
    list-style:none;min-height:44px;display:flex;align-items:center}
  summary::-webkit-details-marker{display:none}
  summary::after{content:'›';margin-left:7px;color:var(--sub);transition:.2s}
  details[open] summary::after{transform:rotate(90deg)}
  pre.log{white-space:pre-wrap;word-break:break-all;font-size:12px;line-height:1.6;
    background:var(--soft);border-radius:10px;padding:14px;max-height:400px;overflow:auto;
    margin:0 0 10px;color:var(--ink)}
</style></head><body><div class="wrap">

<div class="top">
  <a class="nav" id="back" href="/jobs">‹ 全部會議</a>
  <a class="nav" id="navUp" href="/">📤 上傳新錄音</a>
</div>

<div id="err" class="strip hide"></div>

<div class="hero" id="hero">
  <div class="jt"><span class="badge" id="hbadge">讀取中</span></div>
  <h1 id="htitle">…</h1>
  <div class="sub" id="hsub"></div>
  <div class="prog hide" id="hprog">
    <div class="pbar"><i id="hbar"></i></div>
    <div class="sub" id="hstep"></div>
  </div>
</div>

<div class="stack" id="stack">
<section class="card ord-meta" id="secMeta">
  <h2>基本資訊</h2>
  <form id="mform">
    <label class="first">會議主題</label>
    <input id="m_title" placeholder="例：數位轉型第二階段規劃">
    <div class="grid2">
      <div><label>客戶／對象</label><input id="m_client"></div>
      <div><label>日期</label><input id="m_date" type="date"></div>
    </div>
    <label>與會者（用頓號或逗號分隔）</label>
    <input id="m_participants">
    <div class="grid2">
      <div><label>現場約幾人</label>
        <select id="m_nspk">
          <option value="-1">不確定</option>
          <option>2</option><option>3</option><option>4</option><option>5</option>
          <option>6</option><option>7</option><option>8</option>
        </select></div>
      <div><label>會議類型</label>
        <select id="m_mtype">
          <option value="general">一般討論會議</option>
          <option value="client">顧問／客戶會議</option>
          <option value="interview">訪談／研究</option>
        </select></div>
    </div>
    <label>輸出內容</label>
    <div class="opts">
      <label class="chk" id="w_tr_l"><input type="checkbox" id="m_tr"><span>整理過的逐字稿</span></label>
      <label class="chk" id="w_note_l"><input type="checkbox" id="m_note"><span>會議記錄</span></label>
    </div>
    <label>輸出檔案型態</label>
    <div class="opts row">
      <label class="chk" id="w_md_l"><input type="checkbox" id="f_md"><span>Markdown</span></label>
      <label class="chk" id="w_pdf_l"><input type="checkbox" id="f_pdf"><span>PDF</span></label>
      <label class="chk" id="w_docx_l"><input type="checkbox" id="f_docx"><span>Word</span></label>
    </div>
    <label>AI 處理模式</label>
    <div class="opts">
      <label class="chk" id="a_general_l" onclick="setDetailAgentPreset('general')"><input type="radio" name="agent_preset" id="a_general"><span>一般模式（Claude / Codex）</span></label>
      <label class="chk" id="a_private_l" onclick="setDetailAgentPreset('private')"><input type="radio" name="agent_preset" id="a_private"><span>保密模式（LM Studio）</span></label>
    </div>
    <div class="mini">音檔一律留在這台 Mac；保密模式只改變逐字稿後續的 LLM 整理路徑。</div>
    <label>背景／專有名詞</label>
    <textarea id="m_context" placeholder="例：這場會議會提到 ERP、對帳、SaaS，窗口是王經理"></textarea>
    <div class="dirty hide" id="mdirty">有未儲存的變更</div>
    <button type="submit" class="btn primary wide" style="margin-top:20px" id="msave">儲存</button>
  </form>
</section>

<details class="card carddet ord-q hide" id="secQ">
  <summary><span>待確認的問題</span><span class="qsum" id="qsum"></span></summary>
  <div class="inside">
    <div id="qfound"></div>
    <div id="qcards"></div>

    <div style="margin-top:22px;padding-top:20px;border-top:1px solid var(--line)">
      <div class="opslabel" style="font-weight:600;color:var(--ink);font-size:13.5px">全文取代表</div>
      <div class="mini" style="margin-top:2px">整份逐字稿與會議記錄都會套用</div>
      <div id="reps"></div>
      <button type="button" class="addrep" id="addrep">＋ 新增一列</button>
    </div>

    <label>自由補充</label>
    <textarea id="qctx" placeholder="任何有助於寫好這份記錄的背景：人名、專案代號、沒講出口的結論…"></textarea>

    <div class="qsubmit">
      <button type="button" class="btn primary wide" id="qsend">送出並繼續</button>
      <button type="button" class="btn wide" id="qskip">跳過問題，用系統推測跑</button>
    </div>
  </div>
</details>

<section class="card ord-files" id="secFiles">
  <h2>檔案</h2>
  <div id="files"></div>
</section>

<section class="card ord-ops">
  <h2>操作</h2>
  <div class="opsgrp">
    <div class="opslabel">重新產生</div>
    <div class="btnrow">
      <button class="btn" data-stage="scan">重跑（掃描）</button>
      <button class="btn" data-stage="write">重跑（撰寫）</button>
      <button class="btn" data-stage="all">全部重跑</button>
    </div>
  </div>
  <div class="opsgrp">
    <div class="opslabel">收納與空間</div>
    <div class="btnrow">
      <button class="btn" id="btnArch">封存</button>
      <button class="btn" id="btnClean">清理產出</button>
      <button class="btn danger" id="btnDel">完全刪除</button>
    </div>
  </div>
  <details id="logbox">
    <summary>執行紀錄</summary>
    <pre class="log" id="logtxt">讀取中…</pre>
    <button class="btn" id="logRefresh">重新整理</button>
  </details>
</section>
</div>
<button class="fab" id="toTop" type="button" aria-label="回到最上面">↑</button>
<div id="ok" class="strip ok toast hide" aria-live="polite"></div>

<div class="veil hide" id="veil"><div class="modal">
  <div class="mh" id="mo_h"></div>
  <div class="mb" id="mo_b"></div>
  <div class="mf">
    <button class="btn" id="mo_x">取消</button>
    <button class="btn primary" id="mo_ok">確定</button>
  </div>
</div></div>

<script>
const K = new URLSearchParams(location.search).get('k') || localStorage.getItem('mk') || '';
if (K) localStorage.setItem('mk', K);
const $ = id => document.getElementById(id);
const kq = p => p + (K ? (p.includes('?') ? '&' : '?') + 'k=' + encodeURIComponent(K) : '');
const JOB = decodeURIComponent(location.pathname.replace(/^\/job\//, ''));
const B = '/api/job/' + encodeURIComponent(JOB);
$('back').href = kq('/jobs');
$('navUp').href = kq('/');

const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const hb = b => { b = +b || 0; if (b < 1024) return b + ' B';
  const u = ['KB','MB','GB','TB']; let i = -1;
  do { b /= 1024; i++; } while (b >= 1024 && i < 3);
  return b.toFixed(b < 10 ? 1 : 0) + ' ' + u[i]; };
const dur = s => { s = Math.round(+s || 0); if (!s) return '';
  const h = Math.floor(s/3600), m = Math.round(s%3600/60);
  return h ? h + ' 小時 ' + m + ' 分' : Math.max(1, m) + ' 分'; };
const MT = {general:'一般討論', client:'顧問／客戶', interview:'訪談／研究'};
const QT = {speaker:'講者對應', term:'專有名詞', unclear:'辨識不清',
            conflict:'前後矛盾', undecided:'未拍板'};
const SCLS = {awaiting_answers:'s-ask', error:'s-err', done:'s-done',
              transcribing:'s-run', scanning:'s-run', writing:'s-run'};
const RUNNING = ['transcribing','scanning','writing'];
const STEP = {scanning:'正在掃描逐字稿，整理待確認的問題…', writing:'正在撰寫文件…'};

let D = null, Q = null, A = null, META = {};
let META_SNAPSHOT = '';
let okTimer = null;

function metaSnapshot(){
  return JSON.stringify({
    title: $('m_title').value, client: $('m_client').value, date: $('m_date').value,
    participants: $('m_participants').value, num_speakers: $('m_nspk').value,
    meeting_type: $('m_mtype').value, want_transcript: $('m_tr').checked,
    want_note: $('m_note').checked, f_md: $('f_md').checked, f_pdf: $('f_pdf').checked,
    f_docx: $('f_docx').checked, agent_preset: $('a_private').checked ? 'private' : 'general',
    context: $('m_context').value
  });
}
function syncDirty(){
  const dirty = META_SNAPSHOT && metaSnapshot() !== META_SNAPSHOT;
  $('mdirty').classList.toggle('hide', !dirty);
}
function setBodyMode(){
  document.body.classList.toggle('mode-awaiting', D && D.state === 'awaiting_answers');
}
function syncTop(){ $('toTop').classList.toggle('show', window.scrollY > 600); }
$('toTop').onclick = () => window.scrollTo({top:0, behavior:'smooth'});
window.addEventListener('scroll', syncTop, {passive:true});
syncTop();

function showErr(m){ $('ok').classList.add('hide'); $('ok').classList.remove('show');
  const e = $('err'); e.textContent = m; e.classList.remove('hide');
  e.scrollIntoView({block:'nearest', behavior:'smooth'}); }
function showOk(m){ $('err').classList.add('hide');
  const e = $('ok'); e.textContent = m; e.classList.remove('hide');
  requestAnimationFrame(() => e.classList.add('show'));
  clearTimeout(okTimer);
  okTimer = setTimeout(() => { e.classList.remove('show'); setTimeout(() => e.classList.add('hide'), 220); }, 2200); }
function clearErr(){ $('err').classList.add('hide'); }
async function withBtn(id, busyText, okText, fn){
  const b = $(id), old = b.textContent;
  b.disabled = true; b.classList.add('busy'); b.textContent = busyText;
  try {
    const out = await fn();
    b.textContent = okText; b.classList.add('okflash');
    setTimeout(() => { b.textContent = old; b.classList.remove('okflash'); }, 1200);
    return out;
  } finally {
    b.disabled = false; b.classList.remove('busy');
    if (!b.classList.contains('okflash')) b.textContent = old;
  }
}

async function api(path, opts){
  const r = await fetch(kq(path), opts || {});
  const t = await r.text();
  let j = null; try { j = t ? JSON.parse(t) : {}; } catch(_){}
  if (!r.ok){
    const e = new Error((j && j.error) || ('HTTP ' + r.status));
    e.status = r.status; throw e;
  }
  return j || {};
}
const post = (path, body) => api(path, {method:'POST',
  headers:{'Content-Type':'application/json'}, body: JSON.stringify(body || {})});

/* --------------------------------------------------------------- modal --- */
let moOk = null;
function modal(title, html, okLabel, danger, onOk){
  $('mo_h').textContent = title;
  $('mo_b').innerHTML = html;
  $('mo_ok').textContent = okLabel || '確定';
  $('mo_ok').className = 'btn ' + (danger ? 'danger solid' : 'primary');
  $('mo_ok').disabled = false;
  // A modal with no action is a viewer (markdown preview): one button, not two.
  $('mo_x').classList.toggle('hide', !onOk);
  moOk = onOk;
  $('veil').classList.remove('hide');
}
function closeModal(){ $('veil').classList.add('hide'); moOk = null; }
$('mo_x').onclick = closeModal;
$('veil').onclick = e => { if (e.target === $('veil')) closeModal(); };
$('mo_ok').onclick = async () => {
  const fn = moOk; if (!fn) return closeModal();
  $('mo_ok').disabled = true;
  try { await fn(); closeModal(); }
  catch(e){ closeModal(); showErr(e.message); }
  finally { $('mo_ok').disabled = false; }
};

/* ---------------------------------------------------------------- head --- */
function renderHead(){
  setBodyMode();
  const badge = $('hbadge');                 // grabbed before .jt is emptied
  badge.textContent = D.state_label || D.state || '';
  badge.className = 'badge ' + (SCLS[D.state] || '');
  const jt = document.querySelector('.hero .jt');
  jt.innerHTML = '';
  jt.appendChild(badge);
  if (D.archived){
    const b = document.createElement('span');
    b.className = 'badge s-arch'; b.textContent = '已封存';
    jt.appendChild(b);
  }
  if (+D.questions_open > 0){
    const b = document.createElement('span');
    b.className = 'badge s-ask'; b.textContent = D.questions_open + ' 題待回答';
    jt.appendChild(b);
  }
  $('htitle').textContent = D.title || D.job_id;
  document.title = (D.title || D.job_id) + ' · 會議';
  const bits = [D.client, D.date, dur(D.duration),
    D.num_speakers ? D.num_speakers + ' 位講者' : '',
    MT[D.meeting_type], hb(D.size_bytes)].filter(Boolean);
  $('hsub').textContent = bits.join(' · ');

  const running = RUNNING.includes(D.state);
  $('hprog').classList.toggle('hide', !running);
  if (running){
    const asr = D.state === 'transcribing';
    $('hbar').parentNode.classList.toggle('indet', !asr);
    $('hbar').style.width = asr ? Math.round(100 * (+D.progress || 0)) + '%' : '';
    $('hstep').textContent = asr
      ? (D.step_label || '轉寫中') + (D.message ? ' — ' + D.message : '')
      : (STEP[D.state] || D.state_label || '');
  }
  if (D.error) showErr(String(D.error).slice(0, 400));
  $('btnArch').textContent = D.archived ? '取消封存' : '封存';
}

/* ---------------------------------------------------------------- meta --- */
const FCHK = [['m_tr','w_tr_l'],['m_note','w_note_l'],['f_md','w_md_l'],
              ['f_pdf','w_pdf_l'],['f_docx','w_docx_l']];
const APRESET = [['a_general','a_general_l'],['a_private','a_private_l']];
function setDetailAgentPreset(preset){
  $('a_private').checked = preset === 'private';
  $('a_general').checked = preset !== 'private';
  syncChk();
  syncDirty();
}
function syncChk(){
  FCHK.forEach(([c,w]) => $(w).classList.toggle('on', $(c).checked));
  APRESET.forEach(([c,w]) => $(w).classList.toggle('on', $(c).checked));
}
FCHK.forEach(([c,w]) => {
  $(c).addEventListener('change', () => { syncChk(); syncDirty(); });
  $(w).addEventListener('click', e => { if (e.target !== $(c)) { $(c).click(); } });
});
APRESET.forEach(([c,w]) => {
  $(c).addEventListener('change', () => { syncChk(); syncDirty(); });
  $(w).addEventListener('click', e => {
    if (e.target === $(c)) return;
    setDetailAgentPreset(c === 'a_private' ? 'private' : 'general');
  });
});
['m_title','m_client','m_date','m_participants','m_nspk','m_mtype','m_context'].forEach(id =>
  $(id).addEventListener('input', syncDirty));

function renderMeta(){
  $('m_title').value = META.title || D.title || '';
  $('m_client').value = META.client || D.client || '';
  $('m_date').value = META.date || D.date || '';
  $('m_participants').value = META.participants || D.participants || '';
  $('m_nspk').value = String(META.num_speakers != null ? META.num_speakers : -1);
  if (!$('m_nspk').value || $('m_nspk').selectedIndex < 0) $('m_nspk').value = '-1';
  $('m_mtype').value = D.meeting_type || 'general';
  $('m_tr').checked = !!D.want_transcript;
  $('m_note').checked = !!D.want_note;
  const f = D.formats || [];
  $('f_md').checked = f.includes('md');
  $('f_pdf').checked = f.includes('pdf');
  $('f_docx').checked = f.includes('docx');
  const preset = (META.agent_preset || D.agent_preset || 'general');
  setDetailAgentPreset(preset);
  $('m_context').value = META.context || '';
  syncChk();
  META_SNAPSHOT = metaSnapshot();
  syncDirty();
}

$('mform').onsubmit = async e => {
  e.preventDefault();
  const formats = [];
  if ($('f_md').checked) formats.push('md');
  if ($('f_pdf').checked) formats.push('pdf');
  if ($('f_docx').checked) formats.push('docx');
  if (!$('m_note').checked && !$('m_tr').checked)
    return showErr('請至少勾選一項輸出內容（逐字稿或會議記錄）');
  if (!formats.length)
    return showErr('請至少勾選一種輸出檔案型態');
  try {
    await withBtn('msave', '儲存中…', '已儲存 ✓', async () => {
      const j = await post(B + '/meta', {
        title: $('m_title').value, client: $('m_client').value, date: $('m_date').value,
        participants: $('m_participants').value, num_speakers: $('m_nspk').value,
        meeting_type: $('m_mtype').value, want_transcript: $('m_tr').checked,
        want_note: $('m_note').checked, formats: formats,
        agent_preset: $('a_private').checked ? 'private' : 'general',
        context: $('m_context').value});
      META = j.meta || META; D = j.summary || D;
      clearErr(); renderHead(); renderMeta(); renderFiles(); showOk('已儲存');
    });
  } catch(err){ showErr('儲存失敗：' + err.message); }
};

/* ----------------------------------------------------------- questions --- */
function renderQ(){
  const cards = (Q && Q.cards) || [];
  if (!cards.length){ $('secQ').classList.add('hide'); return; }
  $('secQ').classList.remove('hide');
  const answered = Math.min(+((D && D.questions_answered) || 0), cards.length);
  const open = Math.max(0, cards.length - answered);
  let qsum = '保留作為參考';
  if (D && D.state === 'awaiting_answers')
    qsum = open > 0 ? `還有 ${open} 題待回答，回答後才會開始撰寫` : '可直接送出並開始撰寫';
  else if (D && (D.state === 'scanning' || D.state === 'writing'))
    qsum = answered > 0 ? `已收到 ${answered} 題回答，系統處理中` : '系統整理中';
  else if (D && D.state === 'error')
    qsum = answered > 0 ? `上次已收到 ${answered} 題回答，可重跑後沿用` : '上次流程中斷，保留作為參考';
  else if (answered > 0)
    qsum = `已回答 ${answered} / ${cards.length} 題，保留作為參考`;
  $('qsum').textContent = qsum;
  $('secQ').open = !!(D && D.state === 'awaiting_answers');

  const prev = (A && A.cards) || {};
  const found = (Q && Q.replacements) || [];
  $('qfound').innerHTML = found.length
    ? '<div class="found"><b>系統已找到的錯字修正：</b><br>' + found.map(r =>
        esc(r.find) + ' → ' + esc(r.replace) + (r.note ? '（' + esc(r.note) + '）' : '')
      ).join('<br>') + '</div>'
    : '';

  $('qcards').innerHTML = cards.map((c, i) => {
    const saved = prev[c.id] || {};
    const chosen = saved.choice != null && saved.choice !== ''
      ? saved.choice : (c.best_guess || '');
    const opts = (c.options || []).slice();
    if (c.best_guess && !opts.includes(c.best_guess)) opts.unshift(c.best_guess);
    const ev = (c.evidence || []).map(e =>
      '<div class="evrow"><span class="ts">' + esc(e.timestamp || '') + '</span>' +
      '<span>' + esc(e.text || '') + '</span></div>').join('');
    return '<div class="qcard' + (saved.choice || saved.custom ? ' ans' : '') +
      '" data-id="' + esc(c.id) + '" data-choice="' + esc(chosen) + '">' +
      '<div class="qhead"><span class="badge">' + esc(QT[c.type] || c.type || '確認') +
        '</span><span class="qn">' + (i + 1) + ' / ' + cards.length + '</span></div>' +
      '<div class="qq">' + esc(c.question || '') + '</div>' +
      (ev ? '<div class="ev">' + ev + '</div>' : '') +
      '<div class="qopts">' + opts.map(o =>
        '<button type="button" class="opt' + (o === chosen ? ' sel' : '') +
        '" data-v="' + esc(o) + '">' + esc(o) +
        (o === c.best_guess ? '<em>建議</em>' : '') + '</button>').join('') + '</div>' +
      '<div class="mini">或自己填</div>' +
      '<input class="custom" placeholder="輸入你的答案（會蓋過上面的選擇）" value="' +
        esc(saved.custom || '') + '">' +
    '</div>';
  }).join('');

  $('qcards').querySelectorAll('.opt').forEach(btn => {
    btn.onclick = () => {
      const card = btn.closest('.qcard');
      card.dataset.choice = btn.dataset.v;
      card.classList.add('ans');
      card.querySelectorAll('.opt').forEach(b => b.classList.toggle('sel', b === btn));
      if (/皆非|補充|其他|自己/.test(btn.dataset.v)) card.querySelector('.custom').focus();
    };
  });

  $('reps').innerHTML = '';
  const saved = (A && A.replacements) || [];
  if (saved.length) saved.forEach(r => addRep(r.find, r.replace));
  else addRep('', '');
  $('qctx').value = (A && A.context) || '';
}

function addRep(f, t){
  const row = document.createElement('div');
  row.className = 'rep';
  row.innerHTML = '<input class="rf" placeholder="原文"><span class="arrow">→</span>' +
    '<input class="rt" placeholder="改成"><button type="button" class="rx">✕</button>';
  row.querySelector('.rf').value = f || '';
  row.querySelector('.rt').value = t || '';
  row.querySelector('.rx').onclick = () => {
    row.remove();
    if (!$('reps').children.length) addRep('', '');
  };
  $('reps').appendChild(row);
}
$('addrep').onclick = () => addRep('', '');

function collectCards(){
  const out = {};
  $('qcards').querySelectorAll('.qcard').forEach(el => {
    out[el.dataset.id] = {choice: el.dataset.choice || '',
                          custom: (el.querySelector('.custom').value || '').trim()};
  });
  return out;
}
function collectReps(){
  return Array.from($('reps').querySelectorAll('.rep')).map(r => ({
    find: (r.querySelector('.rf').value || '').trim(),
    replace: (r.querySelector('.rt').value || '').trim()
  })).filter(r => r.find);
}

async function sendAnswers(skipped){
  const btnId = skipped ? 'qskip' : 'qsend';
  const busy = skipped ? '套用推測中…' : '送出中…';
  const done = skipped ? '已開始 ✓' : '已送出 ✓';
  $('qsend').disabled = $('qskip').disabled = true;
  try {
    await withBtn(btnId, busy, done, async () => {
      await post(B + '/answers', skipped
        ? {skipped: true, cards: {}, replacements: [], context: $('qctx').value}
        : {skipped: false, cards: collectCards(), replacements: collectReps(),
           context: $('qctx').value});
    });
    showOk(skipped ? '已用系統推測開始撰寫' : '已送出，開始撰寫');
    await load();
    window.scrollTo({top:0, behavior:'smooth'});
  } catch(e){ showErr('送出失敗：' + e.message); }
  finally { $('qsend').disabled = $('qskip').disabled = false; }
}
$('qsend').onclick = () => sendAnswers(false);
$('qskip').onclick = () => modal('跳過所有問題？',
  '系統會直接採用每一題的建議答案開始撰寫。之後仍可以在這頁重跑。',
  '跳過並開始撰寫', false, () => sendAnswers(true));

/* --------------------------------------------------------------- files --- */
function renderFiles(){
  const fs = D.files || [];
  if (!fs.length){
    $('files').innerHTML = '<div class="sub">還沒有產出檔案。' +
      (D.outputs_cleaned ? '（產出已清理，可用上方「重跑」重新產生）' : '') + '</div>';
    return;
  }
  $('files').innerHTML = '<table>' + fs.map(f =>
    '<tr><td class="fn">' + esc(f.display_name || f.name) + '<span class="fmt">' + esc(f.fmt) + '</span></td>' +
    '<td class="fs">' + hb(f.size) + '</td><td class="fa">' +
    (f.fmt === 'md' ? '<button class="lnk" data-prev="' + esc(f.name) + '" data-title="' + esc(f.display_name || f.name) + '">預覽</button>' : '') +
    '<a class="lnk" download="' + esc(f.display_name || f.name) + '" href="' + esc(kq('/files/' + encodeURIComponent(JOB) + '/' +
      encodeURIComponent(f.name))) + '">下載</a></td></tr>').join('') + '</table>';

  $('files').querySelectorAll('[data-prev]').forEach(b => {
    b.onclick = async () => {
      const n = b.dataset.prev;
      modal(b.dataset.title || n, '<div class="sub">讀取中…</div>', '關閉', false, null);
      try {
        const j = await api(B + '/raw/' + encodeURIComponent(n));
        $('mo_b').innerHTML = '<pre></pre>';
        $('mo_b').querySelector('pre').textContent = j.text || '(空白)';
      } catch(e){ $('mo_b').innerHTML = '<div class="strip"></div>';
        $('mo_b').querySelector('.strip').textContent = e.message; }
    };
  });
}

/* ----------------------------------------------------------------- ops --- */
async function rerun(stage, force){
  try {
    await post(B + '/rerun', {stage: stage, force: !!force});
    showOk('已開始重跑');
    setTimeout(load, 600);
  } catch(e){
    if (e.status === 409)
      modal('這場會議還在跑', esc(e.message), '仍要重跑', false,
            () => rerun(stage, true));
    else showErr('重跑失敗：' + e.message);
  }
}
document.querySelectorAll('[data-stage]').forEach(b =>
  b.onclick = () => rerun(b.dataset.stage, false));

$('btnArch').onclick = async () => {
  try { await post(B + '/archive', {archived: !D.archived});
        showOk(D.archived ? '已取消封存' : '已封存'); await load(); }
  catch(e){ showErr(e.message); }
};

$('btnClean').onclick = async () => {
  try {
    const p = await api(B + '/cleanup-preview');
    const n = p.outputs.names || [];
    modal('清理產出',
      '會刪除 <b>' + n.length + '</b> 個產出檔案，釋出 <b>' + hb(p.outputs.bytes) +
      '</b>。<br>錄音、逐字稿、會議設定都會保留，之後可以重跑重新產生。' +
      (n.length ? '<pre style="margin-top:12px">' + esc(n.join('\n')) + '</pre>' : ''),
      '清理', false, async () => {
        const r = await post(B + '/clean');
        showOk('已釋出 ' + hb(r.freed_bytes)); await load();
      });
  } catch(e){ showErr(e.message); }
};

$('btnDel').onclick = async () => {
  try {
    const p = await api(B + '/cleanup-preview');
    modal('完全刪除這場會議',
      '<b>這個動作無法復原。</b><br>會刪除整個資料夾，包含錄音 ' + hb(p.audio.bytes) +
      '、逐字稿與所有產出，總共釋出 <b>' + hb(p.total.bytes) + '</b>。' +
      '<div style="margin-top:14px;color:var(--ink)">請輸入 <b>DELETE</b> 以確認</div>' +
      '<input id="delword" autocapitalize="characters" autocorrect="off" ' +
      'spellcheck="false" style="margin-top:8px" placeholder="DELETE">',
      '永久刪除', true, async () => {
        if (($('delword') || {}).value !== 'DELETE') return;
        await api(B, {method:'DELETE', headers:{'Content-Type':'application/json'},
                      body: JSON.stringify({confirm: 'DELETE'})});
        location.href = kq('/jobs');
      });
    // The button stays dead until the word is typed exactly.
    $('mo_ok').disabled = true;
    const inp = $('delword');
    inp.oninput = () => { $('mo_ok').disabled = inp.value.trim() !== 'DELETE'; };
    setTimeout(() => inp.focus(), 60);
  } catch(e){ showErr(e.message); }
};

async function loadLog(){
  try { const j = await api(B + '/log?lines=200');
        $('logtxt').textContent = j.log || '(還沒有執行紀錄)'; }
  catch(e){ $('logtxt').textContent = '讀取失敗：' + e.message; }
}
$('logbox').addEventListener('toggle', () => { if ($('logbox').open) loadLog(); });
$('logRefresh').onclick = e => { e.preventDefault(); loadLog(); };

/* ---------------------------------------------------------------- load --- */
async function load(){
  try {
    const j = await api(B);
    D = j.summary; Q = j.questions; A = j.answers; META = j.meta || {};
    if (!D.error) clearErr();
    renderHead(); renderMeta(); renderQ(); renderFiles();
  } catch(e){
    showErr(e.status === 401
      ? '權杖無效，請用含 ?k=… 的網址開啟本頁' : '讀取失敗：' + e.message);
  }
}

/* Background refresh keeps the badge, progress bar and file list live without
   ever touching the form or the question cards the user is filling in. */
async function refreshQuiet(){
  try {
    const j = await api(B);
    const wasQ = !!(Q && (Q.cards || []).length);
    D = j.summary; Q = j.questions; A = j.answers;
    renderHead(); renderFiles();
    if (!wasQ && Q && (Q.cards || []).length) renderQ();   // questions just arrived
    if (wasQ && !(Q && (Q.cards || []).length)) $('secQ').classList.add('hide');
  } catch(_){}
}
load();
setInterval(refreshQuiet, 5000);
</script></body></html>"""

JOBS_PAGE = JOBS_PAGE.replace("__CSS__", CSS)
DETAIL_PAGE = DETAIL_PAGE.replace("__CSS__", CSS)


@app.get("/jobs", response_class=HTMLResponse)
def jobs_page(k: str = ""):
    return JOBS_PAGE      # token is checked per-API-call, like the upload page


@app.get("/job/{job_id}", response_class=HTMLResponse)
def job_page(job_id: str, k: str = ""):
    jd(job_id)            # 404 early rather than rendering a shell for nothing
    return DETAIL_PAGE


# ------------------------------------------------------------------- API ----
@app.get("/api/admin/lmstudio")
def api_admin_lmstudio(k: str = ""):
    check(k)
    return lmstudio_status_payload()


@app.post("/api/admin/lmstudio/select-load")
async def api_admin_lmstudio_select_load(request: Request, k: str = ""):
    check(k)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "請提供 JSON request body")
    if not isinstance(body, dict):
        raise HTTPException(400, "請求內容必須是物件")
    model = str(body.get("model") or "").strip()
    if not model:
        raise HTTPException(400, "缺少 model")
    st = lmstudio_status_payload()
    if st.get("active_count"):
        raise HTTPException(409, "仍有保密任務執行中，現在先不要切換模型")
    available = {str(m.get("key") or "").strip(): m for m in st.get("available_models") or []}
    if model not in available:
        raise HTTPException(404, f"LM Studio 找不到模型：{model}")

    current = str(st.get("model") or "").strip()
    current_loaded = current and current in (st.get("loaded_models") or [])
    target_loaded = bool(available[model].get("loaded"))
    unload_result = None
    try:
        if model != current and current_loaded and st.get("can_unload_now"):
            unload_result = lmstudio_runtime.unload_now(reason=f"switch_model:{current}->{model}")
        if target_loaded:
            inst = (available[model].get("loaded_instances") or [{}])[0]
            load_result = {
                "ok": True,
                "skipped": True,
                "reason": "already_loaded",
                "instance_id": inst.get("id") if isinstance(inst, dict) else str(inst or model),
            }
        else:
            load_result = lmstudio_runtime.load_model(model)
        saved = save_private_model(model)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        hint = "；可先按『立即釋放模型』再重試" if exc.code >= 500 else ""
        raise HTTPException(exc.code if 400 <= exc.code < 600 else 502,
                            f"LM Studio 載入失敗：HTTP {exc.code} {raw[:240]}{hint}")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"LM Studio 載入失敗：{type(exc).__name__}: {exc}")
    return {
        "ok": True,
        "saved_model": saved,
        "unload_result": unload_result,
        "load_result": load_result,
        "status": lmstudio_status_payload(),
    }


@app.post("/api/admin/lmstudio/cleanup")
async def api_admin_lmstudio_cleanup(request: Request, k: str = ""):
    check(k)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "請提供 JSON request body")
    if not isinstance(body, dict):
        raise HTTPException(400, "請求內容必須是物件")
    try:
        saved = save_private_cleanup(body.get("mode"), body.get("idle_minutes"))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "saved_cleanup": saved, "status": lmstudio_status_payload()}


@app.post("/api/admin/lmstudio/unload")
def api_admin_lmstudio_unload(k: str = ""):
    check(k)
    st = lmstudio_status_payload()
    if st.get("active_count"):
        raise HTTPException(409, "仍有保密任務執行中，現在先不卸載模型")
    if not st.get("can_unload_now"):
        if st.get("foreign_loaded_count"):
            raise HTTPException(409, "目前載入中的不是本系統保密模式目標模型，先不代為卸載其他工作負載")
        raise HTTPException(409, "目前沒有可由本系統安全卸載的保密模式模型")
    try:
        result = lmstudio_runtime.unload_now(reason="manual_web")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"LM Studio 卸載失敗：{type(exc).__name__}: {exc}")
    return {"ok": True, "result": result, "status": lmstudio_status_payload()}


@app.get("/api/jobs")
def api_jobs(k: str = "", archived: int = 1):
    check(k)
    return {"jobs": jobstate.all_jobs(include_archived=bool(archived))}


@app.get("/api/job/{job_id}")
def api_job(job_id: str, k: str = ""):
    check(k)
    d = jd(job_id)
    # The upload page polls this endpoint and reads state/progress/step_label/
    # message/result/error straight off status.json, so those stay at the top
    # level exactly as before; the review UI reads the nested keys.
    status = read_json(d / "status.json") or {
        "state": "running", "step_label": "啟動中", "progress": 0, "message": ""}
    payload = dict(status)
    payload["summary"] = jobstate.summary(d)
    payload["questions"] = read_json(d / "questions.json")
    payload["answers"] = read_json(d / "answers.json")
    payload["meta"] = read_json(d / "meta.json") or {}
    return JSONResponse(payload)


# Meta fields the web form owns. Anything else already in meta.json (upload
# bookkeeping like `_orig`, or keys a future version adds) is merged through
# untouched.
META_FIELDS = {"title", "client", "date", "participants", "num_speakers",
               "meeting_type", "want_note", "want_transcript", "formats",
               "context", "language", "agent_preset"}


@app.post("/api/job/{job_id}/meta")
async def api_job_meta(job_id: str, req: Request, k: str = ""):
    check(k)
    d = jd(job_id)
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(400, "請求內容不是合法的 JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "請求內容必須是物件")

    meta = read_json(d / "meta.json") or {}
    for key in META_FIELDS & set(body):
        meta[key] = body[key]
    meta = normalize_outputs(meta)
    atomic_json(d / "meta.json", meta)
    return {"ok": True, "meta": meta, "summary": jobstate.summary(d)}


@app.post("/api/job/{job_id}/answers")
async def api_job_answers(job_id: str, req: Request, k: str = ""):
    check(k)
    d = jd(job_id)
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(400, "請求內容不是合法的 JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "請求內容必須是物件")

    cards = body.get("cards") or {}
    if not isinstance(cards, dict):
        raise HTTPException(400, "cards 必須是物件")
    reps = [r for r in (body.get("replacements") or [])
            if isinstance(r, dict) and str(r.get("find") or "").strip()]

    answers = {
        "answered_at": time.time(),
        "skipped": bool(body.get("skipped")),
        "cards": {
            str(cid): {"choice": str((v or {}).get("choice") or ""),
                       "custom": str((v or {}).get("custom") or "")}
            for cid, v in cards.items() if isinstance(v, dict)
        },
        "replacements": [{"find": str(r["find"]), "replace": str(r.get("replace") or "")}
                         for r in reps],
        "context": str(body.get("context") or ""),
    }
    # Written before anything can fail: the user's answers are the one thing
    # here that cannot be recomputed.
    atomic_json(d / "answers.json", answers)
    require_review()

    jobstate.set_state(d, "writing",
                       "跳過問題，用系統推測" if answers["skipped"] else "已回答，開始撰寫")
    spawn_review(d.name, d, "write")
    return {"ok": True, "state": "writing", "answers": answers}


RUNNING_STATES = {"scanning", "writing"}


@app.post("/api/job/{job_id}/rerun")
async def api_job_rerun(job_id: str, req: Request, k: str = ""):
    check(k)
    d = jd(job_id)
    try:
        body = await req.json()
    except Exception:
        body = {}
    stage = str((body or {}).get("stage") or "scan")
    if stage not in ("scan", "write", "all"):
        raise HTTPException(400, "stage 必須是 scan / write / all")

    st = jobstate.load(d)
    if st.get("state") in RUNNING_STATES and not (body or {}).get("force"):
        raise HTTPException(409, f"這場會議正在{jobstate.STATES[st['state']][0]}，"
                                 f"確定要中斷並重跑嗎？")
    require_review()

    arg = "scan" if stage == "all" else stage
    jobstate.set_state(d, "scanning" if arg == "scan" else "writing",
                       f"手動重跑 --stage {arg}")
    spawn_review(d.name, d, arg)
    return {"ok": True, "stage": arg}


@app.post("/api/job/{job_id}/archive")
async def api_job_archive(job_id: str, req: Request, k: str = ""):
    check(k)
    d = jd(job_id)
    try:
        body = await req.json()
    except Exception:
        body = {}
    flag = bool((body or {}).get("archived"))
    jobstate.save(d, archived=flag)
    return {"ok": True, "archived": flag}


@app.get("/api/job/{job_id}/cleanup-preview")
def api_cleanup_preview(job_id: str, k: str = ""):
    check(k)
    return cleanup_plan(jd(job_id))


@app.post("/api/job/{job_id}/clean")
def api_job_clean(job_id: str, k: str = ""):
    check(k)
    d = jd(job_id)
    plan = cleanup_plan(d)

    removed: list[str] = []
    for g in jobstate.OUTPUT_GLOBS:
        for p in sorted(d.glob(g)):
            if not p.is_file() or is_protected(p.name):
                continue
            try:
                p.unlink()
                removed.append(p.name)
            except OSError:
                pass
    for sub in SCRATCH_DIRS:
        sd = d / sub
        if sd.is_dir():
            shutil.rmtree(sd, ignore_errors=True)
            removed.append(sub + "/")

    jobstate.save(d, outputs_cleaned=True)
    return {"ok": True, "removed": removed,
            "freed_bytes": plan["outputs"]["bytes"],
            "size_bytes": jobstate.dir_size(d)}


@app.delete("/api/job/{job_id}")
async def api_job_delete(job_id: str, req: Request, k: str = ""):
    check(k)
    d = jd(job_id)                     # resolved first: traversal is impossible
    try:
        body = await req.json()
    except Exception:
        body = {}
    if (body or {}).get("confirm") != "DELETE":
        raise HTTPException(400, '請在請求中帶上 {"confirm": "DELETE"}')

    freed = jobstate.dir_size(d)
    shutil.rmtree(d)
    # The jobs/*.json sidecar only describes a folder that no longer exists.
    sidecar = JOBS / f"{d.name}.json"
    if sidecar.is_file():
        sidecar.unlink()
    return {"ok": True, "freed_bytes": freed}


@app.get("/api/job/{job_id}/log")
def api_job_log(job_id: str, k: str = "", lines: int = 200):
    check(k)
    d = jd(job_id)
    lines = max(1, min(int(lines or 200), 2000))
    agent = LOGS / f"{d.name}-agent.log"
    plain = LOGS / f"{d.name}.log"
    src = agent if agent.exists() else plain
    return {"log": tail_file(src, lines), "source": src.name if src.exists() else ""}


@app.get("/api/job/{job_id}/raw/{name}")
def api_job_raw(job_id: str, name: str, k: str = ""):
    check(k)
    p = safe_child(jd(job_id), name)
    if p.stat().st_size > 400_000:
        raise HTTPException(413, "檔案太大，請直接下載")
    return {"text": p.read_text(errors="replace"), "name": name}


@app.get("/files/{job_id}/{name}")
def download(job_id: str, name: str, k: str = ""):
    check(k)
    d = jd(job_id)
    p = safe_child(d, name)
    s = jobstate.summary(d)
    disp = jobstate.display_name(
        name,
        client=s.get("client") or "",
        title=s.get("title") or name,
        meeting_date=s.get("date") or "",
    )
    return FileResponse(p, filename=disp)


if __name__ == "__main__":
    port = int(os.environ.get("MEETING_PORT") or CONFIG["port"])
    print(f"token: {TOKEN}", file=sys.stderr)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
