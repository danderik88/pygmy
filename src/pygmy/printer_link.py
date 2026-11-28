"""One MQTT connection per printer, exposing the status fields server.py reads."""
import hashlib
import json
import re
import ssl
import sys
import threading
import time
from pathlib import Path

STALE_S = 30  # No report for this long means the printer is considered disconnected. P1/A1 firmware stalls its MQTT stream for up to ~10 s with the link healthy (measured: 0% ping loss, reports 4-10 s apart); a dead TCP link is caught by the MQTT keepalive instead.
STALE_PUSH_S = 5  # Ask for a fresh report after this much silence, and at most this often.
STALE_AFTER_S = STALE_S  # Alias kept for tests that import it.
USER_ACTION_CODES = {(0x0300, 0x400C), (0x0500, 0x400E)}  # Cancel echoes, not faults.
FIRST_REAL_ERROR = 0x4000  # Lower values are status indicators some firmware sends.
PUSHALL = {"pushing": {"command": "pushall", "sequence_id": "0"}}
GET_VERSION = {"info": {"command": "get_version", "sequence_id": "0"}}
VERSION_RETRY_S = 6 * 3600  # Re-ask the firmware version after this much silence.
# A BMCU answers get_version with this made-up serial, the same on every unit: not an identity.
FAKE_SERIALS = {"study0only"}


def real_serial(sn):
    """The serial if it can identify one AMS, else None."""
    return sn if isinstance(sn, str) and sn.strip() and sn.lower() not in FAKE_SERIALS else None


AMS_MODULES = {"ams": "AMS", "n3f": "AMS 2 Pro", "n3s": "AMS HT", "ams_f1": "AMS Lite"}
DRY_MODELS = ("AMS 2 Pro", "AMS HT")  # The only AMS units with a heater.
AMS_MODEL_BY_INFO_TYPE = {1: "AMS", 2: "AMS Lite", 3: "AMS 2 Pro", 4: "AMS HT"}


def ams_model_from_info(info):
    """Model of an AMS unit from its info hex field (type = low nibble), None when absent/invalid."""
    if not isinstance(info, str) or not info:
        return None
    try:
        return AMS_MODEL_BY_INFO_TYPE.get(int(info, 16) & 0x0F)
    except ValueError:
        return None


class CertificateChanged(Exception):
    """The printer shows a certificate other than the one seen the first time."""


def check_fingerprint(directory, alias, der):
    """Remember the certificate on first sight and refuse any other afterwards."""
    digest = hashlib.sha256(der).hexdigest()
    path = Path(directory) / alias
    if not path.exists():
        path.write_text(digest)
    elif path.read_text().strip() != digest:
        raise CertificateChanged(alias)


def merge(known, update):
    """Deep-merge a report: absent keys keep their value."""
    # ponytail: lists are replaced whole, per-tray AMS merging when an AMS printer joins (plan B8).
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(known.get(key), dict):
            merge(known[key], value)
        else:
            known[key] = value


def as_int(value):
    try:
        return int(value, 16) if isinstance(value, str) and value.lower().startswith("0x") else int(value)
    except (TypeError, ValueError):
        return None


def as_bits(value):
    """Bitfield from a printer hex string like fun/cfg; None when it isn't one."""
    try:
        return int(value, 16)
    except (TypeError, ValueError):
        return None


# ponytail: option bit numbers from BambuStudio/OrcaSlicer DeviceManager.cpp.
# Newer printers (P2S, H2*) send fun and cfg; home_flag is the older fallback.
def report_options(raw, hf):
    """Switches the app may offer, and their state, as the slicer reads them."""
    fun, cfg = as_bits(raw.get("fun")), as_bits(raw.get("cfg"))
    sound_in, tangle_in = fun is None or bool(fun & 0x100), fun is None or bool(fun & 0x200)
    if cfg is not None:
        options = {"auto_recovery": bool(cfg & 0x10000)}
        if sound_in:
            options["sound_enable"] = bool(cfg & 0x400000)
        if tangle_in:
            options["filament_tangle_detect"] = bool(cfg & 0x800000)
        return options
    if raw.get("home_flag") is None:
        return {}
    options = {"auto_recovery": bool(hf & 0x10)}
    if sound_in:
        options["sound_enable"] = bool(hf & 0x20000)
    if tangle_in and (fun is not None or hf & 0x80000):
        options["filament_tangle_detect"] = bool(hf & 0x100000)
    return options


def supports_flow_cali(raw):
    """Whether this printer can run Flow Dynamics Calibration at print start (P1 series cannot)."""
    fun = as_bits(raw.get("fun"))
    if fun is not None:
        return bool(fun & 0x80)
    return bool((as_int(raw.get("home_flag")) or 0) & 0x10000)


def fan_pct(value):
    """Bambu reports fan speed as 0-15, not a percentage."""
    speed = as_int(value)
    return None if speed is None else round(speed * 100 / 15)


LIGHT_LABELS = {"chamber_light": "Chamber light"}


def light_entries(raw):
    """The printer's lights from lights_report. Only chamber_light: the work_light node some models report
    (P2S, checked by hand on Bravo 2026-11-25) lights nothing, the printer's lamps all answer to chamber_light."""
    modes = {x.get("node"): x.get("mode") for x in raw.get("lights_report") or [] if isinstance(x, dict)}
    nodes = ["chamber_light"]
    return [{"name": n, "label": LIGHT_LABELS[n], "on": modes.get(n) == "on" if n in modes else None}
            for n in nodes]


def int_prefix(value):
    """The leading integer of a value like "-44dBm" or "5", else None."""
    m = re.match(r"-?\d+", str(value or ""))
    return int(m.group()) if m else None


def to_float(value):
    try:
        return round(float(value), 4) if value is not None else None
    except (TypeError, ValueError):
        return None


def _diagnostics(raw):
    """Raw report fields as hidden HA diagnostics (kept as reported)."""
    diag = {}
    for key in ("mc_print_stage", "mc_print_sub_stage", "mc_print_line_number", "ams_rfid_status",
                "fan_gear", "flag3", "home_flag", "lifecycle", "mess_production_state",
                "queue_sts", "queue_number", "queue_total"):
        if key in raw:
            diag[key] = raw[key]
    for sub, names in (("online", ("ahb", "rfid")), ("upload", ("status", "progress")), ("net", ("conf",))):
        node = raw.get(sub)
        if isinstance(node, dict):
            for name in names:
                if name in node:
                    diag[f"{sub}_{name}"] = node[name]
    return diag


def with_sequence(payload):
    """Copy payload, filling a missing sequence_id with '0' in every command section."""
    copy = json.loads(json.dumps(payload))
    for section in copy.values():
        if isinstance(section, dict) and "sequence_id" not in section:
            section["sequence_id"] = "0"
    return copy


def build_command(action, heater=None, target=None, on=None, light=None, **_unused):
    """Payloads as the A1 accepts them."""
    if action in ("pause", "resume", "stop"):
        return {"print": {"command": action, "sequence_id": "0"}}
    if action == "temperature":
        gcode = {"bed": "M140", "nozzle": "M104 T0"}[heater]
        return {"print": {"command": "gcode_line", "param": f"{gcode} S{int(target)}", "sequence_id": "0"}}
    if action == "light":
        return {"system": {"command": "ledctrl", "led_node": light or "chamber_light",
                           "led_mode": "on" if on else "off",
                           "led_on_time": 500, "led_off_time": 500, "loop_times": 0, "interval_time": 0,
                           "sequence_id": "0"}}
    raise ValueError(action)


def hms_text(attr, code):
    """The code the way the printer shows it: four groups of four hex digits, e.g. 0300-0D00-0001-0001."""
    digits = f"{attr:08X}{code:08X}"
    return "-".join(digits[i:i + 4] for i in range(0, 16, 4))


def fault(module, code, attr, full_code, severity=None, description=None):
    return {"module": module, "code": f"0x{code:x}", "attr": attr, "severity": severity,
            "description": description or f"HMS error {full_code}"}


def decode_faults(raw):
    faults, seen = [], set()
    hms = raw.get("hms")
    for item in hms if isinstance(hms, list) else []:
        attr, code = as_int(item.get("attr")) or 0, as_int(item.get("code")) or 0
        group = (attr >> 16) & 0xFFFF
        if code < FIRST_REAL_ERROR or (group, code & 0xFFFF) in USER_ACTION_CODES:
            continue
        seen.add((group, code & 0xFFFF))
        faults.append(fault((attr >> 24) & 0xFF, code, attr, f"{attr:08X}{code:08X}", (code >> 16) & 0xFFFF,
                            f"HMS error {hms_text(attr, code)}"))
    print_error = as_int(raw.get("print_error")) or 0
    group, error = (print_error >> 16) & 0xFFFF, print_error & 0xFFFF
    if error >= FIRST_REAL_ERROR and (group, error) not in USER_ACTION_CODES and (group, error) not in seen:
        faults.append(fault(group >> 8, error, print_error, f"{print_error:08X}",
                            description=f"print_error 0x{print_error:08X}"))
    return faults


def drying_fields(unit):
    """Drying state of one AMS unit report; -1 in dry_setting means "not chosen"."""
    dry = as_int(unit.get("dry_time"))
    setting = unit.get("dry_setting") if isinstance(unit.get("dry_setting"), dict) else {}
    temp, duration = as_int(setting.get("dry_temperature")), as_int(setting.get("dry_duration"))
    return {"drying": bool(dry and dry > 0),
            "dry_target": temp if temp is not None and temp >= 0 else None,
            "dry_hours": duration if duration is not None and duration >= 0 else None,
            "dry_block": unit.get("dry_sf_reason") or []}


def map_nozzle_type(value):
    """Newer firmware reports codes like 'HS01'; H->hardened, S->stainless. Known words pass through."""
    if isinstance(value, str) and re.fullmatch(r"[A-Z]{2}\d{2}", value):
        return {"H": "hardened_steel", "S": "stainless_steel"}.get(value[0], value)
    return value


def to_snapshot(raw, firmware_version=None):
    temperature = {"bed": "bed_temper", "bed_target": "bed_target_temper",
                   "nozzle": "nozzle_temper", "nozzle_target": "nozzle_target_temper"}
    temps = {name: raw[key] for name, key in temperature.items() if key in raw}
    temps["nozzle_heating"] = 0 < (temps.get("nozzle_target") or 0) and (temps.get("nozzle") or 0) < temps["nozzle_target"]
    ams = raw.get("ams") if isinstance(raw.get("ams"), dict) else {}
    vt_tray = raw.get("vt_tray")
    lights = light_entries(raw)
    progress = raw.get("mc_percent")
    hf = as_int(raw.get("home_flag")) or 0
    ipcam = raw.get("ipcam") if isinstance(raw.get("ipcam"), dict) else {}
    upgrade = raw.get("upgrade_state") if isinstance(raw.get("upgrade_state"), dict) else {}
    xcam = raw.get("xcam") if isinstance(raw.get("xcam"), dict) else {}
    nozzle_type = raw.get("nozzle_type")
    mapped_nozzle = map_nozzle_type(nozzle_type)
    nozzle = {"nozzle_type": mapped_nozzle, "nozzle_diameter": raw.get("nozzle_diameter")}
    if isinstance(nozzle_type, str) and re.fullmatch(r"[A-Z]{2}\d{2}", nozzle_type):
        nozzle["nozzle_code"] = nozzle_type
    nozzles = [nozzle]
    device = raw.get("device") if isinstance(raw.get("device"), dict) else {}
    # H2D (dual nozzle) reports each extruder under device.nozzle / device.extruder and the chamber under device.ctc.
    listed = (device.get("nozzle") or {}).get("info")
    if isinstance(listed, list) and len(listed) > 1:
        heads = {h.get("id"): h.get("temp") for h in (device.get("extruder") or {}).get("info") or [] if isinstance(h, dict)}
        nozzles = [{"nozzle_type": map_nozzle_type(n.get("type")), "nozzle_diameter": str(n.get("diameter")),
                    **({"nozzle_code": n["type"]} if re.fullmatch(r"[A-Z]{2}\d{2}", str(n.get("type"))) else {}),
                    "temp": heads.get(n.get("id"))} for n in listed if isinstance(n, dict)]
    chamber = raw.get("chamber_temper")
    if chamber is None:
        chamber = ((device.get("ctc") or {}).get("info") or {}).get("temp")
    return {
        "state": raw.get("gcode_state"),
        "subtask_name": raw.get("subtask_name"),
        "progress": float(progress) if progress is not None else None,
        "remaining_time": raw.get("mc_remaining_time"),
        "layer_num": raw.get("layer_num"),
        "total_layers": raw.get("total_layer_num"),
        "temperatures": temps,
        "door_open": bool((as_int(raw.get("home_flag")) or 0) & 0x00800000),
        # bit0 X, bit1 Y, bit2 Z homed (verified on a P1): they clear on M18 or the idle timeout.
        "home_flag": as_int(raw.get("home_flag")),
        "speed_level": raw.get("spd_lvl"),
        "chamber_light": lights[0]["on"],
        "lights": lights,
        "hms_errors": decode_faults(raw),
        "print_error": as_int(raw.get("print_error")) or 0,
        "stage": as_int(raw.get("stg_cur")),  # What the printer is doing now: homing, heating bed...
        "prepare_percent": as_int(raw.get("gcode_file_prepare_percent")),  # Download of the job file.
        "ams": [{**u, **drying_fields(u)} if isinstance(u, dict) else u for u in ams.get("ams") or []],
        "tray_now": as_int(ams.get("tray_now")),
        "ams_exist_bits": ams.get("tray_exist_bits"),  # the inlet sensors: a slot can hold filament with no data
        "vt_tray": [vt_tray] if isinstance(vt_tray, dict) else vt_tray or [],
        "fans": {"part": fan_pct(raw.get("cooling_fan_speed")), "aux": fan_pct(raw.get("big_fan1_speed")),
                 "chamber": fan_pct(raw.get("big_fan2_speed"))},
        "xcam": {k: v for k, v in raw["xcam"].items() if isinstance(v, (bool, str))}
        if isinstance(raw.get("xcam"), dict) else {},
        "options": report_options(raw, hf),
        "nozzles": nozzles,
        "speed_percent": as_int(raw.get("spd_mag")),
        "heatbreak_fan": fan_pct(raw.get("heatbreak_fan_speed")),
        "print_source": raw.get("print_type"),
        "pressure_advance": to_float(raw.get("k")),
        "nozzle_diameter": raw.get("nozzle_diameter"),
        "nozzle_type": mapped_nozzle,
        "wifi_signal": int_prefix(raw.get("wifi_signal")),
        "sd_card": bool(raw.get("sdcard")) if raw.get("sdcard") is not None else None,
        "filament_sensor": as_int(raw.get("hw_switch_state")) == 1,
        "ams_status": as_int(raw.get("ams_status")),
        "firmware_update": upgrade.get("new_version_state") == 1,
        "timelapse": ipcam.get("timelapse") == "enable",
        "camera_recording": ipcam.get("ipcam_record") == "enable",
        "plate_detection": xcam.get("buildplate_marker_detector"),
        "chamber_temp": chamber,
        "diagnostics": _diagnostics(raw),
        "firmware_version": firmware_version,
    }


FLOW_CALI = {"on": (True, 1), "auto": (False, 2), "off": (False, 0)}


def project_file(filename, ams_mapping=(254,), plate=1, flow_cali="on"):
    # One entry per filament id of the 3mf: a tray 0-15, 254 the spool holder, None unused.
    flow, extrude = FLOW_CALI.get(flow_cali, FLOW_CALI["on"])
    task = str(int(time.time() * 1000) % 2_147_483_647 or 1)
    return {"print": {
        "sequence_id": "20000", "command": "project_file",
        "param": f"Metadata/plate_{plate}.gcode", "url": f"ftp://{filename}", "file": filename, "md5": "",
        "bed_type": "auto", "timelapse": False,
        "bed_leveling": False, "auto_bed_leveling": 2, "flow_cali": flow, "extrude_cali_flag": extrude,
        "extrude_cali_manual_mode": 0, "vibration_cali": False, "layer_inspect": False, "nozzle_offset_cali": 0,
        "use_ams": any(t is not None and t < 254 for t in ams_mapping),
        # The firmware refuses raw 254/255 in the flat list: -1 there, the spool holder (255) in ams_mapping2.
        # A None entry is a filament id the 3mf does not use: no filament (255/255), not the spool holder.
        "ams_mapping": [-1 if t is None or t >= 254 else t for t in ams_mapping],
        "ams_mapping2": [{"ams_id": 255, "slot_id": 255} if t is None else
                         {"ams_id": 255, "slot_id": 0} if t >= 254 else
                         {"ams_id": t // 4, "slot_id": t % 4} for t in ams_mapping],
        "subtask_name": filename.replace(".3mf", "").replace(".gcode", ""),
        "profile_id": "0", "project_id": task, "subtask_id": task, "task_id": task,
    }}


class PrinterLink:
    def __init__(self, alias, host, serial, access_code, fingerprint_dir, clock=time.monotonic, ams_drying=False):
        self.alias, self.host, self.serial = alias, host, serial
        self.access_code, self.fingerprint_dir = access_code, fingerprint_dir
        self.ams_drying = ams_drying  # the printer model can start drying, not just the AMS heater
        self.clock = clock
        self.report_topic, self.request_topic = f"device/{serial}/report", f"device/{serial}/request"
        self.lock = threading.Lock()
        self.raw = {}
        self.last_report = None
        self.online = False
        self.refused = False
        self.cert_refused = False  # A refused certificate is final: paho must not keep retrying.
        self.error = None
        self.client = None
        self.last_push_all = None
        self.last_version_req = None
        self.firmware_version = None
        self._stop = False
        self._connected = None
        self.drops = 0  # Times the link went from connected to disconnected; server.py resets the axes on change.
        self.sequence = 0
        self.ams_serials = {}  # AMS unit id (int) -> {"serial": str|None, "model": str}
        self.start_reply = None

    def feed(self, message):
        report = message.get("print")
        if isinstance(report, dict):
            with self.lock:
                merge(self.raw, report)
                self.last_report = self.clock()
                if report.get("command") == "project_file":
                    reply = {"at": time.time()}
                    for key in ("result", "reason"):
                        if key in report:
                            reply[key] = report[key]
                    if "err_code" in report:
                        reply["err_code"] = report["err_code"]
                    elif "error_code" in report:
                        reply["err_code"] = report["error_code"]
                    self.start_reply = reply
                for unit in (report.get("ams") or {}).get("ams") or []:
                    self._apply_ams_fallback(unit)
        info = message.get("info")
        if isinstance(info, dict) and info.get("command") == "get_version":
            for mod in info.get("module") or []:
                if not isinstance(mod, dict):
                    continue
                if mod.get("name") == "ota" and mod.get("sw_ver"):
                    with self.lock:
                        self.firmware_version = mod["sw_ver"]
                    continue
                m = re.match(r"^(ams|n3f|n3s|ams_f1)/(\d+)$", mod.get("name") or "")
                if m:
                    with self.lock:
                        self.ams_serials[int(m.group(2))] = {"serial": real_serial(mod.get("sn")),
                                                              "model": AMS_MODULES[m.group(1)]}

    def _apply_ams_fallback(self, unit):
        """A unit with no get_version serial takes the report's ams_id when it is a real one."""
        if not isinstance(unit, dict):
            return
        try:
            uid = int(unit.get("id"))
        except (TypeError, ValueError):
            return
        stored = self.ams_serials.get(uid) or {}
        model = ams_model_from_info(unit.get("info"))
        if model and model != stored.get("model"):
            stored = self.ams_serials[uid] = {"serial": stored.get("serial"), "model": model}
        if stored.get("serial"):
            return
        ams_id = unit.get("ams_id")
        if real_serial(ams_id) and ams_id.isalnum() and set(ams_id) != {"0"}:
            self.ams_serials[uid] = {"serial": ams_id, "model": stored.get("model") or "AMS"}

    def snapshot(self):
        with self.lock:
            snapshot = to_snapshot(json.loads(json.dumps(self.raw)), self.firmware_version)
            for unit in snapshot.get("ams") or []:
                try:
                    uid = int(unit.get("id"))
                except (TypeError, ValueError):
                    continue
                info = self.ams_serials.get(uid) or {}
                unit["serial"] = info.get("serial")
                unit["model"] = info.get("model", "AMS")
                unit["can_dry"] = unit["model"] in DRY_MODELS and self.ams_drying
            fresh = self.last_report is not None and self.clock() - self.last_report <= STALE_S
        if self.cert_refused:
            snapshot["connected"] = False
            snapshot["error"] = self.error
        else:
            snapshot["connected"] = fresh
        reason = self.error if self.cert_refused else (f"nessun dato da {STALE_S} s" if not fresh else None)
        self._set_connected(snapshot["connected"], reason)
        return snapshot

    def _set_connected(self, connected, reason):
        if connected == self._connected:
            return
        if self._connected and not connected:
            self.drops += 1
        self._connected = connected
        print(f"{self.alias}: {reason if not connected else 'riconnessa'}", file=sys.stderr)

    def maybe_push_stale(self):
        """Ask for a fresh report when the printer has been silent too long."""
        now = self.clock()
        with self.lock:
            silent = self.last_report is None or now - self.last_report >= STALE_PUSH_S
            due = self.last_push_all is None or now - self.last_push_all >= STALE_PUSH_S
        if silent and due and self.publish(PUSHALL):
            self.last_push_all = now

    def maybe_request_version(self):
        """Ask for the firmware version on connect and every VERSION_RETRY_S."""
        now = self.clock()
        with self.lock:
            due = self.last_version_req is None or now - self.last_version_req >= VERSION_RETRY_S
        if due and self.publish(GET_VERSION):
            self.last_version_req = now

    def publish(self, payload):
        """Send once. Nothing is queued: False means it did not leave."""
        # QoS 0 on purpose: with QoS 1 the library resends after a reconnect, and a command must never repeat.
        if self.client is None or not self.online:
            return False
        return self.client.publish(self.request_topic, json.dumps(payload)).rc == 0

    def command(self, action, **params):
        return self.publish(build_command(action, **params))

    def start_print(self, filename, ams_mapping=(254,), plate=1, flow_cali="on"):
        """project_file as Bambu Studio sends it for a file already in the FTP root."""
        return self.publish(project_file(filename, ams_mapping, plate, flow_cali))

    def gcode(self, text):
        """Send G-code lines once, through publish (QoS 0, never resent)."""
        with self.lock:
            self.sequence += 1
            sequence = str(self.sequence)
        param = text if text.endswith("\n") else text + "\n"
        return self.publish({"print": {"command": "gcode_line", "param": param, "sequence_id": sequence}})

    def start(self):
        import paho.mqtt.client as mqtt  # Imported here so reading a snapshot never needs it.
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"bff-{self.alias}")
        client.tls_set(cert_reqs=ssl.CERT_NONE)  # Self-signed: pinned in _on_socket_open.
        client.tls_insecure_set(True)
        client.reconnect_delay_set(1, 30)
        client.on_socket_open = self._on_socket_open
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        self.client = client
        self._stop = False
        client.connect_async(self.host, 8883, keepalive=15)
        client.loop_start()
        threading.Thread(target=self._stale_loop, daemon=True).start()

    def _stale_loop(self):
        while not self._stop:
            self.maybe_push_stale()
            self.maybe_request_version()
            time.sleep(STALE_PUSH_S)

    def stop(self):
        self._stop = True
        if self.client is not None:
            self.client.disconnect()
            self.client.loop_stop()

    def _on_socket_open(self, client, _userdata, sock):
        # Runs after the TLS handshake and before CONNECT: the code only goes to the known printer.
        if self.cert_refused:  # Certificate already refused: no retry, no credentials.
            client.disconnect()
            return
        try:
            check_fingerprint(self.fingerprint_dir, self.alias, sock.getpeercert(binary_form=True))
        except CertificateChanged:
            self._refuse(client, "certificate changed: re-add the printer")
            return
        except Exception as err:  # A failed read must not crash paho's loop thread.
            self._refuse(client, f"{type(err).__name__}: re-add the printer")
            return
        client.username_pw_set("bblp", self.access_code)

    def _refuse(self, client, error):
        """The code never goes out; the link stops for good and status says why."""
        if not self.cert_refused:
            print(f"{self.alias}: {error}", file=sys.stderr)
        self.refused = self.cert_refused = True
        self.error = error
        client.username_pw_set("bblp", "")
        client.disconnect()

    def _on_connect(self, client, _userdata, _flags, reason, _props):
        if self.refused:
            client.disconnect()
            return
        if reason.is_failure:
            return
        self.online = True
        client.subscribe(self.report_topic)
        client.publish(self.request_topic, json.dumps(PUSHALL))
        client.publish(self.request_topic, json.dumps(GET_VERSION))
        self.last_version_req = self.clock()

    def _on_disconnect(self, _client, _userdata, _flags, _reason, _props):
        self.online = False
        with self.lock:
            self.last_report = None
        self._set_connected(False, "socket chiuso")

    def _on_message(self, _client, _userdata, msg):
        try:
            message = json.loads(msg.payload)
        except ValueError:
            return
        if isinstance(message, dict):
            self.feed(message)
