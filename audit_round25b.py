#!/usr/bin/env python3
"""round25b：按**实际持有周期**重算换手率与净值曲线（round25 的 Q3 只算了逐日换手）。
Q3 原口径：相邻截面 Top50 重叠 64.5% → 单期换手 35.5%，但截面是**逐日**的
（78 截面 / 06-26~10-23，gap 中位 1 交易日）。若每日换 35.5%，年化换手超
100 倍，任何 alpha 都会被成本吃掉。所以必须按「持 N 日不换」的口径重算：
H3/H5/H10/H20 各一档，取重叠度与净值。
"""
import json, math, os, sys
from collections import defaultdict
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import factor_eval as fe, audit_round21 as r21, audit_round24b as rb

DMAX = {"score_fund":5.0,"score_quality":10.0,"score_growth":10.0,"score_tech_neg":10.0,
        "score_bias60":5.0,"score_rsi_rev":5.0,"score_size":10.0,"score_lowvol20":10.0}
W = {"score_fund":0.15,"score_quality":0.12,"score_lowvol20":0.13,"score_tech_neg":0.15,
     "score_bias60":0.175,"score_rsi_rev":0.175,"score_size":0.10}

h=json.load(open('factor_history.json',encoding='utf-8'))
snaps,_=fe._drop_stale_price_snapshots(h['snapshots'])
tdays,px=fe.px_index(h); tidx={d:i for i,d in enumerate(tdays)}
em=r21.load_em_cache()
subs=defaultdict(dict)
for s in snaps:
    for r in s['rows']:
        ind=em.get(r.get('code'))
        if not ind: continue
        P=r21.pit_period(sorted({p for dd in ind.values() for p in dd},reverse=True),s['date'])
        if P: subs[s['date']][r['code']]=r21.sub_factors_pit(ind,P)
bf=rb.backfill_reversal(tdays,px)
shares=defaultdict(list)
for s in snaps:
    for r in s['rows']:
        mv,pr=r.get('circ_mv'),r.get('price')
        if isinstance(mv,(int,float)) and mv and isinstance(pr,(int,float)) and pr>0: shares[r['code']].append(mv/pr)
se={c:(lambda v:v[len(v)//2])(sorted(v)) for c,v in shares.items() if v}
size_by_date={}
for s in snaps:
    mv={}
    for r in s['rows']:
        c=r['code']; v=r.get('circ_mv')
        if not (isinstance(v,(int,float)) and v):
            sh=se.get(c); pr=r.get('price')
            if sh and isinstance(pr,(int,float)) and pr>0: v=sh*pr
        if isinstance(v,(int,float)) and v>0: mv[c]=float(v)
    ks=sorted(mv,key=lambda c:-mv[c]); n=len(ks)
    size_by_date[s['date']]={c:round((i/(n-1))*10.0,2) for i,c in enumerate(ks)} if n>1 else {}
def lvraw(code,d0):
    m=px.get(code)
    if not m or d0 not in tidx: return None
    i0=tidx[d0]
    if i0<20: return None
    cl=[]
    for j in range(i0-20,i0+1):
        v=m.get(tdays[j])
        if v is None: return None
        cl.append(v)
    rt=[cl[k]/cl[k-1]-1.0 for k in range(1,len(cl))]; n=len(rt); mu=sum(rt)/n
    return (sum((x-mu)**2 for x in rt)/(n-1))**0.5
lv_by_date={}
for s in snaps:
    raw={}
    for r in s['rows']:
        v=lvraw(r['code'],s['date'])
        if v is not None: raw[r['code']]=v
    ks=sorted(raw,key=lambda c:raw[c]); n=len(ks)
    lv_by_date[s['date']]={c:round((1.0-i/(n-1))*10.0,2) for i,c in enumerate(ks)} if n>1 else {}
def r10(vb):
    ks=sorted(vb,key=lambda c:vb[c]); n=len(ks)
    return {c:round((i/(n-1))*10.0,2) for i,c in enumerate(ks)} if n>1 else {}
qby={};gby={}
for s in snaps:
    d0=s['date']; q={}; g={}
    for r in s['rows']:
        sub=subs.get(d0,{}).get(r['code'])
        if not sub: continue
        a=r21.aggregate(sub,'grouped'); b=r21.aggregate(sub,'g6040')
        if isinstance(a,(int,float)): q[r['code']]=a
        if isinstance(b,(int,float)): g[r['code']]=b
    qby[d0]=r10(q); gby[d0]=r10(g)
def dim_val(d,r,d0):
    if d=='score_fund':
        v=r.get('score_fund'); return v if isinstance(v,(int,float)) and not isinstance(v,bool) else None
    if d=='score_tech_neg':
        v=r.get('score_tech'); return (10.0-v) if isinstance(v,(int,float)) else None
    if d in ('score_bias60','score_rsi_rev'):
        t=(bf.get(d0) or {}).get(r.get('code'))
        return None if not t else (t[1] if d=='score_bias60' else t[2])
    if d=='score_quality': return qby.get(d0,{}).get(r.get('code'))
    if d=='score_growth': return gby.get(d0,{}).get(r.get('code'))
    if d=='score_size': return size_by_date.get(d0,{}).get(r.get('code'))
    if d=='score_lowvol20': return lv_by_date.get(d0,{}).get(r.get('code'))
    return None
def comp(r,d0):
    num=den=0.0
    for k,w in W.items():
        if w<=0: continue
        v=dim_val(k,r,d0)
        if v is None: continue
        mx=DMAX.get(k,10.0); num+=min(float(v),mx)/mx*10.0*w; den+=w
    return (num/max(0.5,den)*10.0) if den>0 else None
def fret(code,d0,hop):
    m=px.get(code)
    if not m or d0 not in tidx: return None
    i0=tidx[d0]
    if i0+hop>=len(tdays): return None
    a,b=m.get(tdays[i0]),m.get(tdays[i0+hop])
    return None if (a is None or b is None or a<=0) else b/a-1.0

print('='*96); print('round25b：按实际持有周期重算换手 + 净值（截面逐日，占比必须按 H 折算）'); print('='*96)
res={}
for H in (1,3,5,10,20):
    # 每 H 个交易日取样一次（模拟「买入后持 H 日不动」）
    picks=[]
    for i in range(0,len(snaps),H):
        s=snaps[i]; d0=s['date']
        arr=[(comp(r,d0),r['code']) for r in s['rows']]
        arr=[x for x in arr if x[0] is not None]
        if len(arr)<50: continue
        arr.sort(key=lambda x:-x[0])
        picks.append((d0,[c for _,c in arr[:50]]))
    ov=[]
    for i in range(1,len(picks)):
        a=set(picks[i-1][1]); b=set(picks[i][1])
        ov.append(len(a&b)/50)
    mo=sum(ov)/len(ov) if ov else None
    # 等权持有 H 日的组合 vs 池等权，逐期
    exc=[]; nav=1.0; navb=1.0
    for d0,codes in picks:
        rs=[fret(c,d0,H) for c in codes]; rs=[x for x in rs if x is not None]
        allr=[fret(r['code'],d0,H) for r in next(s2 for s2 in snaps if s2['date']==d0)['rows']]
        allr=[x for x in allr if x is not None]
        if len(rs)<20 or len(allr)<40: continue
        mr=sum(rs)/len(rs); mb=sum(allr)/len(allr)
        exc.append(mr-mb); nav*=(1+mr); navb*=(1+mb)
    if not exc: continue
    mu=sum(exc)/len(exc)
    res[f'H{H}']={'n_periods':len(exc),'overlap':(round(mo,4) if mo is not None else None),
                  'period_turnover':(round(1-mo,4) if mo is not None else None),
                  'annual_turnover_est':(round((1-mo)*244/H,2) if mo is not None else None),
                  'mean_exc':round(mu,4),
                  'win_rate':round(sum(1 for x in exc if x>0)/len(exc),3),
                  'nav':round(nav,4),'nav_bench':round(navb,4),
                  'nav_excess':round(nav-navb,4)}
    r_=res[f'H{H}']
    print(f"  H{H:<3} 期数{r_['n_periods']:>3}  重叠{r_['overlap']:>6.1%} 单期换手{r_['period_turnover']:>6.1%} "
          f"年化换手{r_['annual_turnover_est']:>7.1f}x  单期超额{r_['mean_exc']:>+7.4f} 胜率{r_['win_rate']:>5.1%} "
          f"净值{r_['nav']:>6.3f} vs 基准{r_['nav_bench']:>6.3f}")
out={'note':'截面逐日(gap中位1交易日)，H=持有交易日','results':res}
json.dump(out,open('audit_round25b.json','w',encoding='utf-8'),ensure_ascii=False,indent=1)
print('已写出 audit_round25b.json')
