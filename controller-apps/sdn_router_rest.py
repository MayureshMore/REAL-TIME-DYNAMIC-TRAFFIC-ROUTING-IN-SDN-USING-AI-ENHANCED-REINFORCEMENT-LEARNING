#!/usr/bin/env python3
# Ryu app: L2 learning + topology discovery + k-shortest paths + REST + actions
# Key fixes in this version:
# - Learn hosts on any non-core port even before topology is known (break bootstrapping deadlock)
# - When a port later becomes core (via LLDP link discovery), purge any host learned there
# - Directed topology (DiGraph) so each edge carries correct per-direction port metadata
# - Only return multi-switch paths (len(dpids) >= 2)
# - Robust JSON responses and OpenAPI handler

from collections import defaultdict
import hashlib, json, time
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, DEAD_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ether_types
from ryu.lib import hub
from ryu.topology import event as topo_event
import networkx as nx
from ryu.app.wsgi import WSGIApplication, ControllerBase, route
from webob import Response
from jsonschema import validate, ValidationError

API_INSTANCE = 'sdn_router_api'

def j(obj, status=200):
    return Response(content_type='application/json',
                    body=json.dumps(obj).encode('utf-8'),
                    status=status)

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
        # L2 learning & host table
        self.mac_to_port = defaultdict(dict)        # dpid -> {mac: port}
        self.hosts = {}                              # mac -> {dpid, port}

        # Switches and links (directed graph)
        self.datapaths = {}
        self.G = nx.DiGraph()

        # Inter-switch (core) ports per switch
        self.core_ports = defaultdict(set)           # dpid -> {port_no}

        # Path/service state
        self.k_paths_cached = {}
        self.routes = {}                             # (src,dst) -> {cookie, path}
        self.last_action_ts = {}
        self.route_cooldown = 5.0

        # Stats
        self.port_stats = []
        self.port_prev = {}
        self.port_rates = []
        self.flow_stats = []
        self.last_stats_ts = 0.0

        # Background workers
        self.monitor_interval = 2
        self.monitor_thread = hub.spawn(self._monitor)
        self.sweep_thread = hub.spawn(self._sweep_core_leaks)

        # REST wiring
        wsgi = kwargs['wsgi']
        wsgi.register(RESTController, {API_INSTANCE: self})

    # --- OpenFlow provisioning ---

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp = ev.msg.datapath
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        # Table-miss to controller
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        dp.send_msg(parser.OFPFlowMod(datapath=dp, priority=0, match=match, instructions=inst))
        self.logger.info("Installed table-miss on dpid=%s", dp.id)

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def state_change_handler(self, ev):
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            if dp.id not in self.datapaths:
                self.datapaths[dp.id] = dp
                self.G.add_node(dp.id)
        elif ev.state == DEAD_DISPATCHER:
            if dp and dp.id in self.datapaths:
                del self.datapaths[dp.id]
                if self.G.has_node(dp.id):
                    self.G.remove_node(dp.id)
                self.core_ports.pop(dp.id, None)
                self.k_paths_cached = {k:v for k,v in self.k_paths_cached.items()
                                       if dp.id not in (k[0], k[1])}

    # --- L2 learning with core-port awareness ---

    def _purge_hosts_on_port(self, dpid, port_no):
        # Remove any host learned on (dpid, port_no) and clear mac_to_port entries
        to_delete = [m for m, meta in self.hosts.items()
                     if meta.get('dpid') == dpid and meta.get('port') == port_no]
        for mac in to_delete:
            self.hosts.pop(mac, None)
        # Also purge L2 table entries pointing to this port
        bad_mac = [m for m, p in self.mac_to_port.get(dpid, {}).items() if p == port_no]
        for mac in bad_mac:
            self.mac_to_port[dpid].pop(mac, None)

    def _mark_core_port(self, dpid, port_no, is_core=True):
        if is_core:
            if port_no not in self.core_ports[dpid]:
                self.core_ports[dpid].add(port_no)
                self._purge_hosts_on_port(dpid, port_no)
        else:
            self.core_ports[dpid].discard(port_no)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        dpid = dp.id

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]
        # Ignore LLDP frames used by topology discovery
        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        in_port = msg.match['in_port']
        src = eth.src
        dst = eth.dst

        # FIX: learn on any non-core port (even if we don't yet know core ports on this switch)
        if in_port not in self.core_ports.get(dpid, set()):
            self.mac_to_port[dpid][src] = in_port
            self.hosts[src] = {'dpid': dpid, 'port': in_port}

        # L2 forwarding (flood if unknown)
        out_port = self.mac_to_port[dpid].get(dst, ofp.OFPP_FLOOD)
        actions = [parser.OFPActionOutput(out_port)]

        # Install a unicast flow when we know the dst on this switch
        if out_port != ofp.OFPP_FLOOD:
            match = parser.OFPMatch(in_port=in_port, eth_src=src, eth_dst=dst)
            inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
            dp.send_msg(parser.OFPFlowMod(datapath=dp, priority=1, match=match, instructions=inst))

        dp.send_msg(parser.OFPPacketOut(datapath=dp, buffer_id=ofp.OFP_NO_BUFFER,
                                        in_port=in_port, actions=actions, data=msg.data))

    # --- Topology events -> directed edges + core ports ---

    @set_ev_cls(topo_event.EventLinkAdd)
    def link_add_handler(self, ev):
        l = ev.link
        u, v = l.src.dpid, l.dst.dpid
        u_p, v_p = l.src.port_no, l.dst.port_no

        # Directed edges with per-direction egress port
        self.G.add_edge(u, v, u_port=u_p, v_port=v_p)
        self.G.add_edge(v, u, u_port=v_p, v_port=u_p)

        # Mark inter-switch ports as core on both ends and purge any hosts there
        self._mark_core_port(u, u_p, True)
        self._mark_core_port(v, v_p, True)
        self._purge_hosts_on_port(u, u_p)
        self._purge_hosts_on_port(v, v_p)

        self.k_paths_cached.clear()

    @set_ev_cls(topo_event.EventLinkDelete)
    def link_del_handler(self, ev):
        l = ev.link
        u, v = l.src.dpid, l.dst.dpid
        u_p, v_p = l.src.port_no, l.dst.port_no
        if self.G.has_edge(u, v): self.G.remove_edge(u, v)
        if self.G.has_edge(v, u): self.G.remove_edge(v, u)
        self._mark_core_port(u, u_p, False)
        self._mark_core_port(v, v_p, False)
        self.k_paths_cached.clear()

    # Continuous sweeper to catch races during discovery
    def _sweep_core_leaks(self):
        while True:
            try:
                for dpid, ports in list(self.core_ports.items()):
                    for p in list(ports):
                        self._purge_hosts_on_port(dpid, p)
            except Exception as e:
                self.logger.error("sweep error: %s", e)
            finally:
                hub.sleep(1.0)

    # --- Stats polling ---

    def _monitor(self):
        while True:
            try:
                for dpid, dp in list(self.datapaths.items()):
                    p = dp.ofproto_parser
                    dp.send_msg(p.OFPPortStatsRequest(dp, 0, dp.ofproto.OFPP_ANY))
                    dp.send_msg(p.OFPFlowStatsRequest(dp))
            except Exception as e:
                self.logger.error("monitor error: %s", e)
            finally:
                hub.sleep(self.monitor_interval)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply_handler(self, ev):
        now = time.time()
        stats, rates = [], []
        for s in ev.msg.body:
            rec = {
                'timestamp': now, 'dpid': ev.msg.datapath.id, 'port_no': s.port_no,
                'rx_pkts': s.rx_packets, 'tx_pkts': s.tx_packets,
                'rx_bytes': s.rx_bytes, 'tx_bytes': s.tx_bytes,
                'rx_dropped': s.rx_dropped, 'tx_dropped': s.tx_dropped,
                'rx_errors': s.rx_errors, 'tx_errors': s.tx_errors,
            }
            stats.append(rec)
            key = (rec['dpid'], rec['port_no'])
            prev = self.port_prev.get(key)
            if prev:
                dt = max(1e-6, now - prev['ts'])
                rates.append({
                    'timestamp': now, 'dpid': rec['dpid'], 'port_no': rec['port_no'],
                    'tx_bps': max(0.0, (rec['tx_bytes'] - prev['tx_bytes']) * 8.0 / dt),
                    'rx_bps': max(0.0, (rec['rx_bytes'] - prev['rx_bytes']) * 8.0 / dt),
                    'tx_pps': max(0.0, (rec['tx_pkts']  - prev['tx_pkts'])  / dt),
                    'rx_pps': max(0.0, (rec['rx_pkts']  - prev['rx_pkts'])  / dt),
                })
            self.port_prev[key] = {**rec, 'ts': now}
        self.port_stats = [x for x in self.port_stats if x['dpid'] != ev.msg.datapath.id] + stats
        self.port_rates = [x for x in self.port_rates if x['dpid'] != ev.msg.datapath.id] + rates
        self.last_stats_ts = now

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def flow_stats_reply_handler(self, ev):
        now = time.time()
        flows = []
        for s in ev.msg.body:
            flows.append({
                'timestamp': now, 'dpid': ev.msg.datapath.id,
                'priority': s.priority, 'table_id': s.table_id,
                'duration_sec': getattr(s, 'duration_sec', 0),
                'packet_count': s.packet_count, 'byte_count': s.byte_count,
                'match': getattr(s, 'match', None).to_jsondict() if hasattr(s, 'match') else {},
            })
        self.flow_stats = [f for f in self.flow_stats if f['dpid'] != ev.msg.datapath.id] + flows
        self.last_stats_ts = now

    # --- Path helpers ---

    def _k_shortest_paths(self, src_dpid, dst_dpid, k=2):
        if src_dpid == dst_dpid:
            return []
        key = (src_dpid, dst_dpid, k)
        if key in self.k_paths_cached:
            return self.k_paths_cached[key]
        if src_dpid not in self.G or dst_dpid not in self.G:
            return []
        try:
            gen = nx.shortest_simple_paths(self.G, src_dpid, dst_dpid)
            paths = []
            for p in gen:
                if len(p) >= 2:
                    paths.append(p)
                if len(paths) >= k:
                    break
            self.k_paths_cached[key] = paths
            return paths
        except Exception:
            return []

    def _path_ports(self, path_dpids, dst_mac=None):
        if len(path_dpids) < 2:
            return []
        hops = []
        for i in range(len(path_dpids) - 1):
            u, v = path_dpids[i], path_dpids[i + 1]
            data = self.G.get_edge_data(u, v)
            if not data or 'u_port' not in data:
                return []
            hops.append({'dpid': u, 'out_port': data['u_port']})
        last = path_dpids[-1]
        if dst_mac and dst_mac in self.hosts and self.hosts[dst_mac]['dpid'] == last:
            hops.append({'dpid': last, 'out_port': self.hosts[dst_mac]['port']})
        return hops

    def _cookie_for_pair(self, src_mac, dst_mac):
        h = hashlib.md5(f"{src_mac}->{dst_mac}".encode()).hexdigest()
        return int(h[:16], 16)

    def _install_unidirectional_path(self, src_mac, dst_mac, path_dpids, priority=100):
        cookie = self._cookie_for_pair(src_mac, dst_mac)
        for hop in self._path_ports(path_dpids, dst_mac=dst_mac):
            dpid, out_port = hop['dpid'], hop['out_port']
            if dpid not in self.datapaths:
                continue
            dp = self.datapaths[dpid]
            parser, ofp = dp.ofproto_parser, dp.ofproto
            actions = [parser.OFPActionOutput(out_port)]
            match = parser.OFPMatch(eth_dst=dst_mac)
            inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
            mod = parser.OFPFlowMod(datapath=dp, priority=priority, match=match, instructions=inst,
                                    cookie=cookie, idle_timeout=60, hard_timeout=300)
            dp.send_msg(mod)
        self.routes[(src_mac, dst_mac)] = {'cookie': cookie, 'path': path_dpids}

    def _links_with_tx_bps(self):
        from collections import defaultdict as dd
        idx = dd(dict)
        for r in self.port_rates:
            idx[r['dpid']][r['port_no']] = r.get('tx_bps', 0.0)
        out = []
        for u, v, data in self.G.edges(data=True):
            out.append({
                'src_dpid': u, 'dst_dpid': v,
                'src_port': data.get('u_port'), 'dst_port': data.get('v_port'),
                'tx_bps': idx.get(u, {}).get(data.get('u_port'), 0.0)
            })
        return out

class RESTController(ControllerBase):
    def __init__(self, req, link, data, **config):
        super().__init__(req, link, data, **config)
        self.app: SDNRouterREST = data[API_INSTANCE]

    @route('health', '/api/v1/health', methods=['GET'])
    def health(self, req, **kwargs):
        return j({'status': 'ok', 'last_stats_ts': self.app.last_stats_ts})

    @route('stats_ports', '/api/v1/stats/ports', methods=['GET'])
    def stats_ports(self, req, **kwargs):
        return j(self.app.port_stats)

    @route('stats_flows', '/api/v1/stats/flows', methods=['GET'])
    def stats_flows(self, req, **kwargs):
        return j(self.app.flow_stats)

    @route('topo_nodes', '/api/v1/topology/nodes', methods=['GET'])
    def topo_nodes(self, req, **kwargs):
        return j(list(self.app.G.nodes()))

    @route('topo_links', '/api/v1/topology/links', methods=['GET'])
    def topo_links(self, req, **kwargs):
        res = []
        for u, v, data in self.app.G.edges(data=True):
            res.append({'src_dpid': u, 'dst_dpid': v,
                        'src_port': data.get('u_port'), 'dst_port': data.get('v_port')})
        return j(res)

    @route('hosts', '/api/v1/hosts', methods=['GET'])
    def hosts(self, req, **kwargs):
        items = [{'mac': m, 'dpid': inf['dpid'], 'port': inf['port']} for m, inf in self.app.hosts.items()]
        return j(items)

    @route('paths', '/api/v1/paths', methods=['GET'])
    def paths(self, req, **kwargs):
        params = req.params
        src_mac, dst_mac = params.get('src_mac'), params.get('dst_mac')
        k = int(params.get('k', 2))
        if not src_mac or not dst_mac:
            return j({'error': 'src_mac and dst_mac required'}, status=400)
        if src_mac not in self.app.hosts or dst_mac not in self.app.hosts:
            return j({'error': 'hosts not yet learned'}, status=404)
        s, d = self.app.hosts[src_mac]['dpid'], self.app.hosts[dst_mac]['dpid']
        paths = self.app._k_shortest_paths(s, d, k=k)
        out = []
        for i, p in enumerate(paths):
            hops = self.app._path_ports(p, dst_mac=dst_mac)
            out.append({'path_id': i, 'dpids': p, 'hops': hops})
        return j(out)

    @route('route', '/api/v1/actions/route', methods=['POST'])
    def route_action(self, req, **kwargs):
        try:
            data = json.loads(req.body)
            validate(instance=data, schema=ROUTE_SCHEMA)
        except ValidationError as ve:
            return j({'error': 'validation_error', 'detail': ve.message}, status=400)
        except Exception:
            return j({'error': 'invalid_json'}, status=400)

        src_mac, dst_mac = data['src_mac'], data['dst_mac']
        if src_mac not in self.app.hosts or dst_mac not in self.app.hosts:
            return j({'error': 'hosts_not_learned'}, status=404)

        chosen = data.get('path')
        if not chosen:
            s, d = self.app.hosts[src_mac]['dpid'], self.app.hosts[dst_mac]['dpid']
            k = int(data.get('k', 2))
            paths = self.app._k_shortest_paths(s, d, k=k)
            if not paths:
                return j({'error': 'no_path'}, status=409)
            idx = int(data.get('path_id', 0))
            idx = 0 if idx < 0 or idx >= len(paths) else idx
            chosen = paths[idx]

        key = (src_mac, dst_mac)
        now = time.time()
        prev = self.app.routes.get(key, {}).get('path')
        if prev and prev != chosen and (now - self.app.last_action_ts.get(key, 0.0)) < self.app.route_cooldown:
            retry = int(self.app.route_cooldown - (now - self.app.last_action_ts.get(key, 0.0)) + 0.5)
            return j({'error': 'cooldown_active', 'retry_after': retry}, status=429)

        self.app._install_unidirectional_path(src_mac, dst_mac, chosen)
        self.app._install_unidirectional_path(dst_mac, src_mac, list(reversed(chosen)))
        self.app.last_action_ts[key] = now
        return j({'status': 'applied', 'path': chosen})

    @route('route_list', '/api/v1/actions/list', methods=['GET'])
    def route_list(self, req, **kwargs):
        items = [{'src_mac': src, 'dst_mac': dst, 'cookie': meta['cookie'], 'path': meta['path']}
                 for (src, dst), meta in self.app.routes.items()]
        return j(items)

    @route('route_delete', '/api/v1/actions/route', methods=['DELETE'])
    def route_delete(self, req, **kwargs):
        params = req.params
        src_mac, dst_mac = params.get('src_mac'), params.get('dst_mac')
        if not src_mac or not dst_mac:
            return j({'error': 'src_mac and dst_mac required'}, status=400)
        key = (src_mac, dst_mac)
        if key not in self.app.routes:
            return j({'error': 'not_found'}, status=404)
        cookie = self.app.routes[key]['cookie']
        mask = 0xFFFFFFFFFFFFFFFF
        count = 0
        for dp in self.app.datapaths.values():
            p, ofp = dp.ofproto_parser, dp.ofproto
            mod = p.OFPFlowMod(datapath=dp, command=ofp.OFPFC_DELETE,
                               out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY,
                               cookie=cookie, cookie_mask=mask)
            dp.send_msg(mod)
            count += 1
        del self.app.routes[key]
        return j({'status': 'deleted', 'affected_dpids': count})

    @route('link_metrics', '/api/v1/metrics/links', methods=['GET'])
    def link_metrics(self, req, **kwargs):
        return j(self.app._links_with_tx_bps())

    @route('openapi', '/api/v1/openapi.yaml', methods=['GET'])
    def openapi(self, req, **kwargs):
        try:
            with open('docs/openapi.yaml', 'rb') as f:
                spec = f.read()
            return Response(content_type='application/yaml', body=spec)
        except Exception as e:
            return j({'error': 'openapi_not_found', 'detail': str(e)}, status=500)
