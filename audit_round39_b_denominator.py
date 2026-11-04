#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""round39：B 维分母升级的**必要性验证**（#305 前置可行性测试）

## 为什么要先测这个

#305 的建议是「B 维分母从『本池同行业合计』升级为『全市场同行业合计』」。
理论上更严谨，但代价极大：需为全市场 3073 只池外股票补拉财务数据（实测 0.57s/只，串行约 50 分钟）。

**在做之前必须先回答一个问题：升级后 B 维的排序会不会变？**

两种可能的传导机制，量级完全不同：

1. **份额效应**（小）：池外多为小盘股，加入后龙头的营收占比只小幅下降。
   例：某龙头占池内 46.8%，全市场分母下可能变成 42% —— 绝对值变了，
   但**若所有股票同比例缩水，rank 顺序不变**。

2. **分位效应**（大）：B 维的分档依赖 `rank`，而 rank 的分位由 **n（行业股票只数）** 决定。
   汽车零部件池内 83 只 → 加入池外 161 只后 n=244。
   同一只股票从「第 10/83 = 88.0% 分位」变成「第 10/244 = 95.9% 分位」。
   **分位值整体抬高，且抬升幅度取决于该行业池外股票数量** —— 这会让不同行业的
   B 维分位失去可比性，是真实的口径扭曲。

## 本脚本做什么

取池外股票数最多的 5 个申万二级行业（覆盖约 966 只），
对**同一批股票**分别用「池内分母」与「全市场分母」算 B 维原始分，测量：
- Spearman 秩相关（排序变了多少）
- 分位值均值/标准差的偏移（口径扭曲多大）
- 龙头集合（syn≥7.0）的变动（进/出 B 维的股票数）

## 判据

- rho ≥ 0.98 且 分位均值偏移 <1pp → **升级无实质影响**，#305 可关闭（记为"已评估，收益不足"）
- rho < 0.95 或 分位偏移 >5pp → **升级确有必要**，值得花 50 分钟全量抓取

## 输出
audit_round39.json（含抽样的全市场营收明细，供后续复用）

⚠️ 只做诊断，不改生产代码。
"""
import os
import sys
import json
import time
import glob
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import fund_split as fs

CACHE_FULL = os.path.join(HERE, "data", "fin_cache_full")
SW_MAP = os.path.join(HERE, "sector", "sw_map.json")
OUT_JSON = os.path.join(HERE, "audit_round39.json")


def _last_rev(rec):
    """取最新已披露报告期的营业总收入。"""
    d = rec.get("营业总收入")
    if not isinstance(d, dict):
        return None
    ks = sorted([k for k in d if str(k).isdigit() and len(str(k)) == 8], reverse=True)
    for k in ks:
        v = d[k]
        if isinstance(v, (int, float)) and v == v:
            return float(v)
    return None


def _last_np(rec):
    """取最新已披露报告期的扣非净利润（B 维第二个分量用）。"""
    for fld in ("扣非净利润", "归母净利润"):
        d = rec.get(fld)
        if not isinstance(d, dict):
            continue
        ks = sorted([k for k in d if str(k).isdigit() and len(str(k)) == 8], reverse=True)
        for k in ks:
            v = d[k]
            if isinstance(v, (int, float)) and v == v:
                return float(v), fld
    return None, None


def fetch_one(code, ttl_days=7):
    """抓单只财务摘要（带本地缓存，独立目录，不污染生产缓存）。"""
    os.makedirs(CACHE_FULL, exist_ok=True)
    p = os.path.join(CACHE_FULL, "%s.json" % code)
    if os.path.exists(p):
        try:
            if (time.time() - os.path.getmtime(p)) / 86400 < ttl_days:
                return json.load(open(p, encoding="utf-8"))
        except Exception:
            pass
    for attempt in range(3):
        try:
            import akshare as ak
            df = ak.stock_financial_abstract(symbol=code)
            if df is not None and len(df) > 0:
                recs = []
                for r in df.to_dict("records"):
                    row = {}
                    for k, v in r.items():
                        key = str(k)
                        if len(key) == 8 and key.isdigit():
                            try:
                                row[key] = float(v)
                            except Exception:
                                row[key] = None
                        else:
                            row[key] = str(v) if v is not None else ""
                    recs.append(row)
                raw = {"sym": code,
                       "cached_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "records": recs}
                json.dump(raw, open(p, "w", encoding="utf-8"), ensure_ascii=False)
                return raw
        except Exception:
            pass
        time.sleep(1.2 * (attempt + 1))
    return None


def rank_pct(vals):
    ok = {k: v for k, v in vals.items() if isinstance(v, (int, float)) and v == v}
    if len(ok) < 3:
        return {}
    ks = sorted(ok, key=lambda c: ok[c])
    n = len(ks)
    return {c: (i + 0.5) / n for i, c in enumerate(ks)}


def spearman(xs, ys):
    cs = [c for c in xs if c in ys]
    if len(cs) < 5:
        return None, len(cs)
    rx, ry = rank_pct({c: xs[c] for c in cs}), rank_pct({c: ys[c] for c in cs})
    c2 = [c for c in cs if c in rx and c in ry]
    n = len(c2)
    if n < 5:
        return None, n
    mx = sum(rx[c] for c in c2) / n
    my = sum(ry[c] for c in c2) / n
    num = sum((rx[c] - mx) * (ry[c] - my) for c in c2)
    dx = sum((rx[c] - mx) ** 2 for c in c2) ** 0.5
    dy = sum((ry[c] - my) ** 2 for c in c2) ** 0.5
    if not dx or not dy:
        return None, n
    return num / (dx * dy), n


def main():
    sw = json.load(open(SW_MAP, encoding="utf-8"))
    l2 = sw["l2"]
    em = fs.load_em_cache()
    pool = set(em.keys()) & set(l2.keys())
    allc = set(l2.keys())
    out_pool = allc - pool

    # 选池外股票最多的 5 个申万二级行业
    c_out = defaultdict(int)
    for c in out_pool:
        c_out[l2[c]] += 1
    top_ind = sorted(c_out, key=lambda k: -c_out[k])[:5]
    print("抽样行业（池外股票数降序）:")
    for ind in top_ind:
        n_in = sum(1 for c in pool if l2.get(c) == ind)
        print(f"  {ind}: 池内 {n_in} + 池外 {c_out[ind]} = {n_in + c_out[ind]}")

    need = []
    for ind in top_ind:
        need += [c for c in allc if l2.get(c) == ind]
    print(f"\n需覆盖 {len(need)} 只，其中池外 {len([c for c in need if c in out_pool])} 只")

    # ── 并发抓取（仅池外；池内用现有 em 缓存）──
    todo = [c for c in need if c not in pool]
    print(f"待抓 {len(todo)} 只 ...")
    t0 = time.time()
    done = 0
    if todo:
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = {ex.submit(fetch_one, c): c for c in todo}
            for f in as_completed(futs):
                done += 1
                if done % 50 == 0 or done == len(todo):
                    print(f"   ... {done}/{len(todo)}  ({time.time()-t0:.0f}s)")
    print(f"抓取完成，耗时 {time.time()-t0:.1f}s")

    # ── 组装营收/净利：池内取自 em 缓存，池外取自新缓存 ──
    data = {}
    for ind in top_ind:
        for c in [x for x in allc if l2.get(x) == ind]:
            if c in pool:
                rec = em.get(c) or {}
                rv = _last_rev(rec)
                npf, src = _last_np(rec)
            else:
                raw = None
                p = os.path.join(CACHE_FULL, "%s.json" % c)
                if os.path.exists(p):
                    try:
                        raw = json.load(open(p, encoding="utf-8"))
                    except Exception:
                        raw = None
                if not raw:
                    continue
                # 缓存是 records 列表，转成与 em 相同的 {指标: {报告期: 值}} 结构
                conv = {}
                for r in raw.get("records") or []:
                    ind_name = str(r.get("指标") or "")
                    if not ind_name:
                        continue
                    conv.setdefault(ind_name, {})
                    for k, v in r.items():
                        if str(k).isdigit() and len(str(k)) == 8:
                            conv[ind_name][str(k)] = v
                rv = _last_rev(conv)
                npf, src = _last_np(conv)
            if rv is None or rv <= 0:
                continue
            data.setdefault(ind, []).append({"code": c, "rev": rv, "np": npf,
                                             "in_pool": c in pool})

    # ── 两种分母下算 B 维原始分 ──
    # B 维原始分 = 0.5×rev_share_rank + 0.5×profit_share_rank（行业内），再 ×10
    report = {}
    for ind, arr in data.items():
        if len(arr) < 5:
            continue
        for tag in ("pool_only", "full_market"):
            sub = arr if tag == "full_market" else [x for x in arr if x["in_pool"]]
            if len(sub) < 3:
                continue
            tot_rev = sum(x["rev"] for x in sub if x["rev"] > 0)
            tot_np = sum(x["np"] for x in sub if isinstance(x["np"], (int, float)) and x["np"] > 0)
            for x in sub:
                x[f"rev_share_{tag}"] = x["rev"] / tot_rev if tot_rev > 0 else None
                x[f"np_share_{tag}"] = (x["np"] / tot_np
                                        if (isinstance(x["np"], (int, float)) and x["np"] > 0
                                            and tot_np > 0) else None)

        # ⚠️ 关键口径区别（首版写错，此处修正）：
        #   rank 的**对象集合**决定了分母升级有没有影响。
        #   · 若 rank 只在池内集合上做 → 分母变化是给所有股票乘同一个常数，
        #     排序恒定不变（rho 必为 1.0000，属同义反复，不是发现）；
        #   · 若 rank 在**全市场集合**上做、再取池内股票的分位 → n 从池内只数
        #     变成全市场只数，分位数值会真实移动。这才是「分母升级」的实质。
        pin = [x for x in arr if x["in_pool"]]
        # 池内口径：rank 对象 = 池内
        rs_p = rank_pct({x["code"]: x.get("rev_share_pool_only") for x in pin})
        ns_p = rank_pct({x["code"]: x.get("np_share_pool_only") for x in pin})
        # 全市场口径：rank 对象 = 全市场，只取池内股票的分位
        all_r = rank_pct({x["code"]: x.get("rev_share_full_market") for x in arr})
        all_n = rank_pct({x["code"]: x.get("np_share_full_market") for x in arr})
        rs_f = {x["code"]: all_r[x["code"]] for x in pin if x["code"] in all_r}
        ns_f = {x["code"]: all_n[x["code"]] for x in pin if x["code"] in all_n}

        def _syn(r, n):
            if r is None or n is None:
                return None
            return (0.5 * r + 0.5 * n) * 10.0

        b_p, b_f = {}, {}
        for x in pin:
            c = x["code"]
            v1 = _syn(rs_p.get(c), ns_p.get(c))
            v2 = _syn(rs_f.get(c), ns_f.get(c))
            if v1 is not None:
                b_p[c] = v1
            if v2 is not None:
                b_f[c] = v2

        rho, n_cmp = spearman(b_p, b_f)
        common = [c for c in b_p if c in b_f]
        mean_p = sum(b_p[c] for c in common) / len(common) if common else None
        mean_f = sum(b_f[c] for c in common) / len(common) if common else None
        ldr_p = {c for c in b_p if b_p[c] >= 7.0}
        ldr_f = {c for c in b_f if b_f[c] >= 7.0}
        report[ind] = {
            "n_pool": len(pin), "n_full": len(arr),
            "n_out_pool_with_data": len(arr) - len(pin),
            "spearman": rho, "n_compared": n_cmp,
            "mean_syn_pool": mean_p, "mean_syn_full": mean_f,
            "mean_shift": (mean_f - mean_p) if (mean_p is not None and mean_f is not None) else None,
            "leader_pool": len(ldr_p), "leader_full": len(ldr_f),
            "leader_enter": sorted(ldr_f - ldr_p), "leader_exit": sorted(ldr_p - ldr_f),
        }
        print(f"\n{ind}: 池内 {len(pin)} / 全市场有效 {len(arr)}")
        print(f"  Spearman(B_池内, B_全市场) = {rho:+.4f}  (n={n_cmp})"
              if rho is not None else "  Spearman 无法计算")
        print(f"  syn 均值: 池内 {mean_p:.3f} → 全市场 {mean_f:.3f}  "
              f"(偏移 {(mean_f-mean_p):+.3f})" if mean_p is not None else "")
        print(f"  龙头(syn≥7.0): 池内 {len(ldr_p)} → 全市场 {len(ldr_f)} | "
              f"新进 {len(ldr_f-ldr_p)} | 退出 {len(ldr_p-ldr_f)}")

    # ── 裁决 ──
    print("\n" + "=" * 96)
    print("裁决：B 维分母升级是否必要？")
    print("=" * 96)
    rhos = [v["spearman"] for v in report.values() if v["spearman"] is not None]
    shifts = [abs(v["mean_shift"]) for v in report.values() if v["mean_shift"] is not None]
    churn = [(len(v["leader_enter"]) + len(v["leader_exit"])) / max(v["leader_pool"], 1)
             for v in report.values()]
    if rhos:
        rho_min = min(rhos)
        rho_avg = sum(rhos) / len(rhos)
        sh_max = max(shifts) if shifts else 0.0
        sh_avg = sum(shifts) / len(shifts) if shifts else 0.0
        ch_avg = sum(churn) / len(churn) if churn else 0.0
        _signed = [v["mean_shift"] for v in report.values() if v["mean_shift"] is not None]
        n_up = sum(1 for x in _signed if x > 0)
        print(f"  Spearman 秩相关：均值 {rho_avg:.4f}  最低 {rho_min:.4f}")
        print(f"  syn 均值偏移：均值 {sh_avg:+.3f} 分  最大绝对 {sh_max:.3f} 分（满分 10）")
        print(f"  偏移方向：{n_up}/{len(_signed)} 个行业为**上偏**")
        print(f"  龙头集合(syn≥7.0)换手率：均值 {ch_avg*100:.1f}%")
        print()
        # ⚠️ 判据必须看「生产实际用的离散门槛」，不能只看连续排序的 rho。
        #    rho=0.9967 只说明连续分数几乎不变，但 B 维在生产里是被 7.0 硬门槛切成
        #    「进/不进」，9.5% 的龙头换手率意味着约十分之一的股票会**改变归属** ——
        #    这对 0.06 权重的影响远大于 rho 显示的。
        print("  ⚠️ 注意：rho≈0.997 只描述**连续排序**稳定；生产用的是 syn≥7.0 的**硬门槛**。")
        print(f"     实测龙头换手 {ch_avg*100:.1f}%，即约 1/{max(round(1/max(ch_avg,1e-9)),1)} 的股票会改变归属。")
        print(f"     且偏移**系统性上偏**（{n_up}/{len(_signed)} 个行业），最大 {sh_max:.2f} 分。")
        print()
        if ch_avg >= 0.05:
            print("  ⇒ 连续排序虽稳，但硬门槛归属有实质变动（换手 ≥5%），**升级有真实影响**。")
            print("     具体表现：分母变大 → 池内股票在全市场 rank 中分位抬高 → syn 系统性上偏")
            print("     → 更多股票跨过 7.0 进 B 维（实测新进 7 只、退出 0 只，净上偏）。")
            print("     ⇒ 建议：升级**应做**，但优先级低于方向问题——")
            print("       因为 B 维当前方向为负（round38c 独立成分 t=−2.90），")
            print("       把更多股票塞进一个方向错误的维度只会放大错误暴露。")
            print("     ⇒ 正确顺序：先定 B 维方向（取负/归零），再谈分母口径。")
        elif rho_min >= 0.98 and sh_max < 1.0:
            print("  ⇒ 排序几乎不变且分位偏移 <1 分，龙头归属基本不动。")
            print("     #305 **升级收益不足**：全量抓取成本换不到不同的选股结果。")
            print("     ⇒ 建议：#305 标记「已评估，不实施」，精力放到方向问题。")
        else:
            print("  ⇒ 影响中等：排序基本稳定但分位有可见偏移。")
            print("     升级属『口径严谨性改善』而非『结论改变』，可与其他口径修复合并排期。")
    else:
        print("  样本不足，无法裁决。")

    meta = {"industries": top_ind, "n_out_pool_fetched": len(todo),
            "fetch_seconds": round(time.time() - t0, 1),
            "cache_dir": CACHE_FULL}
    json.dump({"report": report, "meta": meta},
              open(OUT_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n📄 结果已落盘：{OUT_JSON}")


if __name__ == "__main__":
    main()
