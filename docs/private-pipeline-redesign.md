# 私密模式管線重構設計（Local Evidence Pipeline）

狀態：設計定案，實作中
日期：2026-08-19

## 0. 為什麼要推翻現有設計

現有私密模式（`agent.profiles.private`）把本地模型當 Claude 用：給一份任務書，
要它自己讀規格、自己讀 20,000 字逐字稿、自己決定寫什麼、自己 `write_file`。
這是**長程自主規劃**，正好是 30B 級模型最弱的能力。

實測（`unsloth/muse-glimmer-30b`，BF16 GGUF 57 GB，`trained_for_tool_use: false`）：

| 指標 | 私密模式實測 | Claude 路徑同類會議 |
|---|---|---|
| 一輪 write 耗時 | 2,572 秒 | — |
| `note_general.md` 產出 | 1,272 字元 | — |
| 產出／來源字數比 | **0.061**（第二輪後 0.195） | **0.34–0.79** |

三個根因：

1. **模型選錯**：BF16 GGUF 是 Mac 上最慢的組合，且該模型未受工具訓練，
   卻被 `tool_loop: true` 拿去跑 60 步工具迴圈。
2. **任務型態錯**：要求長程規劃與長程連貫寫作。
3. **零結構化輸出、零內容驗證**：LM Studio 原生支援 `response_format: json_schema`，
   程式碼一次都沒用；產出端沒有引用／數字／簡體字的任何驗證。

## 1. 核心原則

> **本地模型不該做「決定寫什麼」，只該做「在小範圍內填一個嚴格 schema」。**
> 結構、順序、引用、驗證全部交給 Python。

推論：雲端模型的優勢是「一次長程連貫推理」，這複製不了；但可以被繞過 ——
把任務拆到沒有任何一步需要長程推理。

### 1.1 靈魂條款：模型永遠不複述原文

實測抓到的關鍵風險：要模型回傳引文時，原文「就這樣定了。」變成「就這樣定了.」
（全形句號被改成半形點）。**只要讓模型複述原文，它就會微幅改寫**，那正是編造的入口。

因此：

- 模型輸出**只能回 `turn` 編號**（`Turn.index`），不得回原文字串。
- 引文一律由 Python 依 `turn` 從 `transcript_clean.md` 精確擷取。
- 這讓「不編造引文」從一條 prompt 祈求，變成**結構上不可能發生**。

## 2. 管線

私密模式走**獨立管線**，不再與一般模式共用 writer。
一般模式（Claude CLI）完全不動。

| 階段 | 執行者 | 輸入 | 輸出 | 為何小模型做得到 |
|---|---|---|---|---|
| **S0 能力閘門** | Python | config | pass / 明確拒絕 | 純判斷 |
| **S1 清稿** | 模型 | 逐字稿分段 | `replacements`（不回全文） | 輸出從 14k 字降到數百字 |
| **S2 證據抽取（map）** | 模型 | 每段 turns | `evidence_NN.json`（json_schema 強制） | 短 context、固定 schema |
| **S3 合併** | **Python** | 各段 evidence | `evidence.json` | 決定性、可重現 |
| **S4 逐議題撰寫（map）** | 模型 | 單一議題證據包 | 該議題一節 markdown | 小模型最強場景，可平行 |
| **S5 組裝** | **Python** | 各節 | `note_*.md` / `action_items.json` | 結構永遠不缺章節 |
| **S6 驗證閘門** | **Python** | 成品 | pass / 重跑失敗的那一節 | 便宜、可量化 |
| **S7 grounding critic** | 模型（選配） | 一節 + 其證據 | 有無超出證據 | 短、便宜 |

### 2.1 為什麼這能逼近雲端水準

- **涵蓋率**：不再靠模型記得要寫幾個議題，是 Python 數出來的
- **不編造**：引用與數字是程式擷取＋程式驗證
- **結構完整**：骨架由 Python 生成
- **速度**：估 43 分鐘 → 3–6 分鐘

### 2.2 誠實的極限

- **敘事流暢度**輸雲端 —— 逐節生成天然比一次寫完鬆散
- **「已決議 vs 待定」的 judgement** 輸雲端 —— 實測中模型偏好安全地填 `pending`；
  需要 S4 的明確判準拉近，難完全追平

---

## 3. 契約（實作必須照這份，不得自行更名）

### 3.1 新模組

```
bin/private_pipeline.py    S2–S5 主管線
bin/note_verify.py         S6 驗證閘門
bin/schemas.py             所有 json_schema 定義（單一來源）
```

`bin/review.py` 在 `stage_write()` 分流：私密模式 → `private_pipeline.run()`，
其餘維持現狀。

### 3.2 S0 能力閘門

寫在 `bin/lmstudio_runtime.py`，擴充既有 `preflight()`：

```python
def capability_check(model_key: str, *, need_tools: bool, need_ctx: int,
                     acfg: dict | None = None) -> tuple[bool, str]:
    """Verify the model can do the job before an hour of work is spent."""
```

- 資料來源：`list_models()` 已解析的 `tool_use` / `max_context_length` / `format`
- `need_tools=True` 但 `tool_use=False` → **拒絕**，訊息指出同機可用的替代模型
- `format == "gguf"` → **警告**（Mac 上顯著較慢），不擋
- `max_context_length < need_ctx` → 拒絕

### 3.3 結構化輸出

`bin/agent_note.py` 的 `_openai_compat_request()` 增加可選參數：

```python
def _openai_compat_request(prompt, model, acfg, *,
                           schema: dict | None = None,
                           system: str | None = None) -> tuple[str, str, dict, str]:
```

`schema` 非 None 時送出：

```json
{"response_format": {"type": "json_schema",
                     "json_schema": {"name": "<name>", "strict": true, "schema": {...}}}}
```

實測確認 LM Studio 支援且 100% 有效。

### 3.4 S2 證據抽取 schema（`schemas.EVIDENCE`）

模型每段只回這個。**沒有任何欄位允許原文字串**。

```jsonc
{
  "topics": [{
    "topic_id": "t1",              // 段內流水，S3 重編
    "label": "string",             // 模型自己下的短標題（非原文）
    "turns": [12, 13, 14],         // 涵蓋的 turn index
    "status": "decided|pending|parked",
    "status_turn": 14,             // 依據哪個 turn 判定；判不出來填 null
    "speakers": ["艾薇", "Jim"]
  }],
  "points": [{                     // 發言事件
    "turn": 12,
    "speaker": "string",
    "gist": "string",              // 模型改寫的重點，允許
    "topic_id": "t1"
  }],
  "numbers": [{
    "turn": 20,
    "literal": "string",           // 照原文抄的數字字串，S6 會驗證它真的在該 turn 裡
    "means": "string"
  }],
  "actions": [{
    "turn": 33,
    "what": "string",
    "owner": "string|未指派",
    "due": "string|未定"
  }],
  "unclear": [{"turn": 41, "why": "string"}]
}
```

**`literal` 是唯一允許貼近原文的欄位**，且 S6 會逐一驗證它是該 turn 文字的子字串；
驗不過就丟棄該筆並記錄，不得進入成品。

### 3.5 S3 合併（純 Python，零模型）

`private_pipeline.merge_evidence(parts: list[dict], turns: list[Turn]) -> dict`

必須是決定性的：

1. **去重疊**：只採 `chunk.own_turns` 範圍內的項目（`chunk.overlap_count` 已知，不得用猜的）
2. **議題聚類**：相鄰 chunk 的 topic 若 `turns` 區間相接且 label 相似則合併。
   相似度先用純 Python（正規化後字元 n-gram Jaccard ≥ 0.5）；
   同機有 `text-embedding-nomic-embed-text-v1.5` 可用，但**不是第一版必要**。
3. **人名正規化**：以 `meta.participants` + `result.speaker_map` 為準名表，
   模型給的名字對不上就保留原樣並標記
4. **數字去重**：同 turn 同 literal 只留一筆
5. **撈回原文**：每個 topic 依 `turns` 從 `transcript_clean.md` 擷取實際文字，
   組成該議題的證據包
6. **順序**：依 `min(turns)` 排序，永遠照會議實際順序

輸出 `work/evidence.json`，並保留 `work/evidence_raw_NN.json` 供除錯。

### 3.6 S4 逐議題撰寫

一次只餵：

- 該議題的證據包（目標 1–3k 字，超過就再切）
- 這一節的寫作規格（從 `NOTE_SPECS.md` 抽出的**該文件類型片段**，非全文）
- 判定準則（已決議／待定／保留）

輸出：該議題一節 markdown。**平行執行**（沿用 `acfg.max_parallel`）。

### 3.7 S5 組裝（純 Python）

- 骨架、標題層級、目錄、branding、待辦表全由 Python 生成
- `action_items.json` 直接由 S3 的 `actions` 產生，**不經模型**
- 引文由 Python 插入，格式 `> [HH:MM:SS] 講者：原文`

### 3.8 S6 驗證閘門（`bin/note_verify.py`）

```python
def verify(job_dir: Path, evidence: dict, turns: list[Turn]) -> list[Finding]
```

檢查項（全部是程式可判定的）：

| 檢查 | 失敗處置 |
|---|---|
| 每則引文必須是 `transcript_clean.md` 的子字串 | 移除該引文並記錄 |
| 每個數字必須在其 turn 原文中出現 | 移除並記錄 |
| 簡體字掃描（沿用 `bin/zhtw.py`，目前只跑在 ASR 階段） | 自動轉換 |
| 議題數 == S3 算出的議題數 | 重跑缺的那一節 |
| `action_items.json` schema 驗證 | 重跑 |
| 章節骨架完整 | Python 補（本來就是 Python 生成，不該失敗） |

**只重跑失敗的那一節**，不重跑整份。

### 3.9 S1 清稿修正

`templates/SCAN_TASK.md` 目前要模型把 14,000 字清稿**原樣吐回 JSON**，
而 `max_output_tokens = 16384` —— 輸出量 ≈ 輸入量，中文幾乎必爆。
但 `SCAN_TASK` 本來就有 `replacements`（找／換）機制，**draft 全文根本是多餘的**。

改法：私密路徑的 scan 只回 `replacements` + `questions`，不回 `draft`。
Python 依 `replacements` 產生 draft。一般模式（Claude）維持現狀。

### 3.10 config

```jsonc
"private": {
  "backend": "openai_compat",
  "model": "qwen3.8-27b-mtplx",
  "pipeline": "evidence",          // "evidence" | "legacy"
  "tool_loop": false,              // evidence 管線不需要工具
  "api": { "max_output_tokens": 8192, "temperature": 0.1 }
}
```

`pipeline: "legacy"` 保留舊路徑一個版本供比對，之後刪除。

### 3.11 B4 作廢

路徑 C（單次 JSON 信封）的問題不是「文案跟 `AGENT_TASK.md` 不同步」，
而是這個概念本身該刪掉 —— 回覆一旦被 `max_output_tokens` 截斷，整個 job 掛掉。
evidence 管線上線後移除。

---

## 4. 模型建議（同機現成，不需下載）

| 用途 | 模型 | 規格 |
|---|---|---|
| **跑通管線（首選）** | `qwen3.8-27b-mtplx` | mlx 8bit / 262k ctx / tools ✓ |
| 速度優先 | `gemma-4-26b-a4b-it-qat-mlx` | mlx 4bit / 262k ctx / tools ✓ |
| 品質優先 | `gpt-oss-120b` | mlx MXFP4 / 131k ctx / tools ✓ |
| 超長 context | `mistral-small-4-119b-2603` | mlx 4bit / **1M ctx** / tools ✓ |
| **絕對不要** | `unsloth/muse-glimmer-30b` | gguf BF16 / tools **✗** ← 現用 |

新架構下多數步驟不需 tool calling，選型空間反而變大。

---

## 5. 落地順序

| | 內容 | 驗收 |
|---|---|---|
| **P0** | S0 能力閘門 ＋ 換模型 | 用舊模型跑私密 job 會在數秒內被明確擋下 |
| **P1** | `json_schema` 結構化輸出 ＋ scan 砍掉 draft 全文 | 不再有 JSON 解析失敗與截斷崩潰 |
| **P2** | S2–S5 新管線 | 真實 job 跑完，產出比值 ≥ 0.30 |
| **P3** | S6 驗證閘門 | 注入假引文能被抓出 |
| **P4** | eval harness | 對 3 場已有 Claude 產出的會議量化比對 |

**P4 不是可選項** —— 沒有它，之後每次調整都是猜的。
