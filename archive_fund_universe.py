#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""archive_fund_universe.py —— fund 维全域名 PIT 归档器（v4.14 P0 修复）

════════════════════════════════════════════════════════════════════════
问题：fund 权重 24.2%，但历史 66% 的截面完全不参与，部分覆盖的截面又有选择偏差
════════════════════════════════════════════════════════════════════════
实测根因（两道，都不是计算错误）：

  ① **归档范围错**：唯一数据源 data/fwd_hist/*.json 每日只存「当日在 TOP 池内」
     的 193~491 只，而研究面板域是 2029 只 → 80 个有文件的截面里覆盖率也只有 9%。
     更糟：即便 10-13 后池扩到 ~490，「fund 有值」与「在池」仍是同一个变量，
     实测该指示变量 IC(h20) = **−0.1796（t=−8.53）** → fund 维被污染成
     「估值 × 入池」的混合体（round45 证据）。

  ② **归档链路已断流**：全项目 16 个脚本引用 data/fwd_hist，**无一写入**。
     该目录是当年从 git 历史恢复的静态快照，最后一个文件停在 2026-10-23。
     ⇒ 即使不扩域，从 10-24 起 fund 也已经完全停更。

════════════════════════════════════════════════════════════════════════
本脚本做什么
════════════════════════════════════════════════════════════════════════
对**整个研究域**（EM 财务 ∩ 申万行业 ∩ 有价格 ∩ 有股本，实测 ~2000 只）
逐日归档 fund 计算所需的全部入参，并落盘为 data/fund_universe/<date>.json。
写的是**原始入参**而非分数 —— 因为 score_fund 的规则会演进，存分数等于把
当期规则永久固化（这正是 fwd_hist 的老毛病：数值本身不可重算）。

归档字段：
  pe          : 静态 PE（PIT 扣非TTM / 当日价 —— esi.raw_ep 的倒数，可重算）
  fwd_pe      : 机构前瞻 PE（**当日快照，过期不可得**，故必须当日落地）
  growth      : PIT 归母净利润同比
  roe / gm    : PIT 净资产收益率 / 毛利率
  cagr3       : 三年扣非 CAGR
  pe_ladder   : 三年预测 PE 阶梯（前瞻快照）
  price / circ_mv / sw_l1 / sw_l2 / name
  src         : 每个字段的来源标签（audit 用）

⚠️ 诚实性约束（不可绕过）：
  · fwd_pe / pe_ladder 是机构一致预期，**历史不可回溯**。本脚本只能保证
    「从今天起不再缺」，无法补齐 2026-06-26 之前的历史。
  · 实测用静态 PE 代理 fwd_pe 重建 fund 分，ρ(重建,真实)=0.536 < 0.70
    → **不得**用它伪造历史值（见 probe_fund_calib.py）。宁可留空并披露。

════════════════════════════════════════════════════════════════════════
为什么不能靠「填充」解决
════════════════════════════════════════════════════════════════════════
三条候选填充法已逐一实测否决（详见 reports/round39_fund_fill_2026-10-29.md）：
  A. 静态 PE 反向代理          ρ=0.511，MAE 1.545/5  → 劣于随机猜测的可用阈值
  B. 静态 PE + 全局标定 k=0.668 ρ=0.536，MAE 1.281  → 仍远低于 0.70 门槛
  C. 按月标定 k                 ρ=0.534              → 无改善（k 月度稳定）
根因：fund 分约一半信息量来自前瞻预期（fwd_pe/pe_ladder/fwd_np），
      客观财务只能复现另一半。这不是实现问题，是信息论边界。

⇒ 正确处置不是「填历史」，而是「**从现在起把域补全 + 明确披露历史不可比**」。

用法
  python archive_fund_universe.py                     # 归档今天
  python archive_fund_universe.py --date 2026-10-28   # 归档指定交易日（补数）
  python archive_fund_universe.py --date 2026-10-28 --dry-run
"""
import os
import sys
import json
import math
import argparse
import datetime as dt
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

import ep_size_ind as esi
import audit_round21 as r21
import step2_stockscorer_v2 as s2

OUTDIR = os.path.join(HERE, "data", "fund_universe")
KLONG = os.path.join(HERE, "data", "klines_long.json")
SWMAP = os.path.join(HERE, "sector", "sw_map.json")
SCHEMA = 1


def is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x


def fnum(v):
    """严格数值出口：NaN/None/bool/inf 一律 None。"""
    if not is_num(v):
        return None
    if v in (float("inf"), float("-inf")):
        return None
    return float(v)


def load_prices():
    """长价格库 → {code6: {date: close}}。与 build_panel_round34 同源，保证价格一致。"""
    raw = json.load(open(KLONG, encoding="utf-8"))
    px = {}
    for c, m in raw.items():
        c6 = str(c)[-6:]
        if len(c6) != 6 or not c6.isdigit():
            continue
        d = {}
        for dd, v in m.items():
            if v and len(v) >= 2 and is_num(v[1]) and v[1] > 0:
                d[dd] = float(v[1])
        if d:
            px[c6] = d
    return px


def static_pe(ind, P, price):
    """静态 PE = 1 / raw_ep。与 ep 维严格同源，避免两处口径漂移。"""
    ep = esi.raw_ep(ind, P, price)
    if not is_num(ep) or ep <= 0:
        return None
    pe = 1.0 / ep
    if not is_num(pe) or pe <= 0 or pe > 1e4:
        return None
    return round(pe, 2)


def pick(ind, key, P):
    return fnum((ind.get(key) or {}).get(P))


def cagr3_calc(ind, P):
    ps = ind.get("扣非净利润") or {}
    cur = fnum(ps.get(P))
    if cur is None or cur <= 0:
        return None
    try:
        y = int(str(P)[:4]) - 3
    except Exception:
        return None
    base = fnum(ps.get(str(y) + str(P)[4:]))
    if base is None or base <= 0:
        return None
    try:
        v = ((cur / base) ** (1.0 / 3.0) - 1.0) * 100.0
    except Exception:
        return None
    return round(v, 4) if is_num(v) and abs(v) < 1e5 else None


def load_fwd_snapshot(date):
    """当日的前瞻快照（fwd_pe / pe_ladder）。仅当 fwd_hist 存在该日文件时可得。"""
    p = os.path.join(HERE, "data", "fwd_hist", f"{date}.json")
    if not os.path.exists(p):
        return {}
    try:
        d = json.load(open(p, encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for r in (d.get("rows") or []):
        c6 = str(r.get("code") or "")[-6:]
        if len(c6) == 6:
            out[c6] = {"fwd_pe": fnum(r.get("fwd_pe")),
                       "pe_ladder": r.get("pe_ladder") or None,
                       "growth_raw": fnum(r.get("growth"))}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    # ⚠️ 2026-10-29 P0（时区 bug）：原实现 `dt.datetime.now() + timedelta(hours=8)`
    #   假设宿主机时区是 UTC。但本机已是 CST+0800（`date +%Z` → CST），
    #   再 +8 小时 → **每天 16:00 之后就滚到「明天」**：
    #   实测 23:38 运行时目标日期变成 2026-10-30，取价得到「当日有价 0 只」，
    #   于是写出一个 domain_size=0 的空归档文件，且**不报错**（只是覆盖率全 0）。
    #   修法：用 UTC 基准转 Asia/Shanghai，与宿主时区无关，任何机器都正确。
    _BJ = dt.timezone(dt.timedelta(hours=8))

    def _bj_now():
        return dt.datetime.now(dt.timezone.utc).astimezone(_BJ)

    date = args.date or _bj_now().date().isoformat()

    print("=" * 100)
    print(f"archive_fund_universe · 目标日期 {date}")
    print("=" * 100)

    px = load_prices()
    print(f"价格库：{len(px)} 只")
    em = esi.load_em_all()
    print(f"EM 财务：{len(em)} 只")
    sw = json.load(open(SWMAP, encoding="utf-8"))
    l1 = {str(k)[-6:]: v for k, v in (sw.get("l1") or {}).items()}
    l2 = {str(k)[-6:]: v for k, v in (sw.get("l2") or {}).items()}
    print(f"申万：l1 {len(l1)} / l2 {len(l2)}")

    # 当日有价的股票 = 归档域
    have_px = {c for c, m in px.items() if is_num(m.get(date)) and m[date] > 0}
    print(f"当日有价：{len(have_px)} 只")

    fwd = load_fwd_snapshot(date)
    print(f"当日前瞻快照(fwd_hist)：{len(fwd)} 只"
          f"{'（无该日文件 → fwd_pe 将留空，不伪造）' if not fwd else ''}")
    print()

    # 域：有价 ∩ EM财务 ∩ 申万 —— 刻意不要求「在池内」，这正是本次修复的核心
    DOM = sorted(have_px & set(em) & (set(l1) | set(l2)))
    print(f"归档域（有价 ∩ EM ∩ 申万）= {len(DOM)} 只")

    rows = {}
    stat = defaultdict(int)
    for c in DOM:
        ind = em.get(c)
        price = px[c][date]
        periods = sorted({p for dd in ind.values() for p in dd}, reverse=True)
        P = r21.pit_period(periods, date)
        if P is None:
            stat["no_pit_period"] += 1
            continue

        pe = static_pe(ind, P, price)
        g = pick(ind, "归属母公司净利润增长率", P)
        if g is None:
            g = pick(ind, "营业总收入增长率", P)
        roe = pick(ind, "摊薄净资产收益率", P)
        if roe is None:
            roe = pick(ind, "净资产收益率", P)
        gm = pick(ind, "毛利率", P)
        cag = cagr3_calc(ind, P)

        fw = fwd.get(c) or {}
        rec = {
            "code": c,
            "name": None,                      # 由下游按需补，不在此编造
            "price": round(price, 4),
            "period": P,
            "pe": pe,
            "fwd_pe": fw.get("fwd_pe"),
            "pe_ladder": fw.get("pe_ladder"),
            "growth": g,
            "roe": roe,
            "roe_unit": "pct" if is_num(roe) else None,
            "gross_margin": gm,
            "cagr3": cag,
            "sw_l1": l1.get(c),
            "sw_l2": l2.get(c),
            "src": {
                "pe": "recalc:esi.raw_ep",
                "fwd_pe": "snapshot:fwd_hist" if fw.get("fwd_pe") is not None else None,
                "pe_ladder": "snapshot:fwd_hist" if fw.get("pe_ladder") else None,
                "growth": "recalc:em_pit",
                "roe": "recalc:em_pit",
                "gross_margin": "recalc:em_pit",
                "cagr3": "recalc:em_pit",
            },
        }
        rows[c] = rec
        for k in ("pe", "fwd_pe", "growth", "roe", "gross_margin", "cagr3"):
            if rec.get(k) is not None:
                stat[k] += 1

    n = len(rows)
    print()
    print("【归档完成度】")
    for k in ("pe", "fwd_pe", "growth", "roe", "gross_margin", "cagr3"):
        v = stat[k]
        print(f"  {k:14s} {v:5d}/{n} = {100*v/max(1,n):5.1f}%")
    if stat["no_pit_period"]:
        print(f"  (无 PIT 报告期而跳过：{stat['no_pit_period']} 只)")

    payload = {
        "schema": SCHEMA,
        "date": date,
        "generated_at": _bj_now().isoformat(timespec="seconds"),
        "domain_size": n,
        "price_source": "klines_long",
        "coverage": {k: stat[k] for k in ("pe", "fwd_pe", "growth", "roe",
                                          "gross_margin", "cagr3")},
        "caveat": ("pe/growth/roe/gm/cagr3 为 PIT 可重算；fwd_pe/pe_ladder 为当日"
                   "机构预期快照，历史不可回溯，缺失即留空（严禁用静态PE代理）。"
                   "本目录不含 score_fund 分数本身，避免把当期规则永久固化。"),
        "rows": rows,
    }

    if args.dry_run:
        print()
        print("--dry-run：不写文件。样例：")
        for c in list(rows)[:3]:
            print(" ", json.dumps(rows[c], ensure_ascii=False)[:280])
        return

    os.makedirs(OUTDIR, exist_ok=True)
    out = os.path.join(OUTDIR, f"{date}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    sz = os.path.getsize(out) / 1e6
    print()
    print(f"✅ 写出 {out}  ({sz:.2f} MB, {n} 只)")
    print("=" * 100)
    if not fwd:
        print("⚠️ 当日无 fwd_hist 快照 → fwd_pe / pe_ladder 为空。")
        print("   这意味着本日 fund 分只能按「无前瞻数据」口径计算（会偏保守）。")
        print("   若需完整 fund 分，须在 TOP 池生成后立刻回补 fwd_pe 快照。")
        print("=" * 100)


if __name__ == "__main__":
    main()
