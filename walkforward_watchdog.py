"""walkforward_watchdog.py —— 常驻式带 purge walk-forward 看门（round141 / P1-1，独立脚本）。

⚠️ 本脚本**不修改** weight_admission_gate.py：G5 是五门唯一实现，
改内部切分会翻转历史裁决、破坏可比性。现有 walk_forward 训练段与验证段
直接相接（weight_admission_gate.py:683-737）是已知偏差，本看门把它写进
disclosure，并另读 5y 诊断里已带 purge 的双向交叉验证作为补充诊断，
不重算、不新增任何判据实现。

检验项（全部落进脚本）：
  G5 折通过率 / 折内严格单调占比 / 双向 purge 交叉验证权重 L1 漂移
  NW t 与 Bonferroni 头部超额显著性 / 收缩系数 λ
告警：连续多折失效、漂移超限、头部超额持续转负、λ≈0 锁入口、非 pit-valid 数据源
输出：同源 JSON + HTML；只给 should_change_weights，**绝不改 factor_weights.json**。
裁决：暂不接入 run_daily_local.sh（2026-11-12 老大裁决）。
"""
from __future__ import annotations

import html
import json
import os
import sys

import artifact_scope as AS
import scoring_core as SC

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DIAG = os.path.join(ROOT, "data", "weight_diagnostic_5y_20261111.json")
OUT_JSON = os.path.join(ROOT, "data", "walkforward_watchdog_20261112.json")
OUT_HTML = os.path.join(ROOT, "walkforward_watchdog_20261112.html")

L1_UNSTABLE = 0.25
LAMBDA_LOCK = 0.05
CONSECUTIVE_FAIL_ALERT = 2
NEGATIVE_HEAD_ALERT_RATIO = 0.5

DISCLOSURES = [
    "G5 walk-forward 现有实现训练/验证段直接相接，无 purge/embargo；本看门不改写它。",
    "带 purge 的双向交叉验证来自 5y 诊断产物，属诊断级补充，不替代 G5 裁决。",
    "只输出 should_change_weights，不自动改权重；任何变更仍须五门 + 八处同步 + trial 预登记 + 老大裁决。",
    "G5 未过 ⇒ 禁称「经验证的 alpha」；IC 与头部超额方向相反 ⇒ 禁按 IC 排权重。",
]


def _num(v):
    return float(v) if isinstance(v, (int, float)) else None


def infer_meta(diag):
    """从诊断产物的自述披露推断口径；不硬编码 factor_parity。"""
    text = " ".join(str(x) for x in (diag.get("required_disclosures") or []))
    text += " " + str(diag.get("conclusion") or "")
    parity = not any(t in text for t in
                     ("PROXY_ONLY", "代理", "factor_parity=False", "样本内"))
    meta = SC.artifact_meta(
        kernel_mode="production" if parity else "proxy_panel",
        factor_parity=parity)
    meta["source_artifact_inferred"] = True
    return meta


def _fold_ok(f):
    g = (f or {}).get("gates") or {}
    if "pass" in g:
        return bool(g["pass"])
    keys = [k for k in g if k != "g1_monotonic_strict"]
    return bool(keys) and all(bool(g[k]) for k in keys)


def _tail_fail_streak(folds):
    n = 0
    for f in reversed(folds or []):
        if _fold_ok(f):
            break
        n += 1
    return n


def evaluate(diag, scope_meta=None):
    """只读聚合 + 告警 + 决策；不重算任何统计量。"""
    meta = scope_meta if scope_meta is not None else infer_meta(diag)
    scope = AS.classify_scope(meta)
    g = diag.get("gate_five_doors") or {}
    wf = g.get("walkforward_summary") or {}
    folds = g.get("walkforward_folds") or []
    bonf = diag.get("bonferroni") or {}
    shr = diag.get("shrinkage_rule") or {}
    fs = diag.get("factor_summary_5y") or {}

    pass_rate = _num(wf.get("pass_rate"))
    pass_thr = _num(wf.get("pass_rate_threshold"))
    n_folds = wf.get("n_folds")
    n_g1_strict = wf.get("n_g1_strict")
    l1 = _num(diag.get("candidate_half_L1_drift"))
    lam = _num(shr.get("lambda"))
    bonf_thr = _num(bonf.get("threshold")) or _num(shr.get("bonferroni_threshold"))

    single = {k: v for k, v in fs.items()
              if not k.startswith("__") and isinstance(v, dict)}
    head_ts = [_num(v.get("top10_excess_nw_t19")) for v in single.values()]
    head_ts = [x for x in head_ts if x is not None]
    max_head_t = max(head_ts) if head_ts else None
    neg_head = sum(1 for v in single.values()
                   if (_num(v.get("top10_excess_mean")) or 0.0) < 0)
    neg_ratio = (neg_head / len(single)) if single else None
    composite_t = _num((fs.get("__COMPOSITE__") or {}).get("top10_excess_nw_t19"))
    tail_fails = _tail_fail_streak(folds)

    checks = {
        "five_door_verdict": g.get("verdict"),
        "gates": g.get("gates") or {},
        "failed_gates": g.get("failed") or [],
        "g5_folds": n_folds,
        "g5_n_pass": wf.get("n_pass"),
        "g5_pass_rate": pass_rate,
        "g5_pass_rate_threshold": pass_thr,
        "g1_strict_in_folds": n_g1_strict,
        "purged_cross_L1_drift": l1,
        "purged_cross_purge_each_side": ((diag.get("oos_like_purged") or {})
                                         .get("train_first_test_second") or {})
                                        .get("purge_each_side"),
        "max_single_dim_head_excess_nw_t19": max_head_t,
        "composite_head_excess_nw_t19": composite_t,
        "bonferroni_threshold": bonf_thr,
        "lambda": lam,
        "lambda_if_composite_t": _num(shr.get("lambda_if_composite_t")),
        "n_effective_obs": _num(shr.get("n_eff")),
        "negative_head_dims": neg_head,
        "n_scored_dims": len(single),
        "negative_head_ratio": neg_ratio,
        "consecutive_tail_fold_failures": tail_fails,
        "source_reported_should_change": diag.get("should_change_weights"),
        "source_status": diag.get("status"),
    }

    alerts = []
    if tail_fails >= CONSECUTIVE_FAIL_ALERT:
        alerts.append("连续 %d 折失效" % tail_fails)
    if l1 is not None and l1 > L1_UNSTABLE:
        alerts.append("带 purge 双向交叉权重 L1 漂移 %.4f > %.2f（不稳定）"
                      % (l1, L1_UNSTABLE))
    if neg_ratio is not None and neg_ratio > NEGATIVE_HEAD_ALERT_RATIO:
        alerts.append("头部超额持续转负：%d/%d 生效维为负（%.0f%%）"
                      % (neg_head, len(single), neg_ratio * 100))
    if lam is not None and lam <= LAMBDA_LOCK:
        alerts.append("λ=%.4f ≈ 0，权重变更入口锁定" % lam)
    if scope["scope"] != AS.PIT_VALID:
        alerts.append("数据源为 %s，仅允许 proxy-diagnostic 链路"
                      % scope["artifact_status"])

    blockers = []
    if scope["scope"] != AS.PIT_VALID:
        blockers.append("非 pit-valid 产物，不得进入权重审批")
    if g.get("verdict") != "PASS":
        blockers.append("五门 verdict=%s，未过项=%s"
                        % (g.get("verdict"), g.get("failed") or []))
    if pass_rate is None or pass_thr is None or pass_rate < pass_thr:
        blockers.append("G5 折通过率 %s < 阈值 %s" % (pass_rate, pass_thr))
    if l1 is None or l1 > L1_UNSTABLE:
        blockers.append("权重 L1 漂移超限或缺失（%s）" % l1)
    if lam is None or lam <= LAMBDA_LOCK:
        blockers.append("λ 未达解锁条件（%s）" % lam)
    if max_head_t is None or bonf_thr is None or max_head_t < bonf_thr:
        blockers.append("生效单因子头部超额最大 NW t %s < Bonferroni %s"
                        % (max_head_t, bonf_thr))

    should = not blockers
    return {
        "purpose": "常驻式 walk-forward 看门（独立脚本，未接日链）",
        "source_artifact": DEFAULT_DIAG,
        "scope": scope,
        "checks": checks,
        "alerts": alerts,
        "blockers": blockers,
        "should_change_weights": should,
        "requires_manual_arbitration": True,
        "weights_modified": False,
        "disclosures": DISCLOSURES + [scope["disclosure"]],
        "red_line": ("即使 should_change_weights=true，也须五门 + 八处同步 + "
                     "trial 预登记 + verify_weights_round36 PASS + 老大裁决后方可落地。"),
    }


def render_html(r):
    def esc(x):
        return html.escape(str(x))
    rows = "".join(
        "<tr><td>%s</td><td><code>%s</code></td></tr>"
        % (esc(k), esc(v)) for k, v in r["checks"].items())
    alerts = "".join("<li>%s</li>" % esc(x) for x in r["alerts"]) or "<li>无</li>"
    blockers = "".join("<li>%s</li>" % esc(x) for x in r["blockers"]) or "<li>无</li>"
    discs = "".join("<li>%s</li>" % esc(x) for x in r["disclosures"])
    verdict = "TRUE" if r["should_change_weights"] else "FALSE"
    color = "#A32D2D" if r["should_change_weights"] else "#0F6E56"
    return (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
        '<title>walk-forward 看门 · 2026-11-12</title>'
        '<body style="font-family:sans-serif;max-width:960px;margin:24px auto;'
        'line-height:1.6;color:#222">'
        '<h2 style="font-weight:500">walk-forward 看门（独立脚本，未接日链）</h2>'
        '<p>%s</p>' % AS.render_badge(r["scope"]) +
        '<p>should_change_weights = <b style="color:%s">%s</b> '
        '（仅输出结论，不自动改权重）</p>' % (color, verdict) +
        '<h3 style="font-weight:500">检验项</h3><table border="1" '
        'cellspacing="0" cellpadding="6" style="border-collapse:collapse;font-size:13px">'
        '<tr><th>项</th><th>值</th></tr>%s</table>' % rows +
        '<h3 style="font-weight:500">告警</h3><ul>%s</ul>' % alerts +
        '<h3 style="font-weight:500">阻断项</h3><ul>%s</ul>' % blockers +
        '<h3 style="font-weight:500">口径与红线</h3><ul>%s</ul>' % discs +
        '<p><b>%s</b></p></body></html>' % esc(r["red_line"]))


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DIAG
    with open(src, encoding="utf-8") as f:
        diag = json.load(f)
    r = evaluate(diag)
    r["source_artifact"] = os.path.abspath(src)
    out_j = sys.argv[2] if len(sys.argv) > 2 else OUT_JSON
    os.makedirs(os.path.dirname(out_j), exist_ok=True)
    with open(out_j, "w", encoding="utf-8") as f:
        json.dump(r, f, ensure_ascii=False, indent=1)
    with open(OUT_HTML, "w", encoding="utf-8") as f:
        f.write(render_html(r))
    print("should_change_weights =", r["should_change_weights"])
    print("alerts:", len(r["alerts"]), " blockers:", len(r["blockers"]))
    for a in r["alerts"]:
        print("  ALERT:", a)
    for b in r["blockers"]:
        print("  BLOCK:", b)
    print("json:", out_j)
    print("html:", OUT_HTML)


if __name__ == "__main__":
    main()
