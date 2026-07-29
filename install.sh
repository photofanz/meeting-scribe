#!/bin/bash
# ---------------------------------------------------------------------------
# meeting-scribe installer (macOS / Apple Silicon)
#
#   git clone <repo> ~/Meetings && cd ~/Meetings && ./install.sh
#
# Idempotent: safe to re-run after editing config.json or pulling updates.
# ---------------------------------------------------------------------------
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

bold() { printf "\033[1m%s\033[0m\n" "$*"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$*"; }
die()  { printf "  \033[31m✗\033[0m %s\n" "$*" >&2; exit 1; }

SKIP_SERVICE=0
[ "${1:-}" = "--no-service" ] && SKIP_SERVICE=1

# --------------------------------------------------------------- 1. platform
bold "1/6  Checking platform"
[ "$(uname -s)" = "Darwin" ] || die "macOS only — the ASR stage uses Apple Metal (mlx-whisper)."
if [ "$(uname -m)" != "arm64" ]; then
  warn "Not Apple Silicon. mlx-whisper will not run; transcription will fail."
  warn "Continuing anyway so you can inspect the rest of the install."
else
  ok "macOS $(sw_vers -productVersion) on $(uname -m)"
fi

need() { command -v "$1" >/dev/null 2>&1; }

# ffmpeg and uv are hard requirements; pandoc is only needed for .docx output
# (and gives nicer tables in PDF), so a failed pandoc install is not fatal.
MISSING=()
need ffmpeg || MISSING+=(ffmpeg)
need uv     || MISSING+=(uv)
need pandoc || MISSING+=(pandoc)
if [ ${#MISSING[@]} -gt 0 ]; then
  if need brew; then
    bold "     Installing missing tools: ${MISSING[*]}"
    brew install "${MISSING[@]}" || warn "brew install reported an error; re-checking individually"
  else
    warn "Homebrew not found. Install it from https://brew.sh, or install"
    warn "these manually: ${MISSING[*]}"
  fi
fi
need ffmpeg || die "ffmpeg is required (audio normalization). brew install ffmpeg"
need uv     || die "uv is required (creates the Python 3.12 venv). brew install uv"
ok "ffmpeg $(ffmpeg -version 2>/dev/null | head -1 | awk '{print $3}')"
ok "uv $(uv --version 2>/dev/null | awk '{print $2}')"
if need pandoc; then
  ok "pandoc $(pandoc --version 2>/dev/null | head -1 | awk '{print $2}')"
else
  warn "pandoc not found — Word (.docx) output will be unavailable (brew install pandoc)"
fi
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
[ -x "$CHROME" ] || warn "Google Chrome not found — PDF output will be unavailable (it renders via headless Chrome)"

# ------------------------------------------------------------------ 2. dirs
bold "2/6  Creating directories"
mkdir -p inbox jobs archive logs models .uploads
ok "inbox/ jobs/ archive/ logs/ models/ .uploads/"

# ---------------------------------------------------------------- 3. config
bold "3/6  Configuration"
if [ ! -f config.json ]; then
  cp config.example.json config.json
  ok "config.json created from template — edit it to set port / branding / notifier"
else
  ok "config.json already exists (left untouched)"
fi

if [ ! -f .token ]; then
  python3 -c "import secrets;print(secrets.token_urlsafe(18))" > .token
  chmod 600 .token
  ok "upload token generated (.token, mode 600)"
else
  ok "upload token already exists"
fi

# ------------------------------------------------------------------- 4. venv
bold "4/6  Python environment"
if [ ! -x .venv/bin/python ]; then
  uv venv --python 3.12 .venv
fi
uv pip install --python .venv/bin/python -r requirements.txt --quiet
ok "$(.venv/bin/python -V) with $(.venv/bin/python -c 'import mlx_whisper,sherpa_onnx,fastapi;print("mlx-whisper, sherpa-onnx, fastapi")')"

# ----------------------------------------------------------------- 5. models
# ~100 MB of diarization models. The ASR model (~1.5 GB) is pulled by
# mlx-whisper from HuggingFace on first transcription, not here.
bold "5/6  Diarization models"

SEG_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
EMB_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx"
#            note the upstream typo in the tag name ^^^^^^^^^^^ — it is correct.
SEG_SHA="220ad67ca923bef2fa91f2390c786097bf305bceb5e261d4af67b38e938e1079"
EMB_SHA="f682b514c05d947ee3fa91cd6ec6c5c7543479a128373fa29b1faedccd21fd11"

verify() { [ -f "$1" ] && [ "$(shasum -a 256 "$1" | awk '{print $1}')" = "$2" ]; }

if verify "models/sherpa-onnx-pyannote-segmentation-3-0/model.onnx" "$SEG_SHA"; then
  ok "segmentation model present"
else
  echo "     downloading segmentation model (~6 MB)…"
  curl -fL --progress-bar -o /tmp/seg.tar.bz2 "$SEG_URL"
  tar xf /tmp/seg.tar.bz2 -C models/ && rm -f /tmp/seg.tar.bz2
  verify "models/sherpa-onnx-pyannote-segmentation-3-0/model.onnx" "$SEG_SHA" \
    || die "segmentation model checksum mismatch"
  ok "segmentation model verified"
fi

if verify "models/3dspeaker_campplus_zh.onnx" "$EMB_SHA"; then
  ok "speaker embedding model present"
else
  echo "     downloading speaker embedding model (~28 MB)…"
  curl -fL --progress-bar -o models/3dspeaker_campplus_zh.onnx "$EMB_URL"
  verify "models/3dspeaker_campplus_zh.onnx" "$EMB_SHA" \
    || die "embedding model checksum mismatch"
  ok "speaker embedding model verified"
fi

chmod +x bin/*.sh bin/*.py 2>/dev/null || true

# ---------------------------------------------------------------- 6. service
bold "6/6  Background service (launchd)"
LABEL="$(.venv/bin/python bin/config.py service_label)"
PORT="$(.venv/bin/python bin/config.py port)"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"

if [ "$SKIP_SERVICE" = "1" ]; then
  warn "--no-service given; skipping launchd setup"
else
  mkdir -p "$HOME/Library/LaunchAgents"
  sed -e "s|__LABEL__|${LABEL}|g" \
      -e "s|__ROOT__|${ROOT}|g" \
      -e "s|__HOME__|${HOME}|g" \
      templates/launchd.plist.template > "$PLIST"
  ok "$PLIST"

  launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  sleep 2

  CODE="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/" || true)"
  if [ "$CODE" = "200" ]; then
    ok "service running and answering on port ${PORT}"
  else
    warn "service did not answer (HTTP '${CODE}') — check logs/server.log"
  fi
fi

# ------------------------------------------------------------------- report
IP="$(tailscale ip -4 2>/dev/null | head -1)"
if [ -z "$IP" ]; then
  IP="$(ipconfig getifaddr en0 2>/dev/null || echo 127.0.0.1)"
  warn "Tailscale not detected — the URL below is LAN-only."
  warn "Without Tailscale the upload page is reachable by anything on your"
  warn "local network. The token is the only thing protecting it."
fi

echo
bold "Done."
echo
echo "  Upload page:"
echo "    http://${IP}:${PORT}/?k=$(cat .token)"
echo
echo "  Manage:"
echo "    ./bin/service.sh status | restart | log 50 | url"
echo
echo "  Automatic login must be ON for the service to survive a reboot"
echo "  unattended — this is a LaunchAgent and starts at login, not at boot."
echo
