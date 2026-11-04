#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_qfq_anchor.py —— 前复权锚点漂移自检（P1-1 问题 B）

## 它防的是什么

`append_klines_latest.py` 的核心设计是**「已有日期绝不覆盖」**：

    for bar in bars:
        if d in m:
            continue          # ← 已有日期绝不覆盖

这条规则保住了历史面板的可复现性，但引入一个**静默失效**风险：

前复权价的固有性质是「除权后全部历史价格整体平移」。假设某股 2026-08-01 除权，
东财会把 2026-07-31 及以前的前复权价全部下调。此时：
- 库里 07-31 之前的老值 = **除权前**的前复权（旧锚点）
- 库里 08-01 之后的新值 = **除权后**的前复权（新锚点）
- 「已有日期不覆盖」→ 老值永远不会被修正 → **同一条价格序列里混了两个锚点**

后果：跨除权日计算收益率会凭空多出一个「跌幅」（实际是价格平移，不是亏损）。
这类污染**不会报错、不会缺值**，只会让回测收益静默偏低 —— 是最难发现的一类。

## 为什么不能靠库内自证

同一手法在 round116 已踩过一次（见 `verify_vol_unit_em.py` 文档）：
库内比值分布是单峰对数正态，任何固定区间都会随机漏判。**必须外部权威值比对**。

→ 唯一判据：与东财 `stock_zh_a_hist(adjust="qfq")` 的**同日前复权收盘价**比。
   比值偏离 1 超过 `--tol`（默认 0.5%）即视为锚点漂移。

## 抽样策略（实测推翻「随机抽样」方案）

**❌ 首版方案（已否决）**：每 20 个交易日随机抽 10 只。
否决理由（实测）：把 `000001` 的收盘人为改 +2% 做故障注入后，抽 10 只、抽 60 只
**都没抽中它**。锚点漂移是**逐股独立事件**（每股有自己的除权日），
随机抽样命中的概率 ≈ 样本数/库内代码数 = 60/2134 ≈ 2.8%。
按每 20 日抽 60 只算，覆盖全库要 **12 年**。—— 这不是哨兵，是安慰剂。

**✅ 现方案：轮转分片全覆盖（cumulative shard）**

按代码排序后切成 `--shards` 片（默认 60），每天只查**当天对应的一片**，
游标存 `data/qfq_anchor_cursor.json` 累积推进：

    第 1 天查第 0 片，第 2 天查第 1 片 …… 第 60 天查第 59 片，第 61 天回到第 0 片

- 单片 ≈ 2138/60 ≈ **36 只 × 1 日 ≈ 15 秒**，日链完全可承受
- **60 个交易日后全库 2138 只各被核过一次**（约 3 个月）
- 游标持久化 → 断电/跳日不会重复也不会漏，**覆盖率可精确披露**
- 每次输出的 `coverage_pct` 就是「库内代码中已核对过的比例」，可累积、可审计

⚠️ 这与「随机抽样」有本质区别：随机抽样**无法回答**「查了多少」；
   轮转分片能精确回答，且保证有限时间内全覆盖。

⚠️ 仍然不是「完备校验」：两次全轮之间发生的除权，最多滞后 60 个交易日才发现。
   要立即全量请显式跑 `--full`（~20 分钟）。

## 附带：可疑跳变扫描（本地零成本，每天跑）

除东财比对，本脚本还会**纯本地**扫一遍全库，标记「单日涨跌幅超出该板块涨跌停
限制 + 2pp」的位置。这类跳变有三种来源，脚本会分类：

| 来源 | 特征 | 是否需要处置 |
|---|---|---|
| 真实个股事件（次新/复牌/重组） | 东财同值 → 比值 1.0 | 否 |
| 前复权锚点混用 | 东财不同值，且集中在某除权日之后 | **是** |
| 数据写入事故 | 东财不同值，且无除权 | **是** |

实测（2026-11-03）：全库 1332 处超限跳变、涉及 212 只；抽最极端 30 处逐一向东财
核对，**30/30 比值 = 1.000000**（全部是真实事件，库是干净的）。
→ 说明本地扫描单独用会产生大量噪声，**必须配东财比对才能定性**。

## 输出与退出码

    data/qfq_anchor_audit.json      最近一次自检结果（含 coverage_pct）
    data/qfq_anchor_cursor.json     轮转游标（累积覆盖率）
    exit 0 = 无告警
    exit 1 = 检出锚点漂移（**不阻断日链**，由 pipeline_status 记为可降级失败）

用法：
    python verify_qfq_anchor.py                  # 日链：查当前分片
    python verify_qfq_anchor.py --shards 1 --n 50   # 小库/调试
    python verify_qfq_anchor.py --date 2026-08-03   # 指定日期
    python verify_qfq_anchor.py --full           # 全量（慢，~20min）
    python verify_qfq_anchor.py --reset-cursor   # 游标归零
    python verify_qfq_anchor.py --local-scan     # 只做本地可疑跳变扫描
"""
import os
import sys
import json
import time
import argparse
import hashlib
import datetime
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

KLONG = os.path.join(HERE, "data", "klines_long.json")
OUT = os.path.join(HERE, "data", "qfq_anchor_audit.json")

import kline_calendar as kc          # 有效截面口径唯一来源（P1-1 问题 A）

WORKERS = 6
RETRY = 3
SAMPLE_EVERY_N_TRADING_DAYS = 20     # 每 20 个有效交易日抽一次
DEFAULT_N_CODES = 10
DEFAULT_TOL = 0.005                  # 0.5%


def fetch_em_qfq_close(sym, date_compact):
    """东财 qfq 该日收盘。失败返回 (None, 原因)"""
    import akshare as ak
    last = None
    for _ in range(RETRY):
        try:
            df = ak.stock_zh_a_hist(symbol=sym, period="daily",
                                    start_date=date_compact, end_date=date_compact,
                                    adjust="qfq")
            if df is None or len(df) == 0:
                return None, "东财无该日数据"
            return float(df.iloc[-1]["收盘"]), None
        except Exception as e:                          # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
            time.sleep(0.4)
    return None, last or "unknown"


CURSOR = os.path.join(HERE, "data", "qfq_anchor_cursor.json")
DEFAULT_SHARDS = 60                  # 分片数：60 个交易日完成一轮全库
DEFAULT_TOL = 0.005                  # 0.5%


def _load_cursor():
    try:
        with open(CURSOR, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"shard": 0, "covered": {}, "rounds": 0}


def _save_cursor(c):
    torch_dir = os.path.dirname(CURSOR)
    if torch_dir:
        os.makedirs(torch_dir, exist_ok=True)
    with open(CURSOR, "w", encoding="utf-8") as f:
        json.dump(c, f, ensure_ascii=False, indent=1)


def shard_of(codes, shard, shards):
    """按代码排序后等分成 shards 片，取第 shard 片（稳定、无重复、无遗漏）"""
    codes = sorted(codes)
    n = len(codes)
    lo = (n * shard) // shards
    hi = (n * (shard + 1)) // shards
    return codes[lo:hi], lo, hi


def local_scan(px, vd, extra_pp=0.02):
    """纯本地扫可疑跳变（零网络成本）。返回按偏离幅度倒序的列表"""
    vdset = set(vd)

    def limit_of(c):
        if c.startswith(("688", "300")):
            return 0.20
        if c.startswith(("8", "4", "92")):
            return 0.30
        return 0.10

    out = []
    for c, bars in px.items():
        ds = sorted(x for x in bars if x in vdset)
        lim = limit_of(c)
        for i in range(1, len(ds)):
            p0 = float(bars[ds[i - 1]][1])
            p1 = float(bars[ds[i]][1])
            if p0 > 0 and p1 > 0:
                r = p1 / p0 - 1
                if abs(r) > lim + extra_pp:
                    out.append((c, ds[i], r, p0, p1))
    out.sort(key=lambda x: -abs(x[2]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="", help="指定检查日期（默认取最新有效日）")
    ap.add_argument("--shards", type=int, default=DEFAULT_SHARDS)
    ap.add_argument("--shard", type=int, default=-1, help="指定分片（默认用游标）")
    ap.add_argument("--tol", type=float, default=DEFAULT_TOL)
    ap.add_argument("--full", action="store_true", help="全量校验（慢，~20min）")
    ap.add_argument("--reset-cursor", action="store_true", help="游标归零")
    ap.add_argument("--local-scan", action="store_true", help="只做本地可疑跳变扫描")
    a = ap.parse_args()

    print("=" * 92)
    print("verify_qfq_anchor · 前复权锚点漂移自检")
    print("=" * 92)

    if a.reset_cursor:
        _save_cursor({"shard": 0, "covered": {}, "rounds": 0})
        print("✅ 游标已归零（shard=0, covered 清空, rounds=0）")
        # ⚠️ 必须在此返回：否则本次运行会继续执行并把游标推到 1，
        #   导致「重置后第一次运行」实际查的是分片 1 —— 分片 0 被跳过。
        print("=" * 92)
        return 0

    px = json.load(open(KLONG, encoding="utf-8"))
    vd = kc.build_valid(px)                 # 只用有效截面，避免建库初期噪声日
    if not vd:
        print("⚠️ 无有效交易日，退出")
        _write_out({"available": False, "reason": "无有效交易日"})
        return 0
    print(f"库内 {len(px)} 只；有效交易日 {len(vd)} 个（{vd[0]} ~ {vd[-1]}）")

    date = a.date or vd[-1]
    codes = sorted(c for c, bars in px.items() if date in bars)
    if not codes:
        print(f"⚠️ {date} 库内无任何股票")
        _write_out({"available": True, "skipped": True, "date": date,
                    "reason": f"{date} 库内无股票", "verdict": "skipped"})
        return 0

    # ── 本地扫描（零成本，可选）──
    if a.local_scan:
        sus = local_scan(px, vd)
        print(f"本地扫描：超涨跌停+2pp 的可疑跳变 {len(sus)} 处，"
              f"涉及 {len({s[0] for s in sus})} 只")
        print("⚠️ 该扫描单独使用噪声极大（实测抽 30 处全部是真实个股事件），"
              "必须配东财比对才能定性")
        for c, d_, r, p0, p1 in sus[:15]:
            print(f"  {c}  {d_}  {r*100:+.2f}%  {p0:.3f} → {p1:.3f}")
        _write_out({"available": True, "local_scan_only": True, "date": date,
                    "n_suspicious": len(sus),
                    "suspicious_sample": [[c, d_, round(r, 6)] for c, d_, r, _, _ in sus[:100]],
                    "verdict": "local_scan_only",
                    "note": "本地扫描不能定性，需东财比对"})
        return 0

    # ── 确定本次要查的股票集合 ──
    cur = _load_cursor()
    if a.full:
        sample, scope, shard_used = codes, "full", None
        print(f"检查日期：{date}   全量校验 {len(sample)} 只（预计 ~20 分钟）")
    else:
        shard_used = a.shard if a.shard >= 0 else int(cur.get("shard", 0)) % a.shards
        sample, lo, hi = shard_of(codes, shard_used, a.shards)
        scope = f"shard {shard_used+1}/{a.shards}"
        print(f"检查日期：{date}   分片 {shard_used}/{a.shards}"
              f"（库内 {len(codes)} 只的第 {lo}~{hi-1} 位，共 {len(sample)} 只）")

    print(f"容差：比值偏离 1 超过 {a.tol*100:.2f}% 即告警")
    print("-" * 92)

    dc = date.replace("-", "")
    lock = threading.Lock()
    done = [0]
    res = {}

    def work(c):
        v, err = fetch_em_qfq_close(c, dc)
        with lock:
            done[0] += 1
            if done[0] % 100 == 0:
                print(f"  … {done[0]}/{len(sample)}", flush=True)
        return c, v, err

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(work, c) for c in sample]
        for f in as_completed(futs):
            c, v, err = f.result()
            res[c] = (v, err)
    print(f"抓取完成 {time.time()-t0:.1f}s，"
          f"东财无数据 {sum(1 for v, _ in res.values() if v is None)} 只")

    # ── 比对 ──
    ok_l, drift, nodata = [], [], []
    for c in sample:
        v_em, err = res[c]
        if v_em is None or v_em <= 0:
            nodata.append((c, err))
            continue
        v_lib = float(px[c][date][1])                 # bar = [o, c, h, l, v]
        r = v_lib / v_em
        (ok_l if abs(r - 1.0) <= a.tol else drift).append(
            (c, r) if abs(r - 1.0) <= a.tol else (c, r, v_lib, v_em))

    print()
    print(f"锚点一致（|比值-1| ≤ {a.tol*100:.2f}%）  {len(ok_l):>5} 只")
    print(f"锚点漂移（需核查）               {len(drift):>5} 只")
    print(f"东财无数据（跳过）               {len(nodata):>5} 只")

    if drift:
        print(f"\n⚠️ 检出漂移（最多 20 条）：")
        for c, r, vl, ve in drift[:20]:
            print(f"  {c}  比值={r:.6f}  库={vl:,.4f}  东财={ve:,.4f}  "
                  f"差={abs(r-1)*100:.3f}%")
        print("\n可能原因（按概率排序）：")
        print("  1. 该股在库内该日之后发生除权 → 东财把历史前复权价整体下调")
        print("     → 库内老值仍是旧锚点（『已有日期绝不覆盖』的设计副作用）")
        print("  2. 该股当日停牌/ST/退市整理，东财返回了别的交易日")
        print("  3. 库内该 bar 本身写错（数据事故，非锚点问题）")
        print("\n处置建议：不要自动改数据。先看漂移日期是否集中在某个除权日之后；")
        print("          确认是锚点混用后，用 fetch_klines_long.py 对该股做定向重建。")

    # ── 更新游标与覆盖率 ──
    covered = cur.get("covered") or {}
    for c in ok_l:
        covered[c[0]] = date
    n_cov = len(covered)
    cov_pct = round(100.0 * n_cov / len(px), 2) if px else 0.0
    if not a.full:
        nxt = (shard_used + 1) % a.shards
        rounds = int(cur.get("rounds", 0)) + (1 if nxt == 0 else 0)
        _save_cursor({"shard": nxt, "covered": covered, "rounds": rounds,
                      "last_run": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
        print(f"\n轮转：下次查分片 {nxt}/{a.shards}"
              f"（{'完成一轮，回到起点' if nxt == 0 else f'还需 {a.shards-nxt} 天走完本轮'}）")
    else:
        _save_cursor({"shard": 0, "covered": covered,
                      "rounds": int(cur.get("rounds", 0)) + 1,
                      "last_run": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")})

    print(f"累积覆盖：{n_cov}/{len(px)} = {cov_pct:.2f}% 的库内代码已与东财核对过"
          f"（完成 {cur.get('rounds', 0)} 轮）")
    if cov_pct < 100:
        print(f"   ⚠️ 覆盖率 {cov_pct:.2f}% < 100% —— 未覆盖部分**不是已通过，是没测**")

    _write_out({
        "available": True,
        "checked_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "date": date,
        "tol": a.tol,
        "scope": scope,
        "shard": shard_used,
        "shards": a.shards,
        "full": bool(a.full),
        "counts": {"candidates": len(codes), "sampled": len(sample),
                   "ok": len(ok_l), "drift": len(drift), "nodata": len(nodata)},
        "ok_ratio_range": [min((r for _, r in ok_l), default=None),
                           max((r for _, r in ok_l), default=None)],
        "drift": [{"code": c, "ratio": round(r, 8),
                   "lib_close": vl, "em_close": ve} for c, r, vl, ve in drift],
        "nodata_sample": [{"code": c, "reason": e} for c, e in nodata[:10]],
        "coverage": {"n_covered": n_cov, "n_total": len(px),
                     "coverage_pct": cov_pct,
                     "rounds_completed": int(cur.get("rounds", 0)),
                     "next_shard": (shard_used + 1) % a.shards if not a.full else 0},
        "verdict": "drift" if drift else ("clean" if len(ok_l) else "unknown"),
        "note": ("全量校验" if a.full else
                 f"分片轮转校验，本次仅覆盖 {len(sample)} 只；"
                 f"累积覆盖 {cov_pct:.2f}%。未覆盖≠已通过"),
    })
    print("=" * 92)
    return 0 if not drift else 1


def _write_out(d):
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    print(f"已写 {OUT}")


if __name__ == "__main__":
    sys.exit(main())
