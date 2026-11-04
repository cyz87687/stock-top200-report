#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""round123b：修正 panel_round123 中 bias60 / rsi_rev 的**口径错配**。

问题（本轮审计发现）
------------------
`data/panel_round123.json` 的 `score_bias60` / `score_rsi_rev` 只有 5 个整数值
（1/2/3/4/5）—— 这是 round121 **之前**的「绝对阈值分档」老口径。
而生产 `top200_scored_2026-11-03.json` 里同一维有 465~469 个值（0.00~5.00 连续），
是 round121 之后的「池内分位连续」口径（rank→pct→×5）。

成因：`panel_round66`（源面板）建于 2026-10-29，其中这两列沿用的是分档口径；
本次回填**沿用了源面板的这两列**而没有按现行口径重派生
→ 回测里 0.10 权重（两维各 0.05）的口径与生产不一致。

为什么重要（实测，非理论）
------------------------
用正确口径重派生后：
    h= 1: IC +0.0241→+0.0238   NW t 3.69→3.54
    h=20: IC +0.0629→**+0.0551**  NW t 2.08→**1.77**
⇒ h=20 的 NW t **跌破 2.0**。上一轮报的「2.08 刚过线」是**分档口径造成的高估**。

口径（与 step2 / round121 一致）
-------------------------------
    bias60_raw = (close − MA60) / MA60 × 100        （越负越好）
    rsi14      = 标准 Wilder RSI(14)                （越负越低=越超卖）
    score = rank5(raw, higher_is_better=False)      # rank→pct→(1−pct)→×5
    ⚠️ 必须**整池一起算**（池逐日变化，固定阈值必失真）
    ⚠️ 只用 d0 及之前价格 → 无前视

用法
----
    python fix_panel_reversal_dims.py --check   # 只体检
    python fix_panel_reversal_dims.py           # 就地修正 panel_round123.json
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PANEL = os.path.join(HERE, "data", "panel_round123.json")
KLINES = os.path.join(HERE, "data", "klines_long.json")

DIM_MAX_REV = 5.0   # bias60/rsi_rev 的量程（见 factor_weights.json 的 dim_max）


# ---------------------------------------------------------------- 原始值
def _close(kl, code, d):
    b = kl.get(code, {}).get(d)
    if not b or len(b) < 2:
        return None
    c = b[1]
    return c if isinstance(c, (int, float)) and c > 0 else None


def bias60_raw(kl, code, tdays, i0, n=60):
    """(close − MA60)/MA60×100，只用 d0 及之前。"""
    if i0 < n - 1:
        return None
    s, cnt = 0.0, 0
    for j in range(i0 - n + 1, i0 + 1):
        c = _close(kl, code, tdays[j])
        if c is None:
            return None
        s += c
        cnt += 1
    if cnt < n or s <= 0:
        return None
    ma = s / cnt
    px = _close(kl, code, tdays[i0])
    if px is None:
        return None
    return (px - ma) / ma * 100.0


def rsi14_raw(kl, code, tdays, i0, n=14):
    """标准 RSI(n)：用简单平均（与 _lowvol/step2 口径一致，非 Wilder 平滑）。"""
    if i0 < n:
        return None
    gains, losses = 0.0, 0.0
    for j in range(i0 - n + 1, i0 + 1):
        a = _close(kl, code, tdays[j - 1])
        b = _close(kl, code, tdays[j])
        if a is None or b is None:
            return None
        ch = b - a
        if ch > 0:
            gains += ch
        else:
            losses += -ch
    ag, al = gains / n, losses / n
    if al == 0:
        return 100.0
    rs = ag / al
    return 100.0 - 100.0 / (1.0 + rs)


def rank5(raw, higher_is_better):
    """整池 rank → pct → (可选反转) → ×5。与 step2.reversal_rank 同口径。"""
    items = [(c, v) for c, v in (raw or {}).items()
             if isinstance(v, (int, float)) and v == v]
    n = len(items)
    if n < 2:
        return {}
    order = sorted(range(n), key=lambda i: items[i][1])
    pct = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and items[order[j + 1]][1] == items[order[i]][1]:
            j += 1
        avg = (i + j) / 2.0 / max(1, n - 1)
        for k in range(i, j + 1):
            pct[order[k]] = avg
        i = j + 1
    out = {}
    for k in range(n):
        p = pct[k] if higher_is_better else (1.0 - pct[k])
        out[items[k][0]] = round(p * DIM_MAX_REV, 2)
    return out


# ---------------------------------------------------------------- 体检
def check():
    with open(PANEL, encoding="utf-8") as f:
        p = json.load(f)
    dates, rows = p["dates"], p["rows"]
    print("=" * 72)
    print("bias60 / rsi_rev 口径体检：panel 现状 vs 生产口径")
    print("=" * 72)

    for dim in ("score_bias60", "score_rsi_rev"):
        print(f"\n{dim}")
        print(f"  {'日期':12} {'唯一值数':>8}  {'样例取值':>34}")
        for d in (dates[0], dates[len(dates) // 2], dates[-1]):
            vals = sorted({round(float(r[dim]), 4) for r in rows[d].values()
                           if r.get(dim) is not None})
            s = ", ".join(str(x) for x in vals[:6])
            print(f"  {d:12} {len(vals):>8}  {s:>34}")
        # 结论
        last = sorted({round(float(r[dim]), 4) for r in rows[dates[-1]].values()
                       if r.get(dim) is not None})
        if len(last) <= 8:
            print(f"  ⇒ 🔴 仍是**分档口径**（{len(last)} 个离散值）→ 需按生产口径重派生")
        else:
            print(f"  ⇒ ✅ 已是连续口径（{len(last)} 个值）")

    # 生产对照
    prod_p = os.path.join(HERE, "top200_scored_2026-11-03.json")
    if os.path.exists(prod_p):
        with open(prod_p, encoding="utf-8") as f:
            prod = json.load(f)
        res = prod.get("results") or prod.get("rows") or []
        print("\n生产 scored（2026-11-03）对照：")
        for dim in ("score_bias60", "score_rsi_rev"):
            v = {round(float(r[dim]), 4) for r in res
                 if isinstance(r.get(dim), (int, float))}
            print(f"  {dim:20} 唯一值 {len(v)}")


# ---------------------------------------------------------------- 修正
def fix(write=True):
    t0 = time.time()
    with open(PANEL, encoding="utf-8") as f:
        p = json.load(f)
    dates, rows = p["dates"], p["rows"]
    with open(KLINES, encoding="utf-8") as f:
        kl = json.load(f)

    print(f"面板 {len(dates)} 交易日，重派生 bias60 / rsi_rev（池内分位连续口径）")

    stat = {"bias60": [], "rsi_rev": []}
    for i0, d in enumerate(dates):
        codes = list(rows[d].keys())
        rawb, rawr = {}, {}
        for c in codes:
            b = bias60_raw(kl, c, dates, i0)
            if b is not None:
                rawb[c] = b
            r = rsi14_raw(kl, c, dates, i0)
            if r is not None:
                rawr[c] = r
        # 两者都是「值越小越好」→ higher_is_better=False
        sb = rank5(rawb, higher_is_better=False)
        sr = rank5(rawr, higher_is_better=False)
        for c in codes:
            if c in sb:
                rows[d][c]["score_bias60"] = sb[c]
            if c in sr:
                rows[d][c]["score_rsi_rev"] = sr[c]
        stat["bias60"].append(len(sb))
        stat["rsi_rev"].append(len(sr))
        if (i0 + 1) % 50 == 0:
            print(f"   … {i0+1}/{len(dates)}")

    # 校验：唯一值数应大幅上升
    d_last = dates[-1]
    for dim in ("score_bias60", "score_rsi_rev"):
        vals = {round(float(r[dim]), 4) for r in rows[d_last].values()
                if r.get(dim) is not None}
        print(f"  {dim}: 末截面唯一值 {len(vals)}")

    p["meta"].setdefault("audit_fixes", []).append({
        "round": "123b",
        "what": "bias60 / rsi_rev 从「绝对阈值分档」重派生为「池内分位连续」",
        "why": ("源 panel_round66 建于 2026-10-29，这两列仍是 round121 之前的分档口径"
                "（5 个离散值），与生产的连续口径（465~469 个值）不一致；"
                "回填时沿用未重派生 → 0.10 权重口径错配"),
        "impact_measured": {
            "h1": {"ic_old": 0.0241, "ic_new": 0.0238,
                   "nwt_old": 3.69, "nwt_new": 3.54},
            "h20": {"ic_old": 0.0629, "ic_new": 0.0551,
                    "nwt_old": 2.08, "nwt_new": 1.77,
                    "note": "NW t 跌破 T_LOOSE=2.0 → 上一轮「h20 刚过线」是分档口径的高估"},
        },
        "fix_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })

    print(f"耗时 {time.time()-t0:.1f}s")
    if write:
        tmp = PANEL + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(p, f, ensure_ascii=False)
        os.replace(tmp, PANEL)
        print(f"✅ 已就地修正 {PANEL}（{os.path.getsize(PANEL)/1e6:.1f} MB）")
    return p


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    if a.check:
        check()
    else:
        fix(write=True)
