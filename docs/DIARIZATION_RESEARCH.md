# Diarization research note

Date: 2026-07-30
Project: `meeting-scribe`

## Problem

Real meeting recordings are massively over-segmented by the current diarization stack:

- segmentation: `sherpa-onnx-pyannote-segmentation-3-0`
- embedding: `3D-Speaker CAM++ zh`
- clustering: `FastClusteringConfig(num_clusters=-1, threshold=0.75)`
- preprocessing: `ffmpeg ... -ac 1` (downmix stereo to mono)

## What the current code does

`bin/process_meeting.py`

- normalizes every upload to **mono** at 16 kHz
- ignores the user-supplied speaker count during clustering
- relies on clustering threshold only
- uses word timestamps later to split ASR segments across speaker changes

Relevant lines:

- `normalize()` downmixes to mono with `-ac 1`
- `diarize()` uses `FastClusteringConfig(num_clusters=-1, threshold=0.75)`

## Evidence from real jobs

Archive scan (`meta.num_speakers` vs `status.result.num_speakers`):

| job | declared | got | duration |
|---|---:|---:|---:|
| 2026-07-24_林老師_9973f6 | 2 | 112 | 7658 s |
| 2026-07-30_士盟國際通運_909c94 | 4 | 105 | 5399 s |
| 2026-07-29_DT研究團隊_5843b6 | 3 | 61 | 3746 s |
| 2026-07-29_Sharon-貨運服務需求者_c640fb | 2 | 25 | 795 s |

So the failure is not occasional. It is systematic on real meeting data.

## Evidence about the source audio

All 4 archived recordings are stereo `m4a` at 48 kHz.

Channel correlation after converting to 16 kHz stereo:

| job | L/R correlation |
|---|---:|
| 林老師 | 0.5345 |
| DT研究團隊 | 0.5762 |
| Sharon 訪談 | 0.6029 |
| 士盟國際通運 | 0.3483 |

Interpretation:

- the two channels are **not identical dual-mono**
- current `-ac 1` downmix is likely destroying useful spatial / side separation cues
- however, naive single-channel-only diarization is not enough by itself

## Quick experiments

### A. Sharon interview (declared 2 speakers, full file)

Current stack on normalized mono:

- auto clustering, threshold 0.75 → **25 clusters**

Channel-only tests:

- left only → 22 clusters
- right only → 28 clusters

Conclusion:

- simply choosing L or R instead of mono does **not** fix the problem
- but mono downmix is still suspicious and should not remain the only path

### B. Sharon interview: force cluster count

Using the same current models, same mono-preprocessed file:

| setting | result |
|---|---:|
| auto, threshold 0.75 | 25 clusters |
| auto, threshold 0.85 | 15 clusters |
| auto, threshold 0.90 | 14 clusters |
| `num_clusters=2` | **2 clusters** |
| `num_clusters=3` | 2 clusters |
| `num_clusters=4` | 3 clusters |

### C. 士盟 4 人會議：前 10 分鐘 excerpt

| setting | result |
|---|---:|
| auto, threshold 0.75 | 13 clusters |
| auto, threshold 0.85 | 9 clusters |
| auto, threshold 0.90 | 9 clusters |
| `num_clusters=4` | **4 clusters** |
| `num_clusters=5` | 5 clusters |

Conclusion from B + C:

- on the real problem recordings, **constraining cluster count dramatically reduces over-segmentation**
- this contradicts the earlier clean benchmark in `BENCHMARK.md`, where forcing 4 speakers hurt accuracy
- therefore the right answer is **not** “always force count” or “never force count”
- the right answer is a **conditional fallback / selection strategy**

## External research signals

### pyannote community-1

Official sources claim three relevant improvements:

1. improved **speaker assignment and counting**
2. **exclusive speaker diarization** for easier STT alignment
3. offline/local use is supported after model download

Sources:

- `https://huggingface.co/pyannote/speaker-diarization-community-1`
- `https://www.pyannote.ai/blog/community-1`

This makes `community-1` the strongest replacement candidate for the current diarization backend.

### sherpa-onnx docs

Official docs expose both:

- threshold-based auto clustering
- explicit `numClusters`

So the current code is using only one side of the tool's control surface.

Source:

- `https://k2-fsa.github.io/sherpa/onnx/speaker-diarization/index.html`

## Root-cause hypothesis

This looks like a **speaker counting / clustering robustness failure on real meeting audio**, not just a naming problem.

Likely contributors:

1. **automatic speaker count estimation is unstable** on long, mixed-quality meeting recordings
2. **stereo is being collapsed to mono too early**
3. overlap / backchannel / echo / remote-call artifacts create many micro-embeddings
4. the current pipeline has **no rerun logic** when clustering output is obviously absurd

## Recommendation

### Best path: two-stage diarization policy

#### Path 1 — keep current sherpa path as fast baseline
Run current diarization first.

#### Path 2 — add deterministic fallback when result is implausible
Trigger fallback if, for example:

- declared speaker count exists, and
- `got > max(declared*2, declared+3)`

When triggered:

1. rerun diarization with constrained counts:
   - `num_clusters = declared`
   - optionally also `declared + 1`
2. compare candidates with internal heuristics:
   - cluster count closeness to declared count
   - fewer ultra-short speaker alternations
   - fewer single-fragment clusters
   - lower fragmentation per minute
3. choose the best candidate automatically

This is the lowest-risk near-term fix because it keeps the existing stack and only adds fallback logic.

### Stronger medium-term path: add `pyannote/speaker-diarization-community-1`

Use it as:

- either the new default diarizer
- or the fallback backend when sherpa output is implausible

Why this is attractive:

- official focus on **better counting**
- official **exclusive diarization** helps merge with Whisper timestamps
- supports offline/local use after one-time model download

Trade-offs:

- likely slower / heavier than current ONNX path
- needs Hugging Face gated model acceptance + token once
- likely adds PyTorch dependency and larger install footprint

### What I would *not* do

- do **not** rely on LLM renaming to solve a broken clustering problem
- do **not** permanently keep “never force speaker count” as a universal rule
- do **not** assume left-only/right-only channel extraction is enough

## Proposed implementation order

### Option A — pragmatic fix first
1. keep current sherpa backend
2. add absurdity detector
3. rerun with constrained `num_clusters`
4. add candidate scoring to auto-pick the less fragmented output
5. record both raw and chosen diarization stats in JSON for later evaluation

### Option B — better model path
1. add pluggable diarization backend interface
2. keep `sherpa` as default
3. add `pyannote-community-1` backend
4. benchmark both on the 4 archived recordings
5. choose default based on real DER / practical speaker-count sanity

## My recommendation

If the goal is **fastest way to stop 25/61/105/112-speaker nonsense without LLM guessing**, do this first:

> **Implement a deterministic fallback in the existing sherpa pipeline that reruns with constrained cluster counts when auto clustering is obviously absurd.**

Then, if you want the more correct long-term architecture:

> **Add pyannote `community-1` as a second backend and benchmark it against those 4 real meeting files.**

That gives you:

- immediate practical relief
- no dependence on LLM guessing
- a clean path toward a stronger diarization backend later
