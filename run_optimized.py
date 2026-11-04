#!/usr/bin/env python3
"""run_optimized.py —— 优化后的每日链路入口（整合三项提速改造）。

老大 2026-10-26 三项需求在此统一编排：

  ① 财报因子增量缓存（fin_factor_cache.py）
     财务因子只在财报披露日变化 → 缓存 (code, 报告期) → 因子值，
     只有发新财报才重算。EM 缓存加载 50.4s → 0.33s（pickle）。

  ② 异步并发 + polars 向量化
     抓取走 async_fetch（Semaphore + 令牌桶 + 指数退避 + 501黑名单），
     因子计算走 factor_vector（polars），不再 `for stock in all_stocks`。
     实测 K 线 490 只：28.5 分钟 → 24 秒（提速 71x）。

  ③ 降采样调度（schedule_guard.py）
     完整评分仅周五收盘后跑；其余交易日只刷行情。

## 用法
    python run_optimized.py               # 自动判定模式（推荐，供定时任务调用）
    python run_optimized.py --full        # 强制完整链路
    python run_optimized.py --quote-only  # 强制只跑行情
    python run_optimized.py --dry-run     # 只打印判定与预估耗时，不执行
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
CONFIG = os.path.join(HERE, "config.json")


def _cfg():
    try:
        return json.load(open(CONFIG, encoding="utf-8"))
    except Exception:
        return {}


def run_step(name, cmd, timeout=None):
    """跑一个子步骤，打印耗时与退出码。返回 (ok, secs, tail)。"""
    print("\n" + "─" * 70)
    print("▶ %s" % name)
    print("─" * 70)
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=HERE, capture_output=False,
                           timeout=timeout, text=True)
        el = time.time() - t0
        ok = (p.returncode == 0)
        print("─ %s: %s  %.1fs" % (name, "✅" if ok else "❌ exit=%d" % p.returncode, el))
        return ok, el, ""
    except subprocess.TimeoutExpired:
        el = time.time() - t0
        print("─ %s: ⏰ 超时 %.1fs" % (name, el))
        return False, el, "timeout"
    except Exception as e:
        el = time.time() - t0
        print("─ %s: ❌ %s" % (name, e))
        return False, el, str(e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--quote-only", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--date", default="")
    ap.add_argument("--skip-prescreen", action="store_true")
    a = ap.parse_args()

    if a.full:
        os.environ["SCORER_FORCE_FULL"] = "1"
    if a.quote_only:
        os.environ["SCORER_FORCE_QUOTE"] = "1"

    import schedule_guard as SG
    mode, reason = SG.plan_mode()
    date_str = a.date or datetime.now().strftime("%Y-%m-%d")

    print("=" * 70)
    print("  每日链路（优化版）  %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 70)
    print("调度模式：%s" % ("完整评分链路" if mode == "full" else "仅更新行情"))
    print("判定依据：%s" % reason)
    _p, _dt = SG.latest_scored()
    if _p:
        print("最近评分：%s" % os.path.basename(_p))

    c = _cfg()
    af = (c.get("fetch") or {}).get("async_fetch") or {}
    est = "约 3~5 分钟（异步抓取+增量缓存）" if mode == "full" else "约 1 分钟（仅行情）"
    print("预估耗时：%s" % est)

    if a.dry_run:
        print("\n[dry-run] 不执行。将运行的步骤：")
        if mode == "full":
            for s in ("prescreen（初筛）", "fetch（全A快照）", "step2（8维评分）",
                      "enhance_scores", "apply_sector_momentum", "gen_report", "cp→index.html"):
                print("   · %s" % s)
        else:
            print("   · fetch（全A快照，行情层）")
            print("   · 复用最近一期评分的因子分 → 仅刷新现价/涨跌幅/成交额/市值")
        return 0

    if mode == "quote_only":
        # —— 只跑行情：抓快照即可，评分页面沿用最近一期 ——
        #   ⚠️ 这里刻意不重跑 step2：财报因子在报告期内是常量、技术因子黏性 0.903，
        #      日频重算不产生 alpha（round25 实测 IC 峰值在 h40，持有周期 T+10）。
        ok, secs, _ = run_step(
            "行情快照 fetch（全A）",
            [PY, "fetch_top200_sina.py"],
            timeout=900)
        if ok:
            print("\n✅ 行情已更新。评分沿用最近一期 %s。" % (_dt or "?"))
            print("   页面会在页头标注『评分为 %s 期，行情为 %s』。" % (_dt or "?", date_str))
        return 0 if ok else 1

    # —— 完整链路 ——
    steps = []
    # 1) fetch 全A 快照（**内含初筛接线**，见 fetch_top200_sina.py L703-724）
    #    ⚠️ 初筛不可独立成步跑在 fetch 之前：初筛要在「全A 5377 只母体」上做，
    #       而母体（all_universe）由 fetch 落盘。前置跑 = 在已抽样的 500 只上筛，
    #       顺序颠倒会让初筛形同虚设，且与 fetch 内置初筛重复计数。
    #    --skip-prescreen 时置环境变量，由 fetch 内部读取。
    steps.append(("快照 fetch（全A母体→初筛→分层抽样）", [PY, "fetch_top200_sina.py"], 1800))
    # 3) step2 八维评分
    steps.append(("step2 八维评分", [PY, "step2_stockscorer_v2.py",
                                    "top500_all_a.json",
                                    "top200_scored_%s.json" % date_str], 5400))
    # 4) 增强 + 板块动量
    steps.append(("AI 增强", [PY, "enhance_scores.py",
                              "top200_scored_%s.json" % date_str], 1800))
    steps.append(("板块动量", [PY, "apply_sector_momentum.py",
                               "top200_scored_%s.json" % date_str], 1800))
    # 5) 报告
    steps.append(("报告渲染", [PY, "gen_report_top200.py",
                               "top200_scored_%s.json" % date_str,
                               "top200_report_%s.html" % date_str], 1800))

    total = 0.0
    results = []
    if a.skip_prescreen:
        os.environ["SCORER_SKIP_PRESCREEN"] = "1"
    for name, cmd, to in steps:
        ok, secs, err = run_step(name, cmd, timeout=to)
        total += secs
        results.append((name, ok, secs))
        if not ok:
            print("\n❌ 链路在「%s」中断（%.1fs）。后续步骤跳过。" % (name, secs))
            _summary(results, total)
            return 1

    # 6) 同步 index.html（页面口径铁律：必须显式推 index.html）
    rep = os.path.join(HERE, "top200_report_%s.html" % date_str)
    if os.path.exists(rep):
        import shutil
        shutil.copyfile(rep, os.path.join(HERE, "index.html"))
        print("\n✅ index.html 已同步（与 %s 字节一致）" % os.path.basename(rep))
        results.append(("cp→index.html", True, 0.0))

    _summary(results, total)
    return 0


def _summary(results, total):
    print("\n" + "=" * 70)
    print("链路耗时汇总")
    print("=" * 70)
    for name, ok, secs in results:
        print("  %s %-34s %7.1fs" % ("✅" if ok else "❌", name, secs))
    print("  " + "-" * 52)
    print("  合计 %.1fs（%.1f 分钟）" % (total, total / 60))


if __name__ == "__main__":
    sys.exit(main())
