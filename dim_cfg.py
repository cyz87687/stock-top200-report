"""dim_cfg.py —— `dim_max`（各维满分量程）的**单一来源**加载器。

## 为什么必须存在这个模块（round125 血案）

项目铁律：**dim_max 只能有一个来源** = `factor_weights.json → dim_max`。
但 round121~124 我在写临时分析脚本时，图省事在各脚本顶部硬编码：

    DIM_MAX = {"score_bias60": 5.0, "score_rsi_rev": 5.0, "score_lowvol20": 10.0, ...}

一口气散出 8 处副本，且**全部漏了 `score_fund: 5.0`**（因为 fund 在那些方案里权重=0，
写的时候"看不见"它）。这类漏项不会报错，只会在某天有人给 fund 非零权重时
**静默把 fund 分数按 10.0 量程归一 → 分数被系统性压半**。

实测确认漏项位置（round125）：
    recompute_ic_long_v2.py / test_reversal_merge.py / weight_scheme_test.py /
    weight_scheme_head.py / weight_scheme_curve.py / weight_p04_realloc.py /
    weight_p04_constrained.py / weight_p04_attribution.py

⇒ 本模块的唯一职责：**读配置，不猜默认值**。默认表只作配置不可读时的兜底，
  且 `test_dim_cfg_single_source.py` 断言兜底表与配置文件**逐项相等**（漏同步即失败）。

## 与 step2_stockscorer_v2.load_dim_max() 的关系

step2 里已有同名函数，但它的返回键被 `_W_PRIOR_10D` 限定（20 个历史维）。
本模块**不复制 step2 的权重表**，只暴露 `load_dim_max(keys)`：
  · keys=None → 返回配置里**全部** dim_max 项（大概 20 项）
  · keys=[...] → 只取指定键，缺失的用兜底表补

这样分析脚本可以只关心自己用到的几维，又不会漏项。
"""
import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
WEIGHTS_FILE = os.path.join(_HERE, "factor_weights.json")

# ⚠️ 兜底表 —— 必须与 factor_weights.json 的 dim_max **逐项相等**。
#   约束由 test_dim_cfg_single_source.py 断言，禁单方面修改。
#   量程不统一的来由见 step2_stockscorer_v2.load_dim_max 的 docstring：
#   池内分位族（bias60/rsi_rev/lowvol20/ep/size_*/quality/growth/tech_neg…）本就分 0-5 与 0-10 两档，
#   按「每维满分 10」直接加权会让理论天花板只到 67.5 → 评级体系失效。
_SAFETY_DEFAULT = {
    "score_fund": 5.0,
    "score_news": 5.0,
    "score_theme_neg": 10.0,
    "score_size": 10.0,
    "score_sector": 5.0,
    "score_quality": 10.0,
    "score_growth": 10.0,
    "score_tech_neg": 10.0,
    "score_oversold60": 5.0,
    "score_bias60": 5.0,
    "score_rsi_rev": 5.0,
    "score_vol_pulse": 5.0,
    "score_overbought": 5.0,
    "score_theme_intrinsic": 5.0,
    "score_attention": 5.0,
    "score_lowvol20": 10.0,
    "score_ep": 10.0,
    "score_size_ind": 10.0,
    "score_size_growth": 10.0,
    "score_dominant": 10.0,
}

_CACHE = None


def load_dim_max(keys=None, strict=False):
    """返回 {维度名: 满分量程}。

    keys:   None → 配置里的全部项；给 list → 只返回这些键（缺失则取兜底表）
    strict: True → 某键在配置与兜底表里都找不到时直接 KeyError，
            而不是静默按 10.0 处理（分析脚本建议开，生产链保持默认）

    ⚠️ 静默按 10.0 是危险的：`score_fund` 真实量程 5.0，
       当 10.0 用会让该维分数被压半，且不会报错。
    """
    global _CACHE
    if _CACHE is None:
        try:
            with open(WEIGHTS_FILE, encoding="utf-8") as f:
                _CACHE = (json.load(f).get("dim_max") or {})
        except Exception:
            _CACHE = {}
    cfg = _CACHE

    if keys is None:
        out = dict(_SAFETY_DEFAULT)
        for k, v in cfg.items():
            if isinstance(v, (int, float)) and v > 0:
                out[k] = float(v)
        return out

    out = {}
    for k in keys:
        v = cfg.get(k)
        if isinstance(v, (int, float)) and v > 0:
            out[k] = float(v)
        elif k in _SAFETY_DEFAULT:
            out[k] = _SAFETY_DEFAULT[k]
        elif strict:
            raise KeyError(
                f"dim_max 缺少维度 {k!r}：既不在 factor_weights.json 也不在兜底表。"
                f" 请先在配置里登记，禁在脚本里凭印象写一个数。")
        else:
            out[k] = 10.0
    return out


def dim_max_of(dim, default=None):
    """单维查询。找不到时若给了 default 用 default，否则 KeyError（禁止静默 10.0）。"""
    dm = load_dim_max([dim], strict=(default is None))
    return dm.get(dim, default)


if __name__ == "__main__":
    dm = load_dim_max()
    print(f"dim_max 配置来源: {WEIGHTS_FILE}")
    print(f"共 {len(dm)} 项：")
    for k in sorted(dm):
        tag = ""
        if k in _SAFETY_DEFAULT and dm[k] != _SAFETY_DEFAULT[k]:
            tag = f"   ⚠️ 与兜底表不一致（兜底 {_SAFETY_DEFAULT[k]}）"
        print(f"  {k:<28} {dm[k]:>5.1f}{tag}")
    missing = [k for k in _SAFETY_DEFAULT if k not in dm]
    if missing:
        print(f"\n⚠️ 兜底表有、配置缺失：{missing}")
    else:
        print("\n✅ 兜底表全部键都在配置里")
