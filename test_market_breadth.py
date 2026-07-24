#!/usr/bin/env python3
"""大盘赚钱效应模块回归测试 (独立于网络, 仅验证评分逻辑)。

运行: python3 test_market_breadth.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import step2_stockscorer_v2 as m


def _mk(up, down, zt, dt, idx, median=None, amount=None, source="test"):
    return {
        "total": up + down, "up": up, "down": down, "flat": 0,
        "suspended": 0, "zt": zt, "dt": dt, "up5": 0, "up3": 0, "down5": 0,
        "avg_pct": None, "median_pct": median, "amount_yi": amount,
        "index": {"上证": idx, "深证": idx, "创业板": idx},
        "source": source, "active_rate": None,
    }


def assert_score(name, b, lo, hi):
    me = m.compute_money_effect(b)
    sc = me["score"]
    ok = lo <= sc <= hi
    print(f"  [{'✅' if ok else '❌'}] {name}: score={sc} phase={me['phase']} "
          f"(期望 {lo}-{hi})")
    return ok


def main():
    print("=== 大盘赚钱效应 评分逻辑回归 ===")
    all_ok = True
    # 强普涨
    all_ok &= assert_score("强普涨(4000/1000,150zt,+2.5%)",
                           _mk(4000, 1000, 150, 5, 2.5), 65, 100)
    # 偏暖
    all_ok &= assert_score("偏暖(3000/2000,80zt,+0.8%)",
                           _mk(3000, 2000, 80, 10, 0.8), 45, 65)
    # 中性
    all_ok &= assert_score("中性(2400/2500,50zt,-0.2%)",
                           _mk(2400, 2500, 50, 20, -0.2), 25, 50)
    # 偏弱
    all_ok &= assert_score("偏弱(1600/3300,30zt,-1.0%)",
                           _mk(1600, 3300, 30, 25, -1.0), 0, 30)
    # 冰点 (今日真实量级: 534/4632, 净涨停+19, 指数-1.6%)
    all_ok &= assert_score("冰点(534/4632,43zt,24dt,-1.6%)",
                           _mk(534, 4632, 43, 24, -1.6), 0, 20)

    # 数据缺失
    me = m.compute_money_effect(None)
    ok = (not me["available"]) and me["score"] is None
    print(f"  [{'✅' if ok else '❌'}] 数据缺失: available={me['available']} score={me['score']}")
    all_ok &= ok

    # 分项校验: 强普涨时 c_breadth 应为满分区间
    me = m.compute_money_effect(_mk(4000, 1000, 150, 5, 2.5))
    cb = me["components"].get("广度(涨跌家数占比)", 0)
    cok = cb >= 35
    print(f"  [{'✅' if cok else '❌'}] 强普涨广度分项={cb} (期望>=35)")
    all_ok &= cok

    print("=== 结果:", "全部通过 ✅" if all_ok else "存在失败 ❌", "===")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
