"""General-mode scan merge and answer application — the Claude path.

Private-mode scan already has tests (hydrate turn indices, no-draft contract).
The everyday path is the opposite: Claude returns a draft plus replacements,
the user clicks question cards, and Python — not the model — rewrites the
transcript. The bugs that actually shipped here were substitutions that ate
`講者11` when replacing `講者1`, phantom find/replace rows, and unanswered
cards being treated as confirmed names.
"""
from pathlib import Path
import json
import tempfile
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import chunker  # noqa: E402
import review  # noqa: E402

TRANSCRIPT = """**[00:00:10] 講者1**

這批報價我們抓十二萬。

**[00:01:00] 講者11**

那交期呢？三十天夠嗎？

**[00:02:00] 講者1**

就這樣定了。
"""


def parsed_chunk():
    turns = [t for t in chunker.parse_transcript(TRANSCRIPT) if not t.is_preamble]
    return chunker.chunk(turns, 10000, 0)[0]


def job_with_transcript(max_questions=8):
    d = Path(tempfile.mkdtemp())
    (d / "transcript.md").write_text(TRANSCRIPT)
    (d / "meta.json").write_text("{}")
    (d / "status.json").write_text('{"result": {}}')
    job = mock.Mock()
    job.dir = d
    job.acfg = {"max_questions": max_questions}
    return job


def real_job(files=None):
    d = Path(tempfile.mkdtemp())
    (d / "meta.json").write_text("{}")
    (d / "status.json").write_text('{"result": {"title": "報價會議"}}')
    (d / "transcript.md").write_text(TRANSCRIPT)
    for name, text in (files or {}).items():
        payload = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
        (d / name).write_text(payload)
    return review.Job(d, "claude", None, "claude", 60, {"max_questions": 8})


class MergeScansGeneralTests(unittest.TestCase):
    """Claude's scan: a draft is required, replacements must exist in the text."""

    def merge(self, data, *, expect_draft=True, max_questions=8, extra=None):
        job = job_with_transcript(max_questions)
        ch = parsed_chunk()
        rows = [(ch, data, "")]
        if extra:
            rows.extend(extra)
        return review.merge_scans(job, rows, expect_draft=expect_draft)

    def test_the_model_draft_is_what_the_user_sees_not_the_raw_turns(self):
        merged = self.merge({"draft": "清過的稿：十二萬已定。", "questions": []})
        self.assertIn("清過的稿：十二萬已定。", merged["draft"])
        self.assertNotIn("講者1", merged["draft"])
        self.assertEqual(merged["failed"], [])

    def test_a_missing_draft_on_the_claude_path_is_a_failed_chunk(self):
        merged = self.merge({"draft": "", "questions": []}, expect_draft=True)
        self.assertEqual(merged["failed"], [parsed_chunk().index])
        self.assertIn("就這樣定了。", merged["draft"])

    def test_a_dead_chunk_still_contributes_its_raw_turns(self):
        job = job_with_transcript()
        ch = parsed_chunk()
        merged = review.merge_scans(job, [(ch, None, "timeout")], expect_draft=True)
        self.assertEqual(merged["failed"], [ch.index])
        self.assertIn("十二萬", merged["draft"])

    def test_a_replacement_that_never_occurs_is_dropped(self):
        merged = self.merge({
            "draft": TRANSCRIPT,
            "replacements": [
                {"find": "十二萬", "replace": "12 萬", "note": "數字"},
                {"find": "简体全文", "replace": "繁體中文", "note": "整段意見"},
            ],
            "questions": [],
        })
        finds = [r["find"] for r in merged["replacements"]]
        self.assertEqual(finds, ["十二萬"])

    def test_the_same_question_across_chunks_is_one_card(self):
        ch = parsed_chunk()
        ch2 = mock.Mock()
        ch2.index = 1
        ch2.own_turns = ch.own_turns
        first = {"draft": "a", "questions": [
            {"type": "term", "key": "十二萬", "question": "十二萬含稅嗎？",
             "options": ["含稅"], "evidence": []}]}
        second = {"draft": "b", "questions": [
            {"type": "term", "key": "十二萬", "question": "重複",
             "options": ["未稅"], "evidence": []}]}
        job = job_with_transcript()
        merged = review.merge_scans(
            job, [(ch, first, ""), (ch2, second, "")], expect_draft=True)
        cards = [c for c in merged["questions_doc"]["cards"] if c["type"] == "term"]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["options"][:2], ["含稅", "未稅"])
        self.assertEqual(cards[0]["chunks"], [ch.index, 1])

    def test_a_speaker_seen_in_two_chunks_becomes_a_card_even_if_unasked(self):
        ch = parsed_chunk()
        ch2 = mock.Mock()
        ch2.index = 1
        ch2.own_turns = []
        speaker = {"label": "講者1", "guess": "王總", "confidence": "high", "why": "自稱"}
        job = job_with_transcript()
        merged = review.merge_scans(job, [
            (ch, {"draft": "x 講者1", "speakers": [speaker], "questions": []}, ""),
            (ch2, {"draft": "y 講者1", "speakers": [speaker], "questions": []}, ""),
        ], expect_draft=True)
        keys = [c["key"] for c in merged["questions_doc"]["cards"] if c["type"] == "speaker"]
        self.assertIn("講者1", keys)

    def test_speaker_cards_outrank_terms_when_the_budget_is_tight(self):
        questions = (
            [{"type": "term", "key": f"詞{i}", "question": f"詞{i}？", "options": []}
             for i in range(6)]
            + [{"type": "speaker", "key": "講者1", "question": "講者1是誰？",
                "options": ["王總"]}]
        )
        merged = self.merge({"draft": "稿", "questions": questions}, max_questions=3)
        types = [c["type"] for c in merged["questions_doc"]["cards"]]
        self.assertEqual(types[0], "speaker")
        self.assertEqual(len(types), 3)

    def test_every_card_ends_with_the_write_in_option(self):
        merged = self.merge({"draft": "稿", "questions": [
            {"type": "term", "key": "十二萬", "question": "金額？", "options": ["含稅"]}]})
        self.assertEqual(merged["questions_doc"]["cards"][0]["options"][-1],
                         review.OPT_OTHER)


class AnswerOfTests(unittest.TestCase):
    def card(self, **kw):
        row = {"id": "q01", "best_guess": "王總"}
        row.update(kw)
        return row

    def test_a_typed_custom_answer_beats_the_radio_choice(self):
        answers = {"cards": {"q01": {"choice": "李經理", "custom": "王總辦"}}}
        self.assertEqual(review.answer_of(self.card(), answers), ("王總辦", True))

    def test_a_real_choice_is_confirmed(self):
        answers = {"cards": {"q01": {"choice": "李經理", "custom": ""}}}
        self.assertEqual(review.answer_of(self.card(), answers), ("李經理", True))

    def test_write_in_without_text_falls_back_to_the_guess_unconfirmed(self):
        answers = {"cards": {"q01": {"choice": review.OPT_OTHER, "custom": ""}}}
        self.assertEqual(review.answer_of(self.card(), answers), ("王總", False))

    def test_an_unanswered_card_uses_best_guess_and_is_not_confirmed(self):
        self.assertEqual(review.answer_of(self.card(), {"cards": {}}), ("王總", False))


class ResolveAndApplyTests(unittest.TestCase):
    QUESTIONS = {
        "cards": [
            {"id": "q01", "type": "speaker", "key": "講者1",
             "question": "「講者1」是誰？", "best_guess": "王總"},
            {"id": "q02", "type": "speaker", "key": "講者11",
             "question": "「講者11」是誰？", "best_guess": "李經理"},
            {"id": "q03", "type": "term", "key": "十二萬",
             "question": "十二萬含稅嗎？", "best_guess": "十二萬含稅"},
        ],
        "replacements": [{"find": "定了", "replace": "拍板", "note": "scan"}],
    }

    def resolved(self, cards=None, extra_reps=None, skipped=False):
        job = real_job({
            "questions.json": self.QUESTIONS,
            "answers.json": {
                "skipped": skipped,
                "cards": cards or {},
                "replacements": extra_reps or [],
            },
        })
        return job, review.resolve(job)

    def test_confirmed_speakers_are_mapped_guessed_ones_are_not(self):
        _, res = self.resolved(cards={
            "q01": {"choice": "王總"},
            # q02 unanswered → guess
        })
        self.assertEqual(res["speaker_map"], {"講者1": "王總"})
        self.assertEqual(res["speaker_guess"], {"講者11": "李經理"})
        self.assertTrue(res["confirmed"])
        self.assertTrue(res["guessed"])

    def test_a_confirmed_term_becomes_a_replacement_an_unanswered_one_does_not(self):
        _, res = self.resolved(cards={"q03": {"choice": "十二萬含稅"}})
        pairs = [(r["find"], r["replace"]) for r in res["replacements"]]
        self.assertIn(("十二萬", "十二萬含稅"), pairs)
        _, skipped = self.resolved(cards={})
        term_pairs = [(r["find"], r["replace"]) for r in skipped["replacements"]
                      if r["find"] == "十二萬"]
        self.assertEqual(term_pairs, [])

    def test_user_specified_replacements_are_kept(self):
        _, res = self.resolved(extra_reps=[{"find": "三十天", "replace": "45 天"}])
        pairs = [(r["find"], r["replace"]) for r in res["replacements"]]
        self.assertIn(("三十天", "45 天"), pairs)

    def test_speaker_1_does_not_eat_speaker_11(self):
        text = "**講者1** 說了。然後 **講者11** 問交期。"
        rewritten = review.apply_substitutions(text, {
            "speaker_map": {"講者1": "王總", "講者11": "李經理"},
            "replacements": [],
        })
        self.assertIn("王總", rewritten)
        self.assertIn("李經理", rewritten)
        self.assertNotIn("講者1", rewritten)
        self.assertNotIn("王總1", rewritten)

    def test_a_digit_suffix_is_protected_even_without_the_longer_label(self):
        text = "講者1 與 講者11 都在。"
        rewritten = review.apply_substitutions(text, {
            "speaker_map": {"講者1": "王總"},
            "replacements": [],
        })
        self.assertIn("王總 與 講者11 都在。", rewritten)

    def test_clean_transcript_applies_the_map_and_records_what_was_guessed(self):
        job, res = self.resolved(cards={"q01": {"choice": "王總"}})
        (job.dir / "transcript_draft.md").write_text(
            "**[00:00:10] 講者1**\n\n十二萬。\n\n**[00:01:00] 講者11**\n\n交期。\n")
        path = review.write_clean_transcript(job, res)
        body = path.read_text()
        self.assertIn("**[00:00:10] 王總**", body)
        self.assertIn("講者對應（使用者確認）", body)
        self.assertIn("講者1 = 王總", body)
        self.assertIn("講者對應（系統推測，未經確認）", body)
        self.assertIn("講者11 ≈ 李經理", body)
        self.assertIn("## 修正對照表", body)

    def test_skipping_questions_is_flagged_in_the_write_prompt_not_the_note(self):
        _, res = self.resolved(skipped=True)
        block = review.confirmed_block(real_job(), res)
        self.assertIn("跳過問題", block)
        self.assertIn("agent_report.json", block)
        self.assertIn("不要在文件裡註明", block)


if __name__ == "__main__":
    unittest.main()
