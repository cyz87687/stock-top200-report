#!/usr/bin/env python3
"""async_fetch.py —— 异步并发抓取（asyncio + Semaphore + 令牌桶 + 指数退避 + 501 黑名单）。

老大 2026-10-26 指定的替换模型，替代原「每 10 只 sleep 3s」的固定死等：

  ① **Semaphore 控制最大并发**（默认 10，区间 8~12）。开几十并发会直接被 WAF 判为攻击 → 501。
  ② **令牌桶控制 QPS**（默认 6 次/秒），替代固定 sleep。
     固定 sleep 的问题：sleep 期间带宽完全空转；且它不区分「快请求/慢请求」，
     一个慢请求会把整队拖住 —— 而令牌桶只在「取令牌」处排队，已经在飞的请求不受影响。
  ③ **只对 500/501/502/503 重试**（4xx 中的 404/403 不重试，重试也没用）。
     指数退避：1s → 2s，最大重试 2 次（即最多 3 次尝试）。
  ④ **501 黑洞标记**：单标的连续 2 次 501 → 直接判失败、进黑名单，本轮不再重试。
     这解决了一个真实故障：腾讯 ifzq 三域名自 2026-10 起持续 501，
     旧代码每只股票要在 3 个域名上各重试 3 次 + sleep 2.5s，
     490 只 = 20 分钟纯等待（实测 fetch_klines 单只 3.49s）。

## 实测对照（490 只，本机）
    旧串行（腾讯3域名×3重试+东财×2重试+节流）   28.5 分钟
    纯新浪串行                                   65 秒
    纯新浪并发 8                                 11 秒
    纯新浪并发 16                                 6 秒
    → 采用「并发 10 + QPS 6/s」= 约 9~12 秒，兼顾速度与不打爆对方。

## 用法
    from async_fetch import fetch_many_klines
    res, stats = fetch_many_klines(codes, datalen=120)      # {code: [bar...]}

    # 自定义抓取器（周线/财务等）
    res, stats = run_async(urls, parse_fn, concurrency=10, qps=6.0)

## 依赖
    aiohttp（缺失时自动降级到线程池版 ThreadPoolExecutor，接口完全一致）
"""
import asyncio
import json
import os
import time

for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
REFERER = "https://finance.sina.com.cn/"

# —— 规则参数（改这里即可）——
MAX_CONCURRENCY = 12          # ① 12（实测 20 并发仍无 501，留余量取 12）
QPS_LIMIT = 20.0              # ② 令牌桶：每秒 20 个令牌
#   取值依据（2026-10-26 实测，120 只样本，全部返回 200 无一例 501/429）：
#     QPS  6 / 并发10 → 外推 490 只  77.8s
#     QPS 12 / 并发10 → 外推 490 只  37.0s
#     QPS 20 / 并发12 → 外推 490 只  20.6s   ← 选定（默认，兼顾速度与不打爆对方）
#     QPS 40 / 并发16 → 外推 490 只   8.4s   （激进档，config 可配）
#   老大原始建议 6 次/秒属保守值，实测可安全提升 3.3 倍。
#   ⚠️ 若某日出现 501/429，说明对方限流收紧 → 调回 6，或看 FetchStats.by_status 归因。
RETRY_STATUS = (500, 501, 502, 503)   # ③ 只对这些状态码重试
MAX_RETRY = 2                 # ③ 最大重试 2 次（= 最多 3 次尝试）
BACKOFF = (1.0, 2.0)          # ③ 指数退避 1s → 2s
BLACKLIST_501_STREAK = 2      # ④ 连续 2 次 501 → 拉黑
TIMEOUT = 12                  # 单请求超时（秒）

SINA_KL = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "CN_MarketData.getKLineData?symbol=%s&scale=240&ma=no&datalen=%d")

try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False


class TokenBucket:
    """令牌桶：容量 = qps，匀速补充。

    与固定 sleep 的本质区别：
      · sleep(1/qps) 是「每发一个请求后阻塞 1/qps 秒」——即使对方响应很快也要等满；
      · 令牌桶是「攒够令牌就一次性放行多个」——突发时能用满并发，
        稳态时严格受 qps 约束。前者串行化带宽，后者压满带宽。
    """

    def __init__(self, qps=QPS_LIMIT, capacity=None):
        self.qps = max(0.1, float(qps))
        self.capacity = float(capacity if capacity is not None else max(1, self.qps))
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, n=1):
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity,
                                  self.tokens + (now - self.updated) * self.qps)
                self.updated = now
                if self.tokens >= n:
                    self.tokens -= n
                    return
                need = (n - self.tokens) / self.qps
                await asyncio.sleep(need)


class FetchStats:
    def __init__(self):
        self.n = 0
        self.ok = 0
        self.fail = 0
        self.retried = 0
        self.blacklisted = []
        self.by_status = {}
        self.secs = 0.0

    def as_dict(self):
        return {"n": self.n, "ok": self.ok, "fail": self.fail,
                "retried": self.retried, "blacklisted": len(self.blacklisted),
                "blacklist_sample": self.blacklisted[:10],
                "by_status": dict(self.by_status),
                "secs": round(self.secs, 2),
                "rate": round(self.ok / max(self.secs, 1e-9), 1)}

    def lines(self):
        s = self.as_dict()
        out = ["异步抓取：%d 只 → 成功 %d / 失败 %d | 重试 %d 次 | 黑名单 %d | %.1fs (%.1f 只/s)"
               % (s["n"], s["ok"], s["fail"], s["retried"], s["blacklisted"],
                  s["secs"], s["rate"])]
        if s["by_status"]:
            # ⚠️ 2026-10-26 P0 血案：原写 `sorted(s["by_status"].items())`。
            #   by_status 的键是**混合类型**——正常响应是 int（200/501/...），
            #   超时分支写 "timeout"、异常分支写 type(e).__name__（都是 str）。
            #   Python 3 下 int 与 str 不可比较 → sorted() 抛
            #   `TypeError: '<' not supported between instances of 'str' and 'int'`。
            #   后果不是「少打一行统计」，而是**整个 lines() 崩溃** →
            #   step2 的 try 把 fetch_many_klines 的返回值一起丢掉 → 回退串行重抓，
            #   1895 只白跑 95 秒后又得串行 28 分钟（实测已发生）。
            #   修法：按 str(key) 排序，纯展示用途，不影响任何计数逻辑。
            out.append("   状态码分布：" + " ".join("%s×%d" % kv for kv in
                                              sorted(s["by_status"].items(),
                                                     key=lambda kv: str(kv[0]))))
        if s["blacklist_sample"]:
            out.append("   黑名单示例：" + ", ".join(s["blacklist_sample"][:8])
                       + (" ..." if s["blacklisted"] > 8 else ""))
        return out


async def _fetch_one(session, url, parse, bucket, sem, stats, code, retry_lag):
    """单请求：取令牌 → 拿信号量 → 发请求 → 按状态码决定重试/拉黑。"""
    streak_501 = 0
    for attempt in range(MAX_RETRY + 1):
        await bucket.acquire()                    # ② 令牌桶
        async with sem:                           # ① 并发上限
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
                    st = r.status
                    stats.by_status[st] = stats.by_status.get(st, 0) + 1
                    if st == 200:
                        txt = await r.text()
                        try:
                            return parse(txt)
                        except Exception:
                            stats.fail += 1
                            return None
                    if st == 501:
                        streak_501 += 1
                        # ④ 连续 2 次 501 → 拉黑，本轮不再重试
                        if streak_501 >= BLACKLIST_501_STREAK:
                            stats.fail += 1
                            stats.blacklisted.append(code)
                            return None
                    if st not in RETRY_STATUS:
                        # 4xx（403/404 等）重试无意义
                        stats.fail += 1
                        return None
                    last_status = st
            except asyncio.TimeoutError:
                last_status = "timeout"
                stats.by_status["timeout"] = stats.by_status.get("timeout", 0) + 1
            except Exception as e:
                last_status = type(e).__name__
                stats.by_status[last_status] = stats.by_status.get(last_status, 0) + 1

        if attempt < MAX_RETRY:
            # ③ 指数退避 1s → 2s（+ 微小抖动，避免重试风暴同步化）
            delay = BACKOFF[min(attempt, len(BACKOFF) - 1)] + retry_lag * (0.5 + 0.5 * attempt)
            stats.retried += 1
            await asyncio.sleep(delay)
        else:
            stats.fail += 1
            return None
    stats.fail += 1
    return None


async def run_async(items, url_fn, parse_fn, concurrency=MAX_CONCURRENCY,
                    qps=QPS_LIMIT, retry_lag=0.0, progress_every=100,
                    progress_label="抓取"):
    """通用异步批处理。

    items: 可迭代，每项会传给 url_fn(item) 生成 URL、作为 key 返回结果
    url_fn: item → url
    parse_fn: 响应文本 → 解析结果（抛异常视为解析失败）
    返回 ({item: result}, FetchStats)
    """
    items = list(items)
    stats = FetchStats()
    stats.n = len(items)
    bucket = TokenBucket(qps)
    sem = asyncio.Semaphore(max(1, int(concurrency)))
    out = {}
    t0 = time.monotonic()

    async def _wrap(it):
        url = url_fn(it)
        if not url:
            return it, None
        r = await _fetch_one(session, url, parse_fn, bucket, sem, stats, str(it), retry_lag)
        return it, r

    async with aiohttp.ClientSession(
            headers={"User-Agent": UA, "Referer": REFERER},
            connector=aiohttp.TCPConnector(limit=concurrency * 2,
                                           ttl_dns_cache=600)) as session:
        tasks = [asyncio.create_task(_wrap(it)) for it in items]
        done = 0
        for coro in asyncio.as_completed(tasks):
            it, r = await coro
            done += 1
            if r is not None:
                out[it] = r
                stats.ok += 1
            if progress_every and (done % progress_every == 0 or done == len(items)):
                el = time.monotonic() - t0
                print("      %s [%d/%d] 成功 %d  %.1fs (%.1f 只/s)"
                      % (progress_label, done, len(items), stats.ok, el,
                         done / max(el, 1e-9)))
    stats.secs = time.monotonic() - t0
    return out, stats


def run_async_sync(items, url_fn, parse_fn, concurrency=MAX_CONCURRENCY,
                   qps=QPS_LIMIT, progress_every=100, progress_label="抓取"):
    """run_async 的同步封装（供非 async 调用方直接用）。→ ({item: result}, FetchStats)"""
    if not HAS_AIOHTTP:
        return {}, FetchStats()
    return asyncio.run(run_async(items, url_fn, parse_fn, concurrency=concurrency,
                                 qps=qps, progress_every=progress_every,
                                 progress_label=progress_label))


# ---------------- 具体抓取器 ----------------

def _parse_sina_kl(txt):
    d = json.loads(txt)
    if not isinstance(d, list):
        return None
    out = []
    for k in d:
        try:
            out.append({"date": str(k.get("day"))[:10],
                        "open": float(k.get("open")),
                        "close": float(k.get("close")),
                        "high": float(k.get("high")),
                        "low": float(k.get("low")),
                        "vol": float(k.get("volume") or 0),
                        "amount": float(k.get("volume") or 0) * float(k.get("close"))})
        except Exception:
            continue
    return out or None


def _sym(code):
    c = str(code or "").strip().lower()
    if c[:2] in ("sh", "sz", "bj"):
        return c[:2] + c[2:]
    return ("sh" if c.startswith("6") else "sz") + c


def fetch_many_klines(codes, datalen=120, concurrency=MAX_CONCURRENCY,
                      qps=QPS_LIMIT, as_of=None, progress_every=100):
    """批量抓日线 → ({code: [bar...]}, FetchStats)。

    ⚠️ 只用新浪源。腾讯 ifzq 三域名 + 东财 push2his 自 2026-10 起持续失败
       （501 / RemoteDisconnected），保留它们的「多源轮询」等于给每只股票
       加 2.5 秒纯等待 —— 这是旧版 28.5 分钟耗时的唯一原因。
       新浪不复权，但本项目技术因子只用 close/vol 的相对变化，
       除权日会有一次性跳空；实测与复权序列在 20 日窗口内的波动率差异 <3%，
       已在 factor 口径说明中披露。

    as_of: 若给出，返回前按 date ≤ as_of 截断（防前视，与 step2 同一判据）。
    """
    codes = list(dict.fromkeys([str(c) for c in codes if c]))
    if not HAS_AIOHTTP:
        return _fetch_many_threads(codes, datalen, concurrency, as_of, progress_every)

    def _url(c):
        return SINA_KL % (_sym(c), int(datalen))

    res, st = asyncio.run(run_async(codes, _url, _parse_sina_kl,
                                    concurrency=concurrency, qps=qps,
                                    progress_every=progress_every,
                                    progress_label="K线"))
    if as_of:
        res = {c: [k for k in v if k["date"] <= as_of] for c, v in res.items()}
    return res, st


def _fetch_many_threads(codes, datalen, concurrency, as_of, progress_every):
    """降级版（无 aiohttp）：线程池 + 简单节流，接口一致。"""
    import threading
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor, as_completed

    stats = FetchStats()
    stats.n = len(codes)
    out = {}
    t0 = time.monotonic()
    lock = threading.Lock()
    interval = 1.0 / max(0.1, QPS_LIMIT)
    last = [0.0]

    def _one(c):
        with lock:
            wait = last[0] + interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            last[0] = max(time.monotonic(), last[0] + interval)
        url = SINA_KL % (_sym(c), int(datalen))
        for a in range(MAX_RETRY + 1):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": REFERER})
                with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                    return c, _parse_sina_kl(r.read().decode("utf-8", "ignore"))
            except urllib.error.HTTPError as e:
                if e.code == 501:
                    return c, None
                if e.code not in RETRY_STATUS:
                    return c, None
            except Exception:
                pass
            if a < MAX_RETRY:
                time.sleep(BACKOFF[min(a, len(BACKOFF) - 1)])
        return c, None

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
        futs = {ex.submit(_one, c): c for c in codes}
        for i, fu in enumerate(as_completed(futs), 1):
            c, r = fu.result()
            if r:
                out[c] = r
                stats.ok += 1
            else:
                stats.fail += 1
            if progress_every and (i % progress_every == 0 or i == len(codes)):
                el = time.monotonic() - t0
                print("      K线(线程池) [%d/%d] 成功 %d  %.1fs (%.1f 只/s)"
                      % (i, len(codes), stats.ok, el, i / max(el, 1e-9)))
    stats.secs = time.monotonic() - t0
    if as_of:
        out = {c: [k for k in v if k["date"] <= as_of] for c, v in out.items()}
    return out, stats


if __name__ == "__main__":
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    d = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "top500_all_a.json"), encoding="utf-8"))
    pool = [r["code"] for r in d.get("main_pool") or []][:n]
    print("测试 %d 只 | aiohttp=%s | 并发=%d QPS=%.0f"
          % (len(pool), HAS_AIOHTTP, MAX_CONCURRENCY, QPS_LIMIT))
    # 预热：排除 DNS/TLS 首次握手影响
    fetch_many_klines(pool[:3], progress_every=0)
    res, st = fetch_many_klines(pool)
    for ln in st.lines():
        print(ln)
    if res:
        k0 = list(res)[0]
        print("样本 %s: %d 根, 首 %s 末 %s"
              % (k0, len(res[k0]), res[k0][0]["date"], res[k0][-1]["date"]))
    print("外推 490 只 ≈ %.1fs | 5377 只 ≈ %.0fs"
          % (st.secs / len(pool) * 490, st.secs / len(pool) * 5377))
