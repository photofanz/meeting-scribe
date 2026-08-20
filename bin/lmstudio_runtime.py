#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jobstate  # noqa: E402
import config as config_mod  # noqa: E402
from config import ROOT  # noqa: E402

RUNTIME_DIR = ROOT / ".runtime"
ACTIVITY_FILE = RUNTIME_DIR / "lmstudio_last_used.json"
LOG_FILE = ROOT / "logs" / "lmstudio-cleanup.log"
ACTIVE_STATES = {"scanning", "writing"}

# A capability verdict can pass and still be worth saying out loud. Callers key
# off this prefix instead of re-deriving the judgement from the model metadata.
CAPABILITY_WARN = "⚠️ "

# The writer feeds a whole cleaned transcript (a two-hour meeting runs ~21k
# 中文 chars, and Chinese is roughly one token per char) plus the note spec,
# and then needs room for its own answer. 32k is the smallest window where that
# fits without silent truncation — below it the model drops the tail of the
# transcript and nobody finds out until the note is short.
WRITER_MIN_CTX = 32768


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def _atomic_json(path: Path, payload: dict) -> None:
    """Write JSON atomically, safely, from several threads at once.

    A fixed `.tmp` name is only atomic against a reader. Two writers race each
    other: both create the same temp file, the first `os.replace` consumes it,
    and the second raises FileNotFoundError on a path it just wrote. That is
    not hypothetical — the evidence pipeline runs its chunks in parallel and
    every one of them touches the activity file, so a whole stage died on
    bookkeeping. The temp name is per-thread; the replace stays atomic.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _log(msg: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(f"[{time.strftime('%F %T')}] {msg}\n")


def cleanup_cfg(acfg: dict | None = None) -> dict:
    defaults = {
        "mode": "idle_eject",  # keep_loaded | idle_eject | after_job
        "idle_minutes": 15,
        "unload_endpoint": "/api/v1/models/unload",
        "allow_unload_all": False,
    }
    live_cfg = config_mod.load()
    cfg = _merge(defaults, (live_cfg.get("agent") or {}).get("private_cleanup") or {})
    if isinstance(acfg, dict):
        cfg = _merge(cfg, acfg.get("private_cleanup") or {})
    mode = str(cfg.get("mode") or "idle_eject").strip().lower()
    if mode not in {"keep_loaded", "idle_eject", "after_job"}:
        mode = "idle_eject"
    try:
        idle = max(1, int(cfg.get("idle_minutes") or 15))
    except Exception:
        idle = 15
    cfg["mode"] = mode
    cfg["idle_minutes"] = idle
    cfg["unload_endpoint"] = str(cfg.get("unload_endpoint") or "/api/v1/models/unload")
    cfg["allow_unload_all"] = bool(cfg.get("allow_unload_all"))
    return cfg


def _private_acfg() -> dict:
    return config_mod.resolve_agent_config({"agent_preset": "private"}, cfg=config_mod.load())


def _api(acfg: dict | None = None) -> dict:
    acfg = acfg or _private_acfg()
    return (acfg.get("api") or {}) if isinstance(acfg, dict) else {}


def _base_url(acfg: dict | None = None) -> str:
    return str(_api(acfg).get("base_url") or "http://127.0.0.1:1234/v1").rstrip("/")


def _api_root(acfg: dict | None = None) -> str:
    base = _base_url(acfg)
    return base[:-3] if base.endswith("/v1") else base


def _auth_headers(acfg: dict | None = None) -> dict:
    api_key = str(_api(acfg).get("api_key") or "lm-studio")
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def touch_activity(acfg: dict | None = None, *, job_id: str = "", stage: str = "", event: str = "request") -> dict:
    acfg = acfg or _private_acfg()
    payload = {
        "ts": time.time(),
        "job_id": job_id,
        "stage": stage,
        "event": event,
        "backend": acfg.get("backend"),
        "model": acfg.get("model"),
        "base_url": _base_url(acfg),
    }
    _atomic_json(ACTIVITY_FILE, payload)
    return payload


def read_activity() -> dict:
    if not ACTIVITY_FILE.exists():
        return {}
    try:
        return json.loads(ACTIVITY_FILE.read_text())
    except Exception:
        return {}


def _loaded_model_ids(acfg: dict | None = None) -> list[str]:
    ids: list[str] = []
    for item in list_models(acfg):
        for inst in item.get("loaded_instances") or []:
            if isinstance(inst, dict):
                val = str(inst.get("id") or item.get("key") or "").strip()
            else:
                val = str(inst or item.get("key") or "").strip()
            if val and val not in ids:
                ids.append(val)
    return ids


def _model_instances(model_key: str, acfg: dict | None = None) -> list[str]:
    key = str(model_key or "").strip()
    if not key:
        return []
    ids: list[str] = []
    for item in list_models(acfg):
        if str(item.get("key") or "").strip() != key:
            continue
        for inst in item.get("loaded_instances") or []:
            if isinstance(inst, dict):
                val = str(inst.get("id") or key).strip()
            else:
                val = str(inst or key).strip()
            if val and val not in ids:
                ids.append(val)
    return ids


def list_models(acfg: dict | None = None) -> list[dict]:
    root = _api_root(acfg)
    req = urllib.request.Request(root + "/api/v1/models", headers=_auth_headers(acfg))
    with urllib.request.urlopen(req, timeout=15) as r:
        payload = json.loads(r.read().decode("utf-8"))
    models = payload.get("models") or []
    out: list[dict] = []
    for item in models:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "llm") != "llm":
            continue
        loaded_instances = item.get("loaded_instances") or []
        out.append({
            "key": str(item.get("key") or "").strip(),
            "display_name": str(item.get("display_name") or item.get("key") or "").strip(),
            "publisher": str(item.get("publisher") or "").strip(),
            "params": str(item.get("params_string") or "").strip(),
            "architecture": str(item.get("architecture") or "").strip(),
            "format": str(item.get("format") or "").strip(),
            "max_context_length": int(item.get("max_context_length") or 0),
            "vision": bool(((item.get("capabilities") or {}).get("vision"))),
            "tool_use": bool(((item.get("capabilities") or {}).get("trained_for_tool_use"))),
            "loaded": bool(loaded_instances),
            "loaded_instances": loaded_instances,
        })
    return out


def load_model(model_key: str, acfg: dict | None = None) -> dict:
    model_key = str(model_key or "").strip()
    if not model_key:
        raise ValueError("missing model key")
    root = _api_root(acfg)
    payload = json.dumps({"model": model_key}).encode("utf-8")
    req = urllib.request.Request(root + "/api/v1/models/load", data=payload,
                                 headers=_auth_headers(acfg), method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        body = json.loads(r.read().decode("utf-8") or "{}")
    _log(f"loaded model={model_key} instance_id={body.get('instance_id') or ''}".strip())
    return body


def _alternatives(models: list[dict], *, need_tools: bool, need_ctx: int,
                  exclude: str = "") -> list[str]:
    """Models on the *same* machine that would actually do the job.

    A rejection that only says "this model cannot" leaves the operator to go
    read LM Studio's model list themselves. The whole point of failing in
    seconds is that the next thing they type is already in the message.
    """
    ok = [m for m in models
          if m["key"] != exclude
          and (m.get("tool_use") or not need_tools)
          and int(m.get("max_context_length") or 0) >= need_ctx]
    # mlx before gguf (measurably faster on Apple silicon), loaded before cold,
    # then widest context — so the first name offered is the cheapest to adopt.
    ok.sort(key=lambda m: (m.get("format") == "gguf", not m.get("loaded"),
                           -int(m.get("max_context_length") or 0)))
    return [m["key"] for m in ok[:3]]


def capability_check(model_key: str, *, need_tools: bool, need_ctx: int,
                     acfg: dict | None = None) -> tuple[bool, str]:
    """Verify the model can do the job before an hour of work is spent.

    ``preflight()`` answers "is it loadable"; this answers "is it the right
    tool", which is the question the private-mode post-mortem actually turned
    on. A 57 GB BF16 GGUF with ``trained_for_tool_use: false`` loads perfectly
    and then spends 2,572 seconds producing a 1,272-character note, because it
    was handed a 60-step tool loop it was never trained for. Metadata LM Studio
    already publishes says so up front, for the cost of one HTTP GET.

    Returns (ok, reason). A reason prefixed with ``CAPABILITY_WARN`` passes but
    is worth printing: format is a speed problem, not a correctness one, so it
    never blocks a job the operator deliberately configured.
    """
    model_key = str(model_key or "").strip()
    if not model_key:
        return False, "保密模式未設定模型（agent.profiles.private.model 是空的）"

    try:
        models = list_models(acfg)
    except urllib.error.HTTPError as exc:
        return False, f"LM Studio 回應 HTTP {exc.code}，無法取得模型清單"
    except Exception as exc:  # noqa: BLE001
        return False, (f"連不上 LM Studio（{_api_root(acfg)}）："
                       f"{type(exc).__name__}: {exc}")

    match = next((m for m in models if m["key"] == model_key), None)
    if match is None:
        available = ", ".join(m["key"] for m in models[:8]) or "（清單是空的）"
        return False, f"LM Studio 上找不到模型 {model_key}。目前可用：{available}"

    alts = _alternatives(models, need_tools=need_tools, need_ctx=need_ctx,
                         exclude=model_key)
    hint = ("；同一台機器上可改用：" + "、".join(alts)) if alts else ""

    if need_tools and not match.get("tool_use"):
        return False, (f"{model_key} 未受工具呼叫訓練（trained_for_tool_use=false），"
                       f"這條路徑需要模型自己呼叫 read_file／write_file{hint}")

    ctx = int(match.get("max_context_length") or 0)
    if ctx < need_ctx:
        return False, (f"{model_key} 的 context 只有 {ctx:,} token，"
                       f"這條路徑至少需要 {need_ctx:,}（逐字稿會被無聲截斷）{hint}")

    if str(match.get("format") or "").lower() == "gguf":
        return True, (f"{CAPABILITY_WARN}{model_key} 是 gguf 格式，"
                      f"在 Apple silicon 上明顯慢於 mlx{hint}")

    return True, (f"{model_key} 能力檢查通過"
                  f"（{match.get('format') or '?'} / {ctx:,} ctx / "
                  f"tools={'✓' if match.get('tool_use') else '✗'}）")


def preflight(model_key: str, acfg: dict | None = None) -> tuple[bool, str]:
    """Check a private-mode model can actually serve before a long stage starts.

    Returns (ok, reason). Failures are reported in LM Studio's own words so the
    caller never has to guess whether the service was down, the model was
    missing, or the machine simply lacked the memory to load it.
    """
    model_key = str(model_key or "").strip()
    if not model_key:
        return False, "保密模式未設定模型（agent.profiles.private.model 是空的）"

    try:
        models = list_models(acfg)
    except urllib.error.HTTPError as exc:
        return False, f"LM Studio 回應 HTTP {exc.code}，無法取得模型清單"
    except Exception as exc:  # noqa: BLE001
        return False, (f"連不上 LM Studio（{_api_root(acfg)}）："
                       f"{type(exc).__name__}: {exc}")

    match = next((m for m in models if m["key"] == model_key), None)
    if match is None:
        available = ", ".join(m["key"] for m in models[:8]) or "（清單是空的）"
        return False, (f"LM Studio 上找不到模型 {model_key}。"
                       f"目前可用：{available}")

    if match.get("loaded"):
        return True, f"{model_key} 已載入"

    try:
        load_model(model_key, acfg)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        detail = raw.strip()
        try:
            body = json.loads(raw)
            detail = str((body.get("error") or {}).get("message") or raw).strip()
        except Exception:  # noqa: BLE001
            pass
        return False, f"模型 {model_key} 載入失敗（HTTP {exc.code}）：{detail}"
    except Exception as exc:  # noqa: BLE001
        return False, f"模型 {model_key} 載入失敗：{type(exc).__name__}: {exc}"

    return True, f"{model_key} 載入成功"


def _active_private_jobs() -> list[str]:
    out: list[str] = []
    archive = ROOT / "archive"
    if not archive.exists():
        return out
    for d in archive.iterdir():
        if not d.is_dir():
            continue
        meta_path = d / "meta.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            continue
        if str(meta.get("agent_preset") or "general") != "private":
            continue
        try:
            state = (jobstate.load(d) or {}).get("state")
        except Exception:
            state = None
        if state in ACTIVE_STATES:
            out.append(d.name)
    return out


def unload_instance(instance_id: str, acfg: dict | None = None, *, reason: str = "") -> dict:
    """Unload one specific loaded instance, whoever loaded it.

    ``unload_now`` deliberately only touches the configured private-mode model,
    which deadlocks when a foreign workload is holding the memory we need. This
    is the explicit, operator-confirmed escape hatch for that case.
    """
    instance_id = str(instance_id or "").strip()
    if not instance_id:
        raise ValueError("missing instance_id")
    acfg = acfg or _private_acfg()
    cfg = cleanup_cfg(acfg)
    root = _api_root(acfg)
    payload = json.dumps({"instance_id": instance_id}).encode("utf-8")
    req = urllib.request.Request(root + cfg["unload_endpoint"], data=payload,
                                 headers=_auth_headers(acfg), method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        body = json.loads(r.read().decode("utf-8") or "{}")
    _log(f"force unloaded instance_id={body.get('instance_id') or instance_id} reason={reason}")
    return {"ok": True, "instance_id": body.get("instance_id") or instance_id, "reason": reason}


def unload_now(acfg: dict | None = None, *, reason: str = "") -> dict:
    acfg = acfg or _private_acfg()
    cfg = cleanup_cfg(acfg)
    root = _api_root(acfg)
    endpoint = cfg["unload_endpoint"]
    configured_model = str(acfg.get("model") or "").strip()
    target_instances = _model_instances(configured_model, acfg)

    candidates: list[str] = []
    if target_instances:
        candidates.extend(target_instances)
    elif configured_model and not _loaded_model_ids(acfg):
        candidates.append(configured_model)

    last_err = "configured model is not currently loaded"
    for instance_id in candidates:
        payload = json.dumps({"instance_id": instance_id}).encode("utf-8")
        req = urllib.request.Request(root + endpoint, data=payload,
                                     headers=_auth_headers(acfg), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read().decode("utf-8") or "{}"
                body = json.loads(raw)
                msg = f"unloaded instance_id={body.get('instance_id') or instance_id} reason={reason}".strip()
                _log(msg)
                return {"ok": True, "instance_id": body.get("instance_id") or instance_id, "reason": reason}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            last_err = f"{instance_id}: HTTP {exc.code} {raw[:240]}"
        except Exception as exc:  # noqa: BLE001
            last_err = f"{instance_id}: {type(exc).__name__}: {exc}"

    loaded = _loaded_model_ids(acfg)
    if cfg.get("allow_unload_all") and len(loaded) == 1 and configured_model and configured_model == loaded[0]:
        instance_id = loaded[0]
        payload = json.dumps({"instance_id": instance_id}).encode("utf-8")
        req = urllib.request.Request(root + endpoint, data=payload,
                                     headers=_auth_headers(acfg), method="POST")
        with urllib.request.urlopen(req, timeout=20) as r:
            body = json.loads(r.read().decode("utf-8") or "{}")
        _log(f"fallback unloaded sole loaded model instance_id={body.get('instance_id') or instance_id} reason={reason}")
        return {"ok": True, "instance_id": body.get("instance_id") or instance_id, "reason": reason}

    raise RuntimeError(last_err)


def schedule_cleanup(acfg: dict | None = None, *, job_id: str = "", stage: str = "") -> dict:
    acfg = acfg or _private_acfg()
    cfg = cleanup_cfg(acfg)
    mark = touch_activity(acfg, job_id=job_id, stage=stage, event="finished")
    if str(acfg.get("backend") or "") != "openai_compat":
        return {"scheduled": False, "mode": cfg["mode"], "reason": "backend_not_openai_compat"}
    if cfg["mode"] == "keep_loaded":
        _log(f"skip cleanup mode=keep_loaded job={job_id} stage={stage}")
        return {"scheduled": False, "mode": cfg["mode"]}
    if cfg["mode"] == "after_job":
        return unload_now(acfg, reason=f"after_job:{job_id}:{stage}")

    cmd = [sys.executable, str(Path(__file__).resolve()), "wait-and-clean",
           "--ref-ts", str(mark["ts"]), "--job-id", job_id, "--stage", stage]
    with LOG_FILE.open("ab") as log:
        log.write(f"[{time.strftime('%F %T')}] spawn idle cleanup ref_ts={mark['ts']} job={job_id} stage={stage}\n".encode())
        log.flush()
        subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                         start_new_session=True, cwd=str(ROOT))
    return {"scheduled": True, "mode": cfg["mode"], "ref_ts": mark["ts"]}


def wait_and_clean(ref_ts: float, *, job_id: str = "", stage: str = "") -> int:
    acfg = _private_acfg()
    cfg = cleanup_cfg(acfg)
    wait_sec = int(cfg["idle_minutes"]) * 60
    _log(f"idle wait start ref_ts={ref_ts} wait_sec={wait_sec} job={job_id} stage={stage}")
    time.sleep(wait_sec)
    latest = read_activity()
    latest_ts = float(latest.get("ts") or 0)
    if latest_ts > float(ref_ts) + 1e-6:
        _log(f"idle cleanup skipped due to newer activity latest_ts={latest_ts} ref_ts={ref_ts}")
        return 0
    active = _active_private_jobs()
    if active:
        _log(f"idle cleanup skipped because private jobs still active: {', '.join(active)}")
        return 0
    try:
        unload_now(acfg, reason=f"idle_eject:{job_id}:{stage}")
        return 0
    except Exception as exc:  # noqa: BLE001
        _log(f"idle cleanup failed: {type(exc).__name__}: {exc}")
        return 1


def status(acfg: dict | None = None) -> dict:
    acfg = acfg or _private_acfg()
    cfg = cleanup_cfg(acfg)
    activity = read_activity()
    active = _active_private_jobs()
    loaded: list[str] = []
    models: list[dict] = []
    load_error = ""
    list_error = ""
    try:
        models = list_models(acfg)
        loaded = _loaded_model_ids(acfg)
    except Exception as exc:  # noqa: BLE001
        msg = f"{type(exc).__name__}: {exc}"
        load_error = msg
        list_error = msg
    selected = str(acfg.get("model") or "").strip()
    selected_instances: list[str] = []
    if selected:
        selected_instances = [mid for mid in loaded if mid == selected]
        if not selected_instances:
            try:
                selected_instances = _model_instances(selected, acfg)
            except Exception:
                selected_instances = []
    foreign_loaded = [mid for mid in loaded if mid not in selected_instances]
    can_unload_now = bool(selected_instances) and not active
    return {
        "backend": acfg.get("backend"),
        "model": selected,
        "base_url": _base_url(acfg),
        "cleanup": cfg,
        "activity": activity,
        "loaded_models": loaded,
        "loaded_count": len(loaded),
        "available_models": models,
        "available_count": len(models),
        "selected_model_loaded": bool(selected_instances),
        "selected_model_instances": selected_instances,
        "foreign_loaded_models": foreign_loaded,
        "foreign_loaded_count": len(foreign_loaded),
        "active_private_jobs": active,
        "active_count": len(active),
        "can_unload_now": can_unload_now,
        "load_error": load_error,
        "list_error": list_error,
        "log_path": str(LOG_FILE),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="LM Studio runtime helpers for meeting jobs")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_touch = sub.add_parser("touch")
    p_touch.add_argument("--job-id", default="")
    p_touch.add_argument("--stage", default="")
    p_touch.add_argument("--event", default="request")

    p_sched = sub.add_parser("schedule")
    p_sched.add_argument("--job-id", default="")
    p_sched.add_argument("--stage", default="")

    p_wait = sub.add_parser("wait-and-clean")
    p_wait.add_argument("--ref-ts", type=float, required=True)
    p_wait.add_argument("--job-id", default="")
    p_wait.add_argument("--stage", default="")

    p_unload = sub.add_parser("unload-now")
    p_unload.add_argument("--reason", default="manual")

    args = ap.parse_args()
    if args.cmd == "touch":
        print(json.dumps(touch_activity(job_id=args.job_id, stage=args.stage, event=args.event), ensure_ascii=False))
        return 0
    if args.cmd == "schedule":
        print(json.dumps(schedule_cleanup(job_id=args.job_id, stage=args.stage), ensure_ascii=False))
        return 0
    if args.cmd == "wait-and-clean":
        return wait_and_clean(args.ref_ts, job_id=args.job_id, stage=args.stage)
    if args.cmd == "unload-now":
        print(json.dumps(unload_now(reason=args.reason), ensure_ascii=False))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
