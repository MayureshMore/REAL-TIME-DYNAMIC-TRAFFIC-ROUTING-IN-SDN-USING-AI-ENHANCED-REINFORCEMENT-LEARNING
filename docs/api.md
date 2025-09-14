# Controller REST API
Base URL: `http://<controller_ip>:8080/api/v1`

## GET /health
Returns controller liveness and last stats timestamp.

### Response
```json
{ "status": "ok", "last_stats_ts": 1725312345.12 }
