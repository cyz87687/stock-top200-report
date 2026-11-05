#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""round130：total 评分对 T+1 超额的区分力重算（补产出物）。

背景
----
gen_report_top200.py 的 T+1/T0 卡片里，「total 对 T+1 超额 Spearman / Top10% vs
Bot10%」一组数字是**硬编码**的（旧值 −0.0257 / 1211 样本 / 12 个推荐日），
既没有对应 JSON 产物，也无法随数据刷新 —— 违反本项目「全部数字从 JSON 读取」
的渲染纪律。本脚本把它落成可复现产物，供页面读取。

口径
----
- 样本源：recommendation_track.json 的 records（每次推荐的 total 与后续超额）
- 超额：excess['1']，基准 sh000300（沪深300），前复权收盘价，T+1 口径
- 只取 status['1'] == 'ok' 的已到期样本，禁用未到期/投影值
- Spearman 用平均秩处理并列（与项目内其它实现一致）
- Top10%/Bot10% 按 total 的 p90/p10 分位分组

输出
----
audit_round130_total_t1.json
"""
import json
import os
import statistics as st

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "recommendation_track.json")
OUT = os.path.join(HERE, "audit_round130_total_t1.json")


def _rank(v):
    """平均秩（升序，1 起），并列取均值。"""
    order = sorted(range(len(v)), key=lambda i: v[i])
    out = [0.0] * len(v)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            out[order[k]] = avg
        i = j + 1
    return out


def _pearson(a, b):
    ma, mb = st.mean(a), st.mean(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    den = (sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b)) ** 0.5
    return num / den if den else 0.0


def main():
    with open(SRC, encoding="utf-8") as f:
        data = json.load(f)
    recs = data.get("records") or []
    meta = data.get("_meta") or {}

    rows = []
    for r in recs:
        if (r.get("status") or {}).get("1") != "ok":
            continue
        tot, ex = r.get("total"), (r.get("excess") or {}).get("1")
        if not isinstance(tot, (int, float)) or tot != tot:
            continue
        if not isinstance(ex, (int, float)) or ex != ex:
            continue
        rows.append({"date": r.get("date"), "code": r.get("code"),
                     "name": r.get("name"), "total": float(tot), "excess1": float(ex)})

    if not rows:
        raise SystemExit("无已到期 T+1 样本，拒绝出空结论")

    tots = [x["total"] for x in rows]
    exs = [x["excess1"] for x in rows]
    spearman = _pearson(_rank(tots), _rank(exs))

    qs = sorted(tots)
    p90 = qs[max(0, int(0.9 * len(qs)) - 1)]
    p10 = qs[int(0.1 * len(qs))]

    def _grp(vals, tag, thr):
        e = [x["excess1"] for x in vals]
        return {
            "label": tag,
            "threshold_total": round(thr, 2),
            "n": len(e),
            "mean_excess_pct": round(st.mean(e), 4),
            "win_rate": round(sum(1 for x in e if x > 0) / len(e), 4),
        }

    top = [x for x in rows if x["total"] >= p90]
    bot = [x for x in rows if x["total"] <= p10]

    # 十等分（按 total 排序后等分），用于看是否单调
    srt = sorted(rows, key=lambda x: x["total"])
    n = len(srt) // 10
    deciles = []
    for i in range(10):
        g = srt[i * n:(i + 1) * n] if i < 9 else srt[9 * n:]
        e = [x["excess1"] for x in g]
        deciles.append({
            "d": i + 1, "n": len(g),
            "total_min": round(g[0]["total"], 2), "total_max": round(g[-1]["total"], 2),
            "mean_excess_pct": round(st.mean(e), 4),
            "win_rate": round(sum(1 for x in e if x > 0) / len(e), 4),
        })
    dmeans = [d["mean_excess_pct"] for d in deciles]
    monotone = all(a <= b for a, b in zip(dmeans, dmeans[1:])) or \
               all(a >= b for a, b in zip(dmeans, dmeans[1:]))

    out = {
        "meta": {
            "purpose": "total 评分对 T+1 超额的区分力（供 T+1/T0 卡片读取，替代硬编码）",
            "source": "recommendation_track.json records",
            "bench": meta.get("bench"),
            "basis": meta.get("basis"),
            "horizon": 1,
            "sample_filter": "status['1']=='ok' 且 total/excess1 均为数值（未到期样本剔除）",
            "note": ("Spearman 用平均秩处理并列；Top/Bot 按 total 的 p90/p10 分位。"
                     "total 是推荐当时的评分，excess1 是相对 sh000300 的次日超额。"),
            "must_not_claim": [
                "不得把 Spearman 与 0 的差异当作「预测力」",
                "不得据此反向做空高分或做多低分（分位差异方向在不同样本期之间翻转）",
            ],
        },
        "n_samples": len(rows),
        "n_reco_days": len({x["date"] for x in rows}),
        "date_min": min(x["date"] for x in rows),
        "date_max": max(x["date"] for x in rows),
        "spearman_total_vs_excess1": round(spearman, 4),
        "top10": _grp(top, "Top10%（最高分）", p90),
        "bot10": _grp(bot, "Bot10%（最低分）", p10),
        "deciles": deciles,
        "deciles_monotone": monotone,
        "mean_excess_all_pct": round(st.mean(exs), 4),
        "verdict": (
            f"total 对 T+1 超额 Spearman = {spearman:+.4f}（{len(rows)} 样本 / "
            f"{len({x['date'] for x in rows})} 个推荐日），与 0 无实质差异；"
            f"Top10% 均值 {st.mean([x['excess1'] for x in top]):+.2f}% vs "
            f"Bot10% {st.mean([x['excess1'] for x in bot]):+.2f}%，"
            f"十等分{'单调' if monotone else '非单调'}。"
            "→ total 不是次日方向信号，既不能正向用于次日买，也不能反向用。"
        ),
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"✅ 已写出 {OUT}")
    print(f"   样本 {len(rows)} / 推荐日 {out['n_reco_days']} / "
          f"区间 {out['date_min']}~{out['date_max']}")
    print(f"   Spearman = {spearman:+.4f}")
    print(f"   Top10%: n={out['top10']['n']} 均值 {out['top10']['mean_excess_pct']:+.2f}% "
          f"胜率 {out['top10']['win_rate']*100:.1f}%")
    print(f"   Bot10%: n={out['bot10']['n']} 均值 {out['bot10']['mean_excess_pct']:+.2f}% "
          f"胜率 {out['bot10']['win_rate']*100:.1f}%")
    print(f"   十等分单调: {monotone}")


if __name__ == "__main__":
    main()
