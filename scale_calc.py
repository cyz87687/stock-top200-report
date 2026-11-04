#!/usr/bin/env python3
"""复合分「量尺」唯一实现来源（P0-4 修复，2026-10-27）。

问题
----
修复前四处复合分实现（step2 / enhance_scores / apply_sector_momentum /
factor_eval）都是：

    total = Σ min(v,DMAX)/DMAX×10×w  /  max(0.5, Σ「该股可用维」的 w)  × 10

分母随「这只股票缺了几维」变化 → **同一页面上不同股票用不同尺子**。
实测（top200_scored_2026-10-27.json，1895 只）Σ可用权重分布：

    1.0000 → 1356 只（放大 1.000x）
    0.9250 →  312 只（放大 1.081x）
    0.8500 →  191 只（放大 1.176x）
    0.7500 →    4 只（放大 1.333x）

即「数据缺失反而占便宜」：缺维越多，分数被抬得越高。最高虚高 16.33 分。

修法（B_neutral）
-----------------
① 分母恒定为 Σ「全部生效权重」（当前配置 = 1.0），不再随股票变化；
② 缺维在分子上填 **中性分 DMAX/2**（无信息中性值），而非 0 分。

为什么不填 0 分（A_zero）
    实测 A_zero 的复合 IC 从 h5 +0.1520 → +0.0926（配对 t −5.76）、
    h20 +0.1879 → +0.1071（t −8.94）→ 显著恶化。填 0 等于把「没数据」
    当成「最差」，凭空扣分制造噪声。

为什么 DMAX/2 是无偏的
    实测当日池内中位数 vs DMAX/2：
      score_quality 5.000/5.00、score_lowvol20 5.000/5.00、
      score_ep 5.000/5.00、score_size_ind 5.000/5.00  → 偏差 0.000
      score_fund 2.200/2.50 → 偏差 −0.300（量程 5 的 6%）
    池内 rank 分位族天然中位 = 5.0，所以 DMAX/2 对它们是**精确中位数**，
    对唯一非分位族 fund 只有 6% 量程的轻微偏低，方向保守（不抬高）。

与 P0-2 的语义调和（重要，勿混）
    P0-2 反对的是「用增速**伪造**一个看起来像实测的估值分」——那是把 A 维的
    证据冒充 B 维，属数据造假。本模块填的是**声明为缺失的中性值**，并在
    `missing` 里逐维上报，由调用方写进 reasons / 页面标记。
    「不计分」与「按中性值参与但明确标注」是两种合规处置，后者不伪造证据。

口径变更纪律
------------
量尺变更后 total 与本模块上线前**不可比**（同 v4.4 标度变更、v4.7 量程变更）。
跨版本比较必须用同一把尺重算，禁直接引历史 total。
"""

import json
import os

# 缺维中性填充比例：DMAX × 0.5 = 量程中点
NEUTRAL_FILL_RATIO = 0.5


def is_num(x):
    """True 仅当 x 是有限实数（排除 bool / None / NaN / 字符串）。"""
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x


def sum_all_weights(weights):
    """Σ 全部生效权重（只计 w>0 的维）。

    这就是统一量尺的分母。权重配置变更后自动跟随，禁硬编码 1.0 ——
    历史上多处硬编码导致「配置改了、分母没改」的静默失配。
    """
    return sum(float(w) for w in (weights or {}).values() if is_num(w) and w > 0)


def composite_total(parts, dim_max, sum_all=None, weights=None,
                    neutral_ratio=NEUTRAL_FILL_RATIO, missing_out=None):
    """按统一量尺算百分制复合分。

    参数
    ----
    parts : list[(key, value_or_None, weight)]
        待汇总维度。value 为 None/非数 → 视为缺维。weight<=0 的项被忽略
        （权重 0 的维不应影响总分，但调用方可以照传，便于留档）。
    dim_max : dict[key -> 满分量程]
    sum_all : float | None
        统一分母。None 时从 weights 现算；两者都缺则从 parts 内 w>0 求和
        （此时退化为「仅统计传入的维」，仅用于无法拿到全量权重表的场景）。
    weights : dict | None
        全量权重表（推荐传）。用于在 parts 只含部分维时仍取到正确的总分母。
    neutral_ratio : float
        缺维填充比例，默认 0.5（量程中点）。
    missing_out : list | None
        传入则把缺失维 key 追加进去，供调用方写 reasons / 页面标记。

    返回
    ----
    (total: float, missing: list[str])
        total 为百分制（0-100），保留 2 位小数。
    """
    missing = []
    if sum_all is None:
        if weights:
            sum_all = sum_all_weights(weights)
        else:
            sum_all = sum(float(w) for _, _, w in parts if is_num(w) and w > 0)
    if not is_num(sum_all) or sum_all <= 0:
        return 0.0, missing

    num = 0.0
    for key, val, w in parts:
        if not is_num(w) or w <= 0:
            continue                      # 权重 0 / 非法权重 → 不参与
        mx = float(dim_max.get(key) or 10.0)
        if mx <= 0:
            mx = 10.0
        if is_num(val):
            num += min(float(val), mx) / mx * 10.0 * w
        else:
            num += neutral_ratio * 10.0 * w        # 中性填充：DMAX/2 → 归一后恒 5.0
            missing.append(key)
            if missing_out is not None:
                missing_out.append(key)
    # 归一化后的量纲是 0-10，加权平均仍是 0-10 → ×10 得百分制（勿漏乘）
    return round(num / sum_all * 10.0, 2), missing


def missing_note(missing, dim_max=None, labels=None):
    """把缺失维列表转成给人看的一句话（写进 reasons / 页面披露条）。"""
    if not missing:
        return ""
    labels = labels or {}
    names = [labels.get(k, k.replace("score_", "")) for k in missing]
    return "中性填充(非实测)：" + "/".join(names)


def load_weights_and_dim_max(base_dir=None):
    """读 factor_weights.json → (weights, dim_max, meta)。失败回退空表。

    仅供本模块的独立自检/一次性脚本使用；生产线四个入口各自已有带兜底的
    加载函数（step2.load_dim_max / enhance._load_dim_max 等），不要用本函数
    替换它们，否则会丢掉各自的回退默认值。
    """
    p = os.path.join(base_dir or os.path.dirname(os.path.abspath(__file__)),
                     "factor_weights.json")
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return {}, {}, {}
    w = {k: float(v or 0) for k, v in (d.get("weights") or {}).items()}
    dm = {k: float(v) for k, v in (d.get("dim_max") or {}).items() if is_num(v) and v > 0}
    return w, dm, d


if __name__ == "__main__":
    w, dm, meta = load_weights_and_dim_max()
    sa = sum_all_weights(w)
    print(f"factor_weights version = {meta.get('version')}")
    print(f"生效维（w>0）: {sum(1 for v in w.values() if v > 0)} 个, Σw = {sa:.6f}")
    for k, v in w.items():
        if v > 0:
            print(f"  {k:22s} w={v:.4f}  DMAX={dm.get(k):.1f}  中性填充={dm.get(k,10)/2:.1f}")
    # 自检：全维齐备时应恰好等于加权均值 ×10
    parts = [(k, dm.get(k, 10.0), v) for k, v in w.items() if v > 0]
    t_all, m_all = composite_total(parts, dm, sa)
    print(f"\n自检1 全维满分(每维=DMAX) → total={t_all} (应=100.0), 缺维={m_all}")
    parts0 = [(k, 0.0, v) for k, v in w.items() if v > 0]
    t_zero, _ = composite_total(parts0, dm, sa)
    print(f"自检2 全维零分            → total={t_zero} (应=0.0)")
    parts_none = [(k, None, v) for k, v in w.items() if v > 0]
    t_neu, m_neu = composite_total(parts_none, dm, sa)
    print(f"自检3 全维缺失(中性填充)  → total={t_neu} (应=50.0), 缺维{len(m_neu)}个")
    # 自检4：缺一维 vs 齐备，量尺必须一致（同维同值下分数差异只来自该维本身）
    parts_a = [(k, dm.get(k, 10.0) / 2, v) for k, v in w.items() if v > 0]
    t_a, _ = composite_total(parts_a, dm, sa)
    print(f"自检4 全维填中位值        → total={t_a} (应=50.0，验证量尺无偏)")
