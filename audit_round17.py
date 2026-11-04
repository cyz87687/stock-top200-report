#!/usr/bin/env python3
"""Round17：权重与因子的「日报 → 生产」全链路穿透审计。

老大 2026-10-25 提问：「检查每日因子日报的权重和因子有没有应用到 A股成交额 TOP500
评分报告，目前看只应用了部分。」

本脚本不做任何新计算，只做**追溯**：把 factor_weights.json 的每一个权重维度，
沿「日报评估 → step2 计算 → scored 落盘 → enhance → apply → 报告展示 → 多因子组合」
七环逐环判定「有没有应用、用在哪、覆盖多少样本」，输出一张可复算的矩阵。

判定规则（写死，不靠人眼）：
  · 日报评估环：factor_ic.json 的 factors 里有没有该维度的独立 IC（n>0 才算）
  · 生产计算环：scored json 里有没有该维度的数值（该维度有 >0 行有值才算落地）
  · 权重覆盖度 = 该股实际参与加权的权重之和（缺维从分母剔除，这是既定正确口径）
  · 组合轴环：pick_multifactor 的选股轴里有没有该维度

⚠️ 纪律：本脚本只读文件、不写文件、不改任何生产数据。所有结论必须能在
   audit_round17.json 里逐项复算。
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable


def _load(p):
    fp = os.path.join(HERE, p)
    if not os.path.exists(fp):
        return None
    try:
        with open(fp, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _src(p):
    fp = os.path.join(HERE, p)
    if not os.path.exists(fp):
        return ""
    with open(fp, encoding="utf-8", errors="replace") as f:
        return f.read()


def latest(pat):
    fs = sorted(f for f in os.listdir(HERE)
                if re.match(pat, f) and "_watch_" not in f and ".bak" not in f)
    return fs[-1] if fs else None


def main():
    wmeta = _load("factor_weights.json") or {}
    W = wmeta.get("weights") or {}
    ic = _load("factor_ic.json") or {}
    icf = ic.get("factors") or {}

    sc_name = latest(r"top200_scored_\d{4}-\d{2}-\d{2}\.json")
    scored = _load(sc_name) if sc_name else None
    pool_name = "top500_all_a.json"
    pool = _load(pool_name) or {}

    mf_name = None
    sd = os.path.join(HERE, "sector")
    if os.path.isdir(sd):
        fs = sorted(f for f in os.listdir(sd) if re.match(r"mf_pick_\d{4}-\d{2}-\d{2}\.json", f))
        mf_name = fs[-1] if fs else None
    mf = _load(os.path.join("sector", mf_name)) if mf_name else None

    src_step2 = _src("step2_stockscorer_v2.py")
    src_apply = _src("apply_sector_momentum.py")
    src_enh = _src("enhance_scores.py")
    src_pick = _src("pick_multifactor.py")
    src_daily = _src("gen_factor_daily.py")

    rows = (scored or {}).get("results") or []
    n_rows = len(rows)

    # ---- 多因子组合实际使用的选股轴 ----
    # ⚠️ 必须区分三种角色，不能都记成「用了」：
    #   ① 正向加权轴：score_fund / score_news
    #   ② 聚合成块使用：REV_BLOCK 五维 → 先合成 __rev__ 再按块权重加权（间接使用）
    #   ③ 负向硬排除：DROP_HIGH 里的维度（attention / theme）——高分即剔除，不是加权轴
    mf_pos = set(["score_fund", "score_news"])
    mf_block = set(x.strip() for x in
                   re.findall(r'REV_BLOCK\s*=\s*\((.*?)\)', src_pick, re.S)[0]
                   .replace('"', '').split(',')) if re.search(r'REV_BLOCK\s*=\s*\(', src_pick) else set()
    mf_drop = set()
    for m in re.finditer(r'DROP_HIGH\s*=\s*\{(.*?)\}', src_pick, re.S):
        for k in re.findall(r'"(score_[a-z0-9_]+)"', m.group(1)):
            mf_drop.add(k)

    out_dims = []
    for k, v in sorted(W.items(), key=lambda x: -x[1]):
        # ① 日报评估环
        f = icf.get(k) or {}
        hz = f.get("horizons") or {}
        n_eval = {h: (hz.get(h) or {}).get("n") or 0 for h in ("1", "5", "20")}
        n_best = max(n_eval.values()) if n_eval else 0
        in_daily = bool(f) and n_best > 0
        # ② step2 计算并落盘
        step2_compute = (f"s_theme_neg" in src_step2 and k == "score_theme_neg") or \
                        (k == "score_size" and "_mv_rank" in src_step2)
        step2_write = bool(re.search(rf'"{k}"\s*:', src_step2))
        # ③ scored 数据
        n_have = sum(1 for r in rows if isinstance(r.get(k), (int, float)))
        # ④ enhance / apply 读取
        in_enh = f'"{k}"' in src_enh or f"'{k}'" in src_enh
        in_apply = f'"{k}"' in src_apply or f"'{k}'" in src_apply
        # ⑤ 报告展示（_dim_composites / 维度摘要）
        in_report = k in src_daily or k in _src("gen_report_top200.py")
        # ⑥ 组合角色（正向加权轴 / 聚合成块 / 负向排除 / 未使用）
        if k in mf_pos:
            mf_role = "正向加权轴"
        elif k in mf_block:
            mf_role = "聚合成块(__rev__)"
        elif k in mf_drop:
            mf_role = "负向硬排除"
        else:
            mf_role = "—"

        cov = 0.0
        if n_have:
            cov = sum(wt for kk, wt in W.items()
                      if wt > 0 and isinstance(rows[0].get(kk), (int, float)))
        out_dims.append({
            "dim": k, "weight": v,
            "daily_report": {"evaluated": in_daily, "n_by_h": n_eval,
                             "note": ("独立 IC 已评估" if in_daily else
                                      "⚠️ 日报从未单独评估该维度（只在复合 IC 里被 _derive_dims 隐式带入）")},
            "step2": {"computes": bool(step2_compute), "writes_to_scored": step2_write},
            "scored_data": {"n_rows_with_value": n_have, "n_rows_total": n_rows,
                            "present": n_have > 0},
            "enhance_reads": in_enh, "apply_reads": in_apply,
            "mf_portfolio_role": mf_role,
            "effective_weight_when_present": round(cov, 4),
        })

    # ---- 权重覆盖度（用现权重重算，与 apply 同口径）----
    def recalc(r):
        s = den = 0.0
        used = []
        for k, wt in W.items():
            if wt <= 0:
                continue
            val = r.get(k)
            if isinstance(val, (int, float)):
                s += float(val) * wt
                den += wt
                used.append(k)
        if den <= 0:
            return None, used
        return round(s / den * 20, 2), used

    diffs, covs = [], []
    for r in rows:
        t, used = recalc(r)
        if t is None:
            continue
        diffs.append(round(t - float(r.get("total") or 0), 2))
        covs.append(sum(W[k] for k in used))

    import statistics as st
    n_cmp = len(diffs)
    cov_mean = st.mean(covs) if covs else 0.0

    # ---- 母体口径 ----
    pr = pool.get("pool_rules") or {}
    pool_info = {
        "file": pool_name,
        "date": pool.get("date"),
        "rules_version": pr.get("version"),
        "size_quantile": pr.get("size_quantile"),
        "sample_strata": pr.get("sample_strata"),
        "size_as_factor": pr.get("size_as_factor"),
        "sampling": pr.get("sampling"),
        "mv_median_yi": pr.get("mv_median_yi"),
        "mv_max_yi": pr.get("mv_max_yi"),
        "mv_log_sd": pr.get("mv_log_sd"),
    }
    cfg = _load("config.json") or {}
    cfg_pr = cfg.get("pool_rules") or {}
    cfg_info = {
        "rules_version": cfg_pr.get("version"),
        "size_quantile": cfg_pr.get("size_quantile"),
        "sample_strata": cfg_pr.get("sample_strata"),
        "size_as_factor": cfg_pr.get("size_as_factor"),
    }

    # ---- 汇总 ----
    w_total = sum(W.values())
    w_missing_in_data = sum(v for k, v in W.items()
                            if v > 0 and not any(isinstance(r.get(k), (int, float)) for r in rows))
    w_not_evaluated = sum(v for k, v in W.items()
                          if v > 0 and not (icf.get(k) or {}).get("horizons"))
    w_weak_prior = sum(v for k, v in W.items() if k not in
                       ("score_fund", "score_news", "score_sector"))

    res = {
        "date_run": sc_name.replace("top200_scored_", "").replace(".json", "") if sc_name else None,
        "weights_version": wmeta.get("version"),
        "weights_sum": round(w_total, 4),
        "scored_file": sc_name,
        "n_rows": n_rows,
        "dims": out_dims,
        "weight_coverage": {
            "n_compared": n_cmp,
            "mean_effective_weight": round(cov_mean, 4),
            "missing_weight_pct": round((1 - cov_mean) * 100, 2),
            "total_diff_mean": round(st.mean(diffs), 3) if diffs else None,
            "total_diff_median": round(st.median(diffs), 3) if diffs else None,
            "total_diff_abs_max": max((abs(x) for x in diffs), default=None),
            "n_cross_rating_threshold": sum(1 for x in diffs if abs(x) >= 1.0),
        },
        "pool": {"data": pool_info, "config": cfg_info,
                 "in_sync": (pr.get("version") == cfg_pr.get("version")
                             and pr.get("size_quantile") == cfg_pr.get("size_quantile"))},
        "summary": {
            "w_total": round(w_total, 4),
            "w_missing_in_scored_data": round(w_missing_in_data, 4),
            "w_never_evaluated_in_daily_report": round(w_not_evaluated, 4),
            "w_weak_prior_only": round(w_weak_prior, 4),
        },
        "boundaries": [
            "本脚本只做追溯，不重算 IC、不改生产数据",
            "权重覆盖度按 apply_sector_momentum 既定口径：缺维从分母剔除，不按 0 计入",
            "effective_weight 取该行实际有值的维度权重之和，缺维即从分母消失（这是正确防呆，但会让 total 静默只反映部分权重）",
        ],
    }
    with open(os.path.join(HERE, "audit_round17.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)

    # ---- 打印 ----
    print("=" * 108)
    print(f"Round17 权重/因子穿透审计 · scored={sc_name} · 权重版本={wmeta.get('version')}")
    print("=" * 108)
    print(f"{'维度':<26}{'权重':>7}  {'日报评估':<12}{'样本(1/5/20)':<18}"
          f"{'scored落地':<12}{'组合角色':<20}")
    print("-" * 108)
    for d in out_dims:
        k = d["dim"]
        nb = d["daily_report"]["n_by_h"]
        ns = f"{nb.get('1') or 0}/{nb.get('5') or 0}/{nb.get('20') or 0}"
        print(f"{k:<26}{d['weight']:>6.1%}  "
              f"{('✅' if d['daily_report']['evaluated'] else '❌ 未评估'):<12}{ns:<18}"
              f"{('✅ ' + str(d['scored_data']['n_rows_with_value']) + '行') if d['scored_data']['present'] else '❌ 0 行':<12}"
              f"{d['mf_portfolio_role']:<20}")
    print("-" * 108)
    wc = res["weight_coverage"]
    print(f"权重合计 {res['summary']['w_total']:.2f}（应为 1.00）")
    print(f"⚠️ scored 数据里缺失的权重合计：{res['summary']['w_missing_in_scored_data']:.1%}"
          f"  → total 实际只反映 {wc['mean_effective_weight']:.1%} 的权重")
    print(f"⚠️ 日报从未单独评估的权重合计：{res['summary']['w_never_evaluated_in_daily_report']:.1%}")
    print(f"   纯先验（无统计证据）权重合计：{res['summary']['w_weak_prior_only']:.1%}")
    print()
    print(f"现权重复算 vs 文件 total：n={wc['n_compared']}  "
          f"差均值 {wc['total_diff_mean']:+.2f}  中位 {wc['total_diff_median']:+.2f}  "
          f"最大绝对差 {wc['total_diff_abs_max']:.2f}  "
          f"≥1 分的有 {wc['n_cross_rating_threshold']} 只")
    print()
    print("母体口径：")
    print(f"  数据 top500_all_a.json: version={pool_info['rules_version']} "
          f"size_quantile={pool_info['size_quantile']} strata={pool_info['sample_strata']} "
          f"size_as_factor={pool_info['size_as_factor']} sampling={pool_info['sampling']}")
    print(f"  配置 config.json     : version={cfg_info['rules_version']} "
          f"size_quantile={cfg_info['size_quantile']} strata={cfg_info['sample_strata']} "
          f"size_as_factor={cfg_info['size_as_factor']}")
    print(f"  → {'✅ 一致' if res['pool']['in_sync'] else '❌ 不一致（数据是旧配置的产物）'}")
    print(f"  → 池内流通市值 中位 {pool_info['mv_median_yi']} 亿 / 上限 {pool_info['mv_max_yi']} 亿"
          f" / log-SD {pool_info['mv_log_sd']}")
    print()
    print(f"✅ 写入 audit_round17.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
