#!/usr/bin/env python3
"""
阶段 2 —— 逐公司资料汇总与校验器。

在「本地大模型逐公司查阅资料」阶段（阶段 1）完成后运行：
  输入：
    - review_input_<date>.json      阶段 0 产出的候选清单（作为「应查名单」基准）
    - review_research_<date>.json  阶段 1 产出的逐公司资料（本地大模型填写）
  输出：
    - review_research_merged_<date>.json   校验通过的结构化汇总（供阶段 3 生成 ai_assessment）
    - review_research_issues_<date>.md     校验问题清单（人读）

校验项（数据纪律硬约束，防止编造与张冠李戴）：
  E1 date 一致性             —— research.date 必须等于 input.date
  E2 深度档覆盖完整性         —— deep 档候选必须全部有资料
  E3 代码越界                 —— research 里的 code 必须都在候选清单内
  E4 名称一致性               —— code → name 必须与评分产物一致（防张冠李戴）
  E5 必填字段                 —— 每家公司需有 verdict / findings
  E6 来源可核验（deep 档）    —— 至少 1 条来源且含 http(s) URL
  E7 行情数值一致性           —— research 若给出 price/pct_chg，须与评分产物一致（容差 0.5%）
  E8 verdict 枚举             —— 取值须在允许集合内
  E9 findings 长度下限        —— deep ≥ 80 字、light ≥ 30 字（防敷衍）

用法：
  python3 merge_review_research.py                       # 自动取当日最新
  python3 merge_review_research.py --input X --research Y
  python3 merge_review_research.py --emit-template       # 生成待填模板
  python3 merge_review_research.py --force               # 有 error 也强行出汇总（仅调试用）
"""
import json
import os
import sys
import glob
import argparse
from collections import Counter
from datetime import datetime

VERDICTS = ("维持", "上调", "下调", "规避", "观察", "中性")
SEV_ERROR, SEV_WARN = "error", "warn"


def _find_latest(here, pattern):
    fs = [f for f in sorted(glob.glob(os.path.join(here, pattern)), reverse=True)
          if ".bak" not in f]
    return fs[0] if fs else None


def _find_latest_research(here):
    """资料文件专用的查找器。
    ⚠️ 必须排除 .template.json —— 模板里 verdict/findings 全为空,
       且 't' > '.' 会让它在 reverse 排序中排在真实资料之前, 被误选为输入。
    """
    fs = [f for f in sorted(glob.glob(os.path.join(here, "review_research_*.json")), reverse=True)
          if ".bak" not in f and ".template." not in f and ".merged." not in f]
    return fs[0] if fs else None


def _load(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _close(a, b, tol=0.005):
    """相对容差比对（默认 0.5%）。"""
    if a is None or b is None:
        return True          # 缺值不判错（由其他规则覆盖）
    if b == 0:
        return abs(a) < 1e-9
    return abs(a - b) / abs(b) <= tol


# ---------- 模板生成（供阶段 1 填写）----------
def emit_template(pkg):
    comps = []
    for c in pkg["candidates"]:
        comps.append({
            "code": c["code"],
            "name": c["name"],
            "tier": c.get("tier"),
            "rating_alg": c.get("rating"),
            "pct_chg_alg": c.get("pct_chg"),
            "price_alg": c.get("price"),
            "sector": c.get("sector_l2") or c.get("sector"),
            "focus": c.get("focus"),
            # ↓↓↓ 阶段 1 需填写以下字段 ↓↓↓
            "verdict": "",             # 维持 / 上调 / 下调 / 规避 / 观察 / 中性
            "confidence": "",          # 高 / 中 / 低
            "findings": "",            # deep≥80字 / light≥30字：查阅到的核心事实
            "catalysts": [],           # 催化（有则填）
            "risks": [],               # 风险（有则填）
            "sources": [],             # [{"title":"","url":"","date":""}]  deep 档至少 1 条
            "target_price": None,      # 机构目标价（查不到填 null，勿编造）
            "rating_note": "",         # 对算法评级的验证或质疑
            "price": None,             # 如需参与数值校验，填当日收盘价
            "pct_chg": None,           # 如需参与数值校验，填当日涨跌幅(%)
        })
    return {
        "date": pkg["date"],
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "researcher": "WorkBuddy（本地复盘）",
        "companies": comps,
        "sector_notes": [],            # [{"sector":"","note":""}]
        "market_commentary": "",       # 全局复盘结论文本（300-500字）
        "watch_directions": [],        # 5-6 条明日关注方向
    }


# ---------- 校验 ----------
def validate(pkg, res):
    issues = []   # (severity, code, msg)

    def err(code, msg):
        issues.append((SEV_ERROR, code, msg))

    def warn(code, msg):
        issues.append((SEV_WARN, code, msg))

    inp_date = pkg.get("date")
    res_date = res.get("date")
    if str(res_date) != str(inp_date):
        err("E1", f"date 不一致：候选清单={inp_date}，资料={res_date}")

    cand = {c["code"]: c for c in pkg["candidates"]}
    deep_codes = {c["code"] for c in pkg["candidates"] if c.get("tier") == "deep"}

    comps = res.get("companies") or []
    if not comps:
        err("E5", "companies 为空，未提供任何公司资料")
        return issues, {}

    seen = Counter()
    for c in comps:
        code = str(c.get("code") or "").strip()
        name = c.get("name")
        if not code:
            err("E3", f"存在无 code 的记录（name={name}）")
            continue
        seen[code] += 1

        if code not in cand:
            err("E3", f"[{code} {name}] 不在当日候选清单内（越界/编造代码）")
            continue

        # E4 名称一致性
        exp = cand[code]["name"]
        if name and exp and str(name).strip() != str(exp).strip():
            err("E4", f"[{code}] 名称不一致：资料={name}，评分产物={exp}（疑似张冠李戴）")

        tier = cand[code].get("tier")
        # E5 必填
        verdict = (c.get("verdict") or "").strip()
        findings = (c.get("findings") or "").strip()
        if not verdict:
            err("E5", f"[{code} {exp}] 缺 verdict（复盘结论）")
        if not findings:
            err("E5", f"[{code} {exp}] 缺 findings（查阅到的核心事实）")

        # E8 verdict 枚举
        if verdict and verdict not in VERDICTS:
            warn("E8", f"[{code} {exp}] verdict='{verdict}' 不在允许集合 {list(VERDICTS)}，将按『观察』处理")

        # E9 长度下限
        _min = 80 if tier == "deep" else 30
        if findings and len(findings) < _min:
            warn("E9", f"[{code} {exp}] findings 仅 {len(findings)} 字（{tier} 档建议 ≥{_min} 字）")

        # E6 来源可核验
        srcs = c.get("sources") or []
        bad_url = [s for s in srcs if not str((s or {}).get("url", "")).startswith("http")]
        if tier == "deep":
            if not srcs:
                err("E6", f"[{code} {exp}] deep 档无来源，无法核验")
            elif len(srcs) == len(bad_url):
                err("E6", f"[{code} {exp}] deep 档来源均无有效 http(s) URL")
        if bad_url:
            warn("E6", f"[{code} {exp}] {len(bad_url)} 条来源缺 URL")

        # E7 行情数值一致性
        for fld, key in (("price", "price"), ("pct_chg", "pct_chg")):
            given = _num(c.get(fld))
            expect = _num(cand[code].get(key))
            if given is not None and expect is not None and not _close(given, expect):
                err("E7", f"[{code} {exp}] {fld} 与评分产物不符：资料={given}，产物={expect}")

    # E2 深度档覆盖
    res_codes = {str(c.get("code") or "") for c in comps}
    missing = sorted(deep_codes - res_codes)
    if missing:
        _names = [f"{cand[c]['name']}({c})" for c in missing]
        err("E2", f"deep 档有 {len(missing)} 只未提供资料：{', '.join(_names)}")

    # 重复 code
    dup = [f"{k}×{v}" for k, v in seen.items() if v > 1]
    if dup:
        warn("E3", f"存在重复 code：{', '.join(dup)}")

    # 轻量档缺失（仅提示）
    light_codes = {c["code"] for c in pkg["candidates"] if c.get("tier") == "light"}
    miss_light = sorted(light_codes - res_codes)
    if miss_light:
        warn("E2", f"light 档有 {len(miss_light)} 只未提供资料（不阻断，页面将回退算法结论）")

    return issues, seen


# ---------- 汇总 ----------
def build_merged(pkg, res, issues):
    cand = {c["code"]: c for c in pkg["candidates"]}
    merged = []
    for c in (res.get("companies") or []):
        code = str(c.get("code") or "").strip()
        base = cand.get(code)
        if base is None:
            continue
        verdict = (c.get("verdict") or "").strip()
        if verdict not in VERDICTS:
            verdict = "观察"
        merged.append({
            "code": code,
            "name": base["name"],
            "tier": base.get("tier"),
            "sources": base.get("sources"),
            "sector": base.get("sector_l2") or base.get("sector"),
            "rating_alg": base.get("rating"),
            "total_alg": base.get("total"),
            "pct_chg": base.get("pct_chg"),
            "price": base.get("price"),
            "verdict": verdict,
            "confidence": (c.get("confidence") or "").strip() or None,
            "findings": (c.get("findings") or "").strip(),
            "catalysts": c.get("catalysts") or [],
            "risks": c.get("risks") or [],
            "sources": c.get("sources") or [],
            "target_price": c.get("target_price"),
            "rating_note": (c.get("rating_note") or "").strip() or None,
        })
    merged.sort(key=lambda x: (0 if x["tier"] == "deep" else 1, -(x.get("total_alg") or 0)))

    sev_cnt = Counter(s for s, _, _ in issues)
    return {
        "date": pkg["date"],
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "generated_by": "merge_review_research.py (阶段2 校验汇总)",
        "researcher": res.get("researcher") or "WorkBuddy（本地复盘）",
        "source_input": pkg.get("source_scored"),
        "validation": {
            "n_error": sev_cnt.get(SEV_ERROR, 0),
            "n_warn": sev_cnt.get(SEV_WARN, 0),
            "passed": sev_cnt.get(SEV_ERROR, 0) == 0,
            "issues": [{"severity": s, "code": c, "message": m} for s, c, m in issues],
        },
        "market": pkg.get("market"),
        "sectors": pkg.get("sectors"),
        "themes": pkg.get("themes"),
        "companies": merged,
        "sector_notes": res.get("sector_notes") or [],
        "market_commentary": (res.get("market_commentary") or "").strip(),
        "watch_directions": res.get("watch_directions") or [],
    }


def write_issues_md(path, merged):
    v = merged["validation"]
    L = [f"# 复盘资料校验报告 · {merged['date']}", "",
         f"- 生成时间：{merged['generated_at']}",
         f"- 研究员：{merged['researcher']}",
         f"- 校验结果：**{'通过' if v['passed'] else '未通过'}**"
         f"（error {v['n_error']} / warn {v['n_warn']}）", ""]
    if not v["issues"]:
        L.append("✅ 未发现问题。")
    else:
        L.append("| 级别 | 规则 | 说明 |")
        L.append("|------|------|------|")
        for i in v["issues"]:
            L.append(f"| {'❌' if i['severity'] == 'error' else '⚠️'} {i['severity']} "
                     f"| {i['code']} | {i['message']} |")
    n_deep = sum(1 for c in merged["companies"] if c["tier"] == "deep")
    n_light = sum(1 for c in merged["companies"] if c["tier"] == "light")
    vc = Counter(c["verdict"] for c in merged["companies"])
    L += ["", "## 覆盖情况", "",
          f"- 深度档：{n_deep} 只",
          f"- 轻量档：{n_light} 只",
          f"- 结论分布：{dict(vc)}", ""]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", help="review_input_<date>.json（默认取最新）")
    ap.add_argument("--research", help="review_research_<date>.json（默认取最新）")
    ap.add_argument("--emit-template", action="store_true", help="生成待填模板")
    ap.add_argument("--force", action="store_true", help="有 error 也出汇总")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    in_path = args.input or _find_latest(here, "review_input_*.json")
    if not in_path or not os.path.exists(in_path):
        print("❌ 未找到 review_input_*.json，请先运行 export_review_input.py")
        return 1
    pkg = _load(in_path)
    print(f"📥 候选清单: {os.path.basename(in_path)}（{pkg['stats']['n_candidates']} 只）")

    if args.emit_template:
        tpl = emit_template(pkg)
        out = os.path.join(here, f"review_research_{pkg['date']}.template.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(tpl, f, ensure_ascii=False, indent=2)
        print(f"✅ 已生成填写模板: {out}")
        print(f"   deep {sum(1 for c in tpl['companies'] if c['tier']=='deep')} 只 / "
              f"light {sum(1 for c in tpl['companies'] if c['tier']=='light')} 只")
        return 0

    res_path = args.research or _find_latest_research(here)
    if not res_path or not os.path.exists(res_path):
        print("❌ 未找到 review_research_*.json（阶段1 逐公司资料）")
        print("   可先运行：python3 merge_review_research.py --emit-template")
        return 1
    res = _load(res_path)
    print(f"📥 逐公司资料: {os.path.basename(res_path)}（{len(res.get('companies') or [])} 条）")

    issues, _ = validate(pkg, res)
    for sev, code, msg in issues:
        print(f"   {'❌' if sev == SEV_ERROR else '⚠️'} [{code}] {msg}")

    merged = build_merged(pkg, res, issues)
    v = merged["validation"]
    if not v["passed"] and not args.force:
        print(f"\n❌ 校验未通过（error {v['n_error']}），已中止。修正后重跑，或加 --force 强制。")
        return 2

    out = os.path.join(here, f"review_research_merged_{merged['date']}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    md = os.path.join(here, f"review_research_issues_{merged['date']}.md")
    write_issues_md(md, merged)

    print(f"\n✅ 已汇总: {out}")
    print(f"   校验: {'通过' if v['passed'] else '未通过(forced)'} "
          f"error={v['n_error']} warn={v['n_warn']}")
    print(f"   公司资料: {len(merged['companies'])} 条 "
          f"(deep {sum(1 for c in merged['companies'] if c['tier']=='deep')} / "
          f"light {sum(1 for c in merged['companies'] if c['tier']=='light')})")
    print(f"   校验报告: {md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
