#!/usr/bin/env python3
# LinUCB contextual bandit for path selection with simple anomaly-aware filtering.

import argparse, time, math, random, requests
from collections import defaultdict

def api_base(host, port): return f"http://{host}:{port}/api/v1"

def get_hosts(base):
    r=requests.get(f"{base}/hosts",timeout=5); r.raise_for_status(); return r.json()
def get_ports(base):
    r=requests.get(f"{base}/stats/ports",timeout=5); r.raise_for_status(); return r.json()
def get_paths(base, src, dst, k):
    r=requests.get(f"{base}/paths", params={'src_mac':src,'dst_mac':dst,'k':k}, timeout=8); r.raise_for_status(); return r.json()
def post_route(base, src, dst, path_id=None, path=None, k=2):
    payload={'src_mac':src,'dst_mac':dst,'k':k}
    if path_id is not None: payload['path_id']=int(path_id)
    if path is not None: payload['path']=list(path)
    r=requests.post(f"{base}/actions/route", json=payload, timeout=8); r.raise_for_status(); return r.json()

def index_ports(snapshot):
    idx=defaultdict(dict)
    for p in snapshot:
        try: idx[int(p['dpid'])][int(p['port_no'])]=p
        except Exception: pass
    return idx

def path_features(hops, prev_idx, cur_idx, dt):
    if dt<=0: dt=1e-6
    tx0=rx0=e0=d0=0.0; tx1=rx1=e1=d1=0.0
    for h in hops:
        dpid=int(h['dpid']); outp=int(h['out_port'])
        p0=prev_idx.get(dpid,{}).get(outp); p1=cur_idx.get(dpid,{}).get(outp)
        if not p0 or not p1: continue
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
    def __init__(self,d,alpha=1.0):
        self.d=d; self.alpha=alpha
        self.A=defaultdict(lambda:[[1.0 if i==j else 0.0 for j in range(d)] for i in range(d)])
        self.b=defaultdict(lambda:[0.0]*d)
    def Ainv(self,A):
        n=self.d
        aug=[row[:] + [1.0 if i==j else 0.0 for j in range(n)] for i,row in enumerate(A)]
        for i in range(n):
            piv=aug[i][i] or 1e-6
            aug[i]=[v/piv for v in aug[i]]
            for k in range(n):
                if k==i: continue
                fac=aug[k][i]
                aug[k]=[aug[k][j]-fac*aug[i][j] for j in range(2*n)]
        return [row[n:] for row in aug]
    def mv(self,M,v): return [sum(M[i][j]*v[j] for j in range(self.d)) for i in range(self.d)]
    def quad(self,v,M): t=self.mv(M,v); return sum(v[i]*t[i] for i in range(self.d))
    def predict_ucb(self,i,x):
        A=self.A[i]; b=self.b[i]; Ai=self.Ainv(A)
        theta=self.mv(Ai,b); mu=sum(theta[j]*x[j] for j in range(self.d))
        bonus=self.alpha*math.sqrt(max(1e-12,self.quad(x,Ai)))
        return mu+bonus
    def update(self,i,x,r):
        A=self.A[i]; b=self.b[i]
        for p in range(self.d):
            for q in range(self.d): A[p][q]+=x[p]*x[q]
        for p in range(self.d): b[p]+=r*x[p]
        self.A[i]=A; self.b[i]=b

def reward_from_deltas(prev_idx, cur_idx, hops, dt):
    x,anom=path_features(hops, prev_idx, cur_idx, dt)
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
    prev_ports=get_ports(base); prev_idx=index_ports(prev_ports); prev_t=time.time()

    for t in range(args.trials):
        time.sleep(2.0)
        cur_ports=get_ports(base); cur_idx=index_ports(cur_ports); now=time.time(); dt=now-prev_t

        paths=get_paths(base,src,dst,args.k)
        if not paths: print('No paths; retrying...'); prev_t=now; continue

        arms=[]
        for i,p in enumerate(paths):
            x,anom=path_features(p.get('hops',[]), prev_idx, cur_idx, dt)
            if anom<=args.err_thresh: arms.append((i,x,p))
        if not arms:
            arms=[(i, path_features(p.get('hops',[]), prev_idx, cur_idx, dt)[0], p) for i,p in enumerate(paths)]

        if random.random()<args.epsilon: j=random.randrange(len(arms))
        else:
            scores=[lin.predict_ucb(i,x) for (i,x,_) in arms]
            j=max(range(len(arms)), key=lambda k:scores[k])
        pid,x,chosen=arms[j]
        print(f"[t={t}] choose path_id={pid} dpids={chosen.get('dpids')}")
        try: post_route(base,src,dst,path_id=pid,k=args.k)
        except Exception as e: print("post_route failed:", e)

        time.sleep(3.0)
        new_ports=get_ports(base); new_idx=index_ports(new_ports)
        r=reward_from_deltas(prev_idx,new_idx,chosen.get('hops',[]),dt=3.0)
        lin.update(pid,x,r); print(f"  reward≈{r:.2f}")

        prev_idx=new_idx; prev_t=time.time()
    print("LinUCB finished.")

if __name__=='__main__': main()
