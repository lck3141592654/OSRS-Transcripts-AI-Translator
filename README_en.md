# OSRS Transcripts Hybrid Translation Pipeline

<div align="center">

[English](README_en.md) | [中文](README.md)

</div>

## Overview

An AI-powered automated pipeline designed for large-scale game text translation. Scripts can be placed anywhere — no specific project structure required.
> ⚠️ **Beta Notice**: This tool is currently in beta and only supports **Simplified Chinese** translation. Other languages will be supported in the official release.

## Latest Update v0.4.6 (20/10/2026), see [Changelog](Changelog.md) for details
- **Much faster term scanning**: new TL4 candidate index (inverted index for plain terms, 3-gram index for terms with punctuation or digits); `narrow_relevant_glossary` cold start drops from 14-16s to 0.42-0.48s and `scan_issues` takes 0.21s for 3,000 rows; main translation, retranslation, P2 evaluation and P3 polish share one index instead of rebuilding it per batch
- **Leaner prompts**: main translation sends only `index` and `english`, dropping `notes`, `wiki_url` and the "return an empty string for unchanged translations" instruction; batch payload shrinks from 16,564 to 7,253 characters (-56%)
- **Resume and I/O**: `progress.json` is maintained incrementally (one checkpoint-directory scan per sheet) and translation/P3 persist after every batch; a fresh translation run reads the Excel file only once; `iterrows` hot spots vectorised; the review report is coloured while writing
- **Timeouts and 429**: the LLM client now uses per-phase timeouts (connect 60 / read 1,800 / write 300 / pool 300 seconds) with SDK retries disabled, and the per-request `timeout=` overrides at four call sites are removed; duplicate 429s received while cooling down no longer add extra strikes
- **Main API default concurrency is now 100**: `MAIN_DEFAULT_LIMIT` changed from 10 to 100; main APIs without `APIx_PARALLEL_LIMIT` in `.env` now default to 100 (lower the concurrency if you hit 429s)
- **Misc**: removed unused `API_TIMEOUT` imports from `enforcer.py` / `llm_translator.py` / `proofreader.py`
- **Simplified** the content of README.md and README_en.md

### Features

- **Ultra-low cost**: With DeepSeek V4 Flash, translating ~75,000 dialogue entries costs approximately **$2.39 USD**
- **Enforced terminology accuracy**: With script assistance, AI achieves **99%+ accuracy** on proper nouns such as character names, locations, and item names. In contrast, tools like Gemini's Gem feature or Qwen's project feature still have a non-negligible rate of mistranslating proper nouns, even with well-crafted prompts.
- **Interactive operation**: Supports selecting sheets and entry ranges
- **Async concurrency**: Sends multiple API requests simultaneously, significantly reducing translation time
- **Checkpoint resume**: If the translation is interrupted due to network issues or manual interruption (Ctrl+C), simply re-run the script to resume from where it left off — no need to start over
- **Multi-worksheet parallelism**: All worksheets run concurrently within each phase, with phase-barrier synchronization; one shared API batch pool keeps 429 state consistent across the run
- **Auto-review**: Four-layer checks (terminology / placeholders / untranslated / spacing), generates a color-coded review report
- **Multi-API support**: Supports using multiple API keys simultaneously (main/fallback classification), tasks going into one shared queue that whichever API is free pulls from first, and automatic 429 rate-limit handling (every 429 restarts a 60-second cooldown, permanent disable only at the threshold, and the threshold is relaxed automatically when only one usable API remains), maximizing translation throughput
- **Automated proofreading workflow**: LLM dual-round evaluation cross-validation + up to 3 rounds of polishing + retranslation protection, produces color-coded proofreading reports
- **Glossary-based proofreading**: Enforces glossary during proofreading to prevent the LLM from deviating from correct terminology when polishing

### File Structure

| File | Purpose |
|---|---------------------------------|
| `batch_translate.py` | **Main translation script** — interactive entry point |
| `batch_proofread.py` | **Proofreading script** — interactive entry point (Full / Quick) |
| `proofreader.py` | **Proofreading core module** — LLM dual-round evaluation + polishing + retranslation protection (incl. quick/retranslation mode) |
| `glossary.py` | Reads terminology Excel or auto-extracts glossary from target + outputs review file |
| `tm_matcher.py` | **Customizable regex templates**: fill in translations automatically to reduce API calls |
| `llm_translator.py` | **AI batch translation core**, async parallel calls, with smart JSON extraction and json_repair fallback |
| `enforcer.py` | **Four-layer enforcement** (terminology/placeholders/untranslated/spacing), with script preprocessing, auto-correction + color-coded review report |
| `shared_pool.py` | **Shared batch pool** - one set of API configs, concurrency and 429 state for the whole run |
| `api_config.py` | Multi-API configuration parser, supports main/fallback classification, category-level default concurrency, per-API overrides |
| `.env` | API configuration file, supports multi-API numbered format and legacy single-API format |
| `.env.example` | Configuration template |
| `workplace/` | **Working directory** (auto-created), all inputs and outputs stored here |

> The `workplace/` directory contains the following subdirectories:
> - `_checkpoint/` — Translation checkpoint data for resume, each sheet has its own subdirectory (`{sheet_name}/`), auto-deleted after completion
> - `_proofread_checkpoint/` — Proofreading-specific checkpoint, independent from main translation
> - `_proofread_backup/` — Proofreading backup
> - `_debugmessage/` — Low translation rate debug logs (auto-generated, displayed and cleared on next startup)
> - `_quick_checkpoint/` — Quick proofread resume checkpoint (isolated from full proofreading, auto-deleted after completion)

---

## Installation

**Requirement: Python 3.10 or later** (3.10 / 3.11 / 3.12 / 3.13 all work; this project is developed and tested on Python 3.13). Versions below 3.10 will fail to start because of unsupported syntax.

```bash
pip install openpyxl pandas openai python-dotenv json-repair
```

## Create the `.env` File and Add Your API Key
- This tool supports any **OpenAI-compatible API**; the recommended option is the `deepseek-flash` model from [DeepSeek Official](https://platform.deepseek.com/)
- Copy `.env.example` to `.env` and fill in your settings. Two formats are supported; examples below:

### Multi-API Format (Optional)

```env
# Main API (default concurrency=100)
API1_TYPE=main
# API1_PARALLEL_LIMIT=100   <- optional: uncomment to set a custom concurrency
API1_MODEL_PROVIDER=Deepseek
API1_MODEL=deepseek-flash
API1_API_KEY=sk-your_api_key_here
API1_BASE_URL=https://api.deepseek.com/

# Fallback API (default concurrency=1, recommended for free APIs)
API2_TYPE=fallback
# API2_PARALLEL_LIMIT=1    <- optional: uncomment to set a custom concurrency
API2_MODEL_PROVIDER=nvidia
API2_MODEL=deepseek-ai/deepseek-v4-flash
API2_API_KEY=nvapi-your_free_key_here
API2_BASE_URL=https://integrate.api.nvidia.com/v1
```

### Legacy Single-API Format (Optional)
```env
API_KEY=sk-your_api_key_here
MODEL_PROVIDER=Deepseek
MODEL=deepseek-flash
BASE_URL=https://api.deepseek.com/
```
---

## Usage

### Running the Main translation script
Place the translation target Excel and glossary (optional) into the `workplace/` directory, then run `batch_translate.py`.

### Interactive Steps (Translation)

```
Step 1: Select target Excel  → Lists all .xlsx files under workplace/
Step 2: Select glossary      → Choose a glossary Excel, auto-extract from target's name/manual sheets, or skip (built-in ADD_LIST only)
Step 3: Select worksheets    → Single or multiple
Step 4: Select range         → All untranslated / First N for testing / Specify row range
Step 5: Confirm execution      → Starts translation (template matching → LLM batch translation → terminology enforcement), then outputs the translated file and review report
```

### Output After Execution (Translation)

All output files are placed under the `workplace/` directory:

| File | Description |
|------|------|
| `{source_name}_translated_output.xlsx` | **Completed translation file**, located in `workplace/` |
| `review_report.xlsx` | **Review report** (entries needing manual review), located in `workplace/` |
| `auto_glossary_for_review.xlsx` | **Review list for the auto-extracted glossary**, located in `workplace/` (only produced when "auto-extract" is chosen; an empty file is still written when nothing was extracted) |
| `_checkpoint/` | **Checkpoint resume temp directory**, located in `workplace/` (auto-deleted after completion) |

---

### Usage: Proofreading
Place the translated Excel into `workplace/` and run `batch_proofread.py`.
> ⚠️ **Note**: The proofreading pipeline supports **multiple worksheets** (all sheets run concurrently within each phase, with a phase barrier between phases).

### Interactive Steps (Proofreading)

```
Step 0: Select proofread mode → [1] Full / [2] Quick (retranslation fixes only)
Step 1: Select target Excel    → Lists all .xlsx files under workplace/
Step 2: Select glossary (required) → Choose a glossary Excel or auto-extract (proofreading requires a glossary, cannot skip)
Step 3: Select worksheet       → Choose the worksheet to proofread
Step 4: Select proofreading range → Select the entry range to proofread
Step 5: Confirm execution      → Starts proofreading (Phase 2→3→4a→4b→Reports)
```

### Output After Execution (Proofreading)

| File | Description |
|------|---------|
| `{source_name}_proofread_output.xlsx` | **Proofread Excel file** |
| `proofread_report.xlsx` | **Proofreading summary report** (Type1/Type2 color-coded) |
| `review_report_proofread.xlsx` | **Retranslation protection review details** |
| `auto_glossary_for_review.xlsx` | **Review list for the auto-extracted glossary**, located in `workplace/` (only produced when "auto-extract" is chosen; an empty file is still written when nothing was extracted) |
| `_proofread_checkpoint/` | **Checkpoint resume temp directory**, located in `workplace/` (auto-deleted after completion) |
| `{source_name}_quick_proofread_output.xlsx` | **Quick-proofread Excel file** (retranslation fixes only) |
| `quick_proofread_report.xlsx` | **Quick proofread report** (four mechanical issue types, color-coded) |
| `_quick_checkpoint/` | **Quick proofread resume temp directory**, located in `workplace/` (auto-deleted after completion) |

---

### Checkpoint Resume

If the translation/proofreading is interrupted due to network issues, API errors, or manual interruption (Ctrl+C), the script supports resuming from where it left off:

**Resume workflow:**
1. Re-run `batch_translate.py` / `batch_proofread.py`
2. The script automatically scans `workplace/_checkpoint/session.json` (translation), `workplace/_proofread_checkpoint/session.json` (full proofreading) or `workplace/_quick_checkpoint/session.json` (quick proofreading) to detect unfinished progress
3. Displays a summary of the last translation settings (Excel, worksheet, progress)
4. Asks whether to resume:
5. Choose `Y` → automatically loads glossary settings, restores translated/proofread entries, and resumes from the interruption point
6. Choose `n` → clears old checkpoint and starts a fresh translation

> ⚠️ **Note**: The checkpoint resume feature **ensures the vast majority of translated progress is saved, but not 100%**. In extreme cases (such as abrupt shutdown), the last 1-2 batches that were completed but not yet written to checkpoint may not be recoverable.

---

## Glossary & Custom Dictionary

### Excel Glossary

Use an Excel file with the same format as the translation target, column order:
`english / translation / category / sub_category / source / notes / wiki_url`

Only rows with a non-empty `translation` column are loaded as glossary entries.

### Built-in ADD_LIST

Hardcoded terms can be written directly in `glossary.py` (takes priority over Excel when there's a conflict):

```python
ADD_LIST: dict[str, str] = {
    "Old School RuneScape": "Old School RuneScape",
    "OSRS": "OSRS",
    "RuneLite": "RuneLite",
    "RuneLingual": "RuneLingual",
    # Add your own, e.g.:
    # "Saradomin": "萨拉多明",
}
```

### Built-in IGNORE_LIST

Some common nouns that are prone to mistranslation have been pre-excluded in `glossary.py`'s `IGNORE_LIST` to prevent the AI from incorrectly treating them as terminology.

**Example (located in `glossary.py`):**

```python
IGNORE_LIST: set[str] = {
    "Toolkit", "Vial", "Bones", "Book", "Man", "Run", "Ash",
    "Red", "Will", "Art", "Smith", "Gem", "Woman", "Tree",
    "Shadow", "Rock", "Letter", "Jug", "Egg",
    # ... see glossary.py for the full list
}
```

**How to add entries:**
- Any common English word you don't want checked as terminology can be added
- Adding a word also matches its **singular/plural/case** forms (e.g., adding `Egg` automatically filters `Eggs`, `EGGS`; conversely, listing a plural such as `Cages` also ignores the singular `Cage`). Words ending in `-ses` are inherently ambiguous (e.g. `buses`→`bus`, while `bus` itself normalizes to `bu`), so use the same form in both the glossary and the list.
- Entries with the same name already in the Excel glossary will also be ignored
- **Do not add proper nouns** (character names, locations, race names, etc.): entries in this list are filtered out when the glossary loads, so the term never reaches the prompt and neither term enforcement nor quick proofread can check it

---

## Template Matching (tm_matcher)

Template matching can be used to define regex templates that fill in the translation automatically (e.g., `"Talk to (.+?)\."` → `"与{0}对话。"`), reducing the number of API calls. Interested users can refer to the comments in `tm_matcher.py` to learn the syntax.

Fill-in rules: `{}` placeholders map to `{0}{1}...` in order (the first `{}` is `{0}`), and you can also write `{0}{1}` yourself to control word order. If a template cannot be filled completely (more placeholders than captured arguments, or an out-of-range index) it is **not applied** and the entry goes to the AI instead (a literal `{2}` is never written into a translation).

---

## Constants

The following constants are located in `api_config.py` and `llm_translator.py`. **Do not change them unless you know exactly what you're doing:**

| Constant | Default | Description |
|------|--------|------------------------------|
| `BATCH_SIZE_LIMIT` | 100 | Max entries per API request batch (llm_translator.py) |
| `MAIN_DEFAULT_LIMIT` | 100 | Main API default concurrency (api_config.py) |
| `FALLBACK_DEFAULT_LIMIT` | 1 | Fallback API default concurrency (api_config.py) |
| `REQUEST_INTERVAL` | 1 | Interval in seconds between requests from the same API (api_config.py) |
| `API_CONNECT_TIMEOUT` / `API_READ_TIMEOUT` / `API_WRITE_TIMEOUT` / `API_POOL_TIMEOUT` | 60 / 1800 / 300 / 300 | Per-phase timeout values in seconds (api_config.py); SDK retries are disabled (`max_retries=0`) and handled by the script's own 3 retry attempts |
| `PERMANENT_DISABLE_AFTER` | 2 | Permanently disable an API after the Nth 429 (api_config.py); relaxed dynamically when only one usable API remains (see below) |
| `SINGLE_API_EXTRA_429_TOLERANCE` | 3 | Extra 429s tolerated when only one usable API remains: threshold = your configured value + 3 (default 5). It reverts as soon as another API is usable again (reversible; never overrides a value you raised or lowered) |
| `OVER_RETURN_TOLERANCE` | 1.2 | A batch is treated as failed and retried when it returns more than 1.2x the requested entry count (api_config.py) |

> What counts as a 429: the 429 status code, a standalone `429` in the message, the `rate limit` family (`rate limit` / `rate_limit` / `rate-limit` / `ratelimit`), plus `too many requests` and `quota exceeded`. Any detected 429 **restarts** the 60-second cooldown (the timer resets every time) and the API is permanently disabled only after reaching the threshold.

> When adjusting concurrency: you can set `APIx_PARALLEL_LIMIT` in `.env` for individual APIs, or modify the category defaults in `api_config.py`. Setting it too high may trigger API endpoint rate limits. The defaults are main=100 and fallback=1; lower the concurrency if you hit 429s.

---

## Performance Reference (after v0.4.6)

### DeepSeek-V4.1-Flash-1017 Off-Peak Benchmarks (cache 0.02 RMB/input 1 RMB/output 4 RMB)

| Entries | Avg Cost (Translation) | Translation Time | Avg Cost (Proofreading) | Proofreading Time |
|---------|---------------|------------------|----------|------|
| 1,000   | ~1 RMB        | 88s              | ~1 RMB   | 142s |
| 5,000   | 1-2 RMB       | 312s             | —        | —    |

---

## Troubleshooting

### API Related

**Q: After running, `ImportError: No module named 'openai'` appears**
<br>A: Dependencies not installed. Run `pip install openai pandas openpyxl python-dotenv json-repair`.

**Q: `no available API` appears**
<br>A: The `.env` file is missing or its settings are invalid (missing API_KEY / MODEL / BASE_URL). Copy `.env.example` to `.env` and fill in your API Key; the script also prints warnings listing which fields are missing per API.

**Q: API requests keep failing or timing out**
<br>A: Check that `BASE_URL` and `MODEL` in `.env` are correct. If using OpenRouter, verify your account balance is sufficient. You can also try lowering the concurrency of your main/fallback APIs to reduce rate limit risk.

**Q: The terminal keeps printing "429, cooling down for 60 seconds"**
<br>A: That is expected behaviour: every 429 restarts a 60-second cooldown for that API, and its jobs are requeued and picked up by the other APIs in the meantime. The API is permanently disabled only after reaching the threshold (2 by default; relaxed automatically to 5 when only one usable API remains). If it happens often, lower the concurrency or add another API key.

**Q: "All APIs permanently disabled" appears**
<br>A: Every configured API reached the cumulative 429 threshold. Already-translated progress is kept; check your API keys/quota and re-run to resume.

### Terminology Related

**Q: The review report has many terminology issues I don't recognize**
<br>A: Common nouns may be incorrectly treated as terminology. Check your Excel glossary for unintended entries, or add the word to `glossary.py`'s `IGNORE_LIST`.

**Q: I added a term but the AI didn't use it**
<br>A: Check that the `translation` column in your glossary Excel is non-empty (only rows with a non-empty translation column are loaded). For `ADD_LIST` entries, verify spelling and capitalization are exactly correct.

**Q: I added `Egg`, but `Eggs` is still flagged**
<br>A: Make sure your `glossary.py` is the latest version — `IGNORE_LIST` now uses normalized matching (adding `Egg` automatically filters `Eggs`, `EGGS`, etc.). Entries with the same name in the Excel glossary are filtered out at load time too, so there is no need to remove them by hand.

**Q: Why does the term `White wolf` match "white wolves"?**
<br>A: Term matching supports rule-based plural forms (including irregular and unchanged plurals), so `wolf`/`wolves`, `person`/`people` and `mouse`/`mice` all match. Word boundaries are also required (`Black 2h sword` no longer matches "...swordsman", `Attack mix(2)` no longer matches "superattack mix(2)"). The single-word plural fallback and the last word of multi-word terms share one rule set.

### File Related

**Q: No output files were generated after translation**
<br>A: Check whether the target Excel already has translations (non-empty `translation` column). If all entries are already translated, the script skips the worksheet. Try selecting a range option other than "All untranslated".

**Q: The output `_translated_output.xlsx` won't open**
<br>A: Make sure the file isn't locked by another program (e.g., Excel). If it still won't open, an error may have occurred during translation — check the terminal output for error messages.

**Q: The `_checkpoint/` directory wasn't auto-deleted**
<br>A: It's automatically deleted when the script completes normally. If the script was forcibly interrupted, `_checkpoint/` is preserved for future resume. Manually deleting it won't affect functionality.

### Checkpoint Resume Related

**Q: Progress is not detected when resuming**
<br>A: Translation depends on `workplace/_checkpoint/session.json`, full proofreading on `workplace/_proofread_checkpoint/session.json`, and quick proofreading on `workplace/_quick_checkpoint/session.json`. When moving files, move the matching directory as a whole; deleting `session.json` disables resume.

**Q: Translations from a completed sheet are missing after resume**
<br>A: Resume automatically restores data from completed sheets. If the interruption occurred during translation, simply re-run the script to continue from the interrupted sheet.

**Q: Some translations are missing after resuming**
<br>A: As noted in the "Checkpoint Resume" section, in extreme cases (such as abrupt shutdown) the last 1-2 batches may not be saved. This is normal — simply re-translate the missing entries.

**Q: The target Excel has been modified since the last run — can I still resume?**
<br>A: Resume relies on recorded selected indices (`selected_indices`). If the target Excel's content or entry order has changed, the indices may no longer match. It's recommended to choose "No" and start a fresh translation.

### Debug Related

**Q: The message `[APIx] ⚠️ Low translation rate 1.1% (1/94), debug info saved` appears**
<br>A: The API lacks sufficient translation capability — only 1 out of 94 entries was successfully translated. Check the debug files in `workplace/_debugmessage/` to review the full response and consider switching to a different model.

**Q: The message `[APIx] ⚠️ API returned a single object instead of an array` appears**
<br>A: The API does not support batch responses, returning only a single item. If this message appears repeatedly, consider removing it from `.env` or switching to another model.

**Q: The message `JSON repair successful` appears**
<br>A: The model's response contained minor JSON formatting issues (e.g., missing commas or colons). `json_repair` has automatically fixed them — no impact on translation results.

### Proofreading Related

**Q: The message `[APIx] ⚠️ Completion rate 40% (4/10) below 75%, retrying` appears**
<br>A: The 75% completion rate check is a normal mechanism in the proofreading phase. It automatically retries up to 3 attempts. If still below 75% after 3 tries, it accepts the result (debug info is only written when all three attempts fail outright).

**Q: What are Type1 and Type2 in the proofreading report?**
<br>A: Type1 refers to terminology/placeholder/untranslated issues that can be automatically detected by scripts, marked with background colors. Type2 refers to fluency issues found by LLM dual-round evaluation — only (acceptable, acceptable), (acceptable, mild) and (mild, acceptable) are skipped; mild + mild is still sent for polishing.

**Q: Can proofreading handle multiple worksheets?**
<br>A: Yes. Proofreading processes multiple worksheets concurrently within each phase, with a phase barrier between phases.

**Q: What is the difference between Quick and Full proofreading?**
<br>A: Quick proofreading only performs retranslation fixes (terminology/placeholder/untranslated/spacing) and skips P2 fluency evaluation and P3 polishing, so it is faster; outputs are `_quick_proofread_output.xlsx` and `quick_proofread_report.xlsx`.

**Q: After resuming, some cells became empty and were translated again**
<br>A: Missing-value markers such as `nan` / `nat` / `none` in checkpoints or backups are no longer restored as translations (earlier versions could write them back into the sheet, even overwriting an existing translation and carrying them into the output). Those rows are treated as untranslated and any later run will translate them again (choosing "No" only clears the checkpoint — it does not avoid the retranslation); if you want to keep the original text, fill the cell back in manually before running.

---

## License

This project is licensed under GPL v3. See the [LICENSE](LICENSE) file for details.

Copyright (c) 2026 lck3141592654
