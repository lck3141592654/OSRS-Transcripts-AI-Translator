import json, time, re, asyncio, uuid
from pathlib import Path
from datetime import datetime
import pandas as pd
from dotenv import load_dotenv
from glossary import find_term_spans, build_relevance_context, MISSING_SENTINELS
from api_config import (
    API_TIMEOUT, OVER_RETURN_TOLERANCE,
    is_rate_limit_error, is_rate_limit_payload, RateLimitPayloadError,
)

load_dotenv(Path(__file__).resolve().parent / ".env")

BATCH_SIZE_LIMIT = 100

def _is_na(value) -> bool:
    """判斷「值本身是不是 NA 純量」：None／pd.NA／NaN／NaT。

    這裡只回答「這個值是 NA 嗎」，**不做**空字串與哨兵字串
    （"nan"／"nat"／"none"）判定——序列化時空字串與 "nan" 都是合法 JSON 值。

    與 `is_missing_translation` 的 NA 部分同口徑（該函式現在呼叫這裡），
    因此 pd.NA／pd.NaT 這類非 float 的缺值不會再被漏掉。
    對非純量（list／ndarray／Series）`pd.isna` 會回傳陣列，`bool()` 可能
    丟出 ValueError，故以 try/except 包住並回傳 False（不可拋錯）。
    ArithmeticError 是為了 Decimal('sNaN')（pd.isna 會拋 decimal.InvalidOperation）。
    """
    if value is None or value is pd.NA:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError, ArithmeticError):
        return False


def _json_safe(value):
    """把 pandas 空值（含 pd.NA／NaT）轉成 None，避免在提示詞中產生無效 JSON。

    只有真正的 NA 純量轉 None；字串 "nan"、空字串、0 都是合法 JSON 值，
    必須維持原值（舊寫法只認 float NaN，pd.NA／NaT 會原樣回傳，
    `json.dumps` 直接拋 TypeError）。
    """
    if _is_na(value):
        return None
    return value


CHECKPOINT_SUBDIR = "_checkpoint"
PROGRESS_FILE = "progress.json"
SESSION_FILE = "session.json"
# 固定工作目錄名稱：除錯訊息等一律寫到程式所在目錄下的這個資料夾
WORKPLACE_DIR_NAME = "workplace"
DEBUG_MESSAGE_SUBDIR = "_debugmessage"


def get_workplace_dir() -> Path:
    """回傳固定工作目錄（程式所在目錄 / workplace），必要時建立。"""
    wp = Path(__file__).parent / WORKPLACE_DIR_NAME
    wp.mkdir(parents=True, exist_ok=True)
    return wp


def get_debug_message_dir(create: bool = True) -> Path:
    """回傳低翻譯率除錯訊息目錄（工作目錄 / _debugmessage）。

    先前 proofreader.py 與 llm_translator.py 各自寫死同一組路徑常數，
    與 batch_translate.py／batch_proofread.py 的 _get_workplace() 重複；
    統一由這裡提供，日後改動工作目錄名稱只需要改一個地方。

    create=False 時只回傳路徑、不建立目錄：顯示與清理上次 debug 訊息的
    呼叫端要靠 ``exists()`` 判斷有沒有東西要清，若順手建了空目錄，
    就會永遠印出「已清除 debug 訊息」。
    """
    d = get_workplace_dir() / DEBUG_MESSAGE_SUBDIR
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def _timestamp(msg: str):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"  [{now}] {msg}")


SYSTEM_PROMPT_BASE = (
    "你是一个 OSRS（Old School RuneScape）"
    "游戏文本的简体中文翻译专家。\n"
    "请遵守以下规则：\n"
    "1. 只能输出简体中文，除非是占位符、标签或代码\n"
    "2. 保持游戏术语一致\n"
    "3. 保留 HTML/XML 标签（如 <col...>, <br>, </col> 等）\n"
    "4. 保留占位符（如 []內的文字）\n"
    "5. 流暢自然和避免翻譯腔優先，不必完全忠實原文內容\n"
    "6. 每条翻译独立进行，不要合并或省略\n"
    "7. 如果术语表的人名、地名等名詞用'.'分隔，例如'索菲娅.休斯'，你必須使用'.'分隔，不要自行改為'·'或其他方式。"
    "你必須使用'.'分隔，不要自行改為'·'或其他方式。你必須使用'.'分隔，不要自行改為'·'或其他方式。\n"
    "8. 输出格式为原始 JSON 阵列（不要使用 ```json 代码块包裹），每条物件包含 "
    "\"index\" 和 \"translation\" 两个栏位\n"
    "9. 如有 notes、wiki_url 等原始栏位，一并保留在输出物件中\n\n"
    "## 强制术语表（禁止使用官方和社区通常的翻譯，遇到以下英文词必须使用指定的中文翻译）\n"
    "{RELEVANT_GLOSSARY}"
)


def get_relevant_glossary(batch, glossary):
    """只回傳當前批次有出現的術語（含複數歸一化匹配）。"""
    batch_text = " ".join(item.get("english", "") for item in batch).lower()
    _ctx = build_relevance_context(batch_text)
    term_matches = []
    for eng, chn in glossary.items():
        spans = find_term_spans(eng, batch_text, _ctx)
        if spans:
            term_matches.append((eng, chn, spans))
    if not term_matches:
        return []
    term_matches.sort(key=lambda x: len(x[0]), reverse=True)
    relevant = []
    covered_spans = []
    for eng, chn, spans in term_matches:
        all_covered = all(
            any(s >= cs[0] and e <= cs[1] for cs in covered_spans)
            for s, e in spans
        )
        if not all_covered:
            relevant.append((eng, chn))
            covered_spans.extend(spans)
    return relevant

def is_missing_translation(value, english: str | None = None) -> bool:
    """Determine whether a translation cell should be treated as missing.
    String "nan"/"nat"/"none" counts as missing unless the source text is exactly that word.

    任何 `pd.isna` 為 True 的純量（float NaN、np.float64 NaN、NaT、
    pd.NA／pd.NaT）都算缺值；`pd.NA` 的 `str()` 是 "<NA>"，
    既不是空字串也不在 ("nan","nat","none") 內，舊寫法會把它當成「已有譯文」。
    對非純量（list／ndarray／Series）`pd.isna` 會回傳陣列，
    `bool()` 可能丟出 ValueError，故以 try/except 包住並維持原本行為。
    ArithmeticError 是為了 Decimal('sNaN')（pd.isna 會拋 decimal.InvalidOperation）；
    pd.read_excel 不會產生 Decimal，屬防禦性涵蓋，讓這個判定對所有純量都不會拋錯。

    NA 純量的判定集中在本檔的 `_is_na()`：兩者語意不同（這裡還包含空字串與
    哨兵字串），但 NA 部分必須完全一致，改這裡等於同時改動兩者。
    哨兵字串集合來自 `glossary.MISSING_SENTINELS`（單一來源），
    避免 glossary 與本檔各有一份清單而走鐘。
    """
    if _is_na(value):
        return True
    s = str(value).strip()
    if not s:
        return True
    if s.lower() in MISSING_SENTINELS:
        if english is None or str(english).strip().lower() not in MISSING_SENTINELS:
            return True
    return False


def _english_hint(english) -> str | None:
    """把該列原文轉成可傳給 `is_missing_translation(english=)` 的提示值。

    只有在原文本身是非空字串時才有意義；缺值（NaN／None）或非字串時回傳 None，
    避免把 "nan" 這種由 NaN 轉出來的字串誤當成「原文真的是 nan」。
    """
    if isinstance(english, str) and english.strip():
        return english
    return None


def _english_hint_at(df, idx) -> str | None:
    """安全地取某列的原文提示值（`_english_hint(df.at[idx, "english"])` 的防護版）。

    沒有 english 欄、索引不存在、或索引重複（`df.at` 會回 Series）時一律回 None，
    呼叫端就不必各自寫一次欄位／索引檢查——先前 enforcer 的 scan_issues 是把英文欄
    先 str() 再往下傳，NaN 因此變成字串 "nan"，讓哨兵譯文被錯誤豁免（P1 與 P4 對
    同一列判定相反）。所有需要原文提示的地方都應該走這個函式。
    """
    try:
        if "english" in df.columns and idx in df.index:
            return _english_hint(df.at[idx, "english"])
    except (TypeError, ValueError, KeyError):
        pass
    return None


def _sanitize_sheet_name(name: str) -> str:
    """將工作表名稱轉換為安全的目錄名稱。"""
    return re.sub(r'[<>:"/\\|?*]', '_', name)

def _checkpoint_dir(output_dir=None, sheet_name=None) -> Path:
    if output_dir:
        base = Path(output_dir) / CHECKPOINT_SUBDIR
    else:
        base = Path(__file__).parent / CHECKPOINT_SUBDIR
    if sheet_name:
        base = base / _sanitize_sheet_name(sheet_name)
    return base

# ── 進度檔案 ──

def atomic_write_text(path: Path, content: str) -> None:
    """原子寫入文字檔：.tmp → fsync → rename，避免中斷造成檔案損毀。"""
    import os as _os
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    with open(tmp, "ab") as f:
        _os.fsync(f.fileno())
    tmp.replace(path)


def save_progress(output_dir, completed_indices: list, sheet_name=None):
    cd = _checkpoint_dir(output_dir, sheet_name)
    cd.mkdir(parents=True, exist_ok=True)
    data = {
        "completed_count": len(completed_indices),
        "completed_indices": completed_indices,
        "last_saved": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    atomic_write_text(cd / PROGRESS_FILE, json.dumps(data, ensure_ascii=False, indent=2))


# ── Session 檔案 ──

def save_session(output_dir, session_info: dict):
    cd = _checkpoint_dir(output_dir)
    cd.mkdir(parents=True, exist_ok=True)
    atomic_write_text(cd / SESSION_FILE, json.dumps(session_info, ensure_ascii=False, indent=2))


# ── 備份：每批次獨立一個 part 檔案（UUID 避免覆蓋） ──

def save_backup_part(output_dir, session_tag: str, batch_num: int, batch_data: dict, sheet_name=None):
    """原子寫入單一批次的 part 檔案（強制同步磁碟後才 rename）"""
    cd = _checkpoint_dir(output_dir, sheet_name)
    cd.mkdir(parents=True, exist_ok=True)
    atomic_write_text(cd / f"part_{session_tag}_{batch_num:06d}.json", json.dumps(batch_data, ensure_ascii=False))


def _part_sort_key(p: Path) -> tuple:
    """part 檔排序鍵：以寫入時間（mtime_ns）為主，檔名為平手順序。

    stat 失敗（檔案剛好被移除等罕見競態）時退回 (0, 檔名)，不會讓合併崩潰。
    """
    try:
        return (p.stat().st_mtime_ns, p.name)
    except OSError:
        return (0, p.name)


def load_backup(output_dir=None, sheet_name=None) -> dict:
    """掃描所有 part_*.json，以 idx 為鍵合併所有備份資料。

    合併順序以檔案的寫入時間（mtime）為準，後寫入者覆蓋先寫入者。
    原本以檔名排序，而 part 檔名內含隨機 tag（UUID），
    同一 idx 存在於多個 part（LLM 批次值 + enforce 重譯修正值）時，
    還原值會由 tag 字串排序決定（不確定），續傳可能還原到較舊的值；
    改為時間序後，較新的修正（如 enforce 重譯結果）必定勝出。
    """
    cd = _checkpoint_dir(output_dir, sheet_name)
    if not cd.exists():
        return {}
    all_backup: dict = {}
    for f in sorted(cd.glob("part_*.json"), key=_part_sort_key):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            all_backup.update(data)
        except Exception:
            pass
    return all_backup

def sync_progress(output_dir=None, sheet_name=None):
    """從 backup part 檔案反推 progress.json，確保絕對同步"""
    backup = load_backup(output_dir, sheet_name)
    # 備份檔損壞或人為編輯時 key 可能不是數字：單一壞 key 不可讓整批同步失敗
    indices = []
    skipped = 0
    for k in backup:
        try:
            indices.append(int(k))
        except (TypeError, ValueError):
            skipped += 1
    if skipped:
        print(f"  警告：進度同步略過 {skipped} 個非數字 key（備份檔損壞或人為編輯）")
    indices.sort()
    if indices:
        save_progress(output_dir, indices, sheet_name)
    else:
        pp = _checkpoint_dir(output_dir, sheet_name) / PROGRESS_FILE
        if pp.exists():
            pp.unlink()

def delete_checkpoint_files(output_dir, sheet_name=None):
    """清除所有 checkpoint 檔案"""
    cd = _checkpoint_dir(output_dir, sheet_name)
    if cd.exists():
        import shutil
        shutil.rmtree(cd)


# ── 通用 async worker pool ──


# ── LLM API 調用 ──

async def _translate_batch(client, model_name, batch, glossary, api_id="?"):
    """發送單一批次到 LLM API 進行翻譯"""
    relevant = get_relevant_glossary(batch, glossary)
    glossary_str = "\n".join([f"  {e} → {c}" for e, c in relevant]) if relevant else "  无"
    system_prompt = SYSTEM_PROMPT_BASE.replace("{RELEVANT_GLOSSARY}", glossary_str)

    user_content = json.dumps([
        {"index": item["_idx"], "english": item["english"],
         "notes": _json_safe(item.get("notes")), "wiki_url": _json_safe(item.get("wiki_url"))}
        for item in batch
    ], ensure_ascii=False)

    text = ""
    for attempt in range(3):
        try:
            resp = await client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],

                temperature=0.1,
                timeout=API_TIMEOUT,
            )
            # 保持正則操作，不要改為字串，否則會大幅降低翻譯成功率
            text = resp.choices[0].message.content.strip()
            text = re.sub(r"^```(?:json)?\n?", "", text, flags=re.IGNORECASE)
            text = re.sub(r"\n```$", "", text)
            results = json.loads(text)
            # 有些供應商 HTTP 200 但 body 是限流錯誤物件；必須當成 429 往上拋，
            # 否則會一直被當成「格式錯誤」重試，永遠打被限流的端點
            if is_rate_limit_payload(results):
                raise RateLimitPayloadError(f"429 rate limit in response payload: {text[:200]}")

            # 方法一：先試明確的 key 名稱
            if isinstance(results, dict):
                if "translations" in results:
                    results = results["translations"]
                elif "translated" in results:
                    results = results["translated"]
                elif "data" in results:
                    results = results["data"]
                elif "object" in results and isinstance(results["object"], dict) and "translated" in results["object"]:
                    results = results["object"]["translated"]
            # 方法二：如果還是 dict，自動搜尋 index+translation 陣列
            if isinstance(results, dict):
                found = None
                for key, value in results.items():
                    if isinstance(value, list) and len(value) > 0 and isinstance(value[0], dict) \
                            and "index" in value[0] and "translation" in value[0]:
                        found = value
                        break
                if found is None:
                    for key, value in results.items():
                        if isinstance(value, dict):
                            for k2, v2 in value.items():
                                if isinstance(v2, list) and len(v2) > 0 and isinstance(v2[0], dict) \
                                        and "index" in v2[0] and "translation" in v2[0]:
                                    found = v2
                                    break
                if found is not None:
                    results = found
            # ：如果 dict 本身就有 index+translation，視為單一條目 → 拋出例外觸發重試
            if isinstance(results, dict) and "index" in results and "translation" in results:
                print(f"    [{api_id}] ⚠️ API 回傳單一物件而非陣列（嘗試 {attempt + 1}/3）")
                print(f"    [{api_id}] ⚠️ 如果多次顯示此訊息，代表此 API 不適合翻譯任務，建議從 .env 移除或更換模型")
                if attempt < 2:
                    print(f"    [{api_id}] 準備重試...")
                    raise ValueError("single object returned, need array")
                else:
                    results = [results]
                    print(f"    [{api_id}] 3 次嘗試均回傳單一物件，強制使用")
                    print(f"    [{api_id}] ⚠️ 如果多次顯示此訊息，代表此 API 不適合翻譯任務，建議從 .env 移除或更換模型")
            if isinstance(results, list):
                if len(results) > int(len(batch) * OVER_RETURN_TOLERANCE):
                    raise ValueError(f"回傳 {len(results)} 條，超過批次 {len(batch)} 的 {int(OVER_RETURN_TOLERANCE * 100)}%，視為失敗重試")
                batch_indices = {item["_idx"] for item in batch}
                mapped = {}
                for r in results:
                    if isinstance(r, dict) and r.get("index") in batch_indices:
                        mapped[r["index"]] = r
                results = list(mapped.values())
                if not results:
                    raise ValueError("回傳為空或無匹配的 index，視為失敗重試")
            if isinstance(results, list) and len(results) > 0:
                success = sum(1 for r in results if isinstance(r, dict) and r.get("translation"))
                rate = min(success, len(batch)) / len(batch) if len(batch) > 0 else 0
                if rate < 0.25:
                    debug_dir = get_debug_message_dir()
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    uid = uuid.uuid4().hex[:4]
                    (debug_dir / f"debug_{ts}_{uid}.json").write_text(
                        json.dumps({
                            "api_id": api_id,
                            "model": model_name,
                            "batch_size": len(batch),
                            "returned": len(results),
                            "success": success,
                            "rate": round(rate, 3),
                            "response_preview": text[:3000]
                        }, ensure_ascii=False, indent=2), encoding="utf-8")
                    print(f"    [{api_id}] ⚠️ 低翻譯率 {rate:.1%}（{success}/{len(batch)}），已儲存 debug 訊息")
            if isinstance(results, list) and len(results) > 0:
                null_count = sum(1 for r in results if isinstance(r, dict) and r.get("translation") is None)
                if null_count > len(results) * 0.5:
                    print(f"    [{api_id}] 警告：{null_count}/{len(results)} 條翻譯為 null")
            if isinstance(results, list):
                if results and not all("index" in r and "translation" in r for r in results):
                    print(f"    [{api_id}] 警告：JSON 陣列中缺少 index/translation 欄位")
                    print(f"    回傳內容前 200 字: {text[:200]}")
            if isinstance(results, dict):
                if "translations" not in results and "index" not in results:
                    print(f"    [{api_id}] 警告：JSON 物件結構異常（無 translations 也無 index）")
                    print(f"    回傳內容前 200 字: {text[:200]}")
            # 結構無法識別（非 JSON 陣列）視為失敗重試（與其他 LLM 路徑的
            # 「空回傳視為失敗」政策一致），避免把 0 進度當成成功批次完成
            if not isinstance(results, list):
                print(f"    [{api_id}] 警告：回傳結構無法識別（非 JSON 陣列）")
                print(f"    回傳內容前 200 字: {text[:200]}")
                raise ValueError("回傳結構無法識別（非 JSON 陣列），視為失敗重試")
            return results
        except Exception as e:
            if is_rate_limit_error(e):
                raise
            # ↓ 嘗試用 json_repair 修復 JSON 格式錯誤
            if isinstance(e, json.JSONDecodeError):
                try:
                    from json_repair import repair_json
                    repaired = repair_json(text)
                    if repaired:
                        results = json.loads(repaired)
                        if is_rate_limit_payload(results):
                            raise RateLimitPayloadError(
                                f"429 rate limit in response payload: {str(results)[:200]}")
                        print(f"    [{api_id}] ⚠️ JSON 修復成功")
                        if isinstance(results, dict) and "translations" in results:
                            results = results["translations"]
                        if isinstance(results, list):
                            if not results:
                                raise ValueError("回傳為空，視為失敗重試")
                            if len(results) > int(len(batch) * OVER_RETURN_TOLERANCE):
                                raise ValueError(f"回傳 {len(results)} 條，超過批次 {len(batch)} 的 {int(OVER_RETURN_TOLERANCE * 100)}%，視為失敗重試")
                            batch_indices = {item["_idx"] for item in batch}
                            mapped = {}
                            for r in results:
                                if isinstance(r, dict) and r.get("index") in batch_indices:
                                    mapped[r["index"]] = r
                            results = list(mapped.values())
                            if not results:
                                raise ValueError("回傳為空或無匹配的 index，視為失敗重試")
                            return results
                except RateLimitPayloadError:
                    # 修復後的內容仍是限流錯誤包裝 → 不可被下面吞掉，要往上拋成 429
                    raise
                except Exception:
                    pass
            # ↑ 修復結束
            print(f"    [{api_id}] API 錯誤 (嘗試 {attempt + 1}/3): {e}")
            if attempt < 2:
                await asyncio.sleep(5)

    return [{"index": item["_idx"], "translation": None, "_error": "API failed"} for item in batch]


# ── 非同步翻譯核心（共享批次池） ──
def prepare_sheet_translation(df: pd.DataFrame, output_dir=None, sheet_name=None):
    # Restore already-translated entries from backup and build the pending list (local work).
    df = df.copy()
    backup = load_backup(output_dir, sheet_name)
    if backup:
        restore_count = 0
        skipped = 0
        for idx_str, trans in backup.items():
            # 備份檔損壞或人為編輯時 key 可能不是數字 → 只跳過該筆
            try:
                idx = int(idx_str)
            except (TypeError, ValueError):
                skipped += 1
                continue
            if idx not in df.index:
                continue
            # 備份值必須是非空字串才可寫入 translation 欄：
            # dict／list／數字會被 str() 之後當成譯文（或在 str dtype 欄位上直接拋錯）
            if not (isinstance(trans, str) and trans.strip()):
                skipped += 1
                continue
            eng_hint = _english_hint(df.at[idx, "english"] if "english" in df.columns else None)
            # 備份值本身若是缺值標記（字串 "nan"/"nat"/"none"），不可還原成真值；
            # 但原文真的是那個字時（english= 提示）同值譯文合法
            if is_missing_translation(trans, english=eng_hint):
                skipped += 1
                continue
            # 與下方 pending 判定同口徑（NaN／None／空字串／"nan" 皆視為缺值）
            # 只還原缺值，不覆蓋既有譯文
            if is_missing_translation(df.at[idx, "translation"], english=eng_hint):
                df.at[idx, "translation"] = trans
                restore_count += 1
        if skipped:
            print(f"  警告：備份中 {skipped} 筆資料無效（非數字 key 或非字串譯文），已略過")
        if restore_count > 0:
            print(f"  從備份還原 {restore_count} 條翻譯")
    if "translation" not in df.columns:
        df["translation"] = None
    pending = []
    for idx, row in df.iterrows():
        if is_missing_translation(row.get("translation"), english=_english_hint(row.get("english"))):
            pending.append({
                "_idx": idx,
                "english": str(row["english"]),
                "sub_category": str(row.get("sub_category", "")),
                "notes": row.get("notes"),
                "wiki_url": row.get("wiki_url"),
            })
    if not pending:
        print("  所有条目已完成，无需翻译")
    pending.sort(key=lambda x: (x.get("sub_category", ""), x.get("_idx", 0)))
    print(f"  待翻译: {len(pending)} 条")
    return df, pending


async def translate_sheet_phase(df, glossary, output_dir, pending, sheet_name, pool):
    # Submit this worksheet translation batches to the shared pool and update df.
    batches = _group_into_batches(pending)
    total_batches = len(batches)
    session_tag = uuid.uuid4().hex[:8]

    async def _process_batch(ws, job):
        cfg = ws["cfg"]
        batch_num, batch = job.batch_num, job.batch
        batch_indices = {item["_idx"] for item in batch}
        _timestamp(f"[{sheet_name}] 批次 {batch_num}/{total_batches} 開始 ({len(batch)} 条) [{cfg.api_id} {cfg.model}]")
        try:
            results = await _translate_batch(ws["client"], cfg.model, batch, glossary, cfg.api_id)
            new_backup = {}
            skipped = 0
            for res in results:
                if not isinstance(res, dict):
                    skipped += 1
                    continue
                # 單筆格式異常（index 無法匹配、translation 非字串等）
                # 只影響該筆，不可讓整個批次的其他結果一起損失
                try:
                    idx = res.get("index")
                    trans = res.get("translation")
                    # 只有非空字串才可寫入 translation 欄：dict／數字在 str dtype 欄位上
                    # 會直接拋錯（整批結果一起丟失），list 則會被靜默轉成錯誤的譯文；
                    # 缺值哨兵（"nan"/"nat"/"none"）也不是譯文，寫進去會與三條還原守門
                    # 不一致（守門拒絕還原同樣的值），故一律跳過、讓該列維持缺值重試。
                    if (idx is None or not isinstance(trans, str) or not trans.strip()
                            or is_missing_translation(trans, english=_english_hint_at(df, idx))):
                        skipped += 1
                        continue
                    if idx in batch_indices:
                        df.at[idx, "translation"] = trans
                        new_backup[str(idx)] = str(trans)
                    else:
                        skipped += 1
                except (TypeError, ValueError) as e:
                    skipped += 1
                    print(f"    [{cfg.api_id}] 警告：略過無效回傳（index={res.get('index')!r}）：{e}")
            if skipped:
                print(f"    [{cfg.api_id}] 警告：{skipped} 條回傳無效（非物件、index 不在工作表中或譯文非字串），已忽略")
            if new_backup:
                save_backup_part(output_dir, session_tag, batch_num, new_backup, sheet_name)
                sync_progress(output_dir, sheet_name)
            _timestamp(f"[{sheet_name}] 批次 {batch_num}/{total_batches} 完成 ({len(new_backup)} 条) [{cfg.api_id} {cfg.model}]")
        except Exception as e:
            if is_rate_limit_error(e):
                pool.note_429(cfg)
                if cfg.is_permanently_disabled:
                    print(f"  [{cfg.api_id} {cfg.model}] 第 {cfg.strike} 次 429，永久停用")
                else:
                    print(f"  [{cfg.api_id} {cfg.model}] 第 {cfg.strike} 次 429，冷卻 60 秒")
                pool.retry(job)
            else:
                print(f"  [{cfg.api_id} {cfg.model}] 錯誤：{e}")

    if not batches:
        return df
    try:
        await asyncio.gather(*[pool.submit(bn, b, _process_batch) for bn, b in enumerate(batches, 1)])
    except (asyncio.CancelledError, KeyboardInterrupt):
        sync_progress(output_dir, sheet_name)
        print("\n  ⚠️檢測到中斷，已儲存翻譯備份")
        raise
    sync_progress(output_dir, sheet_name)
    _timestamp(f"[{sheet_name}] 翻译完成")
    return df

def _group_into_batches(pending):
    """將待翻譯條目按 NPC 分組後切成批次"""
    # ── Step 1：按 sub_category 分組 ──
    groups = []
    i = 0
    while i < len(pending):
        npc = pending[i].get("sub_category", "")
        group = []
        while i < len(pending) and pending[i].get("sub_category", "") == npc:
            group.append(pending[i])
            i += 1
        groups.append((npc, group))

    # ── Step 2：大 NPC（≥100）拆分，餘數進小池；小 NPC 整組進小池 ──
    batches: list[list[dict]] = []
    small_pool: list[tuple[str, list[dict]]] = []

    for npc, group in groups:
        gs = len(group)
        if gs >= BATCH_SIZE_LIMIT:
            for k in range(0, gs, BATCH_SIZE_LIMIT):
                chunk = group[k:k + BATCH_SIZE_LIMIT]
                if len(chunk) == BATCH_SIZE_LIMIT:
                    batches.append(chunk)
                else:
                    small_pool.append((npc, chunk))
        else:
            small_pool.append((npc, group))

    # ── Step 3：小池合併（維持各 NPC 群組完整） ──
    current_batch: list[dict] = []
    for npc, group in small_pool:
        gs = len(group)
        if len(current_batch) + gs <= BATCH_SIZE_LIMIT:
            current_batch.extend(group)
        else:
            if current_batch:
                batches.append(list(current_batch))
            current_batch = list(group)
    if current_batch:
        batches.append(list(current_batch))

    batch_sizes = [len(b) for b in batches]
    print(f"  分組結果: {len(batches)} 批, 各批大小: {batch_sizes}")
    return batches


