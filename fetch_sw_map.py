#!/usr/bin/env python3
"""更新申万一/二级行业成分映射 → sector/sw_map.json

数据来源: akshare index_component_sw (申万官网口径)
用法: python3 fetch_sw_map.py
建议: 每周更新一次即可(成分变动不频繁)
"""
import os
import sys
import json
import time

for k in ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY', 'all_proxy']:
    os.environ.pop(k, None)

import akshare as ak

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'sector', 'sw_map.json')


def main():
    print("拉取申万一级/二级行业列表...", flush=True)
    l1 = ak.sw_index_first_info()
    l2 = ak.sw_index_second_info()
    l1_codes = {str(r['行业代码']).replace('.SI', ''): r['行业名称'] for _, r in l1.iterrows()}
    l2_codes, l2_parent = {}, {}
    for _, r in l2.iterrows():
        c = str(r['行业代码']).replace('.SI', '')
        l2_codes[c] = r['行业名称']
        l2_parent[r['行业名称']] = r['上级行业']
    print(f"  一级 {len(l1_codes)} 个, 二级 {len(l2_codes)} 个", flush=True)

    l1_members, l2_members = {}, {}
    for code, name in l1_codes.items():
        try:
            df = ak.index_component_sw(symbol=code)
            l1_members[name] = [str(x).zfill(6) for x in df['证券代码'].tolist()]
        except Exception as e:
            print(f"  [WARN] L1 {name}: {str(e)[:60]}", flush=True)
            l1_members[name] = []
    for i, (code, name) in enumerate(l2_codes.items(), 1):
        try:
            df = ak.index_component_sw(symbol=code)
            l2_members[name] = [str(x).zfill(6) for x in df['证券代码'].tolist()]
        except Exception as e:
            print(f"  [WARN] L2 {name}: {str(e)[:60]}", flush=True)
            l2_members[name] = []
        if i % 30 == 0:
            print(f"  二级 {i}/{len(l2_codes)}", flush=True)

    code2l1 = {c: n for n, cs in l1_members.items() for c in cs}
    code2l2 = {c: n for n, cs in l2_members.items() for c in cs}
    out = {'l1': code2l1, 'l2': code2l2, 'l1_members': l1_members, 'l2_members': l2_members,
           'l2_parent': l2_parent, 'fetched_at': time.strftime('%Y-%m-%d %H:%M:%S')}
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False)
    print(f"✅ 写入 {OUT}: 一级覆盖 {len(code2l1)} 只, 二级覆盖 {len(code2l2)} 只")
    return 0


if __name__ == '__main__':
    sys.exit(main())
