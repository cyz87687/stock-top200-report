#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""#293c quality 为什么是负 IC —— 分离「NaN 污染」与「量纲混杂」两个缺陷。

修复 NaN 后 quality 的 h20 IC = −0.0298 (t=−4.03)，显著为负。
但 fund_split 还有第二个已知缺陷：Q_GROUPS 组内子因子**量纲混杂**
（roe 是 %、ocf_ni 是倍数、debt 是 %取负、profit_vol 是标准差），
组内直接算术平均 → debt(−49.69) 压倒 roe(4.98)。

本脚本对比四种 quality 口径，判定负 IC 来自哪个缺陷：
  V1 现状（含NaN）        —— 复现线上
  V2 仅清NaN              —— 隔离缺陷①
  V3 清NaN + 子因子先截面z化再加权（组内等权作用在 z 值上）—— 隔离缺陷②
  V4 纯 ROE 单因子        —— 概念基准（经典质量因子）
"""
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import fund_split as FS      # noqa: E402
import factor_eval as fe     # noqa: E402
import audit_round34 as A34  # noqa: E402


def is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x and not math.isinf(x)


def rank10_map(raw):
    present = [(c, v) for c, v in raw.items() if is_num(v)]
    present.sort(key=lambda kv: kv[1])
    n = len(present)
    out = {c: None for c in raw}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and present[j + 1][1] == present[i][1]:
            j += 1
        avg = (i + j) / 2.0
        for k in range(i, j + 1):
            out[present[k][0]] = round(avg / max(1, n - 1) * 10.0, 2)
        i = j + 1
    return out


def zscore_map(raw):
    """截面内 winsorize(1/99) → z 化。NaN 视为缺失。"""
    items = [(c, v) for c, v in raw.items() if is_num(v)]
    if len(items) < 10:
        return {}
    vs = sorted(v for _, v in items)
    lo = vs[int(len(vs) * 0.01)]
    hi = vs[int(len(vs) * 0.99) - 1] if len(vs) > 100 else vs[-1]
    clipped = {c: min(max(v, lo), hi) for c, v in items}
    mu = sum(clipped.values()) / len(clipped)
    sd = (sum((x - mu) ** 2 for x in clipped.values()) / len(clipped)) ** 0.5
    if sd <= 0:
        return {}
    return {c: (v - mu) / sd for c, v in clipped.items()}


def main():
    panel = json.load(open(os.path.join(HERE, "data", "panel_round30.json"), encoding="utf-8"))
    em = FS.load_em_cache()
    px = A34.load_px()
    tdays = A34.build_tdays(px)
    tidx = {x: i for i, x in enumerate(tdays)}

    codes = set()
    for d0 in panel["dates"]:
        codes.update((panel["rows"].get(d0) or {}).keys())
    codes = sorted(codes)

    dates = panel["dates"]
    # 逐截面收集子因子
    print(f"域 {len(codes)} 只 · {len(dates)} 截面；逐截面构建 4 套 quality …")

    V1 = {d: {} for d in dates}   # 含NaN 原始族分
    V2 = {d: {} for d in dates}   # 清NaN 原始族分
    V4 = {d: {} for d in dates}   # 纯 ROE
    # V3 需要「逐子因子 z 化 + 组内等权 + 组间等权」，分两步
    sub_by_date = {d: {} for d in dates}

    for d0 in dates:
        for c6 in codes:
            ind = em.get(c6)
            if not ind:
                continue
            periods = sorted({p for dd in ind.values() for p in dd}, reverse=True)
            P = FS.pit_period(periods, d0)
            if not P:
                continue
            s_raw = FS.sub_factors_pit(ind, P)
            # V1：原样（NaN 会进 fam，但这里是 dict 直接算，需模拟 fam 的 isinstance 放行）
            q1, _ = FS.family_scores(s_raw)
            V1[d0][c6] = q1 if isinstance(q1, float) else None
            # V2：清 NaN
            s_cl = {k: (v if is_num(v) else None) for k, v in s_raw.items()}
            q2, _ = FS.family_scores(s_cl)
            V2[d0][c6] = q2
            # V4：纯 ROE
            V4[d0][c6] = s_cl.get("roe")
            sub_by_date[d0][c6] = s_cl

    # V3：逐截面 → 逐子因子 z 化 → 组内均值 → 组间等权
    V3 = {d: {} for d in dates}
    for d0 in dates:
        store = sub_by_date[d0]
        zmap = {}
        for subname in FS.QUAL_SUBS:
            raw = {c: (store.get(c) or {}).get(subname) for c in store}
            zmap[subname] = zscore_map(raw)
        for c6 in store:
            num = den = 0.0
            for gname, members in FS.Q_GROUPS.items():
                zs = [zmap[m].get(c6) for m in members]
                zs = [z for z in zs if z is not None]
                if not zs:
                    continue
                num += sum(zs) / len(zs)
                den += 1.0
            V3[d0][c6] = num / den if den > 0 else None

    # 全部转成 0-10 分位
    for tag, M in (("V1", V1), ("V2", V2), ("V3", V3), ("V4", V4)):
        for d0 in dates:
            M[d0] = rank10_map(M[d0])

    def ic_series(qmaps, hop):
        ics = []
        for d0 in dates:
            if tidx.get(d0) is None or tidx[d0] + hop >= len(tdays):
                continue
            cur = panel["rows"].get(d0) or {}
            xs, ys = [], []
            for c in cur:
                q = qmaps[d0].get(c)
                if not is_num(q):
                    continue
                m = px.get(c)
                i0 = tidx.get(d0)
                if not m or i0 is None:
                    continue
                p0, p1 = m.get(d0), m.get(tdays[i0 + hop])
                if p0 and p1 and p0 > 0:
                    xs.append(q)
                    ys.append(p1 / p0 - 1.0)
            if len(xs) >= 40:
                ic = fe.rank_ic(xs, ys)
                if ic is not None:
                    ics.append(ic)
        if len(ics) < 5:
            return None
        mu = sum(ics) / len(ics)
        sd = (sum((x - mu) ** 2 for x in ics) / (len(ics) - 1)) ** 0.5
        return {"ic": mu, "n": len(ics), "t": mu / (sd / len(ics) ** 0.5) if sd > 0 else None}

    LAB = {"V1": "V1 现状（含NaN，不可复现）",
           "V2": "V2 仅清NaN（量纲仍混杂）",
           "V3": "V3 清NaN+子因子z化（修量纲）",
           "V4": "V4 纯ROE（概念基准）"}
    print("\n" + "=" * 96)
    print("【four 口径 quality IC 对比】")
    print("=" * 96)
    print(f"  {'口径':26s} {'h5 IC':>9s} {'t':>6s} {'h20 IC':>9s} {'t':>6s} {'h40 IC':>9s} {'t':>6s}")
    res = {}
    for tag, M in (("V1", V1), ("V2", V2), ("V3", V3), ("V4", V4)):
        row = {}
        cells = []
        for hop in (5, 20, 40):
            r = ic_series(M, hop)
            row[f"h{hop}"] = r
            if r:
                cells.append(f"{r['ic']:+.4f}")
                cells.append(f"{r['t']:+.2f}")
            else:
                cells += ["n/a", "n/a"]
        res[tag] = row
        print(f"  {LAB[tag]:26s} " + " ".join(f"{x:>9s}" if i % 2 == 0 else f"{x:>6s}"
                                              for i, x in enumerate(cells)))

    json.dump(res, open(os.path.join(HERE, "audit_round35c.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("\n写入 audit_round35c.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
