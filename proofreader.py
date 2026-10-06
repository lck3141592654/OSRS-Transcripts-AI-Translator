#!/usr/bin/env python3
# proofreader.py - automated proofreading core module v1.0
# Four phases: 1a Script Check, 1b Template Match, 2 LLM Eval, 3 Polish, 4 Retry Protect

import sys, os, json, asyncio, time, re, uuid
from pathlib import Path
from datetime import datetime
from copy import deepcopy
import pandas as pd
sys.path.insert(0, str(Path(__file__).parent))

from glossary import load_glossary, auto_extract_glossary, find_term_spans, build_relevance_context, filter_relevant_terms
from tm_matcher import match_and_fill
from llm_translator import (
    _group_into_batches, _sanitize_sheet_name,
    save_backup_part, sync_progress, atomic_write_text,
    get_relevant_glossary, is_missing_translation, get_debug_message_dir,
    _english_hint, _english_hint_at,
)
from enforcer import (
    scan_issues, _enforce_async, narrow_relevant_glossary,
)
from api_config import (
    API_TIMEOUT, OVER_RETURN_TOLERANCE,
    is_rate_limit_error, is_rate_limit_payload, RateLimitPayloadError,
)
from shared_pool import SharedBatchPool

# Constants
CHECKPOINT_SUBDIR = "_proofread_checkpoint"
BACKUP_SUBDIR = "_proofread_backup"
PROGRESS_FILE = "progress.json"
SESSION_FILE = "session.json"
QUICK_CHECKPOINT_SUBDIR = "_quick_checkpoint"

def _timestamp(msg):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"  [{now}] {msg}")

def elapsed(start, label):
    secs = (datetime.now() - start).total_seconds()
    print(f"  [{label}] 耗時: {secs:.1f} 秒")

def _proofread_checkpoint_dir(output_dir=None, sheet_name=None):
    if output_dir:
        base = Path(output_dir) / CHECKPOINT_SUBDIR
    else:
        base = Path(__file__).parent / CHECKPOINT_SUBDIR
    if sheet_name:
        base = base / _sanitize_sheet_name(sheet_name)
    return base

# Session management (isolated from main _checkpoint/)
def _save_session(output_dir, session_info):
    cd = _proofread_checkpoint_dir(output_dir)
    cd.mkdir(parents=True, exist_ok=True)
    tmp = cd / f"{SESSION_FILE}.tmp"
    final = cd / SESSION_FILE
    tmp.write_text(
        json.dumps(session_info, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    with open(tmp, "ab") as sf:
        os.fsync(sf.fileno())
    tmp.replace(final)

def _load_session(output_dir):
    sf = _proofread_checkpoint_dir(output_dir) / SESSION_FILE
    if sf.exists():
        try:
            return json.loads(sf.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None

# Phase checkpoint markers
def _mark_phase_complete(output_dir, sheet_name, phase):
    cd = _proofread_checkpoint_dir(output_dir, sheet_name)
    cd.mkdir(parents=True, exist_ok=True)
    marker = cd / f"_{phase}_done"
    atomic_write_text(marker, json.dumps({ "phase": phase, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S") }, ensure_ascii=False))

def _is_phase_complete(output_dir, sheet_name, phase):
    marker = _proofread_checkpoint_dir(output_dir, sheet_name) / f"_{phase}_done"
    return marker.exists()


# === Phase 4b: Template Correction ===
TEMPLATE_CHECKPOINT_FILE = "template_indices.json"

def _save_template_checkpoint(output_dir, sheet_name, indices):
    cd = _proofread_checkpoint_dir(output_dir, sheet_name)
    cd.mkdir(parents=True, exist_ok=True)
    atomic_write_text(cd / TEMPLATE_CHECKPOINT_FILE, json.dumps({"template_indices": indices}, ensure_ascii=False))

def template_correction(df, glossary, output_dir, sheet_name=None):
    df = match_and_fill(df, glossary)
    done = list(df.index[df["_status"] == "已處理"])
    if done:
        _save_template_checkpoint(output_dir, sheet_name, done)
        print(f"    模板校正完成：{len(done)} 條已處理")
    if "_status" in df.columns:
        df = df.drop(columns=["_status"])
    return df

# Phase 2 prompt: LLM Dual-round Fluency Evaluation
SYSTEM_PROMPT_EVAL = (
    "你是一个中文翻译品质评估专家。\n"
    "请遵守以下规则：\n"
    "请评估以下译文的流畅度和翻译腔程度，分为四级：\n\n"
    "- 没问题：流畅自然，没有翻译腔\n"
    "- 轻度：稍微不自然，但可理解\n"
    "- 中等：明显翻译腔，需要修改\n"
    "- 严重：完全不符合中文表达习惯\n\n"
    "输出格式为原始 JSON 阵列（不要使用 ```json 代码块包裹），每条物件包含 \"index\"、\"level\" 和 \"reason\" 三个栏位。\n"
    "例如：[{\"index\": 0, \"level\": \"没问题\", \"reason\": \"流畅自然\"}]\n"
    "## 强制术语表（禁止使用官方和社区通常的翻譯，遇到以下英文词必须使用指定的中文翻译）\n"
    "{RELEVANT_GLOSSARY}"
)

def _normalize_level(value) -> str:
    """評估等級正規化：型別錯誤／空值一律視為「严重」。

    `dict.get(key, default)` 只在 key 不存在時用預設值；key 存在但值是 None／
    空字串／非字串時會拿到壞值，流進 `_cross_reference`（unhashable 直接拋錯、
    或與 skip_pairs 比對後被漏報）、報告與檢查點。此處與 `_pad_eval_results`
    的補位語意一致，一律以「严重」處理，避免漏報。
    """
    if isinstance(value, str) and value.strip():
        return value
    return "严重"


def _normalize_eval_entry(entry, index=None):
    """把模型／檢查點回傳的評估物件正規化成 {"index","level","reason"}。"""
    if not isinstance(entry, dict):
        return None
    idx = entry.get("index", index)
    return {"index": idx, "level": _normalize_level(entry.get("level")),
            "reason": entry.get("reason", "")}


def _cross_reference(round1, round2):
    # 跳過組合僅限 (沒問題,沒問題)、(沒問題,輕度)、(輕度,沒問題)；兩輪皆「輕度」仍送潤色
    skip_pairs = {("没问题", "没问题"), ("轻度", "没问题"), ("没问题", "轻度")}
    # 防禦：即使呼叫端沒先正規化，非 dict／非字串 level 也不可讓 (unhashable) 比較拋錯
    lv1 = _normalize_level(round1.get("level") if isinstance(round1, dict) else None)
    lv2 = _normalize_level(round2.get("level") if isinstance(round2, dict) else None)
    return (lv1, lv2) not in skip_pairs


def _pad_eval_results(results, batch):
    """過濾非 dict 並對回傳條數不足的缺失項用「严重」補位，避免漏報。"""
    clean = [r for r in results if isinstance(r, dict)]
    present = {r.get('index') for r in clean}
    padded = list(clean)
    for item in batch:
        idx = item.get('_idx', 0)
        if idx not in present:
            padded.append({'index': idx, 'level': '严重', 'reason': '評估缺失，預設為嚴重'})
    return padded


async def _evaluate_batch(client, model, batch, glossary, api_id):
    relevant = get_relevant_glossary(batch, glossary)
    glossary_str = "\n".join([f"  {e} → {c}" for e, c in relevant]) if relevant else "  无"
    system_prompt = SYSTEM_PROMPT_EVAL.replace("{RELEVANT_GLOSSARY}", glossary_str)
    user_content = json.dumps([
        {"index": item["_idx"], "english": item["english"], "translation": item["translation"]}
        for item in batch
    ], ensure_ascii=False)

    text = ""
    for attempt in range(3):
        try:
            resp = await client.chat.completions.create(
                model=model,
                                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],

                temperature=0.1,
                timeout=API_TIMEOUT,
            )
            text = resp.choices[0].message.content.strip()
            text = re.sub(r"^```(?:json)?\n?", "", text, flags=re.IGNORECASE)
            text = re.sub(r"\n```$", "", text)
            results = json.loads(text)
            # 有些供應商 HTTP 200 但 body 是限流錯誤物件 → 當成 429 往上拋
            if is_rate_limit_payload(results):
                raise RateLimitPayloadError(f"429 rate limit in response payload: {text[:200]}")

            # 方法一：先试明确的 key 名称
            if isinstance(results, dict):
                known_keys = ["evaluations", "results", "data", "assessments"]
                for key in known_keys:
                    if key in results:
                        candidate = results[key]
                        if isinstance(candidate, list) and len(candidate) > 0 and isinstance(candidate[0], dict):
                            results = candidate
                            break

            # 方法二：自动搜寻 index+level+reason 数组
            if isinstance(results, dict):
                found = None
                for key, value in results.items():
                    if isinstance(value, list) and len(value) > 0 and isinstance(value[0], dict) \
                            and "index" in value[0] and "level" in value[0]:
                        found = value
                        break
                if found is None:
                    for key, value in results.items():
                        if isinstance(value, dict):
                            for k2, v2 in value.items():
                                if isinstance(v2, list) and len(v2) > 0 and isinstance(v2[0], dict) \
                                        and "index" in v2[0] and "level" in v2[0]:
                                    found = v2
                                    break
                if found is not None:
                    results = found

            # 单一条目：dict 同时有 index+level → 包装为数组
            if isinstance(results, dict) and "index" in results and "level" in results:
                print(f"    [{api_id}] ⚠️ API 回傳單一物件而非陣列（嘗試 {attempt + 1}/3）")
                print(f"    [{api_id}] ⚠️ 如果多次顯示此訊息，代表此 API 不適合翻譯任務，建議從 .env 移除或更換模型")
                if attempt < 2:
                    print(f"    [{api_id}] 準備重試...")
                    raise ValueError("single object returned, need array")
                else:
                    results = [results]
                    print(f"    [{api_id}] 3 次嘗試均回傳單一物件，強制使用")

            if isinstance(results, list) and len(results) > int(len(batch) * OVER_RETURN_TOLERANCE):
                raise ValueError(f"回傳 {len(results)} 條，超過批次 {len(batch)} 的 {int(OVER_RETURN_TOLERANCE * 100)}%，視為失敗重試")
            if isinstance(results, dict):
                results = [results]
            if isinstance(results, list):
                batch_indices = {item.get("_idx", 0) for item in batch}
                mapped = {}
                for r in results:
                    if isinstance(r, dict) and r.get("index") in batch_indices:
                        mapped[r["index"]] = r
                results = list(mapped.values())
                if not results:
                    raise ValueError("回傳為空或無匹配的 index，視為失敗重試")

            if isinstance(results, list) and len(results) > 0:
                # 低评估率检查（< 75% 視為失敗重試，在所有路徑後執行）
                success_count = sum(1 for r in results if isinstance(r, dict) and r.get("level") and r.get("reason"))
                rate = min(success_count, len(batch)) / len(batch) if len(batch) > 0 else 0
                if rate < 0.75:
                    if attempt < 2:
                        print(f"    [{api_id}] ⚠️ 完成率 {rate:.0%}（{success_count}/{len(batch)}）低於 75%，重試")
                        raise ValueError(f"completion rate {rate:.0%} < 75%")
                    else:
                        print(f"    [{api_id}] ⚠️ 完成率 {rate:.0%}（{success_count}/{len(batch)}）低於 75%，已使用所有嘗試")

                # null 检查
                null_count = sum(1 for r in results if isinstance(r, dict) and not r.get("level"))
                if null_count > len(results) * 0.5:
                    print(f"    [{api_id}] 警告：{null_count}/{len(results)} 条评估为 null")

                # 缺字段检查
                if results and not all(isinstance(r, dict) and "index" in r and "level" in r for r in results):
                    print(f"    [{api_id}] 警告：JSON 陣列中缺少 index/level 字段")
                    print(f"    回傳內容前 200 字: {text[:200]}")

            results = _pad_eval_results(results, batch)
            return results

        except json.JSONDecodeError:
            try:
                from json_repair import repair_json
                repaired = repair_json(text)
                if repaired:
                    results = json.loads(repaired)
                    if is_rate_limit_payload(results):
                        raise RateLimitPayloadError(
                            f"429 rate limit in response payload: {str(results)[:200]}")
                    print(f"    [{api_id}] ⚠️ JSON 修復成功")
                    if isinstance(results, dict):
                        if "evaluations" in results:
                            results = results["evaluations"]
                        elif "index" in results and "level" in results:
                            raise ValueError("single object after repair, need array")
                    if isinstance(results, list) and len(results) > int(len(batch) * OVER_RETURN_TOLERANCE):
                        raise ValueError(f"回傳 {len(results)} 條，超過批次 {len(batch)} 的 {int(OVER_RETURN_TOLERANCE * 100)}%，視為失敗重試")
                    if isinstance(results, list):
                        batch_indices = {item.get("_idx", 0) for item in batch}
                        mapped = {}
                        for r in results:
                            if isinstance(r, dict) and r.get("index") in batch_indices:
                                mapped[r["index"]] = r
                        results = list(mapped.values())
                        success_count = sum(1 for r in results if isinstance(r, dict) and r.get("level") and r.get("reason"))
                        rate = min(success_count, len(batch)) / len(batch) if len(batch) > 0 else 0
                        if rate < 0.75:
                            if attempt < 2:
                                print(f"    [{api_id}] ⚠️ 完成率 {rate:.0%}（{success_count}/{len(batch)}）低於 75%，重試")
                                raise ValueError(f"completion rate {rate:.0%} < 75%")
                            else:
                                print(f"    [{api_id}] ⚠️ 完成率 {rate:.0%}（{success_count}/{len(batch)}）低於 75%，已使用所有嘗試")
                        null_count = sum(1 for r in results if isinstance(r, dict) and not r.get("level"))
                        if null_count > len(results) * 0.5:
                            print(f"    [{api_id}] 警告：{null_count}/{len(results)} 条评估为 null")
                        if results and not all(isinstance(r, dict) and "index" in r and "level" in r for r in results):
                            print(f"    [{api_id}] 警告：JSON 陣列中缺少 index/level 字段")
                            print(f"    回傳內容前 200 字: {text[:200]}")
                        results = _pad_eval_results(results, batch)
                        return results
            except RateLimitPayloadError:
                # 修復後的內容仍是限流錯誤包裝 → 不可被吞掉，要往上拋成 429
                raise
            except Exception:
                pass
        except ValueError as e:
            print(f"    [{api_id}] API 錯誤 (嘗試 {attempt + 1}/3): {e}")
            if attempt < 2:
                await asyncio.sleep(3)
        except Exception as e:
            if is_rate_limit_error(e):
                raise
            print(f"    [{api_id}] API 錯誤 (嘗試 {attempt + 1}/3): {e}")
            if attempt < 2:
                await asyncio.sleep(3)

    _save_debug_info(api_id, model, len(batch), 0, 0, text[:3000] if 'text' in locals() and text else "All 3 attempts failed")
    return [{"index": item.get("_idx", 0), "level": None, "reason": "評估失敗"} for item in batch]

def _save_phase2_results(output_dir, sheet_name, round1_results, round2_results):
    cd = _proofread_checkpoint_dir(output_dir, sheet_name)
    cd.mkdir(parents=True, exist_ok=True)
    atomic_write_text(cd / "phase2_round1.json", json.dumps(round1_results, ensure_ascii=False))
    atomic_write_text(cd / "phase2_round2.json", json.dumps(round2_results, ensure_ascii=False))
    _mark_phase_complete(output_dir, sheet_name, "phase2")


def _load_phase2_category(output_dir, sheet_name, df):
    cd = _proofread_checkpoint_dir(output_dir, sheet_name)
    f1 = cd / "phase2_round1.json"
    f2 = cd / "phase2_round2.json"
    if not f1.exists() or not f2.exists():
        return []
    try:
        r1 = json.loads(f1.read_text(encoding="utf-8"))
        r2 = json.loads(f2.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(r1, dict) or not isinstance(r2, dict):
        print("    警告：P2 檢查點結構異常（非物件），略過還原")
        return []
    default_r2 = {"level": "严重", "reason": "评估缺失，预设为严重"}
    result = []
    skipped = 0
    for idx_str, r1r in r1.items():
        # 檢查點損壞或人為編輯：單筆壞資料只跳過該筆，不可讓整個工作表還原失敗
        try:
            idx = int(idx_str)
        except (TypeError, ValueError):
            skipped += 1
            continue
        if not isinstance(r1r, dict):
            skipped += 1
            continue
        r2r = r2.get(idx_str, default_r2)
        if not isinstance(r2r, dict):
            r2r = default_r2
        r1r = _normalize_eval_entry(r1r, idx)
        r2r = _normalize_eval_entry(r2r, idx)
        # index 一律以 key 為準（與原行為一致），避免鍵值不一致的檢查點產生錯位
        r1r["index"] = idx
        r2r["index"] = idx
        if _cross_reference(r1r, r2r):
            if idx in df.index:
                try:
                    row = df.loc[idx]
                except Exception:
                    skipped += 1
                    continue
                result.append({
                    "index": idx,
                    "english": str(row.get("english", "")),
                    "translation": str(row.get("translation", "")),
                    "round1": r1r,
                    "round2": r2r,
                })
    if skipped:
        print(f"    警告：P2 檢查點有 {skipped} 筆無效資料（非數字 key 或非物件），已略過")
    return result


def _save_phase2_progress(output_dir, sheet_name, data) -> None:
    """原子寫入 P2 進度（記錄 tag 與各輪已完成批次）。"""
    cd = _proofread_checkpoint_dir(output_dir, sheet_name)
    cd.mkdir(parents=True, exist_ok=True)
    atomic_write_text(cd / "phase2_progress.json", json.dumps(data, ensure_ascii=False))


def _load_phase2_progress(output_dir, sheet_name) -> dict | None:
    fp = _proofread_checkpoint_dir(output_dir, sheet_name) / "phase2_progress.json"
    if fp.exists():
        try:
            return json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _delete_phase2_progress(output_dir, sheet_name) -> None:
    fp = _proofread_checkpoint_dir(output_dir, sheet_name) / "phase2_progress.json"
    if fp.exists():
        try:
            fp.unlink()
        except OSError:
            pass


def _update_phase2_progress(output_dir, sheet_name, tag, total_batches, batch_num, rnd) -> None:
    """每完成一批就更新進度檔（同步執行，無 await，避免並行寫入競爭）。"""
    key = "round1_done" if rnd == 1 else "round2_done"
    data = _load_phase2_progress(output_dir, sheet_name) or {
        "tag": tag, "total_batches": total_batches, "round1_done": [], "round2_done": [],
    }
    data["tag"] = tag
    data["total_batches"] = total_batches
    done = data.setdefault(key, [])
    if batch_num not in done:
        done.append(batch_num)
    _save_phase2_progress(output_dir, sheet_name, data)


def _load_phase2_parts(output_dir, sheet_name, tag, rnd) -> dict:
    """合併同 tag 的 P2 批次 part 檔（r1/r2 分開）。"""
    cd = _proofread_checkpoint_dir(output_dir, sheet_name)
    merged = {}
    if not cd.exists():
        return merged
    for f in sorted(cd.glob(f"{tag}_*_r{rnd}.json")):
        try:
            merged.update(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    return merged


def _plan_phase2_tasks(batches, resume_progress, output_dir, sheet_name):
    """回傳 (tag, rr1, rr2, tasks)。續傳時只重送未完成的批次。"""
    if resume_progress:
        tag = resume_progress.get("tag") or uuid.uuid4().hex[:8]
        rr1 = _load_phase2_parts(output_dir, sheet_name, tag, 1)
        rr2 = _load_phase2_parts(output_dir, sheet_name, tag, 2)
        done1 = set(resume_progress.get("round1_done", []))
        done2 = set(resume_progress.get("round2_done", []))
        tasks = [
            (bn, rnd, batches[bn - 1])
            for bn in range(1, len(batches) + 1)
            for rnd, done in ((1, done1), (2, done2))
            if bn not in done
        ]
        return tag, rr1, rr2, tasks
    tag = uuid.uuid4().hex[:8]
    tasks = [
        (bn, rnd, batch)
        for bn, batch in enumerate(batches, 1)
        for rnd in (1, 2)
    ]
    return tag, {}, {}, tasks


async def phase2_llm_evaluate(df, glossary, output_dir, sheet_name=None, pool=None, resume_progress=None):
    pending = []
    for idx, row in df.iterrows():
        trans = row.get("translation")
        # 與 P1／P4 同口徑：None／NaN／NaT／pd.NA／空字串／哨兵字串都不是可用譯文，
        # 原文本身真的是 "nan"/"nat"/"none" 時（english= 提示）才算合法譯文
        if is_missing_translation(trans, english=_english_hint(row.get("english"))):
            continue
        ts = str(trans).strip()
        pending.append({
            "_idx": idx,
            "english": str(row.get("english", "")),
            "translation": ts,
            "sub_category": str(row.get("sub_category", "")),
        })
    if not pending:
        return df, []
    pending.sort(key=lambda x: (x.get("sub_category", ""), x.get("_idx", 0)))
    batches = _group_into_batches(pending)
    total_items = len(pending)
    print(f"  P2：{total_items} 條待評估，共 {len(batches)} 批")
    if pool is None:
        print("  錯誤：沒有可用的共享 API 池")
        return df, []
    total_batches = len(batches)
    tag, rr1, rr2, all_tasks = _plan_phase2_tasks(batches, resume_progress, output_dir, sheet_name)

    async def process_batch(ws, job):
        orig_bn, rnd, batch = job.ctx
        cfg = ws["cfg"]
        _timestamp(f"P2-R{rnd} 批次 {orig_bn}/{total_batches} 開始 ({len(batch)} 條) [{cfg.api_id} {cfg.model}]")
        try:
            results = await _evaluate_batch(ws["client"], cfg.model, batch, glossary, cfg.api_id)
            results = results[:len(batch)]
            # 正規化後的等級一律是非空字串（壞值→「严重」），計數與 skip_pairs 才一致
            normalized = []
            for res in results:
                if not isinstance(res, dict):
                    continue
                idx = res.get("index")
                if idx is None:
                    continue
                normalized.append(_normalize_eval_entry(res, idx))
            results = normalized
            success_count = sum(1 for r in results if r.get("level") and r.get("reason"))
            rr = rr1 if rnd == 1 else rr2
            part_data = {}
            for res in results:
                idx = res.get("index")
                key = str(idx)
                rr[key] = res
                part_data[key] = res
            if part_data:
                cd2 = _proofread_checkpoint_dir(output_dir, sheet_name)
                cd2.mkdir(parents=True, exist_ok=True)
                atomic_write_text(cd2 / f"{tag}_{orig_bn:06d}_r{rnd}.json", json.dumps(part_data, ensure_ascii=False))
                _update_phase2_progress(output_dir, sheet_name, tag, total_batches, orig_bn, rnd)
            _timestamp(f"P2-R{rnd} 批次 {orig_bn}/{total_batches} 完成 ({success_count} 條) [{cfg.api_id} {cfg.model}]")
        except Exception as e:
            if is_rate_limit_error(e):
                pool.note_429(cfg)
                if cfg.is_permanently_disabled:
                    print(f"  [{cfg.api_id}] 429，永久停用")
                else:
                    retries = getattr(cfg, "_429_retries", 0) + 1
                    cfg._429_retries = retries
                    print(f"  [{cfg.api_id}] 429，冷卻 60 秒 (retry #{retries})")
                pool.retry(job)
            else:
                print(f"  [{cfg.api_id}] 錯誤：{e}")
                if hasattr(e, "response") and hasattr(e.response, "text"):
                    try:
                        print(f"    回應: {e.response.text[:500]}")
                    except Exception:
                        pass

    try:
        await asyncio.gather(*[pool.submit(fbn, task, process_batch, ctx=task) for fbn, task in enumerate(all_tasks, 1)])
    except RuntimeError as e:
        # 不可吞掉：吞掉會讓 P2 從未真正執行，但工作表仍被標記完成、
        # checkpoint 與 session 一併被清除，使用者無法續傳補做
        if "all APIs permanently disabled" in str(e):
            print("  ⚠️ 所有 API 已永久停用，Phase 2 評估中斷")
            print(f"  P2 部分進度已保留（R1 {len(rr1)} 條），續傳時會從未完成批次繼續")
        raise
    round1_results = deepcopy(rr1)
    round2_results = deepcopy(rr2)
    _save_phase2_results(output_dir, sheet_name, round1_results, round2_results)
    _delete_phase2_progress(output_dir, sheet_name)
    second_category = []
    default_r2 = {"level": "严重", "reason": "评估缺失，预设为严重"}
    skipped = 0
    for idx_str, r1r in round1_results.items():
        # 續傳時 round1_results 來自 part 檔：單筆壞資料只跳過該筆
        try:
            idx = int(idx_str)
        except (TypeError, ValueError):
            skipped += 1
            continue
        if not isinstance(r1r, dict):
            skipped += 1
            continue
        r2r = round2_results.get(idx_str, default_r2)
        if not isinstance(r2r, dict):
            r2r = default_r2
        r1r = _normalize_eval_entry(r1r, idx)
        r2r = _normalize_eval_entry(r2r, idx)
        if _cross_reference(r1r, r2r):
            if idx in df.index:
                row = df.loc[idx]
                second_category.append({
                    "index": idx,
                    "english": str(row.get("english", "")),
                    "translation": str(row.get("translation", "")),
                    "round1": r1r,
                    "round2": r2r,
                })
    if skipped:
        print(f"  警告：P2 評估結果有 {skipped} 筆無效資料（非數字 key 或非物件），已略過")
    print(f"  P2 完成：{len(second_category)} 條第二類問題")
    return df, second_category


# === Phase 3: LLM Polish ===
SYSTEM_PROMPT_POLISH = (
    "你是一个 OSRS（Old School RuneScape）游戏文本的简体中文翻译专家。\n"
    "请对以下原文重新翻译，使译文更加流畅自然，减少翻译腔。\n"
    "请遵守以下规则：\n"
    "1. 只能输出简体中文，除非是占位符、标签或代码\n"
    "2. 保持游戏术语一致\n"
    "3. 保留 HTML/XML 标签（如 <col...>, <br>, </col> 等）\n"
    "4. 保留占位符（如 []内的文字）\n"
    "5. 流畅自然和避免翻译腔优先，不必完全忠实原文内容，但要記住OSRS是西方奇幻類遊戲，不應出現過於中式的字詞\n"
    "6. 每条翻译独立进行，不要合并或省略\n"
    "7. 输出格式为原始 JSON 阵列（不要使用 ```json 代码块包裹），每条物件包含 "
    "\"index\" 和 \"translation\" 两个栏位\n"
    "8. 如果术语表的人名、地名等名詞用'.'分隔，例如'索菲娅.休斯'，你必須使用'.'分隔，不要自行改為'·'或其他方式。"
    "你必須使用'.'分隔，不要自行改為'·'或其他方式。你必須使用'.'分隔，不要自行改為'·'或其他方式。\n"
    "## 强制术语表（禁止使用官方和社区通常的翻譯，遇到以下英文词必须使用指定的中文翻译）\n"
    "{RELEVANT_GLOSSARY}"
)


async def _polish_batch(client, model, batch, glossary, api_id):
    relevant = get_relevant_glossary(batch, glossary)
    glossary_str = "\n".join([f"  {e} \u2192 {c}" for e, c in relevant]) if relevant else "  \u65e0"
    system_prompt = SYSTEM_PROMPT_POLISH.replace("{RELEVANT_GLOSSARY}", glossary_str)
    user_content = json.dumps([
        {"index": item["_idx"], "english": item.get("english", "")}
        for item in batch
    ], ensure_ascii=False)
    text = ""
    for attempt in range(3):
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],

                temperature=0.1,
                timeout=API_TIMEOUT,
            )
            text = resp.choices[0].message.content.strip()
            text = re.sub(r"^```(?:json)?\n?", "", text, flags=re.IGNORECASE)
            text = re.sub(r"\n```$", "", text)
            results = json.loads(text)
            # 有些供應商 HTTP 200 但 body 是限流錯誤物件 → 當成 429 往上拋
            if is_rate_limit_payload(results):
                raise RateLimitPayloadError(f"429 rate limit in response payload: {text[:200]}")
            if isinstance(results, dict):
                known_keys = ["translations", "translated", "data", "results"]
                for key in known_keys:
                    if key in results:
                        candidate = results[key]
                        if isinstance(candidate, list) and len(candidate) > 0 and isinstance(candidate[0], dict):
                            results = candidate
                            break
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
            if isinstance(results, dict) and "index" in results and "translation" in results:
                print(f"    [{api_id}] \u26a0\ufe0f API \u56de\u50b3\u55ae\u4e00\u7269\u4ef6\u800c\u975e\u9663\u5217\uff08\u5617\u8a66 {attempt + 1}/3\uff09")
                if attempt < 2:
                    print(f"    [{api_id}] \u6e96\u5099\u91cd\u8a66...")
                    raise ValueError("single object returned, need array")
                else:
                    results = [results]
                    print(f"    [{api_id}] 3 \u6b21\u5617\u8a66\u5747\u56de\u50b3\u55ae\u4e00\u7269\u4ef6\uff0c\u5f37\u5236\u4f7f\u7528")
                    print(f"    [{api_id}] \u26a0\ufe0f \u5982\u679c\u591a\u6b21\u986f\u793a\u6b64\u8a0a\u606f\uff0c\u4ee3\u8868\u6b64 API \u4e0d\u9069\u5408\u7ffb\u8b6f\u4efb\u52d9\uff0c\u5efa\u8b70\u5f9e .env \u79fb\u9664\u6216\u66f4\u63db\u6a21\u578b")
            if isinstance(results, list) and len(results) > int(len(batch) * OVER_RETURN_TOLERANCE):
                raise ValueError(f"回傳 {len(results)} 條，超過批次 {len(batch)} 的 {int(OVER_RETURN_TOLERANCE * 100)}%，視為失敗重試")
            if isinstance(results, dict):
                results = [results]
            if isinstance(results, list):
                batch_indices = {item.get("_idx", 0) for item in batch}
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
                if rate < 0.75:
                    if attempt < 2:
                        print(f"    [{api_id}] \u26a0\ufe0f \u5b8c\u6210\u7387 {rate:.0%}\uff08{success}/{len(batch)}\uff09\u4f4e\u65bc 75%\uff0c\u91cd\u8a66")
                        raise ValueError(f"completion rate {rate:.0%} < 75%")
                    else:
                        print(f"    [{api_id}] \u26a0\ufe0f \u5b8c\u6210\u7387 {rate:.0%}\uff08{success}/{len(batch)}\uff09\u4f4e\u65bc 75%\uff0c\u5df2\u4f7f\u7528\u6240\u6709\u5617\u8a66")
                null_count = sum(1 for r in results if isinstance(r, dict) and r.get("translation") is None)
                if null_count > len(results) * 0.5:
                    print(f"    [{api_id}] \u8b66\u544a\uff1a{null_count}/{len(results)} \u689d\u7ffb\u8b6f\u70ba null")
                if results and not all("index" in r and "translation" in r for r in results):
                    print(f"    [{api_id}] \u8b66\u544a\uff1aJSON \u9663\u5217\u4e2d\u7f3a\u5c11 index/translation \u6b04\u4f4d")
                    print(f"    \u56de\u50b3\u5167\u5bb9\u524d 200 \u5b57: {text[:200]}")

            return results
        except json.JSONDecodeError:
            try:
                from json_repair import repair_json
                repaired = repair_json(text)
                if repaired:
                    results = json.loads(repaired)
                    if is_rate_limit_payload(results):
                        raise RateLimitPayloadError(
                            f"429 rate limit in response payload: {str(results)[:200]}")
                    print(f"    [{api_id}] ⚠️ JSON 修復成功")
                    if isinstance(results, dict):
                        if "translations" in results:
                            results = results["translations"]
                        elif "index" in results and "translation" in results:
                            raise ValueError("single object after repair, need array")
                    if isinstance(results, list) and len(results) > int(len(batch) * OVER_RETURN_TOLERANCE):
                        raise ValueError(f"回傳 {len(results)} 條，超過批次 {len(batch)} 的 {int(OVER_RETURN_TOLERANCE * 100)}%，視為失敗重試")
                    if isinstance(results, list):
                        batch_indices = {item.get("_idx", 0) for item in batch}
                        mapped = {}
                        for r in results:
                            if isinstance(r, dict) and r.get("index") in batch_indices:
                                mapped[r["index"]] = r
                        results = list(mapped.values())
                        success = sum(1 for r in results if isinstance(r, dict) and r.get("translation"))
                        rate = min(success, len(batch)) / len(batch) if len(batch) > 0 else 0
                        if rate < 0.75:
                            if attempt < 2:
                                print(f"    [{api_id}] ⚠️ 完成率 {rate:.0%}（{success}/{len(batch)}）低於 75%，重試")
                                raise ValueError(f"completion rate {rate:.0%} < 75%")
                            else:
                                print(f"    [{api_id}] ⚠️ 完成率 {rate:.0%}（{success}/{len(batch)}）低於 75%，已使用所有嘗試")
                        null_count = sum(1 for r in results if isinstance(r, dict) and r.get("translation") is None)
                        if null_count > len(results) * 0.5:
                            print(f"    [{api_id}] 警告：{null_count}/{len(results)} 条翻译为 null")
                        if results and not all("index" in r and "translation" in r for r in results):
                            print(f"    [{api_id}] 警告：JSON 陣列中缺少 index/translation 字段")
                            print(f"    回傳內容前 200 字: {text[:200]}")
                        return results
            except RateLimitPayloadError:
                # 修復後的內容仍是限流錯誤包裝 → 不可被吞掉，要往上拋成 429
                raise
            except Exception:
                pass
        except ValueError as e:
            print(f"    [{api_id}] \u932f\u8aa4 (\u5617\u8a66 {attempt + 1}/3): {e}")
            if attempt < 2:
                await asyncio.sleep(3)
        except Exception as e:
            if is_rate_limit_error(e):
                raise
            print(f"    [{api_id}] API \u932f\u8aa4 (\u5617\u8a66 {attempt + 1}/3): {e}")
            if attempt < 2:
                await asyncio.sleep(3)
    return [{"index": item.get("_idx", 0), "translation": None, "_error": "Polish failed"} for item in batch]

def _save_polish_checkpoint(output_dir, sheet_name, backup_data, rnd, failed_indices=None):
    cd = _proofread_checkpoint_dir(output_dir, sheet_name)
    cd.mkdir(parents=True, exist_ok=True)
    fp = cd / "polish_results.json"
    # 累積合併各輪結果，續傳時才能還原先前所有輪次的潤色成果
    old_data = {}
    if fp.exists():
        try:
            old = json.loads(fp.read_text(encoding="utf-8"))
            if isinstance(old, dict) and isinstance(old.get("data"), dict):
                old_data = old["data"]
        except Exception:
            pass
    merged = {**old_data, **backup_data}
    data = {"rounds_completed": rnd + 1, "data": merged}
    data["failed_indices"] = list(failed_indices or [])
    atomic_write_text(fp, json.dumps(data, ensure_ascii=False))


def _apply_polish_from_checkpoint(df, output_dir, sheet_name):
    fp = _proofread_checkpoint_dir(output_dir, sheet_name) / "polish_results.json"
    if fp.exists():
        restored = 0
        skipped = 0
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "rounds_completed" in data:
                backup = data.get("data", {})
            else:
                backup = data
            if not isinstance(backup, dict):
                print("    警告：潤色檢查點結構異常（非物件），略過還原")
                return df
            for idx_str, trans in backup.items():
                # 檢查點損壞或人為編輯：單筆壞資料只跳過該筆，不可讓其餘還原全部失效
                try:
                    idx = int(idx_str)
                except (TypeError, ValueError):
                    skipped += 1
                    continue
                if not (isinstance(trans, str) and trans.strip()):
                    skipped += 1
                    continue
                # 缺值哨兵（"nan"/"nat"/"none"，含 pd.NA 等 NA 純量）不是有效譯文：
                # 還原進 df 會被下游當成「已有譯文」，例如快速校對的 completed-sheet
                # 還原之後不再重譯，哨兵值會直接寫進輸出檔。
                # english= 一律走 _english_hint_at：英文欄的 NaN 不可變成字串 "nan" 而豁免。
                if is_missing_translation(trans, english=_english_hint_at(df, idx)):
                    skipped += 1
                    continue
                if idx in df.index:
                    # 潤色結果是本輪最新成果，理當覆蓋工作表中的舊譯文
                    df.at[idx, "translation"] = trans
                    restored += 1
            if skipped:
                print(f"    警告：潤色檢查點有 {skipped} 筆無效資料（非數字 key、非字串譯文或缺值哨兵），已略過")
            print(f"    從 checkpoint 還原 {restored} 條潤色結果")
        except Exception as e:
            print(f"    警告：潤色檢查點還原失敗（已略過）：{e}")
    return df


def _get_polish_round(output_dir, sheet_name) -> int:
    """回傳已完成的 P3 潤色輪次（0 = 尚未開始）。"""
    fp = _proofread_checkpoint_dir(output_dir, sheet_name) / "polish_results.json"
    if fp.exists():
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("rounds_completed"), int):
                return data["rounds_completed"]
        except Exception:
            pass
    return 0


def _is_empty_translation(trans, english=None) -> bool:
    """譯文是否為空／缺失（P3 只對這類條目重試）。

    與 P2／P4 同口徑：直接沿用 `is_missing_translation`（含 pd.NA／NaT、
    空字串與字串 "nan"/"nat"/"none"）。舊寫法只認 None／float NaN／空字串，
    於是同一格在 P3 被當成「已有譯文」而略過、在 P2／P4 卻被當成缺值。
    english：該列英文原文，原文真的是這些字時不算缺值（由 _english_hint 過濾，
    避免把 NaN 轉出來的 "nan" 誤當成原文）。
    """
    return is_missing_translation(trans, english=_english_hint(english))


def _has_polish_checkpoint(output_dir, sheet_name) -> bool:
    return _proofread_checkpoint_dir(output_dir, sheet_name).joinpath("polish_results.json").exists()


def _get_polish_failed_indices(output_dir, sheet_name) -> list:
    """回傳上一輪失敗（沒有新譯文結果）的條目索引。"""
    fp = _proofread_checkpoint_dir(output_dir, sheet_name) / "polish_results.json"
    if fp.exists():
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                failed = data.get("failed_indices", [])
                if not isinstance(failed, list):
                    print("    警告：潤色檢查點的 failed_indices 非陣列，視為空")
                    return []
                # 單一壞索引只跳過該筆，其餘仍要能續傳
                indices = []
                skipped = 0
                for i in failed:
                    try:
                        indices.append(int(i))
                    except (TypeError, ValueError):
                        skipped += 1
                if skipped:
                    print(f"    警告：failed_indices 有 {skipped} 筆非數字，已略過")
                return sorted(indices)
        except Exception:
            pass
    return []


def _resume_polish_indices(all_indices, df, failed_indices) -> list:
    """續傳 P3 時要處理的條目：譯文仍為空，或上一輪失敗但保留舊譯文。"""
    failed = set(failed_indices or [])
    result = []
    for idx in all_indices:
        if idx not in df.index:
            continue
        eng = df.at[idx, "english"] if "english" in df.columns else None
        if idx in failed or _is_empty_translation(df.at[idx, "translation"], eng):
            result.append(idx)
    return sorted(result)


async def polish_translations(df, second_category, glossary, output_dir, sheet_name=None, pool=None, start_round: int = 0, failed_indices=None):
    all_indices = set()
    for item in second_category:
        all_indices.add(item["index"])
    if not all_indices:
        return df

    if pool is None:
        print("  错误：没有可用的共享 API 池")
        return df

    MAX_ROUNDS = 3
    current_indices = sorted(all_indices)
    # 續傳：只保留「譯文仍為空」或「上一輪失敗」的條目，已潤色完成的不重送
    if start_round > 0:
        current_indices = _resume_polish_indices(all_indices, df, failed_indices)

    for rnd in range(start_round, MAX_ROUNDS):
        pending = []
        for idx in current_indices:
            if idx not in df.index:
                continue
            row = df.loc[idx]
            pending.append({
                "_idx": idx,
                "english": str(row.get("english", "")),
                "sub_category": str(row.get("sub_category", "")),
            })
        if not pending:
            break

        pending.sort(key=lambda x: (x.get("sub_category", ""), x.get("_idx", 0)))
        batches = _group_into_batches(pending)
        print(f"  P3 第 {rnd + 1}/{MAX_ROUNDS} 轮：{len(pending)} 条待润色，共 {len(batches)} 批")

        tag = uuid.uuid4().hex[:8]
        all_backup = {}
        total_batches = len(batches)

        async def process_batch(ws, job):
            cfg = ws["cfg"]
            bn, batch = job.batch_num, job.batch
            _timestamp(f"P3 批次 {bn}/{total_batches} 开始 ({len(batch)} 条) [{cfg.api_id} {cfg.model}]")
            try:
                results = await _polish_batch(ws["client"], cfg.model, batch, glossary, cfg.api_id)
                new_backup = {}
                skipped = 0
                for res in results:
                    if not isinstance(res, dict):
                        skipped += 1
                        continue
                    # 單筆格式異常只影響該筆，不可讓整個批次的其他潤色結果一起損失
                    try:
                        idx = res.get("index")
                        trans = res.get("translation")
                        # 只有非空字串才可寫入 translation 欄（dict／數字會在 str dtype 拋錯）；
                        # 缺值哨兵也不是譯文（與三條還原守門同口徑），跳過讓該列維持缺值重試
                        if (idx is None or not isinstance(trans, str) or not trans.strip()
                                or is_missing_translation(trans, english=_english_hint_at(df, idx))):
                            skipped += 1
                            continue
                        if idx in df.index:
                            df.at[idx, "translation"] = trans
                            new_backup[str(idx)] = str(trans)
                            all_backup[str(idx)] = str(trans)
                        else:
                            skipped += 1
                    except (TypeError, ValueError) as e:
                        skipped += 1
                        print(f"  [{cfg.api_id}] 警告：略過無效潤色回傳（index={res.get('index')!r}）：{e}")
                if skipped:
                    print(f"  [{cfg.api_id}] 警告：{skipped} 條回傳無效（非物件、index 不在工作表中或譯文非字串），已忽略")
                if new_backup:
                    save_backup_part(output_dir, tag, bn, new_backup, sheet_name)
                    sync_progress(output_dir, sheet_name)
                _timestamp(f"P3 批次 {bn}/{total_batches} 完成 ({len(new_backup)} 条) [{cfg.api_id} {cfg.model}]")
            except Exception as e:
                if is_rate_limit_error(e):
                    pool.note_429(cfg)
                    if cfg.is_permanently_disabled:
                        print(f"  [{cfg.api_id}] 429，永久停用")
                    else:
                        retries = getattr(cfg, "_429_retries", 0) + 1
                        cfg._429_retries = retries
                        print(f"  [{cfg.api_id}] 429，冷卻 60 秒 (retry #{retries})")
                    pool.retry(job)
                else:
                    print(f"  [{cfg.api_id}] 错误：{e}")
                    if hasattr(e, "response") and hasattr(e.response, "text"):
                        try:
                            print(f"    回应: {e.response.text[:500]}")
                        except Exception:
                            pass

        try:
            await asyncio.gather(*[pool.submit(bn, b, process_batch) for bn, b in enumerate(batches, 1)])
        except RuntimeError as e:
            if "all APIs permanently disabled" in str(e):
                print("  ⚠️ 所有 API 已永久停用，Phase 3 润色中断")
                if all_backup:
                    partial_failed = sorted(idx for idx in current_indices if str(idx) not in all_backup)
                    _save_polish_checkpoint(output_dir, sheet_name, all_backup, rnd - 1, failed_indices=partial_failed)
                    sync_progress(output_dir, sheet_name)
                print(f"  P3 完成（部分）：已储存 {len(all_backup)} 条结果")
                return df
            raise

        failed = sorted(idx for idx in current_indices if str(idx) not in all_backup)
        if all_backup or failed:
            _save_polish_checkpoint(output_dir, sheet_name, all_backup, rnd, failed_indices=failed)
            sync_progress(output_dir, sheet_name)

        _timestamp(f"P3 第 {rnd + 1} 轮：{len(all_backup)} 条已润色")

        # After polish, retry items that still have empty/failed translations
        if rnd < MAX_ROUNDS - 1:
            remaining = set()
            for idx in current_indices:
                if idx not in df.index:
                    continue
                trans = df.at[idx, "translation"]
                # 重送「譯文仍為空」或「本輪失敗但保留舊譯文」的條目
                eng = df.at[idx, "english"] if "english" in df.columns else None
                if _is_empty_translation(trans, eng) or idx in failed:
                    remaining.add(idx)

            current_indices = sorted(remaining)

            if not current_indices:
                print(f"  P3 第 {rnd + 1} 輪後：全部完成")
                break
            print(f"  P3 第 {rnd + 1} 輪後：{len(current_indices)} 條剩餘")
        else:
            print(f"  P3 完成")

    return df

# === Checkpoint Cleanup ===
def _delete_proofread_checkpoint(output_dir):
    cd = _proofread_checkpoint_dir(output_dir)
    if cd.exists():
        import shutil
        shutil.rmtree(cd)
        print("  校對 checkpoint 已清除：" + str(cd))


def _save_debug_info(api_id, model, batch_size, returned, success, text):
    import uuid
    from datetime import datetime
    debug_dir = get_debug_message_dir()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    uid = uuid.uuid4().hex[:4]
    (debug_dir / f"debug_{ts}_{uid}.json").write_text(
        json.dumps({
            "api_id": api_id,
            "model": model,
            "batch_size": batch_size,
            "returned": returned,
            "success": success,
            "rate": round(success / batch_size, 3) if batch_size > 0 else 0,
            "response_preview": text[:3000]
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"    [{api_id}] \u26a0\ufe0f {success}/{batch_size} \u4f4e\u7ffb\u8bd1\u7387\uff0c\u5df2\u5132\u5b58 debug \u8a0a\u606f")


# === Phase 4: Retry Protect ===
async def retry_protect(df, glossary, output_dir, sheet_name=None, pool=None):
    import pandas as pd
    # Compute relevant glossary (same logic as enforcer.enforce())
    all_text = " ".join(str(row.get("english", "")).lower() for _, row in df.iterrows())
    _ctx = build_relevance_context(all_text)
    relevant_glossary = filter_relevant_terms(glossary, all_text, _ctx)
    # 上界只是「不會漏」；收斂成真正命中的集合，讓後續掃描回到精確成本
    relevant_glossary = narrow_relevant_glossary(df, relevant_glossary)
    # Run retry directly in current event loop with the shared pool
    if pool is not None:
        df = await _enforce_async(df, relevant_glossary, output_dir=output_dir, sheet_name=sheet_name, shared_pool=pool)
    # Final scan (same as enforcer.enforce())
    final_pool = scan_issues(df, relevant_glossary)
    review_rows = []
    for idx, row, issues in final_pool:
        trans = row.get("translation")
        # 審查報告的「目前譯文」不可出現 "<NA>"／"nan" 這類缺值字面值
        trans_str = "" if is_missing_translation(
            trans, english=_english_hint(row.get("english"))) else str(trans)
        for issue_type, suggested_fix, desc in issues:
            review_rows.append({
                "english": str(row.get("english", "")),
                "current_translation": trans_str,
                "category": str(row.get("category", "")),
                "issue": f"[{issue_type}] {desc}",
            })
    review_df = pd.DataFrame(review_rows)
    return df, review_df


# === Backup ===
def backup_target_file(excel_path):
    backup_dir = Path(excel_path).parent / BACKUP_SUBDIR
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"{Path(excel_path).stem}_{ts}{Path(excel_path).suffix}"
    backup_path = backup_dir / backup_name
    import shutil
    shutil.copy2(excel_path, backup_path)
    print(f"  備份已建立：{backup_path}")
    return backup_path



def _dedup_report_rows(df: pd.DataFrame, subset, label: str) -> pd.DataFrame:
    """報告列去重：同一個問題（工作表＋原文＋問題描述）只保留最後一筆。

    同一個問題可能因多輪重譯或多次續傳被記錄多次（all_review_rows 逐輪串接，
    續傳時 session 的 accumulated_review_rows 再疊一次），報告因此出現重複列。
    後面的記錄才是最新一輪的結果，故 keep="last"。

    去重鍵另外納入譯文欄位（若存在）：同一工作表內同一句原文可能出現在不同列，
    只有問題描述相同、譯文不同時，那兩列是**不同的複查對象**，不可合併，
    否則使用者會看不到其中一列的問題。欄位缺失或值不可雜湊時退回原始資料，
    不讓報告產生流程失敗。
    """
    try:
        subset = list(subset) + [
            c for c in ("original_translation", "current_translation", "new_translation")
            if c in df.columns and c not in subset
        ]
        if not set(subset).issubset(df.columns):
            return df
        before = len(df)
        out = df.drop_duplicates(subset=list(subset), keep="last").reset_index(drop=True)
    except (TypeError, ValueError, KeyError) as e:
        print(f"    警告：{label} 去重失敗（保留原始列）：{e}")
        return df
    if len(out) != before:
        print(f"    {label} 去重：{before} → {len(out)} 條（同工作表＋原文＋問題描述＋譯文保留最後一筆）")
    return out


def generate_reports(excel_path, all_translated_dfs, all_original_dfs, all_second_category, all_review_rows: list[pd.DataFrame], output_dir, overall_start):
    from openpyxl.styles import Color, PatternFill
    out_path = Path(output_dir) / f"{Path(excel_path).stem}_proofread_output.xlsx"
    if all_translated_dfs:
        with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
            for s_name, df_s in all_translated_dfs.items():
                df_s.to_excel(writer, index=False, sheet_name=s_name)
        print(f"\n  校對輸出：{out_path}")
    all_report_rows = []
    for sheet_name in all_original_dfs:
        df_orig = all_original_dfs[sheet_name]
        df_pol = all_translated_dfs.get(sheet_name, df_orig)
        for item in all_second_category.get(sheet_name, []):
            idx = item["index"]
            eng = str(df_orig.at[idx, "english"]) if idx in df_orig.index else ""
            orig = str(item.get("translation", ""))
            new = str(df_pol.at[idx, "translation"]) if idx in df_pol.index else orig
            r1 = item["round1"]; r2 = item["round2"]
            detail = f"R1:{r1.get('level','?')}({r1.get('reason','')})|R2:{r2.get('level','?')}({r2.get('reason','')})"
            all_report_rows.append({"sheet_name": sheet_name, "english": eng, "original_translation": orig, "new_translation": new, "remark": f"Type2-{detail}"})
    # Type1：P4a 重譯保護後仍存在的機械問題（術語/佔位符/未翻譯/空格）
    for rdf in all_review_rows:
        for _, row in rdf.iterrows():
            all_report_rows.append({
                "sheet_name": str(row.get("sheet_name", "")),
                "english": str(row.get("english", "")),
                "original_translation": str(row.get("current_translation", "")),
                "new_translation": str(row.get("current_translation", "")),
                "remark": f"Type1-{row.get('issue', '')}",
            })
    if all_report_rows:
        rdf = pd.DataFrame(all_report_rows)
        rcols = ["sheet_name", "english", "original_translation", "new_translation", "remark"]
        rdf = rdf[rcols]
        rdf = _dedup_report_rows(rdf, ["sheet_name", "english", "remark"], "proofread_report")
        rp = Path(output_dir) / "proofread_report.xlsx"
        with pd.ExcelWriter(rp, engine="openpyxl") as writer:
            rdf.to_excel(writer, index=False, sheet_name="Sheet1")
            ws = writer.sheets["Sheet1"]
            # Type1: 背景顏色標記（標註問題類型）
            type1_fills = {
                "glossary": PatternFill(start_color=Color(rgb="D9D9D9"), end_color=Color(rgb="D9D9D9"), fill_type="solid"),
                "placeholder": PatternFill(start_color=Color(rgb="BDD7EE"), end_color=Color(rgb="BDD7EE"), fill_type="solid"),
                "untranslated": PatternFill(start_color=Color(rgb="F4CCCC"), end_color=Color(rgb="F4CCCC"), fill_type="solid"),
                "space": PatternFill(start_color=Color(rgb="E2EFDA"), end_color=Color(rgb="E2EFDA"), fill_type="solid"),
            }
            # Type2: 文字顏色標記（Type2-R1 部分藍字，其餘黑字）
            from openpyxl.cell.rich_text import CellRichText, TextBlock
            from openpyxl.cell.text import InlineFont
            f_blue = InlineFont(sz=11, color=Color(rgb="FF0070C0"))
            f_black = InlineFont(sz=11, color=Color(rgb="FF000000"))
            remark_col = None
            for ci, cell in enumerate(ws[1], start=1):
                if cell.value == "remark":
                    remark_col = ci; break
            if remark_col:
                for row in ws.iter_rows(min_row=2):
                    v = str(row[remark_col-1].value) if row[remark_col-1].value else ""
                    if v.startswith("Type1-"):
                        for t, fill in type1_fills.items():
                            if t in v:
                                for cell in row:
                                    cell.fill = fill
                                break
                    elif v.startswith("Type2-R1"):
                        parts = v.split("|R2:", 1)
                        if len(parts) == 2:
                            rich = CellRichText([TextBlock(f_blue, parts[0])])
                            rich += CellRichText([TextBlock(f_black, "|R2:" + parts[1])])
                        else:
                            rich = CellRichText([TextBlock(f_blue, v)])
                        row[remark_col-1].value = rich
                    # 其他 Type2（如 Type2-R2）保留黑色（預設）
        print(f"  proofread_report.xlsx 已寫入：{rp}（{len(rdf)} 條）")
    if all_review_rows:
        combined = pd.concat(all_review_rows, ignore_index=True)
        # 對應欄位名是 issue（不叫 remark）；同一問題只保留最後一筆
        combined = _dedup_report_rows(combined, ["sheet_name", "english", "issue"], "review_report_proofread")
        rvp = Path(output_dir) / "review_report_proofread.xlsx"
        with pd.ExcelWriter(rvp, engine="openpyxl") as writer:
            combined.to_excel(writer, index=False, sheet_name="Sheet1")
            ws = writer.sheets["Sheet1"]
            fg = PatternFill(start_color=Color(rgb="D9D9D9"), end_color=Color(rgb="D9D9D9"), fill_type="solid")
            fp = PatternFill(start_color=Color(rgb="BDD7EE"), end_color=Color(rgb="BDD7EE"), fill_type="solid")
            fu = PatternFill(start_color=Color(rgb="F4CCCC"), end_color=Color(rgb="F4CCCC"), fill_type="solid")
            ic = None
            fs = PatternFill(start_color=Color(rgb="E2EFDA"), end_color=Color(rgb="E2EFDA"), fill_type="solid")
            for ci, cell in enumerate(ws[1], start=1):
                if cell.value == "issue":
                    ic = ci; break
            if ic:
                for row in ws.iter_rows(min_row=2):
                    v = str(row[ic-1].value) if row[ic-1].value else ""
                    fill = fg if v.startswith("[glossary]") else (fp if v.startswith("[placeholder]") else (fu if v.startswith("[untranslated]") else (fs if v.startswith("[space]") else None)))
                    if fill:
                        for cell in row:
                            cell.fill = fill
        print(f"  review_report_proofread.xlsx 已寫入：{rvp}（{len(combined)} 條）")
    else:
        print(f"  review_report：未發現問題")
    cp_dir = _proofread_checkpoint_dir(output_dir)
    if cp_dir.exists():
        import shutil; shutil.rmtree(cp_dir)
        print(f"  已清除校對 checkpoint")
    ecp = Path(output_dir) / "_checkpoint"
    if ecp.exists():
        import shutil; shutil.rmtree(ecp)
        print(f"  已清除強制檢查 checkpoint")
    elapsed(overall_start, "總計")


def _build_session_data(excel_path, glossary_path, glossary_sheets, current_sheet, completed_sheets, pending_sheets, sheet_configs, accumulated_review_rows=None, mode: str = "proofread"):
    session = {
        "excel_path": str(Path(excel_path).resolve()),
        "glossary_path": str(glossary_path) if glossary_path else None,
        "glossary_sheets": glossary_sheets,
        "current_sheet": current_sheet,
        "completed_sheets": list(completed_sheets),
        "pending_sheets": list(pending_sheets),
        "sheet_configs": sheet_configs,
        "mode": mode,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if accumulated_review_rows:
        # 與 batch_translate 一致：存完整內容（dict 清單），而非只存數量
        rows = []
        for df in accumulated_review_rows:
            for _, r in df.iterrows():
                rows.append(r.to_dict())
        session["accumulated_review_rows"] = rows
    return session


def _remove_session_file(output_dir) -> None:
    """完成後清除 session，避免已完成的工作表再次觸發續傳提示。"""
    for sf in (_proofread_checkpoint_dir(output_dir) / SESSION_FILE, _quick_checkpoint_dir(output_dir) / SESSION_FILE):
        if sf.exists():
            try:
                sf.unlink()
            except OSError:
                pass


async def _proofread_phase2(sheet_name, st, glossary, workplace_str, pool):
    df = st["df"]
    _timestamp(f"P2 開始：{sheet_name}...")
    if _is_phase_complete(workplace_str, sheet_name, "phase2"):
        sc = _load_phase2_category(workplace_str, sheet_name, df)
        print(f"    P2 還原：{len(sc)} 條第二類問題")
    else:
        progress = _load_phase2_progress(workplace_str, sheet_name)
        if progress:
            print("    P2 續傳：從未完成批次繼續")
        df, sc = await phase2_llm_evaluate(df, glossary, workplace_str, sheet_name=sheet_name, pool=pool, resume_progress=progress)
    st["df"] = df
    st["sc"] = sc


async def _proofread_phase3(sheet_name, st, glossary, workplace_str, pool):
    df = st["df"]
    sc = st["sc"]
    _timestamp(f"P3 開始：{sheet_name}...")
    start_round = _get_polish_round(workplace_str, sheet_name)
    failed = _get_polish_failed_indices(workplace_str, sheet_name)
    if _has_polish_checkpoint(workplace_str, sheet_name):
        df = _apply_polish_from_checkpoint(df, workplace_str, sheet_name)
        if start_round > 0:
            print(f"    P3 續傳：從第 {start_round + 1} 輪繼續")
    df = await polish_translations(df, sc, glossary, workplace_str, sheet_name=sheet_name, pool=pool, start_round=start_round, failed_indices=failed)
    st["df"] = df


async def _proofread_phase4a(sheet_name, st, glossary, workplace_str, pool):
    df = st["df"]
    _timestamp(f"P4a 開始：{sheet_name}...")
    restored = _restore_enforce_backup(df, workplace_str, sheet_name)
    if restored > 0:
        print(f"    從重譯 checkpoint 還原 {restored} 條修正")
    df, review_df = await retry_protect(df, glossary, workplace_str, sheet_name=sheet_name, pool=pool)
    st["df"] = df
    st["review_df"] = review_df


async def run_proofread(excel_path, glossary_path, glossary_sheets, sheet_names, sheet_configs, session=None):
    overall_start = datetime.now()
    workplace = Path(excel_path).parent
    _timestamp("正在備份目標檔案...")
    backup_target_file(excel_path)
    _timestamp("正在載入術語庫...")
    t0 = datetime.now()
    if str(glossary_path) == "__AUTO__":
        glossary = auto_extract_glossary(excel_path, str(workplace))
    else:
        glossary = load_glossary(glossary_path, glossary_sheets)
    elapsed(t0, "術語庫")
    print(f"  術語庫載入完成：{len(glossary)} 條")
    all_translated_dfs = {}
    all_original_dfs = {}
    all_second_category = {}
    all_review_rows: list[pd.DataFrame] = []
    if session and session.get("accumulated_review_rows"):
        all_review_rows.append(pd.DataFrame(session["accumulated_review_rows"]))
        print(f"    還原 {len(session['accumulated_review_rows'])} 條審查記錄")
    completed_sheets = []
    pending_sheets = list(sheet_names)
    _save_session(str(workplace), _build_session_data(
        excel_path, glossary_path, glossary_sheets,
        pending_sheets[0] if pending_sheets else "",
        [], pending_sheets, sheet_configs))
    # Phase barrier: all sheets run each phase in parallel through the shared pool.
    pool = SharedBatchPool()
    try:
        await pool.start()
    except RuntimeError:
        # 只清除本次剛寫入的 session；不可刪整個 _proofread_checkpoint，
        # 那會把先前未完成（可續傳）的進度一起清掉（與 run_quick_proofread 同口徑）
        _sf = _proofread_checkpoint_dir(str(workplace)) / SESSION_FILE
        if _sf.exists():
            try:
                _sf.unlink()
            except OSError:
                pass
        raise
    active = [c for c in pool.api_configs if not c.is_permanently_disabled]
    print(f"  已載入 {len(active)} 個可用 API：")
    for cfg in active:
        print(f"    {cfg.api_id} {cfg.model_provider}/{cfg.model} ({cfg.api_type}, 並發={cfg.parallel_limit})")

    try:
        # Pre-read all worksheets
        sheet_states = {}
        for sheet_name in pending_sheets:
            df_original = pd.read_excel(excel_path, sheet_name=sheet_name, dtype=str)
            all_original_dfs[sheet_name] = df_original
            print(f"    總行數：{len(df_original)}")
            sheet_cfg = sheet_configs.get(sheet_name, {})
            selected_indices = set(sheet_cfg.get("selected_indices", []))
            if _is_phase_complete(str(workplace), sheet_name, "all"):
                if not selected_indices:
                    all_translated_dfs[sheet_name] = df_original
                else:
                    df = df_original[df_original.index.isin(selected_indices)].copy()
                    df = _apply_polish_from_checkpoint(df, str(workplace), sheet_name)
                    restored = _restore_enforce_backup(df, str(workplace), sheet_name)
                    if restored > 0:
                        print(f"    從重譯 checkpoint 還原 {restored} 條修正")
                    df, review_df = await retry_protect(df, glossary, str(workplace), sheet_name=sheet_name, pool=pool)
                    if not review_df.empty:
                        review_df.insert(0, "sheet_name", sheet_name)
                        all_review_rows.append(review_df)
                    df_original.update(df)
                    all_translated_dfs[sheet_name] = df_original
                    sc = _load_phase2_category(str(workplace), sheet_name, df)
                    all_second_category[sheet_name] = sc
                completed_sheets.append(sheet_name)
                print(f"  已從 checkpoint 還原 {sheet_name}")
                continue
            if not selected_indices:
                print(f"  {sheet_name} 未選取任何條目，跳過")
                all_translated_dfs[sheet_name] = df_original
                completed_sheets.append(sheet_name)
                continue
            mask = df_original.index.isin(selected_indices)
            df = df_original[mask].copy()
            sheet_states[sheet_name] = {
                "df_original": df_original, "df": df, "sc": [], "review_df": None,
            }

        # Phase 2: LLM dual-round evaluation (all sheets in parallel)
        t4 = datetime.now()
        _timestamp("Phase 2：LLM 評估（全部工作表並行）...")
        await asyncio.gather(*[
            _proofread_phase2(sheet_name, st, glossary, str(workplace), pool)
            for sheet_name, st in sheet_states.items()
        ])
        elapsed(t4, "P2")

        # Phase 3: LLM polish (all sheets in parallel)
        t5 = datetime.now()
        _timestamp("Phase 3：潤色（全部工作表並行）...")
        await asyncio.gather(*[
            _proofread_phase3(sheet_name, st, glossary, str(workplace), pool)
            for sheet_name, st in sheet_states.items()
        ])
        elapsed(t5, "P3")

        # Phase 4a: retry protect (all sheets in parallel)
        t6 = datetime.now()
        _timestamp("Phase 4a：重譯保護（全部工作表並行）...")
        results = await asyncio.gather(*[
            _proofread_phase4a(sheet_name, st, glossary, str(workplace), pool)
            for sheet_name, st in sheet_states.items()
        ], return_exceptions=True)
        for sheet_name, res in zip(sheet_states, results):
            if isinstance(res, BaseException):
                if isinstance(res, RuntimeError) and "all APIs permanently disabled" in str(res):
                    print("\n  ⚠️所有 API 已永久停用，校對中止")
                raise res
        elapsed(t6, "P4a")

        # Merge results
        for sheet_name, st in sheet_states.items():
            df = st["df"]
            all_second_category[sheet_name] = st["sc"]
            review_df = st["review_df"]
            if review_df is not None and not review_df.empty:
                review_df.insert(0, "sheet_name", sheet_name)
                all_review_rows.append(review_df)
            ecp = Path(str(workplace)) / "_checkpoint" / _sanitize_sheet_name(sheet_name) / "enforce_checkpoint.json"
            if ecp.exists():
                ecp.unlink()
            st["df_original"].update(df)
            all_translated_dfs[sheet_name] = st["df_original"]
            _mark_phase_complete(str(workplace), sheet_name, "all")
            completed_sheets.append(sheet_name)
            print(f"  {sheet_name} 完成")

        pending_sheets.clear()
        _save_session(str(workplace), _build_session_data(
            excel_path, glossary_path, glossary_sheets,
            "", completed_sheets, pending_sheets, sheet_configs,
            accumulated_review_rows=all_review_rows))
    finally:
        await pool.close()

    # Phase 4b：模板校正（在所有重譯完成後執行）
    t_p4b = datetime.now()
    _timestamp("Phase 4b：模板校正...")
    for sheet_name in all_translated_dfs:
        sheet_cfg = sheet_configs.get(sheet_name, {})
        selected_indices = set(sheet_cfg.get("selected_indices", []))
        if not selected_indices:
            continue
        df_original = all_translated_dfs[sheet_name]
        df = df_original[df_original.index.isin(selected_indices)].copy()
        df = template_correction(df, glossary, str(workplace), sheet_name=sheet_name)
        df_original.update(df)
        all_translated_dfs[sheet_name] = df_original
    elapsed(t_p4b, "P4b")

    generate_reports(excel_path, all_translated_dfs, all_original_dfs,
        all_second_category,
        all_review_rows, str(workplace), overall_start)
    _delete_proofread_checkpoint(str(workplace))
    _remove_session_file(str(workplace))


# === 快速校對（重譯模式）===

def _quick_checkpoint_dir(output_dir=None, sheet_name=None):
    if output_dir:
        base = Path(output_dir) / QUICK_CHECKPOINT_SUBDIR
    else:
        base = Path(__file__).parent / QUICK_CHECKPOINT_SUBDIR
    if sheet_name:
        base = base / _sanitize_sheet_name(sheet_name)
    return base


def _save_quick_session(output_dir, session_info):
    cd = _quick_checkpoint_dir(output_dir)
    cd.mkdir(parents=True, exist_ok=True)
    atomic_write_text(cd / SESSION_FILE, json.dumps(session_info, ensure_ascii=False, indent=2))


def _load_quick_session(output_dir):
    sf = _quick_checkpoint_dir(output_dir) / SESSION_FILE
    if sf.exists():
        try:
            return json.loads(sf.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


def _save_quick_sheet_translations(output_dir, sheet_name, translations):
    cd = _quick_checkpoint_dir(output_dir, sheet_name)
    cd.mkdir(parents=True, exist_ok=True)
    atomic_write_text(cd / "translations.json", json.dumps(translations, ensure_ascii=False))


def _load_quick_sheet_translations(output_dir, sheet_name) -> dict:
    f = _quick_checkpoint_dir(output_dir, sheet_name) / "translations.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_quick_sheet_review(output_dir, sheet_name, rows):
    cd = _quick_checkpoint_dir(output_dir, sheet_name)
    cd.mkdir(parents=True, exist_ok=True)
    atomic_write_text(cd / "review_rows.json", json.dumps(rows, ensure_ascii=False))


def _load_quick_sheet_review(output_dir, sheet_name) -> list:
    f = _quick_checkpoint_dir(output_dir, sheet_name) / "review_rows.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def _mark_quick_sheet_complete(output_dir, sheet_name):
    cd = _quick_checkpoint_dir(output_dir, sheet_name)
    cd.mkdir(parents=True, exist_ok=True)
    atomic_write_text(cd / "_all_done", json.dumps(
        {"phase": "all", "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S")}, ensure_ascii=False))


def _is_quick_sheet_complete(output_dir, sheet_name) -> bool:
    return (_quick_checkpoint_dir(output_dir, sheet_name) / "_all_done").exists()


def _restore_enforce_backup(df, output_dir, sheet_name) -> int:
    """把重譯（enforce）備份套回 df，僅在重譯確實被中斷時還原（依 enforce_tag 過濾 part）。"""
    cp = Path(output_dir) / "_checkpoint" / _sanitize_sheet_name(sheet_name)
    ecp = cp / "enforce_checkpoint.json"
    if not ecp.exists():
        return 0
    try:
        enforce_tag = json.loads(ecp.read_text(encoding="utf-8")).get("enforce_tag")
    except Exception:
        enforce_tag = None
    if not enforce_tag:
        return 0
    backup = {}
    for f in sorted(cp.glob(f"part_{enforce_tag}_*.json")):
        try:
            backup.update(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    restore_count = 0
    skipped = 0
    for idx_str, trans in backup.items():
        # 備份檔損壞或人為編輯：單筆壞資料只跳過該筆，不可讓整個工作表還原失敗
        try:
            idx = int(idx_str)
        except (TypeError, ValueError):
            skipped += 1
            continue
        if not (isinstance(trans, str) and trans.strip()):
            skipped += 1
            continue
        # 缺值哨兵（"nan"/"nat"/"none"，含 pd.NA 等 NA 純量）不是有效譯文：
        # 還原進 df 會讓該列被下游誤認為已有譯文（同 _apply_polish_from_checkpoint）
        if is_missing_translation(trans, english=_english_hint_at(df, idx)):
            skipped += 1
            continue
        if idx in df.index:
            # 重譯備份是上一次中斷前較新的修正結果，理當覆蓋工作表中的舊譯文
            df.at[idx, "translation"] = trans
            restore_count += 1
    if skipped:
        print(f"    警告：重譯備份有 {skipped} 筆無效資料（非數字 key、非字串譯文或缺值哨兵），已略過")
    return restore_count


async def _quick_phase4a(sheet_name, st, glossary, workplace_str, pool):
    df = st["df"]
    _timestamp(f"P4a 開始：{sheet_name}...")
    restored = _restore_enforce_backup(df, workplace_str, sheet_name)
    if restored > 0:
        print(f"    從重譯 checkpoint 還原 {restored} 條修正")
    df, review_df = await retry_protect(df, glossary, workplace_str, sheet_name=sheet_name, pool=pool)
    st["df"] = df
    st["review_df"] = review_df


def generate_quick_reports(excel_path, all_translated_dfs, all_review_rows: list[pd.DataFrame], output_dir, overall_start):
    from openpyxl.styles import Color, PatternFill
    out_path = Path(output_dir) / f"{Path(excel_path).stem}_quick_proofread_output.xlsx"
    if all_translated_dfs:
        with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
            for s_name, df_s in all_translated_dfs.items():
                df_s.to_excel(writer, index=False, sheet_name=s_name)
        print(f"\n  快速校對輸出：{out_path}")

    rp = Path(output_dir) / "quick_proofread_report.xlsx"
    if all_review_rows:
        combined = pd.concat(all_review_rows, ignore_index=True)
    else:
        combined = pd.DataFrame(columns=["sheet_name", "english", "current_translation", "category", "issue"])
    # 對應欄位名是 issue；同一問題只保留最後一筆（多輪／多次續傳會重複記錄）
    combined = _dedup_report_rows(combined, ["sheet_name", "english", "issue"], "quick_proofread_report")
    with pd.ExcelWriter(rp, engine="openpyxl") as writer:
        combined.to_excel(writer, index=False, sheet_name="Sheet1")
        ws = writer.sheets["Sheet1"]
        fill_glossary = PatternFill(start_color=Color(rgb="D9D9D9"), end_color=Color(rgb="D9D9D9"), fill_type="solid")
        fill_placeholder = PatternFill(start_color=Color(rgb="BDD7EE"), end_color=Color(rgb="BDD7EE"), fill_type="solid")
        fill_untranslated = PatternFill(start_color=Color(rgb="F4CCCC"), end_color=Color(rgb="F4CCCC"), fill_type="solid")
        fill_space = PatternFill(start_color=Color(rgb="E2EFDA"), end_color=Color(rgb="E2EFDA"), fill_type="solid")
        ic = None
        for ci, cell in enumerate(ws[1], start=1):
            if cell.value == "issue":
                ic = ci
                break
        if ic:
            for row in ws.iter_rows(min_row=2):
                v = str(row[ic - 1].value) if row[ic - 1].value else ""
                fill = (fill_glossary if v.startswith("[glossary]")
                        else fill_placeholder if v.startswith("[placeholder]")
                        else fill_untranslated if v.startswith("[untranslated]")
                        else fill_space if v.startswith("[space]") else None)
                if fill:
                    for cell in row:
                        cell.fill = fill
    print(f"  quick_proofread_report.xlsx 已寫入：{rp}（{len(combined)} 條）")

    qcp = _quick_checkpoint_dir(output_dir)
    if qcp.exists():
        import shutil
        shutil.rmtree(qcp)
        print(f"  已清除快速校對 checkpoint")
    ecp = Path(output_dir) / "_checkpoint"
    if ecp.exists():
        import shutil
        shutil.rmtree(ecp)
        print(f"  已清除強制檢查 checkpoint")
    elapsed(overall_start, "總計")


async def run_quick_proofread(excel_path, glossary_path, glossary_sheets, sheet_names, sheet_configs, session=None):
    overall_start = datetime.now()
    workplace = Path(excel_path).parent
    _timestamp("正在備份目標檔案...")
    backup_target_file(excel_path)
    _timestamp("正在載入術語庫...")
    t0 = datetime.now()
    if str(glossary_path) == "__AUTO__":
        glossary = auto_extract_glossary(excel_path, str(workplace))
    else:
        glossary = load_glossary(glossary_path, glossary_sheets)
    elapsed(t0, "術語庫")
    print(f"  術語庫載入完成：{len(glossary)} 條")

    all_translated_dfs = {}
    all_review_rows: list[pd.DataFrame] = []
    if session and session.get("accumulated_review_rows"):
        all_review_rows.append(pd.DataFrame(session["accumulated_review_rows"]))
        print(f"    還原 {len(session['accumulated_review_rows'])} 條審查記錄")
    completed_sheets = []
    pending_sheets = list(sheet_names)
    _save_quick_session(str(workplace), _build_session_data(
        excel_path, glossary_path, glossary_sheets,
        pending_sheets[0] if pending_sheets else "",
        [], pending_sheets, sheet_configs, mode="quick_proofread"))

    pool = SharedBatchPool()
    try:
        await pool.start()
    except RuntimeError:
        # 只清除本次剛寫入的 session；不可 rmtree 整個 _quick_checkpoint，
        # 那會把先前已完成工作表的 translations.json / _all_done 一起刪掉
        _sf = _quick_checkpoint_dir(str(workplace)) / SESSION_FILE
        if _sf.exists():
            try:
                _sf.unlink()
            except OSError:
                pass
        raise
    active = [c for c in pool.api_configs if not c.is_permanently_disabled]
    print(f"  已載入 {len(active)} 個可用 API：")
    for cfg in active:
        print(f"    {cfg.api_id} {cfg.model_provider}/{cfg.model} ({cfg.api_type}, 並發={cfg.parallel_limit})")

    try:
        sheet_states = {}
        for sheet_name in pending_sheets:
            df_original = pd.read_excel(excel_path, sheet_name=sheet_name, dtype=str)
            print(f"    總行數：{len(df_original)}")
            sheet_cfg = sheet_configs.get(sheet_name, {})
            selected_indices = set(sheet_cfg.get("selected_indices", []))
            if _is_quick_sheet_complete(str(workplace), sheet_name):
                df_full = df_original.copy()
                saved = _load_quick_sheet_translations(str(workplace), sheet_name)
                restore_count = 0
                skipped = 0
                if not isinstance(saved, dict):
                    print(f"    警告：{sheet_name} 的快速校對檢查點結構異常（非物件），略過還原")
                    saved = {}
                for idx_str, trans in saved.items():
                    # 檢查點損壞或人為編輯：單筆壞資料只跳過該筆，不可讓整個工作表還原失敗
                    try:
                        idx = int(idx_str)
                    except (TypeError, ValueError):
                        skipped += 1
                        continue
                    if not (isinstance(trans, str) and trans.strip()):
                        skipped += 1
                        continue
                    # 缺值哨兵（"nan"/"nat"/"none"，含 pd.NA 等 NA 純量）不是有效譯文：
                    # 這條路徑還原後不再重譯，哨兵值會直接寫進輸出檔
                    if is_missing_translation(trans, english=_english_hint_at(df_full, idx)):
                        skipped += 1
                        continue
                    if idx in df_full.index:
                        # 快速校對結果是本輪最新成果，理當覆蓋來源檔的舊譯文
                        df_full.at[idx, "translation"] = trans
                        restore_count += 1
                if skipped:
                    print(f"    警告：{sheet_name} 檢查點有 {skipped} 筆無效資料（非數字 key、非字串譯文或缺值哨兵），已略過")
                if restore_count > 0:
                    print(f"    從 checkpoint 還原 {restore_count} 條修正")
                rows = _load_quick_sheet_review(str(workplace), sheet_name)
                if rows:
                    all_review_rows.append(pd.DataFrame(rows))
                    print(f"    還原 {len(rows)} 條審查記錄")
                all_translated_dfs[sheet_name] = df_full
                completed_sheets.append(sheet_name)
                print(f"  已從 checkpoint 還原 {sheet_name}")
                continue
            if not selected_indices:
                print(f"  {sheet_name} 未選取任何條目，跳過")
                all_translated_dfs[sheet_name] = df_original
                completed_sheets.append(sheet_name)
                continue
            df = df_original[df_original.index.isin(selected_indices)].copy()
            sheet_states[sheet_name] = {"df_original": df_original, "df": df}

        t = datetime.now()
        _timestamp("Phase 4a：重譯修正（全部工作表並行）...")
        results = await asyncio.gather(*[
            _quick_phase4a(sheet_name, st, glossary, str(workplace), pool)
            for sheet_name, st in sheet_states.items()
        ], return_exceptions=True)
        for sheet_name, res in zip(sheet_states, results):
            if isinstance(res, BaseException):
                if isinstance(res, RuntimeError) and "all APIs permanently disabled" in str(res):
                    print("\n  ⚠️所有 API 已永久停用，快速校對中止")
                raise res
        elapsed(t, "P4a")

        for sheet_name, st in sheet_states.items():
            df = st["df"]
            review_df = st["review_df"]
            if review_df is not None and not review_df.empty:
                review_df.insert(0, "sheet_name", sheet_name)
                all_review_rows.append(review_df)
            ecp = Path(str(workplace)) / "_checkpoint" / _sanitize_sheet_name(sheet_name) / "enforce_checkpoint.json"
            if ecp.exists():
                ecp.unlink()
            st["df_original"].update(df)
            all_translated_dfs[sheet_name] = st["df_original"]
            saved = {}
            for idx in df.index:
                trans = df.at[idx, "translation"]
                # NaN 不是 None：只判 is not None 會把空譯文存成字串 "nan"。
                # english= 必須用 _english_hint_at：直接 str() 會把缺值的英文欄
                # 變成字串 "nan"／"None"，讓字面譯文 "nan" 被誤判成合法譯文；
                # 該函式同時處理「沒有 english 欄」與「索引重複回 Series」的情況
                if not is_missing_translation(trans, english=_english_hint_at(df, idx)):
                    saved[str(idx)] = str(trans)
            _save_quick_sheet_translations(str(workplace), sheet_name, saved)
            if review_df is not None and not review_df.empty:
                _save_quick_sheet_review(str(workplace), sheet_name, review_df.to_dict(orient="records"))
            _mark_quick_sheet_complete(str(workplace), sheet_name)
            completed_sheets.append(sheet_name)
            print(f"  {sheet_name} 完成")

        pending_sheets.clear()
        _save_quick_session(str(workplace), _build_session_data(
            excel_path, glossary_path, glossary_sheets,
            "", completed_sheets, pending_sheets, sheet_configs, mode="quick_proofread"))
    finally:
        await pool.close()

    # Phase 4b：模板校正（在所有工作表完成後執行；TEMPLATES 為空時無作用）
    t_p4b = datetime.now()
    _timestamp("Phase 4b：模板校正...")
    for sheet_name in all_translated_dfs:
        sheet_cfg = sheet_configs.get(sheet_name, {})
        selected_indices = set(sheet_cfg.get("selected_indices", []))
        if not selected_indices:
            continue
        df_original = all_translated_dfs[sheet_name]
        df = df_original[df_original.index.isin(selected_indices)].copy()
        df = template_correction(df, glossary, str(workplace), sheet_name=sheet_name)
        df_original.update(df)
        all_translated_dfs[sheet_name] = df_original
    elapsed(t_p4b, "P4b")

    generate_quick_reports(excel_path, all_translated_dfs, all_review_rows, str(workplace), overall_start)
    _remove_session_file(str(workplace))

def main():
    print("校對模組已載入。請使用 batch_proofread.py 執行 CLI。")

if __name__ == "__main__":
    main()
