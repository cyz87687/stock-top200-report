#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_round142.py —— round142 改动的一键只读验收（不写任何数据文件）。

设计原则：
  1. **活体复算**，不只读文件自证：秩相关与对齐清单都在进程内重算一遍，
     再与磁盘产物比对，防止「产物是旧的 / 被手工改过」蒙混过关。
  2. **双向验证**：既验「改了什么」，也验「绝对没动什么」
     （权重、内核指纹、CORE_FILES、git 工作区、主页、round141 留痕）。
  3. 全部只读；失败只报 FAIL，不自动修复、不重生成任何产物。

⚠️ 两个基线常量是 2026-11-12 round142 提交时的实测值。若将来**合法**变更了
   权重或 CORE_FILES，这两项会 FAIL —— 那是提醒「基线已变，需复核后更新常量」，
   不是本轮改动出错。

用法：
    python verify_round142.py            # 全部检查
    python verify_round142.py --quiet    # 只输出汇总与 FAIL 项
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import build_panel_long as BPL              # noqa: E402
import factor_alignment as FA               # noqa: E402
import proxy_vs_prod_rank as PR             # noqa: E402
import scoring_core as SC                   # noqa: E402

# 2026-11-12 round142 提交时实测基线
BASE_WEIGHTS_SHA = "9347d48ff91c41accfc6e23825ed8ec235027cba79b23a6b300f4b367a37ebc9"
BASE_KERNEL_ID = "1b5770b7152d14a6"

RANK = os.path.join(HERE, "data", "proxy_vs_prod_rank_20261112_round142.json")
RANK_OLD = os.path.join(HERE, "data", "proxy_vs_prod_rank_20261112.json")
PLAN = os.path.join(HERE, "data", "factor_alignment_20261112.json")
WIND_REPORT = os.path.join(HERE, "reports", "round142_wind_pit_probe_2026-11-12.md")

RESULTS = []


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def _txt(rel):
    with open(os.path.join(HERE, rel), encoding="utf-8") as f:
        return f.read()


def _git(*args):
    try:
        return subprocess.run(["git", "-C", HERE, *args],
                              capture_output=True, text=True, timeout=15).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))


def run():
    # ── A. 绝对没动什么（防「悄悄改模型」）──
    wsha = _sha(os.path.join(HERE, "factor_weights.json"))
    check("权重 factor_weights.json 未变",
          wsha == BASE_WEIGHTS_SHA, wsha[:16] + "…")

    kid = SC.kernel_fingerprint()["kernel_id"]
    check("内核 kernel_id 未变", kid == BASE_KERNEL_ID, kid)

    changed = set(_git("diff", "HEAD", "--name-only").splitlines())
    check("CORE_FILES 8 个文件均未改动",
          not (set(SC.CORE_FILES) & changed),
          "交集=" + (",".join(sorted(set(SC.CORE_FILES) & changed)) or "空"))

    dirty = _git("status", "--porcelain")
    check("git 工作区干净", dirty == "", dirty or "clean")

    head = _git("log", "-1", "--format=%h %s")
    check("HEAD 为 round142 提交", "round142" in head, head[:72])

    # ── B. 披露修正（含会流出的产物 caveat）──
    for rel in ("archive_fwd_snapshot.py", "archive_fund_universe.py"):
        t = _txt(rel)
        bad = [s for s in ("任何数据源都不提供历史回溯", "过期即失，不可回溯",
                           "历史不可回溯，缺失即留空") if s in t]
        good = all(s in t for s in ("口径更正", "mx_ashare_finance_data",
                                    "Wind get_financial_data", "本链路不自动回溯",
                                    "待验证补数候选"))
        check("披露更正 " + rel, not bad and good,
              "残留错误句=" + (",".join(bad) or "无"))

    t_fwd = _txt("archive_fwd_snapshot.py")
    check("静态 PE 代理边界仍保留", "0.536" in t_fwd and "不得" in t_fwd)

    # ── C. tech_neg 方向修正 ──
    t_panel = _txt("build_panel_long.py")
    check("tech_neg 源码方向已翻正",
          "rank10(techneg_raw, higher_better=False)" in t_panel)
    check("score_fund 定义错误已更正",
          "估值/基本面复合分" in t_panel and "事实错误" in t_panel)

    sample = {"a": 1.0, "b": 3.0, "c": 7.0, "d": 9.5, "e": 5.0}
    up = BPL.rank10(sample, True)
    dn = BPL.rank10(sample, False)
    sym = all(abs(dn[k] - (BPL.DIM_MAX_STD - up[k])) < 1e-9 for k in sample)
    check("rank10 对称性实测 rank10(v,False) ≡ 10 − rank10(v,True)",
          sym, "sample n=%d" % len(sample))

    # ── D. 活体复算：秩相关监控 ──
    live = PR.build_report()
    with open(RANK, encoding="utf-8") as f:
        disk = json.load(f)
    same = all(live["by_dim"][d].get("rho_mean") == disk["by_dim"][d].get("rho_mean")
               for d in live["by_dim"])
    check("秩相关产物与活体重算一致（非陈旧文件）", same,
          "%d 维 / %d 日期匹配" % (len(live["overlap"]["common_dims"]),
                                  live["overlap"]["n_date_matches"]))

    a = disk["by_dim"]["score_tech_neg"]["alignment"]
    check("方向预览：符号翻正但仍未达标（诚实边界）",
          a["rho_current_panel"] < 0 < a["rho_if_direction_aligned"]
          and a["rho_if_direction_aligned"] < PR.THRESHOLD
          and a["verdict_if_aligned"] == "RED_ALERT_NOT_EQUIVALENT"
          and not a["panel_npz_rebuilt"],
          "rho %.4f → %.4f（阈值 %.2f）" % (
              a["rho_current_panel"], a["rho_if_direction_aligned"], PR.THRESHOLD))

    with open(RANK_OLD, encoding="utf-8") as f:
        old = json.load(f)
    check("round141 历史产物未被覆盖",
          "alignment_preview" not in old
          and old["by_dim"]["score_tech_neg"]["rho_mean"] < 0)

    # ── E. 活体复算：对齐清单 ──
    with open(PLAN, encoding="utf-8") as f:
        plan_disk = json.load(f)
    plan_live = FA.build_plan()
    check("对齐清单产物与活体重算一致",
          plan_disk["summary"] == plan_live["summary"]
          and [x["status"] for x in plan_disk["alignment_order"]]
          == [x["status"] for x in plan_live["alignment_order"]],
          "%d 项" % plan_disk["summary"]["n_items"])

    by = {x["dim"]: x for x in plan_live["alignment_order"]}
    check("对齐顺序符合裁决",
          [x["dim"] for x in plan_live["alignment_order"][:4]]
          == ["score_tech_neg", "score_quality", "score_growth",
              "score_size_growth"])
    check("两种缺口已区分",
          by["score_fund"]["status"] == "BLIND_SPOT_NO_PANEL_DIM"
          and by["score_ep"]["status"] == "NO_PROD_COUNTERPART_FIELD")
    check("未宣称对齐 / 权重入口仍锁",
          not plan_live["summary"]["all_dims_aligned"]
          and not plan_live["summary"]["weight_change_allowed"])

    # ── F. Wind 实证留痕 ──
    ok_report = os.path.exists(WIND_REPORT)
    if ok_report:
        t = _txt("reports/round142_wind_pit_probe_2026-11-12.md")
        ok_report = all(s in t for s in ("328.8938", "301.957", "862.2815",
                                         "194.63", "46.3107", "QPS", "不能"))
    check("Wind 实测报告含正负证据原始数值", ok_report)

    # ── G. 主页未被本轮触碰（上一轮核查结论可复核）──
    idx = os.path.join(HERE, "index.html")
    r1111 = os.path.join(HERE, "top200_report_2026-11-11.html")
    if os.path.exists(idx) and os.path.exists(r1111):
        same_html = _sha(idx) == _sha(r1111)
        check("主页仍是 11-11 报告副本（本轮未发布）", same_html,
              _sha(idx)[:16] + "…")
    else:
        check("主页仍是 11-11 报告副本（本轮未发布）", False, "文件缺失")
    check("今日无 11-12 报告产物",
          not os.path.exists(os.path.join(HERE, "top200_report_2026-11-12.html"))
          and not os.path.exists(os.path.join(HERE, "analysis_2026-11-12.html")))

    # ── H. round142 单测（快，约 0.03s）──
    suite = unittest.defaultTestLoader.loadTestsFromName("test_round142_alignment")
    res = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
    check("round142 单测全通过", res.wasSuccessful(),
          "ran %d / failures %d / errors %d" % (
              res.testsRun, len(res.failures), len(res.errors)))


def main():
    quiet = "--quiet" in sys.argv
    run()
    n_pass = sum(1 for _, ok, _ in RESULTS if ok)
    n_fail = len(RESULTS) - n_pass
    for name, ok, detail in RESULTS:
        if quiet and ok:
            continue
        print("[%s] %-46s %s" % ("PASS" if ok else "FAIL", name, detail))
    print("\n汇总：%d/%d PASS，%d FAIL" % (n_pass, len(RESULTS), n_fail))
    if not n_fail:
        print("\n预期中的「看起来像失败」的三项，不要误判：")
        print("  · tech_neg 当前面板 rho 仍为负 → panel_r124_mat.npz 未重建，"
              "源码修正尚未回灌历史面板，属预期")
        print("  · 主页仍是 11-11 → 本轮是诊断/工具层，按 SHOW_CORE_ONLY 不进主页")
        print("  · all_dims_aligned=False / weight_change_allowed=False → 权重入口本就应锁")
        print("\n全量回归（约 57 秒）另跑：python -m unittest discover")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
