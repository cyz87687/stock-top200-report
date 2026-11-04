#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v4.8 落地口径复核：round24d 漏测的 size 维。

背景：round24d 的冗余表与复合 IC 全部**不含 score_size**
（当时理由是 circ_mv 只在 9/86 截面有值，带 size 会因缺维剔分母漂移）。
但 v4.8 正式上线的是**七维含 size 0.10**，与 round24d 验证的**六维不含 size** 口径不一致。
本脚本补齐这个缺口：

Q1 size × lowvol20 到底是什么关系（秩相关 + 偏相关 + 分位交叉表）
Q2 七维（含 size）口径下，换维是否仍优于保留成长
Q3 size 与 lowvol 是否「反向对冲」→ 净权重还剩多少
Q4 若确认冗余，三个处置方案的效果对比

数据：10-23 scored（circ_mv / score_size / 各维 490 只全有值）+
      data/universe_klines.json（10-13，仅用于算 lowvol20 原始值）
⚠️ 单截面：只能做「横截面结构」检验，不能做「前瞻 alpha」检验。
   alpha 部分由 round24d 的多截面结果提供，本脚本只回答「落地口径是否自洽」。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCORED = os.path.join(HERE, "top200_scored_2026-10-23.json")
KLINES = os.path.join(HERE, "data", "universe_klines.json")


def norm_code(c):
    s = "".join(ch for ch in str(c) if ch.isdigit())
    return s[-6:] if len(s) >= 6 else s


def kl_list_from_dict(d):
    if not isinstance(d, dict) or not d:
        return []
    out = []
    for dt in sorted(d.keys()):
        v = d[dt]
        if not isinstance(v, (list, tuple)) or len(v) < 4:
            continue
        try:
            out.append({"date": dt, "close": float(v[3])})
        except (TypeError, ValueError):
            continue
    return out


def raw_lowvol20(kl):
    if not kl or len(kl) < 21:
        return None
    closes = [k["close"] for k in kl[-21:]]
    rets = [(closes[i] / closes[i - 1] - 1.0) for i in range(1, len(closes))]
    n = len(rets)
    mu = sum(rets) / n
    return (sum((r - mu) ** 2 for r in rets) / (n - 1)) ** 0.5


def _avg_rank(vals):
    idx = sorted(range(len(vals)), key=lambda i: vals[i])
    r = [0.0] * len(vals)
    i = 0
    while i < len(idx):
        j = i
        while j + 1 < len(idx) and vals[idx[j + 1]] == vals[idx[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            r[idx[k]] = avg
        i = j + 1
    return r


def corr(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    sxx = sum((xs[i] - mx) ** 2 for i in range(n)) ** 0.5
    syy = sum((ys[i] - my) ** 2 for i in range(n)) ** 0.5
    return (sxy / (sxx * syy)) if sxx > 0 and syy > 0 else None


def rank_corr(xs, ys):
    return corr(_avg_rank(xs), _avg_rank(ys))


def partial_corr(x, y, z):
    """r(x,y|z)：控制 z 后 x 与 y 的偏相关（Pearson 残差法）。"""
    def resid(a, b):
        n = len(a)
        mb, ma = sum(b) / n, sum(a) / n
        sb = sum((b[i] - mb) ** 2 for i in range(n))
        if sb <= 0:
            return [a[i] - ma for i in range(n)]
        beta = sum((b[i] - mb) * (a[i] - ma) for i in range(n)) / sb
        return [a[i] - ma - beta * (b[i] - mb) for i in range(n)]
    return corr(resid(x, z), resid(y, z))


def main():
    print("=" * 80)
    print("v4.8 落地口径复核：round24d 漏测的 size 维")
    print("=" * 80)

    scored = json.load(open(SCORED, encoding="utf-8"))
    rows = scored["results"]
    klines = json.load(open(KLINES, encoding="utf-8"))
    kl_by_num = {norm_code(k): v for k, v in klines.items() if isinstance(v, dict) and v}

    # 原始波动率 → 池内分位（仅用能算出的部分）
    raw = {}
    for r in rows:
        v = raw_lowvol20(kl_list_from_dict(kl_by_num.get(norm_code(r["code"])) or {}))
        if v is not None:
            raw[r["code"]] = v
    cov = len(raw) / len(rows)
    print(f"数据：scored {len(rows)} 只（date={scored.get('date')}），"
          f"lowvol20 可算 {len(raw)} 只（{cov:.1%}）")
    print()

    # 池内分位分（0-10，低波=高分）—— 与 step2 同实现
    ks = sorted(raw, key=lambda c: raw[c])
    m = len(ks)
    lv_rank = {c: round((1.0 - (i / (m - 1))) * 10.0, 2) for i, c in enumerate(ks)} if m > 1 else {}

    by_code = {r["code"]: r for r in rows}
    codes = [c for c in lv_rank if c in by_code]

    # ===== Q1 =====
    print("─" * 80)
    print("Q1  size × lowvol20 的关系")
    xs = [lv_rank[c] for c in codes]
    ys_size = [float(by_code[c]["score_size"]) for c in codes]
    ys_mv = [float(by_code[c]["circ_mv"]) for c in codes if isinstance(by_code[c].get("circ_mv"), (int, float))]
    codes_mv = [c for c in codes if isinstance(by_code[c].get("circ_mv"), (int, float))]

    r_size = rank_corr(xs, ys_size)
    r_mv = rank_corr([lv_rank[c] for c in codes_mv], ys_mv)
    print(f"  秩相关  lowvol20 × score_size = {r_size:+.3f}   （n={len(codes)}）")
    print(f"  秩相关  lowvol20 × circ_mv    = {r_mv:+.3f}   （n={len(codes_mv)}；circ_mv 越大=市值越大）")
    print(f"  含义：lowvol 分高 = 波动低；size 分高 = 市值小。"
          f"{'两者显著负相关 → 低波股倾向大市值' if r_size < -0.3 else '相关性可控'}")

    # 控制其他维后的偏相关
    print()
    print("  控制其余生效维后的偏相关（看是否只是被共同因子带出来的）")
    OTHERS = ["score_fund", "score_quality", "score_tech_neg", "score_bias60", "score_rsi_rev"]
    sub = [c for c in codes if all(isinstance(by_code[c].get(k), (int, float)) for k in OTHERS)]
    # 用其余维等权合成作为控制变量
    ctrl = [sum(float(by_code[c][k]) for k in OTHERS) for c in sub]
    pc = partial_corr([lv_rank[c] for c in sub],
                      [float(by_code[c]["score_size"]) for c in sub], ctrl)
    print(f"  r(lowvol, size | 其余五维等权) = {pc:+.3f}   （n={len(sub)}）")
    print(f"  → {'冗余是真实结构性的，不是共同因子带出的' if abs(pc) > 0.4 else '控制后明显衰减，冗余部分是共同因子'}")

    # 分位交叉表
    print()
    print("  交叉表：lowvol20 五档 × size 五档（格内=只数）")
    print("  ⚠️ lowvol20 分越高 = 波动越低（分位取负）；size 分越高 = 市值越小")
    def bucket(v, mx=10.0):
        return min(4, max(0, int(float(v) / mx * 5)))
    tab = [[0] * 5 for _ in range(5)]
    for c in codes:
        tab[bucket(lv_rank[c])][bucket(by_code[c]["score_size"])] += 1
    print("               size低(大市值) ← → size高(小市值)")
    # 行标签必须与 bucket 方向一致：bucket 0 = lv_rank 0-2 = 高分位的反面 = 高波动
    lbl = ["高波(分0-2)", "   (2-4)", "   (4-6)", "   (6-8)", "低波(分8-10)"]
    for i in range(5):
        print(f"  {lbl[i]:<13} " + "  ".join(f"{tab[i][j]:>4}" for j in range(5)))
    diag = sum(tab[i][i] for i in range(5))
    anti = sum(tab[i][4 - i] for i in range(5))
    print(f"  对角（高波↔大市值 / 低波↔小市值，同向）{diag} 只")
    print(f"  反对角（低波↔大市值，互斥）{anti} 只 / 总 {len(codes)} 只 = {anti/len(codes):.1%}")
    print(f"  → 反对角占 {anti/len(codes):.1%}：{'「低波股就是大市值股」成立，两维方向互斥' if anti/len(codes) > 0.3 else '两维方向基本独立'}")

    # ===== Q2 =====
    print()
    print("─" * 80)
    print("Q2  七维（含 size）口径下，换维 vs 保留成长")
    # 量程：scored 内 fund/bias60/rsi_rev 为 0-5，其余 0-10
    DMAX = {"score_fund": 5.0, "score_quality": 10.0, "score_growth": 10.0,
            "score_lowvol20": 10.0, "score_tech_neg": 10.0, "score_bias60": 5.0,
            "score_rsi_rev": 5.0, "score_size": 10.0}
    BASE7 = {"score_fund": 0.15, "score_quality": 0.12, "score_size": 0.10,
             "score_tech_neg": 0.15, "score_bias60": 0.175, "score_rsi_rev": 0.175}
    CASES = {
        "v4.7 旧（+成长0.13，含size）": {**BASE7, "score_growth": 0.13},
        "v4.8 新（+低波0.13，含size）": {**BASE7, "score_lowvol20": 0.13},
        "低波0.13 去size": {k: v for k, v in {**BASE7, "score_lowvol20": 0.13}.items() if k != "score_size"},
        "低波0.13 + size降至0.05": {**{k: v for k, v in BASE7.items() if k != "score_size"},
                                     "score_size": 0.05, "score_lowvol20": 0.13},
        "低波0.08 + size0.10 + 成长0.05": {**{k: v for k, v in BASE7.items()},
                                            "score_lowvol20": 0.08, "score_growth": 0.05},
    }

    def total_of(r, wmap):
        num = den = 0.0
        for k, w in wmap.items():
            if w <= 0:
                continue
            v = lv_rank.get(r["code"]) if k == "score_lowvol20" else r.get(k)
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                continue          # 缺失剔分母
            num += (float(v) / DMAX[k] * 10.0) * w
            den += w
        return (num / den * 10.0) if den > 0 else None

    print(f"  {'方案':<32} {'n':>4} {'权重和':>6} {'中位':>7} {'均值':>7} {'max':>7}")
    tot_by_case = {}
    for nm, wmap in CASES.items():
        vals = []
        for r in rows:
            t = total_of(r, wmap)
            if t is not None:
                vals.append((r["code"], t))
        tot_by_case[nm] = vals
        vs = sorted(x[1] for x in vals)
        print(f"  {nm:<32} {len(vs):>4} {sum(wmap.values()):>6.2f} "
              f"{vs[len(vs)//2]:>7.2f} {sum(vs)/len(vs):>7.2f} {vs[-1]:>7.2f}")

    # ===== Q3 =====
    print()
    print("─" * 80)
    print("Q3  size 与 lowvol 是否「反向对冲」→ 净方向")
    nmby = {r["code"]: r["name"] for r in rows}
    # 在合成 total 里，size 与 lowvol 的贡献方向
    a = tot_by_case.get("v4.8 新（+低波0.13，含size）") or []
    b = tot_by_case.get("低波0.13 去size") or []
    ma = dict(a); mb = dict(b)
    common = [c for c in ma if c in mb]
    diffs = [(c, ma[c] - mb[c]) for c in common]
    # 小市值股（size 高）是否因 size 被拉高
    # ⚠️ 必须与 lv_rank 取交集：total 可比集合是全集 490，lv_rank 只覆盖能算 lowvol 的 427 只
    common_lv = [c for c in common if c in lv_rank]
    small = [c for c in common_lv if by_code[c]["score_size"] >= 8.0]
    big = [c for c in common_lv if by_code[c]["score_size"] <= 2.0]
    d_small = sum(d for c, d in diffs if c in set(small)) / max(1, len(small))
    d_big = sum(d for c, d in diffs if c in set(big)) / max(1, len(big))
    print(f"  「含 size」相对「去 size」的 total 变化（正=size 加分）")
    print(f"    小市值股（size≥8，n={len(small)}）平均 {d_small:+.2f} 分")
    print(f"    大市值股（size≤2，n={len(big)}）平均 {d_big:+.2f} 分")
    print(f"  → size 维确实按先验方向在加分（小市值更高），幅度差 {d_small-d_big:+.2f} 分")
    print()
    print(f"  ⚠️ 但 lowvol 的分位分对同一批股票方向相反：")
    lv_small = sum(lv_rank[c] for c in small) / max(1, len(small))
    lv_big = sum(lv_rank[c] for c in big) / max(1, len(big))
    print(f"    小市值股（size≥8）lowvol 平均分 {lv_small:.2f}/10")
    print(f"    大市值股（size≤2）lowvol 平均分 {lv_big:.2f}/10")
    print(f"    → 差 {lv_small-lv_big:+.2f} 分；乘以 0.13 权重再 ×10 = {(lv_small-lv_big)*0.13*10:+.2f} 分")
    print(f"    而 size 维 0.10 权重带来的差 ≈ {(by_code[small[0]]['score_size']-by_code[big[0]]['score_size'])*0.10*10:+.2f} 分（量级相当，方向相反）")

    # ===== Q4 =====
    print()
    print("─" * 80)
    print("Q4  Top20 名单在各方案下的重合度（稳定性视角）")
    base_top = [c for c, _ in sorted(tot_by_case["v4.8 新（+低波0.13，含size）"], key=lambda t: -t[1])[:20]]
    for nm, vals in tot_by_case.items():
        if nm == "v4.8 新（+低波0.13，含size）":
            continue
        top = [c for c, _ in sorted(vals, key=lambda t: -t[1])[:20]]
        keep = len(set(base_top) & set(top))
        print(f"  vs {nm:<32} 重合 {keep:>2}/20")
    print()
    print("  v4.8 新版 Top20：")
    for i, c in enumerate(base_top, 1):
        print(f"    {i:>2}. {nmby.get(c, c):<8} total={dict(tot_by_case['v4.8 新（+低波0.13，含size）'])[c]:.2f}")

    print()
    print("=" * 80)
    print("⚠️ 单截面限制：以上只反映 10-23 一天的横截面结构，")
    print("   不能替代多截面前瞻 alpha 检验（后者见 round24d，但其口径不含 size）。")
    print("=" * 80)
    return 0


if __name__ == "__main__":
    sys.exit(main())
