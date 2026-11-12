"""weight_perturbation.py —— 扰动稳定性前置检查（round141 / P1-2）。

三种扰动：少量个股随机替换 / 因子值小幅高斯扰动 / 入池边界微移。
输出净值跳动、权重 L1 漂移与稳定性得分。

⚠️ 本模块**只标记「噪声敏感」，不否决候选权重**、不改 G1~G5 判定、
不自动改权重。依据 weight_admission_gate.py:398-412：
「不把它做成第六道门禁是有意的：G1~G5 的统计含义已经固定，
悄悄加一道会改变历史裁决的可比性」。前置检查 ≠ 第六道门。
"""
from __future__ import annotations

import copy
import random

MODES = ("replace_codes", "gaussian_noise", "pool_boundary")
SCORE_PREFIX = "score_"
DEFAULT_NET_JUMP_THRESHOLD = 0.05   # 净值绝对跳动 5 个百分点
DEFAULT_L1_THRESHOLD = 0.25         # 与 P1-1 权重 L1 漂移阈值一致
DISCLOSURE = ("前置检查，非第六道门禁；只输出稳定性得分与标记，"
              "否决权仍归 G1~G5，不改变历史裁决可比性。")


def perturb_rows(rows, mode, *, seed, sigma=0.02, frac=0.01,
                 boundary_shift=1, dim_max=10.0):
    """返回 (new_rows, meta)；输入 rows 不被修改。"""
    if mode not in MODES:
        raise ValueError("未知扰动方式 %r，允许 %s" % (mode, list(MODES)))
    rng = random.Random(int(seed))
    out = copy.deepcopy(list(rows))
    if not out:
        return out, {"mode": mode, "seed": int(seed), "n_rows": 0}

    if mode == "replace_codes":
        n = max(1, int(round(len(out) * float(frac))))
        idxs = rng.sample(range(len(out)), min(n, len(out)))
        donors = rng.sample(range(len(out)), min(len(idxs), len(out)))
        for i, j in zip(idxs, donors):
            repl = copy.deepcopy(out[j])
            repl["code"] = "%s~P" % out[i].get("code", i)
            out[i] = repl

    elif mode == "gaussian_noise":
        keys = [k for k in out[0]
                if k.startswith(SCORE_PREFIX)
                and isinstance(out[0][k], (int, float))]
        sd = float(sigma) * float(dim_max)
        for r in out:
            for k in keys:
                v = r.get(k)
                if isinstance(v, (int, float)):
                    r[k] = round(min(max(float(v) + rng.gauss(0.0, sd), 0.0),
                                     float(dim_max)), 6)

    elif mode == "pool_boundary":
        n = max(1, int(boundary_shift))
        if "total" in out[0]:
            order = sorted(range(len(out)),
                           key=lambda i: out[i].get("total") or 0.0,
                           reverse=True)
            edge = order[:max(n * 2, 2)]
        else:
            edge = list(range(min(max(n * 2, 2), len(out))))
        donors = rng.sample(range(len(out)), min(len(edge), len(out)))
        for i, j in zip(edge, donors):
            repl = copy.deepcopy(out[j])
            repl["code"] = "%s~B" % out[i].get("code", i)
            out[i] = repl

    return out, {
        "mode": mode, "seed": int(seed), "sigma": float(sigma),
        "frac": float(frac), "boundary_shift": int(boundary_shift),
        "n_rows": len(out),
    }


def _net(r):
    v = (r or {}).get("net_return_pct")
    return float(v) / 100.0 if isinstance(v, (int, float)) else 0.0


def _weights(r):
    return dict((r or {}).get("weights") or {})


def _l1(a, b):
    keys = set(a) | set(b)
    return sum(abs(float(a.get(k, 0.0)) - float(b.get(k, 0.0))) for k in keys)


def stability_score(base, perturbed, *,
                    net_jump_threshold=DEFAULT_NET_JUMP_THRESHOLD,
                    l1_threshold=DEFAULT_L1_THRESHOLD):
    """只算分与标记，不做任何准入裁决。"""
    b_net = _net(base)
    b_w = _weights(base)
    net_jumps, l1_drifts = [], []
    for p in perturbed:
        net_jumps.append(abs(_net(p) - b_net))
        l1_drifts.append(_l1(b_w, _weights(p)))
    net_max = max(net_jumps) if net_jumps else 0.0
    l1_max = max(l1_drifts) if l1_drifts else 0.0
    s_net = (max(0.0, 1.0 - net_max / net_jump_threshold)
             if net_jump_threshold > 0 else 1.0)
    s_l1 = (max(0.0, 1.0 - l1_max / l1_threshold)
            if l1_threshold > 0 else 1.0)
    sensitive = (net_max > net_jump_threshold) or (l1_max > l1_threshold)
    return {
        "n_perturbations": len(perturbed),
        "net_jump_max": round(net_max, 6),
        "l1_drift_max": round(l1_max, 6),
        "net_jump_threshold": float(net_jump_threshold),
        "l1_threshold": float(l1_threshold),
        "stability_score": round((s_net + s_l1) / 2.0, 4),
        "noise_sensitive": bool(sensitive),
        "verdict": "NOISE_SENSITIVE_MARK_ONLY" if sensitive else "STABLE",
        "action": ("标记为噪声敏感，不采纳与否由既有 G1~G5 裁决"
                   if sensitive else "无需动作"),
        "disclosure": DISCLOSURE,
    }


def run_suite(evaluate_fn, rows, *, seeds=(1, 2, 3), modes=MODES,
              perturb_kwargs=None, score_kwargs=None):
    """跑一组扰动并返回同源报告。evaluate_fn(rows) -> dict，须含 net_return_pct。"""
    perturb_kwargs = dict(perturb_kwargs or {})
    score_kwargs = dict(score_kwargs or {})
    base = evaluate_fn(rows)
    per, results = [], []
    for mode in modes:
        for seed in seeds:
            pr, meta = perturb_rows(rows, mode, seed=seed, **perturb_kwargs)
            results.append(evaluate_fn(pr))
            per.append(meta)
    return {
        "base": base,
        "perturbations": per,
        "stability": stability_score(base, results, **score_kwargs),
    }
