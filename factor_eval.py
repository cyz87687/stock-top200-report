#!/usr/bin/env python3
"""因子有效性评估（Rank IC）—— 量化视角衡量"推荐/因子"的预测能力。

设计目标（老大需求）：从量化投资视角给出专业因子评估机制，用 Rank IC（信息系数）
等指标衡量个股推荐或因子的预测能力与有效性，并明确展示格式、计算周期与输出结构。

━━━ 指标定义（专业口径）━━━
Rank IC(t → t+h) = Spearman 秩相关( 因子值在 t 日截面的秩, 个股在 t+h 的收益率 )
  · 用秩相关而非 Pearson：对极端涨跌幅/离群因子值稳健，是业界 IC 标准口径
  · h ∈ {1, 5, 20} 交易日，与推荐跟踪表(recommendation_track.json)口径一致
聚合指标（对同一因子的全部截面 IC 序列）：
  · IC 均值   : 因子平均预测能力（>0 表示因子值越大未来收益越高）
  · IC 标准差 : IC 序列波动（稳定性）
  · t 统计量  : mean(IC) / (std(IC) / √n)，|t|≥2 通常视为统计显著
  · IC 胜率   : IC > 0 的截面占比（方向一致性）
  · 分层多空  : 每截面按因子分 5 组，头部组均值收益 − 尾部组均值收益，再取截面均值
                （检验单调性；IC 高但多空差价差为负 = 因子只在尾部有效）

━━━ 两个评估对象 ━━━
1) 五维因子（全截面）: total / score_news / score_tech / score_fund /
   score_theme / score_sector —— 回答"评分模型哪个维度真正有预测力"
2) 推荐组合层: recommendation_track.json 中当日推荐标的的 total 与未来收益
   —— 回答"我们推荐的股票整体是否跑赢"

━━━ 数据来源 ━━━
· factor_history.json（每日追加的紧凑截面快照，主数据源，随仓库分发）
· 本地 top200_scored_*.json（--bootstrap 时用于初始化历史）
· git 历史中的 top200_scored_*.json（--bootstrap-from-git，仅本地可用，
  Actions checkout 默认 depth=1 无历史）

用法：
  python3 factor_eval.py                     # 计算 → factor_ic.json
  python3 factor_eval.py --bootstrap-from-git  # 从 git 历史初始化 factor_history.json
  python3 factor_eval.py --show              # 计算并打印结果表

数据纪律：全部取自行程内真实文件的真实字段；样本不足时如实标注，不补齐、不编造。
"""
import json
import os
import sys
import glob
import math
import subprocess
from collections import defaultdict
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
HISTORY_PATH = os.path.join(HERE, "factor_history.json")
OUT_PATH = os.path.join(HERE, "factor_ic.json")
TRACK_PATH = os.path.join(HERE, "recommendation_track.json")

HORIZONS = (1, 5, 20)          # 交易日，与推荐跟踪表 HORIZONS 一致
MIN_SAMPLE = 20                # 少于此截面数不给有效性判定（统计显著性不足）
QUANTILES = 5                  # 分层检验组数

# 因子字段 → 展示名（顺序即报告表格顺序）
FACTORS = [
    ("total", "综合总分"),
    ("score_theme", "题材热度"),
    ("score_tech", "技术面"),
    ("score_fund", "基本面"),
    ("score_news", "消息面"),
    ("score_sector", "板块动量"),
]

FACTOR_VERDICTS = [
    # (|IC|下限, t下限, 判定, 颜色)
    (0.05, 2.0, "显著有效", "#22c55e"),
    (0.03, 1.5, "弱有效", "#84cc16"),
    (0.02, 0.0, "方向为正·不显著", "#f59e0b"),
]


# ---------------------------------------------------------------- 工具函数
def _norm_code(code):
    c = str(code or "").strip().lower()
    return c[2:] if len(c) > 6 and c[:2] in ("sh", "sz", "bj") else c


def _ranks(vals):
    """平均秩（处理并列）。返回与 vals 等长的秩列表(1-based)。"""
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    ranks = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _pearson(x, y):
    n = len(x)
    if n < 3:
        return None
    mx, my = sum(x) / n, sum(y) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def rank_ic(factor_vals, ret_vals):
    """Spearman 秩相关（= 秩序列的 Pearson）。样本不足或零方差返回 None。"""
    if len(factor_vals) < 5:
        return None
    return _pearson(_ranks(factor_vals), _ranks(ret_vals))


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _std(xs):
    xs = [x for x in xs if x is not None]
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _quantile_spread(factor_vals, ret_vals, q=QUANTILES):
    """分层多空差价差：按因子降序分 q 组，头部组均值收益 − 尾部组均值收益（%）。"""
    n = len(factor_vals)
    if n < q * 5:
        return None
    order = sorted(range(n), key=lambda i: -factor_vals[i])
    g = max(1, n // q)
    top = [ret_vals[i] for i in order[:g]]
    bot = [ret_vals[i] for i in order[-g:]]
    return (_mean(top) or 0) - (_mean(bot) or 0)


# ---------------------------------------------------------------- 历史管理
def snapshot_from_scored(path):
    """从 top200_scored_*.json 提取紧凑截面快照。"""
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    rows = []
    for r in d.get("results", []):
        px = r.get("price")
        if not isinstance(px, (int, float)) or px <= 0:
            continue
        row = {"code": _norm_code(r.get("code")), "price": px}
        for k, _ in FACTORS:
            v = r.get(k)
            if isinstance(v, (int, float)):
                row[k] = v
        if len(row) > 2:          # 至少有一个因子字段
            rows.append(row)
    return {"date": str(d.get("date") or os.path.basename(path)), "rows": rows}


def load_history():
    if not os.path.exists(HISTORY_PATH):
        return {"version": 1, "snapshots": []}
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"version": 1, "snapshots": []}


def save_history(hist):
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, separators=(",", ":"))


def upsert_snapshot(hist, snap, replace=True):
    """按日期去重写入截面；replace=True 时覆盖同日旧快照（重跑/修正场景）。"""
    if not snap or not snap.get("rows"):
        return False
    snaps = hist.setdefault("snapshots", [])
    for i, s in enumerate(snaps):
        if s.get("date") == snap["date"]:
            if replace:
                snaps[i] = snap
                return True
            return False
    snaps.append(snap)
    snaps.sort(key=lambda s: s["date"])
    return True


def bootstrap_from_git():
    """从 git 历史提取全部 top200_scored_*.json 截面（仅本地；Actions 无历史）。"""
    try:
        out = subprocess.run(
            ["git", "log", "--all", "--diff-filter=A", "--name-only", "--format=%H",
             "--", "top200_scored_*.json"],
            capture_output=True, text=True, cwd=HERE, timeout=120).stdout
    except Exception as e:
        print(f"❌ git 历史读取失败: {e}")
        return 1
    commit_of = {}
    cur_commit = None
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("top200_scored_"):
            if "_watch_" in line:
                continue                  # 观察池截面不并入(口径: 主力池 TOP200)
            date = line[len("top200_scored_"):-len(".json")]
            commit_of.setdefault(date, cur_commit)   # 取最早加入的提交
        elif len(line) >= 7 and all(c in "0123456789abcdef" for c in line):
            cur_commit = line
    if not commit_of:
        print("⚠️ git 历史中未找到 top200_scored_*.json")
        return 1

    hist = load_history()
    n_new = n_fail = 0
    for date in sorted(commit_of):
        if any(s.get("date") == date for s in hist.get("snapshots", [])):
            continue
        commit = commit_of[date]
        try:
            raw = subprocess.run(["git", "show", f"{commit}:top200_scored_{date}.json"],
                                 capture_output=True, text=True, cwd=HERE,
                                 timeout=120).stdout
            d = json.loads(raw)
            tmp = os.path.join(HERE, f"._boot_{date}.json")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False)
            snap = snapshot_from_scored(tmp)
            os.remove(tmp)
            if upsert_snapshot(hist, snap):
                n_new += 1
        except Exception as e:
            n_fail += 1
            print(f"   ⚠️ {date} 提取失败: {str(e)[:60]}")
    save_history(hist)
    print(f"✅ 引导完成: factor_history.json 现有 {len(hist['snapshots'])} 个截面"
          f"（新增 {n_new}，失败 {n_fail}）")
    return 0


# ---------------------------------------------------------------- IC 计算
def _fwd_returns(snapshots, h):
    """生成 (date, {code: fwd_ret%}) —— t 日截面与第 h 个后续截面的价差收益。"""
    dates = [s["date"] for s in snapshots]
    out = []
    for i, s in enumerate(snapshots):
        j = i + h
        if j >= len(snapshots):
            break
        later = snapshots[j]
        px_later = {r["code"]: r["price"] for r in later["rows"]}
        rets = {}
        for r in s["rows"]:
            p1 = px_later.get(r["code"])
            if p1 and r["price"] > 0:
                rets[r["code"]] = (p1 - r["price"]) / r["price"] * 100.0
        if rets:
            out.append((s["date"], rets))
    return out


def _eval_factor(snapshots, field):
    """对单因子计算全部 horizon 的 IC 聚合指标。"""
    res = {}
    for h in HORIZONS:
        fwd = _fwd_returns(snapshots, h)
        ics, spreads, dates = [], [], []
        for date, rets in fwd:
            base = next(s for s in snapshots if s["date"] == date)
            fv, rv = [], []
            for r in base["rows"]:
                if r["code"] in rets and isinstance(r.get(field), (int, float)):
                    fv.append(r[field])
                    rv.append(rets[r["code"]])
            ic = rank_ic(fv, rv)
            if ic is not None:
                ics.append(ic)
                spreads.append(_quantile_spread(fv, rv))
                dates.append(date)
        n = len(ics)
        m, sd = _mean(ics), _std(ics)
        t_stat = (m / (sd / math.sqrt(n))) if (n >= 2 and sd and sd > 0) else None
        res[str(h)] = {
            "n": n,
            "dates": dates,
            "ic_series": [round(x, 4) for x in ics],
            "ic_mean": round(m, 4) if m is not None else None,
            "ic_std": round(sd, 4) if sd is not None else None,
            "t_stat": round(t_stat, 2) if t_stat is not None else None,
            "win_rate": round(sum(1 for x in ics if x > 0) / n * 100, 1) if n else None,
            "spread_mean": round(_mean(spreads), 3) if spreads else None,
        }
    return res


def _verdict_h(agg):
    """单周期有效性判定。"""
    n = agg.get("n") or 0
    if n < MIN_SAMPLE:
        return "样本积累中", "#94a3b8", f"n={n}"
    ic = agg.get("ic_mean")
    t = agg.get("t_stat")
    if ic is None:
        return "数据不足", "#94a3b8", "观测不足"
    aic = abs(ic)
    label, color = "无预测力", "#64748b"
    for thr_ic, thr_t, lab, col in FACTOR_VERDICTS:
        t_ok = (t is not None and t >= thr_t) if thr_t > 0 else True
        if aic >= thr_ic and t_ok:
            label, color = lab, col
            break
    if ic < 0 and aic >= 0.02:
        return "负向（可作反向指标）", "#38bdf8", f"IC={ic:+.3f}，方向为负"
    return label, color, f"IC={ic:+.3f}，t={t}"


def _overall_verdict(horizons):
    """综合判定: 取 |t| 最大且样本达标的周期(不同因子有效周期不同,
    如基本面在 T+20 显著而 T+1 不显著, 只报 T+1 会漏判)。"""
    best, best_t = None, -1.0
    for h in HORIZONS:
        a = horizons.get(str(h)) or {}
        if (a.get("n") or 0) < MIN_SAMPLE:
            continue
        t = a.get("t_stat")
        if t is not None and abs(t) > abs(best_t):
            best, best_t = a, t
    if best is None:
        n_max = max((horizons.get(str(h)) or {}).get("n") or 0 for h in HORIZONS)
        return "样本积累中", "#94a3b8", f"各周期样本均 <{MIN_SAMPLE}（最大 {n_max}）"
    lab, col, note = _verdict_h(best)
    return lab, col, note + f"（T+{ [h for h in HORIZONS if (horizons.get(str(h)) or {}) is best][0] }）"


def _eval_recommendations():
    """推荐组合层 IC：按 date 截面，因子=推荐时总分，收益=rets[h]。"""
    if not os.path.exists(TRACK_PATH):
        return None
    try:
        with open(TRACK_PATH, encoding="utf-8") as f:
            recs = json.load(f).get("records", [])
    except Exception:
        return None
    by_date = defaultdict(list)
    for r in recs:
        if isinstance(r.get("total"), (int, float)):
            by_date[r["date"]].append(r)
    out = {}
    for h in HORIZONS:
        ics, spreads, dates = [], [], []
        for date in sorted(by_date):
            rows = by_date[date]
            fv = [r["total"] for r in rows]
            rv = [r.get("rets", {}).get(str(h)) for r in rows]
            pairs = [(f, x) for f, x in zip(fv, rv) if isinstance(x, (int, float))]
            if len(pairs) < 5:
                continue
            fv2 = [p[0] for p in pairs]
            rv2 = [p[1] for p in pairs]
            ic = rank_ic(fv2, rv2)
            if ic is not None:
                ics.append(ic)
                spreads.append(_quantile_spread(fv2, rv2))
                dates.append(date)
        n = len(ics)
        m, sd = _mean(ics), _std(ics)
        t_stat = (m / (sd / math.sqrt(n))) if (n >= 2 and sd and sd > 0) else None
        out[str(h)] = {
            "n": n, "dates": dates,
            "ic_series": [round(x, 4) for x in ics],
            "ic_mean": round(m, 4) if m is not None else None,
            "ic_std": round(sd, 4) if sd is not None else None,
            "t_stat": round(t_stat, 2) if t_stat is not None else None,
            "win_rate": round(sum(1 for x in ics if x > 0) / n * 100, 1) if n else None,
            "spread_mean": round(_mean(spreads), 3) if spreads else None,
        }
    return out


def main():
    args = sys.argv[1:]
    show = "--show" in args
    bootstrap = "--bootstrap-from-git" in args

    hist = load_history()

    # 1) 可选：从 git 历史引导
    if bootstrap:
        bootstrap_from_git()
        hist = load_history()

    # 2) 把本地最新 scored json 并入历史（幂等：同日覆盖）
    local_files = sorted(glob.glob(os.path.join(HERE, "top200_scored_*.json")))
    n_added = 0
    for p in local_files:
        if "_watch_" in os.path.basename(p):
            continue                     # 观察池单独截面不并入（口径：主力池 TOP200）
        try:
            snap = snapshot_from_scored(p)
            if upsert_snapshot(hist, snap):
                n_added += 1
        except Exception as e:
            print(f"⚠️ {os.path.basename(p)} 解析失败: {str(e)[:60]}")
    if n_added:
        save_history(hist)
        print(f"📚 历史截面更新: 新增/覆盖 {n_added} 个（现有 {len(hist['snapshots'])} 个）")

    snaps = [s for s in hist.get("snapshots", []) if s.get("rows")]
    if len(snaps) < 3:
        print(f"❌ 截面数不足（{len(snaps)}），无法计算 IC；可用 --bootstrap-from-git 从 git 历史引导")
        return 1

    # 3) 计算因子 IC
    factors = {}
    for field, label in FACTORS:
        agg = _eval_factor(snaps, field)
        for h in HORIZONS:                     # 逐周期判定
            a = agg[str(h)]
            v, c, note = _verdict_h(a)
            a["verdict"], a["verdict_color"], a["verdict_note"] = v, c, note
        vlabel, vcolor, vnote = _overall_verdict(agg)
        factors[field] = {"label": label, "horizons": agg,
                          "verdict": vlabel, "verdict_color": vcolor, "verdict_note": vnote}

    rec_ic = _eval_recommendations()
    if rec_ic:
        for h in HORIZONS:
            a = rec_ic[str(h)]
            v, c, note = _verdict_h(a)
            a["verdict"], a["verdict_color"], a["verdict_note"] = v, c, note
        _rv, _rc, _rn = _overall_verdict(rec_ic)
        rec_ic = {"horizons": rec_ic, "verdict": _rv, "verdict_color": _rc, "verdict_note": _rn}

    out = {
        "date": snaps[-1]["date"],
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "method": {
            "name": "Rank IC（Spearman 秩相关）",
            "formula": "IC(t→t+h) = Spearman(因子值_t 截面秩, 收益率_t+h)",
            "horizons_days": list(HORIZONS),
            "t_stat": "mean(IC) / (std(IC)/√n)，|t|≥2 视为统计显著",
            "min_sample": MIN_SAMPLE,
            "quantile_spread": f"每截面按因子分 {QUANTILES} 组，头部组均值收益 − 尾部组均值收益",
        },
        "n_snapshots": len(snaps),
        "snapshot_range": [snaps[0]["date"], snaps[-1]["date"]],
        "snapshot_dates": [s["date"] for s in snaps],
        "factors": factors,
        "recommendation_ic": rec_ic,
        "notes": [
            f"全截面样本 {len(snaps)} 个交易日（{snaps[0]['date']} ~ {snaps[-1]['date']}），"
            f"每截面约 {len(snaps[-1]['rows'])} 只（当日成交额 TOP200）",
            "收益率为收盘价简单收益，未扣交易成本；IC 衡量排序能力而非绝对收益",
            "历史截面来自 git 提交记录（评分模型版本随当时模型，含早期的综合行业归类问题）",
        ],
    }
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"✅ 已生成 {os.path.basename(OUT_PATH)}: {len(factors)} 个因子 × {len(HORIZONS)} 周期")

    if show:
        _print_table(out)
    return 0


def _print_table(out):
    print()
    print(f"📐 因子有效性评估（Rank IC）  截面 {out['n_snapshots']} 个 "
          f"({out['snapshot_range'][0]} ~ {out['snapshot_range'][1]})")
    hdr = f"{'因子':<10}" + "".join(f"{'T+' + str(h):>30}" for h in HORIZONS)
    print(hdr)
    print("-" * len(hdr))
    for field, label in FACTORS:
        f = out["factors"][field]
        line = f"{label:<10}"
        for h in HORIZONS:
            a = f["horizons"][str(h)]
            if a["n"] == 0:
                line += f"{'—(未到期)':>30}"
            else:
                cell = (f"IC {a['ic_mean']:+.3f} t={a['t_stat']} "
                        f"胜率{a['win_rate']:.0f}% n={a['n']}")
                line += f"{cell:>30}"
        print(line + f"   → {f['verdict']}")
    if out.get("recommendation_ic"):
        r = out["recommendation_ic"]["horizons"]
        line = f"{'推荐组合':<10}"
        for h in HORIZONS:
            a = r[str(h)]
            if a["n"] == 0:
                line += f"{'—(未到期)':>30}"
            else:
                cell = (f"IC {a['ic_mean']:+.3f} t={a['t_stat']} "
                        f"胜率{a['win_rate']:.0f}% n={a['n']}")
                line += f"{cell:>30}"
        print(line + f"   → {out['recommendation_ic']['verdict']}")


if __name__ == "__main__":
    sys.exit(main())
