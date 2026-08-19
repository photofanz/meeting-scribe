"""The download panel used to be a denylist and leaked whatever the pipeline
learned to write next: the raw ASR transcript, the pre-review draft, INDEX.md
and action_items.json all reached users who had asked for a meeting note."""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import jobstate  # noqa: E402

# A real job directory, as observed in 2026-08-17_DT論文研究小組.
JOB_FILES = [
    "meta.json", "status.json", "state.json", "delivery.json",
    "questions.json", "answers.json", "agent_report.json",
    "source.m4a",
    "transcript.md", "transcript.json", "transcript.txt", "transcript_draft.md",
    "transcript_clean.md", "transcript_clean.pdf", "transcript_clean.docx",
    "note_general.md", "note_general.pdf", "note_general.docx",
    "action_items.json", "INDEX.md",
]


def make_job(names=JOB_FILES):
    d = Path(tempfile.mkdtemp())
    for n in names:
        (d / n).write_text("x", encoding="utf-8")
    return d


class DeliverableAllowlistTests(unittest.TestCase):
    def test_internal_files_never_reach_the_download_list(self):
        rows = jobstate.list_files(make_job(), formats=["md", "pdf", "docx"])
        names = [r["name"] for r in rows]
        for leaked in ("action_items.json", "INDEX.md", "transcript.md",
                       "transcript.json", "transcript.txt", "transcript_draft.md",
                       "meta.json", "state.json", "source.m4a"):
            self.assertNotIn(leaked, names)

    def test_reviewed_transcript_is_kept(self):
        # The user earned transcript_clean by answering question cards; it is a
        # deliverable even though the raw transcript it came from is not.
        rows = jobstate.list_files(make_job(), formats=["md"])
        self.assertIn("transcript_clean.md", [r["name"] for r in rows])

    def test_only_the_requested_formats_are_listed(self):
        rows = jobstate.list_files(make_job(), formats=["pdf"])
        self.assertEqual([r["name"] for r in rows],
                         ["transcript_clean.pdf", "note_general.pdf"])

    def test_md_only_deliverable_survives_a_pdf_only_job(self):
        # email_draft.md has no PDF sibling; filtering by format must not erase it.
        d = make_job(["note_client.md", "note_client.pdf", "email_draft.md"])
        self.assertIn("email_draft.md", [r["name"] for r in jobstate.list_files(d, formats=["pdf"])])

    def test_failed_pdf_step_falls_back_to_markdown(self):
        d = make_job(["note_general.md"])
        self.assertEqual([r["name"] for r in jobstate.list_files(d, formats=["pdf"])],
                         ["note_general.md"])

    def test_order_is_transcript_then_note_and_md_before_pdf(self):
        rows = jobstate.list_files(make_job(), formats=["md", "pdf", "docx"])
        self.assertEqual([r["name"] for r in rows], [
            "transcript_clean.md", "transcript_clean.pdf", "transcript_clean.docx",
            "note_general.md", "note_general.pdf", "note_general.docx",
        ])


if __name__ == "__main__":
    unittest.main()
