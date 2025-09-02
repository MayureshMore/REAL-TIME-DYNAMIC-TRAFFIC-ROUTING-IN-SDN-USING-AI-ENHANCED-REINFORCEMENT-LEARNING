# controller-apps/monitor_rest.py
# Ryu app: Simple L2 learning switch + periodic port/flow stats polling + REST API (/api/v1/health, /api/v1/stats/ports, /api/v1/stats/flows)


from collections import defaultdict
import json
import time


from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, DEAD_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ether_types
from ryu.lib import hub


from ryu.app.wsgi import WSGIApplication, ControllerBase, route
from webob import Response




API_INSTANCE_NAME = 'monitor_rest_api'




class SimpleSwitchMonitor(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]


_CONTEXTS = {
    'wsgi': WSGIApplication,
}


def __init__(self, *args, **kwargs):
    super().__init__(*args, **kwargs)
self.logger.info("Starting SimpleSwitchMonitor with REST API")


# MAC learning
self.mac_to_port = defaultdict(dict) # dpid -> {mac: port}


# Datapath registry
self.datapaths = {}


# Stats storage (latest snapshots)
self.port_stats = [] # list of dicts
self.flow_stats = [] # list of dicts
self.last_stats_ts = 0.0


# Polling
self.monitor_interval = 5 # seconds
self.monitor_thread = hub.spawn(self._monitor)


# REST wiring
wsgi = kwargs['wsgi']
wsgi.register(StatsController, {API_INSTANCE_NAME: self})


# ---------------------------
# Switch connection lifecycle
# ---------------------------
@set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
def switch_features_handler(self, ev):
    dp = ev.msg.datapath
ofp = dp.ofproto
parser = dp.ofproto_parser


# table-miss flow
match = parser.OFPMatch()
actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
mod = parser.OFPFlowMod(datapath=dp, priority=0, match=match, instructions=inst)
dp.send_msg(mod)
self.logger.info("Installed table-miss on dpid=%s", dp.id)


@set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
def state_change_handler(self, ev):
    dp = ev.datapath
if ev.state == MAIN_DISPATCHER:
    if dp.id not in self.datapaths:
    self.datapaths[dp.id] = dp
self.logger.info("Datapath registered: dpid=%s", dp.id)
elif ev.state == DEAD_DISPATCHER:
if dp and dp.id in self.datapaths:
    del self.datapaths[dp.id]
self.logger.info("Datapath unregistered: dpid=%s", dp.id)


# ---------------------------
# L2 learning switch behavior
# ---------------------------
return Response(content_type='application/json', body=json.dumps(self.app.flow_stats))