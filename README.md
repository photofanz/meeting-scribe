# meeting-scribe

[繁體中文版本](README.zh-TW.md)

**A local-first meeting transcription and review system for Apple Silicon Macs.**

`meeting-scribe` is a demo project that shows how a single Mac can handle the full path from **audio upload to transcript, review, meeting-note generation, and final delivery**. It is built for teams that want more control than cloud note-taking tools, and for builders exploring practical patterns for long-form meeting processing.

Rather than presenting transcription as a single API call, this project demonstrates a complete workflow: browser upload, local ASR, speaker diarization, question-driven review, AI writing backends, and multi-format deliverables.

## Highlights

- **Browser-based upload for large recordings**: Supports chunked upload, retry, and long-form audio intake from phones or laptops.
- **Fully local transcription path**: Runs on Apple Silicon with `mlx-whisper`, `ffmpeg`, and ONNX-based diarization.
- **Review before final output**: Surfaces unclear names, numbers, jargon, and contradictions as question cards instead of letting them silently leak into the final note.
- **Regenerable job artifacts**: Preserves source audio, raw transcripts, answers, and state so outputs can be rewritten without starting over.
- **Multiple writing backends**: Can hand off to `claude`, `codex`, local chat-style agents, or LM Studio through an OpenAI-compatible interface.
- **Delivery-ready exports**: Produces transcript and meeting-note outputs in `Markdown`, `PDF`, and `DOCX`.
- **Simple local deployment**: Installs as a long-running `launchd` service for internal use.

---

## Core capabilities

| Capability | Description |
|---|---|
| **Local-first transcription** | Audio is normalized and transcribed locally instead of being sent to a transcription SaaS by default. |
| **Speaker-aware processing** | Uses `sherpa-onnx` + CAM++ zh for speaker clustering, with conditional fallback strategies for weaker diarization cases. |
| **Review-centered reliability** | Long transcripts are chunked, scanned, and reviewed before final writing, which helps reduce hallucinated certainty in high-risk details. |
| **Interactive clarification flow** | Ambiguous names, terms, figures, and conflicting statements are collected into answerable question cards. |
| **Dual AI operating modes** | General mode can use Claude / Codex; privacy mode can use local LM Studio models. |
| **Replayability and auditability** | Source files, transcripts, answers, and state are preserved so the same job can be rerun or audited later. |
| **Multi-format deliverables** | Can output `transcript` and `meeting note` artifacts in `md`, `pdf`, and `docx`. |
| **Notification integration** | Supports `none`, `telegram`, `command`, and `webhook` notification modes. |

---

## What this repo is for

`meeting-scribe` is best read as:

- **an internal tooling demo** for long-form meeting processing,
- **a reference architecture** for local-first transcription + review workflows,
- **and a practical starting point** for teams that want more control over sensitive meeting data.

It is **not** a polished multi-tenant SaaS product, a public internet service, or a universal cross-platform recorder. The value of this repo is in the workflow design: how to turn unreliable raw transcripts into outputs that are reviewable, repeatable, and delivery-ready.

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
| **Network** | Tailscale recommended; otherwise treat the upload page as LAN-only |

### Measured performance

On Apple Silicon, a 2-hour recording can be transcribed in about **13 minutes**, roughly **9–10× realtime**. See [`BENCHMARK.md`](BENCHMARK.md) for benchmark details and model comparisons.

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

This extra review stage is the core idea of the project. It exists to handle real-world failure modes such as **speaker mismatch, terminology ambiguity, unclear numbers, and long transcripts that exceed what a single prompt can reliably process**. See [`docs/REVIEW.md`](docs/REVIEW.md) for the design rationale.

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
