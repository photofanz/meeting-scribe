#!/usr/bin/env python
from __future__ import annotations

from dataclasses import dataclass, asdict
from statistics import median


@dataclass(frozen=True)
class SpkSeg:
    start: float
    end: float
    speaker: int


@dataclass(frozen=True)
class DiarizationStats:
    duration_sec: float
    num_speakers: int
    num_segments: int
    speaker_switches: int
    short_segments_le_0_6: int
    short_segments_le_1_0: int
    singleton_speakers: int
    median_segment_sec: float
    segments_per_min: float
    switches_per_min: float
    short_ratio: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class DiarizationCandidate:
    candidate_id: str
    source: str
    channel: str
    clustering: str
    requested_clusters: int | None
    threshold: float | None
    stats: DiarizationStats
    score: float
    selected: bool = False

    def to_dict(self) -> dict:
        data = asdict(self)
        data["stats"] = self.stats.to_dict()
        return data


def diarization_is_absurd(got: int | None, expected: int | None) -> bool:
    if got is None or expected is None or expected <= 0:
        return False
    return got > max(expected * 2, expected + 3)


def compute_stats(segments: list[SpkSeg]) -> DiarizationStats:
    if not segments:
        return DiarizationStats(
            duration_sec=0.0,
            num_speakers=0,
            num_segments=0,
            speaker_switches=0,
            short_segments_le_0_6=0,
            short_segments_le_1_0=0,
            singleton_speakers=0,
            median_segment_sec=0.0,
            segments_per_min=0.0,
            switches_per_min=0.0,
            short_ratio=0.0,
        )

    durs = [max(0.0, s.end - s.start) for s in segments]
    counts: dict[int, int] = {}
    for s in segments:
        counts[s.speaker] = counts.get(s.speaker, 0) + 1
    switches = sum(1 for a, b in zip(segments, segments[1:]) if a.speaker != b.speaker)
    duration_sec = max(s.end for s in segments) - min(s.start for s in segments)
    duration_min = max(duration_sec / 60.0, 1e-6)
    short_06 = sum(1 for d in durs if d <= 0.6)
    short_10 = sum(1 for d in durs if d <= 1.0)
    return DiarizationStats(
        duration_sec=round(duration_sec, 3),
        num_speakers=len(counts),
        num_segments=len(segments),
        speaker_switches=switches,
        short_segments_le_0_6=short_06,
        short_segments_le_1_0=short_10,
        singleton_speakers=sum(1 for c in counts.values() if c == 1),
        median_segment_sec=round(float(median(durs)), 3),
        segments_per_min=round(len(segments) / duration_min, 3),
        switches_per_min=round(switches / duration_min, 3),
        short_ratio=round(short_06 / max(len(segments), 1), 3),
    )


def score_stats(stats: DiarizationStats, expected_speakers: int | None = None) -> float:
    score = 0.0
    if expected_speakers and expected_speakers > 0:
        delta = abs(stats.num_speakers - expected_speakers)
        # Over-splitting is much more damaging than under-merging in this app.
        if stats.num_speakers >= expected_speakers:
            score += delta * 300
        else:
            score += delta * 180
        if diarization_is_absurd(stats.num_speakers, expected_speakers):
            score += 5000

    score += stats.singleton_speakers * 90
    score += stats.segments_per_min * 6
    score += stats.switches_per_min * 5
    score += stats.short_ratio * 120
    score -= min(stats.median_segment_sec, 8.0) * 3
    return round(score, 3)


def build_fallback_plan(expected_speakers: int, stereo: bool, max_speakers: int = 8) -> list[dict]:
    if expected_speakers <= 0:
        return []

    plans: list[dict] = []
    seen: set[tuple[str, int]] = set()

    def add(channel: str, clusters: int):
        clusters = max(1, min(max_speakers, clusters))
        key = (channel, clusters)
        if key in seen:
            return
        seen.add(key)
        plans.append({
            "channel": channel,
            "num_clusters": clusters,
            "threshold": None,
            "clustering": "fixed",
            "candidate_id": f"{channel}-k{clusters}",
        })

    add("mono", expected_speakers)
    add("mono", expected_speakers + 1)
    if stereo:
        add("left", expected_speakers)
        add("right", expected_speakers)
    return plans


def should_run_fallback(
    baseline: DiarizationStats,
    expected_speakers: int | None,
    stereo: bool,
) -> bool:
    if not expected_speakers or expected_speakers <= 0:
        return False
    if baseline.num_speakers == expected_speakers:
        return False
    if diarization_is_absurd(baseline.num_speakers, expected_speakers):
        return True
    # Even when not fully absurd, a mismatch on stereo meeting audio is worth a
    # deterministic second opinion because user-provided headcount is cheap
    # signal and the sherpa auto-counter is what fails in the wild jobs.
    return stereo or abs(baseline.num_speakers - expected_speakers) >= 2


def choose_best_candidate(
    candidates: list[DiarizationCandidate], expected_speakers: int | None,
) -> DiarizationCandidate:
    if not candidates:
        raise ValueError("at least one diarization candidate is required")

    def key(c: DiarizationCandidate):
        exact = 0
        if expected_speakers and expected_speakers > 0:
            exact = 0 if c.stats.num_speakers == expected_speakers else 1
        return (c.score, exact, c.stats.singleton_speakers, c.stats.switches_per_min)

    best = min(candidates, key=key)
    return DiarizationCandidate(
        candidate_id=best.candidate_id,
        source=best.source,
        channel=best.channel,
        clustering=best.clustering,
        requested_clusters=best.requested_clusters,
        threshold=best.threshold,
        stats=best.stats,
        score=best.score,
        selected=True,
    )
