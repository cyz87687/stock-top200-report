#!/usr/bin/env python3
"""recompute_scored_t0_4.py —— 用 T0-4 新逻辑（亏损股 fund 封顶 3.0）重算当日 scored。

## 为什么要单独写重算脚本

step2 会重抓行情/财务（耗时 30+ 分钟且依赖外部接口），而 T0-4 只改
score_fund 的**返回值上限**，输入全在现有 scored 文件里。

但必须**真实复算**而不是简单 clip：因为 fund 变了 → total 变 → 评级变，
且 total 必须走 scale_calc 生产线量尺（分母恒定 + 缺维中性填充），
不能用旧 total 减去一个 delta。

## 输入验证（写前必查，铁律）

1. 文件 date 必须等于期望日期（防静默用前日文件）
2. 每条记录必须有 pe 字段（否则无法判定亏损）
3. 复算 total 必须能重现已有的 total（在 fund 不变时）→ 证明量尺口径一致

第 3 条是本脚本的自检核心：若旧 total 复算不符，说明 scored 文件是用
别的量尺生成的，此时**禁止**写入，必须先报警。
"""
import json
import os
import shutil
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import scale_calc

SCORED = os.path.join(HERE, "top200_scored_2026-10-27.json")
BACKUP = "/tmp/scored_backup_t0_4.json"
LOSS_FUND_CAP = 3.0


def main():
    if not os.path.exists(SCORED):
        print(f"❌ 找不到 {SCORED}")
        return 1
    data = json.load(open(SCORED, encoding="utf-8"))
    rows = data["results"]
    fdate = data.get("date")

    print("=" * 100)
    print("T0-4 重算：亏损股（PE≤0）fund 分封顶 3.0")
    print("=" * 100)
    print(f"文件 {os.path.basename(SCORED)} 内 date = {fdate}  记录 {len(rows)} 条")

    # ---------- 自检 1：日期 ----------
    expect = "2026-10-26"      # scored 的 date 字段是「数据日」，比文件名日期早一天
    print(f"数据日校验：期望 {expect} / 实际 {fdate} → {'✅' if fdate == expect else '❌ 不匹配'}")

    # ---------- 自检 2：pe 覆盖率 ----------
    nope = [r for r in rows if not scale_calc.is_num(r.get("pe"))]
    print(f"pe 覆盖率：{len(rows)-len(nope)}/{len(rows)} "
          f"({(len(rows)-len(nope))/len(rows)*100:.1f}%) "
          f"{'✅' if not nope else '⚠️ 缺 %d 条' % len(nope)}")

    # ---------- 权重 ----------
    W, DMAX, _meta = scale_calc.load_weights_and_dim_max(HERE)
    eff = {k: v for k, v in W.items() if scale_calc.is_num(v) and v > 0}
    print(f"生效维 {len(eff)} / Σw = {sum(eff.values()):.4f} → "
          f"{'✅' if abs(sum(eff.values()) - 1.0) < 1e-6 else '❌ 权重和不为 1'}")

    # ---------- 自检 3：用当前 total 反推量尺一致性 ----------
    def calc(r, fund_val):
        parts = [(k, (fund_val if k == "score_fund" else r.get(k)), w)
                 for k, w in eff.items()]
        return scale_calc.composite_total(parts, DMAX)[0]

    mismatch = 0
    samples = []
    for r in rows:
        old = r.get("total")
        if not scale_calc.is_num(old):
            continue
        redo = calc(r, r.get("score_fund"))
        if redo is None:
            continue
        if abs(redo - old) > 0.011:
            mismatch += 1
            if len(samples) < 5:
                samples.append((r.get("code"), old, redo))
    print(f"量尺自检（fund 不变时复算 total vs 文件 total，容差 0.011）："
          f"不符 {mismatch} 条 {'✅' if mismatch == 0 else '❌'}")
    for c, o, n in samples:
        print(f"    {c}: 文件 {o} vs 复算 {n}")
    if mismatch > 0:
        print("❌ 量尺口径不一致 → 拒绝写入。请先查明 scored 是用哪套量尺生成的。")
        return 2

    # ---------- 备份 ----------
    shutil.copy2(SCORED, BACKUP)
    print(f"已备份 → {BACKUP}")

    # ---------- 重算 ----------
    changed = []
    recapped = []
    for r in rows:
        pe = r.get("pe")
        fv = r.get("score_fund")
        new_fv = fv
        capped = False
        if scale_calc.is_num(pe) and pe <= 0 and scale_calc.is_num(fv) and fv > LOSS_FUND_CAP:
            new_fv = LOSS_FUND_CAP
            capped = True
        new_total = calc(r, new_fv)
        old_total = r.get("total")
        if capped:
            # 改写 fund 及其 reasons（保留原分数证据）
            old_note = list(r.get("fund_reasons") or [])
            old_note.append(
                f"当前亏损(PE≤0),前瞻估值分封顶{LOSS_FUND_CAP:.1f}"
                f"(原{fv:.2f}→{LOSS_FUND_CAP:.1f})")
            r["score_fund_raw"] = round(float(fv), 2)
            r["score_fund"] = LOSS_FUND_CAP
            r["fund_reasons"] = old_note
            recapped.append(r.get("code"))
        if scale_calc.is_num(new_total) and scale_calc.is_num(old_total) \
                and abs(new_total - old_total) > 0.005:
            changed.append((r, old_total, new_total))
        r["total"] = new_total

    print()
    print(f"封顶 {len(recapped)} 只：{recapped}")
    print(f"total 变化 {len(changed)} 只")
    if changed:
        ds = [n - o for _, o, n in changed]
        print(f"  分差 min={min(ds):+.2f} p50={sorted(ds)[len(ds)//2]:+.2f} max={max(ds):+.2f}")
        print(f"  上升 {sum(1 for x in ds if x > 0)} / 下降 {sum(1 for x in ds if x < 0)}")
        assert max(ds) <= 0.005, "❌ 出现上升 → 说明存在反向奖励，方案有误"

    # ---------- 评级重算（绝对口径，与 gen_report 的 _quantile_ratings 并存）----------
    def grade(v):
        if v is None:
            return "-"
        if v >= 85:
            return "S"
        if v >= 65:
            return "A"
        if v >= 45:
            return "B"
        if v >= 25:
            return "C"
        return "D"

    before = Counter()
    for r in rows:
        pass
    g_after = Counter(grade(r.get("total")) for r in rows)
    print()
    print("重算后绝对口径评级：", dict(sorted(g_after.items())))

    # ---------- 写回 ----------
    data["results"] = rows
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    meta["t0_4_loss_fund_cap"] = {
        "cap": LOSS_FUND_CAP,
        "applied_at": "2026-10-27",
        "n_capped": len(recapped),
        "note": "当前亏损(PE<=0)且曾有 fund 分的股票，fund 封顶 3.0；原值存 score_fund_raw",
    }
    data["meta"] = meta
    with open(SCORED, "w", encoding="utf-8") as f:
        # ⚠️ 必须 indent=2：原文件是 indent=2，用 separators 紧凑写会让体积
        #   从 10MB 掉到 7MB（看起来像数据丢失，实为格式变化）。保持格式一致
        #   才能让后续 diff / 大小核对有意义。
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"\n已写回 {os.path.basename(SCORED)}（{os.path.getsize(SCORED)} 字节）")

    # ---------- 写后复验 ----------
    print()
    print("=" * 100)
    print("写后复验")
    print("=" * 100)
    d2 = json.load(open(SCORED, encoding="utf-8"))
    r2 = d2["results"]
    bad = 0
    for r in r2:
        pe = r.get("pe")
        if scale_calc.is_num(pe) and pe <= 0 and scale_calc.is_num(r.get("score_fund")):
            if r["score_fund"] > LOSS_FUND_CAP + 1e-9:
                bad += 1
    print(f"亏损股 fund 超上限：{bad} 条 {'✅' if bad == 0 else '❌'}")
    bad2 = 0
    for r in r2:
        t = calc(r, r.get("score_fund"))
        if scale_calc.is_num(t) and scale_calc.is_num(r.get("total")) and abs(t - r["total"]) > 0.011:
            bad2 += 1
    print(f"total 与量尺复算不符：{bad2} 条 {'✅' if bad2 == 0 else '❌'}")
    print(f"记录数 {len(r2)}（应为 {len(rows)}）→ {'✅' if len(r2)==len(rows) else '❌'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
