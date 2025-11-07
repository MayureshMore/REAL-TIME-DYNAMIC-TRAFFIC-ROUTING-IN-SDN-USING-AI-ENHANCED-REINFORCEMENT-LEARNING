#!/usr/bin/env python3
# Unified SDN controller app: L2 learning + topology + routing + REST + stats
#
# Key features
#  - L2 host learning (MAC -> {dpid, port})
#  - Live topology via Ryu topo events (nx.DiGraph)
#  - k-shortest path computation and flow installation
#  - Periodic port-stats polling (/api/v1/stats/ports)
#  - REST API (/api/v1/*)
#  - Cooldown on route updates (sends HTTP 429 with Retry-After)
#
# Fixes vs older version
#  - /api/v1/actions/route now returns standard Retry-After header (and JSON retry_after)
#  - Endpoint function is named apply_route to avoid shadowing @route decorator name
#  - Safer JSON helpers and schema validation on inputs

import json
import time
import hashlib
from math import ceil
from collections import defaultdict, deque

import networkx as nx

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import (
    CONFIG_DISPATCHER,
    MAIN_DISPATCHER,
    DEAD_DISPATCHER,
    set_ev_cls,
)
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ether_types
from ryu.lib import hub

from ryu.topology import event as topo_event
from ryu.app.wsgi import WSGIApplication, ControllerBase, route
from webob import Response
from jsonschema import validate, ValidationError

API_INSTANCE = 'sdn_router_api'

#
# -------- Helpers
#

def j(obj, status=200, headers=None):
    """JSON web response helper."""
    resp = Response(
        content_type='application/json',
        body=json.dumps(obj, default=str).encode('utf-8'),
        status=status,
    )
    if headers:
        for k, v in headers.items():
            resp.headers[k] = str(v)
    return resp


def now():
    return time.time()


def mac_str(mac_bytes):
    return ":".join(f"{b:02x}" for b in mac_bytes)


#
# -------- App
#

class SdnRouterApp(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    # Public knobs
    route_cooldown = 1.0  # seconds to wait between path flips for a (src,dst)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # L2 learning: mac -> {'dpid': int, 'port': int}
        self.hosts = {}

        # nx graph of datapaths; edges carry {'src_port', 'dst_port'}
        self.graph = nx.DiGraph()

        # Runtime datapath map: dpid -> datapath
        self.datapaths = {}

        # Latest chosen routes: (src_mac, dst_mac) -> {'path': [dpids], 'ts': time}
        self.routes = {}

        # Last action timestamp per host-pair
        self.last_action_ts = {}

        # Port statistics store: (dpid, port_no) -> dict of counters
        self.port_stats = defaultdict(lambda: {
            'rx_bytes': 0, 'tx_bytes': 0,
            'rx_packets': 0, 'tx_packets': 0,
            'rx_dropped': 0, 'tx_dropped': 0,
            'rx_errors': 0, 'tx_errors': 0,
            'timestamp': 0.0,
        })

        # WSGI/REST wiring
        wsgi = kwargs.get('wsgi')
        if wsgi is not None:
            wsgi.register(RestApi, {API_INSTANCE: self})

        # Background threads
        self.monitor_thread = hub.spawn(self._monitor)

    #
    # ---- OpenFlow handlers
    #

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        """Install table-miss."""
        datapath = ev.msg.datapath
        ofp = datapath.ofproto
        parser = datapath.ofproto_parser

        self.logger.info("Switch features dpid=%s", datapath.id)

        # Table-miss
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER,
                                          ofp.OFPCML_NO_BUFFER)]
        self._add_flow(datapath, priority=0, match=match, actions=actions)

        # Ask for port stats periodically (monitor thread also polls)
        self.datapaths[datapath.id] = datapath

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def state_change_handler(self, ev):
        datapath = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[datapath.id] = datapath
            self.logger.info("DP JOIN dpid=%s", datapath.id)
        elif ev.state == DEAD_DISPATCHER:
            if datapath.id in self.datapaths:
                self.logger.info("DP LEAVE dpid=%s", datapath.id)
                del self.datapaths[datapath.id]
                # Remove from graph
                try:
                    self.graph.remove_node(datapath.id)
                except Exception:
                    pass

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        """L2 learning; do not flood aggressively — rely on REST path install."""
        msg = ev.msg
        datapath = msg.datapath
        parser = datapath.ofproto_parser
        ofp = datapath.ofproto

        in_port = msg.match['in_port']

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)

        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        src = eth.src
        dst = eth.dst

        # Learn source
        self.hosts[src] = {'dpid': datapath.id, 'port': in_port}

        # If we know where to send to, hand off with a short, low-priority flow
        if dst in self.hosts:
            out_dpid = self.hosts[dst]['dpid']
            out_port = None
            if out_dpid == datapath.id:
                out_port = self.hosts[dst]['port']

            if out_port is not None:
                actions = [parser.OFPActionOutput(out_port)]
                # Install a very low-priority direct rule to reduce packet_ins
                match = parser.OFPMatch(eth_dst=dst)
                self._add_flow(datapath, priority=1, match=match, actions=actions, idle_timeout=30)
                self._packet_out(datapath, msg.data, in_port, out_port)
                return

        # Otherwise, drop by default (table-miss sends to controller; do nothing)

    #
    # ---- Topology events
    #

    @set_ev_cls(topo_event.EventLinkAdd, MAIN_DISPATCHER)
    def link_add_handler(self, ev):
        link = ev.link
        # link.src and link.dst have dpid/port info
        s, sp = link.src.dpid, link.src.port_no
        d, dp = link.dst.dpid, link.dst.port_no

        self.graph.add_node(s)
        self.graph.add_node(d)
        self.graph.add_edge(s, d, src_dpid=s, dst_dpid=d, src_port=sp, dst_port=dp)
        self.logger.info("LINK ADD %s[%s] -> %s[%s]", s, sp, d, dp)

    @set_ev_cls(topo_event.EventLinkDelete, MAIN_DISPATCHER)
    def link_del_handler(self, ev):
        link = ev.link
        s, sp = link.src.dpid, link.src.port_no
        d, dp = link.dst.dpid, link.dst.port_no
        if self.graph.has_edge(s, d):
            self.graph.remove_edge(s, d)
            self.logger.info("LINK DEL %s[%s] -> %s[%s]", s, sp, d, dp)

    #
    # ---- Flow ops
    #

    def _add_flow(self, datapath, priority, match, actions,
                  idle_timeout=0, hard_timeout=0, buffer_id=None):
        ofp = datapath.ofproto
        parser = datapath.ofproto_parser
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        if buffer_id:
            mod = parser.OFPFlowMod(datapath=datapath, buffer_id=buffer_id,
                                    priority=priority, match=match,
                                    idle_timeout=idle_timeout, hard_timeout=hard_timeout,
                                    instructions=inst)
        else:
            mod = parser.OFPFlowMod(datapath=datapath, priority=priority,
                                    match=match, idle_timeout=idle_timeout,
                                    hard_timeout=hard_timeout, instructions=inst)
        datapath.send_msg(mod)

    def _packet_out(self, datapath, data, in_port, out_port):
        ofp = datapath.ofproto
        parser = datapath.ofproto_parser
        actions = [parser.OFPActionOutput(out_port)]
        out = parser.OFPPacketOut(datapath=datapath, buffer_id=ofp.OFP_NO_BUFFER,
                                  in_port=in_port, actions=actions, data=data)
        datapath.send_msg(out)

    def _install_path(self, src_mac, dst_mac, path_dpids, priority=100, idle_timeout=90):
        """
        Install symmetric flows for traffic between src_mac and dst_mac along
        the provided list of dpids (forward dir); reverse is installed by caller
        with reversed path.
        """
        if not path_dpids or len(path_dpids) < 1:
            return

        # Build hop out_ports from graph edges
        hops = []
        for i in range(len(path_dpids) - 1):
            u, v = path_dpids[i], path_dpids[i+1]
            if not self.graph.has_edge(u, v):
                self.logger.warning("Path edge missing in graph: %s -> %s", u, v)
                return
            e = self.graph[u][v]
            hops.append({'dpid': u, 'out_port': e.get('src_port')})

        # Tail (the last switch should deliver to host)
        last = path_dpids[-1]
        if dst_mac not in self.hosts or self.hosts[dst_mac]['dpid'] != last:
            self.logger.warning("Destination %s not at last dpid=%s; learned=%s",
                                dst_mac, last, self.hosts.get(dst_mac))
        else:
            hops.append({'dpid': last, 'out_port': self.hosts[dst_mac]['port']})

        # Install along hops
        for hop in hops:
            dpid = hop['dpid']
            out_port = hop['out_port']
            dp = self.datapaths.get(dpid)
            if not dp:
                self.logger.warning("Datapath %s not present, skipping", dpid)
                continue
            parser = dp.ofproto_parser
            actions = [parser.OFPActionOutput(out_port)]
            match = parser.OFPMatch(eth_src=src_mac, eth_dst=dst_mac)
            self._add_flow(dp, priority=priority, match=match,
                           actions=actions, idle_timeout=idle_timeout)

        key = (src_mac, dst_mac)
        self.routes[key] = {'path': list(path_dpids), 'ts': now()}
        self.last_action_ts[key] = now()
        self.logger.info("Installed path %s -> %s via %s", src_mac, dst_mac, path_dpids)

    #
    # ---- Paths
    #

    def _k_shortest_paths(self, src_dpid, dst_dpid, k=2):
        """Return list of up to k simple shortest paths (list of dpids)."""
        if src_dpid not in self.graph or dst_dpid not in self.graph:
            return []
        try:
            gen = nx.shortest_simple_paths(self.graph, src_dpid, dst_dpid)
            paths = []
            for p in gen:
                paths.append(p)
                if len(paths) >= k:
                    break
            return paths
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return []

    #
    # ---- Stats monitor
    #

    def _monitor(self):
        """Periodic poll of port stats."""
        while True:
            try:
                for dpid, dp in list(self.datapaths.items()):
                    self._send_port_stats(dp)
                # Process replies will arrive async and update self.port_stats
            except Exception as e:
                self.logger.warning("monitor loop error: %s", e)
            hub.sleep(1.0)

    def _send_port_stats(self, datapath):
        ofp = datapath.ofproto
        parser = datapath.ofproto_parser
        req = parser.OFPPortStatsRequest(datapath, 0, ofp.OFPP_ANY)
        datapath.send_msg(req)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply_handler(self, ev):
        body = ev.msg.body
        ts = now()
        dpid = ev.msg.datapath.id
        for stat in body:
            p = stat.port_no
            # Skip local port
            if p >= 0xFFFFFF00:
                continue
            entry = self.port_stats[(dpid, p)]
            entry['rx_packets'] = stat.rx_packets
            entry['tx_packets'] = stat.tx_packets
            entry['rx_bytes'] = stat.rx_bytes
            entry['tx_bytes'] = stat.tx_bytes
            entry['rx_dropped'] = stat.rx_dropped
            entry['tx_dropped'] = stat.tx_dropped
            entry['rx_errors'] = stat.rx_errors
            entry['tx_errors'] = stat.tx_errors
            entry['timestamp'] = ts

    #
    # ---- REST data helpers
    #

    def serialize_graph_links(self):
        links = []
        for u, v, data in self.graph.edges(data=True):
            links.append({
                'src_dpid': u,
                'dst_dpid': v,
                'src_port': data.get('src_port'),
                'dst_port': data.get('dst_port'),
            })
        return links

    def serialize_hosts(self):
        return [
            {'mac': mac, 'dpid': data['dpid'], 'port': data['port']}
            for mac, data in self.hosts.items()
        ]

    def serialize_paths(self, src_mac, dst_mac, k):
        if src_mac not in self.hosts or dst_mac not in self.hosts:
            return []
        sdp = self.hosts[src_mac]['dpid']
        ddp = self.hosts[dst_mac]['dpid']
        paths = self._k_shortest_paths(sdp, ddp, k=k)
        out = []
        for pid, p in enumerate(paths):
            hops = []
            for i in range(len(p) - 1):
                e = self.graph[p[i]][p[i+1]]
                hops.append({'dpid': p[i], 'out_port': e.get('src_port')})
            # Tail hop is to host port (if known)
            if dst_mac in self.hosts and self.hosts[dst_mac]['dpid'] == p[-1]:
                hops.append({'dpid': p[-1], 'out_port': self.hosts[dst_mac]['port']})
            out.append({'path_id': pid, 'dpids': p, 'hops': hops})
        return out


#
# -------- REST layer
#

# JSON schemas
ROUTE_SCHEMA = {
    "type": "object",
    "anyOf": [
        {"required": ["src_mac", "dst_mac", "path_id"]},
        {"required": ["src_mac", "dst_mac", "path"]},
    ],
    "properties": {
        "src_mac": {"type": "string"},
        "dst_mac": {"type": "string"},
        "path_id": {"type": "integer", "minimum": 0},
        "k": {"type": "integer", "minimum": 1, "default": 2},
        "path": {
            "type": "array",
            "items": {"type": "integer"},
            "minItems": 1
        }
    },
    "additionalProperties": False
}


class RestApi(ControllerBase):
    def __init__(self, req, link, data, **config):
        super().__init__(req, link, data, **config)
        self.app: SdnRouterApp = data[API_INSTANCE]

    @route('health', '/api/v1/health', methods=['GET'])
    def health(self, req, **kwargs):
        return j({
            "status": "ok",
            "last_stats_ts": max((v['timestamp'] for v in self.app.port_stats.values()), default=0.0),
        })

    @route('graph', '/api/v1/graph', methods=['GET'])
    def graph(self, req, **kwargs):
        return j({
            "nodes": list(self.app.graph.nodes()),
            "links": self.app.serialize_graph_links(),
        })

    @route('hosts', '/api/v1/hosts', methods=['GET'])
    def hosts(self, req, **kwargs):
        return j(self.app.serialize_hosts())

    @route('paths', '/api/v1/paths', methods=['GET'])
    def paths(self, req, **kwargs):
        params = req.GET
        src = params.get('src_mac')
        dst = params.get('dst_mac')
        try:
            k = int(params.get('k', 2))
        except Exception:
            k = 2

        if not src or not dst:
            return j({'error': 'missing_params', 'detail': 'src_mac & dst_mac required'}, 400)

        return j(self.app.serialize_paths(src, dst, k))

    @route('ports', '/api/v1/stats/ports', methods=['GET'])
    def stats_ports(self, req, **kwargs):
        # Response shape: list of dicts for convenience
        out = []
        for (dpid, port), v in self.app.port_stats.items():
            row = {
                'dpid': dpid,
                'port_no': port,
                'rx_packets': v['rx_packets'],
                'tx_packets': v['tx_packets'],
                'rx_bytes': v['rx_bytes'],
                'tx_bytes': v['tx_bytes'],
                'rx_dropped': v['rx_dropped'],
                'tx_dropped': v['tx_dropped'],
                'rx_errors': v['rx_errors'],
                'tx_errors': v['tx_errors'],
                'timestamp': v['timestamp'],
            }
            out.append(row)
        return j(out)

    # RENAMED: avoid shadowing the @route decorator
    @route('action_route', '/api/v1/actions/route', methods=['POST'])
    def apply_route(self, req, **kwargs):
        # Body variants supported:
        #   {"src_mac","dst_mac","path_id","k"}
        #   {"src_mac","dst_mac","path":[dpids...]}
        try:
            data = json.loads(req.body)
            validate(instance=data, schema=ROUTE_SCHEMA)
        except ValidationError as ve:
            return j({'error': 'validation', 'detail': ve.message}, 400)
        except Exception:
            return j({'error': 'invalid_json'}, 400)

        s, d = data['src_mac'], data['dst_mac']
        if s not in self.app.hosts or d not in self.app.hosts:
            return j({'error': 'hosts_not_learned'}, 404)

        # Resolve path
        chosen_path = data.get('path')
        if not chosen_path:
            sdp = self.app.hosts[s]['dpid']
            ddp = self.app.hosts[d]['dpid']
            all_paths = self.app._k_shortest_paths(sdp, ddp, k=int(data.get('k', 2)))
            if not all_paths:
                return j({'error': 'no_path'}, 409)
            pid = min(int(data.get('path_id', 0)), len(all_paths) - 1)
            chosen_path = all_paths[pid]

        key = (s, d)
        prev = self.app.routes.get(key, {}).get('path')

        # Cooldown: only if trying to change an existing path
        if prev and prev != chosen_path:
            delta = now() - self.app.last_action_ts.get(key, 0.0)
            if delta < self.app.route_cooldown:
                retry = max(1, ceil(self.app.route_cooldown - delta))
                # IMPORTANT: standard Retry-After header + JSON field for compatibility
                return j({'error': 'cooldown_active', 'retry_after': retry},
                         status=429,
                         headers={'Retry-After': retry})

        # Forward direction s->d along chosen_path
        self.app._install_path(s, d, chosen_path)
        # Reverse direction d->s along reversed path
        self.app._install_path(d, s, list(reversed(chosen_path)))

        return j({'status': 'applied', 'path': chosen_path})
