#!/usr/bin/env python3
# Unified SDN controller app: L2 learning + topology + routing + REST + stats

from collections import defaultdict
import hashlib, json, time, networkx as nx
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, DEAD_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ether_types
from ryu.lib import hub
from ryu.topology import event as topo_event
from ryu.app.wsgi import WSGIApplication, ControllerBase, route
from webob import Response
from jsonschema import validate, ValidationError

API_INSTANCE = 'sdn_router_api'

def j(obj, status=200, headers=None):
    resp = Response(content_type='application/json',
                    body=json.dumps(obj, default=str).encode('utf-8'),
                    status=status)
    if headers:
        for k, v in headers.items():
            resp.headers[k] = v
    return resp

ROUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "src_mac": {"type": "string"},
        "dst_mac": {"type": "string"},
        "k": {"type": "integer", "minimum": 1},
        "path_id": {"type": "integer", "minimum": 0},
        "path": {"type": "array", "items": {"type": "integer"}}
    },
    "required": ["src_mac", "dst_mac"],
    "anyOf": [{"required": ["path_id"]}, {"required": ["path"]}]
}

class SDNRouterREST(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS = {'wsgi': WSGIApplication}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mac_to_port = defaultdict(dict)
        self.hosts = {}
        self.datapaths = {}
        self.G = nx.DiGraph()
        self.core_ports = defaultdict(set)
        self.k_paths_cached = {}
        self.routes = {}
        self.last_action_ts = {}
        self.route_cooldown = 5.0

        # Stats
        self.port_stats = []
        self.port_prev = {}
        self.port_rates = []
        self.flow_stats = []
        self.last_stats_ts = 0.0
        self.cache_ttl = 2.0

        # Threads
        self.monitor_interval = 2
        self.monitor_thread = hub.spawn(self._monitor)
        self.sweep_thread = hub.spawn(self._sweep_core_leaks)

        wsgi = kwargs['wsgi']
        wsgi.register(RESTController, {API_INSTANCE: self})

    # -------------------- OpenFlow base --------------------
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp = ev.msg.datapath
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        dp.send_msg(parser.OFPFlowMod(datapath=dp, priority=0, match=match, instructions=inst))
        self.logger.info("Installed table-miss on %s", dp.id)

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def state_change_handler(self, ev):
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[dp.id] = dp
            self.G.add_node(dp.id)
        elif ev.state == DEAD_DISPATCHER:
            self.datapaths.pop(dp.id, None)
            if self.G.has_node(dp.id):
                self.G.remove_node(dp.id)
            self.core_ports.pop(dp.id, None)
            self.k_paths_cached = {k:v for k,v in self.k_paths_cached.items()
                                   if dp.id not in (k[0], k[1])}

    # -------------------- L2 Learning --------------------
    def _purge_hosts_on_port(self, dpid, port_no):
        bad = [m for m,h in self.hosts.items() if h['dpid']==dpid and h['port']==port_no]
        for mac in bad: self.hosts.pop(mac, None)
        if dpid in self.mac_to_port:
            for mac,p in list(self.mac_to_port[dpid].items()):
                if p==port_no: self.mac_to_port[dpid].pop(mac)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg, dp = ev.msg, ev.msg.datapath
        parser, ofp = dp.ofproto_parser, dp.ofproto
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]
        if eth.ethertype == ether_types.ETH_TYPE_LLDP: return
        in_port = msg.match['in_port']
        src, dst = eth.src, eth.dst

        if in_port not in self.core_ports.get(dp.id,set()):
            self.mac_to_port[dp.id][src] = in_port
            self.hosts[src] = {'dpid': dp.id, 'port': in_port}

        out_port = self.mac_to_port[dp.id].get(dst, ofp.OFPP_FLOOD)
        actions = [parser.OFPActionOutput(out_port)]
        if out_port != ofp.OFPP_FLOOD:
            match = parser.OFPMatch(in_port=in_port, eth_src=src, eth_dst=dst)
            inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
            dp.send_msg(parser.OFPFlowMod(datapath=dp, priority=1, match=match, instructions=inst))
        dp.send_msg(parser.OFPPacketOut(datapath=dp, buffer_id=ofp.OFP_NO_BUFFER,
                                        in_port=in_port, actions=actions, data=msg.data))

    # -------------------- Topology & Links --------------------
    @set_ev_cls(topo_event.EventLinkAdd)
    def link_add(self, ev):
        u,v = ev.link.src.dpid, ev.link.dst.dpid
        u_p,v_p = ev.link.src.port_no, ev.link.dst.port_no
        self.G.add_edge(u,v,u_port=u_p,v_port=v_p)
        self.G.add_edge(v,u,u_port=v_p,v_port=u_p)
        self.core_ports[u].add(u_p); self.core_ports[v].add(v_p)
        self._purge_hosts_on_port(u,u_p); self._purge_hosts_on_port(v,v_p)
        self.k_paths_cached.clear()
        self.logger.info("Link added %s:%s <-> %s:%s",u,u_p,v,v_p)

    @set_ev_cls(topo_event.EventLinkDelete)
    def link_del(self, ev):
        u,v = ev.link.src.dpid, ev.link.dst.dpid
        u_p,v_p = ev.link.src.port_no, ev.link.dst.port_no
        if self.G.has_edge(u,v): self.G.remove_edge(u,v)
        if self.G.has_edge(v,u): self.G.remove_edge(v,u)
        self.core_ports[u].discard(u_p); self.core_ports[v].discard(v_p)
        self.k_paths_cached.clear()
        self.logger.info("Link deleted %s:%s <-> %s:%s",u,u_p,v,v_p)

    def _sweep_core_leaks(self):
        while True:
            try:
                for dpid,ports in self.core_ports.items():
                    for p in list(ports):
                        self._purge_hosts_on_port(dpid,p)
            except Exception as e:
                self.logger.warning("sweep error: %s",e)
            hub.sleep(2)

    # -------------------- Stats --------------------
    def _monitor(self):
        while True:
            try:
                for dp in list(self.datapaths.values()):
                    p=dp.ofproto_parser
                    dp.send_msg(p.OFPPortStatsRequest(dp,0,dp.ofproto.OFPP_ANY))
                    dp.send_msg(p.OFPFlowStatsRequest(dp))
            except Exception as e:
                self.logger.warning("monitor error: %s",e)
            hub.sleep(self.monitor_interval)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply(self, ev):
        now=time.time(); stats=[]; rates=[]
        for s in ev.msg.body:
            rec={'timestamp':now,'dpid':ev.msg.datapath.id,'port_no':s.port_no,
                 'rx_bytes':s.rx_bytes,'tx_bytes':s.tx_bytes,
                 'rx_pkts':s.rx_packets,'tx_pkts':s.tx_packets,
                 'rx_dropped':s.rx_dropped,'tx_dropped':s.tx_dropped,
                 'rx_errors':s.rx_errors,'tx_errors':s.tx_errors}
            stats.append(rec)
            key=(rec['dpid'],rec['port_no'])
            prev=self.port_prev.get(key)
            if prev:
                dt=max(1e-6,now-prev['ts'])
                rates.append({
                    'timestamp':now,'dpid':rec['dpid'],'port_no':rec['port_no'],
                    'tx_bps':(rec['tx_bytes']-prev['tx_bytes'])*8.0/dt,
                    'rx_bps':(rec['rx_bytes']-prev['rx_bytes'])*8.0/dt})
            self.port_prev[key]={**rec,'ts':now}
        self.port_stats=[x for x in self.port_stats if x['dpid']!=ev.msg.datapath.id]+stats
        self.port_rates=[x for x in self.port_rates if x['dpid']!=ev.msg.datapath.id]+rates
        self.last_stats_ts=now

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def flow_stats_reply(self, ev):
        now=time.time(); flows=[]
        for s in ev.msg.body:
            flows.append({
                'timestamp':now,'dpid':ev.msg.datapath.id,'priority':s.priority,
                'table_id':s.table_id,'packet_count':s.packet_count,
                'byte_count':s.byte_count})
        self.flow_stats=[f for f in self.flow_stats if f['dpid']!=ev.msg.datapath.id]+flows
        self.last_stats_ts=now

    # -------------------- Path Helpers --------------------
    def _k_shortest_paths(self, src, dst, k=2):
        if src==dst: return []
        key=(src,dst,k)
        if key in self.k_paths_cached: return self.k_paths_cached[key]
        try:
            gen=nx.shortest_simple_paths(self.G,src,dst)
            paths=[]
            for p in gen:
                if len(p)>=2: paths.append(p)
                if len(paths)>=k: break
            self.k_paths_cached[key]=paths
            return paths
        except Exception: return []

    def _path_ports(self, dpids, dst_mac=None):
        hops=[]
        for i in range(len(dpids)-1):
            u,v=dpids[i],dpids[i+1]
            data=self.G.get_edge_data(u,v)
            if not data: return []
            hops.append({'dpid':u,'out_port':data.get('u_port')})
        last=dpids[-1]
        if dst_mac and dst_mac in self.hosts and self.hosts[dst_mac]['dpid']==last:
            hops.append({'dpid':last,'out_port':self.hosts[dst_mac]['port']})
        return hops

    def _install_path(self, src_mac, dst_mac, dpids):
        cookie=int(hashlib.md5(f"{src_mac}->{dst_mac}".encode()).hexdigest()[:16],16)
        for hop in self._path_ports(dpids,dst_mac):
            dp=self.datapaths.get(hop['dpid'])
            if not dp: continue
            p,ofp=dp.ofproto_parser,dp.ofproto
            match=p.OFPMatch(eth_dst=dst_mac)
            act=[p.OFPActionOutput(hop['out_port'])]
            inst=[p.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS,act)]
            dp.send_msg(p.OFPFlowMod(datapath=dp,priority=100,match=match,
                                     instructions=inst,cookie=cookie,idle_timeout=60))
        self.routes[(src_mac,dst_mac)]={'cookie':cookie,'path':dpids}
        self.last_action_ts[(src_mac,dst_mac)]=time.time()

    def _links_with_tx_bps(self):
        out=[]
        idx=defaultdict(dict)
        for r in self.port_rates:
            idx[r['dpid']][r['port_no']]=r.get('tx_bps',0.0)
        for u,v,data in self.G.edges(data=True):
            out.append({'src_dpid':u,'dst_dpid':v,
                        'src_port':data.get('u_port'),'dst_port':data.get('v_port'),
                        'tx_bps':idx.get(u,{}).get(data.get('u_port'),0.0)})
        return out


class RESTController(ControllerBase):
    def __init__(self, req, link, data, **config):
        super().__init__(req, link, data, **config)
        self.app: SDNRouterREST = data[API_INSTANCE]

    @route('health', '/api/v1/health', methods=['GET'])
    def health(self, req, **kwargs):
        return j({'status':'ok','last_stats_ts':self.app.last_stats_ts})

    @route('hosts', '/api/v1/hosts', methods=['GET'])
    def hosts(self, req, **kwargs):
        hosts=[{'mac':m,'dpid':h['dpid'],'port':h['port']}
               for m,h in self.app.hosts.items()
               if h['port'] not in self.app.core_ports[h['dpid']]]
        return j(hosts)

    @route('paths', '/api/v1/paths', methods=['GET'])
    def paths(self, req, **kwargs):
        p=req.params; s=p.get('src_mac'); d=p.get('dst_mac'); k=int(p.get('k',2))
        if not s or not d: return j({'error':'missing src_mac/dst_mac'},400)
        if s not in self.app.hosts or d not in self.app.hosts: return j({'error':'hosts not learned'},404)
        sdp,did=self.app.hosts[s]['dpid'],self.app.hosts[d]['dpid']
        paths=self.app._k_shortest_paths(sdp,did,k)
        out=[{'path_id':i,'dpids':p,'hops':self.app._path_ports(p,dst_mac=d)} for i,p in enumerate(paths)]
        return j(out)

    # Avoid shadowing the @route decorator
    @route('action_route', '/api/v1/actions/route', methods=['POST'])
    def apply_route(self, req, **kwargs):
        try:
            data=json.loads(req.body)
            validate(instance=data,schema=ROUTE_SCHEMA)
        except ValidationError as ve:
            return j({'error':'validation','detail':ve.message},400)
        except Exception:
            return j({'error':'invalid_json'},400)
        s,d=data['src_mac'],data['dst_mac']
        if s not in self.app.hosts or d not in self.app.hosts:
            return j({'error':'hosts_not_learned'},404)
        paths=data.get('path')
        if not paths:
            sdp=self.app.hosts[s]['dpid']; ddp=self.app.hosts[d]['dpid']
            allp=self.app._k_shortest_paths(sdp,ddp,k=int(data.get('k',2)))
            if not allp: return j({'error':'no_path'},409)
            pid=min(int(data.get('path_id',0)),len(allp)-1)
            paths=allp[pid]
        key=(s,d); prev=self.app.routes.get(key,{}).get('path')
        if prev and prev!=paths:
            delta=time.time()-self.app.last_action_ts.get(key,0.0)
            if delta<self.app.route_cooldown:
                retry_after = max(0, int(round(self.app.route_cooldown - delta)))
                # add header for clients that honor HTTP Retry-After
                return j({'error':'cooldown_active','retry_after':retry_after},
                         429, headers={'Retry-After': str(retry_after)})
        self.app._install_path(s,d,paths)
        self.app._install_path(d,s,list(reversed(paths)))
        return j({'status':'applied','path':paths})

    @route('stats_ports','/api/v1/stats/ports',methods=['GET'])
    def stats_ports(self,req,**kw):
        return j(self.app.port_stats)

    @route('stats_flows','/api/v1/stats/flows',methods=['GET'])
    def stats_flows(self,req,**kw):
        return j(self.app.flow_stats)

    @route('metrics_links','/api/v1/metrics/links',methods=['GET'])
    def metrics_links(self,req,**kw):
        return j(self.app._links_with_tx_bps())

    # NEW: provide /api/v1/metrics/ports so agents/supervisors don't 404
    @route('metrics_ports','/api/v1/metrics/ports',methods=['GET'])
    def metrics_ports(self, req, **kw):
        """
        Returns the latest computed per-port rates.
        Shape: [{"timestamp", "dpid", "port_no", "tx_bps", "rx_bps"}, ...]
        """
        return j(self.app.port_rates)

    # -------- Added to match OpenAPI/docs --------
    @route('topo_nodes', '/api/v1/topology/nodes', methods=['GET'])
    def topo_nodes(self, req, **kwargs):
        nodes = sorted(list(self.app.G.nodes()))
        return j(nodes)

    @route('topo_links', '/api/v1/topology/links', methods=['GET'])
    def topo_links(self, req, **kwargs):
        links = []
        for u, v, data in self.app.G.edges(data=True):
            links.append({
                'src_dpid': u, 'dst_dpid': v,
                'src_port': data.get('u_port'),
                'dst_port': data.get('v_port')
            })
        return j(links)

    @route('actions_list', '/api/v1/actions/list', methods=['GET'])
    def actions_list(self, req, **kwargs):
        out = []
        for (s, d), meta in self.app.routes.items():
            out.append({'src_mac': s, 'dst_mac': d,
                        'cookie': meta.get('cookie'),
                        'path': meta.get('path')})
        return j(out)

    @route('action_route_delete', '/api/v1/actions/route', methods=['DELETE'])
    def route_delete(self, req, **kwargs):
        p = req.params
        s = p.get('src_mac'); d = p.get('dst_mac')
        if not s or not d:
            return j({'error': 'missing src_mac/dst_mac'}, 400)
        key = (s, d)
        meta = self.app.routes.get(key)
        if not meta:
            return j({'error': 'not_found'}, 404)

        cookie = meta.get('cookie')
        for dp in list(self.app.datapaths.values()):
            ofp = dp.ofproto
            parser = dp.ofproto_parser
            mod = parser.OFPFlowMod(
                datapath=dp,
                cookie=cookie, cookie_mask=0xffffffffffffffff,
                table_id=ofp.OFPTT_ALL,
                command=ofp.OFPFC_DELETE,
                out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY,
                match=parser.OFPMatch()
            )
            dp.send_msg(mod)

        self.app.routes.pop(key, None)
        self.app.last_action_ts.pop(key, None)
        return j({'status': 'deleted'})
