# The review stage

`bin/review.py` is what turns a transcript into documents. It exists because
the naive version — hand the whole transcript to one model and ask for a
meeting note — fails in two specific, observed ways.

**It runs out of room.** A two-hour meeting is a ~127 KB transcript. One
prompt cannot hold it, and the failure is silent: the model reads the opening,
writes a confident note about the first twenty minutes, and exits 0. Nothing
downstream can tell that apart from success.

**It guesses about things only you know.** On a real conference call,
diarisation split three people into 112 speaker clusters. On another, ASR
rendered one surname seven different ways (革新 / 葛新 / 葛星 / 葛晶 / 可欣 /
可信 / 可惜). A model asked to resolve that will resolve it — plausibly, and
sometimes wrongly, and the wrong version ends up stated as fact in a document
you send to a client.

So the work is split at exactly those two seams.

```
  stage 1  scan     transcript.md
                      → chunker.py slices it (a turn is never split)
                      → N readers run in parallel, each on one slice
                      → transcript_draft.md   (cleaned, labels untouched)
                      → questions.json        (≤8 cards, each clickable)
                      → state: awaiting_answers, notify

           ── you answer at /job/<id>, by clicking ──

  stage 2  write    answers.json
                      → Python applies them by regex (never a model)
                      → transcript_clean.md   (final, with a correction table)
                      → the note is written from the CLEAN transcript
                      → PDF / Word conversion, delivery, notify
```

`--stage auto` runs both back to back using every `best_guess`. That produces
a worse document, and the document says so at the top.

## Why the answers are applied in Python

They are applied with `re.sub`, longest-match-first, with a negative lookahead
on trailing digits so `講者1` can never eat the prefix of `講者11`. This is
deliberate. A model told "講者3 is 王總" will honour it for the first twenty
paragraphs and start drifting. The substitution is mechanical, so it is done
mechanically, and the writing agent receives a transcript in which the names
are simply correct — it is never asked to remember a mapping.

The same applies to the correction table printed at the end of
`transcript_clean.md`: it lists what was actually substituted, not what a model
says it did.

## Why the questions look the way they do

A card must be answerable by clicking. If a question needs you to type a
paragraph, it is the wrong question — the readers are told to fix what they
can infer themselves and only escalate what genuinely requires you.

Five types, ranked in this order:

| type | when |
|---|---|
| `speaker` | which real person a diarisation label is. Ranked first: every other answer is cosmetic next to putting the wrong name on a quote. |
| `term` | a proper noun ASR spelled several ways, needing one canonical form |
| `unclear` | audio too poor to recover a figure, date or amount that matters |
| `conflict` | the transcript says two contradictory things and one must win |
| `undecided` | the meeting never actually concluded something the note must state |

Cards are deduplicated across chunks on `(type, key)` and ranked partly by how
many chunks raised them — a surname that confused four readers matters more
than one that confused one. The cap (`agent.max_questions`, default 8) is a
product decision, not a technical one: past roughly eight cards people skip the
whole screen, which is strictly worse than asking fewer questions.

## Private mode takes a different second stage

Everything above describes stage 2 as "hand the clean transcript to a writing
agent". That works when the agent is Claude. It does not work when the agent is
a 27B model on the LM Studio box, and the measurement was unambiguous: 2,572
seconds to produce a 1,272-character note from a 21,000-character transcript —
0.061 of its source, with every topic marked 待定.

The cause was not the model being small. It was being asked to do long-range
autonomous planning: read 20,000 characters, decide what matters, remember the
skeleton, and write the whole document in one sustained pass. That is the one
thing models this size are worst at, and no amount of prompt tuning fixes it.

So private mode (`agent.profiles.private.pipeline: "evidence"`) routes around
it — the work is cut until no single step needs long-range coherence:

```
  S2  map     one chunk, one strict json_schema  -> which turns discuss what
  S3  reduce  pure Python  -> merge, drop the overlap, rejoin split topics,
                              dedupe, order by turn, cut the quotations
  S4  map     one topic, one strict json_schema  -> the prose for one section
  S5  emit    pure Python  -> skeleton, tables, action_items.json, INDEX.md
  S6  gate    pure Python  -> verify, rewrite only the sections that failed
```

Coverage stops depending on the model remembering how many topics there were,
because `bin/private_pipeline.py` counts them. Structure stops depending on it
remembering the skeleton, because Python emits it. And quotation stops
depending on it copying carefully:

> **The model returns turn numbers. It never returns transcript text.**

Asked for a quotation, the model returned 「就這樣定了.」 for 「就這樣定了。」 —
a full-width period quietly turned into an ASCII one. A model allowed to
restate the source will edit it, and an edited quote is a fabricated quote.
Every quotation is instead cut from `transcript_clean.md` by turn index, so
`bin/note_verify.py` can check each one back against the same file. Fabrication
is not discouraged here; it is unreachable.

Three consequences worth knowing:

- **The scan stage stops echoing the draft.** `SCAN_TASK_PRIVATE.md` asks only
  for replacements, speakers and questions. Asking for the cleaned transcript
  back inside a JSON string meant output ≈ input, and `max_output_tokens` cut
  the reply mid-string on essentially every Chinese chunk.
- **The model is checked before the work starts.** `capability_check()` reads
  LM Studio's own metadata — `trained_for_tool_use`, `max_context_length`,
  `format` — and refuses an unusable model in under a tenth of a second, naming
  the models on the same machine that would have worked.
- **`pipeline: "legacy"`** keeps the old single-writer path for one version, so
  the two can be compared on the same job. `bin/note_eval.py` is what compares
  them.

```bash
.venv/bin/python bin/note_verify.py <job_dir>        # audit one job
.venv/bin/python bin/note_eval.py --archive          # score every archived job
```

## Failure behaviour

Every stage is written so that a partial failure degrades instead of aborting.

- **A scan chunk fails** (agent wrote nothing, or unparseable JSON): it is
  retried once, then that slice contributes its *raw* turns to the draft. A
  rough paragraph in the middle of a transcript is much better than a hole, and
  stage 2 still sees the real words. Failed chunk indices are recorded in
  `questions.json.failed_chunks` and shown in the notification.
- **A "replacement" that does not occur in the source is dropped.** Readers
  occasionally return `{"find": "简体全文", "replace": "繁體中文"}` — a note
  about their own work, shaped like a substitution. Applying that globally is a
  hazard and listing it in the correction table is a lie, so anything whose
  `find` is absent from the transcript is discarded and logged.
- **Success is never inferred from an exit code.** Both supported CLIs can
  exit 0 having written nothing (not logged in) and exit non-zero having
  written everything. Every stage checks for the file it asked for.
- **A crash leaves the job diagnosable**: state goes to `error` with the
  exception on it, the scratch directory `.review/` is kept, and
  `logs/<job>-agent.log` has the full CLI output.

## Files a job accumulates

| file | written by | survives "clean outputs" |
|---|---|---|
| `source.*`, `transcript.{md,json,txt}`, `meta.json`, `status.json` | ASR pipeline | yes |
| `questions.json`, `answers.json` | stage 1 / the web UI | yes |
| `transcript_draft.md` | stage 1 | no |
| `transcript_clean.md` (+ pdf/docx) | stage 2, in Python | no |
| `note_*.md` (+ pdf/docx), `action_items.json`, `INDEX.md` | the writing agent, or `private_pipeline.py` | no |
| `.review/evidence.json`, `evidence_raw_NN.json`, `section_TNN.json` | private mode's S2/S3/S4 | no |
| `delivery.json`, `agent_report.json` | stage 2 | no |
| `.review/` scratch (per-chunk JSON, the exact write prompt) | both stages | no |

"Clean outputs" in the jobs list deletes only the regenerable column. The
audio, the raw transcript, your answers and the job metadata always survive, so
any job can be re-run later — with a different template, or after you correct a
name — without re-uploading the recording.

## Running it by hand

```bash
.venv/bin/python bin/review.py latest --stage scan            # ask
.venv/bin/python bin/review.py latest --stage write --deliver # answer applied
.venv/bin/python bin/review.py latest --stage auto --deliver  # both, no human
.venv/bin/python bin/review.py <job_id> --stage write --backend codex
```

`bin/chunker.py <transcript.md>` prints the slice plan without spending any
model time — worth a look before a long job.
