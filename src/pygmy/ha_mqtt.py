"""Publish printer state to Home Assistant through MQTT Discovery and take HA's few commands."""
import json
import re
import sys
import threading
import time
import uuid

try:  # imported as pygmy.ha_mqtt (tests) or flat next to the other modules (server)
    from . import onboard
except ImportError:  # no known parent package
    import onboard

AVAILABILITY = "pygmy/availability"
DISCOVERY = "homeassistant"
PUBLISH_S = 5
SPEEDS = ("silent", "standard", "sport", "ludicrous")
OPTIONS = ("auto_recovery", "sound_enable", "filament_tangle_detect", "bff_spaghetti", "bff_spaghetti_pause")
FANS = ("fan_part", "fan_aux", "fan_chamber")
READABLE_SLOTS = frozenset(f"{unit}{n}" for unit in "abcd" for n in "1234")
# AMS units with a heater: the highest drying temperature each one accepts.
DRY_MODELS = {"AMS 2 Pro": 65, "AMS HT": 85}
DRY_DEFAULT = {"temp": 55, "hours": 8}
DRY_CONTROLS = (("number", "dry_temp"), ("number", "dry_hours"), ("button", "dry_start"), ("button", "dry_stop"))
# Spec §8.6: HA may control the printer, never start a print.
HA_ACTIONS = ("pause", "resume", "stop", "light", "plate_cleared", "speed", *FANS,
              *(f"opt_{key}" for key in OPTIONS), "clear_errors", "dismiss_spaghetti", "plate_detection")


def light_slug(name):
    """A light name as an MQTT/entity suffix: chamber_light, output_pin caselight -> output_pin_caselight."""
    return re.sub(r"[^a-z0-9]+", "_", str(name or "").lower()).strip("_")


def light_config_topic(alias, slug=None):
    return f"{DISCOVERY}/light/pygmy_{alias}/light{'_' + slug if slug else ''}/config"

# Bambu stg_cur codes (the same map the app uses, src/pygmy/app.html).
STAGE = {1: "Auto bed leveling", 2: "Heating bed", 3: "Sweeping XY mech mode", 4: "Changing filament",
         5: "Paused", 6: "Paused: filament runout", 7: "Heating nozzle", 8: "Calibrating extrusion",
         9: "Scanning bed surface", 10: "Inspecting first layer", 11: "Identifying build plate",
         12: "Calibrating lidar", 13: "Homing", 14: "Cleaning nozzle", 15: "Checking extruder temperature",
         16: "Paused by user", 17: "Paused: front cover falling", 18: "Calibrating lidar",
         19: "Calibrating extrusion flow", 20: "Paused: nozzle temperature fault", 21: "Paused: bed temperature fault",
         22: "Unloading filament", 23: "Paused: skipped step", 24: "Loading filament",
         25: "Calibrating motor noise", 26: "Paused: AMS lost", 27: "Paused: heatbreak fan", 29: "Cooling chamber",
         30: "Paused by G-code", 31: "Motor noise showoff", 32: "Paused: nozzle covered",
         34: "Paused: first layer error", 35: "Paused: nozzle clog"}
# (object id, pretty name) of the raw report fields exposed as hidden diagnostics.
DIAG_FIELDS = (
    ("mc_print_stage", "mc_print_stage"), ("mc_print_sub_stage", "mc_print_sub_stage"),
    ("mc_print_line_number", "mc_print_line_number"), ("ams_rfid_status", "ams_rfid_status"),
    ("fan_gear", "fan_gear"), ("flag3", "flag3"), ("home_flag", "home_flag"),
    ("online_ahb", "online.ahb"), ("online_rfid", "online.rfid"), ("lifecycle", "lifecycle"),
    ("mess_production_state", "mess_production_state"), ("upload_status", "upload.status"),
    ("upload_progress", "upload.progress"), ("net_conf", "net.conf"), ("queue_sts", "queue_sts"),
    ("queue_number", "queue_number"), ("queue_total", "queue_total"),
)
MORE_SENSORS = (
    ("sensor", "stage", "Stage", {"value_template": "{{ value_json.stage_text }}"}),
    ("sensor", "total_layers", "Total layers", {"value_template": "{{ value_json.total_layers }}"}),
    ("sensor", "speed_percent", "Speed", {"value_template": "{{ value_json.speed_percent }}",
                                           "unit_of_measurement": "%"}),
    ("sensor", "heatbreak_fan", "Heatbreak fan", {"value_template": "{{ value_json.heatbreak_fan }}",
                                                   "unit_of_measurement": "%"}),
    ("sensor", "file_download", "File download", {"value_template": "{{ value_json.file_download }}",
                                                   "unit_of_measurement": "%"}),
    ("sensor", "print_source", "Print source", {"value_template": "{{ value_json.print_source }}"}),
    ("sensor", "pressure_advance", "Pressure advance",
     {"value_template": "{{ value_json.pressure_advance }}", "entity_category": "diagnostic",
      "suggested_display_precision": 4}),
    ("sensor", "nozzle_diameter", "Nozzle diameter",
     {"value_template": "{{ value_json.nozzle_diameter }}", "entity_category": "diagnostic",
      "unit_of_measurement": "mm"}),
    ("sensor", "nozzle_type", "Nozzle type",
     {"value_template": "{{ value_json.nozzle_type }}", "entity_category": "diagnostic"}),
    ("sensor", "wifi_signal", "WiFi signal",
     {"value_template": "{{ value_json.wifi_signal }}", "entity_category": "diagnostic",
      "device_class": "signal_strength", "unit_of_measurement": "dBm"}),
    ("sensor", "active_slot", "Active slot",
     {"value_template": "{{ value_json.active_slot }}", "json_attributes_topic": None,
      "json_attributes_template": "{{ {'ams_serial': value_json.active_slot_serial} | tojson }}"}),
    ("sensor", "external_spool", "External spool",
     {"value_template": "{{ value_json.external_spool.state }}", "json_attributes_topic": None,
      "json_attributes_template": "{{ value_json.external_spool | tojson }}"}),
    ("sensor", "firmware_version", "Firmware version",
     {"value_template": "{{ value_json.firmware_version }}", "entity_category": "diagnostic"}),
)
MORE_BINARY = (
    ("binary_sensor", "sd_card", "SD card",
     {"value_template": "{{ 'ON' if value_json.sd_card else 'OFF' }}", "entity_category": "diagnostic"}),
    ("binary_sensor", "filament_sensor", "Filament sensor",
     {"value_template": "{{ 'ON' if value_json.filament_sensor else 'OFF' }}"}),
    ("binary_sensor", "firmware_update", "Firmware update",
     {"value_template": "{{ 'ON' if value_json.firmware_update else 'OFF' }}", "entity_category": "diagnostic",
      "device_class": "update"}),
    ("binary_sensor", "timelapse", "Timelapse",
     {"value_template": "{{ 'ON' if value_json.timelapse else 'OFF' }}", "entity_category": "diagnostic"}),
    ("binary_sensor", "camera_recording", "Camera recording",
     {"value_template": "{{ 'ON' if value_json.camera_recording else 'OFF' }}", "entity_category": "diagnostic"}),
)

# (component, object id, name, extra config). Values come from the state JSON on pygmy/<alias>/state.
SENSORS = (
    ("sensor", "state", "Status", {"value_template": "{{ value_json.state | lower }}"}),
    ("sensor", "progress", "Progress", {"value_template": "{{ value_json.progress }}", "unit_of_measurement": "%"}),
    ("sensor", "remaining", "Time left", {"value_template": "{{ value_json.remaining_time }}",
                                                "unit_of_measurement": "min", "device_class": "duration"}),
    ("sensor", "layer", "Layer", {"value_template": "{{ value_json.layer_num }}/{{ value_json.total_layers }}"}),
    ("sensor", "file", "Current file", {"value_template": "{{ value_json.subtask_name }}"}),
    ("sensor", "nozzle", "Nozzle", {"value_template": "{{ value_json.nozzle }}", "unit_of_measurement": "°C",
                                    "device_class": "temperature",
                                    "suggested_display_precision": 0}),
    ("sensor", "nozzle_target", "Nozzle target", {"value_template": "{{ value_json.nozzle_target }}",
                                                     "unit_of_measurement": "°C", "device_class": "temperature",
                                    "suggested_display_precision": 0}),
    ("sensor", "bed", "Bed", {"value_template": "{{ value_json.bed }}", "unit_of_measurement": "°C",
                                 "device_class": "temperature",
                                    "suggested_display_precision": 0}),
    ("sensor", "bed_target", "Bed target", {"value_template": "{{ value_json.bed_target }}",
                                                  "unit_of_measurement": "°C", "device_class": "temperature",
                                    "suggested_display_precision": 0}),
    ("sensor", "hms", "HMS errors", {"value_template": "{{ value_json.hms_count }}"}),
    ("sensor", "end_reason", "Print end", {"value_template": "{{ value_json.end_reason }}"}),
    ("sensor", "error", "Error", {"value_template": "{{ value_json.errors or 'none' }}"}),
    ("sensor", "speed", "Speed mode", {"value_template": "{{ {1: 'silent', 2: 'standard', 3: 'sport', 4: 'ludicrous'}"
                                                ".get(value_json.speed_level, 'unknown') }}"}),
    ("sensor", "fan", "Part fan", {"value_template": "{{ value_json.fan }}", "unit_of_measurement": "%"}),
    ("sensor", "fan_aux", "Aux fan", {"value_template": "{{ value_json.fan_aux }}",
                                                 "unit_of_measurement": "%"}),
    ("sensor", "fan_chamber", "Chamber fan", {"value_template": "{{ value_json.fan_chamber }}",
                                                 "unit_of_measurement": "%"}),
    ("sensor", "spaghetti_score", "Spaghetti score", {"value_template": "{{ value_json.spaghetti_score }}"}),
    ("sensor", "spool", "Spool", {"value_template": "{{ value_json.spool_material or 'none' }}",
                                   "json_attributes_topic": None, "json_attributes_template":
                                   "{{ {'color': value_json.spool_color} | tojson }}"}),
    ("sensor", "spaghetti", "Spaghetti", {"value_template": "{{ {'safe': 'ok', 'warning': 'warning', 'failure': 'failure',"
                                          " 'unknown': 'unsure'}.get(value_json.spaghetti, 'off') }}"}),
    ("binary_sensor", "plate_pending", "Plate needs clearing",
     {"value_template": "{{ 'ON' if value_json.plate_pending else 'OFF' }}", "device_class": "problem"}),
    ("binary_sensor", "connected", "Connected", {"value_template": "{{ 'ON' if value_json.connected else 'OFF' }}",
                                                "device_class": "connectivity"}),
)
CANCELED = 0x0300400C
BUTTONS = (("pause", "Pause"), ("resume", "Resume"), ("stop", "Stop"), ("plate_cleared", "Plate is clear"),
           ("clear_errors", "Clear errors"), ("dismiss_spaghetti", "Not spaghetti"))


def stage_text(stage):
    """The printer's current step as text: idle for 255/-1, unknown numbers as 'stage N'."""
    if stage in (255, -1):
        return "idle"
    if stage in STAGE:
        return STAGE[stage]
    return f"stage {stage}" if stage is not None else None


def active_slot(tray):
    """'255' -> none, '254' -> external, else the AMS slot name (e.g. 5 -> B2, 512 -> HT1)."""
    if tray == 255:
        return "none"
    if tray == 254:
        return "external"
    if tray is None:
        return None
    return onboard.slot_name(tray // 4, tray % 4)


def ams_status_text(value):
    """The AMS status byte's high nibble as text, per the community protocol notes."""
    if value is None:
        return None
    main = (value >> 8) & 0xFF
    return {0: "idle", 1: "filament change", 2: "RFID reading", 3: "assist", 4: "calibration",
            0x10: "self check", 0x20: "debug"}.get(main, f"status {main}")


def flat_state(status):
    """The status tool's dict, flattened for value templates."""
    temps = status.get("temperatures") or {}
    keys = ("connected", "state", "progress", "remaining_time", "layer_num", "total_layers",
            "subtask_name", "speed_level", "hms_count")
    flat = {key: status.get(key) for key in keys}
    flat.update({key: temps.get(key) for key in ("nozzle", "nozzle_target", "bed", "bed_target")})
    flat["light"] = "ON" if status.get("chamber_light") else "OFF"
    flat["lights"] = {l.get("name"): "ON" if l.get("on") else "OFF"
                      for l in status.get("lights") or [] if l.get("name")}
    fans = status.get("fans") or {}
    flat["fan"] = fans.get("part")
    flat["fan_aux"], flat["fan_chamber"] = fans.get("aux"), fans.get("chamber")
    # ponytail: no door sensor: only X1, P2S and H2D have one (A1 has no door, P1 always says closed). Add it per model then.
    flat["plate_pending"] = bool(status.get("awaiting_plate_clear"))
    flat["spaghetti"] = (status.get("spaghetti") or {}).get("verdict")
    flat["spaghetti_score"] = (status.get("spaghetti") or {}).get("score")
    flat["spaghetti_paused"] = (status.get("spaghetti") or {}).get("paused")
    flat["options"] = dict(status.get("options") or {})
    flat["ams"], flat["ams_humidity"] = {}, {}
    for unit in status.get("ams_units") or []:
        humidity = unit.get("humidity")
        flat["ams_humidity"][unit.get("unit")] = ("EDCBA"[humidity - 1]
                                                  if type(humidity) is int and 1 <= humidity <= 5 else None)
        for slot in unit.get("slots") or []:
            if not slot.get("slot"):
                continue
            color = str(slot.get("color") or "")[:6]
            flat["ams"][slot["slot"]] = {"material": slot.get("material"), "name": slot.get("name"),
                                         "color": f"#{color}" if color else None, "remain": slot.get("remain"),
                                         "active": bool(slot.get("active")), "empty": bool(slot.get("empty")),
                                         "profile_id": slot.get("profile_id")}
    # Errors that need someone, minus the filament prompts the printer handles with the user at it.
    flat["alarm"] = next((a.get("description") or "error" for a in status.get("attention") or []
                          if isinstance(a, dict) and a.get("handling") != "persona"), None)
    spool = status.get("spool") or {}
    color = str(spool.get("color") or "")[:6]
    flat["spool_material"], flat["spool_color"] = spool.get("material"), f"#{color}" if color else None
    # FAILED covers both: a user cancel leaves print_error 0300400C.
    failed = str(status.get("state") or "").upper() == "FAILED"
    flat["end_reason"] = ("stopped" if status.get("print_error") == CANCELED else "failed") if failed else "none"
    # HA state is capped at 255 chars.
    flat["errors"] = "; ".join(str(a.get("description") or a.get("message") or a) if isinstance(a, dict) else str(a)
                               for a in status.get("attention") or [])[:250]
    flat["stage_text"] = stage_text(status.get("stage"))
    flat["speed_percent"] = status.get("speed_percent")
    flat["heatbreak_fan"] = status.get("heatbreak_fan")
    flat["file_download"] = status.get("prepare_percent")
    flat["print_source"] = status.get("print_source")
    flat["pressure_advance"] = status.get("pressure_advance")
    flat["nozzle_diameter"] = status.get("nozzle_diameter")
    flat["nozzle_type"] = status.get("nozzle_type")
    flat["wifi_signal"] = status.get("wifi_signal")
    flat["sd_card"] = status.get("sd_card")
    flat["filament_sensor"] = status.get("filament_sensor")
    flat["active_slot"] = active_slot(status.get("tray_now"))
    active_serial = None
    if flat["active_slot"] not in (None, "none", "external"):
        for unit in status.get("ams_units") or []:
            if unit.get("unit") == flat["active_slot"][0]:
                active_serial = unit.get("serial")
                break
    flat["active_slot_serial"] = active_serial
    flat["ams_status"] = ams_status_text(status.get("ams_status"))
    flat["firmware_update"] = status.get("firmware_update")
    flat["timelapse"] = status.get("timelapse")
    flat["camera_recording"] = status.get("camera_recording")
    flat["plate_detection"] = status.get("plate_detection")
    flat["firmware_version"] = status.get("firmware_version")
    flat["diag"] = status.get("diagnostics") or {}
    flat["external_spool"] = status.get("external_spool") or {}
    if "chamber_temp" in status:
        flat["chamber_temp"] = status["chamber_temp"]
    if "door" in status:
        flat["door"] = status["door"]
    flat["ams_units_details"] = {}
    slot_extras = {}
    for unit in status.get("ams_units") or []:
        u = unit.get("unit")
        flat["ams_units_details"][u] = {"temp": unit.get("temp"), "humidity_raw": unit.get("humidity_raw"),
                                        "dry_time": unit.get("dry_time"), "info": unit.get("info")}
        for slot in unit.get("slots") or []:
            if slot.get("slot"):
                slot_extras[slot["slot"]] = slot
    for slot, entry in flat["ams"].items():
        src = slot_extras.get(slot) or {}
        for key in ("nozzle_temp_min", "nozzle_temp_max", "drying_temp", "drying_hours", "state"):
            if key in src:
                entry[key] = src[key]
    return flat


EVENT_TYPES = ("finished", "failed", "error", "spaghetti")
RUNNING = ("RUNNING", "PAUSE")
# English in the repo; BFF_HA_LANG=it on the user's CT. House style (2026-11-12): emoji first, the facts, what to do.
TEXT = {
    "en": {"print": "Print", "finished": "✅ {file} finished{dur}{facts}. Clear the plate.",
           "failed": "❌ {file} failed at {progress} %{layer}.", "layer": " (layer {n} of {t})",
           "error_paused": "⚠️ {alarm}. Print paused: it needs a hand.", "error": "⚠️ {alarm}. It needs a hand.",
           "spaghetti": "🍝 Possible spaghetti on {file}{at}: check the camera.",
           "spaghetti_paused": "🍝 Possible spaghetti on {file}{at}: the print is paused. Check the camera.",
           "at": " at layer {n}", "in": " in {d}", "layers": "{t} layers",
           "low_spool": "🧵 {slot} at {remain} %{name}. Have the next spool ready.",
           "offline": "📴 Offline for 2 min.", "online": "✅ Back online."},
    "it": {"print": "La stampa", "finished": "✅ {file} finita{dur}{facts}. Libera il piatto.",
           "failed": "❌ {file} fallita al {progress} %{layer}.", "layer": " (layer {n} di {t})",
           "error_paused": "⚠️ {alarm}. Stampa in pausa: serve una mano.", "error": "⚠️ {alarm}. Serve una mano.",
           "spaghetti": "🍝 Possibili spaghetti su {file}{at}: guarda la camera.",
           "spaghetti_paused": "🍝 Possibili spaghetti su {file}{at}: la stampa è in pausa. Guarda la camera.",
           "at": " al layer {n}", "in": " in {d}", "layers": "{t} layer",
           "low_spool": "🧵 {slot} al {remain} %{name}. Prepara la prossima bobina.",
           "offline": "📴 Offline da 2 min.", "online": "✅ Di nuovo online."},
}


def duration(minutes):
    h, m = divmod(int(minutes), 60)
    return f"{h} h {m} min" if h else f"{m} min"


def events(prev, cur, lang="en", minutes=None):
    """(event_type, message) for the few things worth a phone notification. Start, progress,
    pause and a stop by the user stay silent (the user: "niente notifiche inutili").
    minutes: how long the print ran, when the bridge saw it start."""
    if not prev:
        return []  # After a restart or reconnect nothing is invented.
    t = TEXT.get(lang, TEXT["en"])
    out = []
    was, now = str(prev.get("state") or "").upper(), str(cur.get("state") or "").upper()
    file = cur.get("subtask_name") or prev.get("subtask_name") or t["print"]
    n, total = cur.get("layer_num") or prev.get("layer_num"), cur.get("total_layers") or prev.get("total_layers")
    if was in RUNNING and now == "FINISH":
        facts = " · ".join(x for x in (cur.get("spool_material"), t["layers"].format(t=total) if total else None) if x)
        out.append(("finished", t["finished"].format(file=file, dur=t["in"].format(d=duration(minutes)) if minutes else "",
                                                     facts=f" · {facts}" if facts else "")))
    if was in RUNNING and now == "FAILED" and cur.get("end_reason") == "failed":
        layer = t["layer"].format(n=n, t=total) if n and total else ""
        why = f": {cur['alarm']}" if cur.get("alarm") else ""
        out.append(("failed", t["failed"].format(file=file, progress=round(prev.get("progress") or 0), layer=layer) + why))
    # One reason per failure: when it is already in the failed line, do not send it twice.
    if not any(kind == "failed" for kind, _ in out) and cur.get("alarm") and cur.get("alarm") != prev.get("alarm"):
        out.append(("error", t["error_paused" if now == "PAUSE" else "error"].format(alarm=cur["alarm"])))
    bad = ("warning", "failure")
    # A new alarm, or one that has just paused the print (warning, then failure + pause: tell again).
    if (cur.get("spaghetti") in bad and prev.get("spaghetti") not in bad) or (cur.get("spaghetti_paused") and not prev.get("spaghetti_paused")):
        key = "spaghetti_paused" if cur.get("spaghetti_paused") else "spaghetti"
        out.append(("spaghetti", t[key].format(file=file, at=t["at"].format(n=n) if n else "")))
    return out


def discovery(alias, name=None, slots=(), units=(), profile=None, lights=()):
    """(topic, config) pairs, published retained so HA rebuilds the entities after a restart.

    slots/units come from the AMS this printer reports right now (e.g. "A1", "A").
    profile is what this printer really has ({"fans": [...], "options": [...]}); None means try everything.
    lights: one entity per light when there is more than one, else the single Light entity.
    Entity ids are pygmy_<alias>_*.
    """
    printer_name = name or alias.upper()
    device = {"identifiers": [f"pygmy_{alias}"], "name": printer_name, "manufacturer": "Bambu Lab"}

    def ams_device(unit):
        return {"identifiers": [f"pygmy_{alias}_ams_{unit}"], "name": f"{printer_name} AMS {unit}",
                "model": "BMCU", "manufacturer": "BMCU", "via_device": f"pygmy_{alias}"}

    base = {"availability_topic": AVAILABILITY, "device": device}
    fans = None if profile is None else set(profile.get("fans") or ())
    options = None if profile is None else set(profile.get("options") or ())
    chamber = profile is not None and profile.get("chamber_sensor")
    xcam_on = profile is None or "buildplate_marker_detector" in (profile.get("xcam") or ())
    state = f"pygmy/{alias}/state"
    out = []
    for component, obj, name, extra in SENSORS:
        if fans is not None and obj.startswith("fan_") and obj != "fan" and obj[4:] not in fans:
            continue
        cfg = {"name": name, "state_topic": state, **extra}
        if "json_attributes_topic" in cfg:
            cfg["json_attributes_topic"] = state
        out.append((component, obj, cfg))
    for action, name in BUTTONS:
        out.append(("button", action, {"name": name, "command_topic": f"pygmy/{alias}/cmd/{action}", "payload_press": "PRESS"}))
    out.append(("event", "event", {"name": "Alert", "state_topic": f"pygmy/{alias}/event",
                                    "event_types": list(EVENT_TYPES)}))
    if len(lights) > 1:
        for l in lights:
            slug = light_slug(l.get("name"))
            out.append(("light", f"light_{slug}", {
                "name": l.get("label") or l["name"], "state_topic": state,
                "value_template": f"{{{{ value_json.lights[{json.dumps(l['name'])}] }}}}",
                "command_topic": f"pygmy/{alias}/cmd/light_{slug}"}))
    else:
        out.append(("light", "light", {"name": "Light", "state_topic": state,
                                        "state_value_template": "{{ value_json.light }}",
                                        "command_topic": f"pygmy/{alias}/cmd/light"}))
    for slot in slots:
        low = slot.lower()
        out.append(("sensor", f"ams_{low}", {
            "name": f"Slot {slot}", "state_topic": state, "device": ams_device(slot[0]),
            "value_template": f"{{{{ value_json.ams['{slot}'].material if (value_json.ams['{slot}'] is defined"
                              f" and not value_json.ams['{slot}'].empty) else 'empty' }}}}",
            "json_attributes_topic": state,
            "json_attributes_template": f"{{{{ value_json.ams['{slot}'] | tojson }}}}"}))
        out.append(("sensor", f"ams_{low}_remain", {"name": f"Slot {slot} remaining", "state_topic": state,
                                                    "device": ams_device(slot[0]), "unit_of_measurement": "%",
                                                    "value_template": f"{{{{ value_json.ams['{slot}'].remain }}}}"}))
        out.append(("sensor", f"ams_{low}_state", {"name": f"Raw slot {slot} state", "state_topic": state,
                                                    "device": ams_device(slot[0]), "entity_category": "diagnostic",
                                                    "value_template": f"{{{{ value_json.ams['{slot}'].state }}}}"}))
    for unit in units:  # a BMCU unit: no humidity sensor, no RFID reader, so only its slots and raw info.
        out.append(("sensor", f"ams_{unit.lower()}_info", {
            "name": f"Raw AMS {unit} info", "state_topic": state, "device": ams_device(unit),
            "entity_category": "diagnostic",
            "value_template": f"{{{{ value_json.ams_units_details['{unit}'].info }}}}"}))
    if units:
        out.append(("sensor", "ams_status", {"name": "AMS status", "state_topic": state,
                                              "value_template": "{{ value_json.ams_status }}"}))
    out.append(("select", "speed", {
        "name": "Speed", "state_topic": state, "options": list(SPEEDS),
        "value_template": "{{ {1: 'silent', 2: 'standard', 3: 'sport', 4: 'ludicrous'}"
                          ".get(value_json.speed_level, 'unknown') }}",
        "command_topic": f"pygmy/{alias}/cmd/speed"}))
    for obj, action, field, name, fan in (("set_fan_part", "fan_part", "fan", "Set part fan", "part"),
                                          ("set_fan_aux", "fan_aux", "fan_aux", "Set aux fan", "aux"),
                                          ("set_fan_chamber", "fan_chamber", "fan_chamber", "Set chamber fan",
                                           "chamber")):
        if fans is not None and fan not in fans:
            continue
        out.append(("number", obj, {"name": name, "state_topic": state,
                                    "value_template": f"{{{{ value_json.{field} }}}}",
                                    "command_topic": f"pygmy/{alias}/cmd/{action}", "min": 0, "max": 100,
                                    "step": 10, "unit_of_measurement": "%", "mode": "slider"}))
    for key, name in (("auto_recovery", "Step loss recovery"), ("sound_enable", "Printer sounds"),
                      ("filament_tangle_detect", "Tangle detection"),
                      ("bff_spaghetti", "Spaghetti alarm (Pygmy)"),
                      ("bff_spaghetti_pause", "Pause on spaghetti (Pygmy)")):
        if options is not None and key not in options and not key.startswith("bff_"):
            continue
        out.append(("switch", f"opt_{key}", {"name": name, "state_topic": state,
                                             "value_template": f"{{{{ 'ON' if value_json.options.{key} else 'OFF' }}}}",
                                             "command_topic": f"pygmy/{alias}/cmd/opt_{key}",
                                             "payload_on": "ON", "payload_off": "OFF"}))
    for component, obj, name, extra in MORE_SENSORS:
        cfg = {"name": name, "state_topic": state, **extra}
        if "json_attributes_topic" in cfg:
            cfg["json_attributes_topic"] = state
        out.append((component, obj, cfg))
    for component, obj, name, extra in MORE_BINARY:
        out.append((component, obj, {"name": name, "state_topic": state, **extra}))
    if xcam_on:
        out.append(("switch", "plate_detection", {"name": "Build plate detection", "state_topic": state,
                    "value_template": "{{ 'ON' if value_json.plate_detection else 'OFF' }}",
                    "command_topic": f"pygmy/{alias}/cmd/plate_detection",
                    "payload_on": "ON", "payload_off": "OFF"}))
    if chamber:
        out.append(("sensor", "chamber_temp", {"name": "Chamber", "state_topic": state,
                    "unit_of_measurement": "°C", "device_class": "temperature",
                    "value_template": "{{ value_json.chamber_temp }}"}))
        out.append(("binary_sensor", "door", {"name": "Door", "state_topic": state,
                    "device_class": "door", "value_template": "{{ value_json.door }}"}))
    for obj, label in DIAG_FIELDS:
        out.append(("sensor", obj, {"name": f"Raw {label}", "state_topic": state,
                    "entity_category": "diagnostic",
                    "value_template": f"{{{{ value_json.diag.{obj} }}}}"}))
    out.append(("camera", "last_photo", {"name": "Last print", "topic": f"pygmy/{alias}/photo"}))
    # The select carries the speed sensor's state plus control; HA still needs a distinct unique_id.
    unique = {"select/speed": "speed_select"}
    return [(f"{DISCOVERY}/{c}/pygmy_{alias}/{obj}/config",
             {**base, **cfg, "unique_id": f"pygmy_{alias}_{unique.get(f'{c}/{obj}', obj)}",
              "default_entity_id": f"{c}.pygmy_{alias}_{obj}"})
            for c, obj, cfg in out]


def dry_control_topics(serial):
    """The config topics of the entities that start drying, to empty them when the printer cannot."""
    return [f"{DISCOVERY}/{c}/pygmy_ams/{serial.lower()}/{obj}/config" for c, obj in DRY_CONTROLS]


def bmcu_dropped_topics(alias, letter):
    """Config topics of the BMCU entities that no longer exist: an empty payload makes HA remove them."""
    low = letter.lower()
    topics = [f"{DISCOVERY}/sensor/pygmy_{alias}/ams_{low}_{field}/config"
              for field in ("humidity", "humidity_pct", "temp", "dry_time")]
    for n in "1234":
        topics += [f"{DISCOVERY}/button/pygmy_{alias}/read_{low}{n}/config"]
    return topics


def serial_unit_discovery(serial, model, alias, letter, printer_name, ams_drying=True):
    """Discovery for an AMS unit keyed by its serial, following the printer it is plugged into.

    The device is stable across printer moves (entity ids stay), only via_device follows the printer.
    Slot entities are numbered 1..4, not lettered: the state carries the printer-level position.
    """
    s = serial.lower()
    device = {"identifiers": [f"pygmy_ams_{s}"], "name": f"AMS {serial[-4:]}", "model": model,
              "manufacturer": "Bambu Lab", "via_device": f"pygmy_{alias}"}
    base = {"availability_topic": AVAILABILITY, "device": device}
    state = f"pygmy/ams/{s}/state"
    out = []

    def emit(component, obj, cfg):
        cfg["unique_id"] = f"pygmy_ams_{s}_{obj}"
        cfg["default_entity_id"] = f"{component}.pygmy_ams_{s}_{obj}"
        out.append((f"{DISCOVERY}/{component}/pygmy_ams_{s}/{obj}/config", {**base, **cfg}))

    emit("sensor", "printer", {"name": "Printer", "state_topic": state,
                                "value_template": "{{ value_json.printer }}"})
    emit("sensor", "position", {"name": "Position", "state_topic": state,
                                "value_template": "{{ value_json.position }}"})
    emit("sensor", "humidity", {"name": "Humidity", "state_topic": state,
                                "value_template": "{{ value_json.humidity }}"})
    emit("sensor", "humidity_pct", {"name": "Humidity %", "state_topic": state,
                                    "value_template": "{{ value_json.humidity_pct }}",
                                    "unit_of_measurement": "%"})
    emit("sensor", "temp", {"name": "Temperature", "state_topic": state,
                            "value_template": "{{ value_json.temp }}", "unit_of_measurement": "°C",
                            "device_class": "temperature"})
    emit("sensor", "dry_time", {"name": "Drying time left", "state_topic": state,
                                "value_template": "{{ value_json.dry_time }}",
                                "unit_of_measurement": "min"})
    if model in DRY_MODELS:
        emit("binary_sensor", "drying", {"name": "Drying", "state_topic": state,
                                         "value_template": "{{ 'ON' if value_json.drying else 'OFF' }}",
                                         "device_class": "heat"})
        emit("sensor", "dry_target", {"name": "Drying target", "state_topic": state,
                                      "value_template": "{{ value_json.dry_target }}",
                                      "unit_of_measurement": "°C", "device_class": "temperature",
                                      "suggested_display_precision": 0})
    if model in DRY_MODELS and ams_drying:  # the printer must be able to start it too, not just the AMS
        emit("number", "dry_temp", {"name": "Drying temperature", "state_topic": state,
                                    "value_template": "{{ value_json.dry_temp }}",
                                    "command_topic": f"pygmy/ams/{s}/cmd/dry_temp",
                                    "min": 45, "max": DRY_MODELS[model], "step": 5, "mode": "box",
                                    "unit_of_measurement": "°C"})
        emit("number", "dry_hours", {"name": "Drying hours", "state_topic": state,
                                     "value_template": "{{ value_json.dry_hours }}",
                                     "command_topic": f"pygmy/ams/{s}/cmd/dry_hours",
                                     "min": 1, "max": 24, "step": 1, "mode": "box"})
        emit("button", "dry_start", {"name": "Start drying",
                                     "command_topic": f"pygmy/ams/{s}/cmd/dry_start", "payload_press": "PRESS"})
        emit("button", "dry_stop", {"name": "Stop drying",
                                    "command_topic": f"pygmy/ams/{s}/cmd/dry_stop", "payload_press": "PRESS"})
    emit("sensor", "info", {"name": "Raw AMS info", "state_topic": state,
                            "value_template": "{{ value_json.info }}", "entity_category": "diagnostic"})
    for n in range(1, 5):
        slot = f"slot_{n}"
        emit("sensor", slot, {"name": f"Slot {n}", "state_topic": state,
             "value_template": f"{{{{ value_json.{slot}.material if (value_json.{slot} is defined"
                               f" and not value_json.{slot}.empty) else 'empty' }}}}",
             "json_attributes_topic": state,
             "json_attributes_template": f"{{{{ value_json.{slot} | tojson }}}}"})
        emit("sensor", f"{slot}_remain", {"name": f"Slot {n} remaining", "state_topic": state,
             "value_template": f"{{{{ value_json.{slot}.remain }}}}", "unit_of_measurement": "%"})
        emit("button", f"read_{n}", {"name": f"Re-read filament {n}",
             "command_topic": f"pygmy/ams/{s}/cmd/read_{n}", "payload_press": "PRESS"})
        emit("sensor", f"raw_slot_{n}_state", {"name": f"Raw slot {n} state", "state_topic": state,
             "value_template": f"{{{{ value_json.{slot}.state }}}}", "entity_category": "diagnostic"})
    return out


def parse_ams_command(topic, payload, ams_location):
    """(alias, action, value) for a command on a serial-keyed AMS unit, resolving where the serial is now."""
    parts = topic.split("/")
    if len(parts) != 5 or parts[0] != "pygmy" or parts[1] != "ams" or parts[3] != "cmd":
        return None
    loc = ams_location.get(parts[2])
    if loc is None:
        return None
    action = parts[4]
    text = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else str(payload)
    if action.startswith("read_") and action[5:] in "1234" and text == "PRESS":
        slot = f"{loc['letter']}{action[5:]}"
        return (loc["alias"], f"read_{slot}".lower(), slot)
    if action in ("dry_temp", "dry_hours"):
        try:
            value = int(float(text))
        except (ValueError, OverflowError):
            return None
        return (loc["alias"], action, (parts[2], loc["letter"], value))
    if action in ("dry_start", "dry_stop") and text == "PRESS":
        return (loc["alias"], action, (parts[2], loc["letter"]))
    return None


def parse_command(topic, payload, aliases):
    """(alias, action, value) for a valid HA command, else None. Anything else is ignored."""
    parts = topic.split("/")
    if len(parts) != 4 or parts[0] != "pygmy" or parts[2] != "cmd" or parts[1] not in aliases:
        return None
    alias, action = parts[1], parts[3]
    text = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else str(payload)
    if action == "light" or action.startswith("light_"):
        return (alias, action, text == "ON") if text in ("ON", "OFF") else None
    if action == "plate_detection":
        return (alias, action, text == "ON") if text in ("ON", "OFF") else None
    if action == "speed":
        return (alias, action, SPEEDS.index(text) + 1) if text in SPEEDS else None
    if action in FANS:
        try:
            value = int(float(text))
        except (ValueError, OverflowError):
            return None
        return (alias, action, value) if 0 <= value <= 100 else None
    if action.startswith("opt_"):
        return (alias, action, text == "ON") if action[4:] in OPTIONS and text in ("ON", "OFF") else None
    if action.startswith("read_"):
        return (alias, action, action[5:].upper()) if action[5:] in READABLE_SLOTS and text == "PRESS" else None
    if action in ("pause", "resume", "stop", "plate_cleared", "clear_errors", "dismiss_spaghetti"):
        return (alias, action, None) if text == "PRESS" else None
    return None


class HaBridge:
    def __init__(self, host, port, user, password, aliases, read_status, run_action, names=None, lang="en",
                 snapshot=None, profile=None, ams_drying=None):
        self.names = names or {}
        self.host, self.port, self.user, self.password = host, port, user, password
        self.aliases = tuple(aliases)
        self.read_status = read_status  # alias -> status dict (the status tool's output)
        self.run_action = run_action  # (alias, action, value, key) -> None, raises on refusal
        self.snapshot = snapshot  # alias -> JPEG bytes or None
        self.profile = profile  # alias -> what the printer has, or None for the full set
        self.can_dry = ams_drying  # alias -> the model can start AMS drying; None keeps the full set
        self.client = None
        self.last = {}
        self.layout = {}  # alias -> (slots, units, fans, options) deciding its discovery
        self.lights = {}  # alias -> tuple of (name, label) the printer reports
        self.lang = lang
        self.started = {}  # alias -> monotonic time the bridge saw the print start
        self.ams_location = {}  # serial lower -> {"alias", "letter", "model"} where the unit is now
        self.ams_disc = {}  # serial lower -> (alias, letter, model) last discovered
        self.ams_last_state = {}  # serial lower -> last JSON published on its state topic
        self.dry_setting = {}  # serial lower -> {"temp", "hours"} chosen in HA, sent on start

    def start(self):
        import paho.mqtt.client as mqtt
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="pygmy-ha")
        client.username_pw_set(self.user, self.password)
        client.will_set(AVAILABILITY, "offline", retain=True)
        client.reconnect_delay_set(1, 30)
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        self.client = client
        client.connect_async(self.host, self.port, keepalive=30)
        client.loop_start()
        threading.Thread(target=self._publish_loop, daemon=True).start()

    def _on_connect(self, client, _userdata, _flags, reason, _props):
        if reason.is_failure:
            print(f"ha: connessione rifiutata ({reason})", file=sys.stderr)
            return
        for alias in self.aliases:
            self._publish_discovery(client, alias)
        client.publish(AVAILABILITY, "online", retain=True)
        client.subscribe("pygmy/+/cmd/+")
        self.last = {}  # Republish every state after a reconnect.
        print("ha: connessa", file=sys.stderr)

    def _profile(self, alias):
        return self.profile(alias) if self.profile else None

    def _ams_drying(self, alias):
        return self.can_dry(alias) if self.can_dry else True

    def _printer_name(self, alias):
        return self.names.get(alias) or alias.upper()

    def _old_ams_topics(self, alias, letter):
        """The old-scheme (pygmy_<alias>_ams_<letter>) config topics of one AMS unit letter."""
        low = letter.lower()
        topics = [f"{DISCOVERY}/sensor/pygmy_{alias}/ams_{low}_humidity/config",
                  f"{DISCOVERY}/sensor/pygmy_{alias}/ams_{low}_temp/config",
                  f"{DISCOVERY}/sensor/pygmy_{alias}/ams_{low}_humidity_pct/config",
                  f"{DISCOVERY}/sensor/pygmy_{alias}/ams_{low}_dry_time/config",
                  f"{DISCOVERY}/sensor/pygmy_{alias}/ams_{low}_info/config"]
        for n in "1234":
            sl = f"{low}{n}"
            topics += [f"{DISCOVERY}/sensor/pygmy_{alias}/ams_{sl}/config",
                       f"{DISCOVERY}/button/pygmy_{alias}/read_{sl}/config",
                       f"{DISCOVERY}/sensor/pygmy_{alias}/ams_{sl}_remain/config",
                       f"{DISCOVERY}/sensor/pygmy_{alias}/ams_{sl}_state/config"]
        return topics

    def _clear_old_scheme(self, alias, letter):
        """Empty retained configs drop the old per-letter AMS device (e.g. "Alpha AMS A")."""
        for topic in self._old_ams_topics(alias, letter):
            self.client.publish(topic, "", retain=True)

    def _publish_discovery(self, client, alias):
        slots, units, _, _ = self.layout.setdefault(alias, ((), (), (), ()))
        lights = self.lights.get(alias, ())
        for topic, config in discovery(alias, self.names.get(alias), slots, units, profile=self._profile(alias),
                                       lights=lights):
            client.publish(topic, json.dumps(config), retain=True)
        if len(lights) > 1:  # per-light entities replace the single Light one
            client.publish(light_config_topic(alias), "", retain=True)
        for unit in units:  # a BMCU has no humidity/RFID entities: retire any retained from before.
            for topic in bmcu_dropped_topics(alias, unit):
                client.publish(topic, "", retain=True)
        dry = self._ams_drying(alias)
        for serial, loc in self.ams_location.items():
            if loc["alias"] == alias:
                for topic, config in serial_unit_discovery(serial, loc["model"], alias, loc["letter"],
                                                           self._printer_name(alias), dry):
                    client.publish(topic, json.dumps(config), retain=True)
                if not dry:
                    for topic in dry_control_topics(serial):
                        client.publish(topic, "", retain=True)

    def add_alias(self, alias, name=None):
        """A printer added from the app: its entities appear without a restart."""
        self.names[alias] = name or self.names.get(alias)
        if alias not in self.aliases:
            self.aliases = self.aliases + (alias,)
        if self.client is not None and self.client.is_connected():
            self._publish_discovery(self.client, alias)

    def remove_alias(self, alias):
        """Empty retained configs make HA drop the entities."""
        self.aliases = tuple(a for a in self.aliases if a != alias)
        self.last.pop(alias, None)
        slots, units, _, _ = self.layout.pop(alias, ((), (), (), ()))
        lights = self.lights.pop(alias, ())
        if self.client is not None:
            for topic, _ in discovery(alias, self.names.get(alias), slots, units, profile=self._profile(alias)):
                self.client.publish(topic, "", retain=True)
            self.client.publish(light_config_topic(alias), "", retain=True)
            for l in lights:
                self.client.publish(light_config_topic(alias, light_slug(l[0])), "", retain=True)
        self.names.pop(alias, None)

    def _on_message(self, _client, _userdata, msg):
        command = parse_command(msg.topic, msg.payload, self.aliases)
        if command is None and msg.topic.startswith("pygmy/ams/"):
            parts = msg.topic.split("/")
            if len(parts) == 5 and parts[3] == "cmd":
                if parts[2] in self.ams_location:
                    command = parse_ams_command(msg.topic, msg.payload, self.ams_location)
                else:
                    print("ha: comando AMS ignorato: nessuna stampante collegata", file=sys.stderr)
                    return
        if command is None:
            return
        if command[1] in ("dry_temp", "dry_hours"):
            serial, _letter, value = command[2]
            self.dry_settings(serial)["temp" if command[1] == "dry_temp" else "hours"] = value
            return
        if command[1] in ("dry_start", "dry_stop"):
            alias, action, (serial, letter) = command
            setting = self.dry_settings(serial)
            command = (alias, action, {"letter": letter, **(setting if action == "dry_start" else {})})
        # Off the MQTT thread: a command waits for the printer's answer.
        threading.Thread(target=self._run, args=command, daemon=True).start()

    def dry_settings(self, serial):
        """The drying temperature and hours this unit last had in HA: kept here until Start."""
        return self.dry_setting.setdefault(serial, dict(DRY_DEFAULT))

    def _run(self, alias, action, value):
        try:
            self.run_action(alias, action, value, "ha-" + uuid.uuid4().hex)
            print(f"ha: {alias} {action} eseguito", file=sys.stderr)
        except Exception as err:  # Logged, never retried: a command is sent at most once.
            print(f"ha: {alias} {action} rifiutato: {err}", file=sys.stderr)

    def _serial_state(self, unit, alias, printer_name):
        """The JSON for one serial-keyed AMS unit's state topic."""
        out = {}
        h = unit.get("humidity")
        out["humidity"] = "EDCBA"[h - 1] if isinstance(h, int) and 1 <= h <= 5 else None
        # First-gen AMS sends impossible values: temp 6503.6, humidity_raw 0 (no % sensor). Drop them.
        raw_temp = unit.get("temp")
        out["temp"] = raw_temp if isinstance(raw_temp, (int, float)) and -20 <= raw_temp <= 120 else None
        raw_hum = unit.get("humidity_raw")
        out["humidity_pct"] = raw_hum if isinstance(raw_hum, (int, float)) and 1 <= raw_hum <= 100 else None
        out["dry_time"] = unit.get("dry_time")
        if unit.get("model") in DRY_MODELS:
            out["drying"] = bool(unit.get("drying"))
            out["dry_target"] = unit.get("dry_target")
            setting = self.dry_settings((unit.get("serial") or "").lower())
            out["dry_temp"], out["dry_hours"] = setting["temp"], setting["hours"]
        out["info"] = unit.get("info")
        out["printer"] = printer_name
        out["position"] = unit.get("unit")
        for slot in unit.get("slots") or []:
            pos = slot.get("slot")
            if not pos:
                continue
            key = f"slot_{pos[1]}"
            color = str(slot.get("color") or "")[:6]
            entry = {"material": slot.get("material"), "name": slot.get("name"),
                     "color": f"#{color}" if color else None, "remain": slot.get("remain"),
                     "active": bool(slot.get("active")), "empty": bool(slot.get("empty")),
                     "profile_id": slot.get("profile_id"), "position": pos}
            for k in ("nozzle_temp_min", "nozzle_temp_max", "drying_temp", "drying_hours", "state"):
                if k in slot:
                    entry[k] = slot[k]
            out[key] = entry
        return out

    def _publish_ams(self, alias, status, seen):
        """Discovery and state for the serial-keyed AMS units a printer reports right now."""
        printer_name = self._printer_name(alias)
        for unit in status.get("ams_units") or []:
            serial = (unit.get("serial") or "").strip()
            if not serial:
                continue
            s = serial.lower()
            letter, model = unit.get("unit"), unit.get("model") or "AMS"
            seen[s] = {"alias": alias, "letter": letter, "model": model}
            if self.ams_disc.get(s) != (alias, letter, model):
                dry = self._ams_drying(alias)
                for topic, config in serial_unit_discovery(serial, model, alias, letter, printer_name, dry):
                    self.client.publish(topic, json.dumps(config), retain=True)
                if not dry:
                    for topic in dry_control_topics(serial):
                        self.client.publish(topic, "", retain=True)
                self._clear_old_scheme(alias, letter)
                self.ams_disc[s] = (alias, letter, model)
            state = json.dumps(self._serial_state(unit, alias, printer_name))
            if state != self.ams_last_state.get(s):
                self.client.publish(f"pygmy/ams/{s}/state", state, retain=True)
                self.ams_last_state[s] = state

    def _forget_gone_ams(self, seen, read_ok):
        """A serial on no successfully-read printer keeps its discovery; its state says printer none, slots unchanged."""
        for s, loc in list(self.ams_location.items()):
            if s in seen:
                continue
            if loc["alias"] not in read_ok:
                continue  # that printer failed to report: not a confirmed unplug
            prev = json.loads(self.ams_last_state.get(s, "{}"))
            prev.update({"printer": "none", "position": "none"})
            state = json.dumps(prev)
            self.client.publish(f"pygmy/ams/{s}/state", state, retain=True)
            self.ams_last_state[s] = state
        self.ams_location = seen

    def publish_states(self):
        seen = {}
        read_ok = set()
        for alias in self.aliases:
            try:
                status = self.read_status(alias)
                payload = json.dumps(flat_state(status))
            except Exception as err:
                print(f"ha: stato {alias} non letto: {err}", file=sys.stderr)
                continue
            read_ok.add(alias)
            self._remember_layout(alias, status)
            self._publish_ams(alias, status, seen)
            if payload != self.last.get(alias) and self.client.publish(f"pygmy/{alias}/state", payload).rc == 0:
                prev = json.loads(self.last[alias]) if self.last.get(alias) else None
                self.last[alias] = payload
                cur = json.loads(payload)
                minutes = self.track(alias, prev, cur)
                # ponytail: an event lost during an MQTT outage is not queued; HA sees the state anyway.
                for kind, message in events(prev, cur, self.lang, minutes):
                    if kind in ("finished", "failed"):
                        self._publish_photo(alias)
                    self.client.publish(f"pygmy/{alias}/event", json.dumps(
                        {"event_type": kind, "title": self.names.get(alias) or alias.upper(), "message": message}))
        self._forget_gone_ams(seen, read_ok)

    def _remember_layout(self, alias, status):
        """AMS slots, units and the printer's profile decide the entities: when they change, discovery goes out again."""
        units = status.get("ams_units") or []
        plain = [u for u in units if not (u.get("serial") or "").strip()]
        learned = self._profile(alias) or {}
        layout = (tuple(slot["slot"] for unit in plain for slot in unit.get("slots") or () if slot.get("slot")),
                  tuple(unit["unit"] for unit in plain if unit.get("unit")),
                  tuple(learned.get("fans") or ()), tuple(learned.get("options") or ()))
        lights = tuple((l.get("name"), l.get("label")) for l in status.get("lights") or () if l.get("name"))
        if self.layout.get(alias) == layout and self.lights.get(alias) == lights:
            return
        # _on_connect/_add_alias publish with the layout they know (empty at first), so an alias already
        # in self.layout has entities out there that now need the AMS ones too.
        announced = alias in self.layout
        prev_units = set(self.layout.get(alias, ((), (), (), ()))[1])
        prev_lights = self.lights.get(alias, ())
        self.layout[alias] = layout
        self.lights[alias] = lights
        if announced and self.client is not None:
            self._publish_discovery(self.client, alias)
            for gone in prev_units - set(layout[1]):
                self._clear_old_scheme(alias, gone)  # a no-serial unit left: drop its old-scheme device
            for gone in prev_lights:
                if gone not in lights:  # a light went away: drop its retained entity
                    self.client.publish(light_config_topic(alias, light_slug(gone[0])), "", retain=True)

    def _publish_photo(self, alias):
        """The print's last frame with the finished/failed notification; a missing photo never stops it."""
        if self.snapshot is None:
            return
        try:
            photo = self.snapshot(alias)
        except Exception as err:
            print(f"ha: foto {alias} non letta: {err}", file=sys.stderr)
            return
        if photo:
            self.client.publish(f"pygmy/{alias}/photo", photo, retain=True)

    def track(self, alias, prev, cur, clock=time.monotonic):
        """Minutes since the bridge saw this print start, or None (e.g. it started before a restart)."""
        was, now = str((prev or {}).get("state") or "").upper(), str(cur.get("state") or "").upper()
        if now in RUNNING and was not in RUNNING and prev is not None:
            self.started[alias] = clock()
        start = self.started.get(alias)
        if now not in RUNNING:
            self.started.pop(alias, None)
        return round((clock() - start) / 60) if start is not None else None

    def _publish_loop(self):
        while True:
            if self.client.is_connected():
                self.publish_states()
            time.sleep(PUBLISH_S)
