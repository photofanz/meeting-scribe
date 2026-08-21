"""P3 — the gate that decides whether a private-pipeline note is shippable.

The acceptance test for this stage is blunt: inject a quotation nobody said
and see it caught. Everything else here exists so that the catch is not an
accident — traceability, number provenance, topic coverage, the skeleton and
the structured deliverable are each checked separately, so a failure names
what went wrong rather than just failing.
"""
from pathlib import Path
import json
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import chunker  # noqa: E402
import note_verify  # noqa: E402
import private_pipeline as pp  # noqa: E402

TRANSCRIPT = """# 逐字稿（定稿）：報價會議

**[00:00:10] 王總**

這批的報價我們抓十二萬，含稅。

**[00:01:10] 李經理**

那就這樣定了。

**[00:02:00] 王總**

交期我要回去問工廠。
"""


class Fixture:
    """A finished job on disk, exactly as private_pipeline.run() leaves one."""

    def __init__(self, tmp: Path):
        self.dir = tmp
        (tmp / "transcript_clean.md").write_text(TRANSCRIPT)
        self.turns = [t for t in chunker.parse_transcript(TRANSCRIPT) if not t.is_preamble]
        self.by_index = {t.index: t for t in self.turns}
        # Turn 0 is the transcript's front matter, which parse_transcript keeps
        # as a preamble turn; the first real utterance is turn 1.
        self.evidence = {
            "topics": [{"id": "T01", "label": "報價金額", "turns": [1, 2],
                        "status": "decided", "status_turn": 2, "speakers": ["王總"],
                        "points": [{"turn": 1, "speaker": "王總", "gist": "報價十二萬"}],
                        "numbers": [{"turn": 1, "stamp": "00:00:10",
                                     "literal": "十二萬", "means": "本批報價"}]},
                       {"id": "T02", "label": "交期確認", "turns": [3],
                        "status": "pending", "status_turn": None, "speakers": ["王總"],
                        "points": [], "numbers": []}],
            "actions": [{"turn": 3, "stamp": "00:02:00", "what": "問工廠交期",
                         "owner": "王總", "due": "未定"}],
            "numbers": [{"turn": 1, "stamp": "00:00:10", "literal": "十二萬",
                         "means": "本批報價"}],
            "unclear": [], "roster": ["王總", "李經理"], "dropped": {}, "turn_count": 3,
        }
        self.sections = [
            {"topic_id": "T01", "heading": "報價金額", "status": "decided",
             "basis": "王總拍板", "quote_turns": [2],
             "discussion": [{"turn": 1, "point": "報價抓十二萬含稅"}]},
            {"topic_id": "T02", "heading": "交期確認", "status": "pending",
             "basis": "尚未確認", "quote_turns": [3],
             "discussion": [{"turn": 3, "point": "要回去問工廠"}]},
        ]
        self.meta = {"title": "報價會議", "client": "某公司", "date": "2026-08-20",
                     "participants": "王總、李經理", "meeting_type": "general"}
        self.write()

    def write(self):
        (self.dir / "note_general.md").write_text(pp.render_document(
            "note_general", self.meta, self.evidence, self.sections,
            ["報價定為十二萬含稅。"], self.by_index))
        (self.dir / "action_items.json").write_text(json.dumps(
            pp.build_action_items(self.meta, {}, self.evidence, self.sections),
            ensure_ascii=False, indent=2))

    def note(self):
        return (self.dir / "note_general.md").read_text()

    def set_note(self, text):
        (self.dir / "note_general.md").write_text(text)

    def verify(self):
        return note_verify.verify(self.dir, self.evidence, self.turns, stems=["note_general"])


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.job = Fixture(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def kinds(self, findings):
        return sorted({f.kind for f in findings})

    def test_a_document_the_pipeline_produced_passes_clean(self):
        self.assertEqual(self.job.verify(), [])

    # -- the acceptance test ------------------------------------------------ #
    def test_an_injected_quotation_is_caught(self):
        self.job.set_note(self.job.note() + "\n> 王總：「我們決定明年收購對方公司。」\n")
        findings = [f for f in self.job.verify() if f.kind == "quote"]
        self.assertEqual(len(findings), 1)
        self.assertIn("我們決定明年收購對方公司。", findings[0].evidence)

    def test_a_quotation_edited_by_one_character_is_caught(self):
        """The failure this pipeline was built around: 。 quietly became .

        Nothing about the sentence is invented — only its punctuation — and it
        still has to fail, because a model that edits a quote is a model that
        will eventually rewrite one.
        """
        self.job.set_note(self.job.note().replace("那就這樣定了。」", "那就這樣定了.」"))
        findings = self.job.verify()
        self.assertEqual([f.kind for f in findings], ["quote"])

    def test_a_multi_line_turn_survives_the_quote_round_trip(self):
        """A turn can span lines; a blockquote cannot. Rendering joins them.

        So the round trip cut -> render -> verify has to hold, or every long
        utterance in a real transcript would be reported as fabricated.
        """
        multi = TRANSCRIPT.replace("交期我要回去問工廠。", "交期我要回去\n問工廠。")
        (self.job.dir / "transcript_clean.md").write_text(multi)
        turns = [t for t in chunker.parse_transcript(multi) if not t.is_preamble]
        by_index = {t.index: t for t in turns}
        rendered = pp.render_quote(pp.quote_for(by_index[3]))
        self.assertEqual(rendered.count("\n"), 0)
        # The whole note, so the only quotation checked is this one.
        self.job.set_note(rendered + "\n")
        findings = note_verify.verify(self.job.dir, self.job.evidence, turns,
                                      stems=["note_general"])
        self.assertEqual([f for f in findings if f.kind == "quote"], [])

    def test_a_number_not_present_in_its_own_turn_is_caught(self):
        self.job.evidence["numbers"][0]["literal"] = "二十萬"
        findings = self.job.verify()
        self.assertIn("number", self.kinds(findings))

    def test_a_number_attributed_to_the_wrong_turn_is_caught(self):
        self.job.evidence["numbers"][0]["turn"] = 3
        self.assertIn("number", self.kinds(self.job.verify()))

    def test_a_dropped_section_is_reported_with_the_topic_to_rewrite(self):
        text = self.job.note().split("### 2.")[0] + "\n## 三、決議事項\n"
        self.job.set_note(text + self.job.note().split("## 三、決議事項", 1)[1])
        findings = [f for f in self.job.verify() if f.kind == "topic_count"]
        self.assertTrue(findings)
        self.assertEqual([f.topic_id for f in findings], ["T02"])
        self.assertTrue(findings[0].rerun)

    def test_a_sharpened_heading_does_not_count_as_a_missing_section(self):
        """S4 may improve the label it was handed; that is not a gap."""
        self.job.sections[1]["heading"] = "交期確認（待工廠回覆）"
        self.job.write()
        self.assertEqual([f for f in self.job.verify() if f.kind == "topic_count"], [])

    def test_a_missing_chapter_is_reported(self):
        self.job.set_note(self.job.note().replace("## 四、待辦事項", "## 四、其他"))
        findings = [f for f in self.job.verify() if f.kind == "skeleton"]
        self.assertTrue(any("待辦事項" in f.detail for f in findings))

    def test_simplified_characters_are_flagged_as_fixable(self):
        self.job.set_note(self.job.note().replace("報價金額", "报价金额"))
        findings = [f for f in self.job.verify() if f.kind == "simplified"]
        self.assertEqual([f.severity for f in findings], ["warn"])

    def test_a_broken_action_items_file_is_reported(self):
        (self.job.dir / "action_items.json").write_text('{"decisions": "not a list"}')
        findings = [f for f in self.job.verify() if f.kind == "schema"]
        self.assertTrue(findings)

    def test_a_missing_note_is_reported_rather_than_skipped(self):
        (self.job.dir / "note_general.md").unlink()
        self.assertIn("skeleton", self.kinds(self.job.verify()))


class AutofixTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.job = Fixture(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_an_untraceable_quote_is_removed_not_rewritten(self):
        self.job.set_note(self.job.note() + "\n> 王總：「我們決定明年收購對方公司。」\n")
        counts = note_verify.autofix(self.job.dir, ["note_general"])
        self.assertEqual(counts["quotes_removed"], 1)
        self.assertNotIn("收購對方公司", self.job.note())
        self.assertEqual(self.job.verify(), [])

    def test_real_quotes_survive_the_fix(self):
        before = len(note_verify.quote_lines(self.job.note()))
        note_verify.autofix(self.job.dir, ["note_general"])
        self.assertEqual(len(note_verify.quote_lines(self.job.note())), before)

    def test_simplified_text_is_converted_in_place(self):
        self.job.set_note(self.job.note().replace("報價金額", "报价金额"))
        counts = note_verify.autofix(self.job.dir, ["note_general"])
        self.assertEqual(counts["simplified_converted"], 1)
        self.assertIn("報價金額", self.job.note())
        self.assertNotIn("报价金额", self.job.note())


class LoadEvidenceTests(unittest.TestCase):
    def test_job_root_copy_wins_over_scratch(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / ".review").mkdir()
        (tmp / ".review" / "evidence.json").write_text('{"where": "scratch"}')
        (tmp / "evidence.json").write_text('{"where": "root"}')
        self.assertEqual(note_verify.load_evidence(tmp)["where"], "root")

    def test_scratch_is_used_when_the_durable_copy_is_missing(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / ".review").mkdir()
        (tmp / ".review" / "evidence.json").write_text('{"where": "scratch"}')
        self.assertEqual(note_verify.load_evidence(tmp)["where"], "scratch")

    def test_missing_everywhere_returns_none(self):
        tmp = Path(tempfile.mkdtemp())
        self.assertIsNone(note_verify.load_evidence(tmp))


if __name__ == "__main__":
    unittest.main()
