# meeting-scribe

手機錄完會議 → 上傳 → **本機**轉寫與講者分離 → AI agent 整理成會議記錄。

**音檔、逐字稿、會議記錄完全不離開你的 Mac。** 不需要專用硬體，不需要任何 SaaS 訂閱，
不需要 OpenAI / Whisper API key，不需要 HuggingFace token。

實測（Apple M3、128 GB）：**2 小時會議約 10 分鐘轉寫完**，約 9–10× 實時。

---

## 這是什麼

市面上的隨身錄音 AI 裝置（Pocket、Plaud、YoooClaw C·ONE…）賣的其實是三件事：
錄音硬體、雲端轉寫、AI 整理。第一件你的手機本來就有；後兩件，**Apple Silicon 跑得夠快，
而且不用把客戶對話交給任何第三方**。

這個專案就是後兩件事的本機實作：

```
iPhone 語音備忘錄
      │  (Tailscale 內網，分段上傳，無檔案大小限制)
      ▼
┌─────────────────────────────────────────┐
│  你的 Mac                                │
│                                          │
│  ffmpeg 正規化 16 kHz mono               │
│        │                                 │
│        ├── 聲紋分離 (sherpa-onnx, ONNX)  │
│        └── ASR (mlx-whisper, Metal GPU)  │
│        │                                 │
│  合併 → 簡轉繁 → transcript.md           │
└─────────────────────────────────────────┘
      │
      ▼  完成通知（唯一離開這台機器的東西）
  AI agent（Hermes / Claude Code / 任何能讀本機檔案的）
      │
      ▼
  清稿 → 會議記錄 → PDF / Markdown / Word → 歸檔
```

---

## 需求

| 項目 | 說明 |
|---|---|
| **macOS + Apple Silicon** | 必要。ASR 走 mlx-whisper（Metal GPU），Intel Mac 與 Linux 不支援 |
| 記憶體 | 建議 32 GB 以上。2 小時錄音峰值約 14.6 GB |
| Homebrew | 安裝腳本會用它補齊 `ffmpeg` 與 `uv` |
| Tailscale | 建議。沒有的話上傳頁只在區網可見，僅靠 token 保護 |
| Google Chrome | 選用，PDF 輸出用它 headless 渲染 |
| pandoc | 選用，Word (.docx) 輸出需要 |
| AI agent | 選用。轉寫本身不需要；會議記錄整理需要一個能讀本機檔案的 agent |

首次轉寫時 mlx-whisper 會自動抓 ASR 模型（約 1.5 GB）。之後全離線。

---

## 安裝

```bash
git clone https://github.com/<you>/meeting-scribe.git ~/Meetings
cd ~/Meetings
./install.sh
```

腳本會做這六件事，可重複執行：

1. 檢查平台與必要工具（缺 `ffmpeg` / `uv` 會用 brew 補）
2. 建立資料夾
3. 產生 `config.json` 與上傳 token（`.token`，權限 600）
4. 建 Python 3.12 venv 並裝相依套件
5. 下載聲紋分離模型（約 34 MB，**帶 SHA256 驗證**）
6. 產生並載入 launchd LaunchAgent，開機自動啟動

跑完會印出上傳頁網址。加 `--no-service` 可跳過背景服務設定。

> **路徑不限定在 `~/Meetings`。** 所有腳本都從自身位置推導根目錄，clone 到哪都能跑。

---

## 設定

`config.json`（安裝時從 `config.example.json` 複製，不進版控）：

```json
{
  "port": 8765,
  "service_label": "com.meetingscribe.uploader",
  "notify": {
    "enabled": true,
    "bin": "~/.local/bin/hermes",
    "target": "telegram"
  },
  "branding": {
    "client_footer": "本文件內容以雙方會議討論為準。",
    "participants_hint": "例：王總、張經理、我"
  },
  "asr": {
    "whisper_model": "mlx-community/whisper-large-v3-turbo",
    "diarization_threads": 4
  }
}
```

改完跑 `./install.sh`（重生 plist）或 `./bin/service.sh restart`。

**通知**：轉寫完成會呼叫 `notify.bin send --to <target> "<訊息>"`。
預設接 [Hermes Agent](https://hermes-agent.nousresearch.com)，但任何吃這組參數的 CLI 都行。
把 `enabled` 設 `false`，訊息會寫進 `logs/undelivered.log`，轉寫流程照跑。

---

## 使用

### 1. 手機開上傳頁

```bash
./bin/service.sh url
```

建議在 Safari 加入主畫面。分段上傳（4 MB／段，失敗自動重試），**沒有檔案大小上限**。

### 2. 填表

最值得花時間的欄位是**「背景／專有名詞」**——客戶名、人名、產業術語餵進去，
辨識準確率會明顯提升，清稿時也會用它統一寫法。

**產出選項**（選擇記在瀏覽器，下次自動帶入）

| # | 選項 | `meta.json` 欄位 | 預設 |
|---|---|---|---|
| 1 | 輸出內容（可複選）：逐字稿 · 會議記錄 | `want_transcript` / `want_note` | 會議記錄 |
| 2 | 會議類型：一般討論 / 顧問客戶三版 / 訪談研究 | `meeting_type` | `general` |
| 3 | 輸出檔案型態（可複選）：PDF · Markdown · Word | `formats` | `["pdf","md"]` |

`formats` 是全域的，對這個 job 的每份文件都套用。兩段都有防呆：一項都不勾會擋下，
後端另有保險會強制回到「會議記錄」。

### 3. 等通知

轉寫完成推一則訊息，列出這次要產出什麼。回覆「整理這場會議」，agent 依 `meta.json` 接手。
會議記錄的結構規格在 `templates/NOTE_SPECS.md` —— **那份檔案就是給 agent 讀的合約**。

---

## 產出

```
archive/<YYYY-MM-DD>_<對象>_<6碼>/
    source.m4a           原始音檔
    meta.json            上傳表單內容 + 產出選項
    status.json          處理進度（UI 與 agent 都輪詢這個）
    transcript.md        逐字稿（繁體、講者標籤、時間戳）
    transcript.json      結構化（每段的講者與起訖時間）
    transcript.txt       純文字
    transcript_clean.md  校訂後逐字稿（勾了才產）
    note_*.md/.pdf/.docx 會議記錄
    INDEX.md             檔案清單與機密層級
```

後綴 6 碼是為了同一天同對象開兩場會不撞名。

---

## 組成

| 檔案 | 做什麼 |
|---|---|
| `bin/config.py` | 部署設定載入（`ROOT` 由檔案位置推導，非硬編碼） |
| `bin/upload_server.py` | 上傳網頁（FastAPI，分段上傳） |
| `bin/process_meeting.py` | ffmpeg → 聲紋分離 ∥ ASR → 字級切分 → 簡轉繁 |
| `bin/zhtw.py` | 簡轉繁（s2tw + 台灣商務／技術詞修正表） |
| `bin/make_pdf.py` | Markdown → 品牌 HTML → PDF（headless Chrome） |
| `bin/make_docx.py` | Markdown → Word（pandoc + CJK 字型範本） |
| `bin/run_job.sh` | 轉寫完成後發通知 |
| `bin/service.sh` | launchd 控制器 |
| `templates/NOTE_SPECS.md` | 各類會議記錄的結構規格（agent 讀這份） |

模型：

- ASR：`mlx-community/whisper-large-v3-turbo`（Apple Metal）
- 語者分段：`sherpa-onnx-pyannote-segmentation-3-0`
- 聲紋比對：`3D-Speaker CAM++ zh`

選型理由與 benchmark 見 [`BENCHMARK.md`](BENCHMARK.md)。

---

## 維運

```bash
./bin/service.sh status      # PID / 狀態 / HTTP 碼
./bin/service.sh url         # 上傳頁完整網址
./bin/service.sh restart     # 改完程式碼
./bin/service.sh log 50
./bin/service.sh rotate      # log 超過 5 MB 才輪替
./bin/service.sh disable     # 永久停用（登入不再自啟）
```

不經網頁手動轉寫：

```bash
.venv/bin/python bin/process_meeting.py <音檔> \
  --outdir archive/<job_id> --language zh \
  --title "..." --client "..." --date YYYY-MM-DD \
  --initial-prompt "以下是繁體中文商務會議錄音。背景與專有名詞：..."
```

---

## 限制（都是真的踩過的）

1. **講者標籤沒有姓名**，是聲紋分群結果（講者1／講者2），要靠 agent 依內容推斷對應。
2. **線上會議錄音的聲紋分離會失準。** 各人裝置與網路不同造成音色差異，實測一場三人
   線上會議被分成 61 群。實體會議、單一麥克風的效果好得多。**分不準時不要硬猜是誰。**
3. **逐字稿一定有同音錯字**，LLM 清稿是必要步驟，不是加分項。
4. **要連得到 Tailscale**（或至少同一區網）。
5. **Mac 要開機且已登入。** 這是 LaunchAgent，開機停在登入畫面不會啟動——
   無人值守的機器請開自動登入。
6. **3 小時以上建議先切段。** 2 小時峰值記憶體約 14.6 GB。
7. **上傳頁只靠 token 保護**，沒有帳號系統。它的安全模型建立在「只有 Tailnet 連得到」。
   不要把它 port-forward 到公網。

---

## 安全

- `.gitignore` 用**拒絕全部再白名單**的寫法。這個 repo 就住在放真實客戶錄音的資料夾裡，
  一般的「忽略這些路徑」寫法只要漏一條就會外洩。
- `.token`、`config.json`、`archive/`、`logs/`、`models/`、`.venv/` 都不進版控。
- 提交前確認：`git status --porcelain` 不該出現任何 `archive/` 或音檔。

---

## License

MIT
