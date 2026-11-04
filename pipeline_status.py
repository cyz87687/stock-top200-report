#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pipeline_status.py —— 日链步级状态记录与降级披露（round117 P0-2）

━━━ 为什么需要这个文件 ━━━
round117 实测：run_daily_local.sh 的 14 个步骤里有 12 个是 **fail-open** ——
失败只往日志写一行 ⚠️，主链继续跑，最后无条件 `echo result=success`。
更糟的是 `selfcheck_pipeline.py`（项目唯一自检入口）的退出码被 `|| echo` 吃掉
（原 L215-216），于是「自检发现可疑」和「自检通过」在日志里长得一样。

结果：**报告是在降级状态下生成的，但报告自己不知道**。
典型后果：
  · append_klines_latest 失败 → 归档用陈旧价 → fund 维覆盖率悄悄掉
  · archive_fund_universe 失败 → 同上
  · enhance_scores 失败 → AI 加权缺失，但总分照常输出
  · apply_sector_momentum 失败 → 行业维没并进去，总分口径变了
这些全都是「数字还在，但口径变了」——比崩掉更危险。

━━━ 设计原则 ━━━
1. **不改变任何步骤的阻塞性**（不改业务语义），只做「记录 + 打标」。
   哪些步骤该硬失败由 CRITICAL 集合声明，本次先把「自检失败」升级为硬失败，
   其余保持 fail-open 但**必须在报告里出现**。
2. **状态文件即真相**：每步跑完立刻落盘（append-only TSV → finalize 成 JSON），
   中途崩了也留得下已完成步骤的真实状态。
3. **缺文件 ≠ 全绿**：gen_report 读不到状态文件时，必须显示「未记录」，
   不能默认当成「全部成功」。（这是本项目反复踩的「缺字段被当成不适用」坑。）

━━━ 用法 ━━━
  # 日链里每步跑完调用（追加一行）
  python pipeline_status.py --record "append_klines_latest" 0 --note "Klines appended"

  # gen_report 之前汇总成 JSON
  python pipeline_status.py --finalize

  # 供 gen_report 读取的产物：pipeline_status_YYYY-MM-DD.json
"""
import argparse
import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
TSV = os.path.join(HERE, ".pipeline_status.tsv")

# ── 步骤分级 ────────────────────────────────────────────────────────────
# critical: 失败即口径不可信，日链应中止（目前仅 fetch/score/report 三步，
#           与 run_daily_local.sh 里已有的 exit 1 行为一致）
# degradable: 允许失败（不阻断），但必须在报告里以「降级」披露
CRITICAL = {
    "fetch_top200_sina": "行情源，失败则当天无数据",
    "check_data_fresh": "数据新鲜度守卫，失败即陈旧数据",
    "step2_stockscorer": "评分本体，失败则无 scored",
    "gen_report_top200": "报告生成，失败则无产物",
}

DEGRADABLE = {
    "append_klines_latest": ("价格库增量", "archive 会用陈旧价 → fund 维覆盖率下降"),
    "refresh_kline_meta": ("有效截面口径刷新",
                           "建库初期无效日（36 日/截面<150 只）仍被下游当作有效截面"),
    "verify_qfq_anchor": ("前复权锚点自检",
                          "检出锚点漂移 → 跨除权日收益率失真，回测收益可能偏低"),
    "archive_fund_universe": ("fund 全域归档", "fund 维（权重 .20）覆盖率停在旧值"),
    "archive_fwd_snapshot": ("前瞻估值快照归档", "fwd_pe 覆盖缺失，估值档位不可信"),
    "generate_ai_assessment": ("AI 评估", "AI 加权维度缺失，总分少一维"),
    "enhance_scores": ("评分增强", "增强项未写入，总分口径回退"),
    "sector_momentum_daily": ("行业动量", "行业动量未计算"),
    "apply_sector_momentum": ("行业维合并", "total 未含行业维 → 与权重声明不符"),
    "pick_portfolio": ("组合生成", "当日无组合建议"),
    "portfolio_risk": ("组合层风险度量",
                       "组合波动/beta/回望回撤缺失 → 读者看不到持仓的固有风险，"
                       "只能看到「选了什么」，看不到「这些票一起会怎么动」"),
    "portfolio_metrics": ("组合日频指标",
                          "推荐组合的收益/回撤/重叠度停在上一日 → 换手率与回撤视图陈旧"),
    "track_recommendations": ("推荐跟踪", "跟踪曲线断点"),
    "factor_eval": ("因子 IC + 权重重估", "IC/权重视图停在上一日"),
    # ── round125 补注册（上一轮遗漏）─────────────────────────────────────
    # 血案：round124 在 run_daily_local.sh 新增 Step 3.965 并调用 `_rec long_panel_ic`，
    #   但**没有把 "long_panel_ic" 加进本表** → finalize 会把它判成 kind="unknown"。
    #   而 unknown 的语义是「链路记录里出现了不该出现的步骤名」，
    #   于是每天都会报一条假告警，真正该看的降级反而被淹掉。
    #   ⇒ 新增 `_rec` 调用时**必须同步在本表登记**，否则步级状态不可信。
    #   本步失败的真实后果：h=20 唯一可引用 IC 停在旧版本，而页面仍显示它 →
    #   与「TTL 未过期≠数据新鲜」同型事故，必须在报告里出现。
    "long_panel_ic": ("长面板 h=20 权威 IC 重建",
                      "h=20 IC 停在上一版；页面显示的 long_panel_ic 已陈旧，"
                      "不得当作当日结论"),
    "panel_audit": ("面板审计", "权重盲区未披露 → 读者误以为全部维度都验过"),
    "selfcheck_pipeline": ("链路自检", "自检未通过或有可疑项"),
    "fetch_focus_list": ("AI 关注池", "关注池停在上一日"),
    "fetch_bench": ("指数基准", "市场状态可能基于陈旧指数而失真"),
    "regime_model": ("市场状态与风险预算", "报告显示管道告警占位"),
    "orphan_scan": ("孤儿 json 归档", "根目录继续膨胀，但当日数字不受影响"),
}

ALL_KNOWN = set(CRITICAL) | set(DEGRADABLE)


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def record(name, rc, note="", secs=None):
    """追加一步的执行结果。rc=0 成功，非 0 失败。"""
    rc = int(rc)
    # 制表符/换行会破坏 TSV，替换掉
    note = str(note or "").replace("\t", " ").replace("\n", " ")[:300]
    line = "\t".join([
        name, str(rc), "1" if rc == 0 else "0",
        secs if secs is not None else "", _now(), note,
    ])
    with open(TSV, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    return 0


def finalize(out_path=None, date_tag=None, skip_core=False):
    """把 TSV 汇总成 JSON。同名步骤保留最后一次记录（幂等重跑场景）。

    skip_core=True 表示当日走了幂等分支（scored 已存在 → 跳过 fetch/fresh/step2），
    此时这三步「未执行」是**预期行为**，不应算异常。
    """
    SKIP_CORE_STEPS = {"fetch_top200_sina", "check_data_fresh", "step2_stockscorer"}
    rows = []
    if os.path.exists(TSV):
        with open(TSV, encoding="utf-8") as f:
            for ln in f:
                ln = ln.rstrip("\n")
                if not ln.strip():
                    continue
                parts = ln.split("\t")
                if len(parts) < 3:
                    continue
                name, rc = parts[0], parts[1]
                ok = parts[2] == "1"
                secs = parts[3] if len(parts) > 3 else ""
                ts = parts[4] if len(parts) > 4 else ""
                note = parts[5] if len(parts) > 5 else ""
                rows.append({"step": name, "rc": int(rc), "ok": ok,
                             "secs": secs, "at": ts, "note": note})

    # 同名保留最后一次
    last = {}
    order = []
    for r in rows:
        if r["step"] not in last:
            order.append(r["step"])
        last[r["step"]] = r

    steps, degraded, failed_critical, unknown = [], [], [], []
    for name in order:
        r = last[name]
        kind = ("critical" if name in CRITICAL
                else "degradable" if name in DEGRADABLE
                else "unknown")
        rec = {
            "step": name,
            "kind": kind,
            "ok": r["ok"],
            "rc": r["rc"],
            "at": r["at"],
            "secs": r["secs"],
            "note": r["note"],
        }
        if kind == "critical":
            rec["why"] = CRITICAL.get(name)
        elif kind == "degradable":
            _label, _impact = DEGRADABLE[name]
            rec["label"] = _label
            rec["impact"] = _impact
        steps.append(rec)
        if not r["ok"]:
            if kind == "critical":
                failed_critical.append(name)
            elif kind == "degradable":
                degraded.append(name)
        if kind == "unknown":
            unknown.append(name)

    # 未执行的已知步骤（例如 SKIP_CORE 跳过的 fetch/step2）
    executed = set(last)
    not_run_all = sorted(ALL_KNOWN - executed)
    # 区分「预期跳过」与「异常跳过」——SKIP_CORE 时 fetch/fresh/step2 不跑是正常的
    if skip_core:
        not_run_expected = sorted(set(not_run_all) & SKIP_CORE_STEPS)
        not_run = sorted(set(not_run_all) - SKIP_CORE_STEPS)
    else:
        not_run_expected = []
        not_run = not_run_all

    n_fail_crit = len(failed_critical)
    n_degraded = len(degraded)
    n_not_run = len(not_run)
    # ⚠️ TSV 缺失必须最先判 unknown。原实现把 `elif not os.path.exists(TSV)`
    # 排在 `elif n_not_run` 之后 → TSV 不存在时 rows 为空、22 个已知步骤全进
    # not_run，分支永远命中 degraded，unknown 是**死代码**。而「没有任何记录」
    # 与「有记录但确实没跑」是两件事：前者无法判断链路是否完整，只能 unknown。
    if not os.path.exists(TSV):
        verdict = "unknown"
        verdict_text = "无步级状态记录（状态文件缺失），无法判断链路是否完整"
    elif n_fail_crit:
        verdict = "failed"
        verdict_text = f"{n_fail_crit} 个关键步骤失败，当日产物不可信"
    elif n_degraded:
        verdict = "degraded"
        verdict_text = f"{n_degraded} 个可降级步骤失败，产物已生成但口径不完整"
    elif n_not_run:
        # 有「该跑却没跑」的步骤，不能报 ok —— 这正是原来 result=success 的谎言所在
        verdict = "degraded"
        verdict_text = f"{n_not_run} 个步骤未执行（非幂等跳过），产物可能不完整"
    else:
        verdict = "ok"
        verdict_text = "全部已执行步骤成功"

    out = {
        "generated_at": _now(),
        "date": date_tag or datetime.now().strftime("%Y-%m-%d"),
        "verdict": verdict,
        "verdict_text": verdict_text,
        "skip_core": bool(skip_core),
        "counts": {
            "total_recorded": len(steps),
            "failed_critical": n_fail_crit,
            "degraded": n_degraded,
            "not_run": n_not_run,
            "not_run_expected": len(not_run_expected),
        },
        "failed_critical": failed_critical,
        "degraded": degraded,
        "not_run": not_run,
        "not_run_expected": not_run_expected,
        "unknown_steps": unknown,
        "steps": steps,
        "note": ("fail-open 步骤失败不阻断主链，但会在报告里披露；"
                 "「未执行」不等于「成功」——SKIP_CORE 幂等跳过计入 not_run_expected，"
                 "其余缺步骤计入 not_run 并降级为 degraded。"),
    }

    if out_path is None:
        out_path = os.path.join(
            HERE, f"pipeline_status_{(date_tag or datetime.now().strftime('%Y-%m-%d'))}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    return out_path, out


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--record", metavar="STEP", help="记录一步结果")
    ap.add_argument("--rc", type=int, default=0, help="退出码")
    ap.add_argument("--note", default="", help="备注")
    ap.add_argument("--secs", default=None, help="耗时秒")
    ap.add_argument("--finalize", action="store_true", help="汇总成 JSON")
    ap.add_argument("--date", default=None, help="日期标签")
    ap.add_argument("--out", default=None, help="输出路径")
    ap.add_argument("--reset", action="store_true", help="清空 TSV（每日开跑前）")
    ap.add_argument("--show", action="store_true", help="打印当前汇总")
    ap.add_argument("--skip-core", action="store_true",
                    help="当日走了幂等跳过分支（scored 已存在），fetch/fresh/step2 未执行属预期")
    a = ap.parse_args()

    if a.reset:
        if os.path.exists(TSV):
            os.remove(TSV)
        print("已清空步级状态")
        return 0

    if a.record:
        record(a.record, a.rc, a.note, a.secs)
        return 0

    if a.finalize or a.show:
        p, out = finalize(a.out, a.date, skip_core=a.skip_core)
        if a.show:
            print(f"verdict={out['verdict']} — {out['verdict_text']}")
            for s in out["steps"]:
                icon = "✅" if s["ok"] else "⚠️"
                print(f"  {icon} {s['step']:<28} rc={s['rc']}  {s.get('label','')}")
            if out["not_run"]:
                print(f"  ❗ 应跑未跑: {', '.join(out['not_run'])}")
            if out["not_run_expected"]:
                print(f"  ⏭️ 幂等跳过(预期): {', '.join(out['not_run_expected'])}")
        else:
            print(f"✅ 已写入 {os.path.basename(p)} (verdict={out['verdict']})")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
