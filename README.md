# meeting-scribe

[繁體中文版本](README.zh-TW.md)

**A local-first meeting intelligence pipeline for Apple Silicon Macs.**

`meeting-scribe` turns meeting recordings into deliverable transcripts and formal meeting documents. It covers the full workflow from **upload, transcription, speaker diarization, review, document generation, and delivery**, all running on your own Mac. It is not a cloud SaaS product or a one-off model demo. It is a local meeting-processing infrastructure layer designed for real operational workflows.

## What you can enable right after deployment

`meeting-scribe` is not a loose collection of scripts. It is a deployable, continuously operable local meeting-processing solution:

- **Mobile-friendly upload entry point**: Upload large recordings from a browser with chunked transfer, automatic retry, and a workflow designed for long-form audio.
- **On-device transcription and speaker diarization pipeline**: Built on Apple Silicon, `mlx-whisper`, and ONNX diarization for speed, privacy, and control.
- **Traceable and replayable job system**: Every meeting has its own state, artifacts, and intermediate materials for rewriting, auditing, and delivery.
- **Review workflow for long meetings**: Uses chunked scanning, question cards, and answer injection to reduce summary drift and factual errors in long transcripts.
- **Switchable AI backends**: Supports `claude`, `codex`, chat-style agents, and LM Studio / OpenAI-compatible APIs.
- **Document generation and delivery**: Outputs transcripts, meeting notes, action items, and deliverables in `Markdown / PDF / Word`.
- **Long-running local deployment mode**: Uses `install.sh` and `launchd` to provide a maintainable internal service.

---

## Core capabilities

| Capability | Description |
|---|---|
| **Local-first transcription** | Audio is normalized, transcribed, and converted from simplified to traditional Chinese locally, without relying on the Whisper API or third-party transcription SaaS. |
| **Speaker-aware processing** | Uses `sherpa-onnx` + CAM++ zh for speaker clustering, with headcount-aware fallback support. |
| **Long-meeting reliability** | Large transcripts can be chunked and scanned in parallel to avoid silent failure from a single prompt over long content. |
| **Interactive review** | Names, jargon, amounts, and conflicting statements are turned into answerable question cards before they enter final deliverables. |
| **Dual AI operating modes** | General mode can use Claude / Codex; privacy mode can use local LM Studio models. |
| **Controlled model lifecycle** | Includes private model cleanup strategies: `keep_loaded`, `idle_eject`, and `after_job`. |
| **Multi-format deliverables** | Can output `transcript` and `meeting note` formats in `md`, `pdf`, and `docx`. |
| **Replayability and auditability** | Preserves source files, raw transcripts, answers, and state so documents can be regenerated without re-uploading audio. |
| **Notification and delivery integration** | Supports `none`, `telegram`, `command`, and `webhook` notification modes. |

---

## Product positioning

`meeting-scribe` is built for work environments that need to balance **privacy, accuracy, and deliverability**:

- **Consultants, sales teams, PMs, and managers**: Need to turn long meetings into sendable formal records and action items.
- **Teams that care about privacy and data boundaries**: Do not want client meetings, interviews, or internal discussions sent to third-party cloud services.
- **Internal workstations built around Apple Silicon**: Want a dedicated Mac to act as a stable internal meeting-processing node.
- **Long meetings and high-risk content**: Cannot accept summaries that look complete while actually covering only the first part of the meeting.

It is not recording hardware and not a public cloud collaboration platform. It is closer to a **deployable local meeting-processing layer** for internal use.

---

## System requirements

| Item | Spec |
|---|---|
| **OS** | macOS |
| **Hardware** | Apple Silicon required |
| **ASR backend** | `mlx-whisper` (Metal GPU) |
| **Diarization** | `sherpa-onnx-pyannote-segmentation-3-0` + `3D-Speaker CAM++ zh` |
| **Recommended memory** | 32 GB or more |
| **Peak memory for a 2-hour meeting** | About 14.6 GB |
| **Input** | m4a / common audio formats (normalized via ffmpeg) |
| **Output** | `transcript.md/.json/.txt`, `transcript_clean.md/.pdf/.docx`, `note_*.md/.pdf/.docx`, `action_items.json` |
| **Deployment** | Local repo + Python venv + `launchd` |
| **Network** | Tailscale recommended; without it, the upload page should be treated as LAN-only |

### Measured performance

On Apple Silicon, a 2-hour recording can be transcribed in about **13 minutes**, roughly **9–10× realtime**. See [`BENCHMARK.md`](BENCHMARK.md) for detailed benchmarks and model comparisons.

---

## Architecture overview

```text
Mobile recording / audio file
      │
      ▼
Chunked upload Web UI
      │
      ▼
ffmpeg normalization ──┬── ASR (mlx-whisper / Metal)
                       └── Speaker diarization (sherpa-onnx / ONNX)
      │
      ▼
transcript.md / transcript.json / transcript.txt
      │
      ▼
review pipeline
  ├─ scan: chunk, parallel scan, generate question cards
  ├─ answer: user answers key uncertainties at /job/<id>
  └─ write: Python applies answers, then AI writes formal documents
      │
      ▼
Markdown / PDF / Word / action_items / delivery
```

The point of this design is not to add extra process for its own sake. It is meant to handle real-world issues such as **long transcripts, speaker mismatch, terminology ambiguity, and unclear key numbers** in a stable way. See [`docs/REVIEW.md`](docs/REVIEW.md) for the rationale behind the review stage.

---

## Quick start

### 1. Install

```bash
git clone https://github.com/photofanz/meeting-scribe.git ~/Meetings
cd ~/Meetings
./install.sh
```

`install.sh` will:

1. Check the platform and required tools
2. Create the folder structure
3. Generate `config.json` and `.token`
4. Create a Python 3.12 venv and install dependencies
5. Download diarization models with SHA256 verification
6. Install and load the `launchd` LaunchAgent

> The project does not have to live in `~/Meetings`; all scripts derive the root path from their own location.

### 2. Get the upload URL

```bash
./bin/service.sh url
```

### 3. Upload audio and choose outputs

The upload form supports:

- Output type: transcript / meeting note
- Meeting type: general discussion / consulting client / research interview
- File format: PDF / Markdown / Word
- AI preset: general mode / privacy mode

### 4. Wait for transcription and review

The default `agent.mode = "review"` means:

- The system first completes the transcript and scan stage
- If there are key uncertainties, question cards appear at `/job/<id>`
- Final cleaned transcripts and meeting notes are generated only after you answer them

---

## Output and data structure

Each meeting creates a dedicated job directory under `archive/`:

```text
archive/<YYYY-MM-DD>_<subject>_<6-char>/
    source.m4a
    meta.json
    status.json
    state.json
    transcript.md
    transcript.json
    transcript.txt
    questions.json
    answers.json
    transcript_draft.md
    transcript_clean.md
    note_*.md
    action_items.json
    delivery.json
    agent_report.json
    INDEX.md
```

### What gets preserved

`meeting-scribe` is not designed to throw everything away after a run. It preserves enough material for replay and audit:

- **Always preserved**: source audio, raw transcript, question-card answers, job metadata
- **Regenerable**: draft transcript, clean transcript, notes, PDF, Word, and temporary review artifacts

This means you can:

- Rewrite the same meeting notes after changing templates
- Regenerate documents after correcting names or terminology
- Re-deliver outputs without re-uploading the recording

---

## AI / agent integration

### Supported writing backends

| Type | Use |
|---|---|
| **Chat-style agent** | Manual handoff to Hermes / Claude Code–style agents |
| **Local CLI** | Background execution with `claude` / `codex` for automation |
| **OpenAI-compatible API** | For example LM Studio, suitable for privacy mode |

### Two default operating modes

| Mode | Typical backend | Use |
|---|---|---|
| **General mode** | `claude` / `codex` | Everyday meeting processing |
| **Privacy mode** | `openai_compat` + LM Studio | Sensitive meetings and local-model workflows |

### LM Studio cleanup strategy

| Mode | Behavior |
|---|---|
| `keep_loaded` | Keep the model resident for fastest next use |
| `idle_eject` | Unload automatically after `idle_minutes` of inactivity |
| `after_job` | Unload immediately after each privacy-mode job |

The system also exposes an LM Studio management status card that makes the following visible:

- Target model
- Currently loaded model
- Whether a privacy-mode job is still running
- Whether manual model release is allowed

For more on agent wiring and manual vs automated workflows, see [`docs/AGENT.md`](docs/AGENT.md).

---

## Main configuration

During installation, `config.json` is generated from `config.example.json`. Common sections include:

| Section | Purpose |
|---|---|
| `port` / `service_label` | Web service and `launchd` settings |
| `notify` | Notification mode and target |
| `branding` | Document branding and footer |
| `asr` | Whisper model, speaker threshold, fallback behavior |
| `agent.mode` | `review` / `auto` / `manual` |
| `agent.profiles` | Model backends for general mode and privacy mode |
| `agent.private_cleanup` | Cleanup strategy for privacy-mode models |

### Notification modes

| mode | Description |
|---|---|
| `none` | No push notifications; check status only at `/jobs` |
| `telegram` | Send notifications through a Telegram bot |
| `command` | Call your own CLI or script |
| `webhook` | POST JSON into an internal system |

Interactive setup tool:

```bash
.venv/bin/python bin/notify_setup.py
```

---

## Operations commands

```bash
./bin/service.sh status
./bin/service.sh url
./bin/service.sh restart
./bin/service.sh log 50
./bin/service.sh rotate
./bin/service.sh disable
```

### Process a job manually

```bash
.venv/bin/python bin/review.py latest --stage scan
.venv/bin/python bin/review.py latest --stage write --deliver
.venv/bin/python bin/review.py latest --stage auto --deliver
.venv/bin/python bin/chunker.py archive/<job_id>/transcript.md
```

---

## Project structure

| File / module | Role |
|---|---|
| `bin/upload_server.py` | Upload page, jobs page, job detail page, and question-card UI |
| `bin/process_meeting.py` | Audio normalization, ASR, speaker diarization, transcript shaping |
| `bin/review.py` | The `scan → answer → write` workflow for long meetings |
| `bin/agent_note.py` | Single-pass document writing |
| `bin/lmstudio_runtime.py` | LM Studio status, model loading, and cleanup decisions |
| `bin/notify.py` | Unified outbound notification entry point |
| `bin/make_pdf.py` / `bin/make_docx.py` | Document format conversion |
| `templates/NOTE_SPECS.md` | Contract/spec for meeting-note outputs |
| `templates/SCAN_TASK.md` / `PARTIAL_TASK.md` / `AGENT_TASK.md` | Prompt templates for AI stages |

---

## Documentation guide

| Document | Content |
|---|---|
| [`docs/AGENT.md`](docs/AGENT.md) | How to connect Claude / Codex / chat agents / LM Studio |
| [`docs/REVIEW.md`](docs/REVIEW.md) | Why the review stage exists and how it prevents failure |
| [`BENCHMARK.md`](BENCHMARK.md) | Runtime speed, memory usage, and model selection data |
| [`docs/DIARIZATION_RESEARCH.md`](docs/DIARIZATION_RESEARCH.md) | Background research and conditional fallback logic for speaker diarization |

---

## Limits and deployment boundaries

These are not TODOs. They are the current real boundaries of the system:

1. **macOS + Apple Silicon only.** ASR depends on the Metal backend in `mlx-whisper`.
2. **Speaker diarization is not speaker naming.** The system can separate speaker clusters, but name mapping still requires confirmation in the review stage.
3. **Online meeting recordings may degrade diarization quality significantly.** Multiple devices, microphones, and network conditions reduce speaker consistency.
4. **Transcripts still require review.** Especially for names, terminology, numbers, dates, and amounts.
5. **This is not a public internet service.** It is best used inside Tailscale or a LAN; the upload page is protected by a token, not a full account system.
6. **Split recordings longer than 3 hours when possible.** The system can process them, but peak resource usage and waiting time increase.

---

## Security model

- `.gitignore` uses a **deny-all + whitelist** strategy to reduce the chance of committing real meeting data by mistake
- `config.json`, `.token`, `archive/`, `logs/`, `models/`, and `.venv/` are not version-controlled
- Job cleanup removes only regenerable artifacts, not original evidence or configuration materials
- Privacy-mode model release logic avoids unloading foreign loaded models that belong to other workloads

---

## License

MIT
