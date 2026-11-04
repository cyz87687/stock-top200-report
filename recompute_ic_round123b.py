#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""round123b：在修正口径后的面板上重算三周期 IC，并写回 factor_ic.json 审计段。

⚠️ 本脚本是 round123 那版 IC 的**更正**。
上一版报告的 h=20 「IC=+0.0629 / NW t=2.08」建立在 bias60/rsi_rev 的
**分档口径**（只有 5 个离散值）上，而生产是连续口径 → 高估。
见 fix_panel_reversal_dims.py 的 impact_measured。

用法：python recompute_ic_round123b.py
"""
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PANEL = os.path.join(HERE, "data", "panel_round123.json")
KLINES = os.path.join(HERE, "data", "klines_long.json")
IC_PATH = os.path.join(HERE, "factor_ic.json")

# 现行 v4.17-cycle-rules（引用前实读 factor_weights.json）
W = {"score_fund": 0.20, "score_size_growth": 0.20, "score_tech_neg": 0.10,
     "score_quality": 0.10, "score_ep": 0.10, "score_dominant": 0.10,
     "score_growth": 0.10, "score_bias60": 0.05, "score_rsi_rev": 0.05}
# round125: dim_max 改为**单一来源**（原为硬编码副本）。
#   铁律：量程只能来自 factor_weights.json → dim_cfg.load_dim_max。
from dim_cfg import load_dim_max
DIM_MAX = load_dim_max(['score_bias60', 'score_rsi_rev'], strict=True)