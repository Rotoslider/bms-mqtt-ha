#!/usr/bin/env python3
"""
Pylontech LV RS485 battery emulator.

Presents the 8 parallel packs (read by bms_mqtt.py and published to MQTT) as ONE
virtual Pylontech battery to a Sungold/SRNE-class inverter set to BMS protocol PYL.
Answers the inverter's 6x-branch polls (CID2 0x61 analog, 0x62 alarms, 0x63
charge/discharge management). Frame layout is replicated byte-for-byte from the
reference emulator Fahmula/esphome-pylontech-rs485 (verified against SRNE).

SAFETY:
  * Read-only toward the batteries; only WRITES to the inverter's RS485 BMS port.
  * `listen_only` (config) logs what it WOULD send without transmitting — use it to
    sniff the inverter's polls / confirm wiring before going live.
  * If too few packs have fresh data, it stops responding (fail-safe): the inverter
    raises a BMS-comms fault and halts charge/discharge rather than acting on stale data.
  * Charge/discharge limits are derived conservatively from the WORST cell and worst
    temperature across all packs. Review the thresholds in config.json["emulator"].

Run:
  python3 pylon_emulator.py --selftest      # validate codec, print sample frames, no serial
  python3 pylon_emulator.py                 # run (honors listen_only in config)
"""
import argparse, json, logging, os, sys, threading, time
import paho.mqtt.client as mqtt

LOG = logging.getLogger("pylon")
CONFIG_PATH = os.environ.get("BMS_CONFIG", "/opt/bms-mqtt/config.json")
SOI, EOI = 0x7E, 0x0D
VER, ADR, CID1 = 0x20, 0x02, 0x46   # response constants (CID2 in responses is always 00)


# ---------------------------------------------------------------------------
# Pylontech frame codec (matches reference exactly)
# ---------------------------------------------------------------------------
def length_field(info_len: int) -> bytes:
    n = ((info_len >> 8) & 0xF) + ((info_len >> 4) & 0xF) + (info_len & 0xF)
    lchk = (~n & 0x0F) + 1
    return b"%1X%03X" % (lchk & 0xF, info_len)


def checksum(frame_data: bytes) -> bytes:
    s = sum(frame_data) & 0xFFFF
    return b"%04X" % ((~s + 1) & 0xFFFF)


def build_response(info_ascii: bytes) -> bytes:
    """Wrap an INFO payload (ASCII-hex bytes) in a full response frame."""
    frame_data = b"%02X%02X" % (VER, ADR) + b"4600" + length_field(len(info_ascii)) + info_ascii
    return bytes([SOI]) + frame_data + checksum(frame_data) + bytes([EOI])


def decode_frame(frame: bytes) -> dict:
    """Parse a raw frame (SOI..EOI). Returns dict incl. cid2 hex and checksum validity."""
    body = frame[1:-1]            # strip SOI / EOI
    data, chk = body[:-4], body[-4:]
    valid = checksum(data) == chk
    out = {"valid": valid, "raw": frame.decode("ascii", "replace")}
    if len(data) >= 8:
        out["ver"] = data[0:2].decode()
        out["adr"] = data[2:4].decode()
        out["cid1"] = data[4:6].decode()
        out["cid2"] = data[6:8].decode()
    return out


def u16(v) -> bytes:
    return b"%04X" % (int(round(v)) & 0xFFFF)


def u8(v) -> bytes:
    return b"%02X" % (int(round(v)) & 0xFF)


# ---------------------------------------------------------------------------
# Aggregation of the 8 packs into one virtual battery
# ---------------------------------------------------------------------------
class Aggregator:
    def __init__(self, cfg):
        self.cfg = cfg
        self.packs = {}          # bid -> (timestamp, state dict)
        self.lock = threading.Lock()

    def update(self, bid, state):
        with self.lock:
            self.packs[bid] = (time.monotonic(), state)

    def fresh(self):
        win = self.cfg["freshness_seconds"]
        now = time.monotonic()
        with self.lock:
            return [s for (ts, s) in self.packs.values() if now - ts <= win]

    def virtual(self):
        """Return aggregated virtual-battery dict, or None if not enough fresh data."""
        packs = self.fresh()
        if len(packs) < self.cfg["min_fresh_batteries"]:
            return None

        def vals(key, default=None):
            return [p[key] for p in packs if key in p and p[key] is not None] or \
                   ([default] if default is not None else [])

        v = vals("voltage"); cur = vals("current"); soc = vals("soc")
        cmax = vals("cell_max_mv"); cmin = vals("cell_min_mv")
        tmax = vals("temp_max"); tmin = vals("temp_min")
        cyc = vals("cycles", 0)
        rem = vals("remaining_ah", 0); nom = vals("nominal_ah", 0)
        problems = [p.get("problem") == "ON" for p in packs]
        # a pack whose BMS opened its charge/discharge FET => that path is blocked
        chg_fets = [p.get("fet_charge", "ON") == "ON" for p in packs]
        dis_fets = [p.get("fet_discharge", "ON") == "ON" for p in packs]

        if not (v and cur and soc and cmax and cmin and tmax and tmin):
            return None
        temps = tmax + tmin
        return {
            "voltage": sum(v) / len(v),
            "current": sum(cur),                      # parallel banks add
            "soc": round(sum(soc) / len(soc)),
            "cell_max_mv": max(cmax),
            "cell_min_mv": min(cmin),
            "temp_max": max(tmax),
            "temp_min": min(tmin),
            "temp_avg": sum(temps) / len(temps),
            "cycles": max(cyc),
            "remaining_ah": sum(rem),
            "nominal_ah": sum(nom),
            "n_fresh": len(packs),
            "any_problem": any(problems),
            "charge_blocked": not all(chg_fets),
            "discharge_blocked": not all(dis_fets),
        }


# ---------------------------------------------------------------------------
# The "brain": derive conservative limits from the worst cell / temp
# ---------------------------------------------------------------------------
def compute_limits(va, e):
    """va = virtual battery dict, e = emulator config. Returns a limits dict."""
    cmax, cmin = va["cell_max_mv"], va["cell_min_mv"]
    tmax, tmin = va["temp_max"], va["temp_min"]

    def taper(x, x0, x1, y0, y1):
        if x <= min(x0, x1):
            return y0 if x0 < x1 else y1
        if x >= max(x0, x1):
            return y1 if x0 < x1 else y0
        return y0 + (y1 - y0) * (x - x0) / (x1 - x0)

    # Charge current: full until taper_start, ramp to ccl_min by taper_end
    ccl = taper(cmax, e["cell_chg_taper_start_mv"], e["cell_chg_taper_end_mv"],
                e["ccl_max_amps"], e["ccl_min_amps"])
    # Discharge current: full until dis_taper_start (falling), ramp down to dcl_min
    dcl = taper(cmin, e["cell_dis_taper_start_mv"], e["cell_dis_taper_end_mv"],
                e["dcl_max_amps"], e["dcl_min_amps"])

    charge_ok = True
    discharge_ok = True
    if cmax >= e["cell_overvolt_mv"]:
        charge_ok = False
    if cmin <= e["cell_undervolt_mv"]:
        discharge_ok = False
    if not (e["charge_temp_min_c"] <= tmin and tmax <= e["charge_temp_max_c"]):
        charge_ok = False
    if not (e["discharge_temp_min_c"] <= tmin and tmax <= e["discharge_temp_max_c"]):
        discharge_ok = False
    if va["any_problem"] or va["charge_blocked"]:
        charge_ok = False
    if va["any_problem"] or va["discharge_blocked"]:
        discharge_ok = False

    if not charge_ok:
        ccl = 0.0
    if not discharge_ok:
        dcl = 0.0

    return {
        "cvl_v": e["cvl_volts"],
        "dvl_v": e["dvl_volts"],
        "ccl_a": max(0.0, ccl),
        "dcl_a": max(0.0, dcl),
        "charge_enable": charge_ok,
        "discharge_enable": discharge_ok,
        "force_charge": va["soc"] <= e["force_charge_soc"],
    }


# ---------------------------------------------------------------------------
# Response builders (CID2 0x61 / 0x62 / 0x63), payloads per reference source
# ---------------------------------------------------------------------------
def dk(celsius):                      # deci-Kelvin
    return int(round((celsius + 273.15) * 10))


def info_61(va):
    t = dk(va["temp_avg"]); tmx = dk(va["temp_max"]); tmn = dk(va["temp_min"])
    cur_ca = int(round(va["current"] * 100)) & 0xFFFF      # centi-amps, 2's-comp
    fields = [
        u16(va["voltage"] * 1000), u16(cur_ca), u8(va["soc"]),
        u16(va["cycles"]), u16(va["cycles"]), u8(100), u8(100),     # cycles x2, SOH x2
        u16(va["cell_max_mv"]), u16(0x0101), u16(va["cell_min_mv"]), u16(0x0108),
        u16(t), u16(tmx), u16(0x0103), u16(tmn), u16(0x0109),       # cell temps
        u16(t), u16(tmx), u16(0x0101), u16(tmn), u16(0x0101),       # mosfet temps
        u16(t), u16(tmx), u16(0x0101), u16(tmn), u16(0x0101),       # bms temps
    ]
    return b"".join(fields)


def info_62(va, e):
    a1 = a2 = p1 = p2 = 0
    cmax, cmin = va["cell_max_mv"], va["cell_min_mv"]
    if cmax >= e["cell_overvolt_mv"] - 50:
        a1 |= (1 << 5)                                   # cell voltage high alarm
    if cmin <= e["cell_undervolt_mv"] + 50:
        a1 |= (1 << 4)                                   # cell voltage low alarm
    if va["temp_max"] >= e["charge_temp_max_c"]:
        a1 |= (1 << 3)                                   # cell temp high alarm
    if va["temp_min"] <= e["charge_temp_min_c"]:
        a1 |= (1 << 2)                                   # cell temp low alarm
    if cmax >= e["cell_overvolt_mv"]:
        p1 |= (1 << 5)                                   # cell overvoltage protection
    if cmin <= e["cell_undervolt_mv"]:
        p1 |= (1 << 4)                                   # cell undervoltage protection
    if va["any_problem"]:
        p2 |= (1 << 3)                                   # system fault
    return u8(a1) + u8(a2) + u8(p1) + u8(p2)


def info_63(lim):
    status = 0
    if lim["charge_enable"]:
        status |= (1 << 7)
    if lim["discharge_enable"]:
        status |= (1 << 6)
    if lim["force_charge"]:
        status |= (1 << 5)
    return (u16(lim["cvl_v"] * 1000) + u16(lim["dvl_v"] * 1000) +
            u16(lim["ccl_a"] * 10) + u16(lim["dcl_a"] * 10) + u8(status))


# ---------------------------------------------------------------------------
# Serial loop
# ---------------------------------------------------------------------------
def run(cfg):
    import serial
    e = cfg["emulator"]
    agg = Aggregator(e)

    # MQTT: subscribe to per-pack state
    base = cfg["mqtt"]["base_topic"]
    cli = mqtt.Client(client_id="pylon-emulator")
    if cfg["mqtt"].get("username"):
        cli.username_pw_set(cfg["mqtt"]["username"], cfg["mqtt"].get("password"))

    def on_connect(c, u, f, rc):
        c.subscribe(f"{base}/+/state")
        LOG.info("MQTT connected (rc=%s), subscribed %s/+/state", rc, base)

    def on_message(c, u, msg):
        try:
            bid = msg.topic.split("/")[1]
            agg.update(bid, json.loads(msg.payload))
        except Exception as ex:
            LOG.debug("bad msg on %s: %s", msg.topic, ex)

    cli.on_connect = on_connect
    cli.on_message = on_message
    cli.connect(cfg["mqtt"]["host"], cfg["mqtt"]["port"], keepalive=60)
    cli.loop_start()

    listen_only = e.get("listen_only", True)
    ser = serial.Serial(e["serial_port"], e["baud"], timeout=0.2)
    LOG.info("Serial open %s @ %d 8N1 | listen_only=%s", e["serial_port"], e["baud"], listen_only)
    LOG.warning("LISTEN-ONLY: logging polls, NOT transmitting." if listen_only
                else "LIVE: responding to inverter polls.")

    buf = bytearray()
    last_summary = 0
    while True:
        chunk = ser.read(256)
        if chunk:
            for byte in chunk:
                if byte == SOI:
                    buf.clear(); buf.append(byte)
                elif buf:
                    buf.append(byte)
                    if byte == EOI:
                        handle_frame(bytes(buf), ser, agg, e, listen_only)
                        buf.clear()
        # periodic state summary
        now = time.monotonic()
        if now - last_summary > 30:
            last_summary = now
            va = agg.virtual()
            if va:
                lim = compute_limits(va, e)
                LOG.info("virtual: %.2fV %.1fA SOC%d%% cell %d-%dmV t%.0f-%.0fC | "
                         "CVL %.1f CCL %.0f DCL %.0f chg=%d dis=%d (packs=%d)",
                         va["voltage"], va["current"], va["soc"], va["cell_min_mv"],
                         va["cell_max_mv"], va["temp_min"], va["temp_max"], lim["cvl_v"],
                         lim["ccl_a"], lim["dcl_a"], lim["charge_enable"],
                         lim["discharge_enable"], va["n_fresh"])
            else:
                LOG.warning("virtual: NO FRESH DATA -> would fail-safe (not respond)")


def handle_frame(frame, ser, agg, e, listen_only):
    d = decode_frame(frame)
    cid2 = d.get("cid2", "??")
    LOG.info("RX %s cid2=%s valid=%s", d["raw"].strip(), cid2, d.get("valid"))
    if not d.get("valid"):
        LOG.warning("  bad checksum -> ignoring")
        return
    if cid2 not in ("61", "62", "63"):
        LOG.info("  cid2 %s not handled -> ignoring", cid2)
        return

    va = agg.virtual()
    if va is None:
        LOG.warning("  no fresh battery data -> NOT responding (fail-safe)")
        return
    lim = compute_limits(va, e)
    if cid2 == "61":
        resp = build_response(info_61(va))
    elif cid2 == "62":
        resp = build_response(info_62(va, e))
    else:
        resp = build_response(info_63(lim))
        LOG.info("  -> CID63 CVL=%.1fV CCL=%.0fA DCL=%.0fA chg=%d dis=%d force=%d",
                 lim["cvl_v"], lim["ccl_a"], lim["dcl_a"], lim["charge_enable"],
                 lim["discharge_enable"], lim["force_charge"])
    if listen_only:
        LOG.info("  WOULD SEND: %s", resp.decode("ascii", "replace").strip())
    else:
        ser.write(resp)
        LOG.info("  TX %s", resp.decode("ascii", "replace").strip())


# ---------------------------------------------------------------------------
def selftest():
    # 1) Verify checksum/length against the two known-good frames from the spec.
    assert checksum(b"0002464F0000") == b"FD9A", checksum(b"0002464F0000")
    assert checksum(b"20024600E00200") == b"FD3B", checksum(b"20024600E00200")
    assert length_field(2) == b"E002", length_field(2)
    assert length_field(0) == b"0000", length_field(0)
    print("codec self-test: PASS (checksums + length field match spec)")

    # 2) Build a sample 0x63 with a representative virtual battery + limits.
    e = json.load(open(CONFIG_PATH))["emulator"]
    va = {"voltage": 53.2, "current": 12.5, "soc": 90, "cell_max_mv": 3360,
          "cell_min_mv": 3300, "temp_max": 28.0, "temp_min": 25.0, "temp_avg": 26.5,
          "cycles": 20, "remaining_ah": 720, "nominal_ah": 800, "n_fresh": 8,
          "any_problem": False, "charge_blocked": False, "discharge_blocked": False}
    lim = compute_limits(va, e)
    for cid, payload in (("61", info_61(va)), ("62", info_62(va, e)), ("63", info_63(lim))):
        fr = build_response(payload)
        d = decode_frame(fr)
        assert d["valid"], f"self-built {cid} failed its own checksum!"
        print(f"sample CID{cid} frame: {fr.decode().strip()}  (valid={d['valid']})")
    print(f"sample limits: CVL={lim['cvl_v']}V CCL={lim['ccl_a']}A DCL={lim['dcl_a']}A "
          f"chg={lim['charge_enable']} dis={lim['discharge_enable']}")
    print("frame round-trip self-test: PASS")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stdout)
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    cfg = json.load(open(CONFIG_PATH))
    if args.selftest:
        selftest()
    else:
        run(cfg)


if __name__ == "__main__":
    main()
