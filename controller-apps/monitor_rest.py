# controller-apps/monitor_rest.py
for stat in ev.msg.body:
# Skip local ports if desired (port_no == ofproto.OFPP_LOCAL)
stats.append({
    'timestamp': now,
    'dpid': ev.msg.datapath.id,
    'port_no': stat.port_no,
    'rx_pkts': stat.rx_packets,
    'tx_pkts': stat.tx_packets,
    'rx_bytes': stat.rx_bytes,
    'tx_bytes': stat.tx_bytes,
    'rx_dropped': stat.rx_dropped,
    'tx_dropped': stat.tx_dropped,
    'rx_errors': stat.rx_errors,
    'tx_errors': stat.tx_errors,
})
# Replace snapshot for this dpid (simple strategy)
# Keep only last N seconds by timestamp if you want rolling window; for now, overwrite entire list
# Group per dpid and merge into single list
# For simplicity here, just append and trim last snapshot timestamp
self.port_stats = [s for s in self.port_stats if s.get('dpid') != ev.msg.datapath.id]
self.port_stats.extend(stats)
self.last_stats_ts = now


@set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
def flow_stats_reply_handler(self, ev):
    now = time.time()
flows = []
for stat in ev.msg.body:
    match = stat.match.to_jsondict().get('OFPMatch', {}).get('oxm_fields', [])
flows.append({
    'timestamp': now,
    'dpid': ev.msg.datapath.id,
    'priority': stat.priority,
    'table_id': stat.table_id,
    'duration_sec': stat.duration_sec,
    'packet_count': stat.packet_count,
    'byte_count': stat.byte_count,
    'match': match,
})
self.flow_stats = [f for f in self.flow_stats if f.get('dpid') != ev.msg.datapath.id]
self.flow_stats.extend(flows)
self.last_stats_ts = now




class StatsController(ControllerBase):
    def __init__(self, req, link, data, **config):
    super().__init__(req, link, data, **config)
self.app = data[API_INSTANCE_NAME]


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