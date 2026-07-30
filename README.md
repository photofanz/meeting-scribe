# meeting-scribe

手機錄完會議 → 上傳 → **本機**轉寫與講者分離 → AI agent 整理成會議記錄。

**音檔、逐字稿、會議記錄完全不離開你的 Mac。** 不需要專用硬體，不需要任何 SaaS 訂閱，
不需要 OpenAI / Whisper API key，不需要 HuggingFace token。

實測（Apple M3、128 GB）：**2 小時會議約 13 分鐘轉寫完**，約 9–10× 實時。

最近一次完整跑通的真實長會議：2 小時 07 分錄音 → 轉寫 13.1 分鐘（9.7× 實時）
→ 切成 5 片平行掃描、整理出 8 題 → 回答後產出定稿逐字稿與會議記錄的
Markdown / PDF / Word。順帶一提，那場的聲紋分離把 2 個人分成了 112 群——
所以才有問題卡這一關。

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
│  ffmpeg 正規化 16 kHz mono（ASR）        │
│        │                                 │
│        ├── 聲紋分離 (sherpa-onnx, ONNX)  │
│        │    └─ 若講者數異常：固定群數 /   │
│        │       左右聲道 fallback 自動重跑 │
│        └── ASR (mlx-whisper, Metal GPU)  │
│        │                                 │
│  合併 → 簡轉繁 → transcript.md           │
└─────────────────────────────────────────┘
      │
      ▼
┌─────────────────────────────────────────┐
│  review 階段（本機 claude / codex CLI）  │
│                                          │
│  掃描：切片平行讀完整份逐字稿            │
│        → transcript_draft.md             │
│        → questions.json（≤8 張卡）       │
│        │                                 │
│   ── 你在網頁上「點選」回答 ──           │
│        │                                 │
│  定稿：答案用 Python regex 套回          │
│        → transcript_clean.md             │
│        → 會議記錄由定稿逐字稿寫成        │
└─────────────────────────────────────────┘
      │
      ▼
  PDF / Markdown / Word → 歸檔 → 完成通知
```

長會議不是丟一個大 prompt 給模型就好——2 小時的逐字稿約 127 KB，塞不進單次
context，而且失敗是無聲的：模型讀完開頭，自信地寫出前 20 分鐘的會議記錄，然後
exit 0。切片與提問這兩道關卡就是為了擋這件事，設計理由見 [docs/REVIEW.md](docs/REVIEW.md)。

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
| AI agent | 選用。轉寫本身不需要。會議記錄需要一個 LLM：可以是對話 agent，也可以是本機的 `claude` / `codex` CLI（見 [docs/AGENT.md](docs/AGENT.md)） |

首次轉寫時 mlx-whisper 會自動抓 ASR 模型（約 1.5 GB）。之後全離線。

---

## 安裝

```bash
git clone https://github.com/photofanz/meeting-scribe.git ~/Meetings
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
    "mode": "none",
    "events": ["awaiting_answers", "done", "error"],
    "telegram": {"bot_token": "", "chat_id": ""},
    "command": "",
    "webhook": {"url": "", "secret": ""}
  },
  "branding": {
    "brand_name": "MEETING NOTES",
    "client_footer": "本文件內容以雙方會議討論為準。",
    "participants_hint": "例：王總、張經理、我"
  },
  "asr": {
    "whisper_model": "mlx-community/whisper-large-v3-turbo",
    "diarization_threads": 4,
    "diarization_threshold": 0.75,
    "diarization_max_speakers": 8,
    "diarization_stereo_fallback": true
  },
  "agent": {
    "mode": "review",
    "backend": "claude",
    "bin": null,
    "model": null,
    "timeout_sec": 3600,
    "chunk_chars": 14000,
    "max_parallel": 3,
    "max_questions": 8
  }
}
```

每個鍵都可省略，省略就吃 `bin/config.py` 的預設值；`config.example.json` 裡有逐鍵說明。
改完跑 `./install.sh`（重生 plist）或 `./bin/service.sh restart`。

**通知**（`notify.mode`，四選一，預設完全不推播）：

| mode | 做什麼 | 需要什麼 |
|---|---|---|
| `none` | 不推播，所有狀態都在 `/jobs` 頁看 | 無（預設，讓全新 clone 不依賴任何其他軟體） |
| `telegram` | 直接打 Telegram Bot API | @BotFather 的 token 與 chat id |
| `command` | 呼叫你原本就在用的通知 CLI | 一組 argv 樣板 |
| `webhook` | POST 帶 HMAC-SHA256 簽章的 JSON 到你的服務 | url ＋ secret |

```bash
.venv/bin/python bin/notify_setup.py     # 互動式填完並自動驗證
```

`notify.events` 決定哪些狀態值得推。`command` 模式的 `{message}` / `{files}` 是在
argv 切分**之後**才代入，所以會議標題含引號或空白也不會變成多餘參數。推不出去的
訊息會寫進 `logs/undelivered.log`，主流程照跑。舊版 `notify.enabled/bin/target`
設定檔仍相容，載入時自動對應到 `command` 模式。

**文件撰寫**（`agent.mode`）：

| mode | 行為 |
|---|---|
| `review` | **預設。** 掃描 → 在 `/job/<id>` 出題 → 你點選回答 → 才寫文件 |
| `auto` | 掃描與撰寫連續跑完，全用 `best_guess`，沒有人在迴圈裡 |
| `manual` | 停在逐字稿，由你（或對話 agent）手動啟動 |

`review` 是預設，因為那些答案正是阻止會議記錄捏造人名與數字的東西；`auto` 產出的
文件品質較差，而且文件開頭會自己說明這件事。細節見 [docs/REVIEW.md](docs/REVIEW.md)
與 [docs/AGENT.md](docs/AGENT.md)。

---

## 使用

### 1. 手機開上傳頁

```bash
./bin/service.sh url
```

建議在 Safari 加入主畫面。分段上傳（4 MB／段，失敗自動重試），**沒有檔案大小上限**。

### 2. 填表

最值得花時間的欄位是**「背景／專有名詞」**，第二值得的是**「現場約幾人」**。
前者能提升術語與人名辨識；後者現在不只是 metadata，而是聲紋分群炸掉時的
**deterministic fallback 訊號**：系統會自動拿它去重跑固定群數候選，而不是放任
2 人會議切成 25 / 112 群。

**產出選項**（選擇記在瀏覽器，下次自動帶入）

| # | 選項 | `meta.json` 欄位 | 預設 |
|---|---|---|---|
| 1 | 輸出內容（可複選）：逐字稿 · 會議記錄 | `want_transcript` / `want_note` | 會議記錄 |
| 2 | 會議類型：一般討論 / 顧問客戶三版 / 訪談研究 | `meeting_type` | `general` |
| 3 | 輸出檔案型態（可複選）：PDF · Markdown · Word | `formats` | `["pdf","md"]` |

`formats` 是全域的，對這個 job 的每份文件都套用。兩段都有防呆：一項都不勾會擋下，
後端另有保險會強制回到「會議記錄」。

### 3. 回答問題（`agent.mode = "review"`，預設）

轉寫完成後，系統會平行讀完整份逐字稿，然後把**只有你才知道答案**的事情整理成
最多 8 張卡片，狀態轉為 `awaiting_answers`，並在 `/job/<id>` 等你。

卡片一律設計成**用點的就能回答**——要你打一段字的問題就是錯的問題。五種類型：

| 類型 | 什麼時候出現 |
|---|---|
| `speaker` | 某個聲紋標籤（講者1／講者2）到底是誰。排第一，因為把話講錯人比什麼都嚴重 |
| `term` | 同一個專有名詞被 ASR 拼成好幾種寫法，要選一個正式版本 |
| `unclear` | 音質太差，某個關鍵數字／日期／金額救不回來 |
| `conflict` | 逐字稿裡有兩句互相矛盾，必須有一個勝出 |
| `undecided` | 會議其實沒有結論，但會議記錄非得寫一句 |

答完按送出，答案會用 Python `re.sub` **機械式**套回逐字稿（不是交給模型記憶對照表），
產出 `transcript_clean.md`，會議記錄才從這份定稿寫起。實際替換了什麼，會列在定稿
逐字稿末尾的更正表——列的是真的做了什麼，不是模型說它做了什麼。

改成 `manual` 則停在逐字稿，回一句「整理這場會議」由對話 agent 接手；改成 `auto`
則全程不問你。三種模式共用同一份規格 `templates/NOTE_SPECS.md` —— **那份檔案就是
給 agent 讀的合約**，所以產出的文件結構一致。

手動重跑某個 job：

```bash
.venv/bin/python bin/review.py latest --stage scan             # 只出題
.venv/bin/python bin/review.py latest --stage write --deliver  # 套用答案並寫文件
.venv/bin/python bin/review.py latest --stage auto  --deliver  # 兩段連跑，不問人
.venv/bin/python bin/chunker.py archive/<job_id>/transcript.md # 只看切片計畫，不花模型時間
```

---

## 產出

```
archive/<YYYY-MM-DD>_<對象>_<6碼>/
    source.m4a            原始音檔
    meta.json             上傳表單內容 + 產出選項
    status.json           處理進度（UI 與 agent 都輪詢這個）
    state.json            狀態機與各階段時間戳
    transcript.md         逐字稿（繁體、講者標籤、時間戳）
    transcript.json       結構化（每段的講者與起訖時間）
    transcript.txt        純文字
    questions.json        掃描階段整理出的問題卡
    answers.json          你在網頁上的回答
    transcript_draft.md   掃描後、套答案前的草稿
    transcript_clean.md   定稿逐字稿（+ .pdf / .docx，末尾附更正表）
    note_*.md/.pdf/.docx  會議記錄
    action_items.json     待辦事項
    delivery.json         這次交付了哪些檔案
    agent_report.json     agent 自述做了什麼
    INDEX.md              檔案清單與機密層級
    .chunks/ .review/     切片與提示詞暫存（出問題時的診斷材料）
```

後綴 6 碼是為了同一天同對象開兩場會不撞名。

jobs 頁的「清理產出」只刪得掉可重生的那些檔案——**音檔、原始逐字稿、你的答案與
job metadata 永遠保留**，所以任何一場會議都能事後換模板重跑，或改完人名重寫，
不必重新上傳錄音。

---

## 組成

| 檔案 | 做什麼 |
|---|---|
| `bin/config.py` | 部署設定載入（`ROOT` 由檔案位置推導，非硬編碼） |
| `bin/upload_server.py` | 上傳網頁與 jobs／job 頁（FastAPI，分段上傳，問題卡 UI） |
| `bin/process_meeting.py` | ffmpeg → 聲紋分離 ∥ ASR → 字級切分 → 簡轉繁 |
| `bin/zhtw.py` | 簡轉繁（s2tw + 台灣商務／技術詞修正表） |
| `bin/jobstate.py` | job 狀態的唯一真相（`state.json`，UI 只讀這個） |
| `bin/chunker.py` | 逐字稿決定性切片（不會把一個發言切兩半） |
| `bin/review.py` | 兩階段產文：掃描出題 → 套用答案 → 寫文件 |
| `bin/agent_note.py` | 單次撰寫（短會議夠用，長會議走 `review.py`） |
| `bin/notify.py` | 對外通知的唯一出口（none／telegram／command／webhook） |
| `bin/notify_setup.py` | 互動式設定通知並當場驗證送得出去 |
| `bin/make_pdf.py` | Markdown → 品牌 HTML → PDF（headless Chrome） |
| `bin/make_docx.py` | Markdown → Word（pandoc + CJK 字型範本） |
| `bin/run_job.sh` | 比 HTTP request 活得久的 shell wrapper，依 `agent.mode` 決定後續 |
| `bin/service.sh` | launchd 控制器 |
| `templates/NOTE_SPECS.md` | 各類會議記錄的結構規格（agent 讀這份） |
| `templates/SCAN_TASK.md` | 掃描階段給 agent 的指令 |
| `templates/PARTIAL_TASK.md` | map-reduce 撰寫時，單一切片的取材指令 |
| `templates/AGENT_TASK.md` | 撰寫階段給 agent 的指令 |

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

1. **講者標籤沒有姓名**，是聲紋分群結果（講者1／講者2）。`review` 模式會直接問你
   哪個標籤是誰，答完才機械式替換；`auto` 模式則只能靠內容推斷，會猜錯。
2. **線上會議錄音的聲紋分離會失準。** 各人裝置與網路不同造成音色差異，實測一場三人
   線上會議被分成 61 群，另一場被分成 112 群。實體會議、單一麥克風的效果好得多。
   **分不準時不要硬猜是誰**——這正是問題卡存在的理由。
3. **逐字稿一定有同音錯字**，清稿是必要步驟，不是加分項。實測同一個姓氏被 ASR 寫成
   七種版本（革新／葛新／葛星／葛晶／可欣／可信／可惜），這種只能問人。
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
