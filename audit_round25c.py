#!/usr/bin/env python3
"""round25c：对 v4.8 的**核心质疑**——低波的提升是不是只是「变相加权反转块」。

## 为什么必须验

round23 q2 drop_one 显示：v4.7 基线（h5 IC +0.1137）去掉**任意一维**后 IC 都升高，
只有去 bias60（+0.0946）/ rsi_rev（+0.1101）才下降。即 7 维中真正贡献 alpha 的
只有反转块（bias60+rsi_rev 合计 35% 权重），其余是净损害或中性。

而 v4.8 用 lowvol20 顶掉 growth，h5 IC 从 +0.1153 涨到 +0.1776。若 lowvol20
与反转块同源（低波 = 弱动量 = 超跌），那这个"改善"就只是**变相加权反转块**，
不是新增独立信息 —— 这决定了 v4.8 该不该被当成"加了新因子"。

Q1 控制反转块后 lowvol 的残差 IC（偏相关）
Q2 反转块 + lowvol 同时放大的方案 vs 仅放大反转块
Q3 纯反转双维基准（bias60/rsi_rev 各半）
"""
import json, math, os, sys
from collections import defaultdict
HERE=os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0,HERE)
import factor_eval as fe, audit_round21 as r21, audit_round24b as rb

DMAX={"score_fund":5.0,"score_quality":10.0,"score_growth":10.0,"score_tech_neg":10.0,
      "score_bias60":5.0,"score_rsi_rev":5.0,"score_size":10.0,"score_lowvol20":10.0}
T_BONF=3.08

def _rank(v):
    idx=sorted(range(len(v)),key=lambda i:v[i]); r=[0.0]*len(v); i=0
    while i<len(idx):
        j=i
        while j+1<len(idx) and v[idx[j+1]]==v[idx[i]]: j+=1
        a=(i+j)/2.0+1
        for k in range(i,j+1): r[idx[k]]=a
        i=j+1
    return r
def spear(xs,ys):
    n=len(xs)
    if n<5: return None
    rx,ry=_rank(xs),_rank(ys); mx,my=sum(rx)/n,sum(ry)/n
    sxy=sum((rx[i]-mx)*(ry[i]-my) for i in range(n))
    sxx=sum((rx[i]-mx)**2 for i in range(n))**.5; syy=sum((ry[i]-my)**2 for i in range(n))**.5
    return sxy/(sxx*syy) if sxx>0 and syy>0 else None
def nw_t(xs,lag):
    n=len(xs)
    if n<5: return None
    mu=sum(xs)/n; d=[x-mu for x in xs]; var=sum(x*x for x in d)/n
    for L in range(1,min(lag,n-1)+1):
        gl=sum(d[i]*d[i-L] for i in range(L,n))/n; var+=2*(1-L/(lag+1))*gl
    se=math.sqrt(var/n) if var>0 else None
    return mu/se if se else None
def resid(xs,ys):
    """把 ys 对 xs 做线性回归取残差"""
    n=len(xs); mx=sum(xs)/n; my=sum(ys)/n
    sxx=sum((x-mx)**2 for x in xs)
    if sxx<=0: return [0.0]*n
    b=sum((xs[i]-mx)*(ys[i]-my) for i in range(n))/sxx
    return [ys[i]-(my+b*(xs[i]-mx)) for i in range(n)]

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
    return (sum((x-mu)**2 for x in rt)/(n-1))**.5
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
def dv(d,r,d0):
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
def comp(r,wm,d0):
    num=den=0.0
    for k,w in wm.items():
        if w<=0: continue
        v=dv(k,r,d0)
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

BASE5={"score_fund":0.15,"score_quality":0.12,"score_tech_neg":0.15,"score_bias60":0.175,"score_rsi_rev":0.175}
W48={**BASE5,"score_lowvol20":0.13,"score_size":0.10}

print('='*96); print('round25c：低波的提升是不是「变相加权反转块」？'); print('='*96)

# Q1：控制反转块后 lowvol 的残差 IC
print()
print('─'*96); print('Q1 控制反转块后 lowvol20 的残差 IC（逐截面 OLS 取残差）'); print('─'*96)
for hop in (5,20):
    raw_ics=[]; res_ics=[]; cors=[]
    for s in snaps:
        d0=s['date']; arr=[]
        for r in s['rows']:
            lv=dv('score_lowvol20',r,d0); b=dv('score_bias60',r,d0); rr=dv('score_rsi_rev',r,d0)
            fr=fret(r['code'],d0,hop)
            if None in (lv,b,rr,fr): continue
            arr.append((lv,b,rr,fr))
        if len(arr)<40: continue
        lv=[a[0] for a in arr]; rev=[(a[1]+a[2])/2.0 for a in arr]; fr=[a[3] for a in arr]
        i1=spear(lv,fr); i2=spear(resid(rev,lv),fr); c=spear(lv,rev)
        if None not in (i1,i2,c): raw_ics.append(i1); res_ics.append(i2); cors.append(c)
    if len(raw_ics)<10: continue
    m1=sum(raw_ics)/len(raw_ics); m2=sum(res_ics)/len(res_ics); mc=sum(cors)/len(cors)
    print(f"  h{hop}: 原始 lowvol IC {m1:+.4f} (t0={nw_t(raw_ics,0):+.2f}) → "
          f"控制反转块后残差 IC {m2:+.4f} (t0={nw_t(res_ics,0):+.2f})  "
          f"|  lowvol 与反转块平均秩相关 {mc:+.3f}  n={len(raw_ics)}")
    print(f"        → 残差保留 {m2/m1*100 if m1 else 0:.0f}% 的原始 IC")

# Q2/Q3：方案对比
print()
print('─'*96); print('Q2/Q3 若只是变相加权反转块，那「纯放反转块」应能达到同样效果'); print('─'*96)
# ⚠️ 首版 B 方案把 lowvol 0.13 原样保留，权重与 A 完全相同，
#    跑出「数字一模一样」的假对照。已修正：B 把 lowvol 的 0.13 平摊给
#    bias60/rsi_rev（各 +0.065），总权重仍为 1.00，才是真正的「纯放反转块」。
CASES={
 'A v4.8 现行 7维(低波.13+size.10)':W48,
 'B 同总权重但去低波，0.13全给反转块':{'score_fund':0.15,'score_quality':0.12,
        'score_tech_neg':0.15,'score_size':0.10,
        'score_bias60':0.175+0.065,'score_rsi_rev':0.175+0.065},
 'C 仅反转双维等权(bias60/rsi_rev各半)':{'score_bias60':0.5,'score_rsi_rev':0.5},
 'D 反转块+低波 三项(去fund/质量/tech_neg/size)':{'score_bias60':0.30,'score_rsi_rev':0.30,'score_lowvol20':0.40},
 'E 反转块双维 + 低波 三等权':{'score_bias60':1/3,'score_rsi_rev':1/3,'score_lowvol20':1/3},
 'F 仅低波(归一)':{'score_lowvol20':1.0},
 'G 反转块+低波+fund 三项':{'score_bias60':0.30,'score_rsi_rev':0.30,'score_lowvol20':0.20,'score_fund':0.20},
 'H 反转块+低波+fund+size(去质量/tech_neg)':{'score_bias60':0.25,'score_rsi_rev':0.25,
        'score_lowvol20':0.20,'score_fund':0.20,'score_size':0.10},
 'I 仅bias60单维':{'score_bias60':1.0},
 'J 仅rsi_rev单维':{'score_rsi_rev':1.0},
}
res={}
for hop in (5,20):
    print(f"\n【h{hop}】")
    for nm,wm in CASES.items():
        ics=[]
        for s in snaps:
            d0=s['date']; pr=[]
            for r in s['rows']:
                c=comp(r,wm,d0); fr=fret(r['code'],d0,hop)
                if c is not None and fr is not None: pr.append((c,fr))
            if len(pr)>=40:
                ic=spear([x[0] for x in pr],[x[1] for x in pr])
                if ic is not None: ics.append(ic)
        if len(ics)<10: continue
        m=sum(ics)/len(ics); t0=nw_t(ics,0); t3=nw_t(ics,3); t6=nw_t(ics,6)
        rob=all((x or -9)>=T_BONF for x in (t0,t3,t6))
        k=len(ics)//2; h1=sum(ics[:k])/k; h2=sum(ics[k:])/(len(ics)-k)
        res[f'h{hop}|{nm}']={'n':len(ics),'ic':round(m,4),
            't_by_lag':{'0':round(t0,2) if t0 else None,'3':round(t3,2) if t3 else None,'6':round(t6,2) if t6 else None},
            'robust_bonf':rob,'sub_first':round(h1,4),'sub_second':round(h2,4)}
        print(f"  {nm:<38} n={len(ics):>3} IC {m:+.4f} t0={t0:+.2f} t3={t3:+.2f} t6={t6:+.2f} "
              f"{'✅' if rob else '⚠️'} 前半{h1:+.4f}/后半{h2:+.4f}")

json.dump({'note':'验证 v4.8 提升是否只是变相加权反转块','cases':res},
          open('audit_round25c.json','w',encoding='utf-8'),ensure_ascii=False,indent=1)
print('\n已写出 audit_round25c.json')
