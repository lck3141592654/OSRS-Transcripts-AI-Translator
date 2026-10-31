"""多 API 配置解析模組。

從 .env 讀取多個 API 設定，支援主/副 API 分類，
以及類別級預設並發與個別覆蓋。
"""

import json
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

# 類別級預設並發（硬編碼）
MAIN_DEFAULT_LIMIT = 100
FALLBACK_DEFAULT_LIMIT = 1

# 每個 API 獨立的最小請求間隔（秒）
REQUEST_INTERVAL = 1

# 所有 LLM 請求的統一逾時（秒）
API_TIMEOUT = 3600

# 分項逾時（秒）：整數 timeout=API_TIMEOUT 會被 httpx 展開成「所有分項都是 3600」，
# 連線逾時因此從 SDK 預設的 5 秒膨脹成 1 小時；連線卡住時 worker 會抱著
# semaphore 名額不放，asyncio.gather 讓整個 phase 一起停擺。
#
# 使用者指定的值（2026-10-08 確認）：
#   connect = 60   —— SDK 預設只有 5 秒；正常連線 1 秒內完成，60 秒已非常寬鬆
#   read    = 1800 —— 這就是「等 API 回覆的時間」= 30 分鐘。非串流回應下資料是在
#                     生成完成後才整批送回來，所以 read 實質等於總等待時間
#   write   = 300  —— 寬鬆值；實際一批只有約 18 KB，真正需要的是幾秒
#   pool    = 300  —— 寬鬆值；等待連線池名額幾乎不會觸發
API_CONNECT_TIMEOUT = 60
API_READ_TIMEOUT = 1800
API_WRITE_TIMEOUT = 300
API_POOL_TIMEOUT = 300

# 429 停用次數：累計第 N 次 429 後永久停用（預設 2，可自行調整）
PERMANENT_DISABLE_AFTER = 2

# 池中「只有一個」可用 API 時，額外容忍的 429 次數：
# 唯一一個 API 被永久停用等於整個執行直接中止，故放寬門檻
SINGLE_API_EXTRA_429_TOLERANCE = 3

# 過量回傳容許比例：回傳數超過批次數的這個比例即視為該批次失敗（走 3 輪重試）
OVER_RETURN_TOLERANCE = 1.2


# ── 限流（429）判定 ──────────────────────────────────────────────
# 只認「429 狀態碼」「訊息含獨立的 429」「rate limit 片語」；
# 不可用裸字串 "rate"（會誤判 generate／separate／accurate 這類字）。
RATE_LIMIT_PHRASE = "rate limit"
# 片語寫法容許空白／底線／連字號／無分隔：rate limit / rate_limit / rate-limit / ratelimit
# （實測供應商會回 "rate_limit_exceeded"、"Ratelimit exceeded"，舊的裸 "rate" 判定會命中，
#  只看 RATE_LIMIT_PHRASE 反而漏判 → 真的要冷卻時不冷卻，繼續打被限流的端點）
_RATE_LIMIT_RE = re.compile(r"rate[\s_-]?limit")
# 其他常見的限流文案：沒有 429、也沒有 "rate limit" 字樣，但語意就是限流
# （使用者裁示：這些一律視為 429 處理）
#   too many requests
#   quota 與 exceed 同時出現且距離很近（涵蓋 "quota exceeded" 與 OpenAI 的
#   "You exceeded your current quota" 兩種語序）；刻意不收 "insufficient quota"
#   ——那是額度用盡（計費）而非限流，語意不同。
_RATE_LIMIT_EXTRA_RE = re.compile(
    r"too many requests|quota[^.]{0,40}exceed|exceed[^.]{0,40}quota")
# 「429」必須是獨立數字：避免 "max_tokens 4290 too large" 這種內含 429 的訊息被誤判成限流
_429_RE = re.compile(r"(?<![\d.])429(?![\d.])")

# 以類別名判定限流例外（openai 匯入失敗時仍可運作）
_RATE_LIMIT_EXC_NAMES = frozenset({
    "RateLimitError", "RateLimitException",
    "TooManyRequestsError", "TooManyRequestsException",
    "RateLimitPayloadError",
})


class RateLimitPayloadError(RuntimeError):
    """HTTP 200 但回應內容是限流錯誤包裝時往上拋的例外。

    是 RuntimeError 的子類，訊息一律含 "429"；解析階段用它把限流語意
    傳給批次層，讓 cfg.mark_429()／pool.retry(job) 接手。
    獨立型別是為了讓 json_repair 分支能精準 re-raise，不必用過寬的
    `except RuntimeError` 把修復流程其他 RuntimeError 一起放行。
    """


# 錯誤物件的包裝層 key（供應商可能回 {"error": ...}，或包在 data/response 內）
# detail／code 是為了涵蓋 FastAPI 風格 {"detail": "Rate limit exceeded"} 與
# 頂層 {"code": 429, "message": ...}（沒有 error 鍵）這兩種實測常見形狀。
_PAYLOAD_ERROR_KEYS = ("error", "errors", "detail", "code")
_PAYLOAD_CONTAINER_KEYS = ("data", "response", "body", "result", "detail", "payload")


def _safe_getattr(obj, name, default=None):
    """getattr 的防護版：屬性的 property 拋錯時回傳 default。"""
    try:
        return getattr(obj, name, default)
    except Exception:
        return default


def _safe_lower_str(value) -> str:
    """str(value).lower() 的防護版：取字串失敗時回傳空字串。"""
    try:
        return str(value).lower()
    except Exception:
        return ""


def _is_429_code(value) -> bool:
    """狀態碼是否為 429（容忍 int、字串與 HTTPStatus 這類列舉）。"""
    if value is None or isinstance(value, bool):
        return False
    try:
        if value == 429:
            return True
    except Exception:
        return False
    try:
        return int(str(value).strip()) == 429
    except (TypeError, ValueError, AttributeError):
        return False


def _looks_like_rate_limit_type(exc) -> bool:
    """例外型別是否為限流類（openai 匯入失敗不影響判定）。"""
    try:
        from openai import RateLimitError  # 區域匯入：匯入失敗不讓整個判定失效
        if isinstance(exc, RateLimitError):
            return True
    except Exception:
        pass
    try:
        return any(k.__name__ in _RATE_LIMIT_EXC_NAMES for k in type(exc).__mro__)
    except Exception:
        return False


def is_rate_limit_error(exc: BaseException) -> bool:
    """限流判定：只認 429 狀態碼、訊息含 "429"、或 "rate limit" 片語。

    不可再用裸字串 "rate"（會誤判 generate／separate／accurate 這類字）。
    狀態碼依序看 exc.status_code、exc.response.status_code、exc.code；
    openai.RateLimitError 這類例外型別也算。純函式、不依賴網路。
    """
    if exc is None:
        return False
    if _looks_like_rate_limit_type(exc):
        return True
    for attr in ("status_code", "code"):
        if _is_429_code(_safe_getattr(exc, attr)):
            return True
    response = _safe_getattr(exc, "response")
    if response is not None:
        for attr in ("status_code", "code"):
            if _is_429_code(_safe_getattr(response, attr)):
                return True
    s = _safe_lower_str(exc)
    return _is_rate_limit_text(s)


def _is_rate_limit_text(value) -> bool:
    """文字內容是否代表限流：獨立的 429、rate limit 片語（含底線／連字號寫法），
    或 too many requests／quota exceeded 這類沒有數字也沒有 rate limit 字樣的文案。"""
    s = _safe_lower_str(value)
    return bool(_429_RE.search(s) or _RATE_LIMIT_RE.search(s) or _RATE_LIMIT_EXTRA_RE.search(s))


def _find_payload_error(obj, max_depth: int = 5, max_nodes: int = 64):
    """在 dict 的常見包裝層內廣度優先找出 error／errors／detail／code；找不到回傳 None。

    depth 5 是為了涵蓋實測看到的「多層 data/response/body 包裝」；
    max_nodes 仍設上限，避免異常大的回應拖慢判定。
    """
    queue = [(obj, 0)]
    visited = 0
    while queue and visited < max_nodes:
        current, depth = queue.pop(0)
        visited += 1
        if not isinstance(current, dict):
            continue
        for key in _PAYLOAD_ERROR_KEYS:
            if key in current:
                value = current[key]
                # 值為 None 時不可直接放棄：供應商可能回
                # {"error": null, "data": {"error": {"code": 429}}}，要繼續往包裝層找
                if value is not None:
                    return value
        if depth >= max_depth:
            continue
        for key in _PAYLOAD_CONTAINER_KEYS:
            child = current.get(key)
            if isinstance(child, dict):
                queue.append((child, depth + 1))
    return None


def is_rate_limit_payload(obj) -> bool:
    """判斷已解析的 JSON 內容是否為限流錯誤包裝（HTTP 200 但 body 是 error 物件）。

    只認 dict 根（清單根一律 False，避免誤判正常翻譯陣列）；命中條件與
    is_rate_limit_error 同口徑：error.code == 429、內容含獨立的 429、
    或 rate limit 片語（含 rate_limit／rate-limit／ratelimit 寫法）。
    純函式、不依賴網路。
    """
    if not isinstance(obj, dict):
        return False
    error = _find_payload_error(obj)
    if error is None:
        return False
    candidates = error if isinstance(error, list) else [error]
    for item in candidates:
        if isinstance(item, dict):
            # dict 沒有 .code 屬性：一定要用 .get()，否則這條判定形同死碼
            code = item.get("code")
            if code is None:
                code = item.get("status")
            if _is_429_code(code):
                return True
        else:
            if _is_429_code(_safe_getattr(item, "code")):
                return True
        if _is_rate_limit_text(item):
            return True
    return False


@dataclass
class ApiConfig:
    """單個 API Key 的完整配置"""

    api_id: str         # "API1", "API2" ...
    key: str            # API_KEY
    model_provider: str # 供應商名稱
    model: str          # 模型名稱
    base_url: str       # API 端點
    api_type: str       # "main" | "fallback"
    parallel_limit: int # 最終解析後的並發上限

    # .env 的 APIx_EXTRA_HEADERS／EXTRA_HEADERS 解析結果，每個請求都會帶上。
    # 空 dict ＝ 不帶任何自訂 header（請求內容與改動前完全相同）。
    # repr=False：避免未來有人 print(cfg) 時把 header 值（可能是 session／token）印出來。
    # kw_only=True：欄位插在中間，若有人用位置參數建構 ApiConfig 會靜默錯位；
    # 本類別所有「有預設值」的欄位都是關鍵字限定，位置參數會直接 TypeError。
    extra_headers: dict = field(default_factory=dict, repr=False, kw_only=True)

    # 運行時狀態（不從 .env 讀取）。與 extra_headers 一樣全部關鍵字限定：
    # 這個類別的欄位順序一旦被誤用成位置參數就會靜默錯位，關鍵字限定能讓
    # 那種寫法直接 TypeError（目前兩處建構都是全關鍵字）。
    strike: int = field(default=0, kw_only=True)
    cooldown_until: float = field(default=0.0, kw_only=True)
    # 使用者／載入時決定的門檻；池中只有一個可用 API 時 disable_after 會被暫時放寬，
    # base_disable_after 保留原值，讓放寬可逆（見 shared_pool._relax_single_api_threshold）
    base_disable_after: int = field(default=PERMANENT_DISABLE_AFTER, kw_only=True)
    # 累計幾次 429 後永久停用；池中只有一個可用 API 時會被放寬（見 shared_pool）
    disable_after: int = field(default=PERMANENT_DISABLE_AFTER, kw_only=True)

    @property
    def is_cooling_down(self) -> bool:
        """是否正在冷卻中"""
        import time
        return time.monotonic() < self.cooldown_until

    def mark_429(self) -> None:
        """記錄一次 429，並重新進入 60 秒冷卻。

        同一冷卻窗內只累計一次 strike：一個 API 有 parallel_limit 個並行請求，
        觸頂時同一秒內完成的 10 個回應可能全部是 429；若每個都累加，
        strike 會一次跳 10、直接越過 PERMANENT_DISABLE_AFTER 而永久停用該 API，
        整個 phase 隨之中止。冷卻本身仍然每次重新計時（需求不變）。
        """
        import time
        now = time.monotonic()
        if not self.is_cooling_down:
            self.strike += 1
        self.cooldown_until = now + 60

    @property
    def is_permanently_disabled(self) -> bool:
        """累計 429 達 disable_after 次後永久停用"""
        return self.strike >= self.disable_after

    @property
    def is_available(self) -> bool:
        """該 API 當前是否可用（未冷卻且未永久停用）"""
        return not self.is_cooling_down and not self.is_permanently_disabled


# ── 自訂 HTTP header（APIx_EXTRA_HEADERS） ──────────────────────────
# 部分供應商要求特定 header 才給用，例如 OpenCode Go：
#   - 每個請求帶 x-opencode-session: <穩定的 session id>（路由與 prompt caching）
#   - 建議用自己的 User-Agent，而不是 SDK／HTTP 函式庫的預設名稱
# 兩種寫法都支援：
#   JSON： {"x-opencode-session": "osrs-translator", "User-Agent": "osrs-translator/1.0"}
#   簡寫： x-opencode-session: osrs-translator; User-Agent: osrs-translator/1.0
# 值可用 <RUN_ID>：展開成「每次啟動腳本（一個 process）一個 UUID」。
#
# 為何是 import 期產生一次，而不是每次呼叫 load_api_configs() 產生：
# SharedBatchPool 每建一個池就呼叫一次 load_api_configs()，一次執行最多 4 次
# （主翻譯、校對、enforcer 臨時池…），若在函式內生成，同一次執行會出現多個
# 不同的 session id，供應商端的路由／prompt caching 就會被打散。
_RUN_ID = uuid.uuid4().hex
_RUN_ID_TOKEN = "<RUN_ID>"

# RFC 7230 token：header 名稱只允許這些字元（空白、冒號、非 ASCII 一律不合法）
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")

# RFC 7230 field-value 只允許可見 ASCII 與 HTAB。h11 另外拒絕 \x00/\x0b/\x0c，
# 若放行，載入期看起來正常、送出時卻變成 APIConnectionError: Connection error.，
# 錯誤訊息完全指不到 header。
_HEADER_VALUE_BAD_RE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")

# 不可覆寫的 header：會破壞 SDK 認證或 HTTP 語意（Authorization 會靜默取代 api_key），
# 或是本來就不該由用戶端指定的欄位（例如用 X-Forwarded-For 偽造來源）。
_BLOCKED_HEADERS = frozenset({
    "authorization", "content-type", "content-length", "host",
    "connection", "transfer-encoding", "content-encoding",
    "proxy-authorization", "x-forwarded-for", "forwarded",
})

# x-stainless-* 是 openai SDK 的內部標記，覆寫沒有意義且可能弄壞請求
_BLOCKED_HEADER_PREFIXES = ("x-stainless-",)

# SDK 自己會送的 header：名稱必須用它的寫法。HTTP header 名稱不分大小寫，但 SDK
# 用大小寫敏感的 dict 合併，所以寫 user-agent（小寫）會變成「兩條並存」，
# 伺服器通常取第一條 → 自訂值等於沒生效（實測得知）。
_CANONICAL_HEADER_NAMES = {"user-agent": "User-Agent", "accept": "Accept"}

# 允許但提醒：OpenCode Go 明確要求「用自己的 User-Agent，不要用通用 SDK 名稱」，
# 所以不可把 User-Agent 列進黑名單，只在套用時印一行提示。
_NOTED_HEADERS = frozenset({"user-agent"})


def parse_extra_headers(spec: str) -> dict[str, str]:
    """解析 APIx_EXTRA_HEADERS 的字串（純函式，不讀環境變數）。

    兩種寫法：
      JSON：  {"x-opencode-session": "abc", "User-Agent": "my-agent/1.0"}
      簡寫：  名稱: 值; 名稱2: 值2（只用第一個冒號切分，值內可含 "https://" 這種冒號）

    錯誤處理：
      - 單筆 header 有問題（名稱不合法／空值／非 ASCII／控制字元／被擋）→ 只忽略該筆；
      - 簡寫某一段缺少冒號 → 只忽略該段（值含 ";" 時請改用 JSON）；
      - 整串 JSON 解析失敗 → 放棄全部並回傳空 dict。
    任何情況都不會讓呼叫端例外，也不會讓整個 API 從並發池消失。
    """
    text = (spec or "").strip()
    if not text:
        return {}

    if text.startswith("{"):
        try:
            data = json.loads(text)
        except Exception as e:
            print(f"  警告：自訂 header 的 JSON 解析失敗（{type(e).__name__}），此 API 將不帶自訂 header")
            return {}
        raw_pairs = list(data.items())
    else:
        raw_pairs = []
        for chunk in text.split(";"):
            chunk = chunk.strip()
            if not chunk:
                continue
            name, sep, value = chunk.partition(":")
            if not sep:
                print("  警告：自訂 header 有一段缺少冒號，已忽略該段（值含 ; 時請改用 JSON 寫法）")
                continue
            raw_pairs.append((name.strip(), value.strip()))

    headers: dict[str, str] = {}
    seen: dict[str, str] = {}
    for raw_name, raw_value in raw_pairs:
        name = str(raw_name).strip()
        if not name:
            print("  警告：自訂 header 名稱是空的，已忽略")
            continue
        if not _HEADER_NAME_RE.match(name):
            print(f"  警告：自訂 header 名稱 {name!r} 不合法，已忽略")
            continue
        lowered = name.lower()
        if lowered in _BLOCKED_HEADERS:
            print(f"  警告：自訂 header {name} 會覆寫必要欄位，已忽略")
            continue
        if lowered.startswith(_BLOCKED_HEADER_PREFIXES):
            print(f"  警告：自訂 header {name} 屬於 SDK 內部欄位，已忽略")
            continue
        if not isinstance(raw_value, str):
            print(f"  警告：自訂 header {name} 的值不是字串，已忽略")
            continue
        # 換成 SDK 用的寫法，避免同名不同大小寫在線路上變成兩條
        name = _CANONICAL_HEADER_NAMES.get(lowered, name)
        lowered = name.lower()
        value = raw_value.replace(_RUN_ID_TOKEN, _RUN_ID).strip()
        if not value:
            print(f"  警告：自訂 header {name} 的值是空的，已忽略")
            continue
        if not value.isascii():
            # httpx 用 ASCII 編碼 header 值（連 latin-1 可編碼的 café 都會炸），
            # 而且那是裸 UnicodeEncodeError，不會被 except openai.* 接到。
            print(f"  警告：自訂 header {name} 的值含非 ASCII 字元（HTTP header 只能是 ASCII），已忽略")
            continue
        if _HEADER_VALUE_BAD_RE.search(value):
            print(f"  警告：自訂 header {name} 的值含控制字元或換行，已忽略")
            continue
        if lowered in seen:
            print(f"  警告：自訂 header {seen[lowered]} 重複設定（不分大小寫），以最後一筆為準")
            headers.pop(seen.pop(lowered), None)
        if lowered in _NOTED_HEADERS:
            print(f"  提示：已使用自訂 {name}，SDK 預設值會被覆寫")
        headers[name] = value
        seen[lowered] = name
    return headers


def _safe_extra_headers(env_key: str) -> dict[str, str]:
    """從 os.environ 讀取並解析 header 設定；未設定時回傳空 dict。"""
    return parse_extra_headers(os.getenv(env_key, ""))


# .env 中 API 設定的編號前綴樣式（API1_, API12_ ...）
_API_ENV_PREFIX_RE = re.compile(r"^API(\d+)_")


def _resolve_parallel_limit(limit_str: str, api_type: str, label: str) -> int:
    """解析並發上限：有獨立值就用，否則用類別預設；無效值印警告後退回預設。

    多 API 格式（APIx_PARALLEL_LIMIT）與舊版單一 API 格式（PARALLEL_LIMIT）共用，
    避免兩邊的驗證規則（≥ 1、非數字）分岔。
    """
    default = MAIN_DEFAULT_LIMIT if api_type == "main" else FALLBACK_DEFAULT_LIMIT
    limit_str = (limit_str or "").strip()
    if not limit_str:
        return default
    try:
        value = int(limit_str)
    except ValueError:
        print(f"  警告：{label} 的 PARALLEL_LIMIT '{limit_str}' 無效，使用類別預設")
        return default
    if value < 1:
        print(f"  警告：{label} 的 PARALLEL_LIMIT={limit_str} 必須 ≥ 1，使用類別預設")
        return default
    return value


def _discover_api_indices() -> list[int]:
    """掃描 os.environ 找出所有 API<數字>_ 前綴的編號，去重後由小到大排序。

    改為掃描而非「從 1 逐號遞增、遇到第一個缺 TYPE 的編號就停」：
    否則 .env 只要跳號（例如只有 API1_* 與 API3_*），後面編號的設定永遠載入不到。
    """
    indices = set()
    for name in os.environ:
        m = _API_ENV_PREFIX_RE.match(name)
        if m:
            indices.add(int(m.group(1)))
    return sorted(indices)


def load_api_configs() -> list[ApiConfig]:
    """從 .env 載入所有 API 配置，返回已解析的 ApiConfig 列表。

    掃描環境中所有 API<數字>_ 前綴（編號可跳號），逐一解析。
    每個 API 必填：_TYPE, _MODEL_PROVIDER, _MODEL, _API_KEY, _BASE_URL
    可選：_PARALLEL_LIMIT（覆蓋類別預設）、_EXTRA_HEADERS（自訂 HTTP header，
    格式見 parse_extra_headers；舊版單一 API 格式用 EXTRA_HEADERS）
    """
    configs: list[ApiConfig] = []

    for idx in _discover_api_indices():
        prefix = f"API{idx}_"
        api_type = os.getenv(f"{prefix}TYPE", "").strip().lower()

        if not api_type:
            print(f"  警告：API{idx} 缺少 TYPE，跳過")
            continue

        if api_type not in ("main", "fallback"):
            print(f"  警告：API{idx} 的 TYPE '{api_type}' 無效，跳過（應為 main 或 fallback）")
            continue

        api_key = os.getenv(f"{prefix}API_KEY", "").strip()
        if not api_key:
            print(f"  警告：API{idx} 缺少 API_KEY，跳過")
            continue

        model = os.getenv(f"{prefix}MODEL", "").strip()
        if not model:
            print(f"  警告：API{idx} 缺少 MODEL，跳過")
            continue

        base_url = os.getenv(f"{prefix}BASE_URL", "").strip()
        if not base_url:
            print(f"  警告：API{idx} 缺少 BASE_URL，跳過")
            continue
        model_provider = os.getenv(f"{prefix}MODEL_PROVIDER", "").strip()

        # 解析並發上限：獨立值 > 類別預設
        parallel_limit = _resolve_parallel_limit(
            os.getenv(f"{prefix}PARALLEL_LIMIT", ""), api_type, f"API{idx}")

        configs.append(ApiConfig(
            api_id=f"API{idx}",
            key=api_key,
            model_provider=model_provider,
            model=model,
            base_url=base_url,
            api_type=api_type,
            parallel_limit=parallel_limit,
            extra_headers=_safe_extra_headers(f"{prefix}EXTRA_HEADERS"),
        ))

    if not configs:
        # 向後相容：如果沒有 API1_ 系列設定，回退到舊的單一 API 格式
        legacy_key = os.getenv("API_KEY", "").strip()
        if legacy_key:
            print("  使用舊版單一 API 配置（向後相容）")
            configs.append(ApiConfig(
                api_id="API1",
                key=legacy_key,
                model_provider=os.getenv("MODEL_PROVIDER", "").strip(),
                model=os.getenv("MODEL", "").strip(),
                base_url=os.getenv("BASE_URL", "").strip(),
                api_type="main",
                parallel_limit=_resolve_parallel_limit(
                    os.getenv("PARALLEL_LIMIT", ""), "main", "舊版單一 API"),
                extra_headers=_safe_extra_headers("EXTRA_HEADERS"),
            ))

    return configs