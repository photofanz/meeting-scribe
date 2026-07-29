#!/usr/bin/env python
"""
Interactive setup for the notification layer.

`notify.py` can do four things; getting any of them working means knowing a
bot token, a chat id, an argv template or a webhook secret. Every one of those
is easy to paste wrong and impossible to debug from `config.json` alone — a
transposed character in a chat id just means nothing ever arrives.

So this wizard does the verification the user cannot: it calls `getMe` before
accepting a token, discovers the chat id from `getUpdates` instead of asking
the user to find their own numeric id, checks that a `command` binary exists,
and finishes by sending a *real* notification through the freshly saved config
and reporting what actually came back.

config.json is only ever written through `config.save()`, so this file has no
opinion about the file's layout or its other sections.

    python bin/notify_setup.py
"""
from __future__ import annotations

import json
import os
import secrets
import shlex
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import notify  # noqa: E402
from config import CONFIG, ROOT, save as save_config  # noqa: E402

MODES = ["none", "telegram", "command", "webhook"]
MODE_DESC = {
    "none": "不推播（只用網頁介面看進度）",
    "telegram": "Telegram Bot（直接呼叫 Bot API，不需其他軟體）",
    "command": "外部指令（例如 hermes send）",
    "webhook": "Webhook（POST JSON 到你自己的服務）",
}
ALL_EVENTS = ["transcribed", "scanning", "awaiting_answers", "writing", "done", "error"]
EVENT_DESC = {
    "transcribed": "轉寫完成",
    "scanning": "開始掃描",
    "awaiting_answers": "有問題待回答",
    "writing": "開始撰寫",
    "done": "文件完成",
    "error": "發生錯誤",
}
DEFAULT_EVENTS = ["awaiting_answers", "done", "error"]
DEFAULT_COMMAND = "~/.local/bin/hermes send --to telegram {message}"

API = "https://api.telegram.org/bot{token}/{method}"


# ------------------------------------------------------------------- input --
def ask(prompt: str, default: str = "") -> str:
    hint = f" [{default}]" if default else ""
    try:
        val = input(f"{prompt}{hint}: ").strip()
    except EOFError:
        # stdin closed mid-run: fall back to the default rather than crashing
        # with a traceback the user cannot act on.
        print()
        return default
    return val or default


def ask_yes(prompt: str, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    val = ask(f"{prompt} ({d})").lower()
    if not val:
        return default
    return val in ("y", "yes", "是", "1")


def rule(title: str = "") -> None:
    print("\n" + "─" * 56)
    if title:
        print(title)
        print("─" * 56)


# --------------------------------------------------------------- telegram ----
def tg_call(token: str, method: str, params: dict | None = None, timeout: int = 20) -> dict:
    """Bot API GET. Returns the decoded body even for HTTP errors."""
    url = API.format(token=token, method=method)
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads((exc.read() or b"").decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            return {"ok": False, "description": f"HTTP {exc.code}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "description": f"{type(exc).__name__}: {exc}"}


def setup_telegram(cur: dict) -> dict | None:
    print("""
建立 Telegram Bot：
  1. 在 Telegram 搜尋 @BotFather 並開始對話
  2. 輸入 /newbot，依指示取名（名稱可中文，帳號需以 bot 結尾）
  3. BotFather 會回覆一串 token，長得像 123456789:AAE...，把它貼過來
""".rstrip())
    token = ""
    for attempt in range(1, 6):
        token = ask("Bot token", cur.get("bot_token", "")).strip()
        if not token:
            print("  ✗ 未輸入 token。")
            continue
        print("  … 驗證中")
        me = tg_call(token, "getMe")
        if me.get("ok"):
            uname = (me.get("result") or {}).get("username", "?")
            print(f"  ✓ token 有效，Bot 是 @{uname}")
            break
        print(f"  ✗ token 無效：{me.get('description', '未知錯誤')}")
        if attempt >= 5 or not ask_yes("要再試一次嗎？"):
            return None
    else:
        return None

    chat_id = discover_chat_id(token) or ask(
        "無法自動偵測，請手動輸入 chat_id", str(cur.get("chat_id", ""))).strip()
    if not chat_id:
        print("  ✗ 沒有 chat_id，無法送出通知。")
        return None
    return {"bot_token": token, "chat_id": str(chat_id)}


def discover_chat_id(token: str) -> str | None:
    """Read the chat id off a message the user just sent to the bot.

    Rejected alternative: telling the user to run @userinfobot. It works, but
    it is one more bot to trust and it does not prove *this* bot can see the
    chat — which is the failure we are actually trying to catch.
    """
    uname = (tg_call(token, "getMe").get("result") or {}).get("username", "your_bot")
    for attempt in range(1, 4):
        print(f"\n請現在到 Telegram 對 @{uname} 傳送任意訊息（例如「hi」）。")
        ask("完成後按 Enter 繼續")
        # offset=-1 -> only the newest update, so a stale queue cannot hand us
        # a chat id from some earlier experiment.
        upd = tg_call(token, "getUpdates", {"offset": -1, "limit": 1, "timeout": 0})
        chats = []
        for item in (upd.get("result") or []):
            msg = item.get("message") or item.get("channel_post") or {}
            chat = msg.get("chat") or {}
            if chat.get("id") is not None:
                chats.append(chat)
        if chats:
            chat = chats[-1]
            name = (chat.get("title")
                    or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
                    or chat.get("username") or "(無名稱)")
            print(f"  ✓ 偵測到：{name}  chat_id = {chat['id']}  ({chat.get('type')})")
            if ask_yes("要用這個對話接收通知嗎？"):
                return str(chat["id"])
            continue
        print(f"  ✗ 沒有收到訊息（第 {attempt}/3 次）。常見原因：")
        print("     · 還沒對 Bot 送出訊息 —— 必須「先傳訊息」才收得到")
        print("     · Bot 在群組裡但 privacy mode 開啟：BotFather → /setprivacy → Disable")
        print("     · 有其他程式正在讀取同一個 Bot 的 updates")
        if attempt < 3 and not ask_yes("要再試一次嗎？"):
            break
    return None


# ---------------------------------------------------------------- command ----
def setup_command(cur: str) -> str | None:
    print("""
外部指令模式：整段字串會先以 shell 詞法切開，再把 {message} / {files}
填進「已切好的參數」裡（不會經過 shell，訊息不可能被當成指令執行）。
  {message}  完整通知內文
  {files}    每個附件一行 MEDIA:/abs/path
""".rstrip())
    template = ask("指令樣板", cur or DEFAULT_COMMAND).strip()
    if not template:
        return None
    try:
        argv = notify.build_argv(template, "測試訊息", [])
    except ValueError as exc:
        print(f"  ✗ 樣板無法解析（引號不成對？）：{exc}")
        return None
    if not argv:
        print("  ✗ 樣板是空的。")
        return None
    exe = Path(argv[0])
    found = exe if exe.is_absolute() else Path(shutil.which(argv[0]) or "")
    if not str(found) or not found.exists():
        print(f"  ⚠️ 找不到執行檔：{argv[0]}（仍會存檔，但通知會失敗）")
    elif not found.is_file() or not os.access(found, os.X_OK):
        print(f"  ⚠️ {found} 沒有執行權限（仍會存檔，但通知會失敗）")
    else:
        print(f"  ✓ 執行檔存在：{found}")
    print("  實際會執行：" + " ".join(shlex.quote(a) for a in argv))
    return template


# ---------------------------------------------------------------- webhook ----
def setup_webhook(cur: dict) -> dict | None:
    print("""
Webhook 模式：以 POST 送出 JSON
  {event, job_id, title, body, files, url, timestamp}
若設定 secret，會附上 X-MeetingScribe-Signature: sha256=<HMAC-SHA256(body)>，
你的服務應以同一組 secret 驗簽後才處理。
""".rstrip())
    url = ""
    for _ in range(5):
        url = ask("Webhook URL", cur.get("url", "")).strip()
        if url.startswith(("http://", "https://")):
            break
        print("  ✗ 必須以 http:// 或 https:// 開頭。")
        url = ""
    if not url:
        return None
    secret = cur.get("secret", "")
    if ask_yes("要設定簽章 secret 嗎？", default=True):
        if ask_yes("要自動產生一組嗎？", default=not secret):
            secret = secrets.token_hex(32)
            print(f"  ✓ 已產生（請複製到你的服務端）：\n     {secret}")
        else:
            secret = ask("Secret", secret).strip()
    else:
        secret = ""
    return {"url": url, "secret": secret}


# ----------------------------------------------------------------- events ----
def setup_events(cur: list[str]) -> list[str]:
    rule("要在哪些時候收到通知？")
    for i, ev in enumerate(ALL_EVENTS, 1):
        mark = "✓" if ev in cur else " "
        print(f"  {i}. [{mark}] {ev:<17} {EVENT_DESC[ev]}")
    print("\n輸入編號（可用逗號分隔，例如 3,5,6），直接按 Enter 保持現狀。")
    print("注意：error 一律會送出，不受此設定影響。")
    raw = ask("選擇", "")
    if not raw:
        return cur or list(DEFAULT_EVENTS)
    picked = []
    for tok in raw.replace("、", ",").replace(" ", ",").split(","):
        tok = tok.strip()
        if not tok:
            continue
        if tok.isdigit() and 1 <= int(tok) <= len(ALL_EVENTS):
            picked.append(ALL_EVENTS[int(tok) - 1])
        elif tok in ALL_EVENTS:
            picked.append(tok)
        else:
            print(f"  ⚠️ 忽略無法辨識的項目：{tok}")
    if not picked:
        print("  ⚠️ 沒有有效選擇，沿用預設。")
        return list(DEFAULT_EVENTS)
    return sorted(set(picked), key=ALL_EVENTS.index)


# ------------------------------------------------------------------- main ----
def main() -> int:
    if not sys.stdin.isatty():
        print("bin/notify_setup.py 需要在互動式終端機執行（會逐項詢問設定）。\n"
              "請在終端機直接輸入：\n"
              f"    {sys.executable} {Path(__file__).resolve()}\n"
              "若要在腳本中送出通知，請改用 bin/notify.py。", file=sys.stderr)
        return 1

    cfg = json.loads(json.dumps(CONFIG))  # deep copy: never mutate the shared dict
    n = cfg.setdefault("notify", {})
    cur_mode = n.get("mode", "none")

    rule("Meeting Scribe 通知設定")
    print(f"設定檔：{ROOT / 'config.json'}")
    print(f"目前模式：{cur_mode}（{MODE_DESC.get(cur_mode, '?')}）\n")
    for i, m in enumerate(MODES, 1):
        cursor = "←目前" if m == cur_mode else ""
        print(f"  {i}. {m:<9} {MODE_DESC[m]} {cursor}")
    choice = ask("\n選擇模式（1-4，Enter 保持現狀）", "")
    mode = MODES[int(choice) - 1] if choice.isdigit() and 1 <= int(choice) <= 4 else cur_mode

    if mode == "telegram":
        rule("Telegram Bot")
        tg = setup_telegram(n.get("telegram") or {})
        if not tg:
            print("\n已取消，設定未變更。")
            return 1
        n["telegram"] = tg
    elif mode == "command":
        rule("外部指令")
        template = setup_command(n.get("command") or "")
        if not template:
            print("\n已取消，設定未變更。")
            return 1
        n["command"] = template
    elif mode == "webhook":
        rule("Webhook")
        wh = setup_webhook(n.get("webhook") or {})
        if not wh:
            print("\n已取消，設定未變更。")
            return 1
        n["webhook"] = wh

    n["mode"] = mode
    n["events"] = setup_events(n.get("events") or list(DEFAULT_EVENTS))
    # The legacy trio is what config._migrate_notify() reads; clearing `enabled`
    # stops it from ever overriding the mode we just chose.
    n["enabled"] = None

    save_config(cfg)
    rule("已儲存")
    print(f"  mode   : {n['mode']}")
    print(f"  events : {', '.join(n['events'])}")

    if mode == "none":
        print("\n模式為 none，不會實際推播（訊息仍會寫入 logs/undelivered.log）。")
        return 0

    print("\n正在送出測試通知…")
    res = notify.test()
    if res.get("ok") and not res.get("skipped"):
        print(f"  ✓ 送出成功（mode={res['mode']}）{('：' + res['detail']) if res.get('detail') else ''}")
        print("  請確認你剛剛收到了一則測試訊息。")
        return 0
    print(f"  ✗ 送出失敗（mode={res.get('mode')}）：{res.get('detail') or res.get('reason')}")
    print(f"  訊息已保留在 {ROOT / 'logs' / 'undelivered.log'}")
    print("  設定已寫入 config.json，修正後可重新執行本程式。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
