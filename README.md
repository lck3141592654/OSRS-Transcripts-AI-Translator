# OSRS Transcripts 混合翻譯管線

<div align="center">

[English](README_en.md) | [中文](README.md)

</div>

## 概述

這是一套專為大規模遊戲文本翻譯設計的 AI 自動化管線。腳本目錄可任意放置，無需特定專案結構。
> ⚠️ **測試版公告**：本工具目前為測試階段，暫時只支援**簡體中文**翻譯。其他語言將在正式版中支援。

## 最近更新 v0.4.6 (20/10/2026)，更新詳情請看 [Changelog](Changelog.md)
- **術語掃描大幅加速**：新增 TL4 候選索引（純英文術語用倒排索引、含標點或數字的術語用 3-gram 索引），`narrow_relevant_glossary` 冷啟動由 14–16 秒降至 0.42–0.48 秒、`scan_issues` 3,000 列 0.21 秒；主翻譯、重譯、P2 評估、P3 潤色共用同一份索引，不再逐批重建
- **提示詞更精簡**：主翻譯只送 `index` 與 `english`，移除 `notes`、`wiki_url` 與「譯文相同請回傳空字串」的要求；同批次 payload 由 16,564 降至 7,253 字元（−56%）
- **續傳與 I/O 優化**：`progress.json` 改為增量維護（每張工作表只掃一次 checkpoint 目錄）、翻譯與 P3 每批完成即落盤；全新翻譯路徑的 Excel 只讀一次；`iterrows` 熱點向量化；審查報告改在寫入時直接上色
- **逾時與 429**：LLM client 改用分項逾時（connect 60／read 1,800／write 300／pool 300 秒）並關閉 SDK 重試，移除四個呼叫點的 per-request `timeout=` 覆蓋；並行請求同時 429 時，冷卻中的重複 429 不再重複累計 strike
- **主 API 並發預設改為 100**：`MAIN_DEFAULT_LIMIT` 由 10 調整為 100；沒有在 `.env` 設定 `APIx_PARALLEL_LIMIT` 的主 API 現在預設用 100（若遇到 429 請調降並發）
- **其他**：移除 `enforcer.py`／`llm_translator.py`／`proofreader.py` 未使用的 `API_TIMEOUT` import
- **簡化**README.md、README_en.md的內容

### 特色

- **極低成本**：搭配 DeepSeek V4 Flash，75000 條對話翻譯成本約 **2.39 美元**
- **術語強制準確**：透過腳本輔助，AI 對人名/地名/物品名等專有名詞的翻譯準確率達 **99% 以上**。而 Gemini 的 Gem 功能或 Qwen 的專案之類的功能即使配合優秀的提示詞，在缺少腳本的輔助下，仍不時會錯翻專有名詞。
- **互動式操作**：支援選擇工作表、條目範圍
- **非同步並行**：同時發送多個 API 請求，大幅縮短翻譯時間
- **中斷續傳**：翻譯過程中若因網路問題或人手中斷，重新執行腳本即可從中斷處繼續，無需從頭開始
- **多工作表並行**：所有工作表在同一階段內並行，階段間同步；共用單一 API 批次池，429 冷卻狀態全 run 一致
- **自動審查**：四層檢查（術語 / 佔位符 / 未翻譯 / 空格），產出彩色標示的審查報告
- **多 API 支援**：支援同時使用多個 API Key（主/副分類），任務共用單一佇列、由空閒的 API 先取先做，自動處理 429 限流（每次 429 重新冷卻 60 秒、達門檻才永久停用；只剩一個可用 API 時門檻自動放寬），最大化翻譯吞吐量
- **自動化校對流程**：LLM 雙輪評估交叉驗證 + 最多 3 輪潤色 + 重譯保護，產出彩色標記的校對報告
- **基於術語校對**：校對過程中強制套用術語庫，避免 LLM 在潤色時偏離正確術語

### 檔案結構

| 檔案 | 用途                              |
|---|---------------------------------|
| `batch_translate.py` | **主翻譯腳本** — 互動式操作入口 |
| `batch_proofread.py` | **校對腳本** — 互動式操作入口（完整校對 / 快速校對） |
| `proofreader.py` | **校對核心模組** — LLM 雙輪評估 + 潤色 + 重譯保護（含快速校對/重譯模式） |
| `glossary.py` | 讀取術語庫 Excel 或自動萃取術語庫 + 輸出審查檔 |
| `tm_matcher.py` | **可自訂正則模板**：讓腳本自動填入翻譯，減少 API 調用次數 |
| `llm_translator.py` | **AI 批次翻譯核心**，非同步並行調用，含智慧 JSON 提取與 json_repair 修復 |
| `enforcer.py` | **四層強制檢查**（術語/佔位符/未翻譯/空格），附腳本預處理，自動修正+產出彩色審查報告 |
| `shared_pool.py` | **共享批次池** — 整個執行共用一份 API 配置、並發與 429 冷卻狀態 |
| `api_config.py` | 多 API 配置解析，支援主/副分類、類別預設並發、個別覆蓋 |
| `.env` | API 設定檔，支援多 API 編號格式及舊版單一 API 格式 |
| `.env.example` | 設定範本 |
| `workplace/` | **工作目錄**（腳本自動建立），所有輸入輸出統一存放於此 |

> `workplace/` 目錄包含以下子目錄：
> - `_checkpoint/` — 翻譯續傳暫存，各工作表擁有獨立子目錄（`{sheet_name}/`），翻譯完成後自動刪除
> - `_proofread_checkpoint/` — 校對專用續傳暫存，獨立於主翻譯
> - `_proofread_backup/` — 校對備份
> - `_debugmessage/` — 低翻譯率 debug 記錄（自動產生，下次啟動時顯示後清除）
> - `_quick_checkpoint/` — 快速校對續傳暫存（獨立於完整校對，完成後自動刪除）

---

## 安裝

**環境需求：Python 3.10 或以上**（3.10 / 3.11 / 3.12 / 3.13 皆可執行；本專案開發與實測環境為 Python 3.13）。使用 3.10 以下的版本會因語法不支援而無法啟動。

```bash
pip install openpyxl pandas openai python-dotenv json-repair
```

## 建立 `.env` 檔案，填入 API Key
- 本工具支援任何 **OpenAI 相容 API**，推薦使用[DeepSeek 官方](https://platform.deepseek.com/)的deepseek-flash模型
- 複製 `.env.example` 為 `.env`，填入你的設定。支援兩種格式，以下為範本：

### 多 API 格式（可選）

```env
# 主 API（預設並發=100）
API1_TYPE=main
# API1_PARALLEL_LIMIT=100   <- 選填：取消註解即可自訂並發數
API1_MODEL_PROVIDER=Deepseek
API1_MODEL=deepseek-flash
API1_API_KEY=sk-your_api_key_here
API1_BASE_URL=https://api.deepseek.com/

# 副 API（預設並發=1，建議用作免費API）
API2_TYPE=fallback
# API2_PARALLEL_LIMIT=1    <- 選填：取消註解即可自訂並發數
API2_MODEL_PROVIDER=nvidia
API2_MODEL=deepseek-ai/deepseek-v4-flash
API2_API_KEY=nvapi-your_free_key_here
API2_BASE_URL=https://integrate.api.nvidia.com/v1
```

### 舊版單一 API 格式（可選）
```env
API_KEY=sk-your_api_key_here
MODEL_PROVIDER=Deepseek
MODEL=deepseek-flash
BASE_URL=https://api.deepseek.com/
```
---

## 使用方法

### 使用方法：主翻譯
將翻譯目標 Excel 與術語庫（選填）放入 `workplace/`，再執行 `batch_translate.py`。

### 互動步驟（主翻譯）

```
Step 1：選擇目標 Excel    → 從 workplace/ 下列出所有 .xlsx 檔案
Step 2：選擇術語庫        → 可選 Excel 術語庫、自動從目標 Excel 萃取（`name`／`manual` 工作表）、或跳過（只有 ADD_LIST 硬編碼術語）
Step 3：選擇工作表        → 單選或多選
Step 4：選擇翻譯範圍      → 全部未翻譯 / 前 N 條測試 / 指定行數
Step 5：確認執行          → 開始翻譯（模板比對 → LLM 批次翻譯 → 術語強制後處理），完成後輸出翻譯檔與審查報告
```

### 執行後產出（主翻譯）

所有輸出檔案統一放在 `workplace/` 目錄下：

| 檔案 | 說明 |
|------|------|
| `{來源檔名}_translated_output.xlsx` | **翻譯完成檔案**，位於 `workplace/` |
| `review_report.xlsx` | **審查報告**（需人工確認的條目），位於 `workplace/` |
| `auto_glossary_for_review.xlsx` | **自動萃取術語庫的待審清單**，位於 `workplace/`（僅在選擇「自動萃取」時產生；沒有萃取到條目時仍會產生空檔） |
| `_checkpoint/` | **中斷續傳暫存目錄**，位於 `workplace/`（翻譯完成後自動刪除） |

### 使用方法：校對
將已翻譯完成的 Excel 放入 `workplace/` 後執行 `batch_proofread.py`。
> ⚠️ **注意**：校對管線支援**多工作表**處理（各階段內並行，階段間同步）。

### 互動步驟（校對）

```
Step 0：選擇校對模式      → [1] 完整校對 / [2] 快速校對（只做重譯修正）
Step 1：選擇目標 Excel    → 從 `workplace/` 列出所有 `*.xlsx`
Step 2：選擇術語庫（必選） → 可選 Excel 或自動萃取（校對必須提供術語庫，不可跳過）
Step 3：選擇工作表        → 選擇要校對的工作表
Step 4：選擇校對範圍      → 選取要校對的條目範圍
Step 5：確認執行          → 開始校對（Phase 2→3→4a→4b→報告）
```

### 執行後產出（校對）
| 檔案 | 說明                                      |
|------|-----------------------------------------|
| `{來源檔名}_proofread_output.xlsx` | **校對後的完整 Excel**                        |
|`proofread_report.xlsx` | **校對摘要報告**（Type1/Type2 彩色標記）            |
| `review_report_proofread.xlsx` | **重譯保護審查明細**                            |
| `auto_glossary_for_review.xlsx` | **自動萃取術語庫的待審清單**，位於 `workplace/`（僅在選擇「自動萃取」時產生；沒有萃取到條目時仍會產生空檔） |
| `_proofread_checkpoint/` | **中斷續傳暫存目錄**，位於 `workplace/`（校對完成後自動刪除） |
| `{來源檔名}_quick_proofread_output.xlsx` | **快速校對後的完整 Excel**（僅重譯修正） |
| `quick_proofread_report.xlsx` | **快速校對審查報告**（僅四類機械問題、四色標記） |
| `_quick_checkpoint/` | **快速校對續傳暫存目錄**，位於 `workplace/`（完成後自動刪除） |

---

### 中斷續傳

若翻譯/校對過程中因網路斷線、API 錯誤、或人手中斷（Ctrl+C），腳本支援從中斷處繼續：

**續傳流程：**
1. 重新執行 `batch_translate.py`/`batch_proofread.py`
2. 腳本會自動掃描 `workplace/_checkpoint/session.json`（主翻譯）、`workplace/_proofread_checkpoint/session.json`（完整校對）或 `workplace/_quick_checkpoint/session.json`（快速校對），偵測未完成的進度
3. 顯示上次的翻譯設定摘要（Excel、工作表、進度）
4. 詢問是否繼續上次的進度：
5. 選擇 `Y` → 自動載入術語庫設定、還原已翻譯/校對條目，從中斷處繼續
6. 選擇 `n` → 清除舊暫存，進入全新翻譯流程

> ⚠️ **注意**：中斷續傳功能**能確保保存絕大部分已翻譯進度，但並非 100%**。極端情況下（如突然關機），最近 1-2 批已完成但尚未寫入暫存的翻譯可能無法還原。
---

## 術語庫與自訂詞典

### Excel 術語庫

直接使用與翻譯目標相同格式的 Excel，欄位順序：
`english / translation / category / sub_category / source / notes / wiki_url`

只取 `translation` 欄位有值的行作為術語條目。

### 內建 ADD_LIST

可在 `glossary.py` 中直接寫入硬編碼術語（衝突時優先於 Excel）：

```python
ADD_LIST: dict[str, str] = {
    "Old School RuneScape": "Old School RuneScape",
    "OSRS": "OSRS",
    "RuneLite": "RuneLite",
    "RuneLingual": "RuneLingual",
    # 可自行新增，例如：
    # "Saradomin": "薩拉多明",
}
```

### 內建 IGNORE_LIST

部分容易誤譯的普通名詞已在 `glossary.py` 的 `IGNORE_LIST` 中預先排除，避免 AI 錯誤地將它們當作術語處理。

**範例（位於 `glossary.py` 中）：**

```python
IGNORE_LIST: set[str] = {
    "Toolkit", "Vial", "Bones", "Book", "Man", "Run", "Ash",
    "Red", "Will", "Art", "Smith", "Gem", "Woman", "Tree",
    "Shadow", "Rock", "Letter", "Jug", "Egg",
    # ... 完整清單請見 glossary.py
}
```

**自行新增指引：**
- 任何你不想被當作術語檢查的普通英文單詞都可以加入
- 加入後會同時匹配**單數/複數/大小寫**形態（例如加入 `Egg` 會自動忽略 `Eggs`、`EGGS`；反之清單中寫複數如 `Cages`，也會一併忽略單數 `Cage`）。`-ses` 結尾的字本身有歧義（例如 `buses`→`bus`，而 `bus` 自己會歸一成 `bu`），這類字請在術語庫與清單中使用相同形態
- 已存在於 Excel 術語庫中的同名條目也會被一併忽略
- **不要加入專有名詞**（人名、地名、種族名等）：清單內的詞會在載入術語庫時被永久過濾，該術語不會進入提示詞，術語強制與快速校對也檢查不到

---

## 模板比對（tm_matcher）

模板比對功能可用作自訂正則模板讓腳本自動填入翻譯（如 `"Talk to (.+?)\."` → `"与{0}对话。"`），減少 API 調用次數。有興趣的使用者可以參考 `tm_matcher.py` 中的說明了解語法。

填值規則：`{}` 依出現順序對應 `{0}{1}…`（第一個 `{}` 是 `{0}`），也可直接寫 `{0}{1}` 自行安排語序；若模板填不完整（佔位符比捕獲到的參數多，或索引越界），該模板**不會被套用**，該條目改交給 AI 翻譯（不會把字面 `{2}` 寫進譯文）。

---

## 常數設定

以下常數位於 `api_config.py` 和 `llm_translator.py`，**除非你很清楚自己在做什麼，否則不建議改動**：

| 常數 | 預設值 | 說明                           |
|------|--------|------------------------------|
| `BATCH_SIZE_LIMIT` | 100 | 每批 API 請求的最大條數 (llm_translator.py)            |
| `MAIN_DEFAULT_LIMIT` | 100 | 主 API 預設並發數（api_config.py）   |
| `FALLBACK_DEFAULT_LIMIT` | 1 | 副 API 預設並發數（api_config.py）   |
| `REQUEST_INTERVAL` | 1 | 同一 API 請求間隔秒數（api_config.py） |
| `API_CONNECT_TIMEOUT` / `API_READ_TIMEOUT` / `API_WRITE_TIMEOUT` / `API_POOL_TIMEOUT` | 60 / 1800 / 300 / 300 | 分項逾時秒數（api_config.py）；SDK 重試已關閉（`max_retries=0`），由程式自己的 3 次重試處理 |
| `PERMANENT_DISABLE_AFTER` | 2 | 累計第 N 次 429 後永久停用該 API（api_config.py）；池中只剩一個可用 API 時會動態放寬（見下） |
| `SINGLE_API_EXTRA_429_TOLERANCE` | 3 | 池中「只剩一個可用 API」時額外容忍的 429 次數：門檻 = 使用者設定值 + 3（預設 5），其他 API 回來即恢復原值（可逆，不會覆蓋你調高或調低的值） |
| `OVER_RETURN_TOLERANCE` | 1.2 | 回傳條數超過批次條數 1.2 倍時，該批次視為失敗並重試（api_config.py） |

> 429 判定範圍：429 狀態碼、訊息中獨立的 `429`、`rate limit` 系列寫法（`rate limit` / `rate_limit` / `rate-limit` / `ratelimit`），以及 `too many requests`、`quota exceeded`。判定為 429 時會**重新進入 60 秒冷卻**（每次都會重新計時），累計到門檻才永久停用該 API。

> 調整並發數時請注意：可在 `.env` 中為個別 API 設 `APIx_PARALLEL_LIMIT`，或修改 `api_config.py` 中的類別預設值。數值過高可能觸發 API 端點的 Rate Limit。預設主 API=100、副 API=1；若遇到 429，請調降並發或增加 API Key。

---

## 效能參考 (v0.4.6版本更新後)

### DeepSeek-V4.1-Flash-1017 谷時實測數據（緩存 0.02 RMB/輸入 1 RMB/輸出 4 RMB）

| 翻譯數量    | 平均成本（主翻譯）     | 主翻譯耗時            | 平均成本（校對） | 校對耗時 |
|---------|---------------|------------------|----------|------|
| 1000 條  | ~1 RMB        | 88s              | ~1 RMB   | 142s |
| 5000 條  | 1-2 RMB       | 312s             | —        | —    |

---

## 疑難排解

### API 相關

**Q：執行後出現 `ImportError: No module named 'openai'`**
<br>A：尚未安裝依賴，執行 `pip install openai pandas openpyxl python-dotenv json-repair`。

**Q：啟動後出現 `no available API`**
<br>A：`.env` 尚未建立或設定無效（缺少 API_KEY / MODEL / BASE_URL）。複製 `.env.example` 為 `.env`，填入 API Key 後再執行；腳本也會印出各 API 缺少欄位的警告。

**Q：API 請求一直失敗或超時**
<br>A：檢查 `.env` 中的 `BASE_URL` 和 `MODEL` 是否正確。如果是 OpenRouter，確認帳戶餘額是否足夠。也可嘗試調低主/副 API 的並發數來降低 Rate Limit 風險。

**Q：終端機一直出現「第 N 次 429，冷卻 60 秒」**
<br>A：這是正常機制：每次 429 都會重新冷卻 60 秒，冷卻期間該 API 的任務會自動排回佇列、由其他 API 接手。累計次數達到門檻（預設 2 次；只剩一個可用 API 時自動放寬為 5 次）才會永久停用該 API。若頻繁發生，請降低並發數或增加一組 API Key。

**Q：出現「所有 API 已永久停用」**
<br>A：所有設定的 API 都因累計 429 達到門檻而停用。已翻譯的進度會保留，檢查 API Key／額度後重新執行即可續傳。

### 術語相關

**Q：審查報告中有大量我沒聽過的術語 issue**
<br>A：可能是普通名詞被誤當作術語。檢查你的 Excel 術語庫是否有不該出現的條目，或將該單詞加入 `glossary.py` 的 `IGNORE_LIST`。

**Q：我加入了術語但 AI 沒有使用**
<br>A：檢查術語庫 Excel 中 `translation` 欄位是否有值（只有該欄位非空的列才會被載入）。若來自 `ADD_LIST`，確認拼寫和大小寫是否完全一致。

**Q：加了 `Egg` 之後，為什麼 `Eggs` 還是被標記？**
<br>A：確認你的 `glossary.py` 使用的是最新版本，`IGNORE_LIST` 已改為歸一化匹配（加入 `Egg` 即可過濾 `Eggs`、`EGGS` 等形態）。Excel 術語庫中的同名條目在載入時也會一併被過濾，不需要手動移除。

**Q：為什麼術語 `White wolf` 會命中 "white wolves"？**
<br>A：術語匹配支援規則式複數形（含不規則複數與單複數同形），因此 `wolf`/`wolves`、`person`/`people`、`mouse`/`mice` 這類都會命中；同時也要求詞界（`Black 2h sword` 不會命中 "...swordsman"、`Attack mix(2)` 不會命中 "superattack mix(2)"）。單字術語的複數回退與多字詞術語的最後一詞共用同一套規則。

### 檔案相關

**Q：翻譯輸出沒有產生任何檔案**
<br>A：檢查目標 Excel 中是否已有翻譯（`translation` 欄位非空）。若全部已翻譯，腳本會跳過該工作表。可嘗試選擇「全部未翻譯條目」以外的範圍選項。

**Q：輸出的 `_translated_output.xlsx` 打不開**
<br>A：確認該檔案未被其他程式（如 Excel）佔用。若仍無法開啟，可能是翻譯過程中發生錯誤，檢查終端機輸出是否有錯誤訊息。

**Q：`_checkpoint/` 目錄沒有被自動刪除**
<br>A：腳本正常完成時會自動刪除。若腳本被強制中斷，`_checkpoint/` 會保留下來，方便下次續傳。手動刪除不會影響功能。

### 中斷續傳相關

**Q：續傳時偵測不到進度**
<br>A：主翻譯依賴 `workplace/_checkpoint/session.json`，完整校對依賴 `workplace/_proofread_checkpoint/session.json`，快速校對依賴 `workplace/_quick_checkpoint/session.json`。搬移檔案時要連同對應的整個目錄一起移動；刪掉 `session.json` 就無法續傳。

**Q：續傳時某個工作表的翻譯遺失**
<br>A：續傳會自動還原已完成工作表的資料。若中斷發生在翻譯過程中，重新執行腳本即可從中斷的工作表繼續。

**Q：續傳後部分翻譯遺失**
<br>A：如「中斷續傳」章節所述，極端情況下（如突然關機）最近 1-2 批翻譯可能無法保存。這是正常現象，重新翻譯遺失的部分即可。

**Q：續傳時目標 Excel 已修改，可以繼續嗎？**
<br>A：續傳依賴記錄的選取索引（`selected_indices`）。如果目標 Excel 內容或條目順序已變更，索引可能對不上，建議選擇「否」開始全新翻譯。

### 除錯相關

**Q：出現 `[APIx] ⚠️ 低翻譯率 1.1%（1/94），已儲存 debug 訊息`**
<br>A：該 API 的翻譯能力不足，94 條中只成功翻譯了 1 條。檢查 `workplace/_debugmessage/` 目錄下的 debug 檔案檢視完整回傳內容，建議更換其他模型。

**Q：出現 `[APIx] ⚠️ API 回傳單一物件而非陣列`**
<br>A：該 API 不支援批次回傳多條翻譯，只回了單一條目。如果多次顯示此訊息，建議從 `.env` 移除或更換模型。

**Q：出現 `JSON 修復成功`**
<br>A：模型回傳的 JSON 格式有微小瑕疵（如缺少逗號或冒號），`json_repair` 已自動修復，不影響翻譯結果。

### 校對相關

**Q：校對時出現 `[APIx] ⚠️ 完成率 40%（4/10）低於 75%，重試`**
<br>A：校對階段的 75% 完成率檢查是正常機制，會在 3 次嘗試內自動重試。若 3 次後仍低，則接受該結果（只有三次全部失敗時才會寫入 debug 訊息）。

**Q：校對報告中 Type1 和 Type2 是什麼？**
<br>A：Type1 是腳本可自動偵測的術語/佔位符/未翻譯問題，在報告中以背景顏色標記。Type2 是 LLM 雙輪評估發現的流暢度問題；只有 (沒問題,沒問題)、(沒問題,輕度)、(輕度,沒問題) 三種組合會跳過，兩輪皆「輕度」仍會送潤色。

**Q：校對可以處理多個工作表嗎？**
<br>A：支援。校對會以階段間同步的方式並行處理多個工作表（各階段內全部工作表並行，階段間同步）。

**Q：快速校對和完整校對差在哪？**
<br>A：快速校對只執行重譯修正（術語/佔位符/未翻譯/空格），不做 P2 流暢度評估與 P3 潤色，速度較快；輸出為 `_quick_proofread_output.xlsx` 與 `quick_proofread_report.xlsx`。

**Q：續傳後有些列的譯文變成空白、又被重新翻譯**
<br>A：檢查點或備份裡的 `nan`／`nat`／`none` 這類缺值標記不再被當成譯文還原（先前版本會把它們寫回工作表，甚至覆寫原有譯文並寫進成品）。這些列會被視為未翻譯，之後任何一次執行都會重新翻譯它們（選「否」只清除續傳進度，並不能避免重譯）；若想保留原本的譯文，請先手動把該格補回再執行。

---

## 授權條款 (License)

本項目採用 GPL v3 授權條款。詳細內容請參閱 [LICENSE](LICENSE) 檔案。

Copyright (c) 2026 lck3141592654
