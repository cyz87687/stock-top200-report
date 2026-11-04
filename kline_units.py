#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kline_units.py —— klines_long.json 成交量/成交额的**唯一**口径来源

## 为什么必须有这个模块

`data/klines_long.json` 的 bar 是 `[open, close, high, low, volume]`，
其中 `volume` **存在双口径**（用东财 qfq 逐只交叉验证，非推测）：

| 板块 | 前缀 | volume 单位 | 转「股」系数 |
|---|---|---|---|
| 科创板 | `688` | **股** | ×1 |
| 其他 | `00/30/60/…` | **手** | ×100 |

## 事故史（勿重演）

**事故 A（单日污染，已修）**：`2026-11-02` 一天，非科创板 1,825 只被写成「股」
（库vol/东财vol 恒等 100.0000），已于 2026-11-03 用
`verify_vol_unit_em.py` + `fix_klines_vol_unit.py` 修复并东财全库复核通过。

**事故 B（跨全历史口径缺陷，本模块要解决的）**：11 个 audit 脚本都写
`A[i,j] = v_ * c_` 作为成交额，**漏乘 100**。后果：
- 门槛 `--min-amt 3e7` 实际等价于「**成交额 ≥ 30 亿元**」而非 0.3 亿元
- 主板日均通过数 61.5 / 1197（**5.1%**）而非 1045.6（**87.4%**）
- 相当于所有 T0/T+1 结论都只在「大市值成交额 30 亿+」子集上成立
- 实测影响：114f 低开反转 n 从 188 → 4056，均值 +3.54% → +1.57%，
  胜率 66.5% → 58.9%（剔除涨停后 +1.26%/58.0%）

## 精度边界（重要，不要过度宣称）

`bar_amount = vol(股) × close` 是**近似**，不是真实成交额（真实值是逐笔成交价加权）。
实测 000001 于 2026-11-02：本函数算 18.3989 亿 vs 东财实际 18.5003 亿 → **偏差 0.55%**。
用于「流动性门槛」（差一个数量级才影响）完全够用；但**不可用于精确成交额/换手率统计**。
若要精确值，应直接取东财 `成交额` 字段。

## 用法

    from kline_units import vol_to_shares, bar_amount

    # 方式一：逐 bar 转换
    amt = bar_amount(code, vol, close)        # ← 推荐，自动处理双口径

    # 方式二：需要「股」单位的量（换手率、日均量等）
    sh = vol_to_shares(code, vol)

    # 方式三：批量（numpy，回测脚本用）
    coef = vol_coef_array(codes)              # 返回 shape=(N,) 的系数向量
    A = v_mat * coef[None, :] * c_mat
"""
import numpy as np


def vol_coef(code):
    """volume → 股 的乘数。688 已是股（×1），其余是手（×100）"""
    return 1.0 if str(code).startswith("688") else 100.0


def vol_to_shares(code, vol):
    """把库内 volume 统一成「股」"""
    return float(vol) * vol_coef(code)


def bar_amount(code, vol, close):
    """成交额（元）。库内 vol 单位不统一，本函数是唯一正确入口。

    ⚠️ 禁止再写 `vol * close` —— 那对非 688 会少算 100 倍。
    """
    return vol_to_shares(code, vol) * float(close)


def vol_coef_array(codes):
    """批量版：返回 shape=(N,) 的系数向量，供 numpy 回测矩阵用

        coef = vol_coef_array(codes)
        A = V * coef[None, :] * C
    """
    return np.array([vol_coef(c) for c in codes], dtype=np.float64)


__all__ = ["vol_coef", "vol_to_shares", "bar_amount", "vol_coef_array"]
