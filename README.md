# meeting-scribe

**Local-first meeting transcription and meeting-note production for Apple Silicon Macs.**

`meeting-scribe` 把一場會議從**手機錄音**一路處理到**逐字稿、清稿、會議記錄、PDF / Markdown / Word 交付**，全程以你的 Mac 為主機執行。它不是雲端 SaaS，也不是單一模型 demo，而是一套可長期運轉的本機會議處理系統。

## 開箱即可使用的系統能力

`meeting-scribe` 並非單一用途的轉寫腳本，而是一套可直接部署、可持續運行的本機會議處理系統：

- **手機可用的上傳入口**：透過瀏覽器上傳錄音，支援分段上傳與自動重試
- **本機 ASR + 講者分離管線**：Apple Silicon 上跑 `mlx-whisper` + ONNX diarization
- **可追蹤的 job system**：每場會議都有獨立 job 資料夾、狀態檔、可重跑產物
- **會議 review workflow**：長逐字稿先切片掃描、出問題卡、套用答案，再寫正式文件
- **多後端 AI 整理能力**：可接 `claude`、`codex`、對話 agent，或 LM Studio / OpenAI-compatible API
- **交付檔案產生器**：輸出 Markdown、PDF、Word，以及會議記錄與待辦事項
- **本機部署能力**：`install.sh` + `launchd`，可作為常駐服務運行

---

## 核心能力

| 能力 | 說明 |
|---|---|
| **本機轉寫** | 音檔在本機完成正規化、ASR、簡轉繁，不依賴 Whisper API 或 SaaS 轉寫服務 |
| **講者分離** | 以 `sherpa-onnx` + CAM++ zh 執行聲紋分群；支援 headcount-aware fallback |
| **長會議處理** | 大型逐字稿可自動切片、平行掃描，避免單次 prompt silent failure |
| **互動式清稿** | 把姓名、術語、金額、矛盾敘述整理成可點選回答的問題卡 |
| **雙 AI 模式** | 一般模式可接 Claude / Codex；保密模式可接 LM Studio 本機模型 |
| **可控模型卸載** | 內建 private model cleanup 策略：`keep_loaded` / `idle_eject` / `after_job` |
| **多格式產出** | 可輸出 `transcript` / `meeting note`，格式支援 `md` / `pdf` / `docx` |
| **可重跑、可稽核** | 保留 source、raw transcript、answers、state；可隨時重寫文件而不必重傳音檔 |
| **通知與交付** | 支援 `none` / `telegram` / `command` / `webhook` 四種通知模式 |

---

## 產品定位

`meeting-scribe` 適合這些情境：

- **顧問 / 業務 / PM / Founder**：需要把長會議快速整理成可發出的正式記錄
- **重視隱私的團隊**：不想把客戶會議、訪談內容送到第三方雲端 ASR / note service
- **Apple Silicon 工作站**：希望用一台常駐的 Mac 當內部會議處理節點
- **長會議與高風險內容**：不能接受模型只讀前半段就自信產出摘要的情境

它不是錄音硬體，也不是公有雲協作平台；它是**本機會議處理基礎設施**。

---

## 系統規格

| 項目 | 規格 |
|---|---|
| **作業系統** | macOS |
| **硬體** | Apple Silicon 必要 |
| **ASR backend** | `mlx-whisper`（Metal GPU） |
| **Diarization** | `sherpa-onnx-pyannote-segmentation-3-0` + `3D-Speaker CAM++ zh` |
| **建議記憶體** | 32 GB 以上 |
| **2 小時會議峰值記憶體** | 約 14.6 GB |
| **輸入** | m4a / 一般音訊檔（經 ffmpeg 正規化） |
| **輸出** | `transcript.md/.json/.txt`、`transcript_clean.md/.pdf/.docx`、`note_*.md/.pdf/.docx`、`action_items.json` |
| **部署方式** | 本機 repo + Python venv + `launchd` |
| **網路需求** | 建議搭配 Tailscale；未使用時上傳頁僅適合區網環境 |

### 實測效能

在 Apple Silicon 上，2 小時錄音可在約 **13 分鐘**內完成轉寫，約 **9–10× realtime**。詳細數據與選型比較見 [`BENCHMARK.md`](BENCHMARK.md)。

---

## 架構總覽

```text
手機錄音 / 音檔
      │
      ▼
分段上傳 Web UI
      │
      ▼
ffmpeg 正規化 ──┬── ASR (mlx-whisper / Metal)
                └── 講者分離 (sherpa-onnx / ONNX)
      │
      ▼
transcript.md / transcript.json / transcript.txt
      │
      ▼
review pipeline
  ├─ scan：切片、平行掃描、產生問題卡
  ├─ answer：使用者在 /job/<id> 回答關鍵不確定項
  └─ write：Python 套答案後，再由 AI 寫正式文件
      │
      ▼
Markdown / PDF / Word / action_items / delivery
```

這個設計的目的不是「多一段流程」，而是讓系統能穩定處理**長逐字稿、講者錯配、術語歧義、關鍵數字不清**這些真實世界問題。review stage 的設計理由見 [`docs/REVIEW.md`](docs/REVIEW.md)。

---

## 快速開始

### 1. 安裝

```bash
git clone https://github.com/photofanz/meeting-scribe.git ~/Meetings
cd ~/Meetings
./install.sh
```

`install.sh` 會完成：

1. 檢查平台與必要工具
2. 建立資料夾結構
3. 產生 `config.json` 與 `.token`
4. 建立 Python 3.12 venv 並安裝依賴
5. 下載講者分離模型（含 SHA256 驗證）
6. 安裝並載入 `launchd` LaunchAgent

> 專案不必放在 `~/Meetings`；所有腳本都依自身位置推導 root path。

### 2. 取得上傳網址

```bash
./bin/service.sh url
```

### 3. 上傳錄音、選擇輸出

上傳表單支援：

- 輸出內容：逐字稿 / 會議記錄
- 會議類型：一般討論 / 顧問客戶 / 訪談研究
- 檔案格式：PDF / Markdown / Word
- AI preset：一般模式 / 保密模式

### 4. 等待轉寫與 review

預設 `agent.mode = "review"`：

- 先完成逐字稿與掃描
- 若有關鍵不確定項，會在 `/job/<id>` 顯示問題卡
- 你回答後，系統才會產出定稿逐字稿與會議記錄

---

## 輸出與資料結構

每場會議都會在 `archive/` 下建立一個 job 目錄：

```text
archive/<YYYY-MM-DD>_<對象>_<6碼>/
    source.m4a
    meta.json
    status.json
    state.json
    transcript.md
    transcript.json
    transcript.txt
    questions.json
    answers.json
    transcript_draft.md
    transcript_clean.md
    note_*.md
    action_items.json
    delivery.json
    agent_report.json
    INDEX.md
```

### 哪些資料會被保留

`meeting-scribe` 的設計不是「跑完就丟」，而是保留足夠材料讓你之後能重跑與稽核：

- **永遠保留**：原始音檔、原始逐字稿、問題卡答案、job metadata
- **可重生**：draft、clean transcript、note、PDF、Word、暫存 review 資料

這代表你可以：

- 改模板後重寫同一場會議記錄
- 修正姓名或術語後重新產出文件
- 不重新上傳錄音就重做交付

---

## AI / Agent 整合

### 支援的整理後端

| 類型 | 用途 |
|---|---|
| **對話 agent** | 由 Hermes / Claude Code 類型 agent 手動接手整理 |
| **本機 CLI** | `claude` / `codex` 背景執行，適合自動化 |
| **OpenAI-compatible API** | 例如 LM Studio，適合保密模式 |

### 兩種預設模式

| 模式 | 典型後端 | 用途 |
|---|---|---|
| **一般模式** | `claude` / `codex` | 日常會議整理 |
| **保密模式** | `openai_compat` + LM Studio | 敏感會議與本地模型處理 |

### LM Studio cleanup 策略

| 模式 | 行為 |
|---|---|
| `keep_loaded` | 模型常駐，下一次最快 |
| `idle_eject` | 閒置 `idle_minutes` 後自動卸載 |
| `after_job` | 每次保密任務完成後立即卸載 |

系統現在也提供 LM Studio 管理狀態卡，可清楚顯示：

- 目標模型
- 目前載入模型
- 保密任務是否仍在執行
- 是否允許手動釋放模型

更多 agent 接法與手動 / 自動流程見 [`docs/AGENT.md`](docs/AGENT.md)。

---

## 主要設定

安裝時會從 `config.example.json` 產生 `config.json`。常用區塊如下：

| 區塊 | 用途 |
|---|---|
| `port` / `service_label` | Web 服務與 launchd 設定 |
| `notify` | 通知模式與目的地 |
| `branding` | 文件品牌名與 footer |
| `asr` | Whisper model、speaker threshold、fallback 行為 |
| `agent.mode` | `review` / `auto` / `manual` |
| `agent.profiles` | 一般模式 / 保密模式的模型後端 |
| `agent.private_cleanup` | 保密模式模型卸載策略 |

### 通知模式

| mode | 說明 |
|---|---|
| `none` | 不推播，僅在 `/jobs` 查狀態 |
| `telegram` | 直接發 Telegram Bot 通知 |
| `command` | 呼叫自有 CLI / script |
| `webhook` | POST JSON 到內部系統 |

互動式設定工具：

```bash
.venv/bin/python bin/notify_setup.py
```

---

## 維運指令

```bash
./bin/service.sh status
./bin/service.sh url
./bin/service.sh restart
./bin/service.sh log 50
./bin/service.sh rotate
./bin/service.sh disable
```

### 手動處理 job

```bash
.venv/bin/python bin/review.py latest --stage scan
.venv/bin/python bin/review.py latest --stage write --deliver
.venv/bin/python bin/review.py latest --stage auto --deliver
.venv/bin/python bin/chunker.py archive/<job_id>/transcript.md
```

---

## 專案組成

| 檔案 / 模組 | 角色 |
|---|---|
| `bin/upload_server.py` | 上傳頁、jobs 頁、job 詳細頁、問題卡 UI |
| `bin/process_meeting.py` | 音訊正規化、ASR、講者分離、逐字稿整理 |
| `bin/review.py` | scan → answer → write 的長會議處理流程 |
| `bin/agent_note.py` | 單次文件撰寫 |
| `bin/lmstudio_runtime.py` | LM Studio 狀態、模型載入與 cleanup 決策 |
| `bin/notify.py` | 對外通知統一出口 |
| `bin/make_pdf.py` / `bin/make_docx.py` | 文件格式轉換 |
| `templates/NOTE_SPECS.md` | 會議記錄規格合約 |
| `templates/SCAN_TASK.md` / `PARTIAL_TASK.md` / `AGENT_TASK.md` | AI 階段指令模板 |

---

## 文件導覽

| 文件 | 內容 |
|---|---|
| [`docs/AGENT.md`](docs/AGENT.md) | 如何接 Claude / Codex / 對話 agent / LM Studio |
| [`docs/REVIEW.md`](docs/REVIEW.md) | review stage 的設計原因與失敗保護機制 |
| [`BENCHMARK.md`](BENCHMARK.md) | 實測速度、記憶體、模型選型數據 |
| [`docs/DIARIZATION_RESEARCH.md`](docs/DIARIZATION_RESEARCH.md) | 講者分離研究與條件式 fallback 背景 |

---

## 限制與部署邊界

這些不是 TODO，而是目前系統的真實邊界：

1. **僅支援 macOS + Apple Silicon。** ASR 依賴 `mlx-whisper` 的 Metal backend。
2. **講者分離不等於講者姓名辨識。** 系統能分出聲紋群組，但姓名對應仍需 review 階段確認。
3. **線上會議錄音的聲紋分離品質可能顯著下降。** 多裝置、多麥克風、多網路條件會破壞 speaker consistency。
4. **逐字稿必然仍需清稿。** 特別是姓名、術語、數字、日期、金額等高風險欄位。
5. **這不是公開網際網路服務。** 建議只在 Tailscale / LAN 內使用；上傳頁靠 token 防護，沒有完整帳號系統。
6. **3 小時以上長音檔建議先切段。** 雖然系統可處理，但資源峰值與等待時間會上升。

---

## 安全模型

- `.gitignore` 採 **deny-all + whitelist** 策略，降低誤提交真實會議資料的風險
- `config.json`、`.token`、`archive/`、`logs/`、`models/`、`.venv/` 不進版控
- jobs 的「清理產出」只刪除可重生檔案，不刪原始證據與設定材料
- 保密模式的模型釋放邏輯會避免誤卸載其他工作負載的 foreign loaded model

---

## License

MIT
