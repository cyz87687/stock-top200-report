#!/usr/bin/env python3
"""prescreen.py —— 每日第一步：初筛（降低母体数量，形成初筛池再去跑分）。

老大 2026-10-26 指令的五条硬约束：
  1. 成交额 ≥ 1 亿元
  2. 总市值 ≥ 25 亿元
  3. 近 20 日换手率均值 ≥ 1.5%
  4. 必须具备完整申万二级行业标签（便于行业归类与行业内小市值筛选）
  5. 剔除 ST、新股（上市 < 3 个月）、停牌

## 设计原则
- **只做筛选，不做打分**。打分在 step2；本模块输出「初筛池」交给分层抽样。
- **每条剔除全部计数留痕**（dropped_* + reasons），禁静默。
- **PIT 安全**：K 线按 as_of 截断（复用 step2 已修的前视判据），
  停牌/新股的判据都只用 ≤ as_of 的 bar。
- K 线**一次抓取解决三个问题**：20日换手率、上市日期（判新股）、停牌。
  零额外接口成本。

## ⚠️ 与既有口径的关系（重要）
本模块取代 fetch_top200_sina.apply_pool_rules() 中的筛选段，但**保留其分层抽样**。
两者不可同时生效（否则重复剔除、计数不可对账）。开关在 config.pool_rules.prescreen。

## 已知边界
- 流通股本用「当日快照 circ_mv / price」反推，20 日内若股本变动（增发/解禁）会有偏差。
  实测池内 20 日换手率中位 2.91%，该口径与东财换手率同量级，可接受。
- sw_map 覆盖 5218/5577，**北交所全部无申万标签**（申万不覆盖北交所）。
  故第 4 条会把北交所整体剔除 —— 与老大 2026-10-25「不剔北交所」指令冲突。
  config 提供 `sw_require` 开关（strict / tag_only / off）由老大裁决。
"""
import json
import os
import statistics
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)

HERE = os.path.dirname(os.path.abspath(__file__))
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
SW_MAP_PATH = os.path.join(HERE, "sector", "sw_map.json")

# 成交量双口径：唯一来源是 kline_units.py（见该模块 docstring 事故史）。
#   本文件只做一层薄封装，禁止在此另写 `*100` 之类的本地口径。
try:
    sys.path.insert(0, HERE)
    from kline_units import vol_to_shares as _ku_vol_to_shares
except Exception:                                   # 兜底：与 kline_units 同口径
    def _ku_vol_to_shares(code, vol):
        return float(vol) * (1.0 if str(code).startswith("688") else 100.0)


def _vol_to_shares(sym, vol):
    """腾讯/新浪 volume → 股。sym 可带 sh/sz 前缀，按 6 位码判板块。"""
    return _ku_vol_to_shares(_c6(sym), vol)

# 新浪日线（腾讯 ifzq 三域名 2026-10 起持续 501，新浪是唯一可用源）
SINA_KL = ("https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
           "?symbol=%s&scale=240&ma=no&datalen=%d")

DEFAULTS = {
    "enabled": True,
    "min_amount": 1.0e8,          # ① 当日成交额 ≥ 1 亿
    "min_total_mv": 25e8,         # ② 总市值 ≥ 25 亿
    "min_turnover20": 1.5,        # ③ 近 20 日换手率均值 ≥ 1.5%（%）
    "turnover_days": 20,
    "sw_require": "strict",       # ④ strict=必须具备申万二级 / tag_only=只打标签不剔 / off
    "exclude_st": True,           # ⑤
    "exclude_new": True,
    "new_months": 3,
    "exclude_suspended": True,
    "price_floor": 2.0,           # 面值退市规避（既有口径，保留）
    "kl_datalen": 70,             # 70 根日线：够算 20 日换手 + 判 3 个月新股
    "workers": 8,
}


def _c6(code):
    s = str(code or "").strip().lower()
    for p in ("sh", "sz", "bj"):
        if s.startswith(p):
            return s[2:]
    return s


def _fnum(v, d=0.0):
    try:
        f = float(v)
    except Exception:
        return d
    if f != f or f in (float("inf"), float("-inf")):
        return d
    return f


def load_sw_map():
    """→ (l1: {code6:一级}, l2: {code6:二级})，缺失返回 ({}, {})。"""
    if not os.path.exists(SW_MAP_PATH):
        return {}, {}
    try:
        d = json.load(open(SW_MAP_PATH, encoding="utf-8"))
        return d.get("l1") or {}, d.get("l2") or {}
    except Exception:
        return {}, {}


def _fetch_kl_sina(sym, datalen=70, timeout=12, retry=2):
    """新浪日线 → [{date, close, vol}]（升序）或 []。"""
    url = SINA_KL % (sym, datalen)
    for a in range(retry + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                       "Referer": "https://finance.sina.com.cn/"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                txt = r.read().decode("utf-8", "ignore")
            d = json.loads(txt)
            if not isinstance(d, list):
                return []
            out = []
            for k in d:
                try:
                    out.append({"date": str(k.get("day"))[:10],
                                "close": float(k.get("close")),
                                "vol": float(k.get("volume") or 0)})
                except Exception:
                    continue
            return out
        except Exception:
            if a < retry:
                time.sleep(0.8 * (a + 1))
    return []


TX_KL = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
         "?param=%s,day,,,%d,qfq")


def _fetch_kl_tx(sym, datalen=70, timeout=12, retry=2):
    """腾讯 fqkline → [{date, close, vol}]（升序）或 []。

    ==为什么必须加这个备源（2026-11-04 实测事故）==
    新浪日线当日更新显著滞后（15:19 实测：新浪 sh000001 仍为 11-03，
    腾讯已为 11-04）。而 prescreen 的停牌判据是
    `last_bar_date < as_of ⇒ suspended`，as_of 取 datetime.now()。
    于是「K 线源没今日 bar」被解释成「全市场停牌」→ 5378 只里 3005 只
    全被判 suspended → 初筛池归零 → 选池退化。这是**判据用错源**，
    不是阈值问题，绝不能用「放宽停牌判据」来绕过。

    vol 口径（**实测确认，勿凭记忆改**）：
      腾讯 qfqday 每根为 [date, open, close, high, low, volume]，volume 存在
      **双口径**，与项目 klines_long.json 完全一致：

      | 板块 | 前缀 | 腾讯 volume 单位 | 转「股」 |
      |---|---|---|---|
      | 科创板 | 688 | **股** | ×1 |
      | 其他   | 00/30/60 | **手** | ×100 |

      实测（同日期 sina/腾讯 比值，2026-11-02 与 11-03 两日一致）：
        000001 → 100.000 / 100.000 ；600519 → 100.000 / 99.999
        300308 → 100.000 / 100.000 ；688825 → **1.000 / 1.000** ；688256 → **1.000 / 1.000**

      ⇒ 必须走 `kline_units.vol_to_shares()` 这个项目唯一口径入口。
        **绝不能统一乘 100**——那会把科创板换手率虚增 100 倍，而
        turnover20 正是本函数下游的硬门槛（min_turnover20=1.5%），
        一旦虚增，科创板会被无差别全部放行。
    """
    url = TX_KL % (sym, datalen)
    for a in range(retry + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                       "Referer": "https://gu.qq.com/"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                j = json.loads(r.read().decode("utf-8", "ignore"))
            d = (j.get("data") or {}).get(sym) or {}
            kl = d.get("qfqday") or d.get("day") or []
            out = []
            for k in kl:
                try:
                    out.append({"date": str(k[0])[:10],
                                "close": float(k[2]),
                                "vol": _vol_to_shares(sym, k[5])})
                except Exception:
                    continue
            out.sort(key=lambda x: x["date"])
            return out
        except Exception:
            if a < retry:
                time.sleep(0.8 * (a + 1))
    return []


def fetch_kl(sym, datalen=70, timeout=12, retry=2):
    """K 线多源取优 → [{date, close, vol}]（升序）或 []。

    取「两个源里最新 bar 更晚」的那份；并列优先新浪（历史更长、字段更稳）。
    单源失败自动降级到另一源。
    """
    a = _fetch_kl_sina(sym, datalen, timeout, retry)
    b = _fetch_kl_tx(sym, datalen, timeout, retry)
    if not a:
        return b
    if not b:
        return a
    return b if b[-1]["date"] > a[-1]["date"] else a


def analyze_one(row, as_of, l1, l2, cfg):
    """单只股票 → 初筛判定 + 富集字段（原地写入 row）。

    返回 (kept_bool, reason_str or None)
    reason 取值：no_price / price_floor / low_amount / small_mv / st / delist /
                 suspended / new_stock / no_sw_l2 / low_turnover20 / no_kline
    """
    code = str(row.get("code") or "")
    c6 = _c6(code)
    name = str(row.get("name") or "").upper().replace(" ", "")
    price = _fnum(row.get("price"))
    amt = _fnum(row.get("amount") or row.get("turnover"))
    tmv = _fnum(row.get("total_mv"))
    cmv = _fnum(row.get("circ_mv"))

    # —— 快照可判的三条（零接口成本，先判可省 K 线请求）——
    if price <= 0:
        return False, "no_price"
    if cfg["price_floor"] > 0 and price < cfg["price_floor"]:
        return False, "price_floor"
    if cfg["min_amount"] > 0 and amt < cfg["min_amount"]:
        return False, "low_amount"
    if cfg["min_total_mv"] > 0 and tmv < cfg["min_total_mv"]:
        return False, "small_mv"
    if cfg["exclude_st"]:
        if "ST" in name:
            return False, "st"
        if "退" in name:
            return False, "delist"

    # —— 申万标签（第 4 条）——
    lay2 = l2.get(c6)
    lay1 = l1.get(c6) or ("未知" if not lay2 else None)
    row["sw_l1"] = lay1
    row["sw_l2"] = lay2
    if cfg["sw_require"] == "strict" and not lay2:
        return False, "no_sw_l2"

    # —— K 线派生：停牌 / 新股 / 20日换手率 ——
    #   pre_only=True 时跳过（预筛阶段还没抓 K 线，否则会全判 no_kline）
    if cfg.get("pre_only"):
        return True, None
    kl = row.get("_kl") or []
    if not kl:
        return False, "no_kline"
    last = kl[-1]["date"]
    # 停牌：最后一根 bar 不是 as_of（当日无成交）
    if cfg["exclude_suspended"] and last < as_of:
        return False, "suspended"
    # 新股：上市不足 N 个月 —— 用「最早 bar 是否晚于 as_of - N 月」判定。
    #   ⚠️ 不用 K 线根数判（datalen 截断会误判）；用日期。
    if cfg["exclude_new"]:
        try:
            d0 = datetime.strptime(as_of, "%Y-%m-%d")
            first = datetime.strptime(kl[0]["date"], "%Y-%m-%d")
            m = int(cfg["new_months"])
            cut = d0 - timedelta(days=int(m * 30.44))
            if first > cut:
                row["list_date"] = kl[0]["date"]
                return False, "new_stock"
        except Exception:
            pass
    # 20 日换手率均值 = mean(最近 N 日成交量) / 流通股本
    nd = int(cfg["turnover_days"])
    vols = [k["vol"] for k in kl[-nd:] if k["vol"] > 0]
    tr20 = None
    if vols and cmv > 0 and price > 0:
        shares = cmv / price                      # 流通股本（股）
        if shares > 0:
            tr20 = round(statistics.mean(vols) / shares * 100.0, 3)
    row["turnover20"] = tr20
    if tr20 is None:
        row["turnover20_n"] = 0
        return False, "low_turnover20"
    row["turnover20_n"] = len(vols)
    if tr20 < float(cfg["min_turnover20"]):
        return False, "low_turnover20"
    return True, None


def run(rows, as_of, cfg=None, verbose=True):
    """对全A母体做初筛。

    rows: [{code,name,price,amount,total_mv,circ_mv,...}]（原地富集）
    as_of: "YYYY-MM-DD"（PIT 基准日）
    返回 (kept_rows, stats)
    """
    cfg = dict(DEFAULTS, **(cfg or {}))
    stats = {"enabled": bool(cfg.get("enabled", True)),
             "as_of": as_of,
             "in": len(rows),
             "rules": {k: cfg[k] for k in
                       ("min_amount", "min_total_mv", "min_turnover20", "turnover_days",
                        "sw_require", "exclude_st", "exclude_new", "new_months",
                        "exclude_suspended", "price_floor")},
             "dropped": {}, "out": 0, "kl_ok": 0, "kl_fail": 0}
    # ⚠️ 披露/归因变量必须在**分支外**先初始化（项目血泪两次同型事故：
    #    _neutral_banner / _ev_gap）。codes 在下方 K 线段才赋值，
    #    而归因守卫在函数末尾引用它 —— 若不在此预置，任何提前 return 路径都会 NameError。
    codes = []
    if not stats["enabled"]:
        stats["out"] = len(rows)
        return list(rows), stats

    l1, l2 = load_sw_map()
    if not l1 or not l2:
        print("   ⚠️ sw_map.json 缺失/为空 → 第4条(申万二级标签)降级为 tag_only")
        cfg["sw_require"] = "tag_only"

    # 先用快照条件筛一遍，只有通过的才需要抓 K 线（省 60%+ 请求）
    pre, pre_drop = [], {}
    _pre_cfg = dict(cfg, pre_only=True)
    for r in rows:
        ok, why = analyze_one(r, as_of, l1, l2, _pre_cfg)
        r.pop("sw_l1", None); r.pop("sw_l2", None); r.pop("turnover20", None)
        if ok:
            pre.append(r)
        else:
            pre_drop[why] = pre_drop.get(why, 0) + 1
    if verbose:
        print("   ▸ 快照条件（价格/成交额/总市值/ST）: %d → %d 只进入 K 线派生检查"
              % (len(rows), len(pre)))
        for k, v in sorted(pre_drop.items(), key=lambda x: -x[1]):
            print("      - %-14s %d" % (k, v))

    # 并发抓 K 线
    codes = []
    for r in pre:
        c = str(r.get("code") or "")
        mk = c[:2] if c[:2] in ("sh", "sz", "bj") else ("sh" if _c6(c).startswith("6") else "sz")
        codes.append((r, "%s%s" % (mk, _c6(c))))

    wk = max(1, int(cfg.get("workers") or 4))
    t0 = time.time()
    got = 0
    with ThreadPoolExecutor(max_workers=wk) as ex:
        futs = {ex.submit(fetch_kl, s, int(cfg["kl_datalen"])): r for r, s in codes}
        for i, fu in enumerate(as_completed(futs), 1):
            r = futs[fu]
            try:
                kl = fu.result() or []
            except Exception:
                kl = []
            # PIT 截断：只保留 date ≤ as_of（防前视，与 step2 同一判据）
            r["_kl"] = [k for k in kl if k["date"] <= as_of]
            if r["_kl"]:
                got += 1
            if verbose and (i % 200 == 0 or i == len(codes)):
                el = time.time() - t0
                print("      K线 [%d/%d] 成功 %d  %.0fs (%.1f 只/s)"
                      % (i, len(codes), got, el, i / max(el, 1e-9)))

    stats["kl_ok"] = got
    stats["kl_fail"] = len(pre) - got

    # 完整判定
    kept, drop = [], dict(pre_drop)
    for r in pre:
        ok, why = analyze_one(r, as_of, l1, l2, cfg)
        if ok:
            kept.append(r)
        else:
            drop[why] = drop.get(why, 0) + 1
    for r in pre:
        r.pop("_kl", None)

    stats["dropped"] = drop
    stats["out"] = len(kept)
    stats["kept_rate"] = round(100.0 * len(kept) / max(1, len(rows)), 2)
    if verbose:
        print("   ▸ 全部五条生效: %d → %d 只（初筛通过率 %.1f%%）"
              % (len(rows), len(kept), stats["kept_rate"]))
        for k, v in sorted(drop.items(), key=lambda x: -x[1]):
            print("      - 剔除 %-16s %5d 只" % (k, v))

    # ===== 归因守卫：区分「真全市场停牌」与「K 线源无当日数据」=====
    #   2026-11-04 事故：新浪当日无 bar → last<as_of → 全判 suspended
    #   → 初筛池归零。判据本身正确，但**失败原因被误报成阈值问题**，
    #   会把人引向「放宽 min_turnover20」这种错误处置。此处显式归因。
    _n_sus = drop.get("suspended", 0)
    if rows and _n_sus >= 0.8 * len(rows):
        _src_max = _kl_source_max_date(codes) if codes else None
        stats["suspect_stale_kline"] = True
        stats["kl_source_max_date"] = _src_max
        if verbose:
            print(f"   🚨 suspended 占比 {_n_sus}/{len(rows)} ≥80% —— **不是停牌潮**")
            print(f"      K线源最新 bar={_src_max}，as_of={as_of}")
            if _src_max and _src_max < as_of:
                print("      ⇒ 数据源当日 bar 尚未生成（非交易日/接口延迟）")
                print("      ⇒ 禁止放宽 prescreen 阈值来绕过，应先确认当日是否交易日")
            else:
                print("      ⇒ 源有当日 bar 却仍判停牌，需检查 as_of 与 bar 日期格式")
    return kept, stats


def _kl_source_max_date(codes, sample=8):
    """抽样探 K 线源的「全局最新日期」，用于归因（不参与判定）。"""
    mx = None
    for _, s in codes[:sample]:
        try:
            kl = fetch_kl(s, 5)
        except Exception:
            continue
        if kl and kl[-1]["date"]:
            mx = kl[-1]["date"] if mx is None else max(mx, kl[-1]["date"])
    return mx


REASON_CN = {
    "no_price": "无价格", "price_floor": "价格<2元", "low_amount": "成交额<1亿",
    "small_mv": "总市值<25亿", "st": "ST", "delist": "退市整理",
    "suspended": "停牌", "new_stock": "新股(<3月)", "no_sw_l2": "缺申万二级",
    "low_turnover20": "20日换手<1.5%", "no_kline": "无K线",
}


def summary_lines(stats):
    """→ 中文摘要行列表（供 fetch/报告层打印，禁静默）。"""
    out = []
    if not stats.get("enabled"):
        out.append("初筛：未启用")
        return out
    r = stats.get("rules") or {}
    out.append("初筛（as_of=%s，母体 %d 只 → 初筛池 %d 只，通过率 %.1f%%）"
               % (stats.get("as_of"), stats.get("in"), stats.get("out"),
                  stats.get("kept_rate", 0)))
    out.append("  规则：成交额≥%.1f亿 / 总市值≥%.0f亿 / %d日换手中位≥%.1f%% / "
               "申万二级=%s / 剔ST+新股<%d月+停牌"
               % (r.get("min_amount", 0) / 1e8, r.get("min_total_mv", 0) / 1e8,
                  r.get("turnover_days", 20), r.get("min_turnover20", 0),
                  r.get("sw_require"), r.get("new_months", 3)))
    drop = stats.get("dropped") or {}
    if drop:
        seg = " / ".join("%s %d" % (REASON_CN.get(k, k), v)
                         for k, v in sorted(drop.items(), key=lambda x: -x[1]))
        out.append("  剔除明细：" + seg)
    return out


def main():
    """CLI：python prescreen.py <universe.json> <out.json> [as_of]"""
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("universe", nargs="?", default=os.path.join(HERE, "top500_all_a.json"))
    ap.add_argument("out", nargs="?", default="")
    ap.add_argument("as_of", nargs="?", default="")
    ap.add_argument("--workers", type=int, default=DEFAULTS["workers"])
    ap.add_argument("--no-kl-cache", action="store_true")
    a = ap.parse_args()

    d = json.load(open(a.universe, encoding="utf-8"))
    as_of = a.as_of or d.get("date") or datetime.now().strftime("%Y-%m-%d")
    # 优先用落盘的全A母体；没有则退回 main_pool（会给出显式告警）
    rows = d.get("all_universe") or d.get("main_pool") or []
    if not d.get("all_universe"):
        print("⚠️ 输入文件无 all_universe（全A母体），退回 main_pool %d 只 —— "
              "初筛应在全A母体上做，结果仅作演示" % len(rows))

    cfg = dict(DEFAULTS, workers=a.workers)
    try:
        c = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
        cfg.update((c.get("pool_rules") or {}).get("prescreen") or {})
        cfg["workers"] = a.workers
    except Exception:
        pass

    kept, st = run(rows, as_of, cfg)
    for ln in summary_lines(st):
        print(ln)

    outp = a.out or ("prescreen_pool_%s.json" % as_of)
    json.dump({"date": as_of, "stats": st, "pool": kept},
              open(outp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("✅ 初筛池已保存: %s（%d 只）" % (outp, len(kept)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
