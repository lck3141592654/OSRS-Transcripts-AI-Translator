import re
import itertools
import pandas as pd
from glossary import normalize_term, MISSING_SENTINELS
from llm_translator import _is_na

TEMPLATES: list[dict] = [
    # ═══════════════════════════════════════════
    #  模板使用說明
    #  {0} {1} {2}... = pattern 中第 1、2、3... 個 (.+?) 的翻譯
    #  可任意排列順序來調整語序，相容 {0} 也相容 {}
    #  {} 會依出現順序對應 {0} {1}...（第一個 {} 是 {0}、第二個是 {1}）
    #  注意：所有捕獲的參數都必須在術語庫中，
    #        模板才會套用，否則交給 LLM。
    # ═══════════════════════════════════════════

    # ─── 單變量（語序一致，{0} 或 {} 皆可） ───
    # {"pattern": r"I can carry (.+) for you\.",     "template": "我可以为你携带{0}。"},
    # {"pattern": r"Welcome to (.+)\.",              "template": "欢迎来到{0}。"},
    # {"pattern": r"Your task is to kill (.+)\.",    "template": "你的任务是杀死{0}。"},
    # {"pattern": r"Talk to (.+)\.",                 "template": "与{0}交谈。"},
    # {"pattern": r"Search (.+)\.",                  "template": "搜索{0}。"},
    # {"pattern": r"You don't have to worry, I'm not a priest. I won't try to convert you to (.+)\.",
    #  "template": "您不需要担1心，我2不是祭司。我3不会试图4让你改信{0}。"},

    # ─── 多變量（用 {0}{1} 調整語序） ───
    # 原文：kill X in Y → {0}=X(target), {1}=Y(location)
    # {"pattern": r"Your task is to kill (.+?) in (.+?)\.", "template": "你的任务是去{1}杀死{0}。"},
    # 原文：Give X to Y → {0}=X(item), {1}=Y(recipient)
    # {"pattern": r"Give (.+?) to (.+?)\.",               "template": "把{0}交给{1}。"},
    # 原文：Travel from X to Y → {0}=X(from), {1}=Y(to)
    # {"pattern": r"Travel from (.+?) to (.+?)\.",        "template": "从{0}旅行到{1}。"},
    # 原文：Use X on Y → {0}=X(item), {1}=Y(target)
    # {"pattern": r"Use (.+?) on (.+?)\.",                "template": "在{1}上使用{0}。"},
]

def _fill_template(template: str, params: list):
    """把模板的佔位符填上參數；無法完整填滿時回傳 None（呼叫端改走 LLM）。

    - 位置式 ``{0} {1} …``：可任意排列語序（既有模板格式，必須保持相容）；
    - 空式 ``{}``：依出現順序對應 ``{0} {1} …``（第一個 {} → {0}、第二個 → {1}）。

    舊版用 ``template.replace("{}", "{0}")`` 會把模板中**所有** ``{}`` 都換成 ``{0}``，
    多變量模板的每個佔位符因此全部指向同一個參數
    （``"把{}交给{}。"`` → ``"把{0}交给{0}。"`` → 兩個位置都是第一個參數）。
    這裡改為先掃描一遍為每個 ``{}`` 配上遞增索引，再用單次 regex 一次填完；
    逐 index 的 ``str.replace`` 已移除，避免填入的參數內容再被後續 replace 改寫。

    若填完後仍有未填的 ``{N}``（模板的佔位符比捕獲到的參數多，或寫了越界索引），
    表示這個模板根本填不完整；此時回傳 None，讓呼叫端不要套用模板、改交給 LLM，
    避免把字面 ``{2}`` 之類的殘留寫進譯文（TEMPLATES 功能本身不變，使用者仍可自行填入）。
    """
    counter = itertools.count()

    def _index(_m):
        return "{" + str(next(counter)) + "}"

    translated = re.sub(r"\{\}", _index, template)

    def _sub(m):
        i = int(m.group(1))
        return params[i] if 0 <= i < len(params) else m.group(0)

    filled = re.sub(r"\{(\d+)\}", _sub, translated)
    if re.search(r"\{\d+\}", filled):
        return None
    return filled


def match_and_fill(df: pd.DataFrame, glossary: dict) -> pd.DataFrame:
    df = df.copy()
    df["_status"] = "\u5f85 LLM"
    df["translation"] = df.get("translation", pd.Series([None] * len(df)))
    for idx, row in df.iterrows():
        # 原文是缺值（NaN／None／pd.NA）或哨兵字串時沒有模板可套用。
        # 用 _is_na 判原始值：pd.NA 經 str() 會變 "<NA>"（不在哨兵集合內）而漏判
        raw_english = row["english"] if "english" in df.columns else None
        if _is_na(raw_english):
            continue
        english = str(raw_english)
        if not english.strip() or english.strip().lower() in MISSING_SENTINELS:
            continue
        for tmpl in TEMPLATES:
            m = re.match(tmpl["pattern"], english, re.IGNORECASE)
            if m:
                params = [g for g in m.groups() if g]
                all_in_glossary = True
                translated_params = []
                for p in params:
                    norm_p = normalize_term(p)
                    if norm_p in glossary:
                        translated_params.append(glossary[norm_p])
                    elif p in glossary:
                        translated_params.append(glossary[p])
                    else:
                        all_in_glossary = False
                        break
                if all_in_glossary:
                    translated = _fill_template(tmpl["template"], translated_params)
                    if translated is None:
                        # 模板填不完整（佔位符比參數多或索引越界）：不套用，留給 LLM
                        print(f"    警告：模板「{tmpl['template']}」的佔位符無法全部填入，"
                              f"已略過（該列交給 LLM）")
                        break
                    df.at[idx, "translation"] = translated
                    df.at[idx, "_status"] = "\u5df2\u8655\u7406"
                    break
    return df
