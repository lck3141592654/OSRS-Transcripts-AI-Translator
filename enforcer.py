import pandas as pd
from pathlib import Path
from collections import Counter
import json, asyncio, time, re
from dotenv import load_dotenv
from datetime import datetime

load_dotenv(Path(__file__).resolve().parent / ".env")

from glossary import (find_term_spans, build_relevance_context,
                      filter_relevant_terms, MISSING_SENTINELS, TermLookup,
                      drop_placeholder_spans, placeholder_spans,
                      keep_english_terms)
from llm_translator import (
    BATCH_SIZE_LIMIT, CHECKPOINT_SUBDIR, atomic_write_text, get_relevant_glossary,
    is_missing_translation, _english_hint, _english_hint_at,
    _is_na, english_fingerprint_df, _checkpoint_dir,
)
from api_config import (
    OVER_RETURN_TOLERANCE,
    is_rate_limit_error, is_rate_limit_payload, RateLimitPayloadError,
)
from shared_pool import SharedBatchPool

# 重譯（Phase C／P4a）取樣溫度。修正型任務仍以確定性為主，但保留一點彈性
# 讓模型能換個寫法。任何結束迴圈的輪次都可能是實質最後一輪（問題 <3 條、
# 修正率 <5% 都會提前結束），且 regressed 守門只擋「未翻譯／佔位符」兩類，
# 因此升溫寫回的術語類新錯誤沒有兜底。
RETRY_TEMPERATURE = 0.2

RETRY_PROMPTS = [
    "1. 只能输出简体中文，除非是占位符、标签或代码。"
    "2. 发送内容在以往翻译时出现游戏术语错误、占位符被翻译、输出原文或空白内容的问题，所以你要注意避免翻译 [] 内的内容、HTML/XML 标签（如 <col...>, <br>, </col> 等），和避免游戏术语错误。"
    "3. 如果术语表的人名、地名等名詞用'.'分隔，例如'索菲娅.休斯'，你必須使用'.'分隔，不要自行改為'·'或其他方式："
    "4. 翻译时要保持流畅自然和避免翻译腔，不必完全忠实原文内容。"
    "5. 每条翻译独立进行，不要合并或省略。",
    "只能输出简体中文，除非是占位符、标签或代码。"
    "发送内容多次在以往翻译时出现游戏术语错误、占位符被翻译、输出原文或空白内容的问题，请你在翻译时避免以上问题，并保持流畅自然和避免翻译腔，不必完全忠实原文内容。"
    "每条翻译独立进行，不要合并或省略。"
    "如果术语表的人名、地名等名詞用'.'分隔，例如'索菲娅.休斯'，你必須使用'.'分隔，不要自行改為'·'或其他方式：",
    "只能输出简体中文，除非是占位符、标签或代码。"
    "发送内容多次在以往翻译时出现游戏术语错误、占位符被翻译、输出原文或空白内容的问题，请你在翻译时避免以上问题，并保持流畅自然和避免翻译腔，不必完全忠实原文内容。"
    "每条翻译独立进行，不要合并或省略。"
    "如果术语表的人名、地名等名詞用'.'分隔，例如'索菲娅.休斯'，你必須使用'.'分隔，不要自行改為'·'或其他方式：",
]

def _find_matched_spans(eng_lower: str, glossary: dict, ctx=None) -> list:
    """對每條原文找出所有匹配到（詞邊界）的術語及其 span，按術語長度降序排列。

    回傳 list[(eng, chn, [(start, end), ...])]
    ctx：由呼叫端預先建立的匹配上下文（build_relevance_context），
    一行文本只建立一次、所有術語共用，避免每術語重掃文本（大表掃描 10x 加速）。
    """
    matches = []
    for eng, chn in glossary.items():
        spans = find_term_spans(eng, eng_lower, ctx)
        if spans:
            matches.append((eng, chn, spans))
    # 長術語優先處理（避免短術語被長術語覆蓋時誤報）
    matches.sort(key=lambda x: len(x[0]), reverse=True)
    return matches


def _join_all_english(df) -> str:
    """把整張工作表的英文原文串成一段小寫文本。

    必須用 map(str) 而不是 fillna("")：原本的 str(row.get("english","")) 會把
    NaN 變成字面 "nan"，用 fillna 會讓文本少 3 個字元、與舊行為不等價。
    也不可用 astype(str)：pandas 3.0 對缺值是 no-op，join 時會直接 TypeError。
    """
    if "english" not in df.columns:
        return " ".join([""] * len(df)).lower()
    return " ".join(df["english"].map(str).str.lower().tolist())


def build_retry_glossary_text(glossary: dict, batch: list, lookup=None) -> str:
    """只把當前重譯批次實際出現的術語做成提示詞文字，避免整張工作表術語造成超 token。"""
    relevant = get_relevant_glossary(batch, glossary, lookup)
    if not relevant:
        return "  无"
    return "\n".join([f"  {e} → {c}" for e, c in relevant])


def check_glossary_usage(english_text: str, translated_text: str, glossary: dict, ctx=None) -> list:
    """詞邊界匹配，防止『V』匹配到『Convert』或『Don』匹配到『don't』這類誤報。

    當原文中長術語（如 "Lord Hosidius"）的翻譯已正確出現在譯文中，
    則其範圍內的短術語（如 "Hosidius"）不再重複檢查。
    ctx：可選的預建匹配上下文（build_relevance_context），由 scan_issues 每行建立一次。
    [...] 佔位符內的英文不參與術語比對：佔位符內容必須逐字保留，術語只出現在
    佔位符裡時（例如 [Fremennik name]）不算命中，也不要求譯文出現中文譯名。
    """
    issues = []
    eng_lower = english_text.lower()
    trans_lower = translated_text.lower()
    ph_spans = placeholder_spans(eng_lower)

    matches = _find_matched_spans(eng_lower, glossary, ctx)

    covered_by_correct = []  # 已被正確翻譯的長術語所覆蓋的 span 集合
    for eng, chn, spans in matches:
        spans = drop_placeholder_spans(spans, eng_lower, ph_spans)
        if not spans:
            continue
        # 檢查此術語的所有匹配 span 是否都被已覆蓋
        all_covered = all(
            any(s >= cs[0] and e <= cs[1] for cs in covered_by_correct)
            for s, e in spans
        )
        if all_covered:
            continue  # 已被更長且翻譯正確的術語覆蓋，跳過

        if chn.lower() in trans_lower:
            # 翻譯正確，將此術語的 span 加入已覆蓋集合
            covered_by_correct.extend(spans)
        else:
            issues.append((eng, chn, f"术语「{eng}」应为「{chn}」"))

    return issues

def check_placeholder(english_text: str, translated_text: str) -> list[str]:
    """比對原文和譯文中的 [...] 是否一致，返回問題描述清單

    除了「原文的每個佔位符是否出現」，也比對兩邊的數量與譯文多出的佔位符：
    原文 [a] [a] 而譯文只剩一個 [a]、或譯文自行多出 [name]，原本都不會被發現。
    """
    issues = []
    eng_cnt = Counter(re.findall(r'\[.*?\]', english_text))
    trans_cnt = Counter(re.findall(r'\[.*?\]', translated_text))
    if eng_cnt == trans_cnt:
        return issues
    for ph, n in eng_cnt.items():
        got = trans_cnt.get(ph, 0)
        if got == 0:
            issues.append(f"佔位符「{ph}」被翻譯或遺失")
        elif got != n:
            issues.append(f"佔位符「{ph}」數量不符（原文 {n} 個，譯文 {got} 個）")
    for ph in trans_cnt:
        if ph not in eng_cnt:
            issues.append(f"譯文多出佔位符「{ph}」")
    return issues


def check_untranslated(english_text: str, translated_text, keep_original_terms: set[str] | None = None) -> list[str]:
    """檢查是否未成功翻譯，返回問題描述清單。

    keep_original_terms：術語庫（ADD_LIST 等）明確要求保持原文的術語集合，
    這些條目譯文與原文相同是「正確行為」，不視為未翻譯。
    集合內一律存小寫（見 scan_issues 的建構式），比對時也把小寫化，
    否則工作表原文的大小寫與 ADD_LIST 的 key 不同時仍會被判為未翻譯。

    原文缺值（空白列）直接回傳 []：沒有原文就沒有「翻譯得對不對」可言。
    舊寫法會把 english=NaN 的列判成「translation 欄位為空」，於是空白列被
    當成問題列送進重譯（提示詞出現 "english": "nan"），永遠修不好；
    scan_issues 另有一層跳過，這裡的早退是給直接呼叫本函式的端點用的。
    """
    # 原文是空值或純空白 → 不判未翻譯（_is_na 與 is_missing_translation 同口徑）
    if _is_na(english_text) or not str(english_text).strip():
        return []
    issues = []
    keep_original_terms = keep_original_terms or set()
    eng_str = str(english_text).strip()
    # 情況 A：translation 為空（含字串 nan/nat/none，但原文本身是這些字時除外）
    # 原文提示值一律走 _english_hint：english_text 可能是 float NaN（英文欄空白），
    # 先 str() 會變成字串 "nan" 而被當成「原文真的是 nan」錯誤豁免（P1 與 P4 分歧）
    if is_missing_translation(translated_text, english=_english_hint(english_text)):
        issues.append("translation 欄位為空")
        return issues
    trans_str = str(translated_text).strip()
    # 情況 B：譯文與原文相同（不分大小寫）
    if trans_str.lower() == eng_str.lower():
        # 原文本身是 nan/nat/none 這類字時，相同譯文是合法結果
        # （哨兵集合與 is_missing_translation 共用同一份 glossary.MISSING_SENTINELS，
        #  避免這裡只擋 "nan" 而放行 "nat"/"none"）
        if eng_str.lower() in MISSING_SENTINELS:
            return issues
        # ADD_LIST 明確要求保持原文的術語：直接視為正確
        # 集合存小寫、比對也小寫，大小寫不同（OSRS / osrs / Osrs）時才不會誤判
        if eng_str.strip().lower() in keep_original_terms:
            return issues
        if re.search(r"[a-zA-Z]", eng_str):
            # 排除純佔位符條目：去除 [] 後只留下空白或標點符號時不視為未翻譯
            stripped = re.sub(r"\[.*?\]", "", eng_str).strip()
            if stripped and re.search(r"[a-zA-Z]", stripped):
                issues.append("譯文與原文相同，未實際翻譯")
    return issues


def _fatal(eng_raw, candidate, keep: frozenset[str]) -> set[str]:
    """回傳這一筆候選譯文的「致命問題」集合（{'untranslated', 'placeholder'} 的子集）。

    只跑 check_untranslated + check_placeholder 兩個便宜檢查，刻意不含 glossary：
    術語檢查要對整份詞庫逐條比對，實測全詞庫 146 ms/列，套在「每次寫入前」
    會讓重譯慢上數千倍（R1 1-A9）。術語錯由重譯提示詞與下一輪掃描處理，
    不需要、也不可以在這裡當成寫入守門。

    keep 是 keep_english_terms() 的結果，由呼叫端算好傳入；本函式不重算。
    """
    out: set[str] = set()
    if check_untranslated(eng_raw, candidate, keep_original_terms=keep):
        out.add("untranslated")
    # candidate 本身缺值時不能拿 str(candidate) 去比對佔位符：
    # 那會把 "nan" 當成一般文字，與「譯文空白」的語意混淆
    if not is_missing_translation(candidate) and check_placeholder(str(eng_raw), str(candidate)):
        out.add("placeholder")
    return out


def regressed(eng_raw, previous, candidate, keep: frozenset[str] = frozenset()) -> str | None:
    """回傳拒絕理由字串；None = 放行。規則是「不得比原本更差」。

    為什麼不是「必須通過所有檢查」：既有成品本來就有大量帶舊問題的譯文
    （R1 實測 329 列會被全通過式守門擋下），把「沒變好」當成「變差」會讓
    覆寫路徑把可用的譯文擋在門外，列永遠停在壞值、還會多燒一輪。
    這裡只追究「候選新增了原本沒有的致命問題」。

    原文缺值（空白列）一律放行：沒有原文可翻，就沒有未翻譯／佔位符可壞。
    previous 是「本次寫入前 df 中的值」，可能是 NaN／None／pd.NA。
    """
    if _is_na(eng_raw):
        return None
    before = _fatal(eng_raw, previous, keep)
    after = _fatal(eng_raw, candidate, keep)
    new = after - before
    # 「把未翻譯修好」優先於「佔位符缺失」：前值還沒有譯文（或只是回吐原文）時，
    # 候選只要真的產生中文就已是一大進步；此時掉佔位符是可以在下一輪由
    # preprocess_issues 修、也會進審查報告的次要問題，不該把有進展的候選擋掉
    # （擋掉等於讓該列永遠停在英文／空白，比「有譯文但少了 [x]」更糟）。
    if "untranslated" in before and "untranslated" not in after:
        new.discard("placeholder")
    if new:
        return f"新增致命問題 {'、'.join(sorted(new))}"
    return None


def check_space_issues(translated_text: str) -> list:
    """檢查譯文中的空格問題，回傳問題清單 list[(type, suggested_fix, desc)]

    英文半形標點的左右空格是正常英文格式，不列入問題；
    只有英文括號（半形 ()）旁的空格屬於括號排版問題。

    缺值判定改用 `is_missing_translation`（與其他站點同口徑）：None／NaN／NaT／
    pd.NA／空字串與純空白都沒有「空格問題」可報，一律提前回傳；
    舊寫法只認 float NaN，pd.NA／NaT 會被 `str()` 成 "<NA>"/"NaT" 後繼續檢查。
    """
    issues = []
    if is_missing_translation(translated_text):
        return issues
    trans_str = str(translated_text)
    if "  " in trans_str:
        issues.append(("space", "", "存在連續多個空格"))
    if re.search(r"[\u4e00-\u9fff] [\u4e00-\u9fff]", trans_str):
        issues.append(("space", "", "中文字間不應有空格"))
    if re.search(r" [\uff0c\u3002\uff01\uff1f\uff1b\uff1a\u300c\u300d\u300e\u300f\uff08\uff09\u3010\u3011\u300a\u300b\u2014\u2014\u2026\u2026\u00b7\u3001]", trans_str):
        issues.append(("space", "", "空格後不應直接接中文標點符號"))
    if re.search(r"[\uff0c\u3002\uff01\uff1f\uff1b\uff1a\u300c\u300d\u300e\u300f\uff08\uff09\u3010\u3011\u300a\u300b\u2014\u2014\u2026\u2026\u00b7\u3001] ", trans_str):
        issues.append(("space", "", "中文標點符號後不應有空格"))
    if re.search(r"\( | \)", trans_str):
        issues.append(("space", "", "英文括號內側不應有空格"))
    if re.search(r"[\u4e00-\u9fff] \(|\) [\u4e00-\u9fff]", trans_str):
        issues.append(("space", "", "中文語境下英文括號外側不應有空格"))
    return issues


def fix_single_placeholder(english_text: str, translated_text: str) -> str:
    """若原文只有一個 [] 佔位符，直接用原文的 [] 內容覆蓋譯文的 [] 內容"""
    eng_phs = re.findall(r'\[(.*?)\]', english_text)
    if len(eng_phs) != 1:
        return translated_text
    target_ph = f"[{eng_phs[0]}]"
    trans_phs = re.findall(r'\[(.*?)\]', translated_text)
    if trans_phs:
        old_ph = f"[{trans_phs[0]}]"
        if old_ph != target_ph:
            return translated_text.replace(old_ph, target_ph)
    return translated_text


def fix_space_issues(text: str) -> str:
    """清除不合格空格，保留正常英文空格。

    英文半形標點的前後空格是正常格式，絕不刪除；
    英文括號（半形 ()）旁的空白屬於括號排版問題，會修正為緊貼括號。
    """
    text = re.sub(r" {2,}", " ", text)
    text = re.sub(r"(?<=[\u4e00-\u9fff]) (?=[\u4e00-\u9fff])", "", text)
    text = re.sub(r" ([\uff0c\u3002\uff01\uff1f\uff1b\uff1a\u300c\u300d\u300e\u300f\uff08\uff09\u3010\u3011\u300a\u300b\u2014\u2014\u2026\u2026\u00b7\u3001])", r"\1", text)
    text = re.sub(r"([\uff0c\u3002\uff01\uff1f\uff1b\uff1a\u300c\u300d\u300e\u300f\uff08\uff09\u3010\u3011\u300a\u300b\u2014\u2014\u2026\u2026\u00b7\u3001]) ", r"\1", text)
    # 英文括號（半形 ()）：
    # 1) 括號內側的空格一律移除（( word → (word、word ) → word)）
    # 2) 括號外側的空格只在中文語境移除（中文前後緊貼括號）；英文句子保留正常空格
    text = re.sub(r"\( ", "(", text)
    text = re.sub(r" \)", ")", text)
    text = re.sub(r"([\u4e00-\u9fff]) \(", r"\1(", text)
    text = re.sub(r"\) ([\u4e00-\u9fff])", r")\1", text)
    return text


def preprocess_issues(df, pool):
    """對問題條目進行腳本預處理（單佔位符修正 + 空格修正），直接修改 df"""
    for idx, row, issues in pool:
        # 未翻譯的列交給 LLM 重譯；若在此先改寫譯文，「譯文＝原文」的判定會失效，
        # 該列會從問題池消失、既不重譯也不進報告
        if any(t == "untranslated" for t, _, _ in issues):
            continue
        eng = str(row.get("english", ""))
        trans = row.get("translation")
        # 缺值（含 pd.NA／NaT／空字串／哨兵字串）沒有可修正的譯文可改；
        # english= 讓原文本身真的是 "nan"/"nat"/"none" 時不誤判
        if is_missing_translation(trans, english=_english_hint(row.get("english"))):
            continue
        new_trans = str(trans)
        new_trans = fix_single_placeholder(eng, new_trans)
        new_trans = fix_space_issues(new_trans)
        if new_trans != str(trans):
            df.at[idx, "translation"] = new_trans
    return df
def narrow_relevant_glossary(df, glossary) -> dict:
    """把 filter_relevant_terms 的安全上界收斂成「真的會在某列命中」的術語集合。

    原本對每一列掃過整個上界詞庫、逐條呼叫 find_term_spans，成本是
    O(列數 × 上界術語數)（實測 0.67 µs/（列 × 術語），10 萬列級要 10 分鐘以上）。
    改用 TermLookup 的候選索引先取候選，成本降到
    O(列數 × (文本詞數 + 文本 3-gram 數) + 命中數)。

    為什麼收斂不會漏：英文原文在整個 enforce 期間不會改變，因此「哪些術語
    會命中某一列」是固定的；這裡只依英文原文判斷（不看譯文），所以尚未翻譯
    的列也一併納入，後續輪次才被翻譯的列不會失去它的術語。
    """
    if not glossary:
        return {}
    lookup = TermLookup(glossary)
    matched: set = set()
    # 用 .map(str) 而不是 astype(str)：pandas 3.0 的 astype(str) 對 NA 是 no-op（見全域限制 12）
    english = (df["english"].map(str).tolist()
               if "english" in df.columns else [""] * len(df))
    for eng in english:
        if not eng:
            continue
        eng_lower = eng.lower()
        ctx = build_relevance_context(eng_lower)
        for e, _chn, _spans in _find_matched_spans(
                eng_lower, lookup.candidates(eng_lower, ctx), ctx):
            matched.add(e)
    if not matched:
        return {}
    # 保留 glossary 的原始順序（下游 get_relevant_glossary 依賴相對順序穩定）
    return {e: c for e, c in glossary.items() if e in matched}

def scan_issues(df, relevant_glossary, lookup=None, keep=None) -> list[tuple]:
    """走訪所有行，彙整三種檢查的結果，回傳問題 pool。

    lookup：可選的 TermLookup；由呼叫端建立一次共用（一次 enforce 最多會呼叫
    這個函式 10 次，全詞庫重建索引要 0.125 秒，絕不可每輪重建）。

    keep：可選的「譯文＝原文才正確」術語集合（keep_english_terms 的結果）。
    不傳時由 relevant_glossary 現算一次；_enforce_async 會自己算好、三輪共用，
    因為本函式每輪至少被呼叫一次，重算等於白白多掃一次詞庫。

    這裡刻意保留 df.iterrows()：改動後的成本由「術語比對」變成「逐列迭代」，
    而 iterrows 在 10 萬列上約 1.9 秒、相對於省下的數分鐘可以接受；
    保留 (idx, row, issues) 的 tuple 契約可以讓下游三個消費者不必改動。
    """
    if keep is None:
        keep = keep_english_terms(relevant_glossary)
    if lookup is None:
        lookup = TermLookup(relevant_glossary)
    pool = []
    for idx, row in df.iterrows():
        eng = row.get("english")
        trans = row.get("translation")
        # 原文缺值（空白列）不檢查、也不進問題池：沒有原文就沒有可翻譯的內容，
        # 舊寫法會把它判成「translation 欄位為空」而送進重譯（提示詞出現
        # "english": "nan"），永遠修不好，還可能把真問題擠出「<3 條跳過重譯」門檻。
        if _is_na(eng):
            continue
        all_issues = []

        untrans_issues = check_untranslated(eng, trans, keep_original_terms=keep)
        if untrans_issues:
            for ui in untrans_issues:
                all_issues.append(("untranslated", "", ui))
            pool.append((idx, row, all_issues))
            continue

        eng_term = str(eng)
        trans_str = str(trans)
        eng_lower = eng_term.lower()
        ctx = build_relevance_context(eng_lower)
        # 只對候選做精確比對（候選保留 glossary 原順序，因此 _find_matched_spans
        # 的長度降序穩定排序結果與舊版逐條掃描相同）
        candidates = lookup.candidates(eng_lower, ctx)
        for g_eng, g_chn, g_desc in check_glossary_usage(eng_term, trans_str, candidates, ctx):
            all_issues.append(("glossary", f"{g_eng} → {g_chn}", g_desc))

        for pi in check_placeholder(eng_term, trans_str):
            all_issues.append(("placeholder", "", pi))

        for _t, _f, desc in check_space_issues(trans_str):
            all_issues.append(("space", "", desc))

        if all_issues:
            pool.append((idx, row, all_issues))

    return pool

def _filter_llm_pool(pool):
    """Keep only entries needing LLM retranslation; pure space issues are handled by scripts."""
    return [item for item in pool if any(itype != "space" for itype, _, _ in item[2])]


def _extract_retry_results(parsed, batch):
    """把重譯階段的模型回傳正規化成「屬於本批次的 dict 清單」。

    支援 dict 包裝（translations/translated/data/results）、巢狀結構與單一物件；
    超過 120% 回傳視為失敗（回傳 None）；空結果回傳 []（由呼叫端視為失敗）。
    """
    if isinstance(parsed, dict):
        found = None
        for key in ("translations", "translated", "data", "results"):
            cand = parsed.get(key)
            if isinstance(cand, list) and (not cand or isinstance(cand[0], dict)):
                found = cand
                break
        if found is None:
            for value in parsed.values():
                if isinstance(value, list) and value and isinstance(value[0], dict) \
                        and "index" in value[0] and "translation" in value[0]:
                    found = value
                    break
            if found is None:
                for value in parsed.values():
                    if isinstance(value, dict):
                        for v2 in value.values():
                            if isinstance(v2, list) and v2 and isinstance(v2[0], dict) \
                                    and "index" in v2[0] and "translation" in v2[0]:
                                found = v2
                                break
        if found is None:
            if "index" in parsed and "translation" in parsed:
                parsed = [parsed]
            else:
                return None
        else:
            parsed = found
    if not isinstance(parsed, list):
        return None
    if len(parsed) > int(len(batch) * OVER_RETURN_TOLERANCE):
        return None
    batch_indices = {item[0] for item in batch}
    mapped = {}
    for r in parsed:
        if isinstance(r, dict) and r.get("index") in batch_indices:
            mapped[r["index"]] = r
    return list(mapped.values())


async def _retry_round(client, model_name, pool, glossary, rnd, lookup=None):
    """非同步執行一輪重譯，返回修正後的條目數"""
    # 每批 BATCH_SIZE_LIMIT（100）條，無群組機制
    all_results = []
    for bs in range(0, len(pool), BATCH_SIZE_LIMIT):
        batch = pool[bs:bs + BATCH_SIZE_LIMIT]
        items = [{"index": idx, "english": str(row["english"])} for idx, row, _ in batch]
        glossary_text = build_retry_glossary_text(glossary, items, lookup)
        prompt = (RETRY_PROMPTS[rnd] + "\n" + json.dumps(items, ensure_ascii=False)
                  + f"\n## 强制术语表（禁止使用官方和社区通常的翻译，遇到以下英文词除非有语境冲突，否则尽量使用指定的中文翻译）\n{glossary_text}"
                  + "\n请回传 JSON 阵列，每条包含 index 和 translation。")
        content = ""
        for attempt in range(3):
            try:
                resp = await client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=RETRY_TEMPERATURE,
                )
                # 保持正則操作，不要改為字串，否則會大幅降低翻譯成功率
                content = resp.choices[0].message.content.strip()
                content = re.sub(r"^```(?:json)?\n?", "", content, flags=re.IGNORECASE)
                content = re.sub(r"\n```$", "", content)
                parsed = json.loads(content)
                # 有些供應商 HTTP 200 但 body 是限流錯誤物件 → 當成 429 往上拋
                if is_rate_limit_payload(parsed):
                    raise RateLimitPayloadError(
                        f"429 rate limit in response payload: {content[:200]}")
                results = _extract_retry_results(parsed, batch)
                if not results:
                    raise ValueError("回傳為空、結構無法識別或無匹配的 index，視為失敗重試")
                all_results.extend(results)
                # 保持正則操作，不要改為字串，否則會大幅降低翻譯成功率
                break
            except json.JSONDecodeError as e:
                # ↓ 嘗試用 json_repair 修復
                try:
                    from json_repair import repair_json
                    repaired = repair_json(content)
                    if repaired:
                        fixed = json.loads(repaired)
                        if is_rate_limit_payload(fixed):
                            raise RateLimitPayloadError(
                                f"429 rate limit in response payload: {str(fixed)[:200]}")
                        results = _extract_retry_results(fixed, batch)
                        if results:
                            all_results.extend(results)
                            print(f"    ⚠️ JSON 修復成功")
                            break
                except RateLimitPayloadError:
                    # 修復後的內容仍是限流錯誤包裝 → 不可被吞掉，要往上拋成 429
                    raise
                except Exception:
                    pass
                # ↑ 修復結束
                print(f"    JSON 解析失敗 (嘗試 {attempt + 1}/3): {e}")
                print(f"    原始回傳內容前 200 字: {content[:200]}")
                if attempt < 2:
                    await asyncio.sleep(2)
            except Exception as e:
                if is_rate_limit_error(e):
                    raise
                print(f"    API 錯誤 (嘗試 {attempt + 1}/3): {e}")
                if attempt < 2:
                    await asyncio.sleep(5)
        else:
            all_results.extend([{"index": idx, "translation": None, "_error": "API failed"}
                               for idx, _, _ in batch])
    batch_indices = {item[0] for item in pool}
    mapped = {}
    for r in all_results:
        if isinstance(r, dict) and r.get("index") in batch_indices:
            mapped[r["index"]] = r
    return list(mapped.values())

async def _enforce_async(df, relevant_glossary, output_dir=None, sheet_name=None, shared_pool=None,
                         checkpoint_root: str = CHECKPOINT_SUBDIR):
    """非同步執行重譯循環，使用多 API 共享隊列模式（支援續傳）

    checkpoint_root：checkpoint 子目錄名（`_checkpoint` 或 `_proofread_checkpoint`）。
    校對管線（P4a）與主翻譯共用同一張工作表時，兩者的 part／progress／
    enforce_checkpoint 必須各自獨立，否則互相覆蓋、續傳會還原到另一邊的資料。
    """
    from llm_translator import save_backup_part
    import uuid
    if shared_pool is None:
        print("  無可用 API，跳過重譯")
        return df

    # TL4 索引：本函式共用（scan_issues 與重譯提示詞都吃同一份）
    lookup = TermLookup(relevant_glossary)

    # ── 檢查 enforce checkpoint ──
    start_round = 0
    enforce_tag = uuid.uuid4().hex[:8]
    cp_file = None
    # 原文指紋：checkpoint 只記輪次時，別的工作表（甚至別的 Excel 檔）留下的
    # enforce_checkpoint.json 會被當成本次的進度，續傳直接跳過該重譯的輪次
    # （R1 懷疑點 6 實測：全新資料 5 列應重譯，只跑了第 3 輪）。
    # 因此讀檔要驗「工作表名 + 原文指紋」，任一不符就從第 1 輪重跑。
    english_sha1 = english_fingerprint_df(df)
    if output_dir and sheet_name:
        # 走 _checkpoint_dir：root 的合法值檢查（assert）只保留在那一處，
        # 不在這裡再拼一次路徑（拼錯時 save_backup_part 才炸，ecp 已經先寫錯目錄）
        cp_file = _checkpoint_dir(output_dir, sheet_name, checkpoint_root) / "enforce_checkpoint.json"
        if cp_file.exists():
            try:
                cp_data = json.loads(cp_file.read_text(encoding="utf-8"))
                if cp_data.get("sheet") != sheet_name or cp_data.get("english_sha1") != english_sha1:
                    print("  ⚠️ 重譯進度記錄的工作表或原文指紋不符，忽略記錄並從第 1 輪重新開始")
                else:
                    start_round = cp_data.get("completed_rounds", 0)
                    # 從 checkpoint 載入之前生成的 enforce_tag，若無則用新的
                    enforce_tag = cp_data.get("enforce_tag", enforce_tag)
                    print(f"  偵測到重譯進度，從第 {start_round + 1} 輪開始")
            except Exception:
                pass

    # 「譯文＝原文才正確」的術語集合：三輪共用同一份，放在迴圈外只算一次
    keep = keep_english_terms(relevant_glossary)

    # 原文缺值列：scan_issues 直接跳過（沒有原文就沒有「譯得對不對」可言），
    # 但跳過是靜默的，使用者會看到「問題池比往年少」卻不知道原因，連帶影響
    # 「少於 3 條就跳過重譯」的門檻（G2）。只印一次，不放在 scan_issues 裡——
    # 那個函式每輪（含預處理後）被呼叫多次，會洗版。
    if "english" in df.columns:
        skipped_na = int(sum(1 for v in df["english"].tolist() if _is_na(v)))
        if skipped_na:
            print(f"  跳過 {skipped_na} 列原文缺值（不列入問題池）")

    for rnd in range(start_round, 3):
        pool = scan_issues(df, relevant_glossary, lookup, keep=keep)


        # ★ 腳本預處理（每輪重譯前都執行）
        if pool:
            # 統計預處理前的問題類型
            glossary_cnt = sum(1 for _, _, issues in pool for t, _, _ in issues if t == "glossary")
            ph_cnt = sum(1 for _, _, issues in pool for t, _, _ in issues if t == "placeholder")
            untranslated_cnt = sum(1 for _, _, issues in pool for t, _, _ in issues if t == "untranslated")
            space_cnt = sum(1 for _, _, issues in pool for t, _, _ in issues if t == "space")
            before_parts = []
            if glossary_cnt > 0:
                before_parts.append(f"術語 {glossary_cnt} 條")
            if ph_cnt > 0:
                before_parts.append(f"佔位符 {ph_cnt} 條")
            if untranslated_cnt > 0:
                before_parts.append(f"未翻譯 {untranslated_cnt} 條")
            if space_cnt > 0:
                before_parts.append(f"空格 {space_cnt} 條")
            if before_parts:
                print(f"  ⚡ 腳本預處理前問題：{'、'.join(before_parts)}")

            preprocess_issues(df, pool)
            pool = scan_issues(df, relevant_glossary, lookup, keep=keep)

            # 統計修正了哪些類型
            space_after = sum(1 for _, _, issues in pool for t, _, _ in issues if t == "space")
            ph_after = sum(1 for _, _, issues in pool for t, _, _ in issues if t == "placeholder")
            solved_parts = []
            solved_space = space_cnt - space_after
            solved_ph = ph_cnt - ph_after
            if solved_space > 0:
                solved_parts.append(f"空格 {solved_space} 條")
            if solved_ph > 0:
                solved_parts.append(f"佔位符 {solved_ph} 條")
            if solved_parts:
                print(f"  ⚡ 腳本預處理修正：{'、'.join(solved_parts)}")
        if not pool:
            print(f"  第 {rnd + 1} 轮检查：全部正确")
            break
        llm_pool = _filter_llm_pool(pool)
        if not llm_pool:
            print(f"  第 {rnd + 1} 轮检查：剩餘問題皆為空格問題，由腳本處理，跳過重譯")
            break
        if len(llm_pool) < 3:
            print(f"  第 {rnd + 1} 轮检查：{len(llm_pool)} 条有问题（少于3条，跳过重译）")
            break
        pool = llm_pool

        # ── 掃描問題條目 ──
        gloss_count = sum(1 for _, _, issues in pool for t, _, _ in issues if t == "glossary")
        ph_count = sum(1 for _, _, issues in pool for t, _, _ in issues if t == "placeholder")
        untrans_count = sum(1 for _, _, issues in pool for t, _, _ in issues if t == "untranslated")
        space_count = sum(1 for _, _, issues in pool for t, _, _ in issues if t == "space")
        print(f"  ── 掃描問題條目 ──")
        print(f"    術語檢查：{gloss_count} 條")
        if ph_count > 0:
            print(f"    佔位符檢查：{ph_count} 條")
        if untrans_count > 0:
            print(f"    未翻譯檢查：{untrans_count} 條")
        if space_count > 0:
            print(f"    空格檢查：{space_count} 條")
        print(f"  第 {rnd + 1} 轮检查：{len(pool)} 条有问题，进行重譯...")

        pre_count = len(pool)

        # 分割問題條目為 BATCH_SIZE_LIMIT 大小的批次，丟進共享池
        batches = [pool[bs:bs + BATCH_SIZE_LIMIT] for bs in range(0, len(pool), BATCH_SIZE_LIMIT)]
        total_batches = len(batches)

        # 守門拒絕數：獨立於 skipped（R1 1-A10：計入 skipped 會印成「回傳無效
        # （非物件、index 不在工作表中，或譯文非字串）」，把「內容不合格」
        # 說成「回傳格式錯誤」，完全誤導排查）。逐分類累計是為了輪末那行統計。
        guard_rejected = 0
        guard_labels = {"untranslated": 0, "placeholder": 0}

        async def _retry_process(ws, job):
            nonlocal guard_rejected
            cfg = ws["cfg"]
            batch_slice = job.batch
            try:
                results = await _retry_round(ws["client"], cfg.model, batch_slice,
                                             relevant_glossary, rnd, lookup=lookup)
                batch_indices = {item[0] for item in batch_slice}
                fixed = 0
                skipped = 0
                for res in results:
                    if not isinstance(res, dict):
                        skipped += 1
                        continue
                    idx = res.get("index")
                    trans = res.get("translation")
                    # 只有非空字串可以寫進 translation 欄。實測 dict/list 會讓 pandas
                    # 拋「Incompatible indexer with Series」，被批次層 except 吞掉後
                    # 整個批次（含合法條目）一起失敗、完全沒有修正；空字串則會把
                    # 既有譯文清成空白。缺值哨兵（"nan"/"nat"/"none"）同樣不是譯文，
                    # 寫進去會與三條還原守門不一致。這裡逐筆擋掉，其餘合法條目照常寫入。
                    if (idx is None or not isinstance(trans, str) or not trans.strip()
                            or is_missing_translation(trans, english=_english_hint_at(df, idx))):
                        skipped += 1
                        continue
                    try:
                        if idx in batch_indices:
                            previous = df.at[idx, "translation"]
                            # df 可能是子集：沒有 english 欄時給 None，_is_na(None)
                            # 會讓 regressed 直接放行（沒有原文可比，就不該擋）
                            eng_raw = df.at[idx, "english"] if "english" in df.columns else None
                            reason = regressed(eng_raw, previous, trans, keep)
                            if reason:
                                # 候選比原本更差（新增未翻譯／佔位符遺失）→ 不覆寫，
                                # 保留原值。既不計 skipped（不是格式錯），也不計 fixed
                                guard_rejected += 1
                                for _label in ("untranslated", "placeholder"):
                                    if _label in reason:
                                        guard_labels[_label] += 1
                                continue
                            df.at[idx, "translation"] = trans
                            # 只有真的改變才計入「已修正」：模型原樣回傳舊值
                            # （常見的 no-op 候選）會寫入同一個字串，卻被印成
                            # 「N 条已修正」，讓修正率看起來比實際好（G3）。
                            # previous 可能是 pd.NA/NaN，不能寫 trans != previous
                            # （boolean value of NA is ambiguous），先判型別。
                            if not (isinstance(previous, str) and previous == trans):
                                fixed += 1
                        else:
                            skipped += 1
                    except (TypeError, ValueError, KeyError, IndexError):
                        skipped += 1
                if skipped:
                    print(f"  [{cfg.api_id}] 警告：{skipped} 條回傳無效（非物件、index 不在工作表中，或譯文非字串），已忽略")
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(f"  [{now}] [重譯] 批次 {job.batch_num}/{total_batches} 完成 ({fixed} 条已修正) [{cfg.api_id} {cfg.model}]")
            except Exception as e:
                if is_rate_limit_error(e):
                    shared_pool.note_429(cfg)
                    if cfg.is_permanently_disabled:
                        print(f"  [{cfg.api_id} {cfg.model}] 第 {cfg.strike} 次 429，永久停用")
                    else:
                        print(f"  [{cfg.api_id} {cfg.model}] 第 {cfg.strike} 次 429，冷卻 60 秒")
                    shared_pool.retry(job)
                else:
                    print(f"  [{cfg.api_id} {cfg.model}] 錯誤：{e}")

        if batches:
            await asyncio.gather(*[shared_pool.submit(bn, b, _retry_process) for bn, b in enumerate(batches, 1)])

        # 每輪一行守門統計：被擋下的候選（保留舊值）必須看得到，
        # 否則使用者只會看到「修正率很低」而不知道是守門在擋
        print(f"  本輪守門拒絕 {guard_rejected} 條（未翻譯 {guard_labels['untranslated']}、"
              f"佔位符 {guard_labels['placeholder']}）")

        # 計算修正率，決定是否繼續
        post_pool = scan_issues(df, relevant_glossary, lookup, keep=keep)

        # ★ 儲存本輪修正到 part 檔案（原子寫入） ★
        if output_dir:
            backup_data = {}
            for idx, row, _ in pool:
                trans = df.at[idx, "translation"]
                # NaN 不是 None：只判 is not None 會把空譯文存成字串 "nan"，
                # 續傳時再寫回成品。
                # english= 必須用 _english_hint：直接 str() 會把缺值的英文欄
                # 變成字串 "nan"／"None"，讓字面譯文 "nan" 被誤判成合法譯文
                if not is_missing_translation(trans, english=_english_hint(row.get("english"))):
                    backup_data[str(idx)] = str(trans)
            if backup_data:
                # root=checkpoint_root：校對管線的 part 檔必須寫在 _proofread_checkpoint，
                # 不可與主翻譯的 _checkpoint 共用（R2 風險 2-1）
                save_backup_part(output_dir, enforce_tag, rnd + 1, backup_data, sheet_name,
                                 root=checkpoint_root)

        # ★ 儲存 enforce checkpoint ★
        if cp_file:
            cp_file.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(cp_file, json.dumps({
                "completed_rounds": rnd + 1,
                "enforce_tag": enforce_tag,
                # 來源識別：跨工作表／跨 Excel 檔殘留的 checkpoint 不可被沿用
                # （讀取端靠這兩個欄位驗證，見上方「檢查 enforce checkpoint」）
                "sheet": sheet_name,
                "english_sha1": english_sha1,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }, ensure_ascii=False, indent=2))

        post_all_count = len(post_pool)
        post_count = len(_filter_llm_pool(post_pool))  # 同口徑：與送重譯前一致（不含純空格）
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if post_all_count == 0:
            print(f"  [{now}] 第 {rnd + 1} 轮重译后：全部正确")
            break
        if post_count == 0:
            print(f"  [{now}] 第 {rnd + 1} 轮重译后：剩餘問題皆為空格問題，由腳本處理")
            break

        corrected = pre_count - post_count
        rate = corrected / pre_count if pre_count > 0 else 0
        print(f"  [{now}] 第 {rnd + 1} 轮重译后：剩余 {post_count} 条，修正率 {rate * 100:.1f}%")
        if rate < 0.05:
            print(f"  [{now}] 修正率低於 5%，跳過後續輪次")
            break

    # ── 所有輪次完成，刪除 enforce checkpoint ──
    if cp_file and cp_file.exists():
        cp_file.unlink()
        print("  重譯完成，已清除進度記錄")

    return df

async def enforce_async(df: pd.DataFrame, glossary: dict, output_dir: str | Path | None = None,
                        report_name: str = "review_report.xlsx", write_report: bool = True,
                        sheet_name: str | None = None, shared_pool=None,
                        checkpoint_root: str = CHECKPOINT_SUBDIR) -> tuple:
    df = df.copy()
    review_rows = []

    # 只保留對當前批次有相關性的術語
    # 用安全上界過濾（詞集合／子字串），不做逐術語全文掃描：
    # 原本對整張工作表的 all_text（實際最大 7 MB）逐條跑 find_term_spans，
    # 每條都要掃全文，整張表是小時級成本（見 filter_relevant_terms 的說明）。
    all_text = _join_all_english(df)
    _ctx = build_relevance_context(all_text)
    relevant_glossary = filter_relevant_terms(glossary, all_text, _ctx)
    # 上界只是「不會漏」；用一次逐列掃描收斂成真正命中的集合，
    # 讓後續每一次 scan_issues 都回到精確成本
    relevant_glossary = narrow_relevant_glossary(df, relevant_glossary)

    # 非同步重譯（使用共享池；未提供時建立臨時池）
    if shared_pool is not None:
        df = await _enforce_async(df, relevant_glossary, output_dir, sheet_name, shared_pool,
                                  checkpoint_root=checkpoint_root)
    else:
        tmp_pool = SharedBatchPool()
        try:
            await tmp_pool.start()
        except RuntimeError:
            tmp_pool = None
        if tmp_pool is not None:
            try:
                df = await _enforce_async(df, relevant_glossary, output_dir, sheet_name, tmp_pool,
                                          checkpoint_root=checkpoint_root)
            finally:
                await tmp_pool.close()

    # 最終審查掃描（與重譯輪同一套 keep：譯文＝原文才正確的術語不判未翻譯）
    keep = keep_english_terms(relevant_glossary)
    lookup = TermLookup(relevant_glossary)
    final_pool = scan_issues(df, relevant_glossary, lookup, keep=keep)
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
    if write_report and not review_df.empty:
        if not output_dir:
            print("  警告：未提供 output_dir，跳過審查報告寫入")
            return df, review_df
        fp = Path(output_dir) / report_name

        from openpyxl.styles import Color, PatternFill

        fill_glossary = PatternFill(start_color=Color(rgb="D9D9D9"), end_color=Color(rgb="D9D9D9"), fill_type="solid")
        fill_placeholder = PatternFill(start_color=Color(rgb="BDD7EE"), end_color=Color(rgb="BDD7EE"), fill_type="solid")
        fill_untranslated = PatternFill(start_color=Color(rgb="F4CCCC"), end_color=Color(rgb="F4CCCC"), fill_type="solid")
        fill_space = PatternFill(start_color=Color(rgb="E2EFDA"), end_color=Color(rgb="E2EFDA"), fill_type="solid")

        with pd.ExcelWriter(fp, engine='openpyxl') as writer:
            review_df.to_excel(writer, index=False, sheet_name='Sheet1')
            worksheet = writer.sheets['Sheet1']

            # 找到 issue 欄位的欄號
            issue_col = None
            for col_idx, cell in enumerate(worksheet[1], start=1):
                if cell.value == "issue":
                    issue_col = col_idx
                    break

            if issue_col:
                for row in worksheet.iter_rows(min_row=2, max_col=worksheet.max_column, max_row=worksheet.max_row):
                    issue_cell = row[issue_col - 1]
                    issue_val = str(issue_cell.value) if issue_cell.value else ""
                    if issue_val.startswith("[glossary]"):
                        fill = fill_glossary
                    elif issue_val.startswith("[placeholder]"):
                        fill = fill_placeholder
                    elif issue_val.startswith("[untranslated]"):
                        fill = fill_untranslated
                    elif issue_val.startswith("[space]"):
                        fill = fill_space
                    else:
                        continue
                    for cell in row:
                        cell.fill = fill

        print(f"  审查报告已写入: {fp} ({len(review_df)} 条需审查)")
    return df, review_df


