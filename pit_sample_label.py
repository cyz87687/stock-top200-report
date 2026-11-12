"""pit_sample_label.py —— 三类样本标签唯一实现（round141 / P0-2）。

标签阶梯（所有报告强制展示）：
  proxy       数据源为代理因子 / 近似 PIT / 样本内，仅可做方向探索
  quasi-oos   权重冻结后，但 h20 前向收益未完整或未越过 purge 边界
  strict-oos  冻结后 + PIT vintage + 逐维因子对齐 + h 完整 + 过 purge

硬性事实（2026-11-12 实测，避免把标签写成空话）：
  factor_weights.json fitted_at = 2026-11-04 23:49:54
  data/panel_r124_mat.npz 末日 = 2026-10-30（冻结后 0 个交易日）
  data/pit_audit_round130.json 带公告时点/修订版本的行 = 0 / 272240
  ⇒ 当前 strict-oos 计数必然为 0，正式滚动验证尚未具备启动条件。

本模块不编节假日表：交易日序列一律由调用方注入真实日历。
"""
from __future__ import annotations

import bisect
import json
import os

PROXY = "proxy"
QUASI_OOS = "quasi-oos"
STRICT_OOS = "strict-oos"
LABELS = (PROXY, QUASI_OOS, STRICT_OOS)

DEFAULT_HORIZON = 20       # factor_weights.json target_horizon
DEFAULT_PURGE = 20         # h=20 前向收益不跨越训练/验证切点
DEFAULT_OOS_TRIGGER = 30   # 累计干净 OOS h20 截面 >= 30 才启动正式滚动验证
STRICT_PIT_MODE = "strict_vintage"

_DISCLOSURE = {
    PROXY: "代理/样本内：仅用于因子方向探索，不得用于权重审批或写作「经验证的 alpha」。",
    QUASI_OOS: "准样本外：前向收益未完整或未过 purge，不得计入严格 OOS 计数。",
    STRICT_OOS: "严格样本外：可作为验证集，永不参与权重拟合。",
}


def _d(s):
    return str(s)[:10]


def normalize_calendar(dates):
    """真实交易日序列（去重升序）；不生成、不外推任何日期。"""
    return sorted({_d(x) for x in dates if x})


def _at_or_before(cal, d):
    """截至 d 的最后一个已知交易日索引。"""
    i = bisect.bisect_right(cal, d) - 1
    return i if i >= 0 else None


def label_snapshot(date, *, asof, weight_fitted_at, calendar,
                   horizon=DEFAULT_HORIZON, purge=DEFAULT_PURGE,
                   factor_parity=False, has_pit_vintage=False,
                   pit_mode="statutory_deadline_approx"):
    """判定单个截面标签。判据顺序不可颠倒（先数据源，再样本内外，再时间边界）。"""
    date, asof, fitted = _d(date), _d(asof), _d(weight_fitted_at)
    cal = normalize_calendar(calendar)
    i_d = bisect.bisect_left(cal, date)
    i_d = i_d if i_d < len(cal) and cal[i_d] == date else None
    i_a = _at_or_before(cal, asof)
    i_f = _at_or_before(cal, fitted)

    if i_d is None or i_a is None or i_f is None:
        return {
            "date": date, "asof": asof, "label": None,
            "reason": "日期不在已注入的真实交易日历中，拒绝给标签",
            "checks": {}, "must_display": True,
            "disclosure": "无标签即不得进入任何验证统计。",
        }

    h_complete = (i_a - i_d) >= int(horizon)
    purge_ok = (i_d - i_f) >= int(purge)
    is_oos = date > fitted
    data_ok = bool(factor_parity) and bool(has_pit_vintage) and pit_mode == STRICT_PIT_MODE

    checks = {
        "factor_parity": bool(factor_parity),
        "has_pit_vintage": bool(has_pit_vintage),
        "pit_mode": pit_mode,
        "after_weight_freeze": is_oos,
        "h20_complete": h_complete,
        "purge_ok": purge_ok,
        "trading_days_to_asof": i_a - i_d,
        "trading_days_since_freeze": i_d - i_f,
    }

    if not data_ok:
        label = PROXY
        reason = "数据源为代理因子或近似 PIT（无公告版本/修订号）"
    elif not is_oos:
        label = PROXY
        reason = "截面不晚于权重冻结日，属样本内（权重已见过该段）"
    elif not h_complete:
        label = QUASI_OOS
        reason = "前向收益窗口 h=%d 尚未完整" % int(horizon)
    elif not purge_ok:
        label = QUASI_OOS
        reason = "距冻结日不足 purge=%d 个交易日，训练/验证边界未隔离" % int(purge)
    else:
        label = STRICT_OOS
        reason = "冻结后 + PIT vintage + 因子对齐 + h 完整 + 过 purge"

    return {
        "date": date, "asof": asof, "label": label, "reason": reason,
        "checks": checks, "must_display": True,
        "disclosure": _DISCLOSURE[label],
        "fit_participation": "forbidden" if label != PROXY else "proxy_only",
    }


def label_all(dates, **kwargs):
    """批量打标；kwargs 透传 label_snapshot。"""
    return [label_snapshot(d, **kwargs) for d in dates]


def oos_gate(labels, threshold=DEFAULT_OOS_TRIGGER):
    """累计干净 OOS h20 截面门禁：>= threshold 才自动启动正式滚动验证。"""
    n = 0
    for x in labels:
        lab = x if isinstance(x, str) else (x or {}).get("label")
        if lab == STRICT_OOS:
            n += 1
    ready = n >= int(threshold)
    return {
        "n_strict_oos": n,
        "threshold": int(threshold),
        "start_formal_validation": ready,
        "verdict": "READY" if ready else "WAIT_FOR_DATA",
        "disclosure": ("达到阈值，可启动正式滚动验证（仍须五门 G1~G5 + Bonferroni）"
                       if ready else
                       "严格 OOS 截面不足，should_change_weights 只能为 false"),
    }


def load_project_defaults(root=None):
    """从项目真实产物读取冻结日、h 与交易日序列；不编节假日表。"""
    root = root or os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(root, "factor_weights.json"), encoding="utf-8") as f:
        fw = json.load(f)
    fitted = _d(fw.get("fitted_at") or "")
    horizon = int(fw.get("target_horizon") or DEFAULT_HORIZON)
    cal = []
    npz = os.path.join(root, "data", "panel_r124_mat.npz")
    if os.path.exists(npz):
        import numpy as np
        with np.load(npz, allow_pickle=True) as z:
            cal += [_d(x) for x in z["dates"]]
    fhp = os.path.join(root, "factor_history.json")
    if os.path.exists(fhp):
        with open(fhp, encoding="utf-8") as f:
            cal += [_d(s.get("date")) for s in json.load(f).get("snapshots", [])]
    return {
        "weight_fitted_at": fitted,
        "horizon": horizon,
        "calendar": normalize_calendar(cal),
    }
