#!/bin/bash
# run_job.sh <job_id> <outdir> <pipeline command...>
# Runs the local transcription pipeline, then pings Telegram via `hermes send`.
set -uo pipefail

JOB_ID="$1"; shift
OUTDIR="$1"; shift

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"

CFG() { "$PY" "$ROOT/bin/config.py" "$@" 2>/dev/null; }
HERMES="$(CFG notify bin || echo "$HOME/.local/bin/hermes")"
HERMES="${HERMES/#\~/$HOME}"
TARGET="$(CFG notify target || echo telegram)"
NOTIFY="$(CFG notify enabled || echo True)"

"$@"
RC=$?

STATUS="$OUTDIR/status.json"

if [ $RC -eq 0 ] && [ -f "$STATUS" ]; then
  MSG=$("$PY" - "$STATUS" "$JOB_ID" "$OUTDIR" <<'PYEOF'
import json, os, sys
s = json.load(open(sys.argv[1])); jid = sys.argv[2]; outdir = sys.argv[3]
r = s.get("result") or {}
meta = {}
mp = os.path.join(outdir, "meta.json")
if os.path.exists(mp):
    try: meta = json.load(open(mp))
    except Exception: pass
mtype = (meta.get("meeting_type") or "general").strip() or "general"
NOTE = {
    "general":   ("一般討論會議", "會議記錄"),
    "client":    ("顧問／客戶會議", "三版會議記錄（客戶／內部覆盤／夥伴）＋寄信草稿"),
    "interview": ("訪談／研究", "訪談紀要＋重點語錄"),
}
label, note_desc = NOTE.get(mtype, NOTE["general"])

want_note = meta.get("want_note", True)
want_tr = meta.get("want_transcript", False)
if not want_note and not want_tr:
    want_note = True
FMT_NAME = {"pdf": "PDF", "md": "Markdown", "docx": "Word"}
fmts = [f for f in ("pdf", "md", "docx") if f in (meta.get("formats") or [])] or ["pdf", "md"]
fmt_label = "＋".join(FMT_NAME[f] for f in fmts)

want = []
if want_tr:
    want.append("整理過的逐字稿")
if want_note:
    want.append(note_desc)
want_line = f"產出：{'、'.join(want)} · {fmt_label}"

steps = ["清稿"]
if want_tr:
    steps.append("逐字稿定稿")
if want_note:
    steps.append(note_desc)
steps.append("歸檔")
plan = " → ".join(steps)
def hms(x):
    x = int(x or 0); return f"{x//3600:02d}:{(x%3600)//60:02d}:{x%60:02d}"
print(f"""🎙️ 會議轉寫完成

**{r.get('title') or '未命名會議'}**
對象：{r.get('client') or '—'} · 類型：{label}
長度：{hms(r.get('duration'))} · 講者：{r.get('num_speakers')} 位
耗時：{r.get('elapsed_sec')}s（{r.get('realtime_factor')}× 實時，全程本機）
{want_line}

Job: `{jid}`

回覆「整理這場會議」，我會跑：
{plan}""")
PYEOF
)
else
  ERR=$( [ -f "$STATUS" ] && "$PY" -c "import json,sys;print((json.load(open('$STATUS')).get('error') or '')[:300])" || echo "pipeline exited $RC" )
  MSG="⚠️ 會議轉寫失敗

Job: \`$JOB_ID\`
$ERR

log: $ROOT/logs/$JOB_ID.log"
fi

# No notifier configured (or it failed) is not fatal — the transcript is already
# on disk. Park the message so nothing is silently lost.
if [ "$NOTIFY" = "True" ] && [ -x "$HERMES" ]; then
  "$HERMES" send --to "$TARGET" "$MSG" >/dev/null 2>&1 || \
    echo "$MSG" >> "$ROOT/logs/undelivered.log"
else
  echo "$MSG" >> "$ROOT/logs/undelivered.log"
fi

exit $RC
