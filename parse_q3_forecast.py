#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
parse_q3_forecast.py —— 三季度预测汇总表结构化解析（v3.5.1 Q3 加分项数据源）

用途：把手工汇总的《半导体消费电子三季度预测汇总.xlsx》解析为程序可消费的
      data/q3_forecast_<date>.json，供 step2_stockscorer_v2.py 的 Q3 景气加分使用。

数据纪律：
  - 所有数值来自 Excel 原值，不编造；无法解析的单元格留 None + kind 留档；
  - 「扭亏为盈/同比扭亏/转正」等文字**不赋数值**，仅标 kind='turnaround'；
  - 区间（如 +93%~+113%）取中值；区间颠倒（如 +42%~+24%）取中值但标 kind='suspect'；
  - A/B 机构口径分别留档（a_* / b_*），不做静默统一。

用法：
  python3 parse_q3_forecast.py [xlsx路径] [输出json路径]
  默认输入：~/Desktop/workbuddy项目/半导体消费电子三季度预测汇总.xlsx
  默认输出：data/q3_forecast_<今日>.json

⚠️ .gitignore 忽略 *.json → 入库需 git add -f
"""
import json
import os
import re
import sys
from datetime import datetime

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_XLSX = os.path.expanduser("~/Desktop/workbuddy项目/半导体消费电子三季度预测汇总.xlsx")

# 扭亏文字关键词（不赋数值，仅标记）
TURNAROUND_WORDS = ("扭亏", "转正", "盈亏")
# 数字提取：允许正负号/小数点/百分号，区间分隔符支持 ~ ～ — -
_NUM_RE = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*%?")


def parse_pct(v):
    """解析同比单元格 → (value|None, kind)

    kind ∈ point(点值) / range(区间取中值) / turnaround(扭亏文字) / suspect(区间颠倒) / none(空)
    无法识别的内容返回 (None, 'none')，原值由调用方留档。
    """
    if v is None:
        return None, "none"
    if isinstance(v, float) and pd.isna(v):
        return None, "none"
    s = str(v).strip()
    if not s or s.lower() == "nan":
        return None, "none"
    if any(w in s for w in TURNAROUND_WORDS):
        return None, "turnaround"
    nums = [float(x) for x in _NUM_RE.findall(s)]
    if not nums:
        return None, "none"
    if len(nums) >= 2:
        lo, hi = nums[0], nums[1]
        mid = round((lo + hi) / 2.0, 2)
        return mid, ("suspect" if lo > hi else "range")
    return nums[0], "point"


def _clean(v):
    """单元格原值清洗为可JSON化字符串（保留 '22(扣非)' 这类口径标记）"""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    return s if s and s.lower() != "nan" else None


def parse_excel(path):
    df = pd.read_excel(path, header=1)
    # 列位（header=1 后）：0行业 1个股 | 2-7 A机构 | 8-13 B机构
    cols = list(df.columns)
    if len(cols) < 14:
        raise SystemExit(f"❌ 列数异常({len(cols)}<14)，表结构可能已变化：{cols}")

    out = {}
    for _, r in df.iterrows():
        name = _clean(r.iloc[1])
        if not name:
            continue
        a_yoy, a_kind = parse_pct(r.iloc[6])
        b_yoy, b_kind = parse_pct(r.iloc[12])
        b_np_raw = _clean(r.iloc[11])
        rec = {
            "sector_raw": _clean(r.iloc[0]),
            "a_yoy": a_yoy, "a_kind": a_kind,
            "b_yoy": b_yoy, "b_kind": b_kind,
            "a_rev": _clean(r.iloc[2]), "b_rev": _clean(r.iloc[8]),
            "a_np": _clean(r.iloc[5]), "b_np": b_np_raw,
            # B机构净利润部分标 (扣非)，口径留档不静默统一
            "b_np_kf": bool(b_np_raw and "扣非" in b_np_raw),
            "a_qoq": _clean(r.iloc[7]), "b_qoq": _clean(r.iloc[13]),
        }
        # 合并 kind：任一方 suspect → suspect（加分减半）；否则任一方 turnaround → turnaround
        if "suspect" in (a_kind, b_kind):
            rec["flag"] = "suspect"
        elif "turnaround" in (a_kind, b_kind):
            rec["flag"] = "turnaround"
        elif a_yoy is not None or b_yoy is not None:
            rec["flag"] = "ok"
        else:
            rec["flag"] = "none"
        out[name] = rec
    return out


def main():
    xlsx = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_XLSX
    if not os.path.exists(xlsx):
        raise SystemExit(f"❌ 找不到输入文件：{xlsx}")
    out_path = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
        HERE, "data", f"q3_forecast_{datetime.now().strftime('%Y-%m-%d')}.json")

    data = parse_excel(xlsx)
    payload = {
        "parsed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_file": os.path.abspath(xlsx),
        "n": len(data),
        "note": "手工汇总表(非程序化接口)；a_/b_ 为A/B机构原始口径；turnaround不赋数值；suspect=区间颠倒",
        "data": data,
    }
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)

    # 摘要
    flags = {}
    for rec in data.values():
        flags[rec["flag"]] = flags.get(rec["flag"], 0) + 1
    print(f"✅ 解析 {len(data)} 只 → {out_path}")
    print(f"   flag 分布: {flags}")
    both = [n for n, r in data.items() if r["a_yoy"] is not None and r["b_yoy"] is not None]
    print(f"   A/B双方都有数值: {len(both)} 只")
    kf = [n for n, r in data.items() if r["b_np_kf"]]
    print(f"   B机构净利润标(扣非): {len(kf)} 只 {kf}")
    print("   ⚠️ 入库需: git add -f " + out_path)


if __name__ == "__main__":
    main()
