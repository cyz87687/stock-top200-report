#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cs_consecutive.py —— 门槛后截面「连续性」判定与披露（round118 P0-2）

═══ 为什么单独一个文件 ═══

`gen_report_top200.py` 是**顶层执行式脚本**（import 时会去读
`top200_scored_<date>.json` 并直接开始渲染），因此无法 import 它来测试内部函数。
而「门槛后 10 个截面是不是连续交易日」这条披露恰恰是最需要被测试锁住的一类：
写错了不会报错，只会安静地显示一句看起来合理、实际没有检查过任何东西的话。

故把三个纯函数抽到这里：
    consecutive_span()      纯计算：给日期列表 + 日轴 → 连续性判定
    iter_gated_dates()      纯抽取：从 factor_ic.json 取出所有门槛后日期序列
    format_consec_spans()   渲染：生成 HTML 片段

═══ 本轮实测事实（2026-11-03 快照）═══
    current_weights_composite_ic h1  n_after_gate=10
    dates_after_gate = 2026-10-13 ~ 2026-10-26（在 snapshot_dates 中索引 69~78）
    → **严格连续 10 个交易日**，即池版本切换后的单一窗口
    对照：snapshot_dates 共 83 个截面，跨 2026-06-26 ~ 2026-11-02 四个多月
    所以「10 个截面」不是从 83 个里分散抽样，而是一段连续窗口 ——
    它没有跨 regime 的稳健性信息。

═══ 关键陷阱（已踩）═══
`factor_ic.json["cross_section_audit"]` 是**聚合摘要表**，
字段只有 n / median / usable / nw_t_after_gate 等，**没有 dates_after_gate**。
日期只在 `horizons[h]["gated"]["dates_after_gate"]` 里。
若从 summary 表找日期会永远得到空列表 → 那句「已检查连续性」就是假的。
`iter_gated_dates()` 就是为绕开这个坑而存在的，并在单测里锁住。
"""
import html as html_mod


def _consecutive_span(dates_after_gate, all_dates):
    """判断门槛后的截面日是否构成**连续的一段**（round118 P0-2）。

    为什么要这个函数：`n_after_gate=10` 这个数字本身看不出「这 10 个是一段连续
    窗口，还是散布在整个样本期」。两者的证据强度完全不同 ——
    散布 = 跨 regime 稳健；连续 = 单一窗口，没有跨期稳健性信息。
    实测（2026-11-03 快照）：门槛后 10 个日期在 `snapshot_dates` 中的索引是
    69~78，**严格连续**，即池版本切换后的单一 10 日窗口。

    返回 dict：
      n               门槛后日期数
      n_in_axis       能在 all_dates 里定位到的个数（防日期不在轴上）
      is_consecutive  是否索引严格连续
      max_run         最长连续段长度
      span_start/end  整段的首末日期
      longest_run     最长连续段的首末日期
    定位不到时全部返回 None（宁可不显示，也不能给错结论）。
    """
    ds = [str(d) for d in (dates_after_gate or [])]
    axis = [str(d) for d in (all_dates or [])]
    if not ds or not axis:
        return {"n": len(ds), "n_in_axis": 0, "is_consecutive": None,
                "max_run": None, "span_start": None, "span_end": None,
                "longest_run": None}
    idx_map = {d: i for i, d in enumerate(axis)}
    idx = [idx_map[d] for d in ds if d in idx_map]
    if not idx:
        return {"n": len(ds), "n_in_axis": 0, "is_consecutive": None,
                "max_run": None, "span_start": ds[0], "span_end": ds[-1],
                "longest_run": None}
    idx_sorted = sorted(idx)
    # 最长连续段（索引差 == 1）
    best_len, best_start, cur_len, cur_start = 1, idx_sorted[0], 1, idx_sorted[0]
    for prev, cur in zip(idx_sorted, idx_sorted[1:]):
        if cur - prev == 1:
            cur_len += 1
        else:
            if cur_len > best_len:
                best_len, best_start = cur_len, cur_start
            cur_len, cur_start = 1, cur
    if cur_len > best_len:
        best_len, best_start = cur_len, cur_start
    return {
        "n": len(ds),
        "n_in_axis": len(idx),
        "is_consecutive": (len(idx) == len(ds) and
                           len(idx_sorted) == len(ds) and
                           all(b - a == 1 for a, b in zip(idx_sorted, idx_sorted[1:]))),
        "max_run": best_len,
        "span_start": ds[0],
        "span_end": ds[-1],
        "longest_run": (axis[best_start], axis[best_start + best_len - 1]),
    }


def _iter_gated_dates(ic):
    """从 factor_ic.json 里抽出所有「(口径, 周期, 门槛后日期列表)」。

    为什么不能读 `cross_section_audit`：那个 summary 表只有 n / median / usable
    等**聚合数字**，**没有** `dates_after_gate`。日期字段只存在两处：
        · `ic["current_weights_composite_ic"][h]["gated"]["dates_after_gate"]`
        · `ic["tradable_*_ic"]["horizons"][h]["gated"]["dates_after_gate"]`
    直接拿 summary 表去找日期会永远得到空列表 —— 那是「看起来检查了、
    实际什么都没检查」的典型形态，故单列本函数并在测试里锁住。
    """
    out = []

    def _take(label, node_h):
        if not isinstance(node_h, dict):
            return
        g = node_h.get("gated")
        if isinstance(g, dict):
            ds = g.get("dates_after_gate") or []
            if len(ds) >= 2:
                out.append((label, node_h.get("__h"), g.get("n_after_gate"), ds))

    node = (ic or {}).get("current_weights_composite_ic")
    if isinstance(node, dict):
        for h in ("1", "5", "20"):
            _take("current_weights_composite_ic", dict(node.get(h) or {}, __h=h))
    for lab in ("tradable_ic", "tradable_w_ic", "tradable_fund_ic"):
        node = (ic or {}).get(lab)
        if not isinstance(node, dict):
            continue
        hz = node.get("horizons") or {}
        for h in ("1", "5", "20"):
            _take(lab, dict(hz.get(h) or {}, __h=h))
    return out


def _format_consec_spans(ic):
    """把每个「口径 × 周期」的门槛后连续性渲染成一段 HTML（round118 P0-2）。

    只在 n_after_gate >= 2 且能在 snapshot_dates 上定位时输出；
    否则返回空串，**不制造「已检查」的假象**。
    """
    axis = [str(d) for d in ((ic or {}).get("snapshot_dates") or [])]
    rows = []
    for label, h, n_ag, ds in _iter_gated_dates(ic):
        sp = _consecutive_span(ds, axis)
        if sp.get("is_consecutive") is None:
            continue
        if sp["is_consecutive"]:
            tag = (f'<b style="color:#f87171;">连续 {sp["n"]} 个交易日</b>'
                   f'（{html_mod.escape(str(sp["span_start"]))} ~ '
                   f'{html_mod.escape(str(sp["span_end"]))}）')
        else:
            lr = sp.get("longest_run") or ("—", "—")
            tag = (f'<b style="color:#fbbf24;">分散</b>（最长连续 {sp["max_run"]} 个，'
                   f'{html_mod.escape(str(lr[0]))} ~ {html_mod.escape(str(lr[1]))}）')
        rows.append(
            '<tr style="border-top:1px solid #1e293b;">'
            f'<td style="padding:3px 8px;color:#cbd5e1;font-size:11px;">'
            f'{html_mod.escape(str(label))}</td>'
            f'<td style="padding:3px 8px;text-align:center;color:#e2e8f0;font-weight:700;">'
            f'h{html_mod.escape(str(h))}</td>'
            f'<td style="padding:3px 8px;text-align:center;color:#94a3b8;">'
            f'{n_ag if n_ag is not None else sp["n"]}</td>'
            f'<td style="padding:3px 8px;color:#cbd5e1;">{tag}</td></tr>')
    if not rows:
        return ""
    _n_consec = sum(1 for label, h, _n, ds in _iter_gated_dates(ic)
                    if (_consecutive_span(ds, axis) or {}).get("is_consecutive"))
    return (
        '<div style="margin:10px 0 0;padding:10px 12px;background:rgba(239,68,68,0.05);'
        'border:1px dashed rgba(239,68,68,0.35);border-radius:6px;">'
        '<div style="font-size:12px;font-weight:800;color:#fca5a5;margin-bottom:6px;">'
        '⚠️ 门槛后截面是不是「连续一段时间」—— 这决定它有没有跨期稳健性</div>'
        '<div style="font-size:11px;color:#94a3b8;line-height:1.7;margin-bottom:6px;">'
        f'当前 <b style="color:#f87171;">{_n_consec} 个口径-周期的门槛后截面是连续交易日</b>。'
        '若门槛后 N 个截面是<b>连续交易日</b>，它们共享同一个市场 regime 与同一个池版本，'
        '因此只能说明「在那一段时间里有效」，<b>不构成跨期稳健性证据</b>；'
        '分散采样则相反。此判定取自 <code>factor_ic.json.snapshot_dates</code>'
        '（IC 自己的截面日轴，83 个），<b>不是价格库日历</b> —— 两者日期集合不同，不可混用。'
        '</div>'
        '<table style="width:100%;border-collapse:collapse;font-size:11.5px;">'
        '<thead><tr style="background:#1e293b;">'
        '<th style="padding:4px 8px;text-align:left;color:#94a3b8;font-weight:600;">口径</th>'
        '<th style="padding:4px 8px;color:#94a3b8;font-weight:600;">周期</th>'
        '<th style="padding:4px 8px;color:#94a3b8;font-weight:600;">门槛后截面</th>'
        '<th style="padding:4px 8px;text-align:left;color:#94a3b8;font-weight:600;">'
        '日期分布</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div>')


# ── 公开别名（测试与外部调用用；下划线版保持内部使用）──
consecutive_span = _consecutive_span
iter_gated_dates = _iter_gated_dates
format_consec_spans = _format_consec_spans


def _selftest():
    """无 pytest 时的自检。返回退出码。"""
    import json
    import os
    ok = 0
    fail = []

    def chk(name, cond, msg=""):
        nonlocal ok
        if cond:
            ok += 1
        else:
            fail.append(f"{name}: {msg}")

    axis = ["2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16",
            "2026-10-19", "2026-10-20"]
    sp = _consecutive_span(axis, axis)
    chk("连续全段", sp["is_consecutive"] is True and sp["max_run"] == 6, sp)
    sp2 = _consecutive_span([axis[0], axis[1], axis[3], axis[4]], axis)
    chk("有缺口", sp2["is_consecutive"] is False and sp2["max_run"] == 2, sp2)
    sp3 = _consecutive_span([], axis)
    chk("空输入", sp3["is_consecutive"] is None, sp3)
    sp4 = _consecutive_span(["2099-01-01", "2099-01-02"], axis)
    chk("不在轴上", sp4["is_consecutive"] is None and sp4["n_in_axis"] == 0, sp4)

    if os.path.exists("factor_ic.json"):
        with open("factor_ic.json", encoding="utf-8") as f:
            ic = json.load(f)
        rows = _iter_gated_dates(ic)
        chk("能抽到门槛后日期", len(rows) > 0,
            "cross_section_audit 里没有 dates_after_gate，必须从 horizons[h].gated 取")
        # summary 表不得被当成日期来源
        chk("summary 表不含日期",
            all("dates_after_gate" not in v
                for v in (ic.get("cross_section_audit") or {}).values()
                if isinstance(v, dict)),
            "若 summary 表突然含日期，本模块的取数路径需复核")
        if rows:
            _lab, _h, _n, ds = rows[0]
            s = _consecutive_span(ds, ic.get("snapshot_dates") or [])
            chk("首个口径可判定", s["is_consecutive"] is not None, s)
        h = _format_consec_spans(ic)
        chk("能渲染", isinstance(h, str) and ("连续" in h or "分散" in h), h[:120])

    print(f"通过 {ok} 项，失败 {len(fail)} 项")
    for f_ in fail:
        print("  ✗", f_)
    return 0 if not fail else 1


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
