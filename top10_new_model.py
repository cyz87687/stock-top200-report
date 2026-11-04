"""top10_new_model.py —— 按「新评分模型」输出前十大，**不改任何配置文件**。

## 为什么要同时给三套前十

本轮发现一处必须披露的口径缺陷（round125）：

`score_size_growth`（权重 0.20，第二大权重）对 514 只股票是**结构性不适用** ——
它们是「行业龙头」（业务份额组内前 30%，`dominant >= 7`）。按 v4.13 分区设计，
龙头走 `dominant` 维、**不拿小盘成长溢价**（`ep_size_ind.py:645` 注释原文：
「龙头仅 dominant(.10)」)。

但 `scale_calc.composite_total` 对「缺失」一律填中性 DIM_MAX/2 = 5.0 →
折算到百分制恒为 10.00 分。实测后果：

    龙头（514 只）：该维恒定贡献 10.00 分
    非龙头（1405 只）：该维均值 6.88 分，其中 440 只（31.3%）为 0 分
    ⇒ 龙头在这个 0.20 权重维上比非龙头均值多 +3.12 分，比 0 分股多 10.00 分

即「龙头仅 .10」在实现上变成「龙头 .10 + 白送 .20 的中性分」，市值大类暴露
不是 3 倍差，而是龙头反而占优。**这不是设计意图，是中性填充与分区设计的冲突。**

三种处理口径给出的前十只有 4 只重合 → 口径选择直接决定名单，
故本脚本**同时输出三套**，不替用户裁决：

    A（生产现行）缺失一律中性 5.0 —— 当前 `index.html` 与 scored 文件采用的口径
    B（语义从严）size_growth 缺失按 0 分 —— 照注释「龙头不给溢价」
    C（分母剔除）size_growth 对龙头不适用，不进分子也不进分母

## 两套权重

    v4.17（现行）  fund .20 / size_growth .20 / tech_neg .10 / quality .10 /
                   ep .10 / dominant .10 / growth .10 / bias60 .05 / rsi_rev .05
    C1（P0-4 推荐，未执行）tech_neg .10 → 0，等额移给 growth → growth .20

C1 实测（长面板 1653 日 × 2257 只、h=20、同 universe）：
    IC +0.0539 → +0.0485（−10%，代价）
    N10 −0.531% → +0.140%（+0.671pp）、N6 −0.879% → +0.250%（+1.129pp）
⚠️ IC 降而头部升 —— 全局排序 vs 最头部的目标冲突，不是矛盾。

## 口径校验

逐只用「生产权重 + 生产口径 A」复算 total，与文件 `total` 比对须 max abs 差 == 0。
不通过即中止（口径不同就不能拿它的分数排名）。
"""
import json
import os
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from dim_cfg import load_dim_max          # noqa: E402

SCORED = os.path.join(HERE, "top200_scored_2026-11-03.json")
OUT = os.path.join(HERE, "data", "top10_new_model.json")

with open(os.path.join(HERE, "factor_weights.json"), encoding="utf-8") as _f:
    _CFG = json.load(_f)
W_V417 = {k: float(v) for k, v in (_CFG.get("weights") or {}).items() if float(v) > 0}

W_C1 = dict(W_V417)
_tn = W_C1.pop("score_tech_neg", 0.0)
W_C1["score_growth"] = round(W_C1.get("score_growth", 0.0) + _tn, 10)

DIM_MAX = load_dim_max(strict=True)

# 结构性不适用维（龙头分区导致 None 不是「数据缺失」）
STRUCT_DIM = "score_size_growth"


def composite(rec, w, caliber="A"):
    """按指定口径重算 total（百分制）。

    caliber A：缺维一律中性 DIM_MAX/2，分母恒 Σ生效权重（= 生产现行）
    caliber B：STRUCT_DIM 缺失按 0 分（不填中性），其余同 A
    caliber C：STRUCT_DIM 缺失时该维不进分子也不进分母（分母按股变动）
    """
    num = 0.0
    den = 0.0
    missing = []
    for k, ww in w.items():
        if ww <= 0:
            continue
        mx = DIM_MAX.get(k) or 10.0
        v = rec.get(k)
        has = isinstance(v, (int, float))

        if not has and k == STRUCT_DIM:
            if caliber == "C":
                continue                    # 不适用 → 整个维度退出
            if caliber == "B":
                den += ww                   # 留在分母，按 0 分计
                continue

        den += ww
        if has:
            num += min(float(v), mx) / mx * 10.0 * ww
        else:
            num += 0.5 * 10.0 * ww
            missing.append(k)
    if den <= 0:
        return None, missing
    return round(num / den * 10.0, 2), missing


def rank_of(rows, w, caliber):
    scored = [(r, composite(r, w, caliber)[0]) for r in rows]
    scored = [(r, s) for r, s in scored if s is not None]
    scored.sort(key=lambda x: (-x[1], x[0]["code"]))
    return scored


def main():
    with open(SCORED, encoding="utf-8") as f:
        doc = json.load(f)
    rows = doc.get("results") or []
    print(f"源: {os.path.basename(SCORED)}  n={len(rows)}")
    print(f"模型: {doc.get('model')}\n")

    # ── 口径校验：生产权重 + 口径 A 必须逐只等于文件 total ──
    diffs = []
    for r in rows:
        s, _ = composite(r, W_V417, "A")
        if s is not None and r.get("total") is not None:
            diffs.append(abs(s - float(r["total"])))
    mx = max(diffs) if diffs else None
    print(f"[口径校验] 生产权重+口径A 复算 vs 文件 total：n={len(diffs)}  max_abs_diff={mx}")
    if mx is None or mx > 0.005:
        print("❌ 口径校验失败 → 中止")
        return 2
    print("✅ 一致（说明复算口径 == 生产口径）\n")

    # ── 结构性不适用维的偏差量化 ──
    lead = [r for r in rows
            if r.get(STRUCT_DIM) is None and isinstance(r.get("score_dominant"), (int, float))]
    non = [r for r in rows if isinstance(r.get(STRUCT_DIM), (int, float))]
    _mx = DIM_MAX[STRUCT_DIM]
    _w = W_V417[STRUCT_DIM]
    _sum = sum(W_V417.values())
    pt_lead = 5.0 / _mx * 10.0 * _w / _sum * 10.0
    pt_non = [(min(r[STRUCT_DIM], _mx) / _mx * 10.0) * _w / _sum * 10.0 for r in non]

    print("─" * 100)
    print(f"⚠️  {STRUCT_DIM}（权重 {_w:.2f}）结构性不适用偏差")
    print("─" * 100)
    print(f"  龙头型（无该维，dominant 有值）: {len(lead):>5} 只 → 该维恒定贡献 {pt_lead:.2f} 分")
    print(f"  非龙头（有该维）            : {len(non):>5} 只 → 均值 {statistics.mean(pt_non):.2f} 分 / "
          f"中位 {statistics.median(pt_non):.2f} 分")
    print(f"  其中 0 分的               : {sum(1 for r in non if r[STRUCT_DIM] == 0):>5} 只 "
          f"({sum(1 for r in non if r[STRUCT_DIM] == 0) / len(non) * 100:.1f}%)")
    print(f"  ⇒ 龙头比非龙头均值多 +{pt_lead - statistics.mean(pt_non):.2f} 分，"
          f"比 0 分股多 {pt_lead:.2f} 分")
    print(f"  ⚠️ 代码注释写「龙头仅 dominant(.10)」，实际是「龙头 .10 + 白送 .20 的中性分」\n")

    # ── 三口径 × 两权重 ──
    results = {}
    for wname, w in [("v4.17", W_V417), ("C1", W_C1)]:
        for cal in ("A", "B", "C"):
            results[(wname, cal)] = rank_of(rows, w, cal)

    def show(wname, cal, n=10):
        lst = results[(wname, cal)]
        base = {r["code"]: i + 1 for i, (r, _) in enumerate(results[(wname, "A")])}
        print(f"{'#':>3} {'代码':<10}{'名称':<10}{'分数':>7}{'A口径排名':>10}"
              f"{'市值亿':>9}{'成交亿':>8}  {'行业':<10}{'sg':>6}{'dom':>6}")
        for i, (r, s) in enumerate(lst[:n], 1):
            sg = r.get(STRUCT_DIM)
            print(f"{i:>3} {r['code']:<10}{(r.get('name') or '')[:9]:<10}{s:>7.2f}"
                  f"{base.get(r['code'], '-'):>10}"
                  f"{(r.get('circ_mv') or 0) / 1e8:>9.1f}{(r.get('amount') or 0) / 1e8:>8.2f}"
                  f"  {(r.get('sector') or '-')[:9]:<10}"
                  f"{(f'{sg:.1f}' if isinstance(sg, (int, float)) else '缺'):>6}"
                  f"{(r.get('score_dominant') or 0):>6.2f}")

    for wname in ("v4.17", "C1"):
        for cal, label in (("A", "生产现行：缺失填中性 5.0"),
                           ("B", "从严：size_growth 缺失按 0 分"),
                           ("C", "分母剔除：对龙头不适用")):
            print("=" * 100)
            print(f"【{wname} · 口径 {cal}】{label}")
            print("=" * 100)
            show(wname, cal)
            print()

    # ── 交叉重合度 ──
    print("=" * 100)
    print("【前十重合度矩阵】")
    print("=" * 100)
    keys = list(results.keys())
    sets = {k: {r["code"] for r, _ in v[:10]} for k, v in results.items()}
    print(f"{'':<16}" + "".join(f"{f'{w}/{c}':>10}" for w, c in keys))
    for k1 in keys:
        row = f"{f'{k1[0]}/{k1[1]}':<16}"
        for k2 in keys:
            row += f"{len(sets[k1] & sets[k2]):>10}"
        print(row)

    # ── 写文件 ──
    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "src": os.path.basename(SCORED),
        "model_in_file": doc.get("model"),
        "weights_v417": W_V417,
        "weights_c1": W_C1,
        "calibration": {"max_abs_diff_vs_file_total": mx, "n": len(diffs)},
        "struct_dim_issue": {
            "dim": STRUCT_DIM, "weight": _w,
            "n_leader_no_dim": len(lead), "n_nonleader": len(non),
            "pt_leader": round(pt_lead, 2),
            "pt_nonleader_mean": round(statistics.mean(pt_non), 2),
            "pt_nonleader_median": round(statistics.median(pt_non), 2),
            "n_zero_score": sum(1 for r in non if r[STRUCT_DIM] == 0),
            "note": ("龙头按 v4.13 分区设计不拿小盘成长溢价（ep_size_ind.py:645 注释），"
                     "但 composite_total 把缺失填中性 5.0 → 百分制恒 10.00 分。"
                     "注释写「龙头仅 dominant(.10)」，实际是「龙头 .10 + 白送 .20 中性分」。"),
        },
        "top10": {f"{w}_{c}": [{
            "rank": i, "code": r["code"], "name": r.get("name"),
            "sector": r.get("sector"), "score": s,
            "total_in_file": r.get("total"),
            "circ_mv": r.get("circ_mv"), "amount": r.get("amount"),
            "pe": r.get("pe"), "pb": r.get("pb"),
            "score_size_growth": r.get(STRUCT_DIM),
            "score_dominant": r.get("score_dominant"),
            "neutral_filled": r.get("neutral_filled"),
        } for i, (r, s) in enumerate(results[(w, c)][:10], 1)]
            for w in ("v4.17", "C1") for c in ("A", "B", "C")},
        "overlap": {f"{k1[0]}_{k1[1]}|{k2[0]}_{k2[1]}": len(sets[k1] & sets[k2])
                    for k1 in keys for k2 in keys},
        "caveat": ("⚠️ 三口径前十仅 4 只重合 —— 口径选择直接决定名单，必须先裁决。"
                   "另：全部 alpha 结论来自同一段构造期，零样本外（n_oos_dates=0）。"
                   "C1 的头部优势未经样本外验证，IC 下降 10% 是已知代价。"
                   "本脚本未修改任何配置。"),
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"\n写出 {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
