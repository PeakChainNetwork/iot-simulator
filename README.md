# IoT Simulator

A standalone telemetry simulator for the **IoT Security Platform**. It pretends
to be a set of industrial machines and **publishes telemetry over MQTT** to
PeakSoft's exposed broker, so you can **test the live platform with real data
flowing through it** — then watch the results in the [dashboard](https://github.com/PeakChainNetwork/iot-security-platform-demo).

It only *publishes* data; it contains none of the platform's backend or
detection logic.

## Run it (no Docker)

Requires **Python 3.10+**.

```bash
# 1. Virtual environment
python -m venv .venv
source .venv/bin/activate          # macOS/Linux
# .\.venv\Scripts\Activate.ps1     # Windows (PowerShell)

# 2. Dependencies
pip install -r requirements.txt

# 3. Configure — point it at PeakSoft's broker (host + credentials from PeakSoft)
cp .env.example .env             # then edit MQTT_HOST / MQTT_PASSWORD

# 4. Start it (connects to the broker and begins publishing)
uvicorn app.main:app --port 8001
```

On start it connects to the broker and publishes telemetry for each device in
`SIM_DEVICES` to `factory/<device_id>/telemetry`. A small control API runs on
**http://localhost:8001** (`/health`, `/state`, `/scenario`).

## Configuration (`.env`)

| Variable | Example | Purpose |
|---|---|---|
| `MQTT_HOST` | `broker.example` | Exposed broker host (from PeakSoft) |
| `MQTT_PORT` | `443` | Broker port (443 for WSS) |
| `MQTT_TRANSPORT` | `websockets` | `websockets` for WSS, or `tcp` for a local broker |
| `MQTT_WS_PATH` | `/mqtt` | WebSocket path |
| `MQTT_TLS` | `true` | Enable TLS (use with WSS) |
| `MQTT_USERNAME` / `MQTT_PASSWORD` | — | Credentials from PeakSoft |
| `SIM_DEVICES` | `dev-001,dev-002` | Device IDs to simulate |
| `SIM_DEFAULT_RATE_HZ` | `1.0` | Publish rate per device |

## Inject test scenarios

Drive anomalies/attacks to see the platform react, via the control API:

```bash
curl -X POST http://localhost:8001/scenario \
  -H "Content-Type: application/json" \
  -d '{"global_":{"scenario":"spike","enabled":true}}'
```

Scenarios: `spike`, `drift`, `flood`, `bad_payload`, `impersonation`.
