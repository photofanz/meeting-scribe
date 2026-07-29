#!/bin/bash
# run_job.sh <job_id> <outdir> <pipeline command...>
#
# The thin shell wrapper that outlives the HTTP request: the upload handler
# spawns this detached, and everything after transcription is decided here
# rather than in the request that started it.
#
#   transcribe -> notify("transcribed") -> hand off per agent.mode
#
# agent.mode:
#   manual  stop here; a human starts the write
#   review  run bin/review.py --stage scan, which parks the job in
#           `awaiting_answers` and notifies
#   auto    scan and write back-to-back, delivering the finished documents
#
# Everything past transcription is best-effort: the transcript is already on
# disk, so a downstream failure is reported, never fatal.
set -uo pipefail

JOB_ID="$1"; shift
OUTDIR="$1"; shift

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

CFG() { "$PY" "$ROOT/bin/config.py" "$@" 2>/dev/null; }
AGENT_MODE="$(CFG agent mode || echo review)"

state() { "$PY" "$ROOT/bin/jobstate.py" "$JOB_ID" --set "$1" --note "$2" >/dev/null 2>&1; }

state transcribing "run_job.sh start"

"$@"
RC=$?

STATUS="$OUTDIR/status.json"

# ---------------------------------------------------------------- failure --
if [ $RC -ne 0 ] || [ ! -f "$STATUS" ]; then
  ERR=$( [ -f "$STATUS" ] \
    && "$PY" -c "import json;print((json.load(open('$STATUS')).get('error') or '')[:300])" 2>/dev/null \
    || echo "pipeline exited $RC" )
  state error "$ERR"
  "$PY" "$ROOT/bin/notify.py" --event error --job-id "$JOB_ID" \
      --title "會議轉寫失敗" \
      --body "$ERR

log: $ROOT/logs/$JOB_ID.log" >/dev/null 2>&1
  exit $RC
fi

# --------------------------------------------------------------- succeeded --
# Compose the "transcription finished" message in Python: it needs meta.json,
# status.json and the agent mode to say what happens next, which is more
# structure than bash should be carrying.
MSG=$("$PY" - "$STATUS" "$JOB_ID" "$OUTDIR" "$AGENT_MODE" <<'PYEOF'
import json, os, sys

status_path, jid, outdir, agent_mode = sys.argv[1:5]
s = json.load(open(status_path))
r = s.get("result") or {}

meta = {}
mp = os.path.join(outdir, "meta.json")
if os.path.exists(mp):
    try:
        meta = json.load(open(mp))
    except Exception:
        pass

NOTE = {
    "general":   ("一般討論會議", "會議記錄"),
    "client":    ("顧問／客戶會議", "三版會議記錄（客戶／內部覆盤／夥伴）"),
    "interview": ("訪談／研究", "訪談紀要＋重點語錄"),
}
mtype = (meta.get("meeting_type") or "general").strip() or "general"
label, note_desc = NOTE.get(mtype, NOTE["general"])

want_note = meta.get("want_note", True)
want_tr = meta.get("want_transcript", False)
if not want_note and not want_tr:
    want_note = True

FMT_NAME = {"pdf": "PDF", "md": "Markdown", "docx": "Word"}
fmts = [f for f in ("pdf", "md", "docx") if f in (meta.get("formats") or [])] or ["pdf", "md"]

want = (["整理過的逐字稿"] if want_tr else []) + ([note_desc] if want_note else [])
tail = {
    "review": "接著會掃描逐字稿，整理出需要你確認的問題，完成後通知你到網頁回答。",
    "auto":   "文件正在背景撰寫中，寫完會自動送出。",
    "manual": "等你指示後才會開始整理。",
}.get(agent_mode, "")


def hms(x):
    x = int(x or 0)
    return f"{x // 3600:02d}:{(x % 3600) // 60:02d}:{x % 60:02d}"


print(f"""**{r.get('title') or '未命名會議'}**
對象：{r.get('client') or '—'} · 類型：{label}
長度：{hms(r.get('duration'))} · 聲紋切出 {r.get('num_speakers')} 群
耗時：{r.get('elapsed_sec')}s（{r.get('realtime_factor')}× 實時，全程本機）
產出：{'、'.join(want)} · {'＋'.join(FMT_NAME[f] for f in fmts)}

{tail}""")
PYEOF
)

"$PY" "$ROOT/bin/notify.py" --event transcribed --job-id "$JOB_ID" \
    --title "會議轉寫完成" --body "$MSG" >/dev/null 2>&1

# ---------------------------------------------------------------- handoff --
case "$AGENT_MODE" in
  manual)
    # The transcript is the deliverable for now. review.py picks the job up
    # whenever someone presses a button in the web UI.
    ;;
  review|auto)
    STAGE=$([ "$AGENT_MODE" = "auto" ] && echo auto || echo scan)
    "$PY" "$ROOT/bin/review.py" "$OUTDIR" --stage "$STAGE" --deliver \
        >> "$ROOT/logs/$JOB_ID-agent.log" 2>&1 || {
      echo "[run_job] review.py --stage $STAGE failed for $JOB_ID" \
        >> "$ROOT/logs/$JOB_ID.log"
      state error "review --stage $STAGE 失敗，詳見 logs/$JOB_ID-agent.log"
    }
    ;;
esac

exit 0
