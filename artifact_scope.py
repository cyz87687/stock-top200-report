"""artifact_scope.py —— 口径隔离唯一实现（round141 / P1-3）。

两条验证链路永久隔离，禁止混报：
  proxy-diagnostic  仅用于因子方向探索，红色水印，禁止用于权重审批
  pit-valid         唯一权重变更准入链路

四态判定不在本模块重写，唯一来源是 scoring_core.classify_artifact /
artifact_meta。本模块只负责把四态映射到链路、强制校验与渲染徽标。

设计约束（来自项目既有裁决）：
  - 不改 scoring_core.py（唯一内核；改则 kernel_id 变化、历史产物失效）
  - 不改 weight_admission_gate 的 G1~G5 判定，不新增第六道门
  - 不自动改权重，只做校验与披露
"""
from __future__ import annotations

import html

import scoring_core as SC

PROXY_DIAGNOSTIC = "proxy-diagnostic"
PIT_VALID = "pit-valid"
SCOPES = (PROXY_DIAGNOSTIC, PIT_VALID)

_RED_DISCLOSURE = (
    "PROXY_ONLY / 代理面板诊断：仅用于因子方向探索，不得用于权重审批，"
    "不得写作「经验证的 alpha」。"
)
_VALID_DISCLOSURE = (
    "pit-valid：因子逐维复现生产算法且内核指纹一致；"
    "仍须通过五门 G1~G5 与 Bonferroni 才允许讨论权重变更。"
)


class ScopeViolation(ValueError):
    """产物链路混用，或把非 PIT 产物送进权重审批。"""


def classify_scope(meta):
    """由产物元数据判定其允许进入的链路（唯一实现）。"""
    c = SC.classify_artifact(meta or {}, strict=False)
    status = c["status"]
    scope = PIT_VALID if status == "COMPARABLE" else PROXY_DIAGNOSTIC
    return {
        "scope": scope,
        "artifact_status": status,
        "comparable": bool(c["comparable"]),
        "factor_parity": bool(c["factor_parity"]),
        "kernel_id": c.get("kernel_id"),
        "current_kernel_id": c.get("current_kernel_id"),
        "reason": c["reason"],
        "watermark": "red" if scope == PROXY_DIAGNOSTIC else "none",
        "weight_approval_allowed": scope == PIT_VALID,
        "disclosure": _VALID_DISCLOSURE if scope == PIT_VALID else _RED_DISCLOSURE,
        "proxy_dims": dict(c.get("proxy_dims") or {}),
    }


def assert_scope(meta, expected_scope, artifact_name=None):
    """声明产物所属链路，与元数据判定不一致即抛 ScopeViolation。"""
    if expected_scope not in SCOPES:
        raise ScopeViolation(
            "未知链路 %r，允许值 %s" % (expected_scope, list(SCOPES)))
    got = classify_scope(meta)
    if got["scope"] != expected_scope:
        raise ScopeViolation(
            "产物 %s 被声明为 %s，但元数据判定为 %s（%s）：%s"
            % (artifact_name or "<unnamed>", expected_scope, got["scope"],
               got["artifact_status"], got["reason"]))
    return got


def assert_weight_approval(meta, artifact_name=None):
    """权重审批唯一入口：非 pit-valid 一律拒绝。"""
    got = classify_scope(meta)
    if not got["weight_approval_allowed"]:
        raise ScopeViolation(
            "权重审批拒绝：产物 %s 为 %s，仅允许 %s 链路；%s"
            % (artifact_name or "<unnamed>", got["artifact_status"],
               PROXY_DIAGNOSTIC, got["reason"]))
    return got


def audit_report_sources(items, purpose="diagnostic"):
    """批量校验一份报告里的产物标签，禁止混用。

    items: [(name, meta, declared_scope), ...]，declared_scope 可省略。
    purpose == "weight_approval" 时，出现任何代理产物即失败。
    """
    rows = []
    violations = []
    seen = set()
    for item in items:
        name, meta = item[0], item[1]
        declared = item[2] if len(item) > 2 else None
        got = classify_scope(meta)
        if declared is not None:
            try:
                assert_scope(meta, declared, name)
            except ScopeViolation as e:
                violations.append(str(e))
        seen.add(got["scope"])
        rows.append({"name": name, **got})
    mixed = len(seen) > 1
    if mixed:
        violations.append(
            "同一报告混入两条链路 %s（禁止混报）" % ", ".join(sorted(seen)))
    if purpose == "weight_approval":
        for r in rows:
            if r["scope"] == PROXY_DIAGNOSTIC:
                violations.append(
                    "权重审批报告混入代理产物：%s（%s）"
                    % (r["name"], r["artifact_status"]))
    return {
        "purpose": purpose,
        "scopes": sorted(seen),
        "mixed": mixed,
        "n_items": len(rows),
        "items": rows,
        "violations": violations,
        "ok": not violations,
    }


def render_badge(meta):
    """HTML 徽标：proxy-diagnostic 用红色水印，pit-valid 用中性标签。"""
    g = classify_scope(meta)
    if g["scope"] == PROXY_DIAGNOSTIC:
        return ('<span style="color:#A32D2D;border:1px solid #A32D2D;'
                'padding:2px 6px;border-radius:4px;font-size:12px">'
                'proxy-diagnostic / %s / 禁用于权重审批</span>'
                % html.escape(g["artifact_status"]))
    return ('<span style="color:#0F6E56;border:1px solid #0F6E56;'
            'padding:2px 6px;border-radius:4px;font-size:12px">'
            'pit-valid / %s</span>' % html.escape(g["artifact_status"]))
