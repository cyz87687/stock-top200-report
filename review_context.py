#!/usr/bin/env python3
"""每日复盘「手工快照」的单一判据与取数助手。

背景(2026-11-21 裁决)：隔夜外盘/研报/连板/两融四类字段在 Wind/通达信/westock MCP
会话内可实测取到真实数据，但 MCP 工具是会话级的，日链 Python 脚本无法直接调用，
底层 HTTP 直连需登录。故采用「手工快照」：每日由助手在会话内调 MCP 取数 → 写入
review_context_<A股交易日>.json → 报告读取本模块判据决定展示真实值或"未接入"。

铁律：
- 快照 trade_date 必须 == 报告数据日期(date_str)，否则整份快照判为不可用(日期守卫)。
- 每个字段必须有 source / as_of / available；available=true 才展示，缺字段回退"未接入"。
- as_of 是数据真实时点，可与 trade_date 不同(如隔夜外盘=美股前一交易日收盘=PIT)，
  页面须同时显示 as_of 与来源，禁把旧数据伪装成当日。
- 无源字段禁编造：available=false 时不产出任何数值。

用法：
  python review_context.py check 2026-11-20       # 校验快照并打印摘要
  python review_context.py guide                   # 打印每日手动取数指引
"""
import json
import os
import sys

FIELD_LABELS = {
    "overseas": "隔夜外盘",
    "research": "研报拆解",
    "limitup": "连板梯队",
    "margin": "两融余额",
}
REQUIRED_META = ("source", "as_of", "available")


def snapshot_path(trade_date):
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        f"review_context_{trade_date}.json")


def load_context(trade_date):
    """返回 (ok, ctx, reasons)。判据单一实现：gen_report_top200 调用本函数。
    ok=True 仅表示『快照本身可用』；具体字段是否展示由 field_available 决定。"""
    p = snapshot_path(trade_date)
    if not os.path.exists(p):
        return False, None, [f"快照不存在: {os.path.basename(p)}"]
    try:
        with open(p, encoding="utf-8") as f:
            ctx = json.load(f)
    except Exception as e:
        return False, None, [f"快照解析失败: {e}"]
    reasons = []
    if ctx.get("schema") != "review_context_v1":
        reasons.append(f"schema 不符: {ctx.get('schema')}")
    if str(ctx.get("trade_date", ""))[:10] != trade_date:
        reasons.append(f"日期守卫: 快照 trade_date={ctx.get('trade_date')} != 报告 {trade_date}")
    if not isinstance(ctx.get("fields"), dict):
        reasons.append("fields 缺失或非对象")
    return (len(reasons) == 0), ctx, reasons


def field_available(ctx, field):
    """单字段可用性：快照整体可用 + 字段 available=true + 三要素齐全。"""
    if not isinstance(ctx, dict):
        return False
    f = (ctx.get("fields") or {}).get(field)
    if not isinstance(f, dict) or f.get("available") is not True:
        return False
    return all(k in f and f[k] not in (None, "") for k in REQUIRED_META)


def field_badge_meta(ctx, field):
    """返回 (source, as_of) 供页面来源徽标使用；不可用时返回 (None, None)。"""
    if not field_available(ctx, field):
        return None, None
    f = ctx["fields"][field]
    return f.get("source"), f.get("as_of")


def _summary(ctx):
    print(f"  schema      : {ctx.get('schema')}")
    print(f"  trade_date  : {ctx.get('trade_date')}")
    print(f"  captured_at : {ctx.get('captured_at')}")
    print(f"  capture_mode: {ctx.get('capture_mode')}")
    for k, lbl in FIELD_LABELS.items():
        f = (ctx.get("fields") or {}).get(k)
        if field_available(ctx, k):
            note = f.get("as_of_note", "")
            print(f"  [有源] {lbl:<8} as_of={f.get('as_of')}  source={f.get('source')}")
            if note:
                print(f"         └ {note}")
        else:
            print(f"  [未接入] {lbl:<7} (available≠true 或三要素缺失)")


GUIDE = """\n=== 每日复盘手工快照 · 取数指引 ===
1) 时间窗：A股收盘后（建议每交易日 15:30 之后）执行。
2) 在会话内调用已挂载 MCP，按下列映射取真实数据：
   - 隔夜外盘：Wind get_index_kline(windcode=DJI.GI / SPX.GI / IXIC.GI,
               period=10, begin_date=前一美股交易日, end_date=A股当日)。
               ⚠ 取『美股前一交易日收盘』(=A股当日盘前可见)，as_of 填该美股日期，符合PIT。
   - 研报拆解：通达信 wenda_report_query(name=标的, bdate/edate=YYYYMMDD,
               keywords=深度报告,业绩点评)。as_of 填研报发布日期(最近可得)。
   - 连板梯队：通达信 tdx_screener(message=连续涨停连板)。as_of 填 A股当日。
               晋级率无现成字段，不自算则不填。
   - 两融余额：westock data_fund_margin(code=sz300059 等, start/end)。
               ⚠ T+1披露，as_of 填数据日；个券样本禁外推为全市场。
3) 复制 review_context_<上一交易日>.json 为 review_context_<当日>.json，
   更新 trade_date / captured_at / 各字段 source, as_of, available, 数值。
   无数据的字段保持 available=false，禁编造数值。
4) 校验：python review_context.py check <当日>
5) 重生成报告：python gen_report_top200.py top200_scored_<当日>.json
   （报告经 load_context/field_available 判据自动读取当日快照；日期不符则整份回退"未接入"）
=== 判据铁律 ===
- trade_date 必须 == 报告数据日期；as_of 是数据真实时点，页面同时显示二者。
- available=false → 该字段不产出任何数值，显示"未接入"。
"""


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 0
    cmd = sys.argv[1]
    if cmd == "guide":
        print(GUIDE)
        return 0
    if cmd == "check":
        if len(sys.argv) < 3:
            print("用法: check <YYYY-MM-DD>")
            return 2
        d = sys.argv[2]
        ok, ctx, reasons = load_context(d)
        print(f"=== 快照校验 {d} ===")
        for r in reasons:
            print("  FAIL:", r)
        if ok:
            print("  PASS: 快照结构/日期守卫通过")
            _summary(ctx)
            return 0
        return 1
    print(f"未知命令: {cmd}")
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
