# 接上 AI Agent

轉寫管線本身不需要 agent —— 跑完你就有 `transcript.md` 了。
Agent 負責的是**後半段**：清稿、寫會議記錄、轉檔、歸檔、把檔案交給你。

這份文件說明怎麼在一台新機器上把 agent 接起來。

---

## Agent 需要什麼

只有兩件事：

1. **能讀寫這個資料夾的檔案**（`archive/<job_id>/`）
2. **能執行 `bin/make_pdf.py` 與 `bin/make_docx.py`**

不需要特別的 SDK 或 API。Hermes Agent、Claude Code、任何有檔案與終端機權限的
coding agent 都可以。

---

## 觸發流程

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

---

## Agent 合約

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

---

## 為什麼規格放在 repo 裡而不是 prompt 裡

`templates/NOTE_SPECS.md` 進版控，agent 的 prompt 不進。

這樣做的好處：改會議記錄的結構時改一個檔案就好，不用去動每台機器的 agent 設定；
而且規格會跟著 `git pull` 一起走。Agent prompt 只需要負責「去讀那份規格」。

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

# 3. 轉檔（正常情況下 agent 會先寫出 note_general.md）
.venv/bin/python bin/make_pdf.py archive/manual-test/transcript.md \
  --out archive/manual-test/test.pdf --kind general \
  --title "測試" --client "測試" --date 2026-07-29
```

三步都過，代表管線是好的，剩下的問題就都在 agent 那一層。
