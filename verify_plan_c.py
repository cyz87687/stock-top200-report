#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
verify_plan_c.py —— 再分配方案 C 上线前的强制验证

背景（为什么必须有这个脚本）：
  decide_round34.py 的 `_comp()` 用 `len(xs) >= 40` 作门槛，而**不要求 fund 齐备**。
  在「4 维等权」里 fund 占 0.25；当某截面全体股票都缺 fund 时，
  prod_score 的「缺维从分子分母同剔」会把 0.25 权重整体剔除，
  于是 4 维等权**退化为 3 维等权**（quality/lowvol/size 各 0.25，Σ=0.75）。

  而 round34 面板里 fund 仅在 80/238 个截面有数据（覆盖 9.1%）。
  也就是说：方案 C 的 h20 IC=+0.0941 是**两套不同模型的混合平均**
  （80 个截面跑 4 维，158 个截面跑 3 维），并不是「4 维等权」这一个模型的表现。

  本脚本要回答三件事：
    Q1 把样本拆成「有 fund / 无 fund」两段，方案 C 各段表现如何？
    Q2 在同一段样本（有 fund 的 80 截面）上公平对比 A/C/D/E，谁真的更好？
    Q3 Σ≠1.0 时 prod_score 的 max(0.5, den) 会不会压缩分数？影响多大？

用法：
  python verify_plan_c.py
输出：
  verify_plan_c.json
"""
import json
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import factor_eval as fe                      # noqa: E402
from audit_round29 import nw_t                # noqa: E402

DMAX = {                                      # 与 factor_weights.json 的 dim_max 同源
    "score_fund": 5.0, "score_bias60": 5.0, "score_rsi_rev": 5.0,
    "score_quality": 10.0, "score_lowvol20": 10.0, "score_size_ind": 10.0,
    "score_tech_neg": 10.0, "score_ep": 10.0,
}
W8 = {
    "score_bias60": .175, "score_rsi_rev": .175, "score_tech_neg": .15,
    "score_lowvol20": .13, "score_quality": .12, "score_size_ind": .10,
    "score_ep": .075, "score_fund": .075,
}
EFF = [k for k, v in W8.items() if v > 0]


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def prod_score(vals, wmap):
    """生产线同构：缺维从分子分母同剔，分母下限 0.5（禁 or 0）。

    ⚠️ 分母下限 0.5 是**量程保护**：若 Σw < 0.5，num/max(0.5,Σw) 会把分数压小。
       本脚本 Q3 专门量化这个压缩。
    """
    num = den = 0.0
    for k, w in wmap.items():
        if w <= 0:
            continue
        v = vals.get(k)
        if not _num(v):
            continue
        mx = DMAX.get(k) or 10.0
        num += min(float(v), mx) / mx * 10.0 * w
        den += w
    return (num / max(0.5, den) * 10.0) if den > 0 else None


def load_px():
    p = os.path.join(HERE, "data", "klines_long.json")
    raw = json.load(open(p, encoding="utf-8"))
    px = {}
    for c, m in raw.items():
        c6 = str(c)[-6:]
        if len(c6) == 6 and c6.isdigit():
            px[c6] = {d: v[1] for d, v in m.items()
                      if v and len(v) >= 2 and _num(v[1]) and v[1] > 0}
    return px


def main():
    print("加载 round34 面板 ...")
    panel = json.load(open(os.path.join(HERE, "data", "panel_round34.json"),
                           encoding="utf-8"))
    dates = panel["dates"]
    rows = panel["rows"]
    print(f"  域 {len(panel['domain'])} 只 / {len(dates)} 截面")

    print("加载价格库 ...")
    px = load_px()
    tdays = sorted({d for m in px.values() for d in m})
    tidx = {d: i for i, d in enumerate(tdays)}
    print(f"  {len(px)} 只 / {len(tdays)} 交易日")

    tot_dec = .13 + .10 + .06 + .075
    PLANS = {
        "A 现行 8 维": dict(W8),
        "B 按裁决比例摊平": {"score_lowvol20": .13 / tot_dec,
                        "score_size_ind": .10 / tot_dec,
                        "score_quality": .06 / tot_dec,
                        "score_fund": .075 / tot_dec},
        "C 4 维等权": {"score_lowvol20": .25, "score_size_ind": .25,
                    "score_quality": .25, "score_fund": .25},
        "D 按 IC 强度加权": {"score_lowvol20": .30, "score_size_ind": .35,
                        "score_quality": .15, "score_fund": .20},
        "E 保留质量 0.12": {"score_lowvol20": .30, "score_size_ind": .33,
                        "score_quality": .12, "score_fund": .25},
        # 严格按裁决但**不带缺口**：只保留 4 维中真正 keep 的 3 维（质量降权）
        "F 3 维 keep 归一": {"score_lowvol20": .13 / .305, "score_size_ind": .10 / .305,
                          "score_fund": .075 / .305},
        # 排除 fund（覆盖率最低且带选样偏差），3 维按 IC 强度
        "G 排除fund·3维IC权": {"score_lowvol20": .13 / .36, "score_size_ind": .10 / .36,
                           "score_quality": .13 / .36},
    }

    def comp(wmap, hop, want_fund=None):
        """逐截面 IC。

        want_fund=None  不限（与 decide_round34._comp 同口径，供复核对齐）
        want_fund=True  仅保留**该截面有 fund 数据**的截面
        want_fund=False 仅保留该截面**无 fund 数据**的截面
        """
        out = []
        for d0 in dates:
            i0 = tidx.get(d0)
            if i0 is None or i0 + hop >= len(tdays):
                continue
            rd = rows.get(d0) or {}
            if want_fund is not None:
                has = any(_num(v.get("score_fund")) for v in rd.values())
                if has != want_fund:
                    continue
            xs, ys = [], []
            for c, v in rd.items():
                # 与 decide_round34 一致：以 size_ind 存在为入样前提
                if not _num(v.get("score_size_ind")):
                    continue
                s = prod_score(v, wmap)
                if s is None:
                    continue
                m = px.get(c)
                if not m:
                    continue
                p0, p1 = m.get(d0), m.get(tdays[i0 + hop])
                if not p0 or not p1:
                    continue
                xs.append(s)
                ys.append(p1 / p0 - 1.0)
            if len(xs) >= 40:
                ic = fe.rank_ic(xs, ys)
                if ic is not None:
                    out.append(ic)
        return out

    def stat(a):
        if not a:
            return None, None
        return sum(a) / len(a), nw_t(a, 0)

    RES = {"plans": {}, "split": {}, "scale": {}}

    # ── Q0：与 decide_round34 逐位复核对齐（不限 fund）
    print("\n" + "=" * 104)
    print("【Q0】与 decide_round34 复核对齐（不限 fund 的混合样本口径）")
    print("=" * 104)
    print(f"  {'方案':22s} {'n_h5':>6s} {'h5 IC':>9s} {'n_h20':>6s} {'h20 IC':>9s}")
    for nm, wm in PLANS.items():
        m5, _ = stat(comp(wm, 5))
        m20, _ = stat(comp(wm, 20))
        print(f"  {nm:22s} {len(comp(wm,5)):6d} {m5 if m5 else 0:+9.4f} "
              f"{len(comp(wm,20)):6d} {m20 if m20 else 0:+9.4f}")
        RES["plans"][nm] = {"n_h5": len(comp(wm, 5)), "h5_ic": round(m5, 4) if m5 else None,
                            "n_h20": len(comp(wm, 20)), "h20_ic": round(m20, 4) if m20 else None}

    # ── Q1：按 fund 有无拆分
    print("\n" + "=" * 104)
    print("【Q1】按『该截面是否有 fund 数据』拆分（暴露混合平均的真相）")
    print("=" * 104)
    print(f"  {'方案':22s} {'有fund n':>9s} {'有fund h20':>11s} {'无fund n':>9s} {'无fund h20':>11s}")
    for nm in ("A 现行 8 维", "C 4 维等权", "D 按 IC 强度加权"):
        wm = PLANS[nm]
        a = comp(wm, 20, want_fund=True)
        b = comp(wm, 20, want_fund=False)
        ma, _ = stat(a)
        mb, _ = stat(b)
        print(f"  {nm:22s} {len(a):9d} {ma if ma else 0:+11.4f} "
              f"{len(b):9d} {mb if mb else 0:+11.4f}")
        RES["split"][nm] = {"n_fund": len(a), "h20_with_fund": round(ma, 4) if ma else None,
                            "n_nofund": len(b), "h20_no_fund": round(mb, 4) if mb else None}

    # ── Q2：同质样本公平对比（只用有 fund 的截面）
    print("\n" + "=" * 104)
    print("【Q2】同质样本公平对比（只用『有 fund』的截面 —— 唯一可直接比的口径）")
    print("=" * 104)
    print(f"  {'方案':22s} {'n':>5s} {'h5 IC':>9s} {'h5 t':>7s} {'h20 IC':>9s} {'h20 t':>7s}")
    fair = {}
    for nm, wm in PLANS.items():
        a5 = comp(wm, 5, want_fund=True)
        a20 = comp(wm, 20, want_fund=True)
        m5, t5 = stat(a5)
        m20, t20 = stat(a20)
        fair[nm] = {"n5": len(a5), "h5": m5, "t5": t5, "n20": len(a20), "h20": m20, "t20": t20}
        print(f"  {nm:22s} {len(a20):5d} {m5 if m5 else 0:+9.4f} {t5 if t5 else 0:+7.2f} "
              f"{m20 if m20 else 0:+9.4f} {t20 if t20 else 0:+7.2f}")
    RES["fair_with_fund"] = {k: {kk: (round(vv, 4) if isinstance(vv, float) else vv)
                                 for kk, vv in v.items()} for k, v in fair.items()}

    # ── Q3：Σw < 0.5 的分数压缩量化
    print("\n" + "=" * 104)
    print("【Q3】Σw < 0.5 时 max(0.5, den) 造成的分数压缩（决定 Σ 能否直接取 0.365）")
    print("=" * 104)
    print(f"  {'Σw':>7s} {'den 生效值':>10s} {'满分上限':>9s} {'85分需原始':>11s} {'结论':>26s}")
    for sw in (1.0, 0.5, 0.365, 0.305, 0.30):
        den = max(0.5, sw)
        cap = sw / den * 100.0
        need = (85.0 / cap * 100.0) if cap > 0 else None
        verdict = "无压缩" if sw >= 0.5 else f"分数 ×{sw/0.5:.3f}，S档不可达"
        RES["scale"][str(sw)] = {"den": den, "cap": round(cap, 2),
                                 "need_for_85": round(need, 1) if need else None,
                                 "verdict": verdict}
        print(f"  {sw:7.3f} {den:10.3f} {cap:9.2f} "
              f"{(f'{need:.1f}' if need else '—'):>11s} {verdict:>26s}")

    json.dump(RES, open(os.path.join(HERE, "verify_plan_c.json"), "w",
                        encoding="utf-8"), ensure_ascii=False, indent=2)
    print("\n已写出 verify_plan_c.json")


if __name__ == "__main__":
    main()
