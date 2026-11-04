#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
earnings_surprise.py —— 业绩预期差三档判定（超预期 / 符合预期 / 低于预期）
=========================================================================

判定对象
--------
比较**预测增速**与**实际增速**（均为「单季同比 %」口径）的差异，输出三档结论。
  - 预测增速 fwd：2026Q3 机构一致预测同比增速，来源 data/q3_forecast_*.json
    （手工汇总表 PIT 快照，2026-10-14，早于三季报披露）。
  - 实际增速 act：data/fin_cache_em/*.json 定期报告累计值自拆单季后计算：
        Q3单季 = 累计(20260930) − 累计(20260630)
        act_yoy = (Q3单季_2026 / Q3单季_2025 − 1) × 100   （去年同季 > 0 时）
  - 口径对齐：预测标「扣非」(b_np_kf / a_np 含"扣非") → 实际取扣非净利润，
    否则取归母净利润。禁跨口径比较。

判定标准与阈值
--------------
Δ = act_yoy − fwd_mid（百分点, pp）。预测带 band 由预测形态构造：

  ┌──────────────┬──────────────────────────┬──────────────────────────┐
  │ 预测形态      │ 预测带 band               │ 判定规则                  │
  ├──────────────┼──────────────────────────┼──────────────────────────┤
  │ 区间预测      │ [lo, hi]（Excel 原始端点） │ act > hi → 超预期         │
  │              │                          │ act < lo → 低于预期       │
  │              │                          │ 带内    → 符合预期        │
  ├──────────────┼──────────────────────────┼──────────────────────────┤
  │ 点值预测      │ [v−10, v+10]（±10pp 等效带）│ 同上（Δ≥+10 / Δ≤−10 / 其间）│
  ├──────────────┼──────────────────────────┼──────────────────────────┤
  │ 扭亏 turnaround│ 无增速数值（方向性预测）   │ 实际单季净利>0 → 符合预期  │
  │              │                          │ 实际≤0       → 低于预期    │
  │              │                          │ （无幅度预测，不设超预期档）│
  └──────────────┴──────────────────────────┴──────────────────────────┘

阈值校准依据（非拍脑袋）：
  - ±10pp 等效带 ≈ 手工表区间预测的典型半宽（如 +93%~+113%）；
  - q3_forecast A/B 双机构预测分歧 n=23：中位 13.7pp、p25 3.1pp —— ±10pp
    约等于单机构预测噪声带，超出噪声带的偏差才称「意外」；
  - strength 强信号阈值 25pp ≈ 2×噪声带（约达 A/B 分歧 p75=39.6pp 下沿）。

A/B 双机构合并规则（fwd_mid / band）
------------------------------------
  - 双方数值分歧 < 25pp（视为同一观点的正常分歧）：
      mid = 双方中值均值（与 step2 merge_q3_yoy 同口径，避免同屏两个"预测增速"）；
      band = [min(各方 lo), max(各方 hi)]（并集：任一机构认可即算符合，偏保守）。
  - 双方分歧 ≥ 25pp（如兆易创新 A 70~90% vs B +764%）：
      **不取均值**（均值是双方都不认的假中枢），以 B 为主（无 B 则 A）判定，
      flags += ab_disagree_big，note 标注双方分歧。
  - 仅一方有数值 → 该方；仅 turnaround → 扭亏路径；全无 → 无法判定。

输入格式（judge_growth_surprise 纯函数）
----------------------------------------
  fwd = {
      "mid": float|None,        # 预测中枢（%），区间取中值；turnaround 为 None
      "lo": float|None, "hi": float|None,   # 预测区间端点（%），点值时 lo=hi=v
      "kind": "point"|"range"|"turnaround"|"none"|"suspect",
      "caliber": "kf"|"gs",     # 预测口径：扣非 / 归母
      "source": "a"|"b"|"ab",   # 预测来源（合并后留档）
  }
  act = {
      "yoy": float|None,        # 实际单季同比增速（%）；负基数时为 None
      "np_q": float|None,       # 实际单季净利润（亿元），扭亏判定用
      "caliber": "kf"|"gs",     # 实际口径（须与 fwd 对齐）
      "period": "2026Q3",
  }
  单位约定：增速全部为百分数数值（160.8 表示 +160.8%），净利润为亿元。

输出结构（返回 dict，亦即 scored json 的 es 字段）
--------------------------------------------------
  {
    "tier":       "超预期"|"符合预期"|"低于预期"|"无法判定",
    "delta_pp":   float|None,   # act − fwd_mid（百分点）
    "band":       [lo, hi]|None,# 判定用预测带
    "fwd_mid":    float|None,   # 参与判定的预测中枢
    "act_yoy":    float|None,
    "act_np_q":   float|None,   # 实际单季净利润（亿元）
    "strength":   "strong"|"normal"|None,   # |Δ|≥25pp 为 strong；符合预期恒 None
    "method":     "range_band"|"point_band"|"turnaround"
                  |"missing_fwd"|"missing_act"|"negative_base",
    "caliber":    "kf"|"gs",
    "period":     "2026Q3",
    "source":     "a"|"b"|"ab"|None,
    "flags":      [...],        # suspect / ab_disagree_big / caliber_mismatch / negative_base
    "dev_ratio":  float|None,   # (act−fwd)/|fwd| 参考值；|fwd|<5 时 None（防小分母爆炸）
    "note":       str,          # 人类可读判定说明
  }
  表示方式：页面徽标 🔴超预期 +48pp / ⚪符合预期 / 🟢低于预期 −12pp / 扭亏兑现；
  无法判定不展示徽标（缺数据不编造）。

数据纪律
--------
  - 缺预测/缺实际/负基数无定义 → tier="无法判定"，相关数值留 None，禁臆造；
  - 判定**不进总分**（权重 0）：入总分须先过 weight_admission_gate 五道门禁；
  - 模块失败必须大声告警，调用方禁静默降级。

用法
----
  python3 earnings_surprise.py                # 审计：全量判定分布 + 样例
  python3 earnings_surprise.py --attach scored.json   # 只写 es 字段（不动 total）
"""
import glob
import json
import os
import shutil
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))

# ===== 阈值（单一事实源；gen_report 展示文案从这里 import，禁两处各写一套）=====
POINT_BAND_PP = 10.0   # 点值预测等效带宽 ±10pp（≈手工表区间半宽 / 单机构噪声带）
STRONG_PP = 25.0       # |Δ|≥25pp → strong；同时是 A/B「大分歧」阈值（≈2×噪声带）
DEV_RATIO_MIN = 5.0    # |fwd_mid|<5% 时 dev_ratio=None（增速接近 0 时相对偏差爆炸）

TIERS = ("超预期", "符合预期", "低于预期", "无法判定")


def _is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _round(x, nd=1):
    return round(x, nd) if _is_num(x) else None


# ══════════════════════════════════════════════════════════════════════
# 1) 纯判定函数（无 I/O，输入输出见模块 docstring）
# ══════════════════════════════════════════════════════════════════════
def judge_growth_surprise(fwd, act):
    """比对预测增速与实际增速 → 三档判定 dict（结构见模块 docstring）。"""
    fwd = fwd or {}
    act = act or {}
    kind = fwd.get("kind") or "none"
    flags = list(fwd.get("flags") or [])
    caliber = act.get("caliber") or fwd.get("caliber") or "gs"
    if fwd.get("caliber") and act.get("caliber") and fwd["caliber"] != act["caliber"]:
        flags.append("caliber_mismatch")
    out = {
        "tier": "无法判定", "delta_pp": None, "band": None, "fwd_mid": None,
        "act_yoy": None, "act_np_q": None, "strength": None,
        "method": "missing_fwd", "caliber": caliber,
        "period": act.get("period") or "2026Q3",
        "source": fwd.get("source"), "flags": flags,
        "dev_ratio": None, "note": "",
    }
    act_yoy = act.get("yoy") if _is_num(act.get("yoy")) else None
    act_np_q = act.get("np_q") if _is_num(act.get("np_q")) else None
    out["act_yoy"] = _round(act_yoy, 2)
    out["act_np_q"] = _round(act_np_q, 3)

    # ---- 扭亏路径（预测无增速数值，按方向兑现判定，不设超预期档）----
    if kind == "turnaround":
        out["method"] = "turnaround"
        if act_np_q is None:
            out["note"] = "预测扭亏但实际单季净利润缺失，无法判定"
            return out
        if act_np_q > 0:
            out["tier"] = "符合预期"
            out["note"] = f"预测扭亏，实际单季净利 {act_np_q:.2f} 亿>0，方向兑现（无幅度预测，不设超预期档）"
        else:
            out["tier"] = "低于预期"
            out["note"] = f"预测扭亏，实际单季净利 {act_np_q:.2f} 亿≤0，扭亏未兑现"
        return out

    # ---- 数值路径：预测缺失 ----
    fwd_mid = fwd.get("mid") if _is_num(fwd.get("mid")) else None
    if fwd_mid is None or kind in ("none", None):
        out["method"] = "missing_fwd"
        out["note"] = "无机构预测增速，无法判定"
        return out

    # ---- 实际缺失 / 负基数（增速无定义）----
    if act_yoy is None:
        if act.get("negative_base"):
            out["method"] = "negative_base"
            out["flags"] = flags + ["negative_base"]
            out["note"] = "去年同期单季净利润≤0，同比增速无定义，无法判定"
        else:
            out["method"] = "missing_act"
            _why = act.get("src_note") or "定期报告未披露或数据不可得"
            out["note"] = f"实际单季同比增速缺失（{_why}），无法判定"
        return out

    # ---- 预测带构造 ----
    lo, hi = fwd.get("lo"), fwd.get("hi")
    if _is_num(lo) and _is_num(hi) and hi >= lo and (hi - lo) > 1e-9:
        band = (float(lo), float(hi))
        method = "range_band"
    else:
        band = (fwd_mid - POINT_BAND_PP, fwd_mid + POINT_BAND_PP)
        method = "point_band"

    delta_pp = act_yoy - fwd_mid
    if act_yoy > band[1]:
        tier = "超预期"
    elif act_yoy < band[0]:
        tier = "低于预期"
    else:
        tier = "符合预期"

    strength = None
    if tier != "符合预期":
        strength = "strong" if abs(delta_pp) >= STRONG_PP else "normal"

    dev_ratio = None
    if abs(fwd_mid) >= DEV_RATIO_MIN:
        dev_ratio = (act_yoy - fwd_mid) / abs(fwd_mid)

    out.update({
        "tier": tier, "delta_pp": _round(delta_pp, 1),
        "band": [_round(band[0], 1), _round(band[1], 1)],
        "fwd_mid": _round(fwd_mid, 1), "method": method, "strength": strength,
        "dev_ratio": (_round(dev_ratio, 3) if dev_ratio is not None else None),
    })
    _suf = f"（{strength}）" if strength else ""
    out["note"] = (
        f"实际单季同比 {act_yoy:+.1f}% vs 预测中枢 {fwd_mid:+.1f}%（Δ={delta_pp:+.1f}pp），"
        f"预测带 [{band[0]:.1f}, {band[1]:.1f}] → {tier}{_suf}")
    return out


# ══════════════════════════════════════════════════════════════════════
# 2) 数据装配：q3_forecast → fwd；fin_cache_em → act
# ══════════════════════════════════════════════════════════════════════
def _side_from_rec(rec, pre):
    """q3_forecast 单侧 → (mid, lo, hi, kind, caliber) | None（无数值且非扭亏）。"""
    kind = rec.get(f"{pre}_kind") or "none"
    mid = rec.get(f"{pre}_yoy")
    lo, hi = rec.get(f"{pre}_lo"), rec.get(f"{pre}_hi")
    # 口径：B 侧 b_np_kf；A 侧看 a_np 文本是否含"扣非"
    np_raw = rec.get(f"{pre}_np") or ""
    if pre == "b":
        caliber = "kf" if rec.get("b_np_kf") else "gs"
    else:
        caliber = "kf" if "扣非" in str(np_raw) else "gs"
    if kind == "turnaround":
        return {"kind": "turnaround", "caliber": caliber}
    if not _is_num(mid):
        return None
    return {"kind": "suspect" if kind == "suspect" else ("range" if (_is_num(lo) and _is_num(hi) and abs((hi or 0) - (lo or 0)) > 1e-9) else "point"),
            "mid": float(mid),
            "lo": (float(lo) if _is_num(lo) else float(mid)),
            "hi": (float(hi) if _is_num(hi) else float(mid)),
            "caliber": caliber}


def _side_band(s):
    """单侧判定带：区间用真实端点；点值等效为 v±POINT_BAND_PP（噪声带）。"""
    if abs(s["hi"] - s["lo"]) > 1e-9:
        return s["lo"], s["hi"]
    return s["mid"] - POINT_BAND_PP, s["mid"] + POINT_BAND_PP


def build_fwd_from_rec(rec):
    """q3_forecast 记录 → judge 用 fwd 结构（A/B 合并规则见模块 docstring）。"""
    a = _side_from_rec(rec, "a")
    b = _side_from_rec(rec, "b")
    flags = []
    flag = rec.get("flag")
    if flag == "suspect":
        flags.append("suspect")

    nums = [s for s in (a, b) if s and s.get("kind") != "turnaround"]
    turns = [s for s in (a, b) if s and s.get("kind") == "turnaround"]

    if not nums:
        if turns:
            src = "ab" if len(turns) == 2 else ("a" if turns[0] is a else "b")
            return {"kind": "turnaround", "mid": None, "lo": None, "hi": None,
                    "caliber": turns[0]["caliber"], "source": src, "flags": flags}
        return {"kind": "none", "mid": None, "lo": None, "hi": None,
                "caliber": "gs", "source": None, "flags": flags}

    if len(nums) == 2:
        s1, s2 = nums[0], nums[1]
        spread = abs(s1["mid"] - s2["mid"])
        if spread < STRONG_PP:
            # 正常分歧：mid=均值（同 merge_q3_yoy），band=两侧噪声带并集
            # （点值侧先扩 ±10 再并，「任一机构认可即算符合」，偏保守）
            mid = (s1["mid"] + s2["mid"]) / 2.0
            b1, b2 = _side_band(s1), _side_band(s2)
            lo, hi = min(b1[0], b2[0]), max(b1[1], b2[1])
            any_range = any(abs(s["hi"] - s["lo"]) > 1e-9 for s in (s1, s2))
            return {"kind": "range" if any_range else "point",
                    "mid": mid, "lo": lo, "hi": hi,
                    "caliber": s2["caliber"], "source": "ab", "flags": flags}
        # 大分歧：不取均值（假中枢），以 B（nums 里 b 侧）为主
        primary = b if (b and b.get("kind") != "turnaround") else s1
        flags.append("ab_disagree_big")
        return {"kind": primary["kind"], "mid": primary["mid"],
                "lo": primary["lo"], "hi": primary["hi"],
                "caliber": primary["caliber"],
                "source": ("b" if primary is b else "a"),
                "flags": flags}

    s = nums[0]
    return {"kind": s["kind"], "mid": s["mid"], "lo": s["lo"], "hi": s["hi"],
            "caliber": s["caliber"],
            "source": ("a" if s is a else "b"), "flags": flags}


def _cum_metric(rec, metric, period):
    """fin_cache_em 记录 → 指定指标报告期累计值（元）；缺失返回 None。"""
    for r in rec.get("records", []):
        if r.get("指标") == metric:
            v = r.get(period)
            if _is_num(v):
                return float(v)
    return None


def build_act_from_em(code6, caliber, period_ymd="20260930", prev_h1="20260630",
                      base_ymd="20250930", base_h1="20250630"):
    """fin_cache_em → act 结构：单季同比增速 + 单季净利润（亿元）。

    单季 = 累计差分（growth_factor_b._single_quarter 同法，不引入第二实现口径）。
    去年同季 ≤ 0 → yoy=None + negative_base=True（增速无定义，禁除小数/负基数）。
    """
    p = os.path.join(HERE, "data", "fin_cache_em", f"{code6}.json")
    act = {"yoy": None, "np_q": None, "caliber": caliber, "period": "2026Q3",
           "negative_base": False, "src_note": ""}
    if not os.path.exists(p):
        act["src_note"] = "无财务缓存文件"
        return act
    try:
        with open(p, encoding="utf-8") as f:
            rec = json.load(f)
    except Exception:
        act["src_note"] = "财务缓存读取失败"
        return act
    metric = "扣非净利润" if caliber == "kf" else "归母净利润"
    vals = [_cum_metric(rec, metric, k) for k in (period_ymd, prev_h1, base_ymd, base_h1)]
    if any(v is None for v in vals):
        miss = [k for k, v in zip((period_ymd, prev_h1, base_ymd, base_h1), vals) if v is None]
        act["src_note"] = f"定期报告累计值缺失（{','.join(miss)}，三季报未披露或未回填）"
        return act
    q_cur = vals[0] - vals[1]
    q_base = vals[2] - vals[3]
    act["np_q"] = q_cur / 1e8
    act["base_np_q"] = q_base / 1e8
    if q_base > 0:
        act["yoy"] = (q_cur / q_base - 1.0) * 100.0
    else:
        act["negative_base"] = True
    return act


def _load_forecast():
    """取最新 q3_forecast_*.json 的 data 字典。"""
    fs = sorted(glob.glob(os.path.join(HERE, "data", "q3_forecast_*.json")))
    if not fs:
        return {}
    with open(fs[-1], encoding="utf-8") as f:
        return json.load(f).get("data", {}) or {}


def compute_es_map(name2code):
    """全量装配：{name: es_dict}（含 name/code 便于落库）。

    name2code: {股票名称: "sh600xxx"/"sz000xxx"/纯6位} —— 来自 scored 池。
    """
    fc = _load_forecast()
    out = {}
    for name, rec in fc.items():
        fwd = build_fwd_from_rec(rec)
        code = name2code.get(name)
        if code:
            c6 = str(code).strip().lower()
            if len(c6) > 6 and c6[:2] in ("sh", "sz", "bj"):
                c6 = c6[2:]
        else:
            c6 = None
        act = build_act_from_em(c6, fwd.get("caliber") or "gs") if c6 else {
            "yoy": None, "np_q": None, "caliber": fwd.get("caliber") or "gs",
            "period": "2026Q3", "negative_base": False,
            "src_note": "不在评分池（无代码映射），无法取实际值"}
        es = judge_growth_surprise(fwd, act)
        es["name"] = name
        es["code"] = code
        out[name] = es
    return out


# ══════════════════════════════════════════════════════════════════════
# 3) CLI：审计分布 / --attach 只写 es 字段
# ══════════════════════════════════════════════════════════════════════
def _audit():
    # 名称→代码从最新 scored 取
    fs = [f for f in sorted(glob.glob(os.path.join(HERE, "top200_scored_*.json")), reverse=True)
          if "_watch_" not in f and ".bak" not in f]
    name2code = {}
    if fs:
        with open(fs[0], encoding="utf-8") as f:
            for r in (json.load(f).get("results") or []):
                if r.get("name") and r.get("code"):
                    name2code[r["name"]] = r["code"]
    es_map = compute_es_map(name2code)
    from collections import Counter
    tiers = Counter(e["tier"] for e in es_map.values())
    methods = Counter(e["method"] for e in es_map.values())
    print(f"🎯 业绩预期差三档审计（{len(es_map)} 只有预测记录）")
    print("   tier 分布:", dict(tiers))
    print("   method 分布:", dict(methods))
    print("   在池内:", sum(1 for e in es_map.values() if e.get("code")))
    print("\n   样例（按 |Δ| 降序前 10）：")
    ranked = sorted((e for e in es_map.values() if e.get("delta_pp") is not None),
                    key=lambda e: -abs(e["delta_pp"]))
    for e in ranked[:10]:
        print(f"   {e['name']:<8} {e['tier']:<5} Δ={e['delta_pp']:+7.1f}pp "
              f"fwd={e['fwd_mid']} band={e['band']} act={e['act_yoy']} {e.get('strength') or ''} "
              f"[{e['method']}]{' ⚠' + ','.join(e['flags']) if e['flags'] else ''}")
    print("\n   无法判定样例（前 5）：")
    for e in [x for x in es_map.values() if x["tier"] == "无法判定"][:5]:
        print(f"   {e['name']:<8} [{e['method']}] {e['note'][:60]}")
    return 0


def _attach(scored_path):
    with open(scored_path, encoding="utf-8") as f:
        data = json.load(f)
    results = data.get("results") or []
    name2code = {r["name"]: r["code"] for r in results if r.get("name") and r.get("code")}
    es_map = compute_es_map(name2code)
    # 备份（.bak 后缀避开 enhance 的 scored glob——与既有备份纪律一致）
    bak = scored_path + ".bak"
    shutil.copy2(scored_path, bak)
    n = 0
    for r in results:
        e = es_map.get(r.get("name"))
        if e:
            # 只写 es 字段，禁碰 total/rating/任何评分字段
            r["es"] = {k: e[k] for k in (
                "tier", "delta_pp", "band", "fwd_mid", "act_yoy", "act_np_q",
                "strength", "method", "caliber", "period", "source", "flags",
                "dev_ratio", "note")}
            n += 1
    data["es_meta"] = {
        "attached_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "n_marked": n, "n_pool": len(results),
        "thresholds": {"point_band_pp": POINT_BAND_PP, "strong_pp": STRONG_PP},
        "note": "业绩预期差三档（权重0不进总分）；预测=data/q3_forecast PIT，实际=fin_cache_em 单季同比",
    }
    with open(scored_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    from collections import Counter
    tiers = Counter(r["es"]["tier"] for r in results if r.get("es"))
    print(f"✅ es 字段已写入 {scored_path}（{n}/{len(results)} 只，备份 {os.path.basename(bak)}）")
    print("   tier 分布:", dict(tiers))
    return 0


def main():
    args = sys.argv[1:]
    if args and args[0] == "--attach":
        if len(args) < 2:
            print("用法: python3 earnings_surprise.py --attach scored.json")
            return 1
        return _attach(args[1])
    return _audit()


if __name__ == "__main__":
    sys.exit(main())
