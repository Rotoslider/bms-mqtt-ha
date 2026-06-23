#!/usr/bin/env python3
"""
Create Home Assistant alert automations for the battery packs via the REST API.

Config via environment (same as ha_dashboard.py):
  HA_URL, HA_TOKEN or HA_TOKEN_FILE, NUM_BATTERIES

Creates:
  - bms_cell_imbalance      : any pack's cell spread > DELTA_MV (default 120)
  - bms_cell_out_of_range   : any cell < CELL_MIN_MV (3000) or > CELL_MAX_MV (3650)
  - bms_battery_offline     : a pack stops reporting for OFFLINE_MIN (5) minutes

Alerts use persistent_notification. To notify your phone instead, change the
action service to e.g. notify.mobile_app_<your_device>.
"""
import json, os, urllib.request, urllib.error

HA_BASE = os.environ.get("HA_URL", "http://homeassistant.local:8123").rstrip("/")
TOKEN_FILE = os.environ.get("HA_TOKEN_FILE", "/opt/bms-mqtt/.ha_token")
TOK = os.environ.get("HA_TOKEN") or open(TOKEN_FILE).read().strip()
NUM = int(os.environ.get("NUM_BATTERIES", "8"))
DELTA_MV = int(os.environ.get("DELTA_MV", "120"))
CELL_MIN_MV = int(os.environ.get("CELL_MIN_MV", "3000"))
CELL_MAX_MV = int(os.environ.get("CELL_MAX_MV", "3650"))
OFFLINE_MIN = int(os.environ.get("OFFLINE_MIN", "5"))

N = range(1, NUM + 1)
delta = [f"sensor.battery_{i}_cell_delta" for i in N]
cmin = [f"sensor.battery_{i}_cell_min" for i in N]
cmax = [f"sensor.battery_{i}_cell_max" for i in N]
volt = [f"sensor.battery_{i}_voltage" for i in N]

NOTE = {"service": "persistent_notification.create"}


def autos():
    return {
        "bms_cell_imbalance": {
            "alias": "BMS - cell imbalance high",
            "description": f"A pack's cell spread exceeded {DELTA_MV} mV",
            "trigger": [{"platform": "numeric_state", "entity_id": delta,
                         "above": DELTA_MV, "for": {"minutes": 2}}],
            "action": [{**NOTE, "data": {
                "notification_id": "{{ trigger.entity_id }}",
                "title": "Battery cell imbalance",
                "message": "{{ trigger.to_state.attributes.friendly_name }} = "
                           "{{ trigger.to_state.state }} mV spread"}}],
            "mode": "single"},
        "bms_cell_out_of_range": {
            "alias": "BMS - cell voltage out of range",
            "description": f"A cell dropped below {CELL_MIN_MV} or rose above {CELL_MAX_MV} mV",
            "trigger": [
                {"platform": "numeric_state", "entity_id": cmin, "below": CELL_MIN_MV,
                 "for": {"minutes": 1}},
                {"platform": "numeric_state", "entity_id": cmax, "above": CELL_MAX_MV,
                 "for": {"minutes": 1}}],
            "action": [{**NOTE, "data": {
                "notification_id": "{{ trigger.entity_id }}",
                "title": "Battery cell voltage alert",
                "message": "{{ trigger.to_state.attributes.friendly_name }} = "
                           "{{ trigger.to_state.state }} mV"}}],
            "mode": "single"},
        "bms_battery_offline": {
            "alias": "BMS - battery offline",
            "description": f"A battery stopped reporting for {OFFLINE_MIN} minutes",
            "trigger": [{"platform": "state", "entity_id": volt, "to": "unavailable",
                         "for": {"minutes": OFFLINE_MIN}}],
            "action": [{**NOTE, "data": {
                "notification_id": "{{ trigger.entity_id }}",
                "title": "Battery offline",
                "message": "{{ trigger.to_state.attributes.friendly_name | default(trigger.entity_id) }}"
                           " has not reported in " + str(OFFLINE_MIN) + " min"}}],
            "mode": "single"},
    }


def post(path, body):
    req = urllib.request.Request(HA_BASE + path, data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + TOK, "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()[:200]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]


if __name__ == "__main__":
    for aid, cfg in autos().items():
        s, b = post(f"/api/config/automation/config/{aid}", cfg)
        print(f"{aid}: HTTP {s} {b}")
