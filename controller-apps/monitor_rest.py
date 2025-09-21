from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, DEAD_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib import hub
from ryu.app.wsgi import ControllerBase, WSGIApplication, route
from webob import Response
import json, time

def j(obj, status=200):
    return Response(
        content_type='application/json',
        body=json.dumps(obj).encode('utf-8'),
        status=status
    )

class MonitorRest(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS = {'wsgi': WSGIApplication}

    def __init__(self, *args, **kwargs):
        super(MonitorRest, self).__init__(*args, **kwargs)
        self.datapaths = {}
        self.latest_ports = []
        self.latest_flows = []
        self.last_stats_ts = 0.0
        self.monitor_thread = hub.spawn(self._monitor)
        wsgi = kwargs['wsgi']
        wsgi.register(StatsController, {'app': self})

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change_handler(self, ev):
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[dp.id] = dp
            self.logger.info('DP joined: %016x', dp.id)
        elif ev.state == DEAD_DISPATCHER:
            self.datapaths.pop(dp.id, None)
            self.logger.info('DP left: %016x', dp.id)

    def _monitor(self):
        while True:
            for dp in list(self.datapaths.values()):
                ofp = dp.ofproto
                parser = dp.ofproto_parser
                dp.send_msg(parser.OFPPortStatsRequest(dp, 0, ofp.OFPP_ANY))
                dp.send_msg(parser.OFPFlowStatsRequest(dp))
            hub.sleep(2)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats_reply_handler(self, ev):
        ts = time.time()
        self.last_stats_ts = ts
        stats = []
        for s in ev.msg.body:
            stats.append({
                'timestamp': ts, 'dpid': ev.msg.datapath.id, 'port_no': s.port_no,
                'rx_pkts': s.rx_packets, 'tx_pkts': s.tx_packets,
                'rx_bytes': s.rx_bytes, 'tx_bytes': s.tx_bytes,
                'rx_dropped': s.rx_dropped, 'tx_dropped': s.tx_dropped,
                'rx_errors': s.rx_errors, 'tx_errors': s.tx_errors
            })
        self.latest_ports = stats

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def _flow_stats_reply_handler(self, ev):
        ts = time.time()
        self.last_stats_ts = ts
        flows = []
        for s in ev.msg.body:
            match_kv = []
            m = getattr(s, 'match', None)
            try:
                if m and hasattr(m, 'items'):
                    for k, v in m.items():
                        match_kv.append({'field': k, 'value': v})
            except Exception:
                pass
            flows.append({
                'timestamp': ts, 'dpid': ev.msg.datapath.id,
                'priority': s.priority, 'table_id': s.table_id,
                'duration_sec': getattr(s, 'duration_sec', 0),
                'packet_count': s.packet_count, 'byte_count': s.byte_count,
                'match': match_kv
            })
        self.latest_flows = flows

class StatsController(ControllerBase):
    def __init__(self, req, link, data, **config):
        super(StatsController, self).__init__(req, link, data, **config)
        self.app = data['app']

    @route('health', '/api/v1/health', methods=['GET'])
    def health(self, req, **kwargs):

        # Always return valid JSON bytes for WebOb<=1.8.x

        data = {"status": "ok", "last_stats_ts": getattr(self, "last_stats_ts", 0.0)}

        payload = json.dumps(data)

        from webob import Response

        return Response(content_type="application/json", body=payload.encode("utf-8"))

    def ports(self, req, **kwargs):
        return j(self.app.latest_ports)

    @route('flows', '/api/v1/stats/flows', methods=['GET'])
    def flows(self, req, **kwargs):
        return j(self.app.latest_flows)
