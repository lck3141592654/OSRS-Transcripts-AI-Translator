#!/usr/bin/env python3
# batch_proofread.py - 自動化校對互動式 CLI

import sys, json, asyncio
from pathlib import Path
from datetime import datetime
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from glossary import load_glossary, auto_extract_glossary, MISSING_SENTINELS
from llm_translator import delete_checkpoint_files, get_debug_message_dir
from proofreader import (run_proofread, run_quick_proofread,
                         _load_session, _load_quick_session,
                         _proofread_checkpoint_dir, _quick_checkpoint_dir)

SEP = chr(61)*60

def _get_workplace():
    wp = Path(__file__).parent / "workplace"
    wp.mkdir(parents=True, exist_ok=True)
    return wp

def timestamp(msg):
    # 內層字串用單引號：同一種引號嵌套的 f-string 是 Python 3.12（PEP 701）才合法，
    # 在 3.10/3.11 會 SyntaxError（本檔最低支援版本為 3.10）
    print(f"  [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")

def elapsed(start, label):
    secs = (datetime.now() - start).total_seconds()
    print(f"  [{label}] 耗時: {secs:.1f} 秒")

def validate_columns(frames, sheet_names, required=("english", "translation")) -> bool:
    """檢查目標工作表是否具備必要欄位（使用呼叫端已載入的 frames，不重複讀檔）。"""
    ok = True
    for sn in sheet_names:
        df = frames.get(sn)
        if df is None:
            print(f"  錯誤：找不到工作表「{sn}」的資料")
            ok = False
            continue
        missing = [c for c in required if c not in df.columns]
        if missing:
            print(f"  錯誤：工作表「{sn}」缺少必要欄位: {', '.join(missing)}")
            ok = False
    return ok

def list_excel_files(directory):
    if not directory.exists():
        return []
    return sorted([f for f in directory.glob("*.xlsx") if not f.name.startswith("~$")])

def choose_int(prompt, max_val, allow_zero=False):
    while True:
        try:
            choice = input(prompt).strip()
            if not choice:
                return None
            idx = int(choice)
            if idx == 0 and allow_zero:
                return 0
            if 1 <= idx <= max_val:
                return idx
        except ValueError:
            pass
        print("  無效輸入，請重新輸入。")

def choose_from_list(items, title, allow_zero=False):
    print(f"\n{SEP}")
    print(f"  {title}")
    print(SEP)
    for i, item in enumerate(items, 1):
        print(f"  [{i}] {item}")
    if allow_zero:
        print("  [0] 跳過")
    return choose_int("\n請輸入編號: ", len(items), allow_zero=allow_zero)

def choose_multi(items, title):
    print(f"\n{SEP}")
    print(f"  {title}")
    print(SEP)
    for i, item in enumerate(items, 1):
        print(f"  [{i}] {item}")
    print("  [0] 全部選取")
    while True:
        try:
            choice = input("\n請輸入編號（可多個以逗號分隔）: ").strip()
            if choice == "0":
                return list(items)
            raw_indices = [int(x.strip()) for x in choice.split(",") if x.strip()]
            indices = list(dict.fromkeys(raw_indices))
            selected = [items[i-1] for i in indices if 1 <= i <= len(items)]
            if selected:
                return selected
        except (ValueError, IndexError):
            pass
        print("  無效輸入，請重新輸入。")

def step1():
    workplace = _get_workplace()
    xlsx_files = list_excel_files(workplace)
    if not xlsx_files:
        print(f"\n錯誤：找不到 Excel 檔案 {workplace}/")
        print("請將校對目標 Excel 放入 workplace/ 後再執行。")
        input("\n按 Enter 關閉...")
        sys.exit(1)
    fnames = [f.name for f in xlsx_files]
    c = choose_from_list(fnames, f"請選擇目標 Excel 檔案（{workplace}/）：")
    if c is None:
        print("已取消。")
        input("\n按 Enter 關閉...")
        sys.exit(0)
    return xlsx_files[c - 1]

def step2(exclude_path):
    workplace = _get_workplace()
    xlsx_files = [f for f in list_excel_files(workplace) if f.resolve() != Path(exclude_path).resolve()]
    if not xlsx_files:
        inp = input(f"\n找不到 Excel 檔案 {workplace}/，輸入其他路徑或按 Enter 取消 ").strip()
        if inp:
            p = Path(inp)
            if p.exists():
                return p, None
        print("術語庫為必選，無法繼續。")
        sys.exit(1)
    fnames = [f.name for f in xlsx_files]
    fnames.append("自動從目標 Excel 萃取")
    c = choose_from_list(fnames, "選擇術語庫（必選）:")
    if c is None:
        print("術語庫為必選，無法繼續。")
        sys.exit(1)
    if c == len(fnames):
        return Path("__AUTO__"), None
    gp = xlsx_files[c - 1]
    with pd.ExcelFile(gp) as xls:
        sheets = xls.sheet_names
    if len(sheets) == 1:
        print(f"  自動使用工作表: {sheets[0]}")
        return gp, sheets
    selected = choose_multi(sheets, "選擇術語庫工作表:")
    return gp, selected

def step3(excel_path):
    with pd.ExcelFile(excel_path) as xls:
        sheets = xls.sheet_names
    if len(sheets) == 1:
        print(f"  自動使用工作表: {sheets[0]}")
        return sheets
    selected = choose_multi(sheets, "選擇校對工作表:")
    if not selected:
        print("未選取任何工作表，結束。")
        sys.exit(1)
    return selected

def translated_mask(df: pd.DataFrame) -> pd.Series:
    """回傳「該列已有可用譯文」的布林 Series（缺值／哨兵／空白都不算）。

    這是全模組唯一的「已翻譯」判定，step4 與上層的計數都必須用它，
    否則同一個畫面會出現兩個互相矛盾的數字（先前 line 352 用 `~isna()`，
    純空白與 "nat"/"none" 會被算成已翻譯、與 step4 不一致）。

    注意：真正兜住 NA 的是 `notna()` —— pandas 3.0 的 `Series.astype(str)`
    會把缺值保留成 float NaN（不會變成 "nan"／"<NA>" 字串），
    所以 `isin(MISSING_SENTINELS)` 與 `strip() != ""` 對 NA 都不成立。
    """
    if "translation" not in df.columns:
        return pd.Series([False] * len(df), index=df.index)
    s = df["translation"].astype(str)
    return (df["translation"].notna()
            & ~s.str.strip().str.lower().isin(MISSING_SENTINELS)
            & (s.str.strip() != ""))


def step4(excel_path, sheet_name, df=None):
    if df is None:
        df = pd.read_excel(excel_path, sheet_name=sheet_name, dtype=str)
    total = len(df)
    print(f"\n  工作表「{sheet_name}」共有 {total} 行")
    # Find rows with translations and compress to ranges
    trans_indices = []
    trans_ranges = []
    if "translation" in df.columns:
        # 哨兵集合與 is_missing_translation 共用（原本只擋 "nan"，"nat"/"none" 會被誤算成已翻譯）
        mask = translated_mask(df)
        trans_indices = list(df.index[mask])
        if trans_indices:
            s = trans_indices[0]
            e = trans_indices[0]
            for idx in trans_indices[1:]:
                if idx == e + 1:
                    e = idx
                else:
                    trans_ranges.append((s, e))
                    s = idx
                    e = idx
            trans_ranges.append((s, e))
            range_str = ", ".join(f"{st+1}-{ed+1}" if st != ed else f"{st+1}" for st, ed in trans_ranges)
            print(f"  有翻譯的行數: {len(trans_indices)}")
            print(f"  已翻譯行號: {range_str}")
        else:
            print("  警告：找不到任何翻譯！")
    print(f"\n  選擇範圍選項:")
    if trans_indices:
        print(f"  [1] 所有已翻譯行數（{len(trans_indices)} 行）")
    else:
        print(f"  [1] 全部行（1-{total}）")
    print(f"  [2] 指定行數範圍（如 100-500）")
    c = choose_int("\n  請選擇:", 2)
    if c is None or c == 1:
        if trans_indices:
            return list(trans_indices)
        return list(range(total)) if total > 0 else []
    while True:
        try:
            inp = input("  請輸入行數範圍（如 100-500）: ").strip()
            parts = inp.split("-")
            start = int(parts[0]) - 1
            end = int(parts[1])
            if 0 <= start < end <= total:
                return list(range(start, end))
        except (ValueError, IndexError):
            pass
        print("  無效範圍。")
def confirm(excel_path, glossary_path, sheet_names, total_selected, mode="proofread"):
    print(f"\n{SEP}")
    print("  確認資訊")
    print(SEP)
    print(f"  目標: {excel_path.name}")
    print(f"  模式: {'快速校對' if mode == 'quick_proofread' else '完整校對'}")
    if glossary_path and str(glossary_path) == "__AUTO__":
        glossary_label = "自動萃取（name/manual 工作表）"
    elif glossary_path:
        glossary_label = Path(glossary_path).name
    else:
        glossary_label = "自動萃取"
    print(f"  術語庫: {glossary_label}")
    print(f"  工作表: {len(sheet_names)}")
    for sn in sheet_names:
        print(f"    - {sn}")
    print(f"  校對總條數: {total_selected}")
    print(f"\n  開始校對？(y/n): ", end='')
    return input().strip().lower() == "y"


def find_existing_progress():
    session = _load_session(str(_get_workplace()))
    if session and session.get("mode") in ("proofread", "quick_proofread"):
        return session
    quick_session = _load_quick_session(str(_get_workplace()))
    if quick_session and quick_session.get("mode") == "quick_proofread":
        return quick_session
    return None

def prompt_resume(session):
    print(f"\n{SEP}")
    print("  發現上次校對進度")
    print(SEP)
    print(f"  目標: {Path(session['excel_path']).name}")
    mode_label = "快速校對" if session.get("mode") == "quick_proofread" else "完整校對"
    print(f"  模式: {mode_label}")
    cs = session.get("completed_sheets", [])
    ps = session.get("pending_sheets", [])
    print(f"  已完成工作表: {len(cs)}")
    print(f"  待處理工作表: {len(ps)}")
    print(f"\n  繼續校對？(y/n): ", end='')
    return input().strip().lower() == "y"

def main():
    print(SEP)
    print("      校對管線 v1.0")
    print(f"      工作目錄: {_get_workplace()}")
    print(SEP)
    print(f"\n  請選擇校對模式：")
    print(f"  [1] 完整校對（流暢度評估 + 潤色 + 重譯保護）")
    print(f"  [2] 快速校對（只做重譯修正：術語/佔位符/未翻譯/空格）")
    while True:
        mode_choice = input("\n  請輸入: ").strip()
        if mode_choice == "1":
            mode = "proofread"
            break
        if mode_choice == "2":
            mode = "quick_proofread"
            break
        print("  無效輸入，請重新輸入。")

    # ── 檢查上次的 debug 訊息 ──
    debug_dir = get_debug_message_dir(create=False)
    if debug_dir.exists():
        debug_files = sorted(debug_dir.glob("debug_*.json"))
        if debug_files:
            print(f"\n{'=' * 60}")
            print(f"  ⚠️ 上次執行有低翻譯率記錄")
            print(f"{'=' * 60}")
            for f in debug_files:
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    print(f"  ─ [{data.get('api_id', '?')}] {data.get('model', '?')} ─")
                    print(f"    批次大小: {data.get('batch_size')}, "
                          f"成功: {data.get('success')}/{data.get('returned')}, "
                          f"翻譯率: {data.get('rate', 0):.1%}")
                    preview = data.get("response_preview", "")
                    if preview:
                        print(f"    回傳內容:\n{preview}")
                except Exception as e:
                    print(f"  無法讀取 {f.name}: {e}")
            print(f"{'=' * 60}\n")
        import shutil
        shutil.rmtree(debug_dir)
        print(f"  已清除 debug 訊息\n")

    exist_session = find_existing_progress()
    if exist_session and exist_session.get("mode") != mode:
        old_label = "完整校對" if exist_session.get("mode") == "proofread" else "快速校對"
        new_label = "快速校對" if mode == "quick_proofread" else "完整校對"
        print(f"\n  ⚠️ 偵測到「{old_label}」的未完成進度，與目前選擇的「{new_label}」不同")
        if input("  是否清除舊進度並以新模式開始？(y/n): ").strip().lower() != "y":
            print("  已取消。")
            input("\n按 Enter 鍵...")
            return
        old_cp = (_quick_checkpoint_dir if exist_session.get("mode") == "quick_proofread" else _proofread_checkpoint_dir)(str(_get_workplace()))
        if old_cp.exists():
            import shutil
            shutil.rmtree(old_cp)
        delete_checkpoint_files(str(_get_workplace()))
        print("  已清除舊進度（含共享 _checkpoint）。")
        exist_session = None
    if exist_session:
        if not prompt_resume(exist_session):
            if exist_session.get("mode") == "quick_proofread":
                cp = _quick_checkpoint_dir(str(_get_workplace()))
            else:
                cp = _proofread_checkpoint_dir(str(_get_workplace()))
            if cp.exists():
                import shutil; shutil.rmtree(cp)
            delete_checkpoint_files(str(_get_workplace()))
            print("\n  已清除舊進度（含共享 _checkpoint）。")
        else:
            excel_path = Path(exist_session["excel_path"])
            glossary_path_str = exist_session.get("glossary_path")
            glossary_path = Path(glossary_path_str) if glossary_path_str else None
            glossary_sheets = exist_session.get("glossary_sheets")
            pending_sheets = list(exist_session.get("pending_sheets", []))
            completed_sheets = list(exist_session.get("completed_sheets", []))
            sheet_configs = exist_session.get("sheet_configs", {})
            all_sheets = list(completed_sheets) + list(pending_sheets)
            t0 = datetime.now(); timestamp("正在載入術語庫...")
            if glossary_path_str == "__AUTO__":
                glossary = auto_extract_glossary(excel_path, str(_get_workplace()))
            else:
                glossary = load_glossary(glossary_path, glossary_sheets if glossary_path else None)
            elapsed(t0, "術語庫")
            print(f"  術語庫載入完成: {len(glossary)} 條")
            if exist_session.get("mode") == "quick_proofread":
                asyncio.run(run_quick_proofread(excel_path, glossary_path, glossary_sheets, all_sheets, sheet_configs, session=exist_session))
            else:
                asyncio.run(run_proofread(excel_path, glossary_path, glossary_sheets, all_sheets, sheet_configs, session=exist_session))
            return
    # 全新執行
    excel_path = step1()
    glossary_path, glossary_sheets = step2(excel_path)
    sheet_names = step3(excel_path)
    frames = {}
    for sn in sheet_names:
        frames[sn] = pd.read_excel(excel_path, sheet_name=sn, dtype=str)
    if not validate_columns(frames, sheet_names):
        input("\n按 Enter 鍵...")
        return
    sheet_configs = {}
    for sn in sheet_names:
        print(f"\n  --- 工作表: {sn} ---")
        df_full = frames[sn]
        if "translation" in df_full.columns:
            has_t = int(translated_mask(df_full).sum())
            print(f"    總行數: {len(df_full)}，有翻譯: {has_t}")
        sel = step4(excel_path, sn, df_full)
        sheet_configs[sn] = {"selected_indices": sel}
    # run_proofread 之後會自己讀檔（excel_path 可能來自續傳 session），
    # 這裡的 frames 只是用來避免驗證與選取階段重複讀檔，用完即釋放。
    del frames
    total_selected = sum(len(cfg["selected_indices"]) for cfg in sheet_configs.values())
    if total_selected == 0:
        print("\n未選取任何條目。")
        input("\n按 Enter 鍵...")
        return
    if not confirm(excel_path, glossary_path, sheet_names, total_selected, mode):
        print("  已取消。")
        input("\n按 Enter 鍵...")
        return
    if mode == "quick_proofread":
        asyncio.run(run_quick_proofread(excel_path, glossary_path, glossary_sheets, sheet_names, sheet_configs))
    else:
        asyncio.run(run_proofread(excel_path, glossary_path, glossary_sheets, sheet_names, sheet_configs))
    input("\n按 Enter 關閉...")

if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        if "all APIs permanently disabled" in str(e):
            print("\n  ⚠️ 所有 API 已永久停用（累計 429 限流達門檻；只有一個可用 API 時門檻會放寬），無法繼續。")
            print("  已儲存部分校對進度，請檢查 API Key 後重新執行即可續傳。")
            input("\n按 Enter 鍵...")
        else:
            print(f"\n錯誤: {e}")
            import traceback; traceback.print_exc()
            input("\n按 Enter 鍵...")
    except Exception as e:
        print(f"\n錯誤: {e}")
        import traceback; traceback.print_exc()
        input("\n按 Enter 鍵...")
