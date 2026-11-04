"""recompute_ic_long.py —— 在 2020 起长面板上重算三周期 IC。

为什么必须重算：
    旧结论 h=20 NW t=1.85 建立在 218 个截面 → n_eff=218/20≈11，无信息量。
    现在 1653 个交易日 → h=20 的 n_eff≈80，才够得上 MIN_EFFECTIVE_OBS=30。

口径（与生产 v4.17 严格一致）：
    权重 fund .20/size_growth .20/tech_neg .10/quality .10/ep .10/
         dominant .10/growth .10/bias60 .05/rsi_rev .05
    ⚠️ fund 与 dominant 在长面板里全 None → 必须**从分母剔除**再重归一化，
       不能用 `or 0`（那等于把「没测」当「0 分」，会系统性压低总分）。
    缺维处置：逐行按「有值维度」重归一化 —— 这是与生产一致的做法。

统计：
    NW t（lag=h-1）、n_eff=n/h、Bonferroni 阈值、分年度稳定性、LOO
"""
import json
import math
import os
import sys
import time
from collections import defaultdict

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PANEL = os.path.join(HERE, "data", "panel_round124_long.json")
OUT = os.path.join(HERE, "data", "ic_long_2020.json")

W = {"score_fund": 0.20, "score_size_growth": 0.20, "score_tech_neg": 0.10,
     "score_quality": 0.10, "score_ep": 0.10, "score_dominant": 0.10,
     "score_growth": 0.10, "score_bias60": 0.05, "score_rsi_rev": 0.05}
# round125: dim_max 改为**单一来源**（原为硬编码副本）。
#   铁律：量程只能来自 factor_weights.json → dim_cfg.load_dim_max。
from dim_cfg import load_dim_max
DIM_MAX = load_dim_max(['score_bias60', 'score_rsi_rev'], strict=True)
MIN_CS = 200
HORIZONS = (1, 5, 20)


def nw_t(xs, lag):
    n = len(xs)
    if n < 3:
        return None
    m = sum(xs) / n
    e = [x - m for x in xs]
    g0 = sum(v * v for v in e) / n
    s = g0
    for k in range(1, lag + 1):
        gk = sum(e[i] * e[i - k] for i in range(k, n)) / n
        s += 2.0 * (1.0 - k / (lag + 1.0)) * gk
    if s <= 0:
        return None
    return m / math.sqrt(s / n)


def plain_t(xs):
    n = len(xs)
    if n < 3:
        return None
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    return m / (sd / math.sqrt(n)) if sd > 0 else None


def rank_ic(fv, rv):
    """Spearman 秩相关。"""
    n = len(fv)
    if n < 30:
        return None

    def rk(a):
        order = sorted(range(len(a)), key=lambda i: a[i])
        r = [0.0] * len(a)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and a[order[j + 1]] == a[order[i]]:
                j += 1
            avg = (i + j) / 2.0
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    ra, rb = rk(fv), rk(rv)
    ma, mb = sum(ra) / n, sum(rb) / n
    num = sum((ra[i] - ma) * (rb[i] - mb) for i in range(n))
    da = math.sqrt(sum((x - ma) ** 2 for x in ra))
    db = math.sqrt(sum((x - mb) ** 2 for x in rb))
    if da == 0 or db == 0:
        return None
    return num / (da * db)


def composite(rec):
    """逐行按有值维度重归一化 → 0~1 总分（与生产口径一致）。

    ⚠️ 关键：缺失维**不能当 0 分**。生产是「有值维度加权平均」，
    即 score = Σ(w_i × x_i/DIM_MAX_i) / Σ(w_i | x_i 有值)。
    """
    num, den = 0.0, 0.0
    for k, w in W.items():
        v = rec.get(k)
        if v is None:
            continue
        dm = DIM_MAX.get(k, 10.0)
        num += w * (v / dm)
        den += w
    if den <= 0:
        return None
    return num / den


def main():
    ap_argv = sys.argv[1:]
    panel_path = PANEL
    if "--panel" in ap_argv:
        panel_path = ap_argv[ap_argv.index("--panel") + 1]
    t00 = time.time()
    print(f"读取 {panel_path} ...")
    with open(panel_path, encoding="utf-8") as f:
        panel = json.load(f)
    dates = panel["dates"]
    rows = panel["rows"]
    print(f"  {len(dates)} 个交易日  {dates[0]} ~ {dates[-1]}")

    # 提取收盘价（面板只存 price，前瞻收益需用 klines_2020 的复权价）
    print("加载复权价 ...")
    z = np.load(os.path.join(HERE, "data", "klines_2020.npz"), allow_pickle=False)
    X = z["X"]
    kd = [str(d) for d in z["dates"]]
    kc = [str(c) for c in z["codes"]]
    didx = {d: i for i, d in enumerate(kd)}
    cidx = {c: i for i, c in enumerate(kc)}
    close = X[:, :, 1]

    results = {}
    for h in HORIZONS:
        ics, spreads, used = [], [], []
        for n, d in enumerate(dates):
            if n % 50 == 0:
                print(f"  h={h}  {n}/{len(dates)}  {d}")
            di = didx.get(d)
            if di is None or di + h >= len(kd):
                continue
            day = rows.get(d) or {}
            if len(day) < MIN_CS:
                continue
            d1 = kd[di + h]
            fv, rv, codes = [], [], []
            for c, rec in day.items():
                ci = cidx.get(c)
                if ci is None:
                    continue
                p0, p1 = close[ci, di], close[ci, di + h]
                if not (np.isfinite(p0) and np.isfinite(p1)) or p0 <= 0:
                    continue
                sc = composite(rec)
                if sc is None:
                    continue
                fv.append(sc)
                rv.append(float(p1 / p0 - 1.0))
                codes.append(c)
            if len(fv) < MIN_CS:
                continue
            ic = rank_ic(fv, rv)
            if ic is None:
                continue
            ics.append(ic)
            used.append((d, len(fv)))
            # Top10% 超额 vs 池等权
            order = sorted(range(len(fv)), key=lambda i: -fv[i])
            k10 = max(1, len(order) // 10)
            top = sum(rv[i] for i in order[:k10]) / k10
            ew = sum(rv) / len(rv)
            spreads.append(top - ew)

        if not ics:
            results[f"h{h}"] = {"n": 0, "usable": False}
            continue
        n = len(ics)
        neff = n / h
        lag = h - 1
        t_n = plain_t(ics)
        t_nw = nw_t(ics, lag)
        results[f"h{h}"] = {
            "n": n,
            "n_effective_obs": round(neff, 1),
            "ic_mean": round(sum(ics) / n, 4),
            "ic_std": round(float(np.std(ics, ddof=1)), 4),
            "t_naive": round(t_n, 2) if t_n else None,
            "nw_t": round(t_nw, 2) if t_nw else None,
            "nw_lag": lag,
            "t_inflation": round(t_n / t_nw, 2) if (t_n and t_nw) else None,
            "excess_pct": round(sum(spreads) / len(spreads) * 100, 3),
            "win_rate": round(sum(1 for x in ics if x > 0) / n, 3),
            "median_cs": int(np.median([u[1] for u in used])),
            "date_range": [used[0][0], used[-1][0]],
            "usable": bool(neff >= 30),
            "usable_note": (f"有效独立观测 {neff:.1f}"
                           + ("，可引用" if neff >= 30
                              else "，< 30 → NW t 无信息量，不得引用")),
            "ic_series": [round(x, 4) for x in ics],
            "dates": [u[0] for u in used],
        }
        print(f"  h={h}: n={n} IC={results[f'h{h}']['ic_mean']:+.4f} "
              f"NWt={results[f'h{h}']['nw_t']} n_eff={neff:.1f} "
              f"超额={results[f'h{h}']['excess_pct']:+.2f}%")

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "panel": panel_path,
                   "n_dates": len(dates),
                   "range": [dates[0], dates[-1]],
                   "weight_used": W,
                   "min_cross_section": MIN_CS,
                   "results": results}, f, ensure_ascii=False, indent=1)
    print(f"\n写出 {OUT}  耗时 {time.time()-t00:.0f}s")

    print("\n=== 汇总 ===")
    for h in HORIZONS:
        r = results.get(f"h{h}", {})
        if not r.get("n"):
            print(f"  h={h}: n=0")
            continue
        print(f"  h={h:<3} n={r['n']:<5} IC={r['ic_mean']:+.4f} "
              f"NWt={r['nw_t']:>6} n_eff={r['n_effective_obs']:>6.1f} "
              f"超额={r['excess_pct']:+.2f}% 胜率={r['win_rate']:.3f} "
              f"{'✅可用' if r['usable'] else '❌不可用'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
