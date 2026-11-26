#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
earnings_surprise.py —— ES（业绩预期差）维度退役存根
=====================================================

2026-11-26 老大指令：删除《已退役手工预测源》及全部派生数据与引用。
该手工表随三季报披露已过期，ES 的预测侧数据源随之失效。因此：

  * 不再读取 旧预测数据_*.json，不再计算 ES 三档判定；
  * score_es 保持 0 权重，pending_dims 仅做研究项留档；
  * scored / 报告中不再写 es、score_es、es_meta、missing_reason.score_es；
  * 三季度事实分析改用 Wind / 东方财富 MCP 的 2026Q3 已披露数据，
    详见 docs/三季度数据源替换说明_2026-11-26.md。

本文件保留模块名与少量兼容接口，纯粹为避免历史 import 报错；
所有判定函数一律返回「无数据」，禁止被用作新分析的依据。

CLI:
  python3 earnings_surprise.py --strip scored.json   # 清除存量 scored 中的 ES 字段
"""
import json
import os
import sys

# 兼容旧 import（仅展示阈值常量，不参与任何判定）
POINT_BAND_PP = 10.0
STRONG_PP = 25.0


def tier_to_score(tier):
    """兼容接口：ES 已退役，恒返回 None（缺维，不是 0 分）。"""
    return None


def compute_es_map(name2code=None):
    """兼容接口：返回空映射，禁止从任何旧预测表装配数据。"""
    return {}


def judge_growth_surprise(fwd=None, act=None):
    """兼容接口：恒返回「无法判定」，禁止作为当前结论依据。"""
    return {
        "tier": "无法判定", "method": "source_retired", "missing_reason": "data",
        "note": "ES 手工预测源已于 2026-11-26 退役，改用 Wind/东财 2026Q3 实际披露数据",
    }


def strip_es_fields(scored_path):
    """清除 scored JSON 中全部 ES 派生字段；不改 total/rating。返回清除计数。"""
    with open(scored_path, encoding="utf-8") as f:
        data = json.load(f)
    results = data.get("results") or []
    n_es = n_score = n_mr = 0
    for r in results:
        if "es" in r:
            r.pop("es", None)
            n_es += 1
        if "score_es" in r:
            r.pop("score_es", None)
            n_score += 1
        if "es_coverage" in r:
            r.pop("es_coverage", None)
        mr = r.get("missing_reason")
        if isinstance(mr, dict) and "score_es" in mr:
            mr.pop("score_es", None)
            n_mr += 1
            if not mr:
                r.pop("missing_reason", None)
    if "es_meta" in data:
        data.pop("es_meta", None)
    if isinstance(data.get("stats"), dict):
        data["stats"].pop("score_es", None)
    with open(scored_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return {"es": n_es, "score_es": n_score, "missing_reason": n_mr}


def main():
    args = sys.argv[1:]
    if args and args[0] in ("--strip", "--attach", "--detach"):
        if len(args) < 2:
            print("用法: python3 earnings_surprise.py --strip scored.json")
            return 1
        if not os.path.exists(args[1]):
            print(f"文件不存在: {args[1]}")
            return 1
        print("ES 源已退役：清除字段", strip_es_fields(args[1]))
        return 0
    print("ES 源已退役：无判定输出。三季度分析请使用 Wind/东财 MCP 的 2026Q3 实际披露数据。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
