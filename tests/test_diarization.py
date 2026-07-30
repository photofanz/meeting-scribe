from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from diarization import (  # noqa: E402
    DiarizationCandidate,
    DiarizationStats,
    SpkSeg,
    build_fallback_plan,
    choose_best_candidate,
    compute_stats,
    diarization_is_absurd,
    score_stats,
    should_run_fallback,
)


def mk_stats(
    *,
    num_speakers: int,
    num_segments: int,
    speaker_switches: int,
    singleton_speakers: int = 0,
    segments_per_min: float = 8.0,
    switches_per_min: float = 6.0,
    short_ratio: float = 0.2,
    median_segment_sec: float = 2.0,
) -> DiarizationStats:
    return DiarizationStats(
        duration_sec=600.0,
        num_speakers=num_speakers,
        num_segments=num_segments,
        speaker_switches=speaker_switches,
        short_segments_le_0_6=int(num_segments * short_ratio),
        short_segments_le_1_0=int(num_segments * max(short_ratio, 0.25)),
        singleton_speakers=singleton_speakers,
        median_segment_sec=median_segment_sec,
        segments_per_min=segments_per_min,
        switches_per_min=switches_per_min,
        short_ratio=short_ratio,
    )


def mk_candidate(candidate_id: str, s: DiarizationStats, expected: int | None) -> DiarizationCandidate:
    return DiarizationCandidate(
        candidate_id=candidate_id,
        source="sherpa-onnx",
        channel="mono",
        clustering="auto",
        requested_clusters=None,
        threshold=0.75,
        stats=s,
        score=score_stats(s, expected),
        selected=False,
    )


class DiarizationTests(unittest.TestCase):
    def test_diarization_is_absurd(self):
        self.assertTrue(diarization_is_absurd(112, 2))
        self.assertTrue(diarization_is_absurd(9, 4))
        self.assertFalse(diarization_is_absurd(4, 4))
        self.assertFalse(diarization_is_absurd(None, 4))

    def test_build_fallback_plan_stereo_includes_left_right_and_neighbor_counts(self):
        plans = build_fallback_plan(4, stereo=True, max_speakers=8)
        got = {(p["channel"], p["num_clusters"]) for p in plans}
        self.assertIn(("mono", 4), got)
        self.assertIn(("mono", 5), got)
        self.assertIn(("left", 4), got)
        self.assertIn(("right", 4), got)

    def test_should_run_fallback_when_baseline_mismatches_expected(self):
        baseline = mk_stats(num_speakers=6, num_segments=120, speaker_switches=80)
        self.assertTrue(should_run_fallback(baseline, 4, True))
        self.assertTrue(should_run_fallback(baseline, 4, False))
        exact = mk_stats(num_speakers=4, num_segments=90, speaker_switches=50)
        self.assertFalse(should_run_fallback(exact, 4, True))

    def test_choose_best_candidate_prefers_expected_headcount_over_fragmented_auto(self):
        auto = mk_candidate("mono-auto", mk_stats(
            num_speakers=25, num_segments=308, speaker_switches=224,
            singleton_speakers=4, segments_per_min=23.0,
            switches_per_min=17.0, short_ratio=0.22,
            median_segment_sec=1.4,
        ), 2)
        fixed = mk_candidate("mono-k2", mk_stats(
            num_speakers=2, num_segments=289, speaker_switches=190,
            segments_per_min=21.8, switches_per_min=14.3,
            short_ratio=0.19, median_segment_sec=1.5,
        ), 2)
        chosen = choose_best_candidate([auto, fixed], 2)
        self.assertEqual(chosen.candidate_id, "mono-k2")
        self.assertTrue(chosen.selected)

    def test_choose_best_candidate_breaks_tie_with_lower_fragmentation(self):
        a = mk_candidate("mono-k2", mk_stats(
            num_speakers=2, num_segments=289, speaker_switches=190,
            segments_per_min=21.8, switches_per_min=14.3,
            short_ratio=0.19, median_segment_sec=1.5,
        ), 2)
        b = mk_candidate("left-k2", mk_stats(
            num_speakers=2, num_segments=289, speaker_switches=180,
            segments_per_min=21.8, switches_per_min=13.6,
            short_ratio=0.17, median_segment_sec=1.57,
        ), 2)
        chosen = choose_best_candidate([a, b], 2)
        self.assertEqual(chosen.candidate_id, "left-k2")

    def test_compute_stats_counts_switches_and_singletons(self):
        segs = [
            SpkSeg(0.0, 1.0, 0),
            SpkSeg(1.0, 2.0, 1),
            SpkSeg(2.0, 3.0, 0),
            SpkSeg(3.0, 4.0, 2),
        ]
        s = compute_stats(segs)
        self.assertEqual(s.num_speakers, 3)
        self.assertEqual(s.num_segments, 4)
        self.assertEqual(s.speaker_switches, 3)
        self.assertEqual(s.singleton_speakers, 2)


if __name__ == "__main__":
    unittest.main()
