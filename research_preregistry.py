#!/usr/bin/env python3
"""research_preregistry.py —— 候选预注册 + 真实数据消融（round130 / P1-4）。

审计 §五：最突出的过拟合风险是「查看头部名单 → 修改规则 → 名单更符合直觉
→ 宣告改善」的人工反馈循环。改变规则后再看同一段历史，不产生新样本外证据。

本模块把「候选方案」在**看结果之前**冻结进代码（随 git 入库，任何改动都
会在 diff 中暴露），并要求每个候选先声明假设与验收标准；评估统一走
`ablation_long_panel()` 的同一份真实数据、同一套合成分内核。

⚠️ 登记表不是 json 配置文件：本项目 `.gitignore` 忽略 `*.json`，
   配置若落 json 会在只靠 git 部署时静默失效（历史事故）。冻结信息写在
   代码常量里，`--verify` 输出 manifest_sha256 供产物绑定与事后比对。

CLI:
    python research_preregistry.py --verify
    python research_preregistry.py --ablation --out data/ablation_round130.json
"""
import argparse
import hashlib
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
MAT = os.path.join(HERE, "data", "panel_r124_mat.npz")
KL = os.path.join(HERE, "data", "klines_2020.npz")
OUT = os.path.join(HERE, "data", "ablation_round130.json")

# ═══════════════════════════════════════════════════════════════════════════
# 预注册表 —— 冻结时间 2026-11-05，登记时**未查看**任何候选的头部名单/收益结果。
# 任何修改都会改变 manifest_sha256 并出现在 git diff 中。
# ═══════════════════════════════════════════════════════════════════════════
PREREGISTERED = [
    {
        "id": "QN-01",
        "registered_at": "2026-11-05",
        "formula": "质量子因子逐列截面 rank→正态分位(ndtri, clip±3)，再按 Q_GROUPS 组内等权、三组等权",
        "direction": "越高越好（方向已在 sub_factors_pit 内统一）",
        "hypothesis": "消除 ocfg/accruals 等百分数与 roe/ocf_ni 的量纲混均，质量分应更贴近资本回报与现金兑现",
        "acceptance": "① 消融中去掉质量维后 IC 降幅不小于 baseline 的噪声带；② 分年度 IC 符号不翻转；③ 通过冻结前瞻 OOS 后才可切换默认",
        "oos_plan": "切换前须先冻结未来 6 个月真实前瞻记录，不得用构造期数据自评",
        "implemented": True,
        "module": "quality_normalize.score_rows_normalized(method='rank_z')",
        "status": "CANDIDATE_ONLY_NOT_IN_PRODUCTION",
    },
    {
        "id": "QN-02",
        "registered_at": "2026-11-05",
        "formula": "质量子因子 1/99 缩尾 + (x−median)/(IQR/1.349) 稳健 z，clip±3，再组内等权",
        "direction": "越高越好",
        "hypothesis": "保留基数信息的同时抑制极端值；与 QN-01 比较以判断 rank 变换是否损失过多信息",
        "acceptance": "与 QN-01 用同一消融口径比较；不得因某次头部名单更顺眼而改选",
        "oos_plan": "同上，须冻结前瞻 OOS",
        "implemented": True,
        "module": "quality_normalize.score_rows_normalized(method='winsor_iqr')",
        "status": "CANDIDATE_ONLY_NOT_IN_PRODUCTION",
    },
    {
        "id": "GR-01",
        "registered_at": "2026-11-05",
        "formula": "成长拆为「长期存量(cagr3/多期一致性) / 近期加速 / 现金兑现」三分组，各自连续分位后再加权",
        "direction": "越高越好",
        "hypothesis": "现行单季增速动量与 cagr3 秩相关≈0，需分离低基数脉冲与可持续增长",
        "acceptance": "须先补齐多期收入/扣非利润一致性与现金兑现数据，再预注册阈值",
        "oos_plan": "待实现，禁止先用名单试阈值",
        "implemented": False,
        "module": None,
        "status": "PROPOSED_NOT_IMPLEMENTED",
    },
    {
        "id": "VA-01",
        "registered_at": "2026-11-05",
        "formula": "PEG 分母改为 mean(cagr3, 阶梯隐含增速) 的连续 log 增速变换，取消硬阶梯断点",
        "direction": "PEG 越低越好",
        "hypothesis": "阶梯隐含增速会在阈值处制造排名断点，连续变换应降低断点敏感性",
        "acceptance": "断点敏感性检验 + 同一消融口径；不得按头部名单调断点",
        "oos_plan": "待实现，须冻结前瞻 OOS",
        "implemented": False,
        "module": None,
        "status": "PROPOSED_NOT_IMPLEMENTED",
    },
]

ABLATION_DIMS = ("score_growth", "score_quality", "score_ep")


def manifest_sha256():
    blob = json.dumps(PREREGISTERED, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def verify_registry():
    """校验登记完整性；返回可落盘的 dict（不检查外部文件，避免 json 配置坑）。"""
    ids = [c["id"] for c in PREREGISTERED]
    problems = []
    if len(ids) != len(set(ids)):
        problems.append("候选 ID 重复")
    for c in PREREGISTERED:
        for k in ("id", "formula", "hypothesis", "acceptance", "oos_plan",
                  "registered_at", "implemented"):
            if k not in c or c[k] in (None, ""):
                problems.append(f"{c.get('id')} 缺字段 {k}")
        if c.get("status") == "IN_PRODUCTION":
            problems.append(f"{c['id']} 未经冻结 OOS 不得标为 IN_PRODUCTION")
    return {
        "n_candidates": len(PREREGISTERED),
        "n_implemented": sum(1 for c in PREREGISTERED if c["implemented"]),
        "manifest_sha256": manifest_sha256(),
        "registered_at": "2026-11-05",
        "problems": problems,
        "ok": not problems,
        "note": ("登记表随代码入库；修改会改变 manifest_sha256 并在 git diff 中暴露。"
                 "禁止在看到头部名单/收益后再调整候选或阈值。"),
    }


def ablation_long_panel(h=20, min_cs=200):
    """同一份真实长面板上的「留一维度」消融（信息归零但量尺不变）。

    做法：把待消融维度整列置 NaN → 统一内核按中性分填充，分母/权重不变，
    因此「去掉信息」与「换尺子」被严格分离。IC 用非重叠 h 日截面的
    Spearman（rank 后 Pearson），样本区间与有效截面数一并披露。
    """
    import numpy as np
    from dim_cfg import load_dim_max
    from scipy.stats import rankdata
    from scoring_core import artifact_meta, classify_artifact, composite_total_matrix

    with open(os.path.join(HERE, "factor_weights.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    W = {k: float(v) for k, v in (cfg.get("weights") or {}).items()
         if float(v or 0) > 0}
    DM = load_dim_max(["score_bias60", "score_rsi_rev"], strict=True)

    z = np.load(MAT, allow_pickle=False)
    k = np.load(KL, allow_pickle=False)
    pdates = [str(d) for d in z["dates"]]
    codes = [str(c) for c in z["codes"]]
    dims = [str(d) for d in z["dims"]]
    S = z["S"]
    kdates = [str(d) for d in k["dates"]]
    kcodes = [str(c) for c in k["codes"]]
    close = k["X"][:, :, 1]
    if kcodes != codes:
        kmap = {c: i for i, c in enumerate(kcodes)}
        close = close[[kmap[c] for c in codes]]
    kdmap = {d: i for i, d in enumerate(kdates)}

    sel = list(range(0, max(0, len(pdates) - h - 1), h))   # 非重叠截面
    S_sel = np.asarray(S[sel], dtype=np.float64)
    comp, cmeta = composite_total_matrix(S_sel, dims, W, DM)

    ret_cache = {}
    for j, n in enumerate(sel):
        d = pdates[n]
        di = kdmap.get(d)
        if di is None or di + h >= close.shape[1]:
            continue
        p0 = close[:, di].astype(np.float64)
        p1 = close[:, di + h].astype(np.float64)
        with np.errstate(invalid="ignore", divide="ignore"):
            ret = p1 / np.where(p0 > 0, p0, np.nan) - 1.0
        ret_cache[j] = ret

    def ic_of(cmat):
        ics = []
        for j, ret in ret_cache.items():
            m = np.isfinite(cmat[j]) & np.isfinite(ret)
            if int(m.sum()) < min_cs:
                continue
            a, b = rankdata(cmat[j][m]), rankdata(ret[m])
            if a.std() > 1e-12 and b.std() > 1e-12:
                ics.append(float(np.corrcoef(a, b)[0, 1]))
        return (round(float(np.mean(ics)), 5) if ics else None), len(ics)

    base_ic, n_sec = ic_of(comp)
    abl = {}
    for dim in ABLATION_DIMS:
        if dim not in dims:
            abl[dim] = {"error": "该维不在面板列中"}
            continue
        S2 = S_sel.copy()
        S2[..., dims.index(dim)] = np.nan
        comp2, _ = composite_total_matrix(S2, dims, W, DM)
        ic2, n2 = ic_of(comp2)
        abl[dim] = {
            "ic_without_dim": ic2, "n_sections": n2,
            "delta_ic_vs_baseline": (round(ic2 - base_ic, 5)
                                     if (ic2 is not None and base_ic is not None)
                                     else None),
            "interpretation": ("负值 = 该维提供正向边际信息；接近 0 = 该维信息被其他维吸收"),
        }
    _am = artifact_meta(kernel_mode="production", factor_parity=False)
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "h": h, "n_sections": n_sec,
        "section_rule": f"每 {h} 个交易日取一个非重叠截面",
        "min_cross_section": min_cs,
        "baseline_ic": base_ic,
        "baseline_composite": cmeta,
        "ablations": abl,
        "preregistry_manifest_sha256": manifest_sha256(),
        "kernel_class": classify_artifact(_am),
        "disclosure": ("消融是**样本内构造期**的诊断，代理面板因子、无 purge、无样本外；"
                       "只能判断各维边际信息量，不能证明策略有效性，也不构成切换默认实现的依据。"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--ablation", action="store_true")
    ap.add_argument("--h", type=int, default=20)
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()
    if args.verify or not args.ablation:
        print(json.dumps(verify_registry(), ensure_ascii=False, indent=2))
        if not args.ablation:
            return 0
    r = ablation_long_panel(args.h)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(r, f, ensure_ascii=False, indent=1)
    print(json.dumps({k: v for k, v in r.items()
                      if k != "baseline_composite"}, ensure_ascii=False, indent=2))
    print(f"\n写出 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
