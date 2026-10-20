# Changelog

## 20/10/2026 v0.4.5 -> v0.4.6

### 中文
- 術語比對新增 TL4 候選索引（純英文術語用倒排索引、含標點或數字的術語用 3-gram 索引），`narrow_relevant_glossary` 冷啟動由 14–16 秒降至 0.42–0.48 秒、`scan_issues` 3,000 列 0.21 秒；主翻譯、重譯、P2 評估、P3 潤色共用同一份索引，不再逐批重建
- 主翻譯只送 `index` 與 `english`，移除 `notes`、`wiki_url` 與「譯文相同請回傳空字串」的要求，同批次 payload 由 16,564 降至 7,253 字元（−56%）
- `progress.json` 改為增量維護（每張工作表只掃一次 checkpoint 目錄），翻譯與 P3 每批完成即落盤；全新翻譯路徑的 Excel 只讀一次（`validate_columns`／`get_frame` 改吃已載入的 frames）；`iterrows` 熱點向量化；審查報告改在 `ExcelWriter` 寫入時直接上色
- 逾時與 429：client 改為分項逾時 connect 60／read 1,800／write 300／pool 300 秒並關閉 SDK 重試（`max_retries=0`），移除四個呼叫點的 per-request `timeout=` 覆蓋；並行請求同時 429 時，冷卻中的重複 429 只刷新冷卻、不再累計 strike
- 主 API 預設並發 10 → 100（`MAIN_DEFAULT_LIMIT`）；沒有在 `.env` 設定 `APIx_PARALLEL_LIMIT` 的主 API 現在預設用 100，`.env.example` 與 README 兩版同步更新
- 移除 `enforcer.py`／`llm_translator.py`／`proofreader.py` 未使用的 `API_TIMEOUT` import
- 簡化README.md、README_en.md的內容

### English
- new TL4 candidate index for term matching (inverted index for plain terms, 3-gram index for terms with punctuation or digits); `narrow_relevant_glossary` cold start drops from 14-16s to 0.42-0.48s and `scan_issues` takes 0.21s for 3,000 rows; main translation, retranslation, P2 evaluation and P3 polish share one index instead of rebuilding it per batch
- main translation sends only `index` and `english`, dropping `notes`, `wiki_url` and the "return an empty string for unchanged translations" instruction; batch payload shrinks from 16,564 to 7,253 characters (-56%)
- `progress.json` is maintained incrementally (one checkpoint-directory scan per sheet) and translation/P3 persist after every batch; a fresh translation run reads the Excel file only once (`validate_columns` / `get_frame` reuse the loaded frames); `iterrows` hot spots vectorised; the review report is coloured while writing through `ExcelWriter`
- Timeouts and 429: the client now uses per-phase timeouts of connect 60 / read 1,800 / write 300 / pool 300 seconds with SDK retries disabled (`max_retries=0`), and the per-request `timeout=` overrides at four call sites are removed; duplicate 429s received while cooling down only refresh the cooldown instead of adding strikes
- Main API default concurrency 10 -> 100 (`MAIN_DEFAULT_LIMIT`); main APIs without `APIx_PARALLEL_LIMIT` in `.env` now default to 100, with `.env.example` and both READMEs updated accordingly
- removed unused `API_TIMEOUT` imports from `enforcer.py` / `llm_translator.py` / `proofreader.py`
- Simplified the content of README.md and README_en.md

## 6/10/2026 v0.4.4 -> v0.4.5

### 中文
- 加快大表掃描：術語閘門改用安全上界（`filter_relevant_terms`），並新增 `narrow_relevant_glossary` 先收斂真正命中的術語，移除「術語數 × 全文長度」的逐條全文掃描；178 KB 由 34.6 秒降至 0.2 秒，1,000 列 `scan_issues` 由 4.33 秒降至 0.95 秒
- 修正多字詞術語與複數匹配：加入詞界檢查與規則式複數形（`_plural_forms`）；修掉 `Attack mix(2)`、`Male h.a.m.`、`Black 2h sword` 等假陽性，新增 `White wolf` / "white wolves"、`person` / "people" 等命中；全語料移除 765 筆誤配、新增 70 筆真複數
- 修正含括號／數字／句點／連字號的術語永遠匹配不到（真庫 4,043 條，改走精確比對）
- 修正 `normalize_term` 的 `-es` 規則並補上 `-o`（`Buses`→bus、`Bones`→bone、`Heroes`→hero）；IGNORE_LIST 現在會連同單複數一起忽略，術語庫 21,332 → 21,328 條
- `keep_original_terms` 改為不分大小寫比對，避免 ADD_LIST 中「譯文＝原文」的術語因大小寫差異被誤判為未翻譯；佔位符檢查改用 `Counter` 比對數量
- 429 判定收斂為單一實作：只認 429、`rate limit` 系列寫法與 `too many requests`／`quota exceeded`，不再使用裸字串 `"rate"`（原本 400 `unable to generate content` 會被誤判成限流並停用 API）；新增「HTTP 200 但 body 是限流物件」的偵測
- 並發池修正：`close()` 不再留下永遠 pending 的 future、close 後 `submit()` 立即拋錯；冷卻在取得 job 後與真正送出請求前各檢查一次；修正冷卻重排誤刪其他 worker 的登記（會讓執行中的 job 遺失、future 永遠 pending）
- 每次 429 都重新進入 60 秒冷卻（原本只有第一次）；只有一個可用 API 時永久停用門檻動態放寬 +3 且可逆；`.env` 的 API 編號跳號不再中斷載入
- 缺值判定全面統一：新增 `_is_na`／`_english_hint`，哨兵字串改為單一來源 `glossary.MISSING_SENTINELS`；`pd.NA`／`NaT` 不再讓提示詞 JSON 崩潰
- 舊檢查點／備份裡的 `nan`／`nat`／`none` 不再被還原（快速校對原本會把哨兵值覆寫原譯文並寫進成品）；模型回寫不再寫入非字串、空白或哨兵值
- P2 評估的 `level` 缺 key／空值／非字串一律視為「严重」（原本 dict／list 會讓整個 P2 中止且不留檢查點）；報告去重涵蓋全部 4 份輸出；檢查點與報告的 `int(key)` 與逐筆迴圈加上防護（單筆損壞不再讓整表失敗）
- 模板 `{}` 依出現順序對應 `{0}{1}…`；填不完整時不再套用、改交給 LLM（不會把字面 `{2}` 寫進譯文）
- Python 3.10 相容：修正 `batch_proofread.py` 的 f-string 同引號嵌套，README 兩版標明最低版本 Python 3.10
- 其它：除錯訊息目錄改由單一函式提供；`run_proofread` 的 `pool.start()` 失敗不再刪掉可續傳的檢查點；`.gitignore` 補上四個 checkpoint 目錄

### English
- Faster large-sheet scans: the term gate now uses a safe upper bound (`filter_relevant_terms`) plus the new `narrow_relevant_glossary` to first narrow down the terms that actually match, removing the per-term full-text scan (178 KB: 34.6s -> 0.2s; `scan_issues` on 1,000 rows: 4.33s -> 0.95s)
- Fixed multi-word term and plural matching: word-boundary checks plus rule-based plural forms (`_plural_forms`); removes false positives such as `Attack mix(2)`, `Male h.a.m.` and `Black 2h sword` and adds matches such as `White wolf` / "white wolves" and `person` / "people" (765 old false matches removed, 70 genuine plurals added across the corpus)
- Fixed terms containing brackets, digits, dots or hyphens never matching (4,043 entries in the real glossary now go through exact matching)
- Fixed the `-es` rule in `normalize_term` and added `-o` (`Buses`->bus, `Bones`->bone, `Heroes`->hero); IGNORE_LIST now ignores singular and plural together, glossary size 21,332 -> 21,328
- `keep_original_terms` is now case-insensitive, so ADD_LIST terms whose translation equals the source are no longer misflagged as untranslated because of letter case; placeholder checking now compares counts via `Counter`
- 429 detection is now a single implementation recognising the 429 status code, the `rate limit` family and `too many requests` / `quota exceeded`; a bare `"rate"` substring is no longer used (a 400 `unable to generate content` used to be misread as rate limiting and disable the API); new detection of "HTTP 200 but body is a rate-limit object"
- Shared pool fixes: `close()` no longer leaves forever-pending futures and `submit()` after close raises immediately; cooling is re-checked after dequeue and again right before the request; fixed a cooling requeue that deleted another worker's registration, which lost the running job and left its future pending forever
- Every 429 now restarts the 60-second cooldown (previously only the first one); when only one usable API remains its disable threshold is relaxed by +3, reversibly; gaps in `.env` API numbering no longer stop loading
- Missing-value checks are now unified: new `_is_na` / `_english_hint`, sentinel strings come from a single `glossary.MISSING_SENTINELS`, and `pd.NA` / `NaT` no longer break prompt JSON
- Sentinel strings (`nan` / `nat` / `none`) in old checkpoints and backups are no longer restored (quick proofread used to overwrite the original translation with a sentinel and write it into the output); model write-back no longer stores non-strings, blanks or sentinels
- A missing / empty / non-string P2 `level` is treated as "severe" (a dict or list used to abort the whole P2 with no checkpoint); report deduplication now covers all 4 outputs; `int(key)` conversions and per-item loops in checkpoints and reports are guarded so one corrupt entry no longer fails the whole sheet
- Template `{}` placeholders map to `{0}{1}...` in order; an incomplete template is no longer applied and goes to the LLM instead (a literal `{2}` is never written into a translation)
- Python 3.10 compatibility: fixed the f-string with nested same quotes in `batch_proofread.py`; both READMEs now state the minimum version Python 3.10
- Misc: the debug-message directory comes from a single helper; `run_proofread` no longer deletes the resumable checkpoint when `pool.start()` fails; `.gitignore` covers four checkpoint directories

## 24/8/2026 v0.4.3 -> v0.4.4

### 中文
- 修正續傳還原不確定性：`load_backup()` 原本以檔名合併 part 檔，而 part 檔名內含隨機 tag（UUID），同一條目同時存在 LLM 批次值與 enforce 重譯修正值時，還原值會由 tag 字串排序決定（約 50% 機率還原到較舊值）；改為以檔案寫入時間（mtime）排序，後寫入者必定勝出，翻譯續傳不再遺失重譯修正
- 修正翻譯路徑把無法識別的回傳當成成功：模型回傳的 dict 沒有包裝 key 也無 index/translation 時，原本直接包成陣列視為完成（該批 0 進度照常結束），現在與其他 LLM 路徑一致，視為失敗走 3 次重試；並補上最終結構守衛，非 JSON 陣列（含包裝值為字串等）一律重試
- 加快大表掃描：`scan_issues()` 每行只建立一次術語匹配上下文（`build_relevance_context`），由 `check_glossary_usage` / `_find_matched_spans` 傳給 `find_term_spans` 共用，避免每行 × 每術語重建；實測 2,000 行 × 3,000 術語掃描由約 30 秒降至約 3 秒（約 10 倍），10 萬列工作表單次掃描由約 25 分鐘降至約 2.5 分鐘級別

### English
- Fixed resume-restore nondeterminism: `load_backup()` merged part files by filename, but part filenames contain a random UUID tag; when the same index exists in both LLM batch parts and enforce-fix parts, the restored value was decided by tag string order (about a 50% chance of restoring an older value). Parts are now merged by file write time (mtime), so later writes always win and translation resume no longer loses retry-round fixes
- Fixed the translation path treating unrecognizable responses as success: a dict without wrapper keys or index/translation was wrapped into an array and counted as a completed batch (0 progress); it is now treated as a failure with 3 retry attempts, consistent with the other LLM paths, plus a final guard that retries any non-array response (including wrapped string values)
- Faster large-sheet scanning: `scan_issues()` now builds the term-matching context (`build_relevance_context`) once per row and shares it via `check_glossary_usage` / `_find_matched_spans` into `find_term_spans`, avoiding per-term rebuilds; measured ~10x speedup (2,000 rows x 3,000 terms: ~30s to ~3s per scan), bringing a 100k-row sheet scan from ~25 min down to ~2.5 min class

## 12/8/2026 v0.4.2 -> v0.4.3

### 中文
- 修正多字詞術語（3 字以上詞組）位置比對漏配對：首詞之後接不上時改為繼續尋找下一次出現，不再提前回傳找不到；修復 `get_relevant_glossary()` 漏掉長術語、導致術語強制檢查不生效的問題（glossary.py）
- 修正模板比對的 `{}` 佔位符：填值前將 `{}` 正規化為 `{0}`，與文件宣稱的「相容 {0} 也相容 {}」一致（tm_matcher.py）
- 修正 `PARALLEL_LIMIT` 設為 0 或負數時，並發池建立 0 個 worker、任務無限卡死的問題：解析時驗證必須 ≥ 1，無效值印警告並退回類別預設（api_config.py）
- 移除未使用的 import（enforcer.py、llm_translator.py 的 `normalize_term`；proofreader.py 函式內的 `import re`）
- glossary.py：`parts` 提前初始化，避免後續使用時變數未定義
- batch_translate.py：`step4_choose_sheet_mode` 迴圈後加入不可達防呆，確保所有路徑都明確回傳
- proofreader.py：`generate_reports` / `generate_quick_reports` 參數與相關區域變數補上 `list[pd.DataFrame]` 型別註解

### English
- Fixed multi-word term (3+ words) positional matching: the search now continues at the next occurrence when the first word is not followed by the rest of the term, instead of returning not found; fixes `get_relevant_glossary()` dropping long terms so the term enforcement never applied (glossary.py)
- Fixed `{}` placeholder support in template matching: `{}` is normalized to `{0}` before substitution, matching the documented behavior (tm_matcher.py)
- Fixed an infinite hang when `PARALLEL_LIMIT` is 0 or negative: the parser now validates >= 1 and falls back to the category default with a warning (api_config.py)
- Removed unused imports (`normalize_term` in enforcer.py / llm_translator.py; function-local `import re` in proofreader.py)
- glossary.py initializes `parts` early to avoid a potential undefined-variable reference
- batch_translate.py adds an unreachable guard after the selection loop so every path returns explicitly
- proofreader.py adds `list[pd.DataFrame]` type annotations to the report generators and related locals

## 10/8/2026 v0.4.1 -> v0.4.2

### 中文
- 重譯路徑支援 dict 回傳：新增 `_extract_retry_results()`，統一處理包裝 key（translations/translated/data/results）、巢狀結構與單一物件，與主翻譯/評估/潤色路徑一致
- 空回傳視為失敗：四條 LLM 路徑（翻譯/重譯/評估/潤色）在回傳為空或 index 全數不匹配時進入 3 輪重試，不再靜默當作完成 0 條
- 主翻譯「僅測試前 N 條」拒絕負數輸入，套用端另加防呆
- `.env` 改以腳本所在目錄載入（api_config / llm_translator / enforcer），不再依賴啟動工作目錄
- 互動選單與校對的 `pd.ExcelFile` 一律改用 `with` 關閉，避免 Windows 檔鎖
- 未翻譯統計改與 `is_missing_translation()` 同口徑
- 提示詞中的 NaN 空值改為 null（`_json_safe`），避免送出無效 JSON
- 移除未使用的 `_is_plural_like()`
- 校對續傳強化：
  - 拒絕續傳 / 模式切換時一併清空共用 `_checkpoint`，避免殘留 enforce checkpoint 污染下一次執行
  - P2 評估改為批次級續傳（`phase2_progress.json` + part 檔還原），中斷後只重送未完成批次
  - P3 潤色記錄 `failed_indices`，失敗條目在下一輪與續傳都會重送
  - P3 checkpoint 改為累積合併各輪結果，續傳不再遺失先前輪次的潤色成果
- 全 API 永久停用時 `submit()` 立即拋錯，兩個 CLI 頂層顯示友善提示後正常結束（不再卡死）

### English
- Retry path now handles dict responses (wrapped keys / nested structures / single objects) consistently with the other LLM paths
- Empty or fully-filtered responses are treated as failures on all four LLM paths (3-attempt retry), no longer silently completing with 0 items
- "First N" translation range rejects negative input, with an apply-side guard
- .env is now loaded from the script directory (api_config / llm_translator / enforcer) instead of the working directory
- Interactive `pd.ExcelFile` usage now uses `with` (batch_translate / batch_proofread) to avoid Windows file locks
- Untranslated counts now use `is_missing_translation()` consistently
- NaN values in prompts are converted to null (`_json_safe`) to avoid invalid JSON
- Removed unused `_is_plural_like()`
- Proofreading resume hardening:
  - Declining resume / mode switch now clears the shared `_checkpoint` to avoid stale enforce checkpoints
  - P2 evaluation resumes at batch level (`phase2_progress.json` + part-file restore), only re-sending unfinished batches
  - P3 polish records `failed_indices`, so failed entries are re-sent in later rounds and on resume
  - P3 checkpoint now merges results across rounds, so earlier rounds are not lost on resume
- `submit()` raises immediately when all APIs are permanently disabled; both CLIs show a friendly message and exit cleanly (no more hang)

## 9/8/2026 v0.4.0 -> v0.4.1

### 中文
- 修正術語庫混入 'nan' 條目：name/manual 或術語庫工作表中 english 為空的列，原本會因 str(NaN) 變成 'nan' 而塞進術語庫（例如 'nan' → '无'）；現在會跳過空 english 列；同時修正 pd.ExcelFile 未關閉導致 Windows 下檔案被鎖的問題
- 修正空格檢查誤報中英混排空格：「空格後不應直接接中文標點符號」與「中文標點符號後不應有空格」的字元類別原本包含所有中文字（\u4e00-\u9fff），導致「屠龙者 I 任务」這類英文與中文之間的空格被誤判為標點問題；改為只匹配中文標點後，中英混排空格保留且不再誤報，真正的標點空格（如「任务 。」）仍會標記並修正
- 修正空格修正破壞英文文本：`fix_space_issues()` / `check_space_issues()` 不再刪除英文標點（. , ! ? ; :）前後空格，只處理英文半形括號內側空格與中文語境下的括號外側空格（例如 `Mr. Smith. Hello` 不再變成 `MrSmithHello`）
- 修正術語歸一化誤報：`normalize_term()` 不再剝離 a/an/the 開頭冠詞；新增 `find_term_spans()` 取代歸一化正則匹配，只接受「術語原樣」或「文字詞是術語的真正複數變形」（boxes→Box、demons→Demon）；News 不再誤配 new、The Face 不再誤配 face、Boxes 不再誤配 box
- 方向性複數匹配：文字詞必須是術語的複數形態（支援 -s/-es/-ies/-ves 與不規則變形），單數詞不會誤配複數術語（bus 不會誤配 Buses）
- 修正 ADD_LIST 同字條目誤報：RuneLingual、OSRS、RuneLite、Old School RuneScape 等「譯文 = 原文」的術語不再被視為未翻譯，由腳本直接填入、不送 LLM
- 修正字串 "nan"/"nat"/"none" 被翻譯階段漏掉：新增 `is_missing_translation()`，翻譯範圍選取、待翻譯清單、未翻譯檢查三處行為一致
- 重譯提示詞不再超 token：`_retry_round()` 每個批次只帶該批次實際出現的術語（批次級），不再把整張工作表的術語塞進提示詞
- 修正複數形態術語被相關性過濾排除：`enforce_async()` 與 `retry_protect()` 的術語篩選改用 `find_term_spans()`，僅以複數形態出現的術語也會納入檢查與重譯
- 效能優化：新增 `build_relevance_context()` 預先建立文本詞集合與複數對照（每張工作表或批次只建立一次），21,000+ 術語大表掃描從約 18 秒降至約 2.5 秒
- 多字詞術語定位修正：以位置比對回傳真實 span，詞與詞之間只能有空白，分散的詞不再誤配（"ape ... atoll" 不誤配 Ape Atoll）
- 修正校對續傳審查記錄只存數量不存內容：`_build_session_data()` 改為存完整 dict 清單，續傳時還原審查記錄
- 修正 P2 部分完成續傳漏報：R2 缺失預設從「没问题」統一改為「严重」，與 `_pad_eval_results()` 一致
- 續傳路徑一致性：翻譯與校對完成後清除 session 檔，避免已完成的執行再次觸發續傳提示；續傳時審查記錄完整還原
- IGNORE_LIST 補齊冠詞形態與裸詞（Goblin、Wall、Rock、Desert、Container、Corpse、Crack 等），維持移除冠詞剝離後的過濾行為與舊版一致

### English
- Fixed 'nan' entries leaking into the glossary: rows with empty english cells in name/manual sheets or glossary workbooks were turned into 'nan' by str(NaN) and added to the glossary (e.g. 'nan' -> 无); empty english rows are now skipped; also fixed pd.ExcelFile handles not being closed, which locked files on Windows
- Fixed space-check false positives on Latin/CJK mixed spacing: the "space before Chinese punctuation" and "space after Chinese punctuation" character classes previously included all CJK characters (\u4e00-\u9fff), so spaces between Latin and Chinese text (e.g. 屠龙者 I 任务) were wrongly flagged; the classes now match Chinese punctuation only, mixed spacing is preserved, and real punctuation spacing (e.g. 任务 。) is still flagged and fixed
- Fixed the space-correction step destroying English text: `fix_space_issues()` / `check_space_issues()` no longer strip spaces around English punctuation (. , ! ? ; :); they only remove spaces inside English parentheses and around parentheses in Chinese context (e.g. `Mr. Smith. Hello` is no longer mangled into `MrSmithHello`)
- Fixed term-normalization false positives: `normalize_term()` no longer strips leading a/an/the; new `find_term_spans()` replaces normalization-based matching and only accepts an exact term or a genuine plural of it (boxes→Box, demons→Demon); News no longer matches new, The Face no longer matches face, Boxes no longer matches box
- Directional plural matching: a text word must be an actual plural form of the term (supports -s/-es/-ies/-ves and irregular forms); singular words no longer match plural terms (bus does not match Buses)
- Fixed false untranslated flags for ADD_LIST keep-original entries (RuneLingual, OSRS, RuneLite, Old School RuneScape): filled by the script directly instead of being sent to the LLM
- Fixed literal "nan"/"nat"/"none" strings being skipped by the translation stage: new `is_missing_translation()` unifies range selection, pending-list building and untranslated checks
- Retranslation prompts no longer risk exceeding the token limit: `_retry_round()` includes only the terms that actually appear in each batch instead of the whole sheet glossary
- Fixed plural-only terms being dropped by the relevance filter: `enforce_async()` and `retry_protect()` now filter via `find_term_spans()`, so terms appearing only in plural form are checked and retried
- Performance: new `build_relevance_context()` precomputes the text word set and plural map once per sheet or batch; scanning a 21,000+ term glossary dropped from ~18s to ~2.5s
- Multi-word term spans now point at the real occurrence (words must be adjacent with only whitespace between them); scattered words no longer match (an "ape ... atoll" sentence no longer matches Ape Atoll)
- Proofreading session now stores the full review rows (list of dicts) instead of only a count, and restores them on resume
- Fixed P2 partial-completion under-reporting on resume: missing R2 now defaults to "severe" consistently with `_pad_eval_results()`
- Resume consistency: translation and proofreading now clear the session file after completion so a finished run no longer prompts for resume; review rows are restored fully on resume
- IGNORE_LIST completed with article forms and bare forms (Goblin, Wall, Rock, Desert, Container, Corpse, Crack, etc.) so filtering behavior stays identical to the previous version after removing article stripping

## 8/8/2026 v0.3.2 -> v0.4.0
### 中文
- 新增「快速校對」模式（重譯模式）：只執行重譯修正（術語/佔位符/未翻譯/空格 + 最多 3 輪 LLM 重譯），跳過 P2 流暢度評估與 P3 潤色
  - batch_proofread.py 開頭可選 [1] 完整校對 / [2] 快速校對；使用獨立 checkpoint `_quick_checkpoint` 與續傳
  - 輸出 `{來源}_quick_proofread_output.xlsx` + `quick_proofread_report.xlsx`（僅四類機械問題、四色標記）
- 修正完整校對 P2 因共享批次池 submit 未傳 ctx 而無法執行的 bug
- 重譯階段的 429 處理與主翻譯一致：立刻向外拋出，由共享池執行冷卻/永久停用與批次重排
- 統一「過量回傳」政策：
  - 新增 OVER_RETURN_TOLERANCE = 1.2：回傳數超過批次數 120% 即視為該批次失敗，走 3 輪重試
  - 四條 LLM 路徑（翻譯/重譯/評估/潤色）統一依模型回傳的 index 對號入座，只採用屬於當前批次的 index；跨批次或不存在一律忽略，重複 index 後寫覆蓋
  - 完成率分母統一為 min(成功數, 批次數)/批次數，rate 恆 ≤100%
- 校對與續傳修正：
  - P4a 重譯續傳只在存在 enforce_checkpoint 時還原，且只還原 enforce_tag 對應的 part 檔案
  - P2 不再誤動翻譯 checkpoint（移除 sync_progress 呼叫）
  - 評估回傳不足時以「严重」補位，避免漏報
  - 潤色在第二類問題 ≤3 條時也會執行
  - proofread_report.xlsx 加入 Type1 列（術語/佔位符/未翻譯/空格四色標記）
  - 重譯修正率以同口徑計算（空格問題不再拖低修正率）
- 回傳資料防護：LLM 回傳非 dict 或 index 不屬於當前批次時跳過並警告，不再整批失敗
- 字串 "nan"/"nat"/"none" 視為未翻譯
- 互動與輸入：
  - 主翻譯 step4 行數範圍校驗（拒絕負數/反轉/超出總行數）
  - choose_multi 去除重複編號
  - 校對確認畫面顯示「自動萃取」而非 __AUTO__
  - 模式與舊 session 不一致時先詢問是否清除舊進度；無可用 API 時清除殘留 session
  - 目標 Excel 缺少 english/translation 欄位時明確報錯
- 模板比對：模板填入翻譯的條目不再重複送 LLM

### English
- Added "Quick Proofread" mode (retranslation mode): only retranslation fixes (terminology/placeholder/untranslated/spacing + up to 3 LLM rounds), skipping P2 fluency evaluation and P3 polishing
  - batch_proofread.py now offers [1] Full / [2] Quick proofread; isolated `_quick_checkpoint` with resume
  - Outputs `{source}_quick_proofread_output.xlsx` + `quick_proofread_report.xlsx` (four mechanical issue types, color-coded)
- Fixed full proofreading P2 crash caused by a missing ctx in shared pool submit
- Retranslation 429 handling now matches main translation: re-raised immediately, handled by shared pool cooldown/permanent-disable and job requeue
- Unified over-return policy:
  - New OVER_RETURN_TOLERANCE = 1.2: returns exceeding 120% of the batch size are treated as batch failure and go through 3 retry attempts
  - All four LLM paths (translate/retry/eval/polish) map results by the model-returned index, adopting only indices belonging to the current batch; cross-batch or unknown indices are ignored; duplicate indices use last-write-wins
  - Completion rate denominator unified to min(success, batch)/batch, so the rate is always <= 100%
- Proofreading and resume fixes:
  - Enforce resume restores part files only when enforce_checkpoint exists, and only for its enforce_tag
  - P2 no longer touches the translation checkpoint (removed sync_progress call)
  - Eval pads missing items with "severe" to avoid under-reporting
  - Polish now runs even with <= 3 second-category items
  - proofread_report.xlsx now includes Type1 rows (four mechanical types, color-coded)
  - Retranslation correction rate uses a consistent scope (space issues no longer drag it down)
- Response robustness: non-dict results or indices not belonging to the current batch are skipped with a warning instead of failing the whole batch
- String "nan"/"nat"/"none" treated as untranslated
- Interaction and input:
  - Translation range input validated (negative/reversed/out-of-bounds rejected)
  - choose_multi deduplicates repeated numbers
  - Proofread confirmation shows "auto-extract" instead of __AUTO__
  - Mode/session mismatch asks before clearing old progress; leftover quick session is cleaned when no API is available
  - Missing english/translation columns are reported clearly
- Template matching: template-filled entries are no longer re-sent to the LLM

## 7/8/2026 v0.3.1 -> v0.3.2

### 中文
- 新增多工作表並行（翻譯與校對皆支援）：所有工作表在同一階段內並行處理，階段間同步（全部表完成該階段才進下一階段）
- 新增共享批次池（shared_pool.py）：整個執行共用一份 API 配置、並發與 429 冷卻狀態；每個 API 依 parallel_limit 建立多個 worker，批次真正並行處理
- 新增 API_TIMEOUT 統一逾時常數（api_config.py，預設 3600 秒）：主翻譯、校對各階段與重譯共用
- 新增 PERMANENT_DISABLE_AFTER 常數（api_config.py，預設 2）：可調整 429 停用次數
- 重譯逾時修正：固定 60 秒逾時造成慢速 API 誤判 Request timed out.，改為統一逾時
- 純空格問題不再送 LLM：由腳本機械式處理，節省 API 調用
- 術語正則快取：每個術語只編譯一次，大表掃描大幅加速
- 審查報告補齊四色標記：合併報告補上空格問題綠色標記
- checkpoint 全面原子寫入：新增 atomic_write_text helper（.tmp → fsync → rename），session、progress、part、enforce、階段標記、模板、P2、P3 統一使用
- 移除大量死代碼：run_worker_pool、translate_all、enforce 同步包裝與未使用的 import、函數、變數

### English
- Added multi-worksheet parallelism for both translation and proofreading: all sheets run concurrently within each phase, with a phase barrier (all sheets must finish a phase before the next starts)
- Added a shared batch pool (shared_pool.py): one set of API configs, concurrency and 429 state for the whole run; each API spawns parallel_limit workers so batches truly run concurrently
- Added unified API_TIMEOUT constant (api_config.py, default 3600s) shared by translation, proofreading phases and retranslation
- Added PERMANENT_DISABLE_AFTER constant (api_config.py, default 2) to tune the 429 disable threshold
- Fixed retranslation timeout: the fixed 60s timeout caused spurious Request timed out. on slow APIs; now uses the unified timeout
- Space-only issues are no longer sent to the LLM; handled by script preprocessing
- Cached compiled glossary regexes so each term is compiled only once, speeding up large-sheet scanning
- Completed the four-color review report: the merged report now colors space issues green
- Unified atomic checkpoint writes via atomic_write_text (.tmp -> fsync -> rename) for session/progress/part/enforce/phase markers/template/P2/P3 files
- Removed dead code: run_worker_pool, translate_all, enforce sync wrapper and unused imports/functions/variables

## 25/6/2026 v0.3 -> v0.3.1

### 中文
- **新增空格檢查**：`scan_issues()` 加入第四層檢查，檢測六種空格問題（連續多個空格、中文字間空格、空格+中文/英文標點、中文/英文標點+空格），審查報告以綠色標示
- **新增腳本預處理機制**：在 LLM 重譯前三輪皆先執行機械式修正，減少 API 調用
  - 單佔位符修正：原文僅含一個 `[]` 時，直接用原文內容覆蓋譯文的 `[]` 內容
  - 空格修正：自動清除六種不合格空格，保留正常合理空格
- **修正純佔位符誤判**：原文去除 `[]` 後僅剩空白或標點符號時，不再視為未翻譯

### English
- **New space check**: Added a fourth layer to `scan_issues()` detecting six types of spacing issues (consecutive spaces, spaces between Chinese characters, space before/after Chinese/English punctuation). Displayed in green in review reports.
- **New script preprocessing**: Applies mechanical fixes before each of the 3 LLM retranslation rounds, reducing API calls
  - Single placeholder fix: when the source has exactly one `[]`, overwrites the translation's `[]` content with the source's
  - Space fix: automatically removes six types of abnormal spaces while preserving legitimate ones
- **Fixed false positive for placeholder-only entries**: Entries where removing all `[]` leaves only whitespace or punctuation are no longer flagged as untranslated

## 24/6/2026 v0.2.2 -> v0.3

### 中文
- 新增校對管線（proofreader.py + batch_proofread.py）：
  - 三階段流程：LLM 雙輪評估（Phase 2）→ LLM 潤色（Phase 3）→ 重譯保護（Phase 4a）+ 模板校正（Phase 4b）
  - Phase 2 混合 R1+R2 批次池：R1 和 R2 的所有批次混合在一個 worker pool 中處理，而非順序執行兩輪
  - 雙輪交叉驗證：僅有 (沒問題,沒問題)、(沒問題,輕度)、(輕度,沒問題) 三種組合才跳過，其餘（含兩輪皆輕度）列為第二類問題條目
  - 75% 完成率閾值：所有 API 回傳（含正常路徑與 json_repair 修復路徑）皆檢查，低於 75% 即重試，最高優先級
  - 互動式 CLI，支援續傳、debug 訊息檢視、備份與雙報告輸出（proofread_report.xlsx + review_report_proofread.xlsx）
  - 獨立 checkpoint 目錄（_proofread_checkpoint/），不與主翻譯干擾
- ⚠️ **已知限制**：校對管線目前只支援**單工作表**處理，不支援多工作表。
- 移除 response_format：
  - `_translate_batch()`（llm_translator.py）移除 `response_format={"type": "json_object"}`
  - `_evaluate_batch()` 和 `_polish_batch()`（proofreader.py）移除 `response_format={"type": "json_object"}`
  - 所有提示詞改為直接要求「原始 JSON 阵列（不要使用 ```json 代码块包裹）」
  - 保留智慧 JSON 提取（方法一/二）和 json_repair 修復作為後備保障
- 共用 `run_worker_pool()` 提取至 llm_translator.py，供翻譯與校對共用
- 擴充內建術語庫（ADD_LIST）與排除清單（IGNORE_LIST）條目

### English
- Added proofreading pipeline (proofreader.py + batch_proofread.py):
  - Three-phase flow: LLM Dual-round Evaluation (Phase 2) → LLM Polish (Phase 3) → Retry Protect (Phase 4a) + Template Correction (Phase 4b)
  - Phase 2 mixed R1+R2 batch pool: all R1 and R2 batches processed in one worker pool instead of sequential passes
  - Dual-round cross-validation: skips only for (acceptable, acceptable), (acceptable, mild) and (mild, acceptable); all others (including mild + mild) are flagged as category 2
  - 75% completion rate threshold: checked on all API returns (normal path + json_repair path), triggers retry below 75%, highest priority
  - Interactive CLI with resume, debug message review, backup, and dual report output (proofread_report.xlsx + review_report_proofread.xlsx)
  - Isolated checkpoint directory (_proofread_checkpoint/) independent from main translation
- ⚠️ **Known limitation**: The proofreading pipeline currently only supports **single worksheet** processing. Multi-sheet support is not yet implemented.
- Removed response_format:
  - `_translate_batch()` (llm_translator.py) removed `response_format={"type": "json_object"}`
  - `_evaluate_batch()` and `_polish_batch()` (proofreader.py) removed `response_format={"type": "json_object"}`
  - All prompts now directly require "raw JSON array (do not use ```json code blocks)"
  - Retained smart JSON extraction (method 1/2) and json_repair as fallbacks
- Extracted shared `run_worker_pool()` to llm_translator.py, used by both translation and proofreading
- Expanded built-in term list (ADD_LIST) and ignore list (IGNORE_LIST)

## 20/6/2026 v0.2.1 -> v0.2.2

### 中文
- 修正多工作表翻譯：原始設計已支援，但存在多項 BUG（範圍選擇/輸出/續傳），現已全面修復
  - 一次性選取所有工作表，逐表自訂翻譯範圍，無需逐表重複確認
  - 全部完成後才輸出 _translated_output.xlsx 與 review_report.xlsx
- 斷點續傳全面升級支援多工作表：各工作表擁有獨立檢查點目錄（_checkpoint/{sheet_name}/），中斷後續傳時自動接續未完成的工作表，保留已完成的工作表資料
- 新增重譯中斷續傳：重譯過程（術語強制後處理）若中斷，重新執行後會從中斷的輪次繼續，不浪費 API 調用
- save_session() 改為原子寫入（.tmp → rename），避免中斷導致 session.json 損毀
- 擴充內建術語庫（ADD_LIST）與排除清單（IGNORE_LIST）條目

### English
- Fixed multi-sheet translation: originally designed but had multiple bugs (range selection, output, resume), now fully resolved
  - Select all sheets at once, customize range per sheet, confirm once
  - Output _translated_output.xlsx and review_report.xlsx after all sheets complete
- Upgraded checkpoint resume for multi-sheet: each sheet gets its own checkpoint directory (_checkpoint/{sheet_name}/), resuming automatically continues with incomplete sheets while preserving completed ones
- Added enforce checkpoint resume: if the retranslation phase (enforce) is interrupted, re-running will continue from the interrupted round without wasting API calls
- save_session() now uses atomic write (.tmp → rename) to prevent session.json corruption on interrupt
- Expanded built-in term list (ADD_LIST) and ignore list (IGNORE_LIST)

## 19/6/2026 v0.2 -> v0.2.1

### 中文
- 強化除錯訊息：API 錯誤、JSON 解析失敗、低翻譯率均顯示具體原因，不再靜默吞掉
- 新增智慧 JSON 提取：依序嘗試已知 key 名稱，未命中時自動搜尋巢狀結構中含 index+translation 的陣列
- 新增 API 適用性檢測：單一物件回傳時提示該 API 不適合翻譯任務，建議更換
- 新增低翻譯率 debug 記錄：翻譯率低於 25% 時將完整回傳內容儲存至 workplace/_debugmessage/，可直接在該目錄檢視
- 新增 JSON 修復機制：引入 json_repair 自動修復格式不完整的 JSON 回傳（缺少逗號/冒號等），減少重試次數
- 改善 API 兼容性：Nvidia、opencode、OpenRouter 等多種格式差異現已統一處理
- 新增依賴項：json-repair，執行 pip install json-repair 安裝

### English
- Enhanced debug output: API errors, JSON parse failures, and low translation rates now show detailed diagnostics instead of being silently ignored
- Added smart JSON extraction: tries known key names first, then auto-searches nested structures for arrays containing index+translation fields
- Added API compatibility detection: alerts when an API returns single objects instead of arrays, recommending replacement
- Added low-rate debug logging: saves full API response to workplace/_debugmessage/ when translation rate is below 25% for direct inspection
- Added JSON repair: uses json_repair to fix malformed JSON responses (missing commas/colons, etc.), reducing retries
- Improved API compatibility: unified handling of format differences across Nvidia, opencode, OpenRouter, and other providers
- New dependency: json-repair — run pip install json-repair to install

## 18/6/2026 v0.1 -> v0.2

### 中文
- 新增多 API Key 並行作業，自動輪流分配任務並處理 429 限流
- 新增斷點續傳，翻譯中斷後重新執行腳本即可從中斷處繼續
- 新增自動萃取術語庫，從目標 Excel 的 name/manual 工作表自動篩選，無需額外準備
- 統一 workplace/ 工作目錄，所有輸入輸出集中管理，簡化操作流程
- 重譯功能擴充為三層檢查：術語比對、佔位符誤翻檢測、翻譯失敗檢測
- 修正術語誤報：同一句內含重疊字串時（如「Lord Hosidius」與「Hosidius」），長術語翻譯正確即不再對短術語誤報
- 強化互動介面：顯示各批次所用 API、細化重譯統計數據、補全各階段時間戳
- 擴充內建術語庫（ADD_LIST）與排除清單（IGNORE_LIST）條目

### English
- Added multi-API parallel support with round-robin task distribution and automatic 429 rate-limit handling
- Added checkpoint resume — re-run the script to continue from where it left off after interruption
- Added auto-extract glossary from the target Excel's name and manual sheets, no separate file required
- Unified workplace/ directory for all inputs and outputs, simplifying the workflow
- Extended retranslation to three-layer checks: terminology, placeholder corruption, and untranslated entries
- Fixed false positives in review report: when overlapping terms exist (e.g. "Lord Hosidius" vs "Hosidius"), correct longer-term translation no longer triggers a false alarm on the shorter term
- Enhanced interactive UI: per-batch API indicators, detailed retranslation stats, and comprehensive stage timestamps
- Expanded built-in term list (ADD_LIST) and ignore list (IGNORE_LIST)
