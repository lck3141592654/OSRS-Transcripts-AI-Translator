# Persistent shared batch pool for the whole run.

import asyncio
import time

import httpx
from openai import AsyncOpenAI

from api_config import (
    load_api_configs, REQUEST_INTERVAL,
    SINGLE_API_EXTRA_429_TOLERANCE,
    API_CONNECT_TIMEOUT, API_READ_TIMEOUT, API_WRITE_TIMEOUT, API_POOL_TIMEOUT,
)


class _Job:
    __slots__ = ("batch_num", "batch", "func", "ctx", "fut", "retried")

    def __init__(self, batch_num, batch, func, ctx, fut):
        self.batch_num = batch_num
        self.batch = batch
        self.func = func
        self.ctx = ctx
        self.fut = fut
        self.retried = False


class SharedBatchPool:
    # Persistent shared batch pool.
    # One pool per run owns all API configs, clients, semaphores and 429 state.
    # Contract for process functions:
    #   - success -> return the result (pool resolves the future)
    #   - 429     -> call pool.retry(job) then return None (job is requeued)
    #   - error   -> raise (pool sets exception on the future)

    def __init__(self, api_configs=None):
        self.api_configs = api_configs if api_configs is not None else load_api_configs()
        self._relax_single_api_threshold()
        self._queue = asyncio.Queue()
        self._worker_states = []
        self._workers = []
        self._inflight = set()
        self._closed = False
        self._loop = None

    def _relax_single_api_threshold(self):
        """依「其他 API 設定是否都已永久停用」動態調整每個 API 的永久停用門檻。

        剩最後一個還活著的 API 時，把它的門檻放寬為「使用者設定值 +
        SINGLE_API_EXTRA_429_TOLERANCE」——唯一一個 API 被永久停用等於整個執行
        直接中止；其他 API 回來時恢復成使用者設定值，因此放寬是可逆的，也不會
        把使用者調高的門檻降低。

        判定刻意用「其他設定是否都已停用」而不是「可用數 == 1」：後者在
        cfg.mark_429() 之後會把「剛好達到門檻的這一個」也排除掉，導致放寬
        永遠來不及生效（實測：strike 剛好到 2 時仍被判永久停用）。

        以 base_disable_after（使用者設定值）為基準，重複呼叫不會累加；
        在 __init__、worker 迴圈頂端與 note_429()（429 路徑）都會呼叫，
        所以「執行途中其他 API 才停用」也能立即生效。
        """
        alive = [c for c in self.api_configs if not c.is_permanently_disabled]
        for cfg in self.api_configs:
            others_alive = [c for c in alive if c is not cfg]
            target = (cfg.base_disable_after + SINGLE_API_EXTRA_429_TOLERANCE
                      if not others_alive else cfg.base_disable_after)
            if cfg.disable_after != target:
                cfg.disable_after = target

    async def start(self):
        self._loop = asyncio.get_running_loop()
        active = [c for c in self.api_configs if not c.is_permanently_disabled]
        if not active:
            raise RuntimeError("no available API")
        for cfg in active:
            self._worker_states.append({
                "cfg": cfg,
                "client": AsyncOpenAI(
                    api_key=cfg.key,
                    base_url=cfg.base_url,
                    # 關閉 SDK 內建重試：程式自己已有 3 次重試迴圈，
                    # 兩者相乘會讓單一批次的最壞嘗試次數變成 9 次。
                    max_retries=0,
                    # 分項逾時：整數 timeout 會把 connect 也變成 1 小時
                    timeout=httpx.Timeout(
                        connect=API_CONNECT_TIMEOUT,
                        read=API_READ_TIMEOUT,
                        write=API_WRITE_TIMEOUT,
                        pool=API_POOL_TIMEOUT,
                    ),
                ),
                "sem": asyncio.Semaphore(cfg.parallel_limit),
                "rate_lock": asyncio.Lock(),
                "last_request": 0.0,
                "active_count": 0,
            })
        # 每個 API 建立 parallel_limit 個 worker，才能真正並行處理多個批次
        self._workers = [
            asyncio.create_task(self._worker_loop(ws))
            for ws in self._worker_states
            for _ in range(ws["cfg"].parallel_limit)
        ]

    async def submit(self, batch_num, batch, func, ctx=None):
        if self._closed:
            # pool 已關閉：再等下去不會有 worker 消耗這個 job，直接失敗而非永久 pending
            raise RuntimeError("pool closed")
        if self._all_disabled():
            raise RuntimeError("all APIs permanently disabled")
        fut = self._loop.create_future()
        job = _Job(batch_num, batch, func, ctx, fut)
        await self._queue.put(job)
        return await fut

    def note_429(self, cfg):
        """記錄一次 429（cfg.mark_429()）並立即重算永久停用門檻。

        429 可能剛好發生在「另一個 API 已被永久停用」之後；若等到 worker 迴圈下一輪
        才放寬，僅剩的那個 API 可能在這一秒內的第 2 次 429 就被永久停用——正是要避免
        的情況。批次層原本直接呼叫 cfg.mark_429()，改用這個入口可確保判定順序正確。
        """
        cfg.mark_429()
        self._relax_single_api_threshold()

    def retry(self, job):
        job.retried = True
        self._queue.put_nowait(job)

    def _all_disabled(self) -> bool:
        """所有 API 設定是否都已永久停用（語意與原本的 all(...) 相同）"""
        return all(c.is_permanently_disabled for c in self.api_configs)

    async def close(self):
        self._closed = True
        for w in self._workers:
            w.cancel()
        # 取消前先記下正在執行中的 job：worker 的 finally 會把它們從 _inflight
        # 移除，等到 gather 完成就查不到了，那些 future 會永遠 pending
        inflight = list(self._inflight)
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = []
        self._fail_pending_jobs("pool closed", inflight)

    def _fail_pending_jobs(self, message, extra_jobs=()):
        """把佇列中與 extra_jobs（進行中）尚未完成的 future 全部設上例外。

        只在 close() 之後呼叫：worker 已全部結束，不會再有人對這些 future
        set_result／set_exception，因此不會覆寫既有結果（已 done 者一律跳過）。
        """
        jobs = []
        while True:
            try:
                jobs.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        jobs.extend(extra_jobs)
        for job in jobs:
            fut = getattr(job, "fut", None)
            if fut is None or fut.done():
                continue
            try:
                fut.set_exception(RuntimeError(message))
            except Exception:
                pass

    async def _fail_all_queued(self, message):
        while True:
            try:
                job = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if not job.fut.done():
                job.fut.set_exception(RuntimeError(message))

    async def _worker_loop(self, ws):
        cfg = ws["cfg"]
        while not self._closed:
            # 依當下可用 API 數重算門檻：某一條 API 在執行途中被永久停用時，
            # 僅剩的那一條要能立刻獲得放寬（約 1 秒內生效）
            self._relax_single_api_threshold()
            # 迴圈頂端先清佇列：不能只靠「某個 job 剛跑完」時才清理，
            # 否則 worker 卡在冷卻分支時，佇列中的 job 永遠不會被設例外
            if self._queue.qsize() > 0 and self._all_disabled():
                await self._fail_all_queued("all APIs permanently disabled")
            if cfg.is_cooling_down or cfg.is_permanently_disabled:
                await asyncio.sleep(1)
                continue
            try:
                job = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.2)
                continue
            # 取到 job 之後再確認一次：另一個 worker 可能剛觸發 429 讓同一個 cfg
            # 進入冷卻，此時必須把 job 放回佇列，否則冷卻形同無效
            # （retry() 會設 job.retried=True，下面真正要執行時才設回 False）
            # 註：目前上方檢查與這裡之間沒有 await 點，因此兩者結論必然相同、
            # 這裡等價於重複檢查；保留是為了讓未來在中間加入 await（例如新的
            # 前置處理）時，冷卻語意不會悄悄失效。
            if cfg.is_cooling_down or cfg.is_permanently_disabled:
                self.retry(job)
                await asyncio.sleep(1)
                continue
            job.retried = False
            self._inflight.add(job)
            requeue = False
            try:
                async with ws["sem"]:
                    async with ws["rate_lock"]:
                        now = time.monotonic()
                        gap = REQUEST_INTERVAL - (now - ws["last_request"])
                        if gap > 0:
                            await asyncio.sleep(gap)
                        ws["last_request"] = time.monotonic()
                    # 真正送出請求前再確認一次：等待 semaphore（parallel_limit 滿載時
                    # 可能等很久）與 rate_lock 的期間，別的 worker 可能已把這個 cfg
                    # 打成冷卻。只在「取得 job 之後」檢查仍會漏掉這段等待期，
                    # 放行等於冷卻失效、繼續打被限流的端點。
                    if cfg.is_cooling_down or cfg.is_permanently_disabled:
                        # 只做記號，不在此重排：這裡若直接 retry + continue，會穿過
                        # 下面的 finally 把 _inflight 登記刪掉；而重排後的 job 可能
                        # 已被別的 worker 取走並登記，這一刪就刪掉別人的登記，
                        # 讓該 job 執行中卻不在 _inflight 內 → close() 找不到它，
                        # 它的 future 永遠 pending（呼叫端的 gather 永久卡住）。
                        requeue = True
                    else:
                        ws["active_count"] += 1
                        try:
                            result = await job.func(ws, job)
                        except Exception as e:
                            if not job.retried:
                                job.fut.set_exception(e)
                        else:
                            if not job.retried:
                                job.fut.set_result(result)
                        finally:
                            ws["active_count"] -= 1
            finally:
                self._inflight.discard(job)
            if requeue:
                # 先移除登記（finally）再重排，兩者之間沒有任何 await 點，
                # 單執行緒事件迴圈不可能插入別的 worker，因此不會誤刪他人登記
                self.retry(job)
                await asyncio.sleep(1)
                continue
            if self._queue.qsize() > 0 and self._all_disabled():
                await self._fail_all_queued("all APIs permanently disabled")
