#!/usr/bin/env python3
"""
PV MPPT-trap detector -> MQTT (Home Assistant auto-discovery).

Watches the inverter's PV telemetry (as published by Solar Assistant) for the
"stranded tracker" signature of a mixed-orientation series string: after a PV
cold-start in developed daylight, a perturb-and-observe MPPT can park on the
high-voltage local maximum where the whole string's current is capped by its
least-illuminated panels (here: two west-wall panels), silently costing ~75%
of production. Signature: string voltage near open-circuit (> trap_volts)
with only a few amps (< trap_amps), sustained, during hours when the main
array faces the sun.

Publishes a `binary_sensor` (device_class: problem) via MQTT discovery.
Recovery is manual by design — cover the least-illuminated panels for ~2
minutes (kills the false knee; the tracker walks down to the real MPP), or
cycle the PV disconnect and hope (non-deterministic). Neither interrupts
inverter output.

Run:
  python3 pv_trap_monitor.py --selftest    # exercise the detector, no MQTT
  python3 pv_trap_monitor.py               # run as a service
"""
import argparse, json, logging, os, sys, time
import paho.mqtt.client as mqtt

LOG = logging.getLogger("pvtrap")
CONFIG_PATH = os.environ.get("BMS_CONFIG", "/opt/bms-mqtt/config.json")

DEFAULTS = {
    "sa_prefix": "solar_assistant/inverter_1",
    "trap_volts": 240.0,        # ~82% of the 293 V string Voc; healthy MPP is 148-215 V
    "trap_amps": 5.0,           # healthy daytime string current is 8-12 A
    "hold_seconds": 600,        # sustained this long -> alarm (rides out clouds)
    "clear_seconds": 120,       # healthy this long -> all-clear
    "stale_seconds": 120,       # no fresh telemetry -> sensor unavailable
    "window_start_hour": 9,     # local hours when the main array MUST make >5 A;
    "window_end_hour": 17,      #   outside them dawn/dusk near-Voc idling is normal
    "local_utc_offset_hours": 0,  # host clocks often run UTC; e.g. -7 for Arizona
}


class TrapDetector:
    """Pure state machine: feed (volts, amps, local_hour, now) samples,
    read .trapped. Requires the condition to hold `hold_seconds` before
    alarming and to stay clear `clear_seconds` before releasing."""

    def __init__(self, m):
        self.m = m
        self.trapped = False
        self.cond_since = None
        self.ok_since = None

    def update(self, volts, amps, local_hour, now):
        m = self.m
        in_window = m["window_start_hour"] <= local_hour < m["window_end_hour"]
        cond = in_window and volts > m["trap_volts"] and amps < m["trap_amps"]
        if cond:
            self.ok_since = None
            if self.cond_since is None:
                self.cond_since = now
            if not self.trapped and now - self.cond_since >= m["hold_seconds"]:
                self.trapped = True
                return "TRAPPED"
        else:
            self.cond_since = None
            if self.ok_since is None:
                self.ok_since = now
            if self.trapped and now - self.ok_since >= m["clear_seconds"]:
                self.trapped = False
                return "CLEARED"
        return None


def run(cfg):
    m = dict(DEFAULTS, **cfg.get("pv_monitor", {}))
    base = cfg["mqtt"]["base_topic"]
    disc = cfg["mqtt"].get("discovery_prefix", "homeassistant")
    sa = m["sa_prefix"]
    # NOTE: state topic deliberately does NOT match `<base>/+/state` — that
    # wildcard is the emulator's per-pack subscription (phantom-pack hazard).
    t_state = f"{base}/pv_trap/status"
    t_attr = f"{base}/pv_trap/attributes"
    t_avail = f"{base}/pv_trap/availability"

    det = TrapDetector(m)
    last = {}                   # field -> (monotonic_ts, value)

    cli = mqtt.Client(client_id="pv-trap-monitor")
    if cfg["mqtt"].get("username"):
        cli.username_pw_set(cfg["mqtt"]["username"], cfg["mqtt"].get("password"))
    cli.will_set(t_avail, "offline", retain=True)

    def on_connect(c, u, f, rc):
        LOG.info("MQTT connected (rc=%s)", rc)
        for field in ("pv_voltage", "pv_current", "pv_power"):
            c.subscribe(f"{sa}/{field}/state")
        c.publish(f"{disc}/binary_sensor/pv_mppt_trap/config", json.dumps({
            "name": "PV MPPT trap",
            "unique_id": "pv_mppt_trap",
            "object_id": "pv_mppt_trap",
            "device_class": "problem",
            "state_topic": t_state,
            "availability_topic": t_avail,
            "json_attributes_topic": t_attr,
            "device": {"identifiers": ["pv_trap_monitor"],
                       "name": "PV array monitor",
                       "manufacturer": "bms-mqtt-ha"},
        }), retain=True)
        c.publish(t_avail, "online", retain=True)
        c.publish(t_state, "ON" if det.trapped else "OFF", retain=True)

    def on_message(c, u, msg):
        field = msg.topic.split("/")[-2]
        try:
            last[field] = (time.monotonic(), float(msg.payload))
        except ValueError:
            pass

    cli.on_connect = on_connect
    cli.on_message = on_message
    cli.connect(cfg["mqtt"]["host"], cfg["mqtt"]["port"], keepalive=60)
    cli.loop_start()

    avail = True
    while True:
        time.sleep(15)
        now = time.monotonic()
        fresh = {k: v for k, (ts, v) in last.items()
                 if now - ts <= m["stale_seconds"]}
        if "pv_voltage" not in fresh or "pv_current" not in fresh:
            if avail:
                LOG.warning("PV telemetry stale -> sensor unavailable")
                cli.publish(t_avail, "offline", retain=True)
                avail = False
            continue
        if not avail:
            LOG.info("PV telemetry back -> sensor online")
            cli.publish(t_avail, "online", retain=True)
            avail = True
        local_hour = (time.gmtime().tm_hour + m["local_utc_offset_hours"]) % 24
        edge = det.update(fresh["pv_voltage"], fresh["pv_current"],
                          local_hour, now)
        if edge:
            LOG.warning("%s: PV %.1fV %.1fA (%sW)", edge, fresh["pv_voltage"],
                        fresh["pv_current"], fresh.get("pv_power", "?"))
            cli.publish(t_state, "ON" if det.trapped else "OFF", retain=True)
        if edge or det.trapped:
            cli.publish(t_attr, json.dumps({
                "pv_voltage": fresh["pv_voltage"],
                "pv_current": fresh["pv_current"],
                "pv_power": fresh.get("pv_power"),
                "hint": "Cover the two west wall panels ~2 min, then uncover; "
                        "or cycle the PV disconnect (may take several tries). "
                        "House output is not affected.",
            }), retain=True)


def selftest():
    m = dict(DEFAULTS, hold_seconds=600, clear_seconds=120)
    d = TrapDetector(m)
    # healthy midday tracking: never alarms
    assert d.update(185.0, 11.2, 12, 0) is None and not d.trapped
    # trap condition must SUSTAIN hold_seconds before alarming
    assert d.update(271.0, 2.1, 12, 100) is None and not d.trapped
    assert d.update(269.0, 2.4, 12, 400) is None and not d.trapped
    assert d.update(270.0, 2.0, 12, 701) == "TRAPPED" and d.trapped
    # a brief healthy blip must not clear it...
    assert d.update(185.0, 11.0, 13, 800) is None and d.trapped
    # ...but condition returning resets the clear timer
    assert d.update(268.0, 2.2, 13, 850) is None and d.trapped
    # sustained healthy clears
    assert d.update(182.0, 11.5, 13, 900) is None and d.trapped
    assert d.update(181.0, 12.0, 13, 1021) == "CLEARED" and not d.trapped
    # cloud dip at proper MPP voltage never alarms (volts stay ~Vmp)
    d2 = TrapDetector(m)
    for t in range(0, 2000, 100):
        assert d2.update(190.0, 3.0, 12, t) is None and not d2.trapped
    # near-Voc idle OUTSIDE the window (dawn pre-start) never alarms
    d3 = TrapDetector(m)
    for t in range(0, 2000, 100):
        assert d3.update(291.0, 0.1, 6, t) is None and not d3.trapped
    # window edge: same signature inside the window alarms
    d4 = TrapDetector(m)
    d4.update(291.0, 0.1, 9, 0)
    assert d4.update(291.0, 0.1, 9, 601) == "TRAPPED"
    print("trap-detector self-test: PASS (hold/clear/window/cloud cases)")


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stdout)
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        selftest()
    else:
        run(json.load(open(CONFIG_PATH)))


if __name__ == "__main__":
    main()
