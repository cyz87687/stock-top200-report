#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_vol_unit_em.py —— 用东方财富 qfq 权威值逐只核对指定日期的成交量单位

## 为什么需要它

`data/klines_long.json` 的 volume 存在**双口径**（实测，非同推测）：

| 板块 | 前缀 | 单位 | 库vol / 东财vol(手) |
|---|---|---|---|
| 科创板 | 688 | 股 | 恒等 100.00 |
| 其他 | 00/30/60/… | 手 | 恒等 1.00 |

`2026-11-02` 这一天，非科创板被写成「股」，污染 1,771 只用。

## 为什么不能只靠库内自证

`该日 vol / 该股其余日期 vol 中位数` 的分布（实测 n=1,818）是**单峰对数正态**，
中位数 ≈100、区间横跨 20~2000（当日真实量对历史中位量的自然波动就有 2 个数量级）。
所以任何固定的比值区间（如 [50,300]）都会随机漏判约 30%，**库内自证不可靠**。

→ 唯一可靠判据：与东财 `stock_zh_a_hist(adjust="qfq")` 的「成交量」（单位=手）逐只比对。

## 输出

对每只股票计算 `ratio = 库vol / 东财vol`：
- `ratio ≈ 1`    → 库内是「手」口径，正常
- `ratio ≈ 100`  → 库内是「股」口径
- `688` 且 `≈100` → 设计如此（科创板本来就是股），**不动**
- 其他偏离（>5%）→ 进 `anomaly`，供人工核查

用法：
    python verify_vol_unit_em.py --date 2026-11-02
    python verify_vol_unit_em.py --date 2026-11-02 --json out.json
"""
import os
import sys
import json
import time
import argparse
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
KL = os.path.join(HERE, "data", "klines_long.json")

WORKERS = 6
RETRY = 3


def fetch_em_vol(sym, date_compact):
    """取东财 qfq 该日成交量（单位：手）。失败返回 (None, 原因)"""
    import akshare as ak
    last = None
    for _ in range(RETRY):
        try:
            df = ak.stock_zh_a_hist(symbol=sym, period="daily",
                                    start_date=date_compact,
                                    end_date=date_compact, adjust="qfq")
            if df is None or len(df) == 0:
                return None, "东财无该日数据"
            return float(df.iloc[-1]["成交量"]), None
        except Exception as e:                      # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
            time.sleep(0.4)
    return None, last or "unknown"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="2026-11-02")
    ap.add_argument("--json", default="")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 只（调试）")
    a = ap.parse_args()

    dc = a.date.replace("-", "")
    print("=" * 96)
    print(f"verify_vol_unit_em · 东财权威核对 {a.date} 成交量单位")
    print("=" * 96)

    raw = json.load(open(KL, encoding="utf-8"))
    codes = [c for c, m in raw.items() if a.date in m]
    codes.sort()
    if a.limit:
        codes = codes[:a.limit]
    print(f"待核对 {len(codes)} 只（库内共 {len(raw)} 只）")

    lock = threading.Lock()
    done = [0]
    res = {}

    def work(c):
        v, err = fetch_em_vol(c, dc)
        with lock:
            done[0] += 1
            if done[0] % 200 == 0:
                print(f"  … {done[0]}/{len(codes)}")
        return c, v, err

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(work, c) for c in codes]
        for f in as_completed(futs):
            c, v, err = f.result()
            res[c] = (v, err)
    print(f"抓取完成 {time.time()-t0:.1f}s，失败 {sum(1 for v,_ in res.values() if v is None)} 只")

    # ── 分类 ──
    grp = {"hand": [], "share": [], "688_share": [], "anomaly": [], "nodata": []}
    for c in codes:
        v_em, err = res[c]
        if v_em is None or v_em <= 0:
            grp["nodata"].append((c, err))
            continue
        v_lib = float(raw[c][a.date][4])
        r = v_lib / v_em
        if c.startswith("688"):
            if abs(r - 100.0) / 100.0 < 0.05:
                grp["688_share"].append((c, r))
            elif abs(r - 1.0) < 0.05:
                grp["hand"].append((c, r))          # 688 却是手：记录
            else:
                grp["anomaly"].append((c, r, v_lib, v_em))
            continue
        if abs(r - 1.0) < 0.05:
            grp["hand"].append((c, r))
        elif abs(r - 100.0) / 100.0 < 0.05:
            grp["share"].append((c, r))
        else:
            grp["anomaly"].append((c, r, v_lib, v_em))

    print(f"\n非688 手口径(正常)   {len(grp['hand']):>5} 只")
    print(f"非688 股口径(污染)   {len(grp['share']):>5} 只   ← 应被 ÷100")
    print(f"688  股口径(设计)    {len(grp['688_share']):>5} 只   ← 不动")
    print(f"异常(非1非100)       {len(grp['anomaly']):>5} 只")
    print(f"东财无数据           {len(grp['nodata']):>5} 只")

    if grp["share"]:
        rs = [x[1] for x in grp["share"]]
        print(f"\n污染组比值区间 [{min(rs):.4f}, {max(rs):.4f}]  中位 {sorted(rs)[len(rs)//2]:.4f}")
        for c, r in grp["share"][:5]:
            print(f"  {c}  ratio={r:.4f}")
    if grp["anomaly"]:
        print(f"\n⚠️ 异常样例（需人工核查）：")
        for c, r, vl, ve in grp["anomaly"][:15]:
            print(f"  {c}  ratio={r:.4f}  库={vl:,.0f}  东财={ve:,.0f}")
    if grp["nodata"]:
        print(f"\n⚠️ 东财无数据样例：{[(c, e) for c, e in grp['nodata'][:5]]}")

    if a.json:
        out = {
            "date": a.date,
            "counts": {k: len(v) for k, v in grp.items()},
            "share": [[c, r] for c, r in grp["share"]],
            "anomaly": [[c, r, vl, ve] for c, r, vl, ve in grp["anomaly"]],
            "nodata": [[c, e] for c, e in grp["nodata"]],
        }
        json.dump(out, open(a.json, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print(f"\n已写 {a.json}")

    print("=" * 96)
    return 0 if not grp["anomaly"] else 1


if __name__ == "__main__":
    sys.exit(main())
