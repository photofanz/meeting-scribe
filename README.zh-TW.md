# meeting-scribe

[English version](README.md)

**為高信任度會議交付而設計的本機優先處理系統。**

`meeting-scribe` 不是單純把錄音轉成文字的工具，而是一套面向真實商務工作流設計的**會議交付基礎設施**。它將會議錄音一路轉化為可審閱、可追溯、可正式交付的逐字稿與會議文件，涵蓋**上傳、轉寫、講者分離、問題澄清、文件生成與交付**的完整流程，並全程以你的 Mac 為執行主機。

對顧問團隊、專業服務公司與重視客戶信任的組織而言，問題從來不是「能不能生成摘要」，而是：**能不能在保護資料邊界的前提下，穩定產出足夠正式、足夠準確、足夠能代表專業形象的交付成果。** `meeting-scribe` 回答的正是這個問題。

## 方案重點

- **從錄音到交付的一體化流程**：不是把工具拼在一起，而是將上傳、轉寫、review、定稿與交付串成一條可運作的正式生產流程。
- **以風險控制為核心的 review 設計**：對姓名、術語、數字、金額、日期與矛盾敘述進行主動攔截，降低高風險錯誤直接流入正式文件。
- **本機優先與資料邊界清楚**：關鍵音檔、逐字稿與中間材料可留在內部工作站處理，避免一開始就把敏感內容外送到第三方雲端服務。
- **可重跑、可稽核、可持續修訂**：每場會議保留完整 job 狀態與中間材料，後續可依客戶需求、範本調整或資訊修正重新生成文件。
- **兼顧顧問實務與 AI 自動化**：既能接 `claude`、`codex` 等外部模型，也能切換到 LM Studio 本地模型，讓效率與保密要求可以依情境平衡。
- **交付物不是附帶功能，而是核心成果**：系統直接面向逐字稿、正式會議記錄、待辦事項與 `Markdown / PDF / Word` 等對外可用成果。
- **可部署、可常駐、可維運**：透過 `install.sh` 與 `launchd` 落地為內部服務，而不是拋棄式的 demo 指令碼。

---

## 核心能力

| 能力 | 說明 |
|---|---|
| **本機優先轉寫** | 音檔在本機完成正規化、ASR 與簡轉繁處理，降低對外部轉寫 SaaS 的依賴，讓資料邊界更可控。 |
| **講者感知處理** | 以 `sherpa-onnx` + CAM++ zh 執行聲紋分群，並保留 headcount-aware fallback，提升多人會議的可用性。 |
| **長會議穩定性** | 大型逐字稿可切片、平行掃描並分階段處理，避免單次 prompt 對長內容失真或靜默失敗。 |
| **互動式澄清機制** | 將姓名、術語、數字與矛盾敘述整理為可回答的問題卡，把不確定性留在 review 階段解決，而不是讓錯誤進入定稿。 |
| **雙 AI 運作模式** | 一般模式可接 Claude / Codex；保密模式可接 LM Studio 本機模型，因應不同資料敏感度。 |
| **可控模型生命週期** | 內建 private model cleanup 策略：`keep_loaded`、`idle_eject`、`after_job`，兼顧資源效率與作業安全。 |
| **可重跑與可稽核** | 保留 source、raw transcript、answers、state，使後續修訂、重寫與內部稽核有明確依據。 |
| **多格式正式交付** | 可輸出 `transcript` 與 `meeting note`，格式支援 `md`、`pdf`、`docx`，直接對接顧問與管理場景的交付需求。 |
| **通知與流程整合** | 支援 `none`、`telegram`、`command`、`webhook` 四種通知模式，可納入既有內部作業流程。 |

---

## 適用場景

`meeting-scribe` 特別適合下列工作情境：

- **顧問公司與專業服務團隊**：需要把客戶訪談、專案會議與策略討論，整理成可正式對外傳送的會議記錄與行動項目。
- **重視資料邊界的企業內部團隊**：希望在不犧牲效率的情況下，避免將敏感會議內容預設送往外部雲端服務。
- **高價值、高風險會議場景**：例如涉及商業判斷、金額、時程承諾、跨部門決策或客戶溝通的會議，不能接受「看起來完整、實際卻有關鍵錯漏」的摘要。
- **需要持續修訂與版本管理的交付流程**：當會議文件需要依回饋、範本或新資訊反覆調整時，系統必須支援可追溯與可重生成，而非一次輸出後無法回頭。

它不是錄音硬體，也不是公有雲會議 SaaS。更準確地說，它是一個可部署於內部環境、服務正式交付流程的**會議處理層**。

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

在 Apple Silicon 上，2 小時錄音可在約 **13 分鐘**內完成轉寫，約 **9–10× realtime**。這代表它不只是可行概念，而是具備實際營運節奏的處理能力。詳細資料與模型選型比較見 [`BENCHMARK.md`](BENCHMARK.md)。

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

這個設計的重點不在於「流程比較長」，而在於**把高風險的不確定性留在可控制的 review 階段處理**。對正式交付而言，真正昂貴的從來不是多一步確認，而是錯誤地把錯的姓名、錯的金額、錯的判斷寫進客戶文件。review stage 的設計理由見 [`docs/REVIEW.md`](docs/REVIEW.md)。

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

- 改範本後重寫同一場會議記錄
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
| **保密模式** | `openai_compat` + LM Studio + **evidence 管線** | 本地模型只填 schema；結構、引文、驗證由 Python 做 |

### LM Studio cleanup 策略

| 模式 | 行為 |
|---|---|
| `keep_loaded` | 模型常駐，下一次最快 |
| `idle_eject` | 閒置 `idle_minutes` 後自動釋放模型 |
| `after_job` | 每次保密任務完成後立即釋放模型 |

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
| `port` / `service_label` | Web 服務與 `launchd` 設定 |
| `notify` | 通知模式與目的地 |
| `branding` | 文件品牌名與 footer |
| `asr` | Whisper model、speaker threshold、fallback 行為 |
| `agent.mode` | `review` / `auto` / `manual` |
| `agent.profiles` | 一般模式 / 保密模式的模型後端 |
| `agent.private_cleanup` | 保密模式模型釋放策略 |

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
| `bin/review.py` | `scan → answer → write` 的長會議處理流程 |
| `bin/agent_note.py` | 單次文件撰寫 |
| `bin/lmstudio_runtime.py` | LM Studio 狀態、模型載入與 cleanup 決策 |
| `bin/notify.py` | 對外通知統一出口 |
| `bin/make_pdf.py` / `bin/make_docx.py` | 文件格式轉換 |
| `templates/NOTE_SPECS.md` | 會議記錄規格合約 |
| `templates/SCAN_TASK.md` / `PARTIAL_TASK.md` / `AGENT_TASK.md` | AI 階段指令範本 |

---

## 文件導覽

| 文件 | 內容 |
|---|---|
| [`docs/AGENT.md`](docs/AGENT.md) | 如何接 Claude / Codex / 對話 agent / LM Studio |
| [`docs/REVIEW.md`](docs/REVIEW.md) | review stage 的設計原因與失敗保護機制 |
| [`BENCHMARK.md`](BENCHMARK.md) | 實測速度、記憶體、模型選型資料 |
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
- 保密模式的模型釋放邏輯會避免誤釋放其他工作負載的 foreign loaded model

---

## License

MIT
