#!/usr/bin/env python
"""
Agentic tool-calling loop for OpenAI-compatible backends.

Background
----------
The original API backend treated a local model as a text completion service:
the entire spec plus the entire transcript were stuffed into one prompt, and
the model had to emit every deliverable inside a single JSON object.

That framing costs a lot of quality:

  * the writing discipline in ``templates/AGENT_TASK.md`` was replaced by a
    long explanation of how to shape a JSON envelope, so the model spent its
    attention on the envelope rather than on the meeting;
  * every file had to fit in one response, with newlines escaped, which pushes
    a model towards a short summary instead of a full document;
  * one malformed brace threw away an hour of work.

A model with tool-calling support does not need any of that. It can be driven
the same way Claude Code and Codex are driven: hand it the task description,
give it ``read_file`` / ``write_file``, and let it read the transcript in
slices and write each deliverable as its own file.

This module implements that loop. It is deliberately conservative about what
the model is allowed to touch -- see ``_resolve`` -- because the whole point of
private mode is that a local model handles confidential audio without anything
leaving the machine, and a sandbox escape here would undermine that.

    from agent_tools import run_tool_loop
    rc, elapsed, transcript = run_tool_loop(prompt, model, acfg, ...)
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

# --------------------------------------------------------------------------- #
# tool schema
# --------------------------------------------------------------------------- #
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "讀取一個檔案的內容。檔案很長時用 offset/limit 分段讀，"
                "回傳結果會告訴你總行數以及是否還有後續。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "絕對路徑或相對於 job 資料夾的路徑"},
                    "offset": {"type": "integer", "description": "起始行號，從 1 開始，預設 1"},
                    "limit": {"type": "integer", "description": "要讀幾行，預設 400"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "把內容寫入 job 資料夾裡的檔案。"
                "mode='overwrite'（預設）覆蓋整個檔案；"
                "mode='append' 接在既有內容後面，兩段之間自動空一行。"
                "長文件請分段寫：第一次用 overwrite 寫開頭幾節，之後用 append 一節一節接上，"
                "**不要為了塞進單次回覆而把內容壓縮成摘要**。"
                "`.json` 檔必須一次寫完，不能 append。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "檔名，例如 note_general.md"},
                    "content": {"type": "string", "description": "這一段要寫入的內容"},
                    "mode": {
                        "type": "string",
                        "enum": ["overwrite", "append"],
                        "description": "overwrite=覆蓋整份（預設）；append=接在既有內容後面",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "列出 job 資料夾裡現有的檔案，用來確認自己已經寫了哪些。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

DEFAULT_LIMIT = 400
MAX_LIMIT = 3000
MAX_READ_CHARS = 60000


class ToolLoopError(RuntimeError):
    """Transport or protocol failure while driving the tool loop."""


# --------------------------------------------------------------------------- #
# sandbox
# --------------------------------------------------------------------------- #
def _resolve(raw: str, job_dir: Path, read_roots: list[Path], *, write: bool) -> Path:
    """Map a model-supplied path onto disk, refusing anything outside the sandbox.

    Writes are confined to ``job_dir``. Reads additionally allow the template
    directory so the model can pull in the spec. Relative paths are interpreted
    against ``job_dir`` because that is what the task description advertises.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("path 不可為空")
    candidate = Path(raw.strip()).expanduser()
    if not candidate.is_absolute():
        candidate = job_dir / candidate
    resolved = candidate.resolve()

    roots = [job_dir.resolve()] if write else [job_dir.resolve(), *[r.resolve() for r in read_roots]]
    for root in roots:
        if resolved == root or root in resolved.parents:
            return resolved
    allowed = "、".join(str(r) for r in roots)
    raise ValueError(f"路徑不在允許範圍內：{resolved}（允許：{allowed}）")


# --------------------------------------------------------------------------- #
# tool implementations
# --------------------------------------------------------------------------- #
def _tool_read(args: dict, job_dir: Path, read_roots: list[Path]) -> str:
    path = _resolve(args.get("path", ""), job_dir, read_roots, write=False)
    if not path.is_file():
        raise ValueError(f"檔案不存在：{path}")

    offset = args.get("offset")
    offset = 1 if offset in (None, "") else int(offset)
    offset = max(1, offset)
    limit = args.get("limit")
    limit = DEFAULT_LIMIT if limit in (None, "") else int(limit)
    limit = max(1, min(MAX_LIMIT, limit))

    lines = path.read_text(errors="ignore").splitlines()
    total = len(lines)
    chunk = lines[offset - 1: offset - 1 + limit]
    body = "\n".join(chunk)
    truncated = False
    if len(body) > MAX_READ_CHARS:
        body = body[:MAX_READ_CHARS]
        truncated = True

    end = offset - 1 + len(chunk)
    header = f"# {path.name} 第 {offset}-{end} 行（全檔共 {total} 行）"
    if truncated:
        header += "｜內容過長已截斷，請縮小 limit 再讀一次"
    if end < total:
        header += f"｜還有 {total - end} 行未讀，請用 offset={end + 1} 繼續"
    else:
        header += "｜已讀到檔尾"
    return f"{header}\n\n{body}"


def _tool_write(args: dict, job_dir: Path, written: list[str]) -> str:
    """Write or extend one file inside the job folder.

    ``mode="append"`` exists because a model's single reply is bounded but a
    document is not. Forcing every deliverable through one ``write_file`` call
    makes a small model trade completeness for fitting in the response, and the
    observed result is a three-line summary of a two-hour meeting. Appending
    lets it write section by section at full depth.

    Segments are joined with a blank line: the model hands back markdown
    sections, and a heading that lands immediately after the previous
    paragraph stops being a heading in most renderers.
    """
    path = _resolve(args.get("path", ""), job_dir, [], write=True)
    raw_mode = args.get("mode")
    mode = (raw_mode or "overwrite").strip().lower() if isinstance(raw_mode, str) else "overwrite"
    if mode not in ("overwrite", "append"):
        raise ValueError(f"mode 只能是 overwrite 或 append，收到：{raw_mode!r}")

    content = args.get("content")
    if content is None:
        raise ValueError("content 不可為空")
    if not isinstance(content, str):
        # A model that was asked for action_items.json sometimes hands back a
        # decoded object instead of the serialised text. Accept it rather than
        # bouncing the call, since the intent is unambiguous.
        content = json.dumps(content, ensure_ascii=False, indent=2)

    if path.suffix == ".json":
        # A JSON file has exactly one valid shape, so a half-written one is
        # worse than none. Bounce the append and keep the atomic guarantee.
        if mode == "append":
            raise ValueError(
                f"{path.name} 是 JSON 檔，不能用 append 分段寫，請用 overwrite 一次寫完整份。"
            )
        try:
            json.loads(content)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"{path.name} 不是合法 JSON：{exc}｜請修正後重寫") from exc

    path.parent.mkdir(parents=True, exist_ok=True)
    if mode == "append" and path.is_file():
        existing = path.read_text(errors="ignore")
        path.write_text(existing.rstrip("\n") + "\n\n" + content.lstrip("\n"))
        verb = "已附加到"
    else:
        path.write_text(content)
        verb = "已寫入"

    total = len(path.read_text(errors="ignore"))
    if path.name not in written:
        written.append(path.name)

    msg = f"{verb} {path.name}（本次 {len(content):,} 字元，全檔 {total:,} 字元）"
    if path.suffix != ".json":
        msg += (
            f"。若 {path.name} 還沒寫完，請用同一個 path、mode=\"append\" 接著寫下一段；"
            "已經寫好的部分不要重寫。"
        )
    return msg


def _tool_list(job_dir: Path) -> str:
    entries = sorted(p.name for p in job_dir.iterdir() if p.is_file())
    if not entries:
        return "job 資料夾目前沒有檔案。"
    return "job 資料夾現有檔案：\n" + "\n".join(f"- {n}" for n in entries)


def _dispatch(name: str, args: dict, job_dir: Path, read_roots: list[Path],
              written: list[str]) -> tuple[str, bool]:
    """Run one tool call. Returns (result_text, ok)."""
    try:
        if name == "read_file":
            return _tool_read(args, job_dir, read_roots), True
        if name == "write_file":
            return _tool_write(args, job_dir, written), True
        if name == "list_files":
            return _tool_list(job_dir), True
        return f"錯誤：沒有名為 {name} 的工具。可用工具：read_file、write_file、list_files", False
    except Exception as exc:  # noqa: BLE001
        return f"錯誤：{exc}", False


# --------------------------------------------------------------------------- #
# transport
# --------------------------------------------------------------------------- #
def _post(url: str, api_key: str, body: dict, timeout: int) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ToolLoopError(f"推論服務回應 HTTP {exc.code}：{detail[:400]}") from exc
    except Exception as exc:  # noqa: BLE001
        raise ToolLoopError(f"無法連線到推論服務（{url}）：{type(exc).__name__}: {exc}") from exc
    try:
        return json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        raise ToolLoopError(f"推論服務回傳的不是 JSON：{exc}｜開頭內容：{raw[:300]}") from exc


def _parse_tool_calls(msg: dict) -> list[dict]:
    calls = msg.get("tool_calls") or []
    out = []
    for i, call in enumerate(calls):
        fn = call.get("function") or {}
        raw_args = fn.get("arguments")
        if isinstance(raw_args, dict):
            args = raw_args
        else:
            try:
                args = json.loads(raw_args or "{}")
            except Exception:  # noqa: BLE001
                args = {"__parse_error__": str(raw_args)[:200]}
        out.append({
            "id": call.get("id") or f"call_{i}",
            "name": fn.get("name") or "",
            "args": args,
        })
    return out


# --------------------------------------------------------------------------- #
# main loop
# --------------------------------------------------------------------------- #
def run_tool_loop(prompt: str, model: str | None, acfg: dict, *, job_dir: Path,
                  read_roots: list[Path], log_path: Path, timeout: int,
                  max_steps: int = 60, system: str | None = None) -> tuple[int, str, dict]:
    """Drive an OpenAI-compatible model through a read/write tool loop.

    Returns ``(rc, elapsed_seconds_str, summary)``. ``rc`` is 0 when the model
    finished on its own, 124 when it ran out of steps. Transport failures raise
    ``ToolLoopError`` so callers never mistake them for a bad answer.
    """
    api = (acfg.get("api") or {}) if isinstance(acfg, dict) else {}
    base_url = str(api.get("base_url") or "http://127.0.0.1:1234/v1").rstrip("/")
    url = f"{base_url}/chat/completions"
    api_key = str(api.get("api_key") or "lm-studio")
    temp_val = api.get("temperature")
    temperature = float(0.1 if temp_val is None else temp_val)
    max_tokens = int(api.get("max_output_tokens") or 16384)
    model_name = model or str(api.get("model") or "").strip()

    job_dir = Path(job_dir)
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    written: list[str] = []
    steps = 0
    tool_calls_made = 0
    tool_errors = 0
    started = time.time()

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as log:
        log.write(f"\n===== {time.strftime('%F %T')} :: tool_loop =====\n")
        log.write(f"[loop] url={url} model={model_name} max_steps={max_steps} timeout={timeout}s\n")
        log.flush()

        final_text = ""
        while steps < max_steps:
            steps += 1
            body = {
                "model": model_name,
                "messages": messages,
                "tools": TOOLS,
                "tool_choice": "auto",
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            data = _post(url, api_key, body, timeout)
            choice = (data.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            finish = choice.get("finish_reason")
            calls = _parse_tool_calls(msg)
            content = msg.get("content")
            content = content.strip() if isinstance(content, str) else ""

            usage = data.get("usage") or {}
            log.write(
                f"[loop] step {steps}: finish={finish} tool_calls={len(calls)} "
                f"content={len(content)}c prompt_tokens={usage.get('prompt_tokens', '?')}\n"
            )
            log.flush()

            if not calls:
                final_text = content
                log.write(f"[loop] model stopped after {steps} step(s)\n")
                break

            # Echo the assistant turn back verbatim; some servers reject a
            # tool result whose matching call is missing from the history.
            messages.append({
                "role": "assistant",
                "content": msg.get("content") or "",
                "tool_calls": msg.get("tool_calls") or [],
            })

            for call in calls:
                tool_calls_made += 1
                result, ok = _dispatch(call["name"], call["args"], job_dir, read_roots, written)
                if not ok:
                    tool_errors += 1
                preview = call["args"].get("path", "")
                log.write(f"[loop]   -> {call['name']}({preview}) ok={ok} {len(result):,}c\n")
                messages.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": result,
                })
            log.flush()
        else:
            log.write(f"[loop] hit max_steps={max_steps} without the model stopping\n")

        elapsed = f"{time.time() - started:.1f}"
        rc = 0 if steps < max_steps else 124
        log.write(
            f"[loop] done rc={rc} steps={steps} tool_calls={tool_calls_made} "
            f"errors={tool_errors} written={written} elapsed={elapsed}s\n"
        )

    return rc, elapsed, {
        "steps": steps,
        "tool_calls": tool_calls_made,
        "tool_errors": tool_errors,
        "written": written,
        "final_text": final_text,
        "max_steps": max_steps,
    }
