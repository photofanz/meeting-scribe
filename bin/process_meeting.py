#!/usr/bin/env python
"""
Local meeting audio pipeline — 100% on-device.

  audio -> ffmpeg normalize -> speaker diarization (sherpa-onnx)
        -> ASR (mlx-whisper large-v3-turbo, Metal)
        -> merge -> transcript.json / transcript.md

No audio or text ever leaves this machine.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import wave
from dataclasses import asdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import CONFIG, ROOT  # noqa: E402
from diarization import (  # noqa: E402
    DiarizationCandidate,
    SpkSeg,
    build_fallback_plan,
    choose_best_candidate,
    compute_stats,
    score_stats,
    should_run_fallback,
)

MODELS = ROOT / "models"
SEG_MODEL = MODELS / "sherpa-onnx-pyannote-segmentation-3-0" / "model.onnx"
# CAM++ zh: benchmarked 26.7x realtime vs eres2netv2 6.2x, identical accuracy.
# int8 segmentation was rejected — it over-split 2 speakers into 4.
EMB_MODEL = MODELS / "3dspeaker_campplus_zh.onnx"
DIAR_THREADS = CONFIG["asr"]["diarization_threads"]   # 4 beat 10 (ONNX thread thrash)
WHISPER_MODEL = CONFIG["asr"]["whisper_model"]
DIAR_THRESHOLD = CONFIG["asr"].get("diarization_threshold", 0.75)
DIAR_MAX_SPEAKERS = CONFIG["asr"].get("diarization_max_speakers", 8)
DIAR_STEREO_FALLBACK = CONFIG["asr"].get("diarization_stereo_fallback", True)
FFMPEG_AUDIO_FILTER = "highpass=f=60,loudnorm=I=-18:TP=-2:LRA=11"


# ----------------------------------------------------------------- status ---
class Status:
    """Writes a JSON progress file the upload UI / Hermes can poll."""

    STEPS = [
        ("normalize", "音訊正規化"),
        ("diarize", "發言者切分"),
        ("asr", "語音辨識"),
        ("merge", "合併逐字稿"),
        ("done", "完成"),
    ]

    def __init__(self, path: Path):
        self.path = path
        self.data = {
            "state": "running",
            "step": None,
            "step_label": None,
            "progress": 0.0,
            "started_at": time.time(),
            "updated_at": time.time(),
            "message": "",
            "error": None,
            "result": None,
        }
        self.asr_done = False

    def set(self, step: str, message: str = "", progress: float | None = None):
        labels = dict(self.STEPS)
        self.data["step"] = step
        self.data["step_label"] = labels.get(step, step)
        self.data["message"] = message
        if progress is not None:
            self.data["progress"] = progress
        else:
            idx = [s for s, _ in self.STEPS].index(step) if step in labels else 0
            self.data["progress"] = idx / (len(self.STEPS) - 1)
        self.data["updated_at"] = time.time()
        self.flush()
        print(f"[{step}] {message}", flush=True)

    def fail(self, err: str):
        self.data["state"] = "error"
        self.data["error"] = err
        self.data["updated_at"] = time.time()
        self.flush()
        print(f"[ERROR] {err}", file=sys.stderr, flush=True)

    def finish(self, result: dict):
        self.data["state"] = "done"
        self.data["step"] = "done"
        self.data["step_label"] = "完成"
        self.data["progress"] = 1.0
        self.data["result"] = result
        self.data["updated_at"] = time.time()
        self.flush()

    def flush(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2))
        tmp.replace(self.path)


# -------------------------------------------------------------- normalize ---
def probe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def probe_audio_channels(path: Path) -> int:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=channels",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    try:
        return int(out.stdout.strip())
    except ValueError:
        return 1


def normalize(src: Path, dst: Path) -> float:
    """16 kHz mono PCM wav + light loudness normalization for ASR."""
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-i", str(src),
         "-vn", "-ac", "1", "-ar", "16000",
         "-af", FFMPEG_AUDIO_FILTER,
         "-c:a", "pcm_s16le", str(dst)],
        check=True,
    )
    return probe_duration(dst)


def prepare_diarization_wav(src: Path, dst: Path, channel: str) -> None:
    if channel == "mono":
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
            "-af", FFMPEG_AUDIO_FILTER, "-c:a", "pcm_s16le", str(dst),
        ]
    elif channel == "left":
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(src), "-vn", "-ar", "16000",
            "-af", f"pan=mono|c0=FL,{FFMPEG_AUDIO_FILTER}",
            "-c:a", "pcm_s16le", str(dst),
        ]
    elif channel == "right":
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(src), "-vn", "-ar", "16000",
            "-af", f"pan=mono|c0=FR,{FFMPEG_AUDIO_FILTER}",
            "-c:a", "pcm_s16le", str(dst),
        ]
    else:
        raise ValueError(f"unsupported channel mode: {channel}")
    subprocess.run(cmd, check=True)


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        n = w.getnframes()
        raw = w.readframes(n)
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return audio, sr


# --------------------------------------------------------------- diarize ----
def run_sherpa_diarization(
    wav: Path,
    *,
    num_clusters: int | None,
    threshold: float | None,
    status: Status | None = None,
    progress_base: float = 0.12,
    progress_span: float = 0.76,
) -> list[SpkSeg]:
    import sherpa_onnx

    cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=str(SEG_MODEL)
            ),
            num_threads=DIAR_THREADS,
        ),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(EMB_MODEL), num_threads=DIAR_THREADS
        ),
        clustering=sherpa_onnx.FastClusteringConfig(
            num_clusters=-1 if num_clusters is None else num_clusters,
            threshold=DIAR_THRESHOLD if threshold is None else threshold,
        ),
        min_duration_on=0.3,
        min_duration_off=0.5,
    )
    if not cfg.validate():
        raise RuntimeError("diarization config invalid — check model paths")

    sd = sherpa_onnx.OfflineSpeakerDiarization(cfg)
    audio, sr = read_wav(wav)
    if sr != sd.sample_rate:
        raise RuntimeError(f"sample rate mismatch: {sr} vs {sd.sample_rate}")

    last_emit = [0.0]

    def cb(processed, total, _arg=None):
        if status is not None and time.time() - last_emit[0] > 1.5:
            last_emit[0] = time.time()
            frac = processed / max(total, 1)
            asr = "語音辨識已完成" if getattr(status, "asr_done", False) \
                else "語音辨識並行中"
            status.set("diarize", f"發言者切分 {frac*100:.0f}%．{asr}",
                       progress=progress_base + progress_span * frac)
        return 0

    res = sd.process(audio, callback=cb)
    return [SpkSeg(s.start, s.end, s.speaker) for s in res.sort_by_start_time()]


def evaluate_diarization(
    src: Path,
    mono_wav: Path,
    expected_speakers: int,
    outdir: Path,
    status: Status | None = None,
) -> dict:
    stereo = probe_audio_channels(src) > 1
    expected = expected_speakers if expected_speakers > 0 else None
    candidate_segments: dict[str, list[SpkSeg]] = {}
    candidates: list[DiarizationCandidate] = []

    baseline_segments = run_sherpa_diarization(
        mono_wav,
        num_clusters=None,
        threshold=DIAR_THRESHOLD,
        status=status,
        progress_base=0.12,
        progress_span=0.72,
    )
    baseline_stats = compute_stats(baseline_segments)
    baseline = DiarizationCandidate(
        candidate_id="mono-auto",
        source="sherpa-onnx",
        channel="mono",
        clustering="auto",
        requested_clusters=None,
        threshold=DIAR_THRESHOLD,
        stats=baseline_stats,
        score=score_stats(baseline_stats, expected),
        selected=False,
    )
    candidates.append(baseline)
    candidate_segments[baseline.candidate_id] = baseline_segments

    fallback_used = False
    prepared: dict[str, Path] = {"mono": mono_wav}

    if should_run_fallback(baseline_stats, expected, stereo and DIAR_STEREO_FALLBACK):
        fallback_used = True
        plans = build_fallback_plan(
            expected_speakers,
            stereo=stereo and DIAR_STEREO_FALLBACK,
            max_speakers=DIAR_MAX_SPEAKERS,
        )
        if status is not None:
            status.set(
                "diarize",
                f"基線分群 {baseline_stats.num_speakers} 群，與填寫人數不符；正在做穩健重跑校正…",
                progress=0.86,
            )
        for idx, plan in enumerate(plans, start=1):
            channel = plan["channel"]
            if channel not in prepared:
                prepared[channel] = outdir / f"audio_16k_{channel}.wav"
                prepare_diarization_wav(src, prepared[channel], channel)
            if status is not None:
                status.set(
                    "diarize",
                    f"穩健重跑 {idx}/{len(plans)}：{channel} / {plan['num_clusters']} 群",
                    progress=0.86 + 0.05 * idx / max(len(plans), 1),
                )
            segs = run_sherpa_diarization(
                prepared[channel],
                num_clusters=plan["num_clusters"],
                threshold=plan["threshold"],
                status=None,
            )
            stats = compute_stats(segs)
            cand = DiarizationCandidate(
                candidate_id=plan["candidate_id"],
                source="sherpa-onnx",
                channel=channel,
                clustering=plan["clustering"],
                requested_clusters=plan["num_clusters"],
                threshold=plan["threshold"],
                stats=stats,
                score=score_stats(stats, expected),
                selected=False,
            )
            candidates.append(cand)
            candidate_segments[cand.candidate_id] = segs

    best = choose_best_candidate(candidates, expected)
    selected_segments = candidate_segments[best.candidate_id]
    diagnostics = {
        "stereo_input": stereo,
        "fallback_used": fallback_used,
        "selected_candidate": best.to_dict(),
        "candidates": [
            {**c.to_dict(), "selected": c.candidate_id == best.candidate_id}
            for c in candidates
        ],
    }

    for channel, path in prepared.items():
        if channel == "mono":
            continue
        try:
            path.unlink()
        except OSError:
            pass

    return {"segments": selected_segments, "diagnostics": diagnostics}


# ------------------------------------------------------------------- asr ----
def transcribe(wav: Path, language: str | None, initial_prompt: str | None,
               status: Status | None = None) -> dict:
    import mlx_whisper

    if status:
        status.set("asr", "語音辨識中（Metal GPU）…", progress=0.45)

    return mlx_whisper.transcribe(
        str(wav),
        path_or_hf_repo=WHISPER_MODEL,
        language=language,
        initial_prompt=initial_prompt,
        # Needed to split a segment that spans a speaker change. Costs ~10%
        # wall time (ASR 32x -> 20x, but diarization at ~22x was already the
        # parallel bottleneck) and prevents attributing one person's sentence
        # to another — an unacceptable error in a client meeting note.
        word_timestamps=True,
        condition_on_previous_text=False,   # 避免長檔幻覺滾雪球
        temperature=(0.0, 0.2, 0.4),
        compression_ratio_threshold=2.4,
        logprob_threshold=-1.0,
        no_speech_threshold=0.6,
        hallucination_silence_threshold=2.0,
        verbose=None,
    )


# ----------------------------------------------------------------- merge ----
def assign_speaker(seg_start: float, seg_end: float,
                   spk: list[SpkSeg]) -> int | None:
    """Dominant-overlap speaker for an ASR segment."""
    best, best_ov = None, 0.0
    for s in spk:
        ov = min(seg_end, s.end) - max(seg_start, s.start)
        if ov > best_ov:
            best_ov, best = ov, s.speaker
    return best


def hhmmss(t: float) -> str:
    t = int(t)
    return f"{t//3600:02d}:{(t%3600)//60:02d}:{t%60:02d}"


MIN_RUN_SEC = 0.6   # ignore speaker flips shorter than this (clustering noise)


def split_segment_by_speaker(seg: dict, spk: list[SpkSeg]) -> list[dict]:
    """Split one ASR segment wherever the diarized speaker changes.

    Whisper happily emits a single segment spanning a speaker change
    ("...是不是太複雜了。確實太複雜。"), which dominant-overlap assignment then
    credits entirely to one person. Word timestamps let us cut at the seam.
    """
    words = seg.get("words") or []
    if not words:
        return [{"start": float(seg["start"]), "end": float(seg["end"]),
                 "speaker": assign_speaker(float(seg["start"]),
                                           float(seg["end"]), spk),
                 "text": (seg.get("text") or "").strip()}]

    runs: list[dict] = []
    for w in words:
        ws, we = float(w["start"]), float(w["end"])
        who = assign_speaker(ws, we, spk)
        if runs and runs[-1]["speaker"] == who:
            runs[-1]["end"] = we
            runs[-1]["text"] += w["word"]
        else:
            runs.append({"start": ws, "end": we, "speaker": who,
                         "text": w["word"]})

    # Absorb sub-MIN_RUN_SEC blips back into the neighbouring run so a single
    # mis-clustered syllable doesn't fracture the transcript.
    merged: list[dict] = []
    for r in runs:
        short = (r["end"] - r["start"]) < MIN_RUN_SEC
        if merged and (short or r["speaker"] == merged[-1]["speaker"]):
            merged[-1]["end"] = r["end"]
            merged[-1]["text"] += r["text"]
        else:
            merged.append(r)
    # A second pass joins runs the absorption step made adjacent-and-equal.
    out: list[dict] = []
    for r in merged:
        if out and out[-1]["speaker"] == r["speaker"]:
            out[-1]["end"] = r["end"]
            out[-1]["text"] += r["text"]
        else:
            out.append(r)
    return out


def merge(asr: dict, spk: list[SpkSeg], zhtw: bool = True) -> list[dict]:
    conv = None
    if zhtw:
        sys.path.insert(0, str(Path(__file__).parent))
        from zhtw import to_zhtw
        conv = to_zhtw
    out = []
    for s in asr.get("segments", []):
        for piece in split_segment_by_speaker(s, spk):
            text = (piece["text"] or "").strip()
            if not text:
                continue
            if conv:
                text = conv(text)
            out.append({
                "start": round(piece["start"], 2),
                "end": round(piece["end"], 2),
                "speaker": piece["speaker"],
                "text": text,
            })
    return out


def to_markdown(segments: list[dict], meta: dict) -> str:
    lines = [
        f"# 逐字稿：{meta.get('title') or '未命名會議'}",
        "",
        f"- 日期：{meta.get('date','')}",
        f"- 客戶／對象：{meta.get('client','')}",
        f"- 與會者（使用者提供）：{meta.get('participants','')}",
        f"- 音檔長度：{hhmmss(meta.get('duration', 0))}",
        f"- 辨識講者數：{meta.get('num_speakers','?')}",
        f"- 轉寫：mlx-whisper large-v3-turbo（本機 / 離線）",
        "",
        "> 講者標籤為聲紋自動分群結果（講者1／講者2…），尚未對應真實姓名。",
        "",
        "---",
        "",
    ]
    # Group consecutive same-speaker pieces into one readable turn instead of
    # one line per sub-second fragment.
    turns: list[dict] = []
    for s in segments:
        if turns and turns[-1]["speaker"] == s["speaker"]:
            turns[-1]["end"] = s["end"]
            turns[-1]["text"] += s["text"]
        else:
            turns.append(dict(s))

    for t in turns:
        name = f"講者{t['speaker'] + 1}" if t["speaker"] is not None else "未知講者"
        lines.append("")
        lines.append(f"**── {name} ──**　`[{hhmmss(t['start'])} – {hhmmss(t['end'])}]`")
        lines.append("")
        lines.append(t["text"].strip())
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ main ----
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("audio")
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--language", default="zh")
    ap.add_argument("--num-speakers", type=int, default=-1)
    ap.add_argument("--title", default="")
    ap.add_argument("--client", default="")
    ap.add_argument("--participants", default="")
    ap.add_argument("--date", default="")
    ap.add_argument("--initial-prompt", default=None)
    ap.add_argument("--skip-diarize", action="store_true")
    args = ap.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    st = Status(outdir / "status.json")
    t0 = time.time()

    try:
        src = Path(args.audio)
        st.set("normalize", f"正規化 {src.name}", progress=0.02)
        wav = outdir / "audio_16k.wav"
        duration = normalize(src, wav)
        st.set("normalize", f"音檔 {hhmmss(duration)}", progress=0.10)

        # Diarization (ONNX/CPU) and ASR (MLX/Metal GPU) use different
        # hardware, so run them concurrently — wall time ≈ max(), not sum().
        from concurrent.futures import ThreadPoolExecutor

        st.set("diarize", "發言者切分 + 語音辨識（並行）…", progress=0.12)
        # Diarization drives the visible progress bar (it exposes a callback);
        # ASR completion is folded into the message so the bar never rewinds.
        st.asr_done = False
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_spk = (pool.submit(evaluate_diarization, src, wav, args.num_speakers, outdir, st)
                     if not args.skip_diarize else None)
            f_asr = pool.submit(transcribe, wav, args.language,
                                args.initial_prompt, None)
            asr = f_asr.result()
            st.asr_done = True
            diar = f_spk.result() if f_spk else {"segments": [], "diagnostics": None}
            spk = diar["segments"]
            diarization_info = diar["diagnostics"]

        n_spk = len({s.speaker for s in spk}) if spk else 0

        st.set("merge", "合併時間軸 + 簡轉繁…", progress=0.92)
        segments = merge(asr, spk, zhtw=(args.language or "").startswith("zh"))

        meta = {
            "title": args.title, "client": args.client,
            "participants": args.participants, "date": args.date,
            "duration": duration, "num_speakers": n_spk,
            "expected_speakers": args.num_speakers if args.num_speakers > 0 else None,
            "source_file": src.name,
            "asr_language": asr.get("language"),
            "elapsed_sec": round(time.time() - t0, 1),
            "realtime_factor": round(duration / max(time.time() - t0, 1e-6), 1),
        }
        (outdir / "transcript.json").write_text(
            json.dumps({
                "meta": meta,
                "diarization": diarization_info,
                "segments": segments,
                "speaker_turns": [asdict(s) for s in spk],
            }, ensure_ascii=False, indent=2))
        (outdir / "transcript.md").write_text(to_markdown(segments, meta))
        (outdir / "transcript.txt").write_text(
            "\n".join(s["text"] for s in segments))

        try:
            wav.unlink()  # 16k wav 很大，逐字稿產出後即刪
        except OSError:
            pass

        st.finish({
            "outdir": str(outdir),
            "transcript_md": str(outdir / "transcript.md"),
            "transcript_json": str(outdir / "transcript.json"),
            "diarization": diarization_info,
            **meta,
        })
        print(json.dumps(meta, ensure_ascii=False, indent=2))
    except Exception as e:  # noqa: BLE001
        import traceback
        st.fail(f"{type(e).__name__}: {e}\n{traceback.format_exc()}")
        sys.exit(1)


if __name__ == "__main__":
    main()
