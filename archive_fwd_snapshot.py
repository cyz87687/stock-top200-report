#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""archive_fwd_snapshot.py —— 恢复 data/fwd_hist/ 的每日写入（P0 断流修复）

════════════════════════════════════════════════════════════════════════
为什么需要这个脚本
════════════════════════════════════════════════════════════════════════
实测（2026-10-29）：全项目 16 个脚本引用 data/fwd_hist，**无一写入**。
该目录是当年从 git 历史恢复的静态快照，最后一个文件停在 2026-10-23。
=> fund 维（权重 0.242，全模型最大单一权重）自 10-24 起已完全停更。

更关键：fwd_hist 存的是 **fwd_pe（机构前瞻PE）与 pe_ladder（三年预测PE阶梯）**，
这两项是「当日快照，过期即失」——任何数据源都不提供历史回溯。
一旦某天没归档，那天的前瞻数据就**永久丢失**，且无法用任何代理还原
（实测静态PE 代理 ρ 仅 0.536 < 0.70，见 probe_fund_calib.py）。

=> 断流不是「少了一天的数据」，是「永久损失一天的前瞻信息」。
   每断一天，未来可用于验证 fund 的样本就少一天，且不可弥补。

════════════════════════════════════════════════════════════════════════
本脚本做什么
════════════════════════════════════════════════════════════════════════
从当日 scored json 中抽取前瞻字段，写成 fwd_hist 兼容格式（schema 与既有
86 个文件一致，便于 build_panel_round34 / audit_round24 等既有读取方直接复用）。

幂等：若目标日期文件已存在，默认跳过（防重复写入改写历史）；
      --force 才覆盖。

⚠️ 与 archive_fund_universe.py 的分工（两者不重复，务必都跑）：
   · archive_fwd_snapshot  → 只存「前瞻快照」（fwd_pe/pe_ladder），域 = 当日 scored 池
     （前瞻数据本身只覆盖有分析师跟踪的股票，扩域无意义）
   · archive_fund_universe → 存「全域 PIT 入参」（pe/growth/roe/gm/cagr3 + 前瞻快照的拷贝）
     域 = EM财务 ∩ 申万 ∩ 有价格 ≈ 2000 只
   两者合起来才构成完整的 fund 数据底座。

用法
  python archive_fwd_snapshot.py                              # 归档今天（用最新 scored）
  python archive_fwd_snapshot.py --scored top200_scored_2026-10-28.json
  python archive_fwd_snapshot.py --date 2026-10-28 --force     # 覆盖重写
"""
import os
import sys
import json
import glob
import argparse
import datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

FWD_DIR = os.path.join(HERE, "data", "fwd_hist")


def is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x


def pick_scored(explicit):
    if explicit:
        return explicit
    cands = [f for f in glob.glob("top200_scored_*.json") if "_watch_" not in f]
    if not cands:
        raise SystemExit("❌ 找不到 top200_scored_*.json")
    return max(cands)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scored", default="")
    ap.add_argument("--date", default="")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    scored = pick_scored(args.scored)
    print("=" * 96)
    print(f"archive_fwd_snapshot · 源 {scored}")
    print("=" * 96)

    d = json.load(open(scored, encoding="utf-8"))
    results = d.get("results") or []
    if not results:
        raise SystemExit(f"❌ {scored} 无 results")

    # 日期：优先参数，其次文件名，最后文件内字段
    date = args.date
    if not date:
        base = os.path.basename(scored)
        if base.startswith("top200_scored_") and len(base) >= 24:
            date = base[len("top200_scored_"):len("top200_scored_") + 10]
        elif isinstance(d.get("date"), str):
            date = d["date"][:10]
        else:
            # ⚠️ 2026-10-29 P0（时区 bug）：原为 `dt.datetime.now()+timedelta(hours=8)`，
            #   假设宿主是 UTC；本机已是 CST+0800 → 16:00 后滚到明天（同
            #   archive_fund_universe 的事故）。改用 UTC 基准转换，与宿主无关。
            date = dt.datetime.now(dt.timezone.utc).astimezone(
                dt.timezone(dt.timedelta(hours=8))).date().isoformat()

    out_path = os.path.join(FWD_DIR, f"{date}.json")
    print(f"目标日期 {date} → {out_path}")

    if os.path.exists(out_path) and not args.force:
        print(f"⚠️ 已存在且未加 --force → 跳过（幂等保护，防止改写历史）")
        print("=" * 96)
        return

    rows = []
    n_fwd = n_ladder = 0
    for r in results:
        fwd_pe = r.get("fwd_pe")
        ladder = r.get("pe_ladder")
        rec = {
            "code": r.get("code"),
            "name": r.get("name"),
            "price": r.get("price"),
            "fwd_pe": fwd_pe if (is_num(fwd_pe) and fwd_pe > 0) else None,
            "pe_ladder": ladder if isinstance(ladder, dict) and ladder else None,
            "growth": r.get("growth"),
            "growth_raw": r.get("growth_raw"),
            "growth_period": r.get("growth_period"),
            "pe": r.get("pe"),
            "circ_mv": r.get("circ_mv"),
            "score_fund": r.get("score_fund"),
            "score_quality": r.get("score_quality"),
            "score_growth": r.get("score_growth"),
            "score_size": r.get("score_size"),
            "score_tech": r.get("score_tech"),
            "score_bias60": r.get("score_bias60"),
            "score_rsi_rev": r.get("score_rsi_rev"),
            "total": r.get("total"),
        }
        if rec["fwd_pe"] is not None:
            n_fwd += 1
        if rec["pe_ladder"] is not None:
            n_ladder += 1
        rows.append(rec)

    payload = {
        "date": date,
        "source": os.path.basename(scored),
        "generated_at": dt.datetime.now(dt.timezone.utc).astimezone(
            dt.timezone(dt.timedelta(hours=8))).isoformat(timespec="seconds"),
        "n_rows": len(rows),
        "n_fwd_pe": n_fwd,
        "n_pe_ladder": n_ladder,
        "writer": "archive_fwd_snapshot.py",
        "caveat": ("本文件为当日前瞻快照（fwd_pe/pe_ladder 过期即失，不可回溯）。"
                   "域 = 当日 scored 池。历史 86 个文件由 git 恢复，其中 2026-10-24 起曾断流，"
                   "2026-10-29 由本脚本恢复每日写入。"),
        "rows": rows,
    }

    print(f"  行数 {len(rows)}  fwd_pe 有值 {n_fwd}  pe_ladder 有值 {n_ladder}")
    if n_fwd == 0:
        print("  ⚠️ fwd_pe 全空 —— 上游未产出前瞻数据，本文件对 fund 维无增量价值")
    if args.dry_run:
        print("--dry-run：不写文件")
        return

    os.makedirs(FWD_DIR, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    print(f"✅ 写出 {out_path} ({os.path.getsize(out_path)/1e6:.3f} MB)")
    print("=" * 96)


if __name__ == "__main__":
    main()
