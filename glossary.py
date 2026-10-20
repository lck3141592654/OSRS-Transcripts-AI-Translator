import re
import pandas as pd
from pathlib import Path

# 「缺值」哨兵字串：Excel 讀成字串時，NaN／NaT／None 會被寫成這幾個字面字串。
# 它們不是合法譯文，一律視為缺值（原文本身就是這個字時除外，該豁免由
# llm_translator.is_missing_translation 的 english= 提示處理）。
# 單一來源：llm_translator 由這裡匯入，不可在別處另抄一份清單；
# 反方向（glossary 匯入 llm_translator）會造成循環匯入，禁止。
MISSING_SENTINELS = ("nan", "nat", "none")

# 供 find_term_spans 使用的通用 regex 快取
_re_exact_cache: dict = {}
_re_plural_cache: dict = {}
_re_plain_cache: dict = {}
_re_word_cache = re.compile(r"[a-z']+")
# 佔位符 [...]；術語比對要跳過這裡面的英文（見 placeholder_spans）
_re_placeholder = re.compile(r"\[.*?\]")
# 所有格字元：'s / ’s / 複數所有格 s'
_APOS_CHARS = "'\u2019"
# 詞界判定用的「單字字元」集合（英數字與撇號）；供 _find_multi_word_span 的詞界檢查使用
_WORD_CHAR_SET = frozenset("abcdefghijklmnopqrstuvwxyz0123456789'\u2019")

# 不規則複數：單數 → 複數（_plural_forms 的規則無法推導者）
_IRREGULAR_PLURALS: dict[str, str] = {
    "man": "men", "woman": "women", "mouse": "mice", "goose": "geese",
    "tooth": "teeth", "foot": "feet", "child": "children", "person": "people",
    "ox": "oxen", "knife": "knives", "leaf": "leaves", "life": "lives",
    "wife": "wives", "wolf": "wolves", "thief": "thieves", "shelf": "shelves",
    "elf": "elves", "loaf": "loaves", "half": "halves", "calf": "calves",
    "self": "selves", "scarf": "scarves", "dwarf": "dwarves",
}
# 單複數同形：不加任何字尾
_UNCHANGED_PLURALS = frozenset({"fish", "sheep", "deer", "series", "species"})
# 複數 → 單數反查，供 _plural_bases() 產生候選基底
_IRREGULAR_SINGULARS: dict[str, str] = {v: k for k, v in _IRREGULAR_PLURALS.items()}
# _plural_forms 的結果快取（單字比對路徑會對同一批詞重複查詢）
_plural_forms_cache: dict[str, set] = {}

ADD_LIST: dict[str, str] = {
    "Old School RuneScape": "Old School RuneScape",
    "OSRS": "OSRS",
    "RuneLite": "RuneLite",
    "RuneLingual": "RuneLingual",
    "Lord Hosidius": "霍西迪乌斯领主", "Hosidius family": "霍西迪乌斯家族", "Anna Sinclair": "安娜.辛克莱尔",
    "fairy tales": "童话故事", "Shayzien Army": "谢兹恩城卫军", "Phoenix Gang": "凤凰帮",
    "Kourend Royal Guard": "库兰德皇家卫队", "Old School Leagues": "Old School 联赛", "Great Conch": "巨螺岛",
    "Lord Shayzien": "谢兹恩领主", "Deadman mode": "死亡模式", "Piscatoris Fishing Colony": "皮斯卡托里斯渔村",
    "Lord Arceuus": "阿西乌斯领主", "Historical Archive": "历史档案室", "White Wolf Mountain": "白狼山",
    "Kourend Council": "库兰德议会", "Runecrafting altar": "符石祭坛", "Runecraft altar": "符石祭坛",
    "River Molch": "莫尔奇河", "Proselyte Temple Knight": "皈依者圣殿骑士", "Brimhaven Agility Arena": "布尔哈文敏捷竞技场",
    "Arceuus Library": "阿西乌斯图书馆", "Saradomin": "萨拉多明", "Guthix": "古斯", "Zamorak": "扎莫拉克",
    "Armadyl": "阿玛杜尔", "Bandos": "班多斯", "Zaros": "扎罗斯", "Tumeken": "图梅肯",
    "Elidinis": "伊莉迪尼斯", "Jas": "贾斯", "Marimbo": "马里姆博", "Ralos": "拉洛斯",
    "Ranul": "拉努尔", "Kayzertief": "凯泽提夫", "Xeric": "泽里克", "Xerician": "泽里克西亚",
    "Iknami": "朋友", "Kuani": "太好了", "Nilsal": "你好", "Timoiva": "再见",
    "unranked Tournament": "非排位赛", "Old One": "古老者", "bank pin": "银行pin码", "Lady Lumbridge": "仑桥夫人号",
}

# ⚠️ 只放「普通名詞」：清單內的詞會在 load_glossary 被永久過濾，永遠不會進入
# 提示詞或術語檢查。專有名詞（例如 Fremennik）放進來等於讓該術語完全失效。
IGNORE_LIST: set[str] = {"Toolkit", "Vial", "Bones", "Burnt bones", "Cup of tea", "Message", "Book"
                         , "Translation book", "Twigs", "Knife", "Plank", "Rock", "Chisel"
                         , "Barrel", "Cake", "Watch", "Chart", "Journal"
                         , "Man", "Branch", "Manual", "Stool"
                         , "Monkey", "Torso", "Arms", "Legs", "Letter"
                         , "Corpse of woman", "Mud", "Pole", "Rake", "Chores"
                         , "Sand", "Sandy hand", "Roll", "Item", "Dummy"
                         , "Hoop", "Pond", "Tree", "Plant", "Small fern"
                         , "Fern", "Bush", "The desert", "Rug", "Saw"
                         , "Footprint", "A pattern", "A container", "No eggs", "Hair"
                         , "Documents", "Run", "Artefact", "Spear", "Carpet"
                         , "Fungi", "Me", "Stick", "Ash", "? ? ? ?"
                         , "Red", "Will", "Symbol", "Zero", "One"
                         , "Two", "Three", "Four", "Five", "Six"
                         , "Seven", "Eight", "Nine", "Dead Body", "Death"
                         , "Pack", "Woman", "Eye", "Corpses", "Remnant"
                         , "Beam", "Shadow", "Plants", "REPLACE ME", "Rain"
                         , "Head", "Body", "Warp", "Gateway", "Bags"
                         , "Void", "Spike", "Pillow", "A Goblin", "Droppings"
                         , "Extremity", "Surface", "Breach", "Display dial", "Debug Man"
                         , "Debug Woman", "Jug", "Art", "Smith", "Gem"
                         , "Well", "pit", "Other", "Sign", "Notice"
                         , "Wave", "Stand", "Note", "Tap", "Current"
                         , "Start", "Egg", "Mechanism", "Poison", "Fire"
                         , "Display", "Don't Know What", "Black", "Nothing", "Cleaner"
                         , "Charge", "Support", "River", "Handle", "Scout"
                         , "Port", "Target", "Cross", "Font", "Arrows"
                         , "Flowers", "Sails", "Leaflets", "Crack", "Stop!"
                         , "Leaves", "Ed", "Safe", "Mark", "Gene"
                         , "Chest", "History", "Crystal", "Dragon", "Passage"
                         , "Me?", "Steps", "Child", "Throne", "Bone"
                         , "Grip", "Mess", "Locked", "Hunter", "Harness"
                         , "Suspect", "My life", "House", "Stake", "level"
                         , "Prisoner", "Light", "Tracks", "Present", "Eyes"
                         , "Nest", "Bars", "Key", "Shell", "Old man"
                         , "Information", "Sacrifice", "Picture", "Reach", "Giant"
                         , "Edge", "Dad", "White", "Vial of water", "Feather"
                         , "Sieve", "Gossip", "Lift", "Till", "Mirror"
                         , "Dust", "Hanging", "Door", "Trees", "Experiment"
                         , "Dream", "Our lives", "Paper", "Boy", "girl"
                         , "Piles", "List", "Pages", "Study", "Opening"
                         , "Gas", "Gold", "Path", "Core", "Shot"
                         , "Fingers", "Gate", "Boot", "Stocks", "Cook"
                         , "Tomb", "Drew", "Entrance", "Fox", "Space"
                         , "Feud, The", "Golem, The", "A", "B", "C"
                         , "D", "E", "F", "H", "G"
                         , "I", "J", "K", "L", "M"
                         , "N", "O", "P", "Q", "R"
                         , "S", "T", "U", "V", "W"
                         , "X", "Y", "Z", "In Progress", "Not Started"
                         , "Completed", "Intermediate", "Experienced", "Master", "Very Short"
                         , "Short", "Medium", "Long", "Very Long", "Camelot"
                         , "Monkeys", "Rats", "Spiders", "Birds", "Cows"
                         , "Scorpions", "Bats", "Search", "Tail", "Chuck"
                         , "Ship", "Hole", "Plunder", "Posts", "Supplies"
                         , "Entry", "Shorts", "Mercy", "Birds", "Staff"
                         , "Directions", "Circle", "Stone", "Trap", "Carving"
                         , "Floor", "Dishes", "Orange", "Table", "Grave"
                         , "Nick", "A corpse", "Spirit", "Flames", "Shark"
                         , "Cages", "Wood", "Frame", "Heat", "Pots"
                         , "Green", "Wool", "Hammer", "Standard", "Box"
                         , "Bricks", "not", "be", "The stuff", "Casual"
                         , "brain", "Boards", "Mime", "a wall", "switch"
                         , "pin", "web", "file", "remains", "bottle"
                         , "special", "markings", "file", "remains", "bottle"
                         , 'A crack', 'A rock', 'Container', 'Corpse', 'Desert', 'Goblin', 'Goblins', 'Rocks', 'The rocks', 'Wall'
                         , "don", "markings", "file", "remains", "bottle"}

def _strip_plural_suffix(m: "re.Match") -> str:
    """-es 分支保留字根尾字元（buses → bus），-s 分支剝掉字尾（cats → cat）。"""
    return m.group(1) or ""


def normalize_term(term: str) -> str:
    """將術語歸一化，使 IGNORE_LIST 能同時匹配單數和複數型。

    只有真正的複數 -es 才會被剝離：前一個字元是 s/x/z/ch/sh/o
    （boxes → box、buses → bus、churches → church、wishes → wish、heroes → hero）。
    先前的 `(?<=[a-z])es$` 會把所有以 es 結尾的字都砍掉 es，於是
    ①e 屬於字根的字被砍（notes → not、chores → chor、series → sery），
    ②緊接著的 -s 規則又把 s 結尾字根的最後一個 s 一起砍掉
    （buses → bu、classes → clas、glasses → glas），單複數無法歸一成同一個鍵。

    -o 也列入 -es 家族：heroes → hero、potatoes → potato（舊版靠 `es$` 得到相同
    結果）。少了 o 會讓 Heroes 變成 heroe 而與 Hero 分家（-o 結尾單數的複數是 +es）。

    已知限制：-ses 結尾的英文本身有歧義（buses 的字根是 bus、houses 的字根是
    house / case / rose），純規則無法兩者兼顧；此處一律把 -ses 當成複數 -es，
    故 Houses → hous（與 House → house 不同鍵）、Corpses → corps（與 Corpse 不同鍵），
    實測真實術語庫不受影響。
    """
    t = term.lower().strip()
    # -ves → f (wolves → wolf)
    t = re.sub(r"(?i)(?<=[a-z])ves$", "f", t)
    # -ies → y (ponies → pony)
    t = re.sub(r"(?i)(?<=[a-z])ies$", "y", t)
    # -es 與 -s 用同一次 re.sub、兩個互斥分支：
    #   前一個字元是 s/x/z/ch/sh/o 的 -es 才是複數結尾（churches → church、wishes → wish、
    #   boxes → box、buses → bus、heroes → hero），命中時保留該字根字元且不再套 -s 分支。
    #   分成兩次 re.sub 會讓後面的 -s 規則把 -es 分支剛還原的字根 s 再砍一次
    #   （實測 buses → bus → bu、houses → hous → hou、cases → ca、glasses → glas）。
    t = re.sub(r"(?i)(s|x|z|ch|sh|o)es$|(?<=[a-z])s$", _strip_plural_suffix, t)
    return t.strip()


def build_relevance_context(text_l: str) -> tuple[set, dict]:
    """預先建立文本的詞集合與「基底 → 其複數形」對照，供大量術語批次匹配共用。

    plural_map 的鍵是 _plural_bases() 產生、且經 _plural_forms() 反驗過的基底，
    值是文本中確實出現的詞，因此它恰為 _plural_forms 的反關係（不會有兩套規則）。
    """
    tokens = set(_re_word_cache.findall(text_l))
    # 斷詞器把撇號包進同一個 token（saradomin's、demons'）。若不拆出詞首，
    # 所有格形式會讓詞集合預篩直接淘汰該術語，術語強制對所有格文本完全失效。
    words = set(tokens)
    for w in tokens:
        if "'" in w or "\u2019" in w:
            head = re.split(r"['\u2019]", w, maxsplit=1)[0]
            if head:
                words.add(head)
    plural_map: dict[str, set] = {}
    for w in words:
        for base in _plural_bases(w):
            plural_map.setdefault(base, set()).add(w)
    return words, plural_map


def _plural_forms(word: str) -> set[str]:
    """以規則產生 word 的複數形（含 word 自身）。

    這是本模組唯一的複數規則：單字術語的複數回退（find_term_spans）與多字詞
    術語最後一詞的比對（_find_multi_word_span）都由此推導；文本側的 plural_map
    則由 _plural_bases() 以本函式反驗後建立，因此三處不可能各有一套規則。

    涵蓋：一般 +s；-s/-x/-z/-ch/-sh → +es；子音 +y → -ies；-f/-fe → -ves；
    -o → +es；常見不規則（man→men、wolf→wolves…）；單複數同形（fish/sheep…）。
    -f/-fe 與 -o 同時保留 +s（roofs、pianos）：舊版反查會產生這兩種基底，
    只留 -ves/-oes 會讓「Roof 對 roofs」這類既有命中消失。

    注意：新增規則時必須同步 _plural_bases() 的字尾猜想，否則前置過濾會先擋掉術語。
    """
    cached = _plural_forms_cache.get(word)
    if cached is not None:
        return set(cached)
    w = word.lower()
    forms = {w}
    irregular = _IRREGULAR_PLURALS.get(w)
    if irregular is not None:
        forms.add(irregular)                         # man → men、wolf → wolves
    elif w in _UNCHANGED_PLURALS:
        # 單複數同形：複數就是自己（fish/sheep/deer/series/species）。
        # 另外補上規則形（fishes/deers）：語料裡確實會出現，實測 examine:133
        # 「the poor fishes」原本命中 Fish，只留同形會讓這筆既有命中消失。
        # series/species 本身以 s 結尾，規則形是 serieses/specieses 這種不存在的字，故不產生。
        if not w.endswith("s"):
            forms.add(w + "es" if re.search(r"(?:s|x|z|ch|sh)$", w) else w + "s")
    elif re.search(r"(?:s|x|z|ch|sh)$", w):
        forms.add(w + "es")                      # box → boxes、bus → buses、church → churches
    elif re.search(r"[^aeiou]y$", w):
        forms.add(w[:-1] + "ies")                # pony → ponies
    elif w.endswith("fe") and len(w) > 2:
        forms.update((w[:-2] + "ves", w + "s"))  # knife → knives
    elif w.endswith("f") and not w.endswith("ff") and len(w) > 2:
        # len>2 排除單字母 f 與 of/if：f → ves 這種無意義形會讓前置過濾
        # 與複數回退對文字 "ves" 給出不同答案
        forms.update((w[:-1] + "ves", w + "s"))  # wolf → wolves、roof → roofs
    elif w.endswith("o"):
        forms.update((w + "es", w + "s"))        # hero → heroes、piano → pianos
    else:
        forms.add(w + "s")                       # cat → cats
    _plural_forms_cache[word] = forms
    return set(forms)


def _plural_bases(word: str) -> set:
    """回傳「word 可能是它的複數形」的單數基底（含 word 自身）。

    先以字尾猜想產生候選，再逐一用 _plural_forms() 反驗，猜錯的一律丟棄；
    因此 plural_map 恰為 _plural_forms 的反關係，前置過濾與複數回退不會各有一套規則
    ——舊版 _plural_candidates() 與 _find_multi_word_span() 的後綴表各寫一套，
    兩邊不一致正是「White wolf 對 white wolves 不命中」的成因。
    """
    bases = {word}
    guesses = set()
    singular = _IRREGULAR_SINGULARS.get(word)
    if singular is not None:
        guesses.add(singular)                        # men → man、people → person
    if word.endswith("ies") and len(word) > 3:
        guesses.add(word[:-3] + "y")                 # ponies → pony
    if word.endswith("ves") and len(word) > 3:
        guesses.add(word[:-3] + "f")                 # wolves → wolf
        guesses.add(word[:-3] + "fe")                # knives → knife
    if word.endswith("es") and len(word) > 2:
        guesses.add(word[:-2])                       # boxes → box、buses → bus
    if word.endswith("s") and len(word) > 1:
        guesses.add(word[:-1])                       # cats → cat
    for cand in guesses:
        if cand and word in _plural_forms(cand):
            bases.add(cand)
    return bases


def _term_pattern(body: str):
    """詞邊界 + 可選所有格的比對式，只回傳術語本身的 span。

    允許緊接的所有格（Saradomin's / Saradomin’s / demons'），
    但不允許 don't 這類縮寫：Don 仍不會誤配 don't。
    """
    return re.compile(
        r"(?<![a-z" + _APOS_CHARS + r"])(?P<t>" + re.escape(body)
        + r")(?:[" + _APOS_CHARS + r"]s?)?(?![a-z" + _APOS_CHARS + r"])"
    )


def placeholder_spans(text: str) -> list[tuple[int, int]]:
    """回傳 [...] 佔位符的 span。

    佔位符內容必須逐字保留（llm_translator.SYSTEM_PROMPT_BASE 規則 4），
    因此術語檢查與術語提示詞都要跳過落在這裡面的英文詞；否則
    `[Fremennik name]` 會被當成 Fremennik 的實際出現位置，產生譯文永遠
    改不動的假術語問題（同類案例：`[needles/thread/pieces of wool]`）。
    """
    return [m.span() for m in _re_placeholder.finditer(text)]


def drop_placeholder_spans(spans, text: str, ph_spans=None) -> list[tuple[int, int]]:
    """剔除完全落在 [...] 佔位符內的 span，其餘匹配原樣保留。

    ph_spans：可由呼叫端預先算好的 placeholder_spans(text)；同一段文本要
    過濾多條術語時先算一次，避免每條術語都重跑一次 regex。
    """
    if not spans:
        return []
    ph = placeholder_spans(text) if ph_spans is None else ph_spans
    if not ph:
        return list(spans)
    return [s for s in spans if not any(s[0] >= a and s[1] <= b for a, b in ph)]


def find_term_spans(term: str, text: str, ctx: tuple[set, dict] | None = None) -> list[tuple[int, int]]:
    """回傳術語在文本中出現的 span 清單（詞邊界）。

    只接受三種情況：
    1. 術語原樣（小寫化後）出現在文本中；
    2. 術語緊接所有格（Saradomin's / Saradomin’s）；
    3. 文本詞是術語的「真正的複數/變形」（例如 boxes 對應 Box，demons 對應 demon），
       避免 News 誤匹配 new、The Face 誤匹配 face 這類歸一化誤報。
    """
    if not term or not text:
        return []
    term_l = term.lower().strip()
    if not term_l:
        return []
    text_l = text.lower()
    if ctx is None:
        ctx = build_relevance_context(text_l)
    words, plural_map = ctx

    # 詞集合預篩只對「每個詞都是純 [a-z']+」的術語安全。
    # 含括號／數字／句點／連字號的術語（Longbow (u)、Prayer potion(4)、3rd age）
    # 在 [a-z']+ 斷詞下永遠不可能出現在 words 裡；若一併預篩，它們會永遠匹配不到
    # （實測真實術語庫 4043/21333 條），因此改為直接進入精確比對。
    plain = _re_plain_cache.get(term_l)
    if plain is None:
        plain = all(_re_word_cache.fullmatch(p) for p in term_l.split())
        _re_plain_cache[term_l] = plain

    is_multi = " " in term_l
    parts = term_l.split() if is_multi else []
    if plain:
        if is_multi:
            # 詞集合預篩：任一詞（或其變形）不在文本中就直接跳過
            for w in parts:
                if w in words:
                    continue
                if not any(inf in words for inf in plural_map.get(w, ())):
                    return []
        else:
            if term_l not in words and not any(
                inf in words for inf in plural_map.get(term_l, ())
            ):
                return []
    else:
        # 含標點／數字的術語無法用詞集合預篩，改用便宜的子字串前置檢查：
        # 術語沒有逐字出現時，精確比對與複數回退都不可能命中
        # （此檢查比對全文跑一次 regex 快約 45 倍，用來抵銷取消預篩的成本）
        if term_l not in text_l:
            return []

    # 編譯移到預篩之後：被預篩淘汰的術語不必付出編譯成本
    pat = _re_exact_cache.get(term_l)
    if pat is None:
        pat = _term_pattern(term_l)
        _re_exact_cache[term_l] = pat
    # 原樣匹配（詞邊界）
    spans = [m.span("t") for m in pat.finditer(text_l)]
    if spans:
        return spans
    # 無原樣匹配：多字詞以位置比對，單字詞以「文字詞是否為術語的複數」比對
    if is_multi:
        return _find_multi_word_span(parts, text_l)
    # 複數形改以詞邊界比對：原本用 text_l.find() 會落在更長單字內部
    # （例如 Box 對 "sandboxes boxes" 回傳 sandboxes 裡的 boxes），
    # 且只回第一個 span，其餘出現位置全部遺失。
    # 候選由 _plural_forms() 產生（與前置過濾的 plural_map 同一套規則）。
    plural_spans = []
    for w in _plural_forms(term_l):
        if w == term_l:
            continue  # 原樣已在上面找過且無命中
        pat_w = _re_plural_cache.get(w)
        if pat_w is None:
            pat_w = _term_pattern(w)
            _re_plural_cache[w] = pat_w
        plural_spans.extend(m.span("t") for m in pat_w.finditer(text_l))
    return sorted(plural_spans)


def _find_multi_word_span(parts: list, text_l: str) -> list[tuple[int, int]]:
    """以位置比對找出多字詞術語（詞與詞之間只能有空白）的實際 span。

    起點必須是詞首、終點必須是詞尾：精確比對已用 (?<![a-z'’]) / (?![a-z'’]) 保證詞界，
    這裡先前只用 str.find 不比詞界，導致 `Black 2h sword` 在
    `The Black 2h swordsman attacks` 內命中（H1 取消非 plain 術語的詞集合預篩後，
    3,799 條含標點／數字的多字詞術語開始走到這條路，假陽性因此變得可達）。
    複數後綴改為由長到短嘗試，讓 `black 2h swords` 的 span 一併涵蓋結尾的 s。
    """
    first = parts[0]
    last = parts[-1]
    start = 0
    while True:
        pos = text_l.find(first, start)
        if pos < 0:
            return []
        # 起點必須是詞首，不可落在更長單字內部
        if pos > 0 and text_l[pos - 1] in _WORD_CHAR_SET:
            start = pos + 1
            continue
        cursor = pos + len(first)
        ok = True
        for w in parts[1:-1]:
            nxt = text_l.find(w, cursor)
            if nxt < 0 or text_l[cursor:nxt].strip():
                ok = False
                break
            cursor = nxt + len(w)
        if not ok:
            start = pos + 1
            continue
        # 最後一詞：以 _plural_forms() 產生的真正複數形比對（長的優先，讓
        # wolves 先於 wolf），且終點必須是詞尾。舊版固定試後綴 ("ves","ies","es","s","")
        # 與 _plural_candidates() 各寫一套規則，會拼出 wolfves 這種不存在的字串。
        found = False
        for candidate in sorted(_plural_forms(last), key=lambda s: (-len(s), s)):
            nxt = text_l.find(candidate, cursor)
            if nxt < 0 or text_l[cursor:nxt].strip():
                continue
            end = nxt + len(candidate)
            if end < len(text_l) and text_l[end] in _WORD_CHAR_SET:
                continue
            cursor = end
            found = True
            break
        if found:
            return [(pos, cursor)]
        start = pos + 1


def _is_plain_term(term_l: str) -> bool:
    """術語是否「每個詞都是純 [a-z']+」——與 find_term_spans 的 plain 判定完全相同。

    刻意共用 `_re_plain_cache`：斷詞規則與 find_term_spans / filter_relevant_terms
    是同一個（`_re_word_cache`），術語分類不會出現第三套規則。
    """
    plain = _re_plain_cache.get(term_l)
    if plain is None:
        plain = all(_re_word_cache.fullmatch(p) for p in term_l.split())
        _re_plain_cache[term_l] = plain
    return plain


class TermLookup:
    """術語候選索引（TL4）：plain 走倒排索引，non-plain 走 3-gram 索引。

    建構成本 O(術語數 × 平均長度)，每次開掃前建一次即可（全詞庫實測 0.125 秒；
    逐批重建會比舊版逐條掃描更慢，見 Task 7 的共用要求）。

    超集性證明（候選只保證「不會漏」，精確與否仍由 find_term_spans 決定）：
    - plain 術語：find_term_spans 的詞集合預篩要求「術語的每個詞（或其複數形）
      都在文本詞集合中」，因此任取術語的一個詞當索引鍵都必然命中；
    - non-plain 術語：find_term_spans 以 `term_l in text_l` 把關。若術語逐字
      出現在文本中，術語的任一個 3-gram 也必然出現在文本中（同一位置的子字串），
      因此以「術語內最稀有的 3-gram」當鍵必然命中；命中後再用 `in` 複驗。
    """

    __slots__ = ("glossary", "order", "plain_index", "nonplain_index",
                 "nonplain_short")

    def __init__(self, glossary: dict):
        self.glossary = glossary
        self.order = {eng: i for i, eng in enumerate(glossary)}

        plain_terms: dict = {}
        nonplain_terms: dict = {}
        for eng in glossary:
            el = eng.lower().strip()
            if not el:
                continue
            if _is_plain_term(el):
                plain_terms[eng] = set(_re_word_cache.findall(el))
            else:
                nonplain_terms[eng] = el

        # ── plain：挑「詞庫中出現次數最少」的 token 當主要鍵 ──
        token_freq: dict = {}
        for toks in plain_terms.values():
            for t in toks:
                token_freq[t] = token_freq.get(t, 0) + 1
        plain_index: dict = {}
        for eng, toks in plain_terms.items():
            key = min(toks, key=lambda t: (token_freq.get(t, 0), -len(t), t))
            for form in _plural_forms(key):
                plain_index.setdefault(form, []).append(eng)
        self.plain_index = plain_index

        # ── non-plain：挑「術語集合內出現次數最少」的 3-gram 當主要鍵 ──
        gram_freq: dict = {}
        term_grams: dict = {}
        for eng, el in nonplain_terms.items():
            if len(el) < 3:                       # 沒有 3-gram → 直接子字串桶
                term_grams[eng] = None
                continue
            grams = {el[i:i + 3] for i in range(len(el) - 2)}
            term_grams[eng] = grams
            for g in grams:
                gram_freq[g] = gram_freq.get(g, 0) + 1
        nonplain_index: dict = {}
        nonplain_short: list = []
        for eng, grams in term_grams.items():
            if grams is None:
                nonplain_short.append(eng)
                continue
            key = min(grams, key=lambda g: (gram_freq[g], g))
            nonplain_index.setdefault(key, []).append(eng)
        self.nonplain_index = nonplain_index
        self.nonplain_short = nonplain_short

    def candidates(self, text_l: str, ctx=None) -> dict:
        """回傳「可能是精確命中」的術語子集（保留 glossary 原順序）。

        text_l 必須已轉小寫（與 find_term_spans 的內部約定一致）。
        ctx 是可選的 build_relevance_context(text_l)；單列與整批文本都直接傳入。
        """
        words, _plural_map = ctx if ctx is not None else build_relevance_context(text_l)
        hits: set = set()

        # plain：文本詞集合 ∩ 倒排索引
        plain_index = self.plain_index
        for w in words:
            lst = plain_index.get(w)
            if lst:
                hits.update(lst)

        # non-plain：文本的所有 3-gram ∩ 倒排索引，再用整串子字串複驗
        nonplain_index = self.nonplain_index
        if nonplain_index:
            for i in range(len(text_l) - 2):
                lst = nonplain_index.get(text_l[i:i + 3])
                if lst:
                    for eng in lst:
                        if eng.lower().strip() in text_l:
                            hits.add(eng)
        for eng in self.nonplain_short:
            if eng.lower().strip() in text_l:
                hits.add(eng)

        if not hits:
            return {}
        g = self.glossary
        return {e: g[e] for e in sorted(hits, key=self.order.__getitem__)}

def filter_relevant_terms(glossary: dict, text_l: str,
                          ctx: tuple[set, dict] | None = None) -> dict:
    """以「安全上界」過濾術語庫，取代對整份文本逐條 find_term_spans 的精確掃描。

    回傳值必定是精確結果（哪些術語真的出現在文本中）的**超集**，因此下游的
    逐列精確比對不會漏掉任何術語，輸出與精確過濾相同；但成本從
    O(術語數 × 文本長度) 降到 O(術語數) 加一次子字串掃描。

    為什麼是超集：
    - plain 術語若出現，它的每個詞（或其複數／所有格形式）必然出現在文本的
      詞集合中，故此測試必定通過；
    - 非 plain 術語在 find_term_spans 內部本來就先以 `term_l in text_l` 把關，
      同樣是精確結果的超集。

    為什麼多出來的術語無害：本函式的呼叫端傳入的是「整張工作表串起來的
    all_text」，任何會在某一列命中的術語都必然落在精確集合內；因此差集裡的
    術語在整份文本中根本不存在，對 scan_issues 的逐列比對與
    get_relevant_glossary 的批次提示永遠不會命中，只多付一次預篩成本。
    """
    words, plural_map = ctx if ctx is not None else build_relevance_context(text_l)
    relevant: dict = {}
    for eng, chn in glossary.items():
        el = eng.lower().strip()
        if not el:
            continue
        plain = _re_plain_cache.get(el)
        if plain is None:
            plain = all(_re_word_cache.fullmatch(p) for p in el.split())
            _re_plain_cache[el] = plain
        if plain:
            for w in el.split():
                if w in words:
                    continue
                if any(inf in words for inf in plural_map.get(w, ())):
                    continue
                break
            else:
                relevant[eng] = chn
        elif el in text_l:
            relevant[eng] = chn
    return relevant


def load_glossary(
    glossary_path: str | Path | None,
    sheet_names: list[str] | None = None,
) -> dict[str, str]:
    glossary: dict[str, str] = {}
    if glossary_path is not None:
        path = Path(glossary_path)
        if path.exists():
            with pd.ExcelFile(path) as xls:
                sheets = sheet_names if sheet_names else xls.sheet_names
                for sn in sheets:
                    if sn not in xls.sheet_names:
                        continue
                    df = pd.read_excel(path, sheet_name=sn, dtype=str)
                    if "english" in df.columns and "translation" in df.columns:
                        for _, row in df.iterrows():
                            eng_raw = row["english"]
                            if pd.isna(eng_raw):
                                continue
                            eng = str(eng_raw).strip()
                            if not eng:
                                continue
                            trans = row["translation"]
                            if pd.notna(trans) and str(trans).strip():
                                chn = str(trans).strip()
                                # 缺值哨兵（"nan"/"nat"/"none"）不可當成譯文載入；
                                # 舊寫法只擋 "nan"，"nat"/"none"/"None" 會被寫進術語庫
                                if chn.lower() not in MISSING_SENTINELS:
                                    glossary[eng] = chn
    for eng, chn in ADD_LIST.items():
        glossary[eng] = chn

    # 用歸一化匹配來過濾 IGNORE_LIST（捕獲單數/複數/大小寫）
    ignore_norm_set = {normalize_term(t) for t in IGNORE_LIST}
    glossary = {
        k: v for k, v in glossary.items()
        if normalize_term(k) not in ignore_norm_set
    }

    return glossary

def auto_extract_glossary(
    target_excel_path: str | Path,
    output_dir: str | Path,
) -> dict[str, str]:
    """自動從翻譯目標 Excel 的 name/manual 工作表萃取術語庫。

    篩選條件：
    - name 工作表：sub_category ∈ {item, menu, npc, object}
    - manual 工作表：sub_category ∈ {activity, location, quest, slayer_mob}
    - translation 不為空且不等於 english
    """
    target_path = Path(target_excel_path)

    glossary: dict[str, str] = {}
    review_rows = []

    name_categories = {"item", "menu", "npc", "object"}
    manual_categories = {"activity", "location", "quest", "slayer_mob"}

    with pd.ExcelFile(target_path) as xls:
        for sheet_name, valid_cats in [("name", name_categories), ("manual", manual_categories)]:
            if sheet_name not in xls.sheet_names:
                print(f"  工作表 '{sheet_name}' 不存在，跳過")
                continue
            df = pd.read_excel(target_path, sheet_name=sheet_name, dtype=str)
            if "sub_category" not in df.columns:
                print(f"  工作表 '{sheet_name}' 缺少 sub_category 欄位，跳過")
                continue
            if "english" not in df.columns or "translation" not in df.columns:
                print(f"  工作表 '{sheet_name}' 缺少 english 或 translation 欄位，跳過")
                continue

            mask = df["sub_category"].str.strip().str.lower().isin(valid_cats)
            filtered = df[mask]
            print(f"  從 '{sheet_name}' 篩出 {len(filtered)} 行")

            for _, row in filtered.iterrows():
                eng_raw = row.get("english")
                if pd.isna(eng_raw):
                    continue
                eng = str(eng_raw).strip()
                if not eng:
                    continue
                trans = row.get("translation")

                if pd.isna(trans):
                    continue
                trans_str = str(trans).strip()
                # 缺值哨兵（"nan"/"nat"/"none"）不可當成有效譯文寫進審查檔；
                # 舊寫法只擋 "nan"，"nat"/"none"/"None" 會被寫進自動術語庫
                if not trans_str or trans_str.lower() in MISSING_SENTINELS:
                    continue
                if trans_str == eng:
                    continue

                glossary[eng] = trans_str
                review_rows.append({
                    "english": eng,
                    "translation": trans_str,
                    "category": str(row.get("category", "")),
                    "sub_category": str(row.get("sub_category", "")),
                })

    # 合併 ADD_LIST（覆蓋相同 key）
    for eng, chn in ADD_LIST.items():
        glossary[eng] = chn

    # IGNORE_LIST 歸一化過濾
    ignore_norm_set = {normalize_term(t) for t in IGNORE_LIST}
    glossary = {
        k: v for k, v in glossary.items()
        if normalize_term(k) not in ignore_norm_set
    }

    # 輸出審查檔案
    output_path = Path(output_dir) / "auto_glossary_for_review.xlsx"
    if review_rows:
        review_df = pd.DataFrame(review_rows)
        review_df.to_excel(output_path, index=False)
        print(f"  自動術語庫審查檔已寫入: {output_path} ({len(review_df)} 條)")
    else:
        review_df = pd.DataFrame(columns=["english", "translation", "category", "sub_category"])
        review_df.to_excel(output_path, index=False)
        print(f"  自動萃取未產生任何術語條目，空審查檔已建立: {output_path}")

    return glossary