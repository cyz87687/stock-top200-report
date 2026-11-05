#!/usr/bin/env python3
"""scoring_core.py —— 生产/历史评分内核的唯一来源 + 内核指纹守卫。

round130 / P0-1（2026-11-05）修复背景
--------------------------------------
审计确认三处「研究对象漂移」：

1. `recompute_ic_long_v2.py:143-157` 的历史合成分用
   ``comp = (S @ W/DMAX) / (mask @ W/DMAX)`` —— **分母随每只股票缺几维而变**，
   且缺维从分子分母一起剔除；生产 `scale_calc.composite_total` 用
   **固定名义权重分母 + 缺维中性填充**。两把尺子不是同一模型。
2. `build_panel_long.py:373-455` 的历史因子是**代理实现**（size_growth=市值分位、
   quality=ROE 分位、growth=归母同比分位、fund=None），并未复现生产逐维算法，
   更没有 v4.19 三门槛。因此长面板 IC 不能证明生产模型有效。
3. `data/ic_long_2020.json` 仍标 v4.18，但被页面当作当前模型的权威证据。

本模块的处置（**不改生产分数，不动权重**）
------------------------------------------
* `composite_total_matrix()`：与 `scale_calc.composite_total` **逐股等价**的
  向量化实现，历史链统一改调它（单一内核）。
* `kernel_fingerprint()`：把权重版本 / dim_max / 中性填充比例 / 核心源码 SHA-256
  压成一个 `kernel_id`，产物必须携带。
* `classify_artifact()`：读取产物时校验内核代际与「因子是否逐维复现」，
  旧口径或代理面板一律判为 **不可互证**，禁止当作生产模型证据。

⚠️ 边界（必须如实披露，不是待办）
   历史面板的**因子层**无法靠改代码凭空补齐：逐日资金流快照、真实公告时点、
   历史修订版财报在本地都不存在。本模块能完整统一的是**合成层**（量尺/缺失规则/
   量程），因子层通过 `factor_parity=False` 显式标为代理并阻断误用。
   要真正逐维一致，必须重建 PIT 历史数据源（见 `pit_store.py` 与报告）。
"""
import hashlib
import json
import os

import numpy as np

from scale_calc import (NEUTRAL_FILL_RATIO, composite_total, is_num,
                        sum_all_weights)

HERE = os.path.dirname(os.path.abspath(__file__))

# 生产评分链的核心源码/配置。任一字节变化 → kernel_id 变化 → 旧产物自动失效。
# 刻意不含本文件自身，避免「计算指纹的动作改变指纹」自指。
CORE_FILES = (
    "scale_calc.py",
    "factor_weights.json",
    "dim_cfg.py",
    "fund_split.py",
    "growth_factor_b.py",
    "step2_stockscorer_v2.py",
    "enhance_scores.py",
    "apply_sector_momentum.py",
)

# 历史长面板里用「代理」而非生产算法构造的维（见 build_panel_long.py 头部口径）。
PANEL_PROXY_DIMS = {
    "score_size_growth": "流通市值分位（小市值优先），非生产 size_growth 复合分",
    "score_quality": "ROE 单指标分位，非生产三族质量分",
    "score_growth": "归母净利同比分位，非生产成长存量+方式B+三门槛",
    "score_fund": "逐日资金流无历史快照 → 全 None（真盲区）",
    "score_dominant": "业务份额回填，未含生产门禁",
}


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def kernel_fingerprint(base_dir=None):
    """返回当前生产内核指纹 dict（含 kernel_id）。失败不静默：直接抛。"""
    base = base_dir or HERE
    with open(os.path.join(base, "factor_weights.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    active = {k: float(v) for k, v in (cfg.get("weights") or {}).items()
              if is_num(float(v or 0)) and float(v) > 0}
    files = {}
    for name in CORE_FILES:
        p = os.path.join(base, name)
        if os.path.exists(p):
            files[name] = _sha256(p)
    payload = {
        "version": cfg.get("version"),
        "target_horizon": cfg.get("target_horizon"),
        "weights": active,
        "dim_max": cfg.get("dim_max") or {},
        "neutral_fill_ratio": NEUTRAL_FILL_RATIO,
        "sum_active_weights": round(sum(active.values()), 10),
        "code_sha256": files,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")
    return {"kernel_id": hashlib.sha256(blob).hexdigest()[:16], **payload}


def composite_total_matrix(S, dims, weights, dim_max,
                           neutral_ratio=NEUTRAL_FILL_RATIO):
    """`scale_calc.composite_total` 的向量化等价实现（百分制 0-100）。

    S : ndarray, 最后一轴长度 = len(dims)；NaN 表示缺维，其余维可为任意形状
        （历史面板是 (日期, 股票, 维)，也支持 (股票, 维)）。
    weights / dim_max : 与生产同一份配置（**必须传全量**，不能只传矩阵里有的列）。

    与生产严格一致的两条规则：
      ① 分母恒为 Σ「全部生效权重」`sum_all_weights(weights)`，不随缺维变化；
      ② 缺维（含 W 中生效但矩阵**整列缺失**的维）在分子填 `DMAX/2` 中性分。

    返回 (total, meta)。total 与标量版在浮点误差内逐股相等（见 selfcheck）。
    """
    S = np.asarray(S, dtype=np.float64)
    dims = list(dims)
    if S.shape[-1] != len(dims):
        raise ValueError(f"最后一轴 {S.shape[-1]} ≠ dims 长度 {len(dims)}")
    active = {k: float(v) for k, v in (weights or {}).items()
              if is_num(float(v or 0)) and float(v) > 0}
    den = sum_all_weights(active)
    if not is_num(den) or den <= 0:
        raise ValueError("生效权重之和必须 > 0")

    num = np.zeros(S.shape[:-1], dtype=np.float64)
    present_w = 0.0
    for i, d in enumerate(dims):
        w = active.get(d, 0.0)
        if w <= 0:
            continue
        mx = float((dim_max or {}).get(d) or 10.0)
        if mx <= 0:
            mx = 10.0
        v = S[..., i]
        mask = ~np.isnan(v)                       # inf 不算缺维：clip 后等价于满分/零分
        clean = np.where(mask, v, 0.0)
        norm = np.where(mask, np.clip(clean, 0.0, mx) / mx * 10.0,
                        neutral_ratio * 10.0)
        num = num + norm * w
        present_w += w

    blind_w = den - present_w
    if blind_w > 1e-12:                          # 生效但矩阵无列 → 整列中性填充
        num = num + neutral_ratio * 10.0 * blind_w
    total = num / den * 10.0
    return total, {
        "denominator": den,
        "present_weight": round(present_w, 10),
        "blind_weight": round(max(0.0, blind_w), 10),
        "neutral_fill_ratio": neutral_ratio,
        "scale": "percent_0_100",
    }


def scalar_total(parts_values, dims, weights, dim_max,
                 neutral_ratio=NEUTRAL_FILL_RATIO):
    """用生产标量函数算单股总分（供一致性自检与测试调用）。

    ⚠️ 生产调用方的 `parts` 含**全部生效维**，缺值以 None 传入；历史矩阵
       对 `score_fund` 这类真盲区是「整列不存在」。若此处只传矩阵列，
       标量版会少一项中性填充（实测 diff 恰为 10.0 分），自检必然失败。
       ⇒ 对 W 中生效但矩阵无列的维补 `(d, None, w)`，与
         `composite_total_matrix` 的 blind_w 处理等价。
    """
    parts = [(d, v, float(weights.get(d, 0.0))) for d, v in zip(dims, parts_values)]
    present = set(dims)
    for d, w in (weights or {}).items():
        if d not in present and is_num(float(w or 0)) and float(w) > 0:
            parts.append((d, None, float(w)))
    return composite_total(parts, dim_max or {}, sum_all=None,
                           weights=weights, neutral_ratio=neutral_ratio)[0]


def selfcheck(S, dims, weights, dim_max, sample=256, tol=5.1e-3):
    """真实数据一致性自检：逐股比较向量化版与生产标量版。

    这是 P0-1 验收标准「同一输入、同一时点，逐股逐维及总分一致」的可执行证据。
    ⚠️ tol 默认 5.1e-3 而非 0：生产 `scale_calc.composite_total` 返回
       `round(total, 2)`（百分制保留 2 位），向量版为保持秩精度不做 round。
       故逐股最大理论差 ≤ 0.005； tol 取 0.005 + 浮点余量 1e-4。
       若差值超过 tol，说明是真实口径分歧，不是舍入噪声。
    返回 dict（passed=True 表示两实现在同一输入下逐股一致）。
    """
    S = np.asarray(S, dtype=np.float64)
    flat = S.reshape(-1, S.shape[-1])
    vec, meta = composite_total_matrix(flat, dims, weights, dim_max)
    idx = np.linspace(0, len(flat) - 1, min(sample, len(flat)), dtype=int)
    worst, worst_i = 0.0, -1
    for i in idx:
        row = flat[i]
        vals = [None if np.isnan(v) else float(v) for v in row]
        ref = scalar_total(vals, dims, weights, dim_max)
        diff = abs(float(vec[i]) - ref)
        if diff > worst:
            worst, worst_i = diff, int(i)
    return {
        "n_rows": int(len(flat)), "n_checked": int(len(idx)),
        "max_abs_diff": worst, "worst_row": worst_i,
        "passed": bool(worst <= tol), "tol": tol,
        "kernel_id": kernel_fingerprint()["kernel_id"],
        **meta,
    }


def classify_artifact(meta, base_dir=None, strict=True):
    """判定产物能否作为**当前生产模型**的证据。

    status:
      COMPARABLE  —— 内核一致且因子逐维复现（当前长面板尚未达到）
      PROXY_ONLY  —— 内核一致但因子是历史代理，只能做代理面板诊断
      LEGACY      —— 内核指纹不符（旧量尺/旧权重/旧代码）
      UNKNOWN     —— 产物未绑定指纹（旧代际产物）

    任何非 COMPARABLE 一律 `comparable=False`；`strict=True` 时对 UNKNOWN/LEGACY
    附带「禁止用于证明生产模型」的明示文案。
    """
    cur = kernel_fingerprint(base_dir)
    meta = meta or {}
    kid = meta.get("kernel_id")
    parity = bool(meta.get("factor_parity"))
    if not kid:
        st, why = "UNKNOWN", "产物未绑定 kernel_id（旧代际产物）"
    elif kid != cur["kernel_id"]:
        st, why = "LEGACY", (f"产物 kernel_id={kid} ≠ 当前生产 {cur['kernel_id']}"
                            f"（权重/量程/源码已变）")
    elif not parity:
        st, why = "PROXY_ONLY", "因子为历史代理实现，未逐维复现生产算法"
    else:
        st, why = "COMPARABLE", "内核一致且因子逐维复现"
    out = {
        "status": st, "comparable": st == "COMPARABLE",
        "kernel_id": kid, "current_kernel_id": cur["kernel_id"],
        "factor_parity": parity, "reason": why,
        "verdict": ("可作为生产模型证据" if st == "COMPARABLE" else
                    "禁止用于证明当前生产模型有效性"),
        "proxy_dims": PANEL_PROXY_DIMS if st in ("PROXY_ONLY", "UNKNOWN", "LEGACY") else {},
    }
    if strict and st != "COMPARABLE":
        out["disclosure"] = (f"⚠️ 该产物为 {st}：{why}。{out['verdict']}；"
                             f"仅可用于历史诊断，不得写作「经验证的 alpha」。")
    return out


def artifact_meta(kernel_mode="production", factor_parity=False, extra=None):
    """生成写入产物的内核元数据块（供 build_panel_long / recompute_ic_long_v2 使用）。"""
    k = kernel_fingerprint()
    out = {
        "kernel_id": k["kernel_id"],
        "kernel_mode": kernel_mode,
        "model_version": k["version"],
        "target_horizon": k["target_horizon"],
        "factor_parity": bool(factor_parity),
        "sum_active_weights": k["sum_active_weights"],
        "neutral_fill_ratio": k["neutral_fill_ratio"],
        "code_sha256": k["code_sha256"],
        "proxy_dims": dict(PANEL_PROXY_DIMS) if not factor_parity else {},
    }
    if extra:
        out.update(extra)
    return out


if __name__ == "__main__":
    import sys
    k = kernel_fingerprint()
    print(f"kernel_id = {k['kernel_id']}  model={k['version']}  "
          f"h={k['target_horizon']}  Σw={k['sum_active_weights']}")
    for n, h in k["code_sha256"].items():
        print(f"  {n:32s} {h[:16]}…")
    if len(sys.argv) > 1 and sys.argv[1] == "--check-artifact":
        with open(sys.argv[2], encoding="utf-8") as f:
            d = json.load(f)
        print(json.dumps(classify_artifact(d.get("kernel") or d),
                         ensure_ascii=False, indent=2))
