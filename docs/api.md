# Controller REST API
Base URL: `http://<controller_ip>:<port>/api/v1` (default port 8080)

## Health
`GET /health` → `{"status":"ok","last_stats_ts": 1725312345.12}`

## Stats
- `GET /stats/ports` → latest per-port counters (one record per dpid/port)
- `GET /stats/flows` → latest per-flow counters

## Topology & Hosts
- `GET /topology/nodes` → list of switch dpids
- `GET /topology/links` → directed links with ports (`src_dpid,dst_dpid,src_port,dst_port`)
- `GET /hosts` → learned hosts (`mac,dpid,port`)

## Paths
`GET /paths?src_mac=<mac>&dst_mac=<mac>&k=2` → up to `k` candidate paths
```json
[
  {
    "path_id": 0,
    "dpids": [1,3,5],
    "hops": [ {"dpid":1,"out_port":2}, {"dpid":3,"out_port":1}, {"dpid":5,"out_port":3} ]
  }
]
