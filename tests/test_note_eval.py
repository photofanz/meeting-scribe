"""P4 — the eval harness, which is what stops the next tuning round being a guess.

The private path was already tuned once on an impression ("the note looks
short"). That is how a 0.061 output ratio reached a client folder. These tests
pin the parts of the measurement that could quietly stop measuring: quotation
detection, the source fallback for older transcript formats, and the
separation between a good note and a bad one.
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


class SeparationTests(unittest.TestCase):
    def rows(self, *scores):
        return [{"job": job, "score": score} for job, score in scores]

    def test_the_gap_below_the_known_disaster_is_reported(self):
        rows = self.rows(("good_a", 0.9), ("good_b", 0.7), ("群耀_94db8a", 0.5))
        gap = note_eval.separation(rows, "群耀_94db8a")
        self.assertTrue(gap["is_last"])
        self.assertEqual(gap["rank"], 3)
        self.assertAlmostEqual(gap["gap"], 0.2)

    def test_a_disaster_that_did_not_rank_last_is_reported_as_such(self):
        rows = self.rows(("群耀_94db8a", 0.5), ("worse", 0.2))
        gap = note_eval.separation(rows, "群耀_94db8a")
        self.assertFalse(gap["is_last"])
        self.assertLess(gap["gap"], 0)


class ArchiveRegressionTests(unittest.TestCase):
    """The calibration claim, checked against the archive it was calibrated on."""

    @classmethod
    def setUpClass(cls):
        dirs = [d for d in sorted((ROOT / "archive").iterdir()) if d.is_dir()]
        cls.rows = note_eval.evaluate(dirs)

    def test_the_disaster_run_ranks_last_of_every_archived_job(self):
        gap = note_eval.separation(self.rows, "群耀_94db8a")
        self.assertTrue(gap["is_last"], f"排名 {gap['rank']}/{gap['of']}")
        self.assertGreater(gap["gap"], 0.1, "與次低的分數必須拉得開，不能只是剛好墊底")

    def test_every_archived_job_is_scored_rather_than_skipped(self):
        self.assertEqual(len(self.rows), 15)
        self.assertTrue(all(r["source_chars"] > 0 for r in self.rows),
                        [r["job"] for r in self.rows if not r["source_chars"]])


if __name__ == "__main__":
    unittest.main()
