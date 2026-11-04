# -*- coding: utf-8 -*-
"""从现有 factor_ic.json 就地重建 cross_section_audit（幂等，不重跑计算）

用途：round117 P0-1 修复「审计只收录顶层 horizon、漏掉嵌套 horizons 的 4 个口径」
后，无需重跑 20 分钟的 factor_eval 全量计算，直接从已落盘的分块字段重建审计段。
产物与 factor_eval.py 内联逻辑完全一致（同一套键名）。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
IC = os.path.join(HERE, "factor_ic.json")
HORIZONS = (1, 5, 20)
MIN_CROSS_SECTION = 200

CFG = os.path.join(HERE, "config.json")
try:
    with open(CFG, encoding="utf-8") as f:
        _cfg = json.load(f)
    _gate = _cfg.get("min_cross_section")
    MIN_CROSS_SECTION = int(_gate) if _gate is not None else 200
except Exception:
    pass


def main():
    with open(IC, encoding="utf-8") as f:
        d = json.load(f)

    audit = {}
    for label in ("current_weights_composite_ic", "tradable_ic", "tradable_w_ic",
                  "tradable_fund_ic", "recommendation_ic"):
        blk = d.get(label)
        if not isinstance(blk, dict):
            continue
        # 兼容两种结构
        hs = blk.get("horizons") if isinstance(blk.get("horizons"), dict) else blk
        for h in HORIZONS:
            a = hs.get(str(h))
            if not isinstance(a, dict):
                continue
            g = a.get("gated") or {}
            audit[f"{label}|h{h}"] = {
                "n": a.get("n"),
                "cross_section_median": a.get("cross_section_median"),
                "cross_section_min": a.get("cross_section_min"),
                "cross_section_max": a.get("cross_section_max"),
                "n_below_gate": a.get("cross_section_lt_gate"),
                "n_after_gate": g.get("n_after_gate"),
                "ic_mean_after_gate": g.get("ic_mean_after_gate"),
                "nw_t_after_gate": g.get("nw_t_after_gate"),
                "usable": g.get("usable"),
                "gated_note": g.get("usable_note"),
            }

    # 生产池
    pool_cfg, pool_note = None, None
    try:
        with open(CFG, encoding="utf-8") as f:
            c = json.load(f)
        pool_cfg = c.get("pool_size")
        pool_note = c.get("pool_size_note")
    except Exception:
        pass
    actual, src = None, None
    try:
        import glob
        fs = sorted(glob.glob(os.path.join(HERE, "top200_scored_*.json")),
                    key=os.path.getmtime)
        if fs:
            with open(fs[-1], encoding="utf-8") as f:
                sd = json.load(f)
            actual = len(sd.get("results") or []) or sd.get("pool_size") or None
            src = os.path.basename(fs[-1])
    except Exception:
        pass

    meds = [v["cross_section_median"] for v in audit.values()
            if isinstance(v.get("cross_section_median"), (int, float))]
    audit["_summary"] = {
        "min_cross_section_gate": MIN_CROSS_SECTION,
        "pool_size_config": pool_cfg,
        "pool_size_config_note": pool_note,
        "production_pool_size": actual,
        "production_pool_source": src,
        "median_of_medians": (sorted(meds)[len(meds) // 2] if meds else None),
        "note": ("每截面股票数远小于生产池 → IC 结论来自更小的域，不可直接外推到当前池。"
                 "`usable=False` 的 horizon 在门槛下不可复现，不得作为权重依据。"),
    }

    d["cross_section_audit"] = audit
    with open(IC, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)

    n_rows = len([k for k in audit if k != "_summary"])
    print(f"✅ 已重建 cross_section_audit：{n_rows} 行（5 口径 × 3 周期中实际有的）")
    print(f"   门槛 {MIN_CROSS_SECTION} 只 | 生产池 {actual} 只 ({src})")
    n_usable = sum(1 for k, v in audit.items() if k != "_summary" and v.get("usable") is True)
    n_unusable = sum(1 for k, v in audit.items() if k != "_summary" and v.get("usable") is False)
    print(f"   可引用 {n_usable} 行 | 不可复现 {n_unusable} 行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
