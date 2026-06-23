#!/usr/bin/env python3
"""
Create/replace a Home Assistant dashboard for the battery packs via the WebSocket API.

Config via environment:
  HA_URL         e.g. http://homeassistant.local:8123   (default)
  HA_TOKEN       long-lived access token  (or set HA_TOKEN_FILE)
  HA_TOKEN_FILE  path to a file containing the token     (default /opt/bms-mqtt/.ha_token)
  NUM_BATTERIES  how many packs (default 8)
  DASH_PATH      dashboard url_path (default battery-cells)

Example:
  HA_URL=http://<ha-ip>:8123 HA_TOKEN_FILE=/opt/bms-mqtt/.ha_token python3 ha_dashboard.py
"""
import asyncio, json, os
import websockets

HA_BASE = os.environ.get("HA_URL", "http://homeassistant.local:8123").rstrip("/")
WS = HA_BASE.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"
TOKEN_FILE = os.environ.get("HA_TOKEN_FILE", "/opt/bms-mqtt/.ha_token")
TOK = os.environ.get("HA_TOKEN") or open(TOKEN_FILE).read().strip()
URL_PATH = os.environ.get("DASH_PATH", "battery-cells")
NUM = int(os.environ.get("NUM_BATTERIES", "8"))


def battery_card(i):
    cells = [f"sensor.battery_{i}_cell_{c:02d}" for c in range(1, 17)]
    return {
        "type": "vertical-stack",
        "cards": [
            {"type": "entities", "title": f"Battery {i}", "entities": [
                {"entity": f"sensor.battery_{i}_voltage", "name": "Pack voltage"},
                {"entity": f"sensor.battery_{i}_soc", "name": "SOC"},
                {"entity": f"sensor.battery_{i}_current", "name": "Current"},
                {"entity": f"sensor.battery_{i}_power", "name": "Power"},
                {"entity": f"sensor.battery_{i}_temp_max", "name": "Temp (max)"},
                {"entity": f"sensor.battery_{i}_cell_min", "name": "Cell min"},
                {"entity": f"sensor.battery_{i}_cell_max", "name": "Cell max"},
                {"entity": f"sensor.battery_{i}_cell_delta", "name": "Cell delta"},
                {"entity": f"binary_sensor.battery_{i}_balancing", "name": "Balancing"},
                {"entity": f"binary_sensor.battery_{i}_problem", "name": "Problem"},
            ]},
            {"type": "history-graph", "title": f"Battery {i} cells (24h)",
             "hours_to_show": 24, "entities": cells},
        ],
    }


def build_config():
    overview = {"type": "vertical-stack", "cards": [
        {"type": "glance", "title": "Cell delta (mV) — imbalance watch", "show_state": True,
         "entities": [{"entity": f"sensor.battery_{i}_cell_delta", "name": f"Batt {i}"}
                      for i in range(1, NUM + 1)]},
        {"type": "glance", "title": "State of charge",
         "entities": [{"entity": f"sensor.battery_{i}_soc", "name": f"Batt {i}"}
                      for i in range(1, NUM + 1)]},
        {"type": "glance", "title": "Max temperature",
         "entities": [{"entity": f"sensor.battery_{i}_temp_max", "name": f"Batt {i}"}
                      for i in range(1, NUM + 1)]},
    ]}
    return {"title": "Batteries", "views": [
        {"title": "Overview", "path": "overview", "icon": "mdi:view-dashboard",
         "cards": [overview]},
        {"title": "Per battery", "path": "per-battery", "icon": "mdi:battery",
         "cards": [battery_card(i) for i in range(1, NUM + 1)]},
    ]}


async def main():
    async with websockets.connect(WS, max_size=None) as ws:
        await ws.recv()  # auth_required
        await ws.send(json.dumps({"type": "auth", "access_token": TOK}))
        if json.loads(await ws.recv())["type"] != "auth_ok":
            print("AUTH FAILED — check HA_URL / token"); return
        mid = 0

        async def cmd(payload):
            nonlocal mid
            mid += 1; payload["id"] = mid
            await ws.send(json.dumps(payload))
            while True:
                r = json.loads(await ws.recv())
                if r.get("id") == mid and r.get("type") == "result":
                    return r

        r = await cmd({"type": "lovelace/dashboards/create", "url_path": URL_PATH,
                       "title": "Batteries", "mode": "storage",
                       "show_in_sidebar": True, "icon": "mdi:battery-high"})
        print("create dashboard:", r.get("success"), r.get("error", {}).get("message", ""))
        r = await cmd({"type": "lovelace/config/save", "url_path": URL_PATH,
                       "config": build_config()})
        print("save config:", r.get("success"), r.get("error", {}).get("message", ""))


if __name__ == "__main__":
    asyncio.run(main())
