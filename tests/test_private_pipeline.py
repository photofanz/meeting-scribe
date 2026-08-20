"""P2 — the parts of the private pipeline that are pure Python.

S3 (merge) and S5 (assembly) take no model calls at all, which is the point:
coverage, ordering, attribution, quotation and structure stop being things a
27B model has to remember and become things this code does the same way every
time. So they are tested the same way — no mocks of a language model, just
data in and documents out.
"""
from pathlib import Path
import json
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import chunker  # noqa: E402
import private_pipeline as pp  # noqa: E402

TRANSCRIPT = """**[00:00:10] 王總**

這批的報價我們抓十二萬，含稅。

**[00:00:40] 李經理**

十二萬會不會太高？對方上次講的是十萬。

**[00:01:10] 王總**

那就這樣定了。

**[00:02:00] 李經理**

交期呢？三十天夠嗎？

**[00:02:30] 王總**

交期我要回去問工廠，下週再說。
"""


def turns_of(text=TRANSCRIPT):
    return [t for t in chunker.parse_transcript(text) if not t.is_preamble]


def chunk_of(turns, indices):
    ch = chunker.Chunk(index=0, turns=[t for t in turns if t.index in indices])
    return ch


EVIDENCE_REPLY = {
    "topics": [
        {"topic_id": "t1", "label": "報價金額", "turns": [0, 1, 2],
         "status": "decided", "status_turn": 2, "speakers": ["王總", "李經理"]},
        {"topic_id": "t2", "label": "交期確認", "turns": [3, 4],
         "status": "pending", "status_turn": None, "speakers": ["王總"]},
    ],
    "points": [
        {"turn": 0, "speaker": "王總", "gist": "報價抓十二萬含稅", "topic_id": "t1"},
        {"turn": 1, "speaker": "李經理", "gist": "認為偏高，對方上次講十萬", "topic_id": "t1"},
        {"turn": 2, "speaker": "王總", "gist": "拍板照十二萬", "topic_id": "t1"},
        {"turn": 4, "speaker": "王總", "gist": "交期要回去問工廠", "topic_id": "t2"},
    ],
    "numbers": [
        {"turn": 0, "literal": "十二萬", "means": "本批報價"},
        {"turn": 1, "literal": "十萬", "means": "對方上次的價格"},
    ],
    "actions": [
        {"turn": 4, "what": "向工廠確認交期", "owner": "王總", "due": "下週"},
    ],
    "unclear": [],
}


class LabelSimilarityTests(unittest.TestCase):
    def test_a_topic_that_continued_across_the_boundary_is_recognised(self):
        self.assertGreaterEqual(
            pp.label_similarity("付款條件", "付款條件討論"), pp.LABEL_SIMILARITY)

    def test_two_different_topics_stay_apart(self):
        self.assertLess(pp.label_similarity("報價金額", "交期安排"), pp.LABEL_SIMILARITY)

    def test_punctuation_and_case_do_not_decide_identity(self):
        self.assertEqual(pp.label_similarity("報價、付款", "報價付款"), 1.0)


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.turns = turns_of()
        self.parts = [(chunk_of(self.turns, {0, 1, 2, 3, 4}), EVIDENCE_REPLY)]

    def merge(self, parts=None, roster=("王總", "李經理")):
        return pp.merge_evidence(parts or self.parts, self.turns, roster=list(roster))

    def test_topics_come_out_in_meeting_order_with_stable_ids(self):
        ev = self.merge()
        self.assertEqual([t["id"] for t in ev["topics"]], ["T01", "T02"])
        self.assertEqual([t["label"] for t in ev["topics"]], ["報價金額", "交期確認"])

    def test_a_turn_the_chunk_did_not_own_is_dropped_not_reconciled(self):
        """The overlap is context. Counting it twice would duplicate a topic."""
        own = chunk_of(self.turns, {3, 4})
        own.overlap_count = 0
        ev = pp.merge_evidence([(own, EVIDENCE_REPLY)], self.turns, roster=["王總"])
        self.assertEqual([t["label"] for t in ev["topics"]], ["交期確認"])
        self.assertEqual(ev["dropped"]["topic_no_valid_turns"], 1)
        self.assertEqual([n["literal"] for n in ev["numbers"]], [])

    def test_overlap_turns_are_excluded_by_own_turns_not_by_guesswork(self):
        second = chunker.Chunk(index=1, turns=self.turns[2:], overlap_count=2)
        ev = pp.merge_evidence([(second, EVIDENCE_REPLY)], self.turns, roster=["王總"])
        # topic t1 covers 0,1,2 but this chunk only owns 4 onwards.
        self.assertEqual([t["turns"] for t in ev["topics"]], [[4]])

    def test_quotes_are_cut_from_the_transcript_and_are_byte_exact(self):
        ev = self.merge()
        by_index = {t.index: t for t in self.turns}
        quote = pp.quote_for(by_index[2])
        self.assertEqual(quote["text"], "那就這樣定了。")
        # The full-width period survives, which is the entire reason the model
        # is never allowed to type this string itself.
        self.assertIn("。", quote["text"])
        self.assertIn(quote["text"], TRANSCRIPT)
        self.assertEqual(ev["topics"][0]["status"], "decided")

    def test_numbers_are_deduplicated_per_turn_and_literal(self):
        doubled = json.loads(json.dumps(EVIDENCE_REPLY))
        doubled["numbers"].append({"turn": 0, "literal": "十二萬", "means": "重複"})
        ev = self.merge([(chunk_of(self.turns, {0, 1, 2, 3, 4}), doubled)])
        self.assertEqual([n["literal"] for n in ev["numbers"]], ["十二萬", "十萬"])

    def test_a_number_that_is_not_in_its_own_turn_never_reaches_the_document(self):
        """The one near-verbatim field, checked against the source that owns it."""
        wrong = json.loads(json.dumps(EVIDENCE_REPLY))
        wrong["numbers"][0]["literal"] = "五十萬"    # turn 0 says 十二萬
        ev = self.merge([(chunk_of(self.turns, {0, 1, 2, 3, 4}), wrong)])
        self.assertEqual([n["literal"] for n in ev["numbers"]], ["十萬"])
        self.assertEqual(ev["dropped"]["number_not_in_its_turn"], 1)

    def test_numbers_land_on_the_topic_that_owns_their_turn(self):
        ev = self.merge()
        self.assertEqual([n["literal"] for n in ev["topics"][0]["numbers"]], ["十二萬", "十萬"])
        self.assertEqual(ev["topics"][1]["numbers"], [])

    def test_attribution_comes_from_the_turn_not_from_the_model(self):
        wrong = json.loads(json.dumps(EVIDENCE_REPLY))
        wrong["points"][0]["speaker"] = "陳總"   # never in the room
        ev = self.merge([(chunk_of(self.turns, {0, 1, 2, 3, 4}), wrong)])
        self.assertEqual(ev["topics"][0]["points"][0]["speaker"], "王總")

    def test_an_action_with_no_named_owner_is_not_assigned_to_anyone(self):
        anon = json.loads(json.dumps(EVIDENCE_REPLY))
        anon["actions"][0]["owner"] = ""
        ev = self.merge([(chunk_of(self.turns, {0, 1, 2, 3, 4}), anon)])
        self.assertEqual(ev["actions"][0]["owner"], "未指派")

    def test_a_failed_chunk_is_recorded_rather_than_silently_skipped(self):
        ev = pp.merge_evidence([(chunk_of(self.turns, {0, 1}), None)], self.turns)
        self.assertEqual(ev["dropped"]["chunk_failed"], 1)
        self.assertEqual(ev["topics"], [])

    def test_merge_is_deterministic(self):
        a = json.dumps(self.merge(), ensure_ascii=False, sort_keys=True)
        b = json.dumps(self.merge(), ensure_ascii=False, sort_keys=True)
        self.assertEqual(a, b)


class ClusterTests(unittest.TestCase):
    def _topic(self, chunk, label, turns, status="pending", status_turn=None):
        return {"chunk": chunk, "label": label, "turns": turns, "status": status,
                "status_turn": status_turn, "points": [], "numbers": [], "speakers": []}

    def test_adjacent_chunks_with_a_similar_label_are_rejoined(self):
        out = pp.cluster_topics([
            self._topic(0, "付款條件", [8, 9]),
            self._topic(1, "付款條件討論", [10, 11], status="decided", status_turn=11),
        ])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["turns"], [8, 9, 10, 11])
        # The later chunk saw the end of the discussion, so its verdict stands.
        self.assertEqual(out[0]["status"], "decided")

    def test_the_same_subject_twenty_minutes_later_is_a_separate_topic(self):
        out = pp.cluster_topics([
            self._topic(0, "報價討論", [2, 3]),
            self._topic(1, "報價討論", [40, 41]),
        ])
        self.assertEqual(len(out), 2)

    def test_non_adjacent_chunks_are_never_merged(self):
        out = pp.cluster_topics([
            self._topic(0, "報價討論", [2, 3]),
            self._topic(2, "報價討論", [4, 5]),
        ])
        self.assertEqual(len(out), 2)


class SplitTests(unittest.TestCase):
    def test_an_oversized_topic_becomes_consecutive_parts(self):
        turns = [chunker.Turn(index=i, t_start=0, stamp="00:00:00", speaker="王總",
                              text="字" * 1000, raw="") for i in range(6)]
        by_index = {t.index: t for t in turns}
        topic = {"chunk": 0, "label": "長議題", "turns": list(range(6)),
                 "status": "decided", "status_turn": 5, "points": [], "numbers": [],
                 "speakers": []}
        out = pp.split_oversized([topic], by_index, 3000)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["turns"] + out[1]["turns"], list(range(6)))
        self.assertEqual(out[1]["label"], "長議題（續 2）")
        # Only the part holding the deciding turn may claim the verdict.
        self.assertEqual(out[0]["status"], "pending")
        self.assertEqual(out[1]["status"], "decided")

    def test_a_topic_that_fits_is_left_completely_alone(self):
        turns = turns_of()
        by_index = {t.index: t for t in turns}
        topic = {"chunk": 0, "label": "報價", "turns": [0, 1], "status": "pending",
                 "status_turn": None, "points": [], "numbers": [], "speakers": []}
        self.assertEqual(pp.split_oversized([topic], by_index, 3000), [topic])


class ExcerptTests(unittest.TestCase):
    """A quotation shows where a conclusion came from; it does not replay the turn."""

    LONG = ("因為目前他們傳給我們，可能都是業務，他們都是用 LINE 傳給我們。"
            "那我們如果要 key 我們的系統，就是 ERP 系統的話，要先看備註。"
            "月底結帳的時候我們必須要把醫生名填好，再給財會那邊去算費用。"
            "這個是目前前端最吃人工的地方，很容易漏算。")

    def test_a_short_turn_is_quoted_whole(self):
        self.assertEqual(pp.excerpt("那就這樣定了。"), "那就這樣定了。")

    def test_a_long_turn_is_trimmed_to_the_budget(self):
        out = pp.excerpt(self.LONG, max_chars=60)
        self.assertLessEqual(len(out), 60)
        self.assertIn(out, pp.squash(self.LONG))

    def test_the_excerpt_follows_what_the_section_is_about(self):
        """The model's paraphrase steers the cut without supplying any text."""
        out = pp.excerpt(self.LONG, focus="月底結帳要回填醫生名給財會算費用", max_chars=60)
        self.assertIn("醫生名", out)
        self.assertNotIn("LINE", out)

    def test_every_excerpt_stays_an_exact_substring(self):
        for focus in ("", "ERP 系統", "漏算", "業務用 LINE 傳"):
            with self.subTest(focus=focus):
                out = pp.excerpt(self.LONG, focus=focus, max_chars=60)
                self.assertIn(out, pp.squash(self.LONG))

    def test_a_turn_with_no_sentence_breaks_is_still_bounded(self):
        out = pp.excerpt("字" * 500, max_chars=60)
        self.assertEqual(len(out), 60)


class SpecFragmentTests(unittest.TestCase):
    def test_only_this_document_type_and_the_bans_are_handed_over(self):
        frag = pp.spec_fragment("general")
        self.assertIn("## general —", frag)
        self.assertIn("## 通用禁則", frag)
        self.assertNotIn("## client —", frag)
        # The delivery rules and the transcript spec are not the section
        # writer's business, and 200 lines of them is how a small model ends
        # up writing about make_pdf.py instead of about the meeting.
        self.assertNotIn("## 交付規格", frag)
        self.assertNotIn("## transcript_clean.md", frag)
        # The skeleton lives inside a fence and has `##` headings of its own;
        # cutting the block there would drop the depth requirements below it.
        self.assertIn("每個議題的「討論內容」至少 3 條", frag)
        self.assertLess(len(frag), len(pp.SPEC_PATH.read_text()))


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.turns = turns_of()
        self.by_index = {t.index: t for t in self.turns}
        self.evidence = pp.merge_evidence(
            [(chunk_of(self.turns, set(range(5))), EVIDENCE_REPLY)],
            self.turns, roster=["王總", "李經理"])
        self.sections = [
            {"topic_id": "T01", "heading": "報價金額拍板",
             "discussion": [{"turn": 0, "point": "本批報價抓十二萬含稅"},
                            {"turn": 1, "point": "認為偏高，對方上次是十萬"},
                            {"turn": 2, "point": "拍板照十二萬執行"}],
             "status": "decided", "basis": "王總在會中明確拍板", "quote_turns": [2]},
            {"topic_id": "T02", "heading": "交期待工廠確認",
             "discussion": [{"turn": 4, "point": "交期要回去問工廠"}],
             "status": "pending", "basis": "沒有人拍板", "quote_turns": [4]},
        ]
        self.meta = {"title": "報價會議", "client": "某公司", "date": "2026-08-20",
                     "participants": "王總、李經理", "meeting_type": "general"}

    def doc(self, stem="note_general"):
        return pp.render_document(stem, self.meta, self.evidence, self.sections,
                                  ["報價定為十二萬含稅。"], self.by_index)

    def test_the_skeleton_is_always_complete(self):
        text = self.doc()
        for heading in ("一、本次會議重點", "二、議題討論", "三、決議事項",
                        "四、待辦事項", "五、未解決事項與待確認", "六、下次會議"):
            self.assertIn(f"## {heading}", text)

    def test_every_topic_gets_a_numbered_section(self):
        text = self.doc()
        self.assertIn("### 1. 報價金額拍板", text)
        self.assertIn("### 2. 交期待工廠確認", text)

    def test_a_name_the_model_repeated_is_not_printed_twice(self):
        """Python renders the attribution; 「王總：王總表示…」 is the failure."""
        self.sections[0]["discussion"] = [{"turn": 0, "point": "王總表示報價抓十二萬"}]
        body = self.doc().split("### 1.")[1].split("### 2.")[0]
        self.assertIn("  - 王總：表示報價抓十二萬", body)
        self.assertNotIn("王總：王總", body)

    def test_quotes_do_not_swallow_the_whole_turn(self):
        long_turn = "**[00:10:00] 王總**\n\n" + "這是一句很長的話。" * 40
        turns = turns_of(TRANSCRIPT + "\n" + long_turn)
        by_index = {t.index: t for t in turns}
        quote = pp.quote_for(by_index[max(by_index)])
        self.assertLessEqual(len(quote["text"]), pp.QUOTE_MAX_CHARS)

    def test_quotes_are_verbatim_substrings_of_the_source(self):
        text = self.doc()
        quoted = [line for line in text.split("\n") if line.startswith(">")]
        self.assertTrue(quoted)
        for line in quoted:
            inner = line.split("：「", 1)[1].rsplit("」", 1)[0]
            self.assertIn(inner, TRANSCRIPT, f"引文不在逐字稿裡：{inner}")

    def test_notes_carry_no_timestamps_but_interview_quotes_do(self):
        self.assertNotIn("[00:01:10]", self.doc())
        self.assertIn("[00:01:10]", self.doc("note_interview"))

    def test_a_decided_topic_reaches_the_decision_table(self):
        self.assertIn("| 1 | 報價金額拍板 | 王總在會中明確拍板 |", self.doc())

    def test_a_pending_topic_is_listed_as_unresolved_instead(self):
        text = self.doc()
        self.assertIn("- 交期待工廠確認——待定", text)

    def test_a_thin_section_is_topped_up_from_the_evidence(self):
        """NOTE_SPECS wants three attributed points; S3 already has them."""
        self.sections[0]["discussion"] = [{"turn": 0, "point": "只寫了一條"}]
        body = self.doc().split("### 1.")[1].split("### 2.")[0]
        self.assertGreaterEqual(len([l for l in body.split("\n") if l.startswith("  - ")]), 3)

    def test_a_section_the_model_failed_still_appears_with_its_evidence(self):
        self.sections[0] = {"topic_id": "T01", "heading": "報價金額", "discussion": [],
                            "status": "pending", "basis": "", "quote_turns": []}
        body = self.doc().split("### 1.")[1].split("### 2.")[0]
        self.assertIn("報價抓十二萬含稅", body)
        self.assertIn(">", body)   # still quoted, from the longest turn

    def test_a_section_the_model_failed_says_so_in_the_document(self):
        """An unwritten section must not read as a merely thin one."""
        self.sections[0] = {"topic_id": "T01", "heading": "報價金額", "discussion": [],
                            "status": "pending", "basis": "", "quote_turns": [],
                            "failed": True}
        body = self.doc().split("### 1.")[1].split("### 2.")[0]
        self.assertIn("本節未完成", body)
        self.assertIn("人工補寫", body)
        # and only that section is marked
        self.assertNotIn("本節未完成", self.doc().split("### 2.")[1])

    def test_a_section_that_was_written_carries_no_warning(self):
        self.sections[0]["failed"] = False
        self.assertNotIn("本節未完成", self.doc())

    def test_a_pipe_in_the_content_cannot_break_a_table(self):
        self.sections[0]["basis"] = "A|B 兩案"
        row = [l for l in self.doc().split("\n") if l.startswith("| 1 |")][0]
        self.assertEqual(row.count("|"), 4)

    def test_action_items_json_is_built_without_a_model(self):
        data = pp.build_action_items(self.meta, {}, self.evidence, self.sections)
        self.assertEqual(data["decisions"], [{"decision": "報價金額拍板",
                                              "basis": "王總在會中明確拍板"}])
        self.assertEqual(data["actions"], [{"item": "向工廠確認交期", "responsible": "王總",
                                            "deadline": "下週", "status": "未開始"}])
        self.assertEqual(data["numbers"], ["十二萬", "十萬"])
        self.assertEqual(data["meeting"]["participants"], ["王總", "李經理"])

    def test_an_empty_meeting_still_produces_a_complete_document(self):
        self.evidence = pp.merge_evidence([], self.turns)
        self.sections = []
        text = self.doc()
        self.assertIn("## 三、決議事項", text)
        self.assertIn("本次會議未產生明確決議", text)


class FakeJob:
    """The four attributes the model-facing stages actually read."""

    def __init__(self, work: Path, max_parallel=None):
        self.work = work
        self.acfg = {"max_parallel": max_parallel} if max_parallel else {}
        self.backend, self.binary, self.model = "openai_compat", "", "test-model"
        self.log, self.timeout = work / "agent.log", 60
        self.user_context = ""


class StageFailureTests(unittest.TestCase):
    """What happens to the document when a model call comes back empty.

    `_call` returns None on a timeout, an unparseable reply or a schema
    mismatch, and raising the worker count makes all three more likely. The
    contract tested here is that neither failure can reach a reader as a
    complete-looking note.
    """

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.job = FakeJob(Path(self.tmp.name))
        self.turns = turns_of()
        self.by_index = {t.index: t for t in self.turns}
        self.meta = {"title": "報價會議", "meeting_type": "general"}
        self.evidence = pp.merge_evidence(
            [(chunk_of(self.turns, set(range(5))), EVIDENCE_REPLY)],
            self.turns, roster=["王總", "李經理"])

    def patch_call(self, replies):
        """`replies` is a list consumed in call order; None means failure."""
        calls = list(replies)
        seen = []

        def fake_call(job, prompt, schema, system, label):
            seen.append(label)
            return calls.pop(0) if calls else None

        original = pp._call
        pp._call = fake_call
        self.addCleanup(lambda: setattr(pp, "_call", original))
        return seen

    def chunks(self, n=2):
        half = len(self.turns) // 2
        return [chunker.Chunk(index=0, turns=self.turns[:half]),
                chunker.Chunk(index=1, turns=self.turns[half:])][:n]

    # -- S2 ---------------------------------------------------------------- #
    def test_a_chunk_that_failed_to_extract_fails_the_whole_job(self):
        """Evidence with a hole in it produces a note nothing downstream can flag."""
        self.patch_call([EVIDENCE_REPLY, None])
        with self.assertRaises(RuntimeError) as caught:
            pp.extract_evidence(self.job, self.chunks(), self.meta)
        self.assertIn("S2", str(caught.exception))
        self.assertIn("2", str(caught.exception))   # which chunk

    def test_every_chunk_failing_also_fails_the_job(self):
        self.patch_call([None, None])
        with self.assertRaises(RuntimeError):
            pp.extract_evidence(self.job, self.chunks(), self.meta)

    def test_all_chunks_extracting_is_not_a_failure(self):
        self.patch_call([EVIDENCE_REPLY, EVIDENCE_REPLY])
        parts = pp.extract_evidence(self.job, self.chunks(), self.meta)
        self.assertEqual(len(parts), 2)
        self.assertTrue(all(data for _, data in parts))

    # -- S4 ---------------------------------------------------------------- #
    def test_a_topic_the_model_never_wrote_is_flagged_not_skipped(self):
        section_reply = {"heading": "報價金額拍板", "discussion": [], "basis": "拍板",
                         "status": "decided", "quote_turns": [2]}
        self.patch_call([section_reply, None])
        sections = pp.write_sections(self.job, self.evidence, self.meta, self.by_index)
        self.assertEqual(len(sections), len(self.evidence["topics"]))
        self.assertEqual([s.get("failed") for s in sections], [False, True])

    def test_the_failure_reaches_the_rendered_document(self):
        self.patch_call([None, None])
        sections = pp.write_sections(self.job, self.evidence, self.meta, self.by_index)
        text = pp.render_document("note_general", self.meta, self.evidence,
                                  sections, ["重點"], self.by_index)
        self.assertEqual(text.count("本節未完成"), len(self.evidence["topics"]))

    # -- S5 ---------------------------------------------------------------- #
    def test_a_failed_opener_is_reported_as_failed(self):
        """The fallback opener is good, which is exactly why it hides the failure.

        Losing the model call leaves a list of topic headings assembled by
        Python — never wrong, and never the summary anyone asked for. Because
        that list is non-empty, reading failure off the returned value said the
        stage succeeded every time, and the run report's s5_overview counter
        could not have gone above zero.
        """
        self.patch_call([None])
        lines, failed = pp.write_overview(self.job, self.evidence, [], self.meta)
        self.assertTrue(failed)
        self.assertTrue(lines, "the fallback opener should still be written")

    def test_a_written_opener_is_not_reported_as_failed(self):
        self.patch_call([{"highlights": ["報價定為十二萬含稅。"]}])
        lines, failed = pp.write_overview(self.job, self.evidence, [], self.meta)
        self.assertFalse(failed)
        self.assertEqual(lines, ["報價定為十二萬含稅。"])

    def test_a_mechanical_opener_says_so_in_the_document(self):
        text = pp.render_document("note_general", self.meta, self.evidence,
                                  [], ["議題一：待定"], self.by_index,
                                  overview_failed=True)
        self.assertIn("本節未完成", text.split("## 二、")[0])

    def test_a_written_opener_adds_no_warning(self):
        text = pp.render_document("note_general", self.meta, self.evidence,
                                  [], ["重點"], self.by_index)
        self.assertNotIn("本節未完成", text.split("## 二、")[0])


class ParallelKnobTests(unittest.TestCase):
    """Worker count resolves profile -> global -> 3, and nothing else."""

    def cfg(self, agent: dict) -> dict:
        return {"agent": agent}

    def resolve(self, agent: dict) -> int:
        import config as config_mod
        acfg = config_mod.resolve_agent_config({"agent_preset": "private"},
                                               cfg=self.cfg(agent))
        return max(1, int(acfg.get("max_parallel") or 3))

    def test_the_private_profile_knob_wins(self):
        self.assertEqual(self.resolve({"max_parallel": 3,
                                       "profiles": {"private": {"max_parallel": 8}}}), 8)

    def test_a_profile_with_no_opinion_inherits_the_global(self):
        self.assertEqual(self.resolve({"max_parallel": 5,
                                       "profiles": {"private": {"max_parallel": None}}}), 5)
        self.assertEqual(self.resolve({"max_parallel": 5, "profiles": {"private": {}}}), 5)

    def test_neither_set_falls_back_to_three(self):
        self.assertEqual(self.resolve({"profiles": {"private": {}}}), 3)

    def test_the_cli_path_is_not_moved_by_the_private_knob(self):
        """agent.max_parallel counts CLI processes; private counts HTTP requests."""
        import config as config_mod
        agent = {"max_parallel": 3, "backend": "claude",
                 "profiles": {"private": {"max_parallel": 8}}}
        general = config_mod.resolve_agent_config({"agent_preset": "general"},
                                                  cfg=self.cfg(agent))
        self.assertEqual(general["max_parallel"], 3)

    def test_the_shipped_default_keeps_the_two_knobs_separate(self):
        import config as config_mod
        defaults = config_mod.DEFAULTS["agent"]
        self.assertEqual(defaults["max_parallel"], 3)
        self.assertIn("max_parallel", defaults["profiles"]["private"])


class ParallelOrderTests(unittest.TestCase):
    """Worker count must change the clock and nothing else.

    S3 merges chunks in order — an evidence item's turn numbers, the topic
    labels it tries to cluster with, and which of two near-identical labels
    wins are all decided by the sequence the map stage hands back. If that
    sequence tracked completion order instead of input order, raising
    max_parallel would quietly reshuffle the merge and produce a different
    document from the same meeting, which would make every measurement of the
    knob meaningless.
    """

    class _Job:
        def __init__(self, workers):
            self.acfg = {"max_parallel": workers}

    @staticmethod
    def _slow(i):
        # Later items finish first, so completion order is the reverse of
        # input order and any ordering bug is certain rather than likely.
        import time
        time.sleep((12 - i) * 0.001)
        return i

    def test_results_come_back_in_input_order_at_every_worker_count(self):
        items = list(range(12))
        for workers in (1, 2, 3, 6, 8, 12, 16):
            got = pp._parallel(self._Job(workers), items, self._slow)
            self.assertEqual(got, items, f"max_parallel={workers} reordered the results")

    def test_a_single_item_still_comes_back_wrapped_in_a_list(self):
        # The <=1 shortcut skips the pool entirely; it must not skip the shape.
        self.assertEqual(pp._parallel(self._Job(8), [7], lambda x: x * 2), [14])
        self.assertEqual(pp._parallel(self._Job(8), [], lambda x: x), [])

    def test_a_failing_item_stops_the_stage_rather_than_vanishing(self):
        def boom(i):
            if i == 5:
                raise RuntimeError("chunk 5 blew up")
            return i

        with self.assertRaises(RuntimeError):
            pp._parallel(self._Job(8), list(range(20)), boom)


if __name__ == "__main__":
    unittest.main()
