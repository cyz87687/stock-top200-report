#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
verify_plan_c5.py —— 决定性扫描：反转块降权比例的最优值与 regime 稳健性

为什么要重做（前一版 verify_plan_c4.py 的 Q12 有两处缺陷）：
  ① 标签错位：`(1.0, "归零(反转块0)")` 里 scale=1.0 实为**保持反转块满权重**，
     却被标成"归零"，读表会完全反了。
  ② 漏维：REST 只含 lowvol/quality/size/fund（0.425），**漏了 EP(0.075)**，
     于是 scale=1.0 得 +0.0767 而非与 A 一致的 +0.0778，量尺不可比。

本脚本的正确做法：
  其余 5 维（lowvol / quality / size_ind / ep / fund）权重**原样保留**，
  只把反转块三合一（bias60 / rsi_rev / tech_neg）整体乘以 scale ∈ {0, .1, .25, .4, .5, .75, 1.0}，
  然后**归一到 Σ=1.0**（否则 max(0.5,den) 会压缩分数、S 档不可达）。
  对每个 scale 分别报：全样本 / 前段 / 后段 的 h20 IC 与 NW t。

判据（先写死，避免事后挑数）：
  · 全样本最优：h20 IC 最大
  · regime 稳健：前段 Δ 与后段 Δ 都 ≥ -0.005（即两段都不明显变差）
  · 采纳条件：全样本提升 ≥ +0.010 且两段均不劣化 ≥ -0.005 且归属到 ≤ 0.25 档

输出：verify_plan_c5.json
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import factor_eval as fe                      # noqa: E402
from audit_round29 import nw_t                # noqa: E402

DMAX = {"score_fund": 5.0, "score_bias60": 5.0, "score_rsi_rev": 5.0,
        "score_quality": 10.0, "score_lowvol20": 10.0, "score_size_ind": 10.0,
        "score_tech_neg": 10.0, "score_ep": 10.0}
W8 = {"score_bias60": .175, "score_rsi_rev": .175, "score_tech_neg": .15,
      "score_lowvol20": .13, "score_quality": .12, "score_size_ind": .10,
      "score_ep": .075, "score_fund": .075}
REV = ("score_bias60", "score_rsi_rev", "score_tech_neg")
SPLIT = "2026-06-26"


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def prod_score(vals, wmap):
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
    raw = json.load(open(os.path.join(HERE, "data", "klines_long.json"), encoding="utf-8"))
    px = {}
    for c, m in raw.items():
        c6 = str(c)[-6:]
        if len(c6) == 6 and c6.isdigit():
            px[c6] = {d: v[1] for d, v in m.items()
                      if v and len(v) >= 2 and _num(v[1]) and v[1] > 0}
    return px


def main():
    print("加载面板 / 价格 ...")
    panel = json.load(open(os.path.join(HERE, "data", "panel_round34.json"), encoding="utf-8"))
    dates, rows = panel["dates"], panel["rows"]
    px = load_px()
    tdays = sorted({d for m in px.values() for d in m})
    tidx = {d: i for i, d in enumerate(tdays)}

    def series(wmap, hop, seg=None):
        out = []
        for d0 in dates:
            i0 = tidx.get(d0)
            if i0 is None or i0 + hop >= len(tdays):
                continue
            if seg == "pre" and d0 >= SPLIT:
                continue
            if seg == "post" and d0 < SPLIT:
                continue
            rd = rows.get(d0) or {}
            xs, ys = [], []
            for c, v in rd.items():
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

    def st(a):
        if not a:
            return None, None
        return sum(a) / len(a), nw_t(a, 0)

    def mk(scale):
        """反转块 × scale，其余 5 维原样；归一到 Σ=1。"""
        w = {}
        for k, v in W8.items():
            w[k] = v * scale if k in REV else v
        w = {k: v for k, v in w.items() if v > 0}
        s = sum(w.values())
        return {k: v / s for k, v in w.items()}

    SCALES = [0.0, 0.10, 0.25, 0.40, 0.50, 0.75, 1.00]
    RES = {"split_date": SPLIT, "sweep": {}}

    # ⚠️ 基准必须先单独算出来：SCALES 里 1.00 在最后，
    #    若在同一循环里边算边比，前面的档位全部拿不到基准（本轮踩过）。
    _wb = mk(1.00)
    base_all, _ = st(series(_wb, 20))
    base_pre, _ = st(series(_wb, 20, "pre"))
    base_post, _ = st(series(_wb, 20, "post"))
    print(f"\n基准（现行 8 维，反转占比 0.500）：全样本 {base_all:+.4f} / "
          f"前段 {base_pre:+.4f} / 后段 {base_post:+.4f}")

    print("\n" + "=" * 112)
    print("【决定性扫描】反转块整体降权 × scale，其余 5 维不变，归一到 Σ=1")
    print("=" * 112)
    print(f"  {'反转倍数':>8s} {'反转占比':>8s} {'全样本h20':>10s} {'t':>7s} "
          f"{'前段h20':>9s} {'后段h20':>9s} {'前段Δ':>8s} {'后段Δ':>8s} {'判读':>14s}")
    for sc in SCALES:
        wm = mk(sc)
        a_all = series(wm, 20)
        a_pre = series(wm, 20, "pre")
        a_post = series(wm, 20, "post")
        m_all, t_all = st(a_all)
        m_pre, _ = st(a_pre)
        m_post, _ = st(a_post)
        rev_share = sum(v for k, v in wm.items() if k in REV)
        RES["sweep"][f"{sc:.2f}"] = {
            "rev_share": round(rev_share, 4), "n": len(a_all),
            "h20_all": round(m_all, 4) if m_all is not None else None,
            "t_all": round(t_all, 2) if t_all is not None else None,
            "h20_pre": round(m_pre, 4) if m_pre is not None else None,
            "h20_post": round(m_post, 4) if m_post is not None else None}
        d_pre = (m_pre - base_pre) if (m_pre is not None and base_pre is not None) else None
        d_post = (m_post - base_post) if (m_post is not None and base_post is not None) else None
        RES["sweep"][f"{sc:.2f}"]["d_pre"] = round(d_pre, 4) if d_pre is not None else None
        RES["sweep"][f"{sc:.2f}"]["d_post"] = round(d_post, 4) if d_post is not None else None
        if sc == 1.00:
            v = "基准(现行)"
        elif d_pre is not None and d_post is not None and d_pre >= -0.005 and d_post >= -0.005:
            v = "两段皆不劣化 ✅"
        elif d_post is not None and d_post < -0.005:
            v = "后段劣化 ❌"
        else:
            v = "前段劣化"
        RES["sweep"][f"{sc:.2f}"]["verdict"] = v
        print(f"  {sc:8.2f} {rev_share:8.3f} "
              f"{(m_all if m_all is not None else 0):+10.4f} "
              f"{(t_all if t_all is not None else 0):+7.2f} "
              f"{(m_pre if m_pre is not None else 0):+9.4f} "
              f"{(m_post if m_post is not None else 0):+9.4f} "
              f"{(d_pre if d_pre is not None else 0):+8.4f} "
              f"{(d_post if d_post is not None else 0):+8.4f} {v:>14s}")

    # ── 采纳判定（规则前置）
    print("\n" + "=" * 112)
    print("【采纳判定】预先写死的三条：全样本提升≥+0.010、两段均不劣化≥-0.005、占比≤0.25")
    print("=" * 112)
    cands = []
    for sc in SCALES:
        r = RES["sweep"][f"{sc:.2f}"]
        if sc == 1.00 or r["h20_all"] is None:
            continue
        gain = r["h20_all"] - base_all
        dp = r["d_pre"] if r["d_pre"] is not None else -9.0
        dq = r["d_post"] if r["d_post"] is not None else -9.0
        ok = (gain >= 0.010 and dp >= -0.005 and dq >= -0.005
              and r["rev_share"] <= 0.25)
        cands.append((sc, r["rev_share"], r["h20_all"], gain, dp, dq, ok))
        print(f"  scale={sc:.2f} 占比={r['rev_share']:.3f} h20={r['h20_all']:+.4f} "
              f"增益={gain:+.4f} 前段Δ={dp:+.4f} 后段Δ={dq:+.4f} "
              f"{'✅采纳' if ok else '不采纳'}")
    RES["base_all"] = round(base_all, 4) if base_all is not None else None
    feasible = [c for c in cands if c[6]]
    if feasible:
        best = max(feasible, key=lambda x: x[2])
        RES["adopt"] = {"scale": best[0], "rev_share": best[1],
                        "h20_all": best[2], "gain": round(best[3], 4),
                        "d_pre": best[4], "d_post": best[5]}
        print(f"\n  → 建议采纳 scale={best[0]:.2f}（反转块占比 {best[1]:.3f}），"
              f"全样本 h20 {best[2]:+.4f}（+{best[3]:.4f}）")
    else:
        RES["adopt"] = None
        print("\n  → 无方案同时满足三条 → **不采纳任何降权**，维持现行权重并在页面披露 regime 依赖")

    json.dump(RES, open(os.path.join(HERE, "verify_plan_c5.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print("\n已写出 verify_plan_c5.json")


if __name__ == "__main__":
    main()
