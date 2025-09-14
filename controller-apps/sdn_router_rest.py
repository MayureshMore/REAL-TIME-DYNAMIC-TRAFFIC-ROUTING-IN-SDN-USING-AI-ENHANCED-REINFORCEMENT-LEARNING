# controller-apps/sdn_router_rest.py
# Ryu app: L2 learning + Topology discovery + k-shortest paths + route install/delete + stats + REST
# Run with: ryu-manager controller-apps/sdn_router_rest.py ryu.topology.switches --ofp-tcp-listen-port 6633

from collections import defaultdict
import hashlib
import json
import time

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, DEAD_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ether_types
from ryu.lib import hub

# Topology
from ryu.topology import event as topo_event
import networkx as nx

# REST
from ryu.app.wsgi import WSGIApplication, ControllerBase, route
from webob import Response

API_INSTANCE = 'sdn_router_api'


class SDNRouterREST(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS = {'wsgi': WSGIApplication}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.logger.info("Starting SDNRouterREST")

        # L2 learning
        self.mac_to_port = defaultdict(dict)   # dpid -> { mac: port }
        self.hosts = {}                        # mac -> { 'dpid': dpid, 'port': port }

        # Datapaths
        self.datapaths = {}

        # Topology graph and cache
        self.G = nx.Graph()                    # nodes: dpids; edges carry {u_port, v_port}
        self.k_paths_cached = {}               # (src_dpid, dst_dpid, k) -> [ [dpids], ... ]

        # Installed routes (for cleanup)
        self.routes = {}                       # (src_mac, dst_mac) -> {'cookie': int, 'path': [dpids]}

        # Stats
        self.port_stats = []
        self.flow_stats = []
        self.last_stats_ts = 0.0
        self.monitor_interval = 5
        self.monitor_thread = hub.spawn(self._monitor)

        # REST wiring
        wsgi = kwargs['wsgi']
        wsgi.register(RESTController, {API_INSTANCE: self})

    # ---------- Switch lifecycle ----------
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp = ev.msg.datapath
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        # table-miss
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
                self.logger.info("Datapath registered: %s", dp.id)
        elif ev.state == DEAD_DISPATCHER:
            if dp and dp.id in self.datapaths:
                del self.datapaths[dp.id]
                if self.G.has_node(dp.id):
                    self.G.remove_node(dp.id)
                self.logger.info("Datapath unregistered: %s", dp.id)

    # ---------- L2 learning ----------
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        dpid = dp.id

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]
        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return
        in_port = msg.match['in_port']
        src = eth.src
        dst = eth.dst

        # learn
        self.mac_to_port[dpid][src] = in_port
        self.hosts[src] = {'dpid': dpid, 'port': in_port}

        # forwarding decision
        if dst in self.mac_to_port[dpid]:
            out_port = self.mac_to_port[dpid][dst]
        else:
            out_port = ofp.OFPP_FLOOD

        actions = [parser.OFPActionOutput(out_port)]
        if out_port != ofp.OFPP_FLOOD:
            match = parser.OFPMatch(in_port=in_port, eth_src=src, eth_dst=dst)
            inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
            dp.send_msg(parser.OFPFlowMod(datapath=dp, priority=1, match=match, instructions=inst))

        dp.send_msg(parser.OFPPacketOut(datapath=dp, buffer_id=ofp.OFP_NO_BUFFER,
                                        in_port=in_port, actions=actions, data=msg.data))

    # ---------- Topology events (from ryu.topology.switches) ----------
    @set_ev_cls(topo_event.EventLinkAdd)
    def link_add_handler(self, ev):
        l = ev.link
        u, v = l.src.dpid, l.dst.dpid
        u_p, v_p = l.src.port_no, l.dst.port_no
        # store both directions
        self.G.add_edge(u, v, u_port=u_p, v_port=v_p)
        self.G.add_edge(v, u, u_port=v_p, v_port=u_p)
        self.k_paths_cached.clear()
        self.logger.info("Link added: %s[%s] <-> %s[%s]", u, u_p, v, v_p)

    # ---------- Periodic stats polling ----------
    def _monitor(self):
        while True:
            try:
                for dpid, dp in list(self.datapaths.items()):
                    parser = dp.ofproto_parser
                    dp.send_msg(parser.OFPPortStatsRequest(dp, 0, dp.ofproto.OFPP_ANY))
                    dp.send_msg(parser.OFPFlowStatsRequest(dp))
            except Exception as e:
                self.logger.error("Monitor error: %s", e)
            finally:
                hub.sleep(self.monitor_interval)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply_handler(self, ev):
        now = time.time()
        stats = []
        for s in ev.msg.body:
            stats.append({
                'timestamp': now,
                'dpid': ev.msg.datapath.id,
                'port_no': s.port_no,
                'rx_pkts': s.rx_packets,
                'tx_pkts': s.tx_packets,
                'rx_bytes': s.rx_bytes,
                'tx_bytes': s.tx_bytes,
                'rx_dropped': s.rx_dropped,
                'tx_dropped': s.tx_dropped,
                'rx_errors': s.rx_errors,
                'tx_errors': s.tx_errors,
            })
        self.port_stats = [x for x in self.port_stats if x['dpid'] != ev.msg.datapath.id]
        self.port_stats.extend(stats)
        self.last_stats_ts = now

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def flow_stats_reply_handler(self, ev):
        now = time.time()
        flows = []
        for s in ev.msg.body:
            flows.append({
                'timestamp': now,
                'dpid': ev.msg.datapath.id,
                'priority': s.priority,
                'table_id': s.table_id,
                'duration_sec': s.duration_sec,
                'packet_count': s.packet_count,
                'byte_count': s.byte_count,
                'match': s.match.to_jsondict(),
            })
        self.flow_stats = [f for f in self.flow_stats if f['dpid'] != ev.msg.datapath.id]
        self.flow_stats.extend(flows)
        self.last_stats_ts = now

    # ---------- Helpers ----------
    def _k_shortest_paths(self, src_dpid, dst_dpid, k=2):
        key = (src_dpid, dst_dpid, k)
        if key in self.k_paths_cached:
            return self.k_paths_cached[key]
        if src_dpid not in self.G or dst_dpid not in self.G:
            return []
        try:
            gen = nx.shortest_simple_paths(self.G, src_dpid, dst_dpid)
            paths = []
            for i, p in enumerate(gen):
                paths.append(p)
                if i + 1 >= k:
                    break
            self.k_paths_cached[key] = paths
            return paths
        except Exception:
            return []

    def _path_ports(self, path_dpids, dst_mac=None):
        """Return list of (dpid, out_port) along path for forwarding towards dst_mac."""
        hops = []
        for i in range(len(path_dpids) - 1):
            u = path_dpids[i]
            v = path_dpids[i + 1]
            data = self.G.get_edge_data(u, v)
            if not data or 'u_port' not in data:
                return []
            hops.append({'dpid': u, 'out_port': data['u_port']})
        # Last hop to host port if known
        last = path_dpids[-1]
        if dst_mac and dst_mac in self.hosts and self.hosts[dst_mac]['dpid'] == last:
            hops.append({'dpid': last, 'out_port': self.hosts[dst_mac]['port']})
        return hops

    def _cookie_for_pair(self, src_mac, dst_mac):
        h = hashlib.md5(f"{src_mac}->{dst_mac}".encode()).hexdigest()
        return int(h[:16], 16)  # 64-bit cookie from first 16 hex chars

    def _install_unidirectional_path(self, src_mac, dst_mac, path_dpids, priority=100):
        """Install flows matching eth_dst=dst_mac at each hop along path (with cookie & timeouts)."""
        cookie = self._cookie_for_pair(src_mac, dst_mac)
        for hop in self._path_ports(path_dpids, dst_mac=dst_mac):
            dpid = hop['dpid']
            out_port = hop['out_port']
            if dpid not in self.datapaths:
                continue
            dp = self.datapaths[dpid]
            parser = dp.ofproto_parser
            ofp = dp.ofproto
            actions = [parser.OFPActionOutput(out_port)]
            match = parser.OFPMatch(eth_dst=dst_mac)
            inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
            mod = parser.OFPFlowMod(
                datapath=dp,
                priority=priority,
                match=match,
                instructions=inst,
                cookie=cookie,
                idle_timeout=60,   # auto-expire if inactive
                hard_timeout=300   # safety cap
            )
            dp.send_msg(mod)
        self.routes[(src_mac, dst_mac)] = {'cookie': cookie, 'path': path_dpids}
        self.logger.info("Installed path for %s -> %s via %s cookie=0x%x",
                         src_mac, dst_mac, path_dpids, cookie)


class RESTController(ControllerBase):
    def __init__(self, req, link, data, **config):
        super().__init__(req, link, data, **config)
        self.app: SDNRouterREST = data[API_INSTANCE]

    # --- Health & Stats ---
    @route('health', '/api/v1/health', methods=['GET'])
    def health(self, req, **kwargs):
        body = {'status': 'ok', 'last_stats_ts': self.app.last_stats_ts}
        return Response(content_type='application/json', body=json.dumps(body))

    @route('stats_ports', '/api/v1/stats/ports', methods=['GET'])
    def stats_ports(self, req, **kwargs):
        return Response(content_type='application/json', body=json.dumps(self.app.port_stats))

    @route('stats_flows', '/api/v1/stats/flows', methods=['GET'])
    def stats_flows(self, req, **kwargs):
        return Response(content_type='application/json', body=json.dumps(self.app.flow_stats))

    # --- Topology ---
    @route('topo_nodes', '/api/v1/topology/nodes', methods=['GET'])
    def topo_nodes(self, req, **kwargs):
        return Response(content_type='application/json', body=json.dumps(list(self.app.G.nodes())))

    @route('topo_links', '/api/v1/topology/links', methods=['GET'])
    def topo_links(self, req, **kwargs):
        links = []
        for u, v, data in self.app.G.edges(data=True):
            links.append({'src_dpid': u, 'dst_dpid': v,
                          'src_port': data.get('u_port'), 'dst_port': data.get('v_port')})
        return Response(content_type='application/json', body=json.dumps(links))

    @route('hosts', '/api/v1/hosts', methods=['GET'])
    def hosts(self, req, **kwargs):
        items = [{'mac': m, 'dpid': inf['dpid'], 'port': inf['port']} for m, inf in self.app.hosts.items()]
        return Response(content_type='application/json', body=json.dumps(items))

    @route('paths', '/api/v1/paths', methods=['GET'])
    def paths(self, req, **kwargs):
        params = req.params
        src_mac = params.get('src_mac')
        dst_mac = params.get('dst_mac')
        k = int(params.get('k', 2))
        if not src_mac or not dst_mac:
            return Response(status=400, content_type='application/json',
                            body=json.dumps({'error': 'src_mac and dst_mac required'}))
        if src_mac not in self.app.hosts or dst_mac not in self.app.hosts:
            return Response(status=404, content_type='application/json',
                            body=json.dumps({'error': 'hosts not yet learned'}))
        s_dpid = self.app.hosts[src_mac]['dpid']
        d_dpid = self.app.hosts[dst_mac]['dpid']
        paths = self.app._k_shortest_paths(s_dpid, d_dpid, k=k)
        out = []
        for i, p in enumerate(paths):
            hops = self.app._path_ports(p, dst_mac=dst_mac)
            out.append({'path_id': i, 'dpids': p, 'hops': hops})
        return Response(content_type='application/json', body=json.dumps(out))

    # --- Actions ---
    @route('route', '/api/v1/actions/route', methods=['POST'])
    def route_action(self, req, **kwargs):
        try:
            data = json.loads(req.body)
        except Exception:
            return Response(status=400, content_type='application/json',
                            body=json.dumps({'error': 'invalid json'}))
        src_mac = data.get('src_mac')
        dst_mac = data.get('dst_mac')
        path = data.get('path')  # optional explicit list of dpids
        path_id = data.get('path_id')
        if not src_mac or not dst_mac:
            return Response(status=400, content_type='application/json',
                            body=json.dumps({'error': 'src_mac and dst_mac required'}))
        if src_mac not in self.app.hosts or dst_mac not in self.app.hosts:
            return Response(status=404, content_type='application/json',
                            body=json.dumps({'error': 'hosts not yet learned'}))

        s_dpid = self.app.hosts[src_mac]['dpid']
        d_dpid = self.app.hosts[dst_mac]['dpid']
        if not path:
            k = int(data.get('k', 2))
            paths = self.app._k_shortest_paths(s_dpid, d_dpid, k=k)
            if not paths:
                return Response(status=409, content_type='application/json', body=json.dumps({'error': 'no path'}))
            idx = int(path_id) if path_id is not None else 0
            if idx < 0 or idx >= len(paths):
                idx = 0
            path = paths[idx]

        # Install both directions
        self.app._install_unidirectional_path(src_mac, dst_mac, path)
        self.app._install_unidirectional_path(dst_mac, src_mac, list(reversed(path)))
        return Response(content_type='application/json', body=json.dumps({'status': 'applied', 'path': path}))

    @route('route_list', '/api/v1/actions/list', methods=['GET'])
    def route_list(self, req, **kwargs):
        items = []
        for (src, dst), meta in self.app.routes.items():
            items.append({'src_mac': src, 'dst_mac': dst, 'cookie': meta['cookie'], 'path': meta['path']})
        return Response(content_type='application/json', body=json.dumps(items))

    @route('route_delete', '/api/v1/actions/route', methods=['DELETE'])
    def route_delete(self, req, **kwargs):
        params = req.params
        src_mac = params.get('src_mac')
        dst_mac = params.get('dst_mac')
        if not src_mac or not dst_mac:
            return Response(status=400, content_type='application/json',
                            body=json.dumps({'error': 'src_mac and dst_mac required'}))
        key = (src_mac, dst_mac)
        if key not in self.app.routes:
            return Response(status=404, content_type='application/json', body=json.dumps({'error': 'not found'}))
        cookie = self.app.routes[key]['cookie']
        mask = 0xFFFFFFFFFFFFFFFF
        count = 0
        for dp in self.app.datapaths.values():
            parser = dp.ofproto_parser
            ofp = dp.ofproto
            mod = parser.OFPFlowMod(
                datapath=dp,
                command=ofp.OFPFC_DELETE,
                out_port=ofp.OFPP_ANY,
                out_group=ofp.OFPG_ANY,
                cookie=cookie,
                cookie_mask=mask
            )
            dp.send_msg(mod)
            count += 1
        del self.app.routes[key]
        return Response(content_type='application/json', body=json.dumps({'status': 'deleted', 'affected_dpids': count}))
