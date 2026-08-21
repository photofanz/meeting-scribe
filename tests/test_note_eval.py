"""P4 — the eval harness, which is what stops the next tuning round being a guess.

The private path was already tuned once on an impression ("the note looks
short"). That is how a 0.061 output ratio reached a client folder. These tests
pin the parts of the measurement that could quietly stop measuring: quotation
detection, the source fallback for older transcript formats, exclusion of
rewrite backups from live scores, and that a pre_evidence snapshot cannot
outrank the note that replaced it.
"""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import note_eval  # noqa: E402

TRANSCRIPT = """# 逐字稿（定稿）：報價會議

**[00:00:10] 王總**

這批的報價我們抓十二萬，含稅，這個價格我們可以接受。

**[00:01:10] 李經理**

那就這樣定了，我下週安排合約。
"""

GOOD_NOTE = """# 報價會議 會議記錄

## 一、本次會議重點

- 報價定為十二萬含稅。

## 二、議題討論

### 1. 報價金額

- **討論內容**：
  - 王總：報價抓十二萬含稅
- **結論**：已決議——王總拍板

> 王總：「這批的報價我們抓十二萬，含稅，這個價格我們可以接受。」

## 三、決議事項

| # | 決議 | 依據 |
|---|---|---|
| 1 | 報價金額 | 王總拍板 |

## 四、待辦事項

| # | 事項 | 負責人 |
|---|---|---|
| 1 | 安排合約 | 李經理 |

## 五、未解決事項與待確認

- 無

## 六、下次會議

- 未討論
"""


class QuotationTests(unittest.TestCase):
    def test_both_blockquotes_and_inline_quotes_are_counted(self):
        note = ('> 王總：「這批的報價我們抓十二萬。」\n'
                '討論中李經理說「那就這樣定了，我下週安排合約」。\n')
        found = note_eval.quotations(note)
        self.assertEqual(len(found), 2)
        self.assertIn("這批的報價我們抓十二萬。", found)

    def test_a_short_bracketed_term_is_not_a_quotation(self):
        """「已決議」 is how Chinese writes a defined term, not someone's words."""
        self.assertEqual(note_eval.quotations("結論為「已決議」，負責人「未指派」。"), [])

    def test_speaker_and_timestamp_prefixes_are_stripped_before_matching(self):
        found = note_eval.quotations("> [00:01:10] 李經理：那就這樣定了，我下週安排合約。\n")
        self.assertEqual(found, ["那就這樣定了，我下週安排合約。"])


class MetricsTests(unittest.TestCase):
    def job(self, note=GOOD_NOTE, transcript=TRANSCRIPT):
        tmp = tempfile.mkdtemp()
        path = Path(tmp)
        (path / "transcript_clean.md").write_text(transcript)
        (path / "note_general.md").write_text(note)
        return note_eval.metrics(path)

    def test_a_grounded_note_scores_its_quotes_as_traceable(self):
        m = self.job()
        self.assertEqual(m["quote_rate"], 1.0)
        self.assertEqual(m["source_parsed"], "turns")
        self.assertEqual(m["sections_present"], "6/6")

    def test_a_fabricated_quote_shows_up_as_a_lower_grounding_score(self):
        bad = GOOD_NOTE.replace("這個價格我們可以接受。", "我們決定收購對方公司。")
        m = self.job(note=bad)
        self.assertEqual(m["quote_rate"], 0.0)
        self.assertTrue(m["untraceable_examples"])
        self.assertLess(m["score"], self.job()["score"])

    def test_a_two_line_note_cannot_outscore_a_real_one(self):
        stub = "# 報價會議 會議記錄\n\n## 一、本次會議重點\n\n- 討論了報價。\n"
        self.assertLess(self.job(note=stub)["score"], self.job()["score"])

    def test_front_matter_is_not_counted_as_source_material(self):
        m = self.job()
        # Only the two utterances, not the title block above them.
        self.assertLess(m["source_chars"], len(TRANSCRIPT))
        self.assertGreater(m["source_chars"], 40)

    def test_an_older_transcript_format_falls_back_to_the_raw_file(self):
        """July's jobs use `**高老師**：…` with no timestamp — still scorable."""
        old = "# 清稿逐字稿\n\n**高老師**：那我們就開始。\n\n**Jerry**：好，我講一下實驗。\n"
        m = self.job(transcript=old)
        self.assertEqual(m["source_parsed"], "raw")
        self.assertGreater(m["source_chars"], 0)

    def test_simplified_characters_and_stray_timestamps_cost_cleanliness(self):
        dirty = GOOD_NOTE.replace("報價金額", "报价金额") + "\n- 於 [00:05:00] 補充\n"
        m = self.job(note=dirty)
        self.assertGreater(m["simplified_chars"], 0)
        self.assertEqual(m["timestamps"], 1)
        self.assertLess(m["scores"]["cleanliness"], 1.0)

    def test_a_job_with_evidence_json_is_tagged_as_the_evidence_pipeline(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "transcript_clean.md").write_text(TRANSCRIPT)
        (tmp / "note_general.md").write_text(GOOD_NOTE)
        self.assertEqual(note_eval.metrics(tmp)["pipeline"], "")
        (tmp / "evidence.json").write_text("{}")
        self.assertEqual(note_eval.metrics(tmp)["pipeline"], "evidence")

    def test_clearing_the_ratio_bar_is_not_a_perfect_coverage_score(self):
        at_bar = note_eval.scale_past_bar(0.30, 0.30, 0.70)
        richer = note_eval.scale_past_bar(0.60, 0.30, 0.70)
        padded = note_eval.scale_past_bar(1.80, 0.30, 0.70)
        self.assertAlmostEqual(at_bar, 0.70)
        self.assertGreater(richer, at_bar)
        self.assertAlmostEqual(padded, 1.0)
        self.assertGreater(padded, richer)

    def test_two_passing_notes_still_rank_by_how_dense_they_are(self):
        """A 0.30-ratio stub must not tie a 0.60-ratio note that is otherwise equal."""
        spoken = "這批的報價我們抓十二萬，含稅，這個價格我們可以接受。那就這樣定了，我下週安排合約。"
        spoken = spoken * 40  # ~2.4k chars so ratios stay in the interesting band
        transcript = (
            f"# 逐字稿\n\n**[00:00:10] 王總**\n\n{spoken[:len(spoken)//2]}\n\n"
            f"**[00:01:10] 李經理**\n\n{spoken[len(spoken)//2:]}\n"
        )
        thin = GOOD_NOTE
        rich = GOOD_NOTE + "\n\n".join(
            f"### {i}. 補充\n\n> 王總：「這批的報價我們抓十二萬，含稅，這個價格我們可以接受。」\n"
            for i in range(8)
        )
        self.assertLess(self.job(note=thin, transcript=transcript)["score"],
                        self.job(note=rich, transcript=transcript)["score"])
        self.assertLess(self.job(note=thin, transcript=transcript)["scores"]["coverage"],
                        self.job(note=rich, transcript=transcript)["scores"]["coverage"])


class ScalePastBarTests(unittest.TestCase):
    def test_zero_stays_zero(self):
        self.assertEqual(note_eval.scale_past_bar(0, 0.30, 0.70), 0.0)

    def test_below_the_bar_is_a_linear_ramp_to_at_bar(self):
        self.assertAlmostEqual(note_eval.scale_past_bar(0.15, 0.30, 0.70), 0.35)


class SeparationTests(unittest.TestCase):
    def rows(self, *scores):
        return [{"job": job, "score": score} for job, score in scores]

    def test_the_gap_below_the_known_disaster_is_reported(self):
        rows = self.rows(("good_a", 0.9), ("good_b", 0.7), ("legacy_stub", 0.5))
        gap = note_eval.separation(rows, "legacy_stub")
        self.assertTrue(gap["is_last"])
        self.assertEqual(gap["rank"], 3)
        self.assertAlmostEqual(gap["gap"], 0.2)

    def test_a_disaster_that_did_not_rank_last_is_reported_as_such(self):
        rows = self.rows(("legacy_stub", 0.5), ("worse", 0.2))
        gap = note_eval.separation(rows, "legacy_stub")
        self.assertFalse(gap["is_last"])
        self.assertLess(gap["gap"], 0)


class BackupNoteTests(unittest.TestCase):
    def job_dir(self, note=GOOD_NOTE, backup=None, extra=None):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "transcript_clean.md").write_text(TRANSCRIPT)
        (tmp / "note_general.md").write_text(note)
        if backup is not None:
            (tmp / "note_general.pre_evidence.md").write_text(backup)
        if extra is not None:
            (tmp / extra[0]).write_text(extra[1])
        return tmp

    def test_live_metrics_do_not_fold_in_the_pre_evidence_backup(self):
        stub = "# 舊稿\n\n討論了報價。\n"
        path = self.job_dir(note=GOOD_NOTE, backup=stub)
        m = note_eval.metrics(path)
        self.assertEqual(m["note_files"], ["note_general.md"])
        self.assertEqual(m["note_chars"], len(GOOD_NOTE))

    def test_evidence_lift_requires_the_rewrite_to_beat_its_own_backup(self):
        stub = "# 會議記錄\n\n## 一、本次會議重點\n\n- 短。\n"
        path = self.job_dir(note=GOOD_NOTE, backup=stub)
        lifts = note_eval.evidence_lifts([path])
        self.assertEqual(len(lifts), 1)
        self.assertTrue(lifts[0]["ok"])
        self.assertGreater(lifts[0]["after_score"], lifts[0]["before_score"])
        self.assertEqual(lifts[0]["before_files"], ["note_general.pre_evidence.md"])
        self.assertEqual(lifts[0]["after_files"], ["note_general.md"])

    def test_a_rewrite_that_got_worse_fails_the_lift(self):
        stub = "# 會議記錄\n\n## 一、本次會議重點\n\n- 短。\n"
        path = self.job_dir(note=stub, backup=GOOD_NOTE)
        lifts = note_eval.evidence_lifts([path])
        self.assertFalse(lifts[0]["ok"])
        self.assertLess(lifts[0]["delta"], 0)

    def test_jobs_without_a_backup_are_not_a_lift_row(self):
        self.assertEqual(note_eval.evidence_lifts([self.job_dir()]), [])


class ArchiveRegressionTests(unittest.TestCase):
    """If the deploy archive has pre_evidence snapshots, each rewrite must lift."""

    @classmethod
    def setUpClass(cls):
        archive = ROOT / "archive"
        cls.dirs = [d for d in sorted(archive.iterdir()) if d.is_dir()] if archive.is_dir() else []
        cls.rows = note_eval.evaluate(cls.dirs)
        cls.lifts = note_eval.evidence_lifts(cls.dirs)

    def test_every_pre_evidence_backup_is_beaten_by_the_current_note(self):
        if not self.dirs:
            self.skipTest("no local archive")
        if not self.lifts:
            self.skipTest("archive has no note_*.pre_evidence.md snapshots")
        failed = [lift for lift in self.lifts if not lift["ok"]]
        self.assertFalse(failed, failed)

    def test_every_archived_job_is_scored_rather_than_skipped(self):
        if not self.dirs:
            self.skipTest("no local archive")
        self.assertEqual(len(self.rows), len(self.dirs))
        self.assertTrue(all(r["source_chars"] > 0 for r in self.rows),
                        [r["job"] for r in self.rows if not r["source_chars"]])


if __name__ == "__main__":
    unittest.main()
