#!/usr/bin/env python3
"""Round11：theme 反号的增量检验 —— 是不是只是反转块的代理？

Round10 发现两件互相矛盾的事：
  · score_theme 反号后 h20 IC=+0.1207 / NW t=+3.59（过 Bonferroni 3.08）→ 全场最强正 alpha
  · 但 corr(score_theme, score_rsi_rev) = -0.549 → 反号后与反转块正相关 0.549
  · 且五分位不严格单调（Q3/Q4 有交叉）

反转块（oversold60/bias60/rsi_rev/vol_pulse/overbought）已占权重 0.055*4+0.02=0.24。
若 theme_neg 只是反转块的代理，反号纳入 = 变相给反转块加权重（而反转块本身
T+1 IC 为负、样本仅 3~7 个截面），属于重复计权而非新增信息。

本脚本做三重增量检验：
  D1 剔除反转块后的残差 IC（FWL 思路：先回归掉，再看剩余）
  D2 分年/分区间稳定性（四段）
  D3 双变量分组：反转块分档后，theme_neg 是否仍有区分度
"""
import json
import os
import sys
import math

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import factor_eval as fe

HOP = 20
REV_DIMS = ("score_oversold60", "score_bias60", "score_rsi_rev",
            "score_vol_pulse", "score_overbought")


def mean(a):
    return sum(a) / len(a) if a else None


def sd(a):
    if len(a) < 2:
        return None
    m = mean(a)
    return math.sqrt(sum((x - m) ** 2 for x in a) / (len(a) - 1))


def spearman(xs, ys):
    n = len(xs)
    if n < 5:
        return None
    def rank(v):
        o = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        for pos, i in enumerate(o):
            r[i] = pos
        return r
    rx, ry = rank(xs), rank(ys)
    mx, my = mean(rx), mean(ry)
    vx = sum((v - mx) ** 2 for v in rx)
    vy = sum((v - my) ** 2 for v in ry)
    if vx <= 0 or vy <= 0:
        return None
    return sum((rx[i] - mx) * (ry[i] - my) for i in range(n)) / (vx * vy) ** 0.5


def resid_ic(xs, ctrl_list, ys):
    """FWL 残差法：把 xs 对 ctrl_list 做线性回归取残差，再与 ys 求 Spearman。
    纯标准差归一化后的 OLS（因子已可近似视为等距）。"""
    n = len(xs)
    if n < 20:
        return None
    # 构造设计矩阵 [1, c1, c2, ...]
    cols = [[1.0] * n]
    for c in ctrl_list:
        m, s = mean(c), sd(c)
        if not s:
            continue
        cols.append([(c[i] - m) / s for i in range(n)])
    p = len(cols)
    # 正规方程 (X'X)b = X'y  用高斯消元
    A = [[sum(cols[i][k] * cols[j][k] for k in range(n)) for j in range(p)]
         for i in range(p)]
    y = [(xs[i] - mean(xs)) / (sd(xs) or 1) for i in range(n)]
    b = [sum(cols[i][k] * y[k] for k in range(n)) for i in range(p)]
    # 增广矩阵消元
    M = [A[i][:] + [b[i]] for i in range(p)]
    for i in range(p):
        piv = max(range(i, p), key=lambda r: abs(M[r][i]))
        if abs(M[piv][i]) < 1e-10:
            continue
        M[i], M[piv] = M[piv], M[i]
        for r in range(p):
            if r == i:
                continue
            f = M[r][i] / M[i][i]
            for cc in range(i, p + 1):
                M[r][cc] -= f * M[i][cc]
    coef = [0.0] * p
    for i in range(p):
        if abs(M[i][i]) > 1e-10:
            coef[i] = M[i][p] / M[i][i]
    # 残差
    resid = []
    for k in range(n):
        pred = sum(coef[i] * cols[i][k] for i in range(p))
        resid.append(y[k] - pred)
    return spearman(resid, ys)


def nw_t(xs, lag=None):
    n = len(xs)
    if n < 3:
        return None
    m = mean(xs)
    if lag is None:
        lag = min(HOP - 1, max(1, int(n ** (1 / 3)) * 2))
    d = [x - m for x in xs]
    s2 = sum(v * v for v in d) / n
    for k in range(1, min(lag, n - 1) + 1):
        w = 1.0 - k / (lag + 1.0)
        g = sum(d[i] * d[i + k] for i in range(n - k)) / n
        s2 += 2 * w * g
    se = math.sqrt(max(s2, 1e-18) / n)
    return m / se if se > 0 else None


def main():
    h = json.load(open(os.path.join(HERE, "factor_history.json")))
    snaps, _ = fe._drop_stale_price_snapshots(h["snapshots"])
    tdays, px = fe.px_index(h)
    tidx = {d: i for i, d in enumerate(tdays)}

    # 逐截面数据
    per = []          # [{date, theme:[], revblock:[], fwd:[]}]
    for s in snaps:
        d0 = s["date"]
        if d0 not in tidx:
            continue
        i0 = tidx[d0]
        i1 = i0 + HOP
        if i1 >= len(tdays):
            continue
        d1 = tdays[i1]
        th, rev, fw, fund = [], [], [], []
        rev_ok = True
        for r in s["rows"]:
            m = px.get(r.get("code"))
            if not m:
                continue
            p0, p1 = m.get(d0), m.get(d1)
            if not p0 or not p1 or p0 <= 0:
                continue
            t = r.get("score_theme")
            if not isinstance(t, (int, float)):
                continue
            th.append(float(t))
            fw.append(p1 / p0 - 1.0)
            f = r.get("score_fund")
            fund.append(float(f) if isinstance(f, (int, float)) else None)
            rv = [r.get(k) for k in REV_DIMS]
            if all(isinstance(x, (int, float)) for x in rv):
                rev.append([float(x) for x in rv])
            else:
                rev_ok = False
        if len(th) < 40:
            continue
        rec = {"date": d0, "theme": th, "fwd": fw, "rev_ok": rev_ok,
               "rev": rev if rev_ok else None,
               "fund": fund if all(f is not None for f in fund) else None}
        per.append(rec)
    print(f"可用截面 {len(per)} 个（要求 theme 非空且有 T+20 收益）")
    n_rev = sum(1 for r in per if r["rev_ok"])
    print(f"其中反转块五维齐备 {n_rev} 个截面（这是 D1 的实际上限）\n")

    # ---------- D0 基准 ----------
    base = [-spearman(r["theme"], r["fwd"]) for r in per]
    base = [x for x in base if x is not None]
    print("=" * 74)
    print("D0  基准：theme 反号 IC（未控任何变量）")
    print("=" * 74)
    print(f"  n={len(base)}  IC={mean(base):+.4f}  NW t={nw_t(base):+.2f}")

    # ---------- D1 剔除反转块 ----------
    print("\n" + "=" * 74)
    print("D1  增量检验：剔除反转块后 theme_neg 是否还有 alpha（FWL 残差法）")
    print("=" * 74)
    d1_ic, d0_sub = [], []
    for r in per:
        if not r["rev_ok"]:
            continue
        ctrl = [[row[i] for row in r["rev"]] for i in range(5)]
        ric = resid_ic(r["theme"], ctrl, r["fwd"])
        if ric is None:
            continue
        d1_ic.append(-ric)                    # 反号
        d0_sub.append(-spearman(r["theme"], r["fwd"]))
    if d1_ic:
        print(f"  对照组（同截面未剔除）  n={len(d0_sub)}  IC={mean(d0_sub):+.4f}  "
              f"NW t={nw_t(d0_sub):+.2f}")
        print(f"  剔除反转块后残差      n={len(d1_ic)}  IC={mean(d1_ic):+.4f}  "
              f"NW t={nw_t(d1_ic):+.2f}")
        drop = (1 - abs(mean(d1_ic)) / max(1e-9, abs(mean(d0_sub)))) * 100
        print(f"  → 信息衰减 {drop:+.1f}%  "
              f"{'⚠️ 超额主要来自反转块代理，非独立信息' if drop > 50 else '✅ 尚存独立信息'}")
    else:
        print("  ⚠️ 可用截面不足，无法做 FWL")

    # ---------- D2 分段稳定性 ----------
    print("\n" + "=" * 74)
    print("D2  分段稳定性（把 58 个截面按时间等分四段）")
    print("=" * 74)
    N = len(per)
    for k in range(4):
        lo, hi = N * k // 4, N * (k + 1) // 4
        blk = per[lo:hi]
        xs = [-spearman(r["theme"], r["fwd"]) for r in blk]
        xs = [x for x in xs if x is not None]
        if len(xs) >= 3:
            print(f"  段{k+1} {blk[0]['date']}~{blk[-1]['date']}  n={len(xs):<3} "
                  f"IC={mean(xs):+.4f}  NW t={nw_t(xs):+.2f}  "
                  f"正IC占比 {100*sum(1 for v in xs if v>0)/len(xs):.0f}%")

    # ---------- D3 双变量分组 ----------
    print("\n" + "=" * 74)
    print("D3  双变量分组：反转块分档后，theme_neg 还有区分度吗（T+20 等权）")
    print("=" * 74)
    cells = {}
    for r in per:
        if not r["rev_ok"]:
            continue
        # 反转块合成分 = 五维等权
        comp = [sum(row[i] for i in range(5)) / 5.0 for row in r["rev"]]
        order = sorted(range(len(comp)), key=lambda i: comp[i])
        n = len(order)
        for pos, i in enumerate(order):
            rev_bin = min(2, pos * 3 // n)          # 反转块三档（低/中/高）
            cells.setdefault(rev_bin, []).append((r["theme"][i], r["fwd"][i]))
    for rb in sorted(cells):
        recs = cells[rb]
        recs.sort(key=lambda x: -x[0])              # theme 降序 = theme_neg 优先
        n = len(recs)
        if n < 30:
            continue
        top = mean([x[1] for x in recs[:n // 3]]) or 0
        bot = mean([x[1] for x in recs[-n // 3:]]) or 0
        label = {0: "反转块低（超跌少）", 1: "反转块中", 2: "反转块高（超跌多）"}[rb]
        print(f"  {label:<18} n={n:<6} theme低热度组 {top*100:+6.2f}% | "
              f"theme高热度组 {bot*100:+6.2f}% | 差 {(top-bot)*100:+5.2f} pct")

    # ---------- D4 fund 交互 ----------
    print("\n" + "=" * 74)
    print("D4  与 score_fund（0.34 主权重）是否冲突")
    print("=" * 74)
    inter = []
    for r in per:
        a, b = [], []
        for i in range(len(r["theme"])):
            if r["fund"] and r["fund"][i] is not None:
                a.append(r["theme"][i])
                b.append(r["fund"][i])
        if len(a) >= 40:
            c = spearman(a, b)
            if c is not None:
                inter.append(c)
    if inter:
        print(f"  corr(score_theme, score_fund) 截面均值 = {mean(inter):+.3f}  (n={len(inter)})")
        print(f"  → {'低相关，加 theme_neg 不会与 fund 打架' if abs(mean(inter))<0.3 else '存在交互，需谨慎'}")

    out = {"n_snapshots": len(per), "n_rev_ok": n_rev,
           "d0_ic": round(mean(base), 4), "d0_nw_t": round(nw_t(base), 2),
           "d1_resid_ic": round(mean(d1_ic), 4) if d1_ic else None,
           "d1_resid_nw_t": round(nw_t(d1_ic), 2) if d1_ic else None}
    with open(os.path.join(HERE, "audit_round11.json"), "w", encoding="utf-8") as fp:
        json.dump(out, fp, ensure_ascii=False, indent=2)
    print("\n✅ 已写 audit_round11.json")


if __name__ == "__main__":
    main()
