# 會議文件產出規格

處理任何 job 時，**先讀 `<outdir>/meta.json`**，依下列四個欄位決定要做什麼、要交付什麼：

| 欄位 | 值 | 缺漏時的預設 |
|---|---|---|
| `want_transcript` | `true` / `false` — 是否交付整理過的逐字稿 | `false` |
| `want_note` | `true` / `false` — 是否產出會議記錄 | `true` |
| `meeting_type` | `general` / `client` / `interview` | `general` |
| `formats` | `["pdf","md","docx"]` 的子集 | `["pdf","md"]` |

規則：
- `want_note` 與 `want_transcript` 皆為 false → 當作 `want_note=true`（不可零產出）。
- **`formats` 是全域的**：對這個 job 產出的**每一份文件**（逐字稿與各版會議記錄）都套用同一組格式。
- `.md` 一律先產出（它是 PDF 與 Word 的來源），但 `formats` 沒有 `md` 時**不傳給使用者**，只留在歸檔資料夾。
- `formats` 含 `pdf` → 跑 `bin/make_pdf.py`；含 `docx` → 跑 `bin/make_docx.py`；沒有就**不要跑**。
- `meeting_type` 只在 `want_note=true` 時有意義。

**產生器指令**（兩者 CLI 相同）：

```bash
bin/make_pdf.py  <note.md> --out <note.pdf>  --kind <kind> \
    --title "…" --client "…" --date YYYY-MM-DD --participants "…" --duration "HH:MM:SS"
bin/make_docx.py <note.md> --out <note.docx> --kind <kind> ...同上
```

`--kind`：`general` / `client` / `self` / `partner`；逐字稿轉檔時 `make_docx.py` 另外吃 `transcript`（`make_pdf.py` 用 `general`）。

| meeting_type | 產出檔案 | PDF `--kind` |
|---|---|---|
| `general`（預設） | `note_general.md/.pdf`、`action_items.json` | `general` |
| `client` | `note_client`、`note_self`、`note_partner`、`action_items.json`、`email_draft.md` | `client` / `self` / `partner` |
| `interview` | `note_interview.md/.pdf`（含重點語錄） | `general` |

所有類型共同前置步驟：
1. `transcript.md` → `transcript_clean.md`（繁體化、依上下文修 ASR 錯字、講者對應真實姓名、移除重複段落）
2. 依 `want_note` / `want_transcript` 產出對應文件；`formats` 含 `pdf` 才跑 `make_pdf.py`
3. 寫 `INDEX.md`，標註每份文件機密層級

---

## 交付規格

只傳「使用者勾選的東西」，不多傳也不少傳。

每份文件依 `formats` 交付，**同一份文件的多種格式相鄰傳**，順序固定 **md → pdf → docx**。
文件之間的順序：**先逐字稿、後會議記錄**（`client` 三版順序為 客戶版 → 內部覆盤 → 夥伴版）。

**整理過的逐字稿**（`want_transcript=true`）
- 交付 `transcript_clean.{md,pdf,docx}`
- 訊息中附上「修正對照表」摘要（修了幾處、最關鍵的幾個詞）
- `want_transcript=false` 時，`transcript_clean.md` 仍會在歸檔資料夾裡，但**不主動傳**
- 一小時會議的逐字稿約 3–4 萬字；產 PDF／Word 前先提醒使用者頁數（例：「約 40 頁」）

**會議記錄**（`want_note=true`）
- `client` 三版 × 三種格式 → 最多九個檔
- 若 Telegram 拒收 `.md`（附件型別誤判），改傳 `.md.txt` 複本，並在訊息中說明

---

## transcript_clean.md — 整理過的逐字稿規格

**定位**：可直接引用、可貼進論文或報告的定稿逐字稿。不是摘要，**不刪內容**。

**必做的修正**：
1. **繁體化** — 走 `bin/zhtw.py`，全文不得殘留簡體字
2. **ASR 錯字／同音字** — 依上下文修正（例：`播空→撥空`、`全线→權限`、`誓約→續約`、`還務上→財務上`）
3. **專有名詞統一** — 人名、公司名、技術名詞在全文採同一寫法（例：`革新/葛新/葛星/可欣` → `葛鑫`）；優先採用 `meta.json` 的 `context` 與 `participants` 欄位提供的正確寫法
4. **講者標註** — 用真實姓名；若為推測，在文件開頭註明「講者對應為推測」
5. **重複段落** — 錄音重播、卡頓造成的重複，去重並註明
6. **時間戳** — 每個發言段保留 `[HH:MM:SS]`

**格式**：

```markdown
# {主題} 逐字稿（已校訂）

> 對象：{客戶}　·　日期：{YYYY-MM-DD}　·　長度：{HH:MM:SS}
> （如講者對應為推測，在此註明）

## 修正對照表
| 原辨識 | 修正為 | 依據 |
|---|---|---|

## 逐字稿
**[00:00:12] 王總**
發言內容……

**[00:00:48] 李顧問**
發言內容……
```

**禁則**：
- 不得改寫語意、不得潤飾語氣、不得刪除離題內容 —— 只修「聽錯的字」
- 聽不清楚的地方寫 `（音檔不清楚）`，**不要猜**
- 修正對照表要誠實列出所有非顯而易見的修改，讓使用者能回查

---

## general — 一般討論會議記錄（預設版）

**定位**：可直接分送全體與會者的中性記錄。不做立場判讀、不寫主觀評估、不寫談判策略。
**語氣**：客觀、第三人稱、不加形容詞。

**固定結構**：

```markdown
# {主題} 會議記錄

> 主持：{姓名}　·　（如講者對應為推測，在此註明）

<!-- 不要在 markdown 內再放日期／出席／時長表格；make_pdf.py 已自動渲染表頭，重複會出現兩份 -->

## 一、本次會議重點（3 句以內）

## 二、議題討論
### 1. {議題名稱}
- **討論內容**：各方提出的論點，標明是誰提的
- **結論**：已決議 / 待定 / 保留
（每個議題一節，依會議實際議程排序）

## 三、決議事項
| # | 決議 | 依據／理由 |

## 四、待辦事項
| # | 事項 | 負責人 | 期限 | 狀態 |

## 五、未解決事項與待確認
- 會中提出但沒有結論的問題，以及需要誰在何時補資料

## 六、下次會議
- 時間 / 議題 / 需先完成的前置作業
```

**規則**：
- 「決議」與「待辦」必須分開。決議是做了什麼決定，待辦是誰要去做什麼。
- 沒講到的欄位寫「未討論」，**不要編**。
- 待辦沒有明講負責人時寫「未指派」，不要用推測的人名。
- 期限只寫會中明確講出來的；沒講寫「未定」。
- 反對意見與保留意見要寫進「討論內容」，不能只留下最後共識。
- 數字（金額、百分比、週數、人數）一律照逐字稿原文，不四捨五入、不換算。

---

## client — 顧問／客戶會議（三版）

四份文件，同一場會議、三種讀者：

- `note_client.md` — 中性、可外發的會議紀要（結論／討論摘要／行動項目／下次會議）
- `note_self.md` — 內部覆盤：局勢判讀、對方真實動機、我方失誤、報價與談判策略備忘。**嚴禁外傳**
- `note_partner.md` — 給未出席夥伴的補課摘要＋交付要求
- `email_draft.md` — 給客戶的會後信

## interview — 訪談／研究

- 受訪者背景 → 訪談主軸 → 逐題重點 → **重點語錄（逐字引用＋時間戳）** → 我的觀察 → 後續追問清單
- 語錄必須逐字，不得改寫；標註 `[00:12:34]`

---

## 通用禁則
- 不得補寫逐字稿裡沒有的內容。不確定的地方寫「（音檔不清楚）」或「（未討論）」。
- 全文繁體中文；技術名詞保留原文（ERP、SaaS、API）。
- 講者姓名對應若為推測，在文件開頭註明「講者對應為推測」。
