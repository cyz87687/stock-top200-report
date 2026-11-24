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


# ══════════════════════════════════════════════════════════════════════
# 2026-11-24 P0-6 §2 / P1-2：ES 覆盖不足时的有效权重（单一实现）
# ══════════════════════════════════════════════════════════════════════
ES_COVERAGE_MIN = 0.30   # 低于此覆盖率 → ES 维权重归零、按比例分给其余维


def effective_weights_es_gate(weights, es_coverage):
    """按 ES 实际覆盖率决定是否把 score_es 权重按比例让渡给其余生效维。

    背景：ES 全池覆盖仅 3.67%，按「缺维按 0 + 固定分母」等价于给 96.3% 标的
      隐式扣 0.5 分 → 高基本面公司被系统性压低（有 ES 组均分 77.17 vs 无 ES
      组 73.88）。覆盖率 <30% 时该维无判别力，权重归零并按原比例分配给其余维。

    ⚠️ 这是**运行时有效权重**，不改 factor_weights.json（那是名义权重主表，
       八处同步仍以名义表为准）。返回 (eff_weights: dict, adjusted: bool)。
       score_es 缺列/零权重时原样返回，不重复归一。
    """
    w = {k: float(v) for k, v in (weights or {}).items() if is_num(v) and v > 0}
    es_w = w.get("score_es", 0.0)
    if es_w <= 0 or not is_num(es_coverage) or es_coverage >= ES_COVERAGE_MIN:
        return w, False
    rest = {k: v for k, v in w.items() if k != "score_es"}
    rest_sum = sum(rest.values())
    if rest_sum <= 0:
        return w, False
    scale = 1.0 + es_w / rest_sum
    eff = {k: round(v * scale, 6) for k, v in rest.items()}
    return eff, True


def composite_total(parts, dim_max, sum_all=None, weights=None,
                    neutral_ratio=NEUTRAL_FILL_RATIO, missing_out=None,
                    missing_rule="neutral", missing_reason=None):
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
    missing_rule : str
        **v5.0（2026-11-23）新增 —— 缺维处理规则。** 三档：
          "neutral" (默认，旧口径)：缺维在分子填 `neutral_ratio*10`（=5.0 中性分）。
              ⚠️ 这是审计认定的「白送分」来源：全池 1275 分 ≈ 128 只满分红。
          "zero"：**固定分母**（恒 = Σ全部生效权重），缺维在分子按 **0 分**计
              ——「没有的数据不能算中性」。分母不随缺维变化 ⇒ 全池同一把尺
              （保住 P0-4 的可比性铁律），缺数据股被明确扣分而非白拿中性分。
          "structural"：结构性不适用的维（`missing_reason[dim] == "structural"`）
              从分子**与分母同时剔除**（不计分也不白送），其余缺维按 zero 处理。
              ⚠️ 仅当某维对**部分股票**结构性不适用时才有意义；若该维对全池都
              不适用，剔分母会整体放大分数（尺度漂移），此时应改用 zero。
        missing_reason : dict | None
        形如 {"score_size_growth": "structural"|"data"}，标注每个缺维的**原因**。
        ⚠️ 判据只能由**产生该维的模块**给出（唯一实现原则），本函数不自行推断。
        未提供时 "structural" 退化为与 "zero" 等价。

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
    den = sum_all          # 分母恒为 Σ全部生效权重（zero/structural 档不随缺维变化）
    for key, val, w in parts:
        if not is_num(w) or w <= 0:
            continue                      # 权重 0 / 非法权重 → 不参与
        mx = float(dim_max.get(key) or 10.0)
        if mx <= 0:
            mx = 10.0
        if is_num(val):
            # ⚠️ v5.0 修正：clip 必须**双向**。原实现只有上界 min(val, mx)，
            #   负值（如越界的因子分）会直接进分子 → 与向量版
            #   scoring_core.composite_total_matrix 的 np.clip(clean, 0, mx) 不一致，
            #   实测同一输入两把尺子差 4.64 分（2026-11-23 回归 T3 抓到）。
            #   下界 0 与「负分无意义」一致，且与向量版逐股等价。
            num += max(0.0, min(float(val), mx)) / mx * 10.0 * w
            continue
        # ---- 缺维 ----
        missing.append(key)
        if missing_out is not None:
            missing_out.append(key)
        if missing_rule == "neutral":
            num += neutral_ratio * 10.0 * w        # 中性填充：DMAX/2 → 归一后恒 5.0
        elif missing_rule == "structural":
            reason = (missing_reason or {}).get(key)
            if reason == "structural":
                # 结构性不适用 → 分子分母同时剔除（不计分、不白送）
                den -= float(w)
                continue
            num += 0.0 * w                          # 真缺数据 → 按 0
        else:  # "zero"
            num += 0.0 * w                          # 缺维按 0 分计入（分母不变）
    if den <= 0:
        den = sum_all
    # 归一化后的量纲是 0-10，加权平均仍是 0-10 → ×10 得百分制（勿漏乘）
    return round(num / den * 10.0, 2), missing


def missing_note(missing, dim_max=None, labels=None):
    """把缺失维列表转成给人看的一句话（写进 reasons / 页面披露条）。"""
    if not missing:
        return ""
    labels = labels or {}
    names = [labels.get(k, k.replace("score_", "")) for k in missing]
    # v5.0（2026-11-23）：缺维改为按 0 分计入（结构性不适用者已剔分母），
    #   旧文案「中性填充(非实测)」已与实际口径不符 → 改为「缺维按0(非实测)」。
    return "缺维按0(非实测)：" + "/".join(names)


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
