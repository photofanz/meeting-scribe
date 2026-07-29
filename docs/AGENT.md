# 接上 AI Agent

轉寫管線本身不需要 agent —— 跑完你就有 `transcript.md` 了。
Agent 負責的是**後半段**：清稿、寫會議記錄、抽待辦、歸檔。

有兩種接法，選一種就好：

| | **A. 對話 agent（manual）** | **B. 本機 CLI（auto）** |
|---|---|---|
| 誰來寫 | Hermes / Claude Code 之類的對話 agent | `claude` 或 `codex` CLI，背景執行 |
| 觸發 | 你回一句「整理這場會議」 | 轉寫完成後自動接上 |
| 需要你在場 | 要 | **不用** |
| 中途能追問 | 能 | 不能（禁則已寫死在 prompt 裡） |
| 適合 | 想邊看邊改、要客製版本 | 每天固定跑、只想收檔案 |

預設是 A。改成 B 只要改 `config.json` 一個欄位。

---

## B. 本機 CLI（無人值守）

### 需要什麼

一個已登入的 coding-agent CLI，二選一：

```bash
npm i -g @anthropic-ai/claude-code   # -> claude
npm i -g @openai/codex               # -> codex
```

先確認它在非互動模式下真的能寫檔（登入沒過的話會靜靜失敗）：

```bash
claude -p "write hello.txt containing hi" --permission-mode acceptEdits --allowedTools "Read,Write"
codex exec --sandbox workspace-write --skip-git-repo-check "write hello.txt containing hi"
```

### 開啟

`config.json`：

```json
"agent": {
  "mode": "auto",
  "backend": "claude",
  "bin": null,
  "model": null,
  "timeout_sec": 3600
}
```

| 欄位 | 說明 |
|---|---|
| `mode` | `manual`（預設，等人）／ `auto`（轉寫完自動接上） |
| `backend` | `claude` ／ `codex` |
| `bin` | CLI 完整路徑。`null` = 從 PATH 找。launchd 的 PATH 很窄，找不到就填絕對路徑 |
| `model` | 傳給 CLI 的 model 參數。`null` = 用它自己的預設 |
| `timeout_sec` | 逾時上限。兩小時會議約需 5–15 分鐘，預設 3600 秒夠用 |

改完 `bin/service.sh restart`。

### 手動跑一個 job

不必等新錄音，任何已完成轉寫的 job 都能重跑：

```bash
.venv/bin/python bin/agent_note.py archive/<job_id>            # 寫檔，不推播
.venv/bin/python bin/agent_note.py latest --deliver            # 最新的 job，寫完直接傳給你
.venv/bin/python bin/agent_note.py latest --backend codex      # 臨時換一個 backend
.venv/bin/python bin/agent_note.py latest --dry-run            # 只印出 prompt，不執行
```

### 它實際做了什麼

```
transcript.md
     │
     ├─ agent_note.py 依 meta.json 算出「這個 job 該產哪些檔」
     ├─ 套用 templates/AGENT_TASK.md，把規格與禁則組成一段 prompt
     ├─ 呼叫 CLI（cwd = repo 根目錄，只給 Read/Write/Edit/Glob/Grep，不給 shell）
     │      └─ CLI 讀 NOTE_SPECS.md + transcript.md，寫出 .md 與 action_items.json
     ├─ 對照計畫逐檔驗收 —— 缺什麼就記什麼，不看 CLI 的 exit code
     ├─ 依 formats 跑 make_pdf.py / make_docx.py（這段是 Python，不交給 LLM）
     └─ 寫 delivery.json；--deliver 時用 MEDIA: 把檔案推給你
```

**刻意的分工**：LLM 只寫 markdown，格式轉換與交付一律由 Python 做。
CLI 可能 exit 0 卻什麼都沒寫，也可能 exit 非 0 卻寫得好好的——所以驗收只看檔案，
不看 exit code。缺檔會列進 `delivery.json` 的 `missing`，並在推播訊息裡標出來。

### 兩個 prompt 檔

| 檔案 | 內容 | 何時改 |
|---|---|---|
| `templates/NOTE_SPECS.md` | 文件規格：要產哪些檔、每份長什麼樣 | 想改會議記錄的結構 |
| `templates/AGENT_TASK.md` | 給 CLI 的工作說明與禁則 | 想調 agent 的行為、加禁則 |

兩份都進版控，`git pull` 就會跟著走，不用每台機器重設。

### 出問題時

```bash
tail -100 logs/<job_id>-agent.log      # CLI 的完整輸出
cat archive/<job_id>/delivery.json     # 產了什麼、缺了什麼
cat archive/<job_id>/agent_report.json # agent 自己標的不確定處
```

| 症狀 | 原因 |
|---|---|
| exit 0 但一個檔都沒有 | CLI 沒登入。手動跑一次上面的 hello.txt 驗證 |
| `'claude' not found on PATH` | launchd 的 PATH 太窄 → 在 `agent.bin` 填絕對路徑 |
| exit 124 | 逾時。調大 `timeout_sec` |
| 產出簡體字或亂編內容 | 改 `templates/AGENT_TASK.md` 的禁則，重跑該 job |

---

## A. 對話 agent（預設）

### 觸發流程

```
轉寫完成
   └─ bin/run_job.sh 呼叫 <notify.bin> send --to <target> "<訊息>"
        訊息內含 job id，例如 2026-07-29_宏大科技_07211d
             │
             ▼
   你回一句「整理這場會議」
             │
             ▼
   Agent 依下面的合約處理
```

如果你的 agent 沒有推播能力，把 `config.json` 的 `notify.enabled` 設為 `false`，
再直接跟 agent 說「處理 archive 裡最新的 job」即可。

### Agent 合約

把下面這段放進 agent 的 system prompt / skill / `CLAUDE.md`，並把
`<REPO>` 換成實際路徑：

```markdown
## 會議處理

當使用者說「整理這場會議」或指定某個 job id 時：

1. 找到 job 資料夾：`<REPO>/archive/<job_id>/`
   （沒指定 job id 就取 `archive/` 中 mtime 最新的那個）
2. 讀 `<REPO>/templates/NOTE_SPECS.md` —— **那份是規格，一切以它為準**
3. 讀 job 的 `meta.json`，依 `want_transcript` / `want_note` /
   `meeting_type` / `formats` 四個欄位決定要產出什麼
4. 讀 `transcript.md`，照 NOTE_SPECS 的規格處理
5. 依 `formats` 呼叫 `bin/make_pdf.py` / `bin/make_docx.py`
6. 寫 `INDEX.md`，把使用者勾選的檔案交付給他

**禁則**（NOTE_SPECS 有完整版，這裡是最重要的三條）：
- 不得補寫逐字稿裡沒有的內容
- 聲紋分群不可靠時，不得編造講者姓名；標註「講者對應為推測」
- 數字一律照逐字稿原文，不四捨五入、不換算
```

Agent 只需要兩種權限：讀寫 `archive/<job_id>/`、執行 `bin/make_pdf.py` 與
`bin/make_docx.py`。不需要 SDK 或 API key。

---

## 為什麼規格放在 repo 裡而不是 prompt 裡

`templates/NOTE_SPECS.md` 進版控，agent 的 prompt 不進。

這樣做的好處：改會議記錄的結構時改一個檔案就好，不用去動每台機器的 agent 設定；
而且規格會跟著 `git pull` 一起走。兩種接法共用同一份規格，所以 A 跟 B 產出的
文件結構是一致的。

---

## 自己驗一次

不透過 agent，手動跑完整條鏈：

```bash
cd <REPO>

# 1. 轉寫
.venv/bin/python bin/process_meeting.py ~/Desktop/test.m4a \
  --outdir archive/manual-test --language zh \
  --title "測試" --client "測試" --date 2026-07-29

# 2. 看結果
cat archive/manual-test/transcript.md

# 3. 寫文件（B 模式；A 模式的話這步由對話 agent 做）
.venv/bin/python bin/agent_note.py archive/manual-test
```

三步都過，代表整條鏈是好的。
