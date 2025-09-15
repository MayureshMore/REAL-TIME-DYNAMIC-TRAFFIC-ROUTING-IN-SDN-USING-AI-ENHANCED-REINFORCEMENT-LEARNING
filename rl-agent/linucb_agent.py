#!/usr/bin/env python3
# LinUCB contextual bandit for path selection with simple anomaly-aware filtering.

import argparse, time, math, random, requests
from collections import defaultdict

def api_base(host, port): return f"http://{host}:{port}/api/v1"
def get_hosts(base): r=requests.get(f"{base}/hosts",timeout=5); r.raise_for_status(); return r.json()
def get_ports(base): r=requests.get(f"{base}/stats/ports",timeout=5); r.raise_for_status(); return r.json()
def get_paths(base, src, dst, k):
    r=requests.get(f"{base}/paths", params={'src_mac':src,'dst_mac':dst,'k':k}, timeout=8); r.raise_for_status(); return r.json()
def post_route(base, src, dst, path_id=None, path=None, k=2):
    payload={'src_mac':src,'dst_mac':dst,'k':k}
    if path_id is not None: payload['path_id']=path_id
    if path is not None: payload['path']=path
    r=requests.post(f"{base}/actions/route", json=payload, timeout=8); r.raise_for_status(); return r.json()

def index_ports(snap):
    idx=defaultdict(dict)
    for p in snap: idx[p['dpid']][p['port_no']]=p
    return idx

def path_features(hops, prev, cur, dt):
    if dt<=0: dt=1e-6
    tx0=rx0=e0=d0=0; tx1=rx1=e1=d1=0
    for h in hops:
        p0=prev.get(h['dpid'],{}).get(h['out_port'])
        p1=cur.get(h['dpid'],{}).get(h['out_port'])
        if p0 and p1:
            tx0+=p0.get('tx_bytes',0); tx1+=p1.get('tx_bytes',0)
            rx0+=p0.get('rx_bytes',0); rx1+=p1.get('rx_bytes',0)
            e0+=p0.get('rx_errors',0)+p0.get('tx_errors',0)
            e1+=p1.get('rx_errors',0)+p1.get('tx_errors',0)
            d0+=p0.get('rx_dropped',0)+p0.get('tx_dropped',0)
            d1+=p1.get('rx_dropped',0)+p1.get('tx_dropped',0)
    tx_bps=max(0.0,(tx1-tx0)*8.0/dt); rx_bps=max(0.0,(rx1-rx0)*8.0/dt)
    err_rate=max(0.0,(e1-e0)/dt); drop_rate=max(0.0,(d1-d0)/dt)
    x=[tx_bps, rx_bps, err_rate, drop_rate, float(len(hops)), 1.0]
    anomaly=err_rate+drop_rate
    return x, anomaly

class LinUCB:
    def __init__(s,d,alpha=1.0): s.d=d; s.alpha=alpha; s.A=defaultdict(lambda:s.I()); s.b=defaultdict(lambda:[0.0]*d)
    def I(s): I=[[0.0]*s.d for _ in range(s.d)]; [I.__setitem__(i, I[i][:i]+[1.0]+I[i][i+1:]) for i in range(s.d)]; return I
    def Ainv(s,A):
        n=s.d; aug=[row[:] + [1.0 if i==j else 0.0 for j in range(n)] for i,row in enumerate(A)]
        for i in range(n):
            piv=aug[i][i] if abs(aug[i][i])>1e-12 else 1e-6
            aug[i]=[v/piv for v in aug[i]]
            for k in range(n):
                if k==i: continue
                fac=aug[k][i]; aug[k]=[aug[k][j]-fac*aug[i][j] for j in range(2*n)]
        return [row[n:] for row in aug]
    def mv(s,M,v): return [sum(M[i][j]*v[j] for j in range(s.d)) for i in range(s.d)]
    def quad(s,v,M): t=s.mv(M,v); return sum(v[i]*t[i] for i in range(s.d))
    def predict_ucb(s,i,x):
        A=s.A[i]; b=s.b[i]; Ai=s.Ainv(A); th=s.mv(Ai,b)
        mu=sum(th[j]*x[j] for j in range(s.d))
        bonus=s.alpha*math.sqrt(max(1e-12,s.quad(x,Ai))); return mu+bonus
    def update(s,i,x,r):
        A=s.A[i]
        for p in range(s.d):
            for q in range(s.d): A[p][q]+=x[p]*x[q]
        b=s.b[i]
        for p in range(s.d): b[p]+=r*x[p]
        s.A[i]=A; s.b[i]=b

def reward_from_deltas(prev, cur, hops, dt):
    if dt<=0: dt=1e-6
    x,anom=path_features(hops, prev, cur, dt)
    return x[0] - 8000.0*anom

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--controller',default='127.0.0.1'); ap.add_argument('--port',type=int,default=8080)
    ap.add_argument('--k',type=int,default=2); ap.add_argument('--epsilon',type=float,default=0.1)
    ap.add_argument('--alpha',type=float,default=1.0); ap.add_argument('--trials',type=int,default=10)
    ap.add_argument('--err_thresh',type=float,default=5.0)
    args=ap.parse_args()

    base=api_base(args.controller,args.port)
    hosts=get_hosts(base)
    if len(hosts)<2: raise SystemExit('Need at least 2 hosts learned (run pingall)')
    src,dst=hosts[0]['mac'],hosts[1]['mac']

    lin=LinUCB(d=6,alpha=args.alpha)
    prev_ports=get_ports(base); prev_idx=index_ports(prev_ports={p['dpid']:{p['port_no']:p} for p in prev_ports})
    prev_t=time.time()

    for t in range(args.trials):
        time.sleep(2.0)
        cur_ports=get_ports(base); cur_idx=index_ports(cur_ports={p['dpid']:{p['port_no']:p} for p in cur_ports})
        now=time.time(); dt=now-prev_t

        paths=get_paths(base,src,dst,args.k)
        if not paths: print('No paths; retrying...'); prev_t=now; continue

        arms=[]
        for i,p in enumerate(paths):
            x,anom=path_features(p['hops'], prev_idx, cur_idx, dt)
            if anom<=args.err_thresh: arms.append((i,x,p))
        if not arms: arms=[(i, path_features(p['hops'], prev_idx, cur_idx, dt)[0], p) for i,p in enumerate(paths)]

        if random.random()<args.epsilon: j=random.randrange(len(arms))
        else:
            scores=[lin.predict_ucb(i,x) for (i,x,_) in arms]; j=max(range(len(arms)), key=lambda k:scores[k])
        pid,x,chosen=arms[j]
        print(f"[t={t}] choose path_id={pid} dpids={chosen['dpids']}"); post_route(base,src,dst,path_id=pid,k=args.k)

        time.sleep(3.0)
        new_ports=get_ports(base); new_idx=index_ports(new_ports={p['dpid']:{p['port_no']:p} for p in new_ports})
        r=reward_from_deltas(prev_idx,new_idx,chosen['hops'],dt=3.0); lin.update(pid,x,r)
        print(f"  reward≈{r:.2f}")

        prev_idx=new_idx; prev_t=time.time()
    print("LinUCB finished.")

if __name__=='__main__': main()
