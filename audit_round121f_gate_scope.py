#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""round121f — 修正 round121 落地时确认门触发范围「证据外溢」

背景（自查发现，非外部指出）：
  round121 把「深跌 + 未过止跌确认门 → 罚 0.5 分」落地为
      _is_deep = (RSI < 30) **or** (bias <= -15)
  但当时引用的唯一实测证据只覆盖 `RSI < 30` 区间（+1.321pp / t_NW=3.75）。
  `bias <= -15` 这一支**从未被单独验证过**，属证据外溢。

本脚本做两件事：
  ① 从 audit_round121d_dim.json 的 C_confirm_increment 分区，取全部 6 个触发
     口径的「有确认 − 无确认」中位差与三重 t，判定哪一支真的有正向前瞻性；
  ② 用当前生产池的 K 线，统计每个口径实际会罚到多少只股票，
     量化「证据覆盖区间」与「代码实际作用区间」的错配规模。

结论（写入 data/audit_round121f_gate_scope.json）：
  并集口径 RSI<30|bias<=-15 的增量为负（h20 −0.216pp，t=−0.59），
  且 89% 的受罚股票落在「仅 bias<=-15」这一无证据支上 →
  触发范围必须收紧为**只 RSI<30**。

⚠️ 本脚本只读：不写任何生产数据文件（klines / scored / weights）。
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
D121D = os.path.join(HERE, "data", "audit_round121d_dim.json")
KLINE = os.path.join(HERE, "data", "klines_long.json")
SCORED = os.path.join(HERE, "top200_scored_2026-11-03.json")
OUT = os.path.join(HERE, "data", "audit_round121f_gate_scope.json")

# 与 step2 / 121d 完全一致的确认门口径
N_LOW = 20


def confirm_gate(closes, opens):
    """①未创 20 日收盘新低 ②收盘站上 MA5 ③当日收阳 —— 满足其一即确认。

    ⚠️ 刻意不含「站上 MA20」：495,587 观测里「站上 MA20」的最小 RSI=35.45，
       RSI<30 的 20,449 样本命中 0 个 → 结构性空集（见 round120 系列）。
    """
    if len(closes) < N_LOW + 1:
        return None
    cur = closes[-1]
    prev = closes[-(N_LOW + 1):-1]
    if len(prev) < N_LOW - 1:
        return None
    not_new_low = cur > min(prev) + 1e-12
    above_ma5 = cur > sum(closes[-5:]) / 5.0
    yang = opens[-1] is not None and cur > opens[-1]
    return bool(not_new_low or above_ma5 or yang)


def main():
    d121 = json.load(open(D121D, encoding="utf-8"))

    # ── ① 各触发口径的实测增量（直接取 121d，不重算 IC） ──
    measured = {}
    for h in ("h20", "h5"):
        C = d121["horizons"][h]["C_confirm_increment"]
        measured[h] = {
            k: {
                "n_confirmed": v["n_a"],
                "n_unconfirmed": v["n_b"],
                "n_days_paired": v["n_days_paired"],
                "diff_median_pp": v["diff_median_pp"],
                "t": v["t"],
                "t_nw": v["t_nw"],
                "t_sub": v["t_sub"],
                "n_days_sub": v["n_days_sub"],
                "verdict": ("正向前瞻" if v["diff_median_pp"] > 0 and v["t"] > 0
                            else "无证据 / 方向为负"),
            }
            for k, v in C.items()
        }

    # ── ② 生产池里各口径实际命中多少只 ──
    K = json.load(open(KLINE, encoding="utf-8"))
    rows = json.load(open(SCORED, encoding="utf-8"))["results"]

    cover = {
        "both": 0,          # RSI<30 且 bias<=-15
        "rsi_only": 0,      # 仅 RSI<30
        "bias_only": 0,     # 仅 bias<=-15
        "neither": 0,
        "gate_confirmed": 0,
        "gate_unconfirmed": 0,
        "gate_unknown": 0,
        "rsi_penalized": 0,      # 收紧后（只 RSI<30）实际罚分数
        "union_penalized": 0,    # 原并集口径会罚的只数
        "bias_only_penalized": 0,  # 其中「仅 bias<=-15」被误罚的只数
    }
    min_rsi_above_ma20 = None

    for r in rows:
        code = str(r.get("code") or "")[2:]
        kl = K.get(code)
        if not kl:
            continue
        dates = sorted(kl.keys())[-61:]
        opens = [kl[x][0] for x in dates]
        closes = [kl[x][1] for x in dates]
        if len(closes) < 60:
            continue

        m60 = sum(closes[-60:]) / 60.0
        bias = (closes[-1] - m60) / m60 * 100.0 if m60 > 0 else None
        rsi = (r.get("tech") or {}).get("rsi")

        rsi_deep = rsi is not None and rsi < 30
        bias_deep = bias is not None and bias <= -15.0

        if rsi_deep and bias_deep:
            cover["both"] += 1
        elif rsi_deep:
            cover["rsi_only"] += 1
        elif bias_deep:
            cover["bias_only"] += 1
        else:
            cover["neither"] += 1

        # 「站上MA20」的最小 RSI —— 必须在**全样本**上统计（曾在被罚分支里统计，
        #   导致恒为 None：真正站上 MA20 的股票本来就不会进「未确认」那一支）。
        if len(closes) >= 21 and rsi is not None:
            ma20 = sum(closes[-20:]) / 20.0
            if closes[-1] > ma20:
                if min_rsi_above_ma20 is None or rsi < min_rsi_above_ma20:
                    min_rsi_above_ma20 = rsi

        if not (rsi_deep or bias_deep):
            continue

        g = confirm_gate(closes, opens)
        if g is None:
            cover["gate_unknown"] += 1
            continue
        if g:
            cover["gate_confirmed"] += 1
            continue

        cover["gate_unconfirmed"] += 1
        cover["union_penalized"] += 1
        if rsi_deep:
            cover["rsi_penalized"] += 1
        else:
            cover["bias_only_penalized"] += 1

    n_pool = len(rows)
    out = {
        "round": "round121f",
        "purpose": "核查 round121 确认门触发范围是否超出实测证据区间",
        "trigger_rule_121_landed": "RSI<30 OR bias<=-15",
        "trigger_rule_121f_corrected": "RSI<30 （仅此一支）",
        "evidence_by_scope": measured,
        "pool_coverage": {
            **cover,
            "pool_size": n_pool,
            "union_penalty_share": round(cover["union_penalized"] / n_pool, 4),
            "corrected_penalty_share": round(cover["rsi_penalized"] / n_pool, 4),
            "bias_only_share_of_penalized": (
                round(cover["bias_only_penalized"] / cover["union_penalized"], 4)
                if cover["union_penalized"] else None),
        },
        "min_rsi_among_above_ma20": min_rsi_above_ma20,
        "verdict": {
            "h20_rsi_lt_30_pp": measured["h20"]["RSI<30"]["diff_median_pp"],
            "h20_bias_le_15_pp": measured["h20"]["bias<=-15"]["diff_median_pp"],
            "h20_union_pp": measured["h20"]["RSI<30|bias<=-15"]["diff_median_pp"],
            "h20_intersection_pp": measured["h20"]["RSI<30&bias<=-15"]["diff_median_pp"],
            "conclusion": (
                "bias<=-15 单独为负、并集为负，只有 RSI<30（及其与 bias 的交集）为正。"
                "故触发范围收紧为只 RSI<30；罚分覆盖率由 25.1% 降到 "
                f"{cover['rsi_penalized'] / n_pool * 100:.1f}%。"),
        },
        "caveats": [
            "RSI<30 分区的 t_sub 仅 1.55（不重叠子样本仅 8 天）→ 只做罚分、不做加分。",
            "bias<=-15 单独支 n_days_paired=197，不是样本不足，是方向确实为负。",
            "本裁决只改「罚分触发范围」，不改任何权重值与量程。",
        ],
        "source": {
            "measurements": "data/audit_round121d_dim.json#C_confirm_increment",
            "pool": "top200_scored_2026-11-03.json",
            "klines": "data/klines_long.json",
        },
    }

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print(f"✅ 写出 {OUT}")
    print(f"   h20  RSI<30            {measured['h20']['RSI<30']['diff_median_pp']:+.3f}pp  "
          f"t={measured['h20']['RSI<30']['t']:+.2f} t_NW={measured['h20']['RSI<30']['t_nw']:+.2f}")
    print(f"   h20  bias<=-15         {measured['h20']['bias<=-15']['diff_median_pp']:+.3f}pp  "
          f"t={measured['h20']['bias<=-15']['t']:+.2f} t_NW={measured['h20']['bias<=-15']['t_nw']:+.2f}")
    print(f"   h20  并集(原落地)       {measured['h20']['RSI<30|bias<=-15']['diff_median_pp']:+.3f}pp  "
          f"t={measured['h20']['RSI<30|bias<=-15']['t']:+.2f}")
    print(f"   h20  交集             {measured['h20']['RSI<30&bias<=-15']['diff_median_pp']:+.3f}pp  "
          f"t={measured['h20']['RSI<30&bias<=-15']['t']:+.2f}")
    print(f"   池内分布: 两者都深跌 {cover['both']} / 仅RSI {cover['rsi_only']} / "
          f"仅bias {cover['bias_only']} / 都不满足 {cover['neither']}")
    print(f"   罚分只数: 并集 {cover['union_penalized']} → 收紧后 {cover['rsi_penalized']}"
          f"（其中「仅bias」误罚 {cover['bias_only_penalized']}，占 "
          f"{out['pool_coverage']['bias_only_share_of_penalized']:.1%}）")
    print(f"   「站上MA20」的最小 RSI = {min_rsi_above_ma20}（支撑『不含MA20』的结构性空集论据）")


if __name__ == "__main__":
    main()
