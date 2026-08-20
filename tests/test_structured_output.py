"""P1 — constrained decoding instead of asking a model to please return JSON.

Two failures motivated this and both are covered here:

  * The shared scan prompt asked for the cleaned transcript back inside a JSON
    string. Output ≈ input on a 14k-character chunk, `max_output_tokens` cut it
    mid-string, and the chunk was lost. The private path now returns
    replacements + questions only, and points at turns by number.
  * `qwen3.8-27b-mtplx` on LM Studio returns its whole reply in
    `reasoning_content` with `content` empty, even under a strict schema.
    Read literally that is "服務回傳空白內容" for a perfectly good answer.
"""
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import chunker  # noqa: E402
import review  # noqa: E402
import schemas  # noqa: E402
from agent_note import _extract_openai_text, _openai_compat_request  # noqa: E402

ACFG = {"api": {"base_url": "http://lm/v1", "endpoint": "chat_completions",
                "temperature": 0.1, "max_output_tokens": 8192}}


class RequestShapeTests(unittest.TestCase):
    def test_schema_becomes_a_strict_json_schema_response_format(self):
        _, _, body, _ = _openai_compat_request("x", "m", ACFG, schema=schemas.EVIDENCE)
        fmt = body["response_format"]
        self.assertEqual(fmt["type"], "json_schema")
        self.assertTrue(fmt["json_schema"]["strict"])
        # The schema's own title names it on the wire, so schemas.py stays the
        # single source and call sites never repeat a string.
        self.assertEqual(fmt["json_schema"]["name"], "evidence")
        self.assertIs(fmt["json_schema"]["schema"], schemas.EVIDENCE)

    def test_no_schema_leaves_the_request_exactly_as_it_was(self):
        _, url, body, _ = _openai_compat_request("x", "m", ACFG)
        self.assertNotIn("response_format", body)
        self.assertEqual(url, "http://lm/v1/chat/completions")
        self.assertEqual(body["messages"], [{"role": "user", "content": "x"}])

    def test_system_prompt_is_a_system_message(self):
        _, _, body, _ = _openai_compat_request("x", "m", ACFG, system="你是抽取器")
        self.assertEqual(body["messages"][0], {"role": "system", "content": "你是抽取器"})
        self.assertEqual(body["messages"][1]["content"], "x")

    def test_responses_endpoint_uses_its_own_spelling(self):
        acfg = {"api": dict(ACFG["api"], endpoint="responses")}
        _, url, body, _ = _openai_compat_request("x", "m", acfg, schema=schemas.SECTION,
                                                 system="s")
        self.assertTrue(url.endswith("/responses"))
        self.assertEqual(body["text"]["format"]["type"], "json_schema")
        self.assertEqual(body["text"]["format"]["name"], "section")
        self.assertEqual(body["instructions"], "s")


class ReplyExtractionTests(unittest.TestCase):
    def _msg(self, **kw):
        return {"choices": [{"message": kw}]}

    def test_reasoning_only_reply_is_read_rather_than_called_empty(self):
        payload = self._msg(content="", reasoning_content='{"a": 1}')
        self.assertEqual(_extract_openai_text(payload, "chat_completions"), '{"a": 1}')

    def test_real_content_always_wins_over_the_scratchpad(self):
        payload = self._msg(content="答案", reasoning_content="讓我想想…")
        self.assertEqual(_extract_openai_text(payload, "chat_completions"), "答案")

    def test_genuinely_empty_stays_empty(self):
        self.assertEqual(_extract_openai_text(self._msg(content=""), "chat_completions"), "")


class SchemaShapeTests(unittest.TestCase):
    """Strict mode rejects a schema that forgets these; catch it here, not on the wire."""

    def _walk(self, node, path="$"):
        if not isinstance(node, dict):
            return
        types = node.get("type")
        types = [types] if isinstance(types, str) else list(types or [])
        if "object" in types:
            props = node.get("properties") or {}
            self.assertIs(node.get("additionalProperties"), False, f"{path} 少了 additionalProperties:false")
            self.assertEqual(sorted(node.get("required") or []), sorted(props),
                             f"{path} 的 required 必須列出全部欄位")
            for key, sub in props.items():
                self._walk(sub, f"{path}.{key}")
        if "array" in types:
            self._walk(node.get("items"), f"{path}[]")

    def test_every_schema_is_strict_mode_clean(self):
        for name in ("SCAN", "EVIDENCE", "SECTION", "OVERVIEW"):
            with self.subTest(schema=name):
                self._walk(getattr(schemas, name), name)

    def test_evidence_has_exactly_one_field_that_may_touch_source_text(self):
        """The soul clause, pinned to the schema rather than trusted to a prompt.

        Every string a model can put in EVIDENCE is either its own words
        (a label, a paraphrase, a reason) or an identifier. The single
        exception is `numbers[].literal`, which exists so note_verify can
        check a copied string against its own turn and drop it if it drifted.
        A new free-text field is exactly how quote fabrication would creep
        back in, so adding one has to break this test first.
        """
        strings = []

        def walk(node, path="$"):
            if not isinstance(node, dict):
                return
            types = node.get("type")
            types = [types] if isinstance(types, str) else list(types or [])
            if "string" in types and not node.get("enum"):
                strings.append(path)
            for key, sub in (node.get("properties") or {}).items():
                walk(sub, f"{path}.{key}")
            walk(node.get("items"), f"{path}[]")

        walk(schemas.EVIDENCE)
        self.assertEqual(sorted(strings), [
            "$.actions[].due",
            "$.actions[].owner",
            "$.actions[].what",
            "$.numbers[].literal",   # the one allowed near-verbatim field
            "$.numbers[].means",
            "$.points[].gist",
            "$.points[].speaker",
            "$.points[].topic_id",
            "$.topics[].label",
            "$.topics[].speakers[]",
            "$.topics[].topic_id",
            "$.unclear[].why",
        ])

    def test_section_never_lets_the_model_hand_back_a_quotation(self):
        """S4 returns turn numbers for quotes; S5 cuts the text itself."""
        props = schemas.SECTION["properties"]
        self.assertEqual(props["quote_turns"]["items"]["type"], "integer")
        self.assertEqual(sorted(props["discussion"]["items"]["properties"]), ["point", "turn"])

    def test_validator_reports_the_path_that_is_wrong(self):
        bad = {"replacements": [], "questions": [],
               "speakers": [{"label": "講者1", "guess": "王總", "confidence": "maybe", "why": ""}]}
        problems = schemas.validate(bad, schemas.SCAN)
        self.assertTrue(any("speakers[0].confidence" in p for p in problems), problems)

    def test_validator_accepts_a_nullable_integer(self):
        ev = {"topics": [{"topic_id": "t1", "label": "報價", "turns": [3],
                          "status": "pending", "status_turn": None, "speakers": []}],
              "points": [], "numbers": [], "actions": [], "unclear": []}
        self.assertEqual(schemas.validate(ev, schemas.EVIDENCE), [])


class PrivateScanTests(unittest.TestCase):
    TRANSCRIPT = (
        "**[00:00:10] 王總**\n\n這批報價我們抓十二萬。\n\n"
        "**[00:01:00] 李經理**\n\n那交期呢？\n\n"
        "**[00:02:00] 王總**\n\n就這樣定了。\n"
    )

    def _chunk(self):
        turns = chunker.parse_transcript(self.TRANSCRIPT)
        return chunker.chunk(turns, 10000, 0)[0]

    def test_turns_are_rendered_with_the_index_the_model_must_answer_with(self):
        rendered = chunker.render_indexed(self._chunk().own_turns)
        self.assertIn("#0 [00:00:10] 王總：這批報價我們抓十二萬。", rendered)
        self.assertEqual(len(rendered.strip().split("\n")), 3)

    def test_evidence_turns_are_hydrated_into_quotes_by_python(self):
        data = {"questions": [{"key": "十二萬", "evidence_turns": [0, 2]}]}
        review.hydrate_scan_evidence(data, self._chunk())
        card = data["questions"][0]
        self.assertNotIn("evidence_turns", card)
        self.assertEqual([e["timestamp"] for e in card["evidence"]], ["00:00:10", "00:02:00"])
        # Verbatim, including the full-width period the model kept mangling.
        self.assertEqual(card["evidence"][1]["text"], "就這樣定了。")

    def test_a_turn_index_outside_the_chunk_is_dropped_not_guessed(self):
        data = {"questions": [{"key": "x", "evidence_turns": [99]}]}
        review.hydrate_scan_evidence(data, self._chunk())
        self.assertEqual(data["questions"][0]["evidence"], [])

    def test_missing_draft_is_the_contract_not_a_failed_chunk(self):
        job = mock.Mock()
        job.dir = Path("/tmp/nope")
        job.acfg = {"max_questions": 8}
        ch = self._chunk()
        data = {"replacements": [], "speakers": [], "questions": []}
        with mock.patch.object(review.Path, "read_text", return_value=self.TRANSCRIPT):
            merged = review.merge_scans(job, [(ch, data, "")], expect_draft=False)
        self.assertEqual(merged["failed"], [])
        # Python rebuilds the draft from the turns it already parsed.
        self.assertIn("就這樣定了。", merged["draft"])

    def test_legacy_path_still_treats_a_missing_draft_as_a_failure(self):
        job = mock.Mock()
        job.dir = Path("/tmp/nope")
        job.acfg = {"max_questions": 8}
        ch = self._chunk()
        with mock.patch.object(review.Path, "read_text", return_value=self.TRANSCRIPT):
            merged = review.merge_scans(job, [(ch, {"draft": ""}, "")], expect_draft=True)
        self.assertEqual(merged["failed"], [ch.index])


class PipelineSelectionTests(unittest.TestCase):
    def test_api_backend_defaults_to_the_evidence_pipeline(self):
        self.assertTrue(review.uses_evidence_pipeline("openai_compat", {}))

    def test_legacy_is_opt_in_and_explicit(self):
        self.assertFalse(review.uses_evidence_pipeline("openai_compat", {"pipeline": "legacy"}))

    def test_cli_backends_are_untouched(self):
        self.assertFalse(review.uses_evidence_pipeline("claude", {"pipeline": "evidence"}))


if __name__ == "__main__":
    unittest.main()
