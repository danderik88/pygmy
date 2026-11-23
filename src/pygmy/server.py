#!/usr/bin/env python3
"""MCP server for Bambu printers on the LAN: JSON-RPC over HTTP, stdlib only.

Results are built from allowlisted fields, so serial, IP and tokens cannot leak.
Commands exist only with BFF_MCP_CONTROL=1. Each one has a state precondition and
an observed outcome; an unclear outcome locks the printer until `recheck`.
"""
# ponytail: no MCP SDK, these tools need neither SSE nor sessions; adopt it if streaming is ever needed.
import base64
import hashlib
import hmac
import io
import inspect
import json
import math
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib

# ponytail: users write PYGMY_*, the code still reads BFF_*; rename inside when convenient.
for _k, _v in list(os.environ.items()):
    if _k.startswith("PYGMY_"):
        os.environ.setdefault("BFF_" + _k[len("PYGMY_"):], _v)
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))  # Sibling modules, on the CT and in tests.
from printer_link import PUSHALL, PrinterLink, check_fingerprint, supports_flow_cali, with_sequence  # noqa: E402
from moonraker_link import MoonrakerLink, SPEED_PERCENT  # noqa: E402
import camera  # noqa: E402
import spaghetti  # noqa: E402
import devices  # noqa: E402
import ftps  # noqa: E402
import go2rtc  # noqa: E402
import ha_mqtt  # noqa: E402
import notify  # noqa: E402
import onboard  # noqa: E402
import preview  # noqa: E402
import slicer  # noqa: E402
import fetch  # noqa: E402
try:
    import makerworld  # noqa: E402
except ImportError:  # public build: MakerWorld sign-in needs a non-public account API
    import makerworld_off as makerworld  # noqa: E402
import library  # noqa: E402

CFG = {}


def env_public_url(env=None):
    """The secure https address tailscale.sh publishes, or empty: the app offers a one-tap switch to it."""
    env = os.environ if env is None else env
    return (env.get("BFF_PUBLIC_URL") or env.get("PYGMY_PUBLIC_URL") or "").strip().rstrip("/")


LINKS = {}
DB = None
DB_PATH = None  # state.db, for threads that open their own connection
NOTIFIER = None
# ponytail: one lock for all printers, so a slow command delays the others by up to the timeout. Per-printer locks if that hurts.
COMMAND_LOCK = threading.Lock()
# Wakes the print-now worker when a server-engine request appears or its queue state changes.
SLICE_WAKE = threading.Event()
# Photo id -> (expiry, jpeg). In memory on purpose: a photo must not outlive its confirm token.
PHOTOS = {}
# (printer, job) -> {"png": base64|None, "name": job}: the app polls this on the Status tab, one fetch per job.
PREVIEWS = {}
MAX_BODY = 1 << 20
MAX_UPLOAD = 100 << 20
MAX_METADATA = 8 << 20
DEFAULT_PROTOCOL = "2025-06-18"
TOKEN_TTL_S = 120
APP_PAGE = Path(__file__).with_name("app.html")
STATIC = {"/sw.js": ("text/javascript; charset=utf-8", "sw.js"),
          "/manifest.json": ("application/manifest+json", "manifest.json"),
          "/icon-192.png": ("image/png", "icon-192.png"),
          "/icon-512.png": ("image/png", "icon-512.png"),
          "/icon-mono.png": ("image/png", "icon-mono.png")}
POLL_S = 0.25
DEFAULT_TIMEOUT_S = 15
UNKNOWN = "esito non osservato: usa recheck, nessun reinvio automatico"
# Typical g/cm3, used only when the slicer profile says 0 (OrcaSlicer 2.4.2 CLI). The result is an estimate.
TYPICAL_DENSITY = {"PLA": 1.24, "PETG": 1.27, "ABS": 1.04, "ASA": 1.07, "TPU": 1.21}

STATUS_FIELDS = (
    "connected", "state", "subtask_name", "progress", "remaining_time", "layer_num",
    "total_layers", "temperatures", "door_open", "speed_level", "chamber_light", "lights", "print_error",
    "stage", "prepare_percent", "fans", "xcam", "options",
    "speed_percent", "heatbreak_fan", "print_source", "pressure_advance", "nozzle_diameter",
    "nozzle_type", "wifi_signal", "sd_card", "filament_sensor", "tray_now", "ams_status",
    "firmware_update", "timelapse", "camera_recording", "plate_detection", "diagnostics",
    "firmware_version",
)
SEVERITY = {0: "non valido", 1: "errore (stampa ferma)", 2: "avviso (stampa in pausa)", 3: "notifica"}
# Filament-path prompts seen on the real A1: a person has to look or act. Anything else counts as safety.
# ponytail: exact codes only, add new ones as they are seen; a code answerable by the agent needs an answer tool first.
HANDS_ON_HMS = {(7, "0x20002"), (7, "0x8006"), (7, "0x8007")}
NO_FILAMENT_TRAY = 255
# Bambu gcode_state values; `from` is the precondition, `to` the observed outcome.
ACTIONS = {
    "pause": {"from": ("RUNNING",), "to": ("PAUSE",)},
    "resume": {"from": ("PAUSE",), "to": ("RUNNING",)},
    "stop": {"from": ("RUNNING", "PAUSE", "PREPARE"), "to": ("FAILED", "IDLE", "FINISH")},
    "temperature": {"from": None, "to": None},
    "start": {"from": ("IDLE", "FINISH", "FAILED"), "to": ("PREPARE", "RUNNING")},
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS commands (key TEXT PRIMARY KEY, printer TEXT, action TEXT, params TEXT, outcome TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS locks (printer TEXT PRIMARY KEY, key TEXT);
CREATE TABLE IF NOT EXISTS tokens (hash TEXT PRIMARY KEY, printer TEXT, action TEXT, params TEXT, expires REAL);
CREATE TABLE IF NOT EXISTS uploads (file_id INTEGER PRIMARY KEY, name TEXT, model TEXT, nozzle TEXT, filament TEXT, time_s INTEGER, weight_g REAL);
CREATE TABLE IF NOT EXISTS files (file_id INTEGER PRIMARY KEY, data BLOB);
CREATE TABLE IF NOT EXISTS settings (printer TEXT, key TEXT, value TEXT, PRIMARY KEY (printer, key));
CREATE TABLE IF NOT EXISTS print_requests (id INTEGER PRIMARY KEY, printer TEXT, url TEXT, created REAL, done INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS queue (id INTEGER PRIMARY KEY, printer TEXT, file_id INTEGER, own INTEGER, created REAL);
CREATE TABLE IF NOT EXISTS models (request_id INTEGER PRIMARY KEY, data BLOB);
"""


class ToolError(Exception):
    """Message safe to show to the agent."""


def printer_id(args):
    alias = args.get("printer")
    if alias not in CFG["printers"]:
        raise ToolError("Stampante sconosciuta. Alias validi: " + ", ".join(sorted(CFG["printers"])))
    return CFG["printers"][alias]


def as_text(data):
    return [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]


def link_of(alias):
    if alias not in LINKS:
        raise ToolError(f"Collegamento diretto con {alias} non disponibile")
    return LINKS[alias]


def alias_of(pid):
    return next(alias for alias, known in CFG["printers"].items() if known == pid)


def read_status(alias):
    return link_of(alias).snapshot()


def status_of(args):
    printer_id(args)  # Rejects unknown aliases.
    return read_status(args["printer"])


LAST_STATE = {}
ENDED, STARTED = ("FINISH", "FAILED"), ("PREPARE", "RUNNING")


def track_plate(alias, state):
    """BFF owns "plate to clear": on when a print ends, off when the next one starts or someone clears it.

    The printer does not report it. A restart forgets the previous state, so no edge is invented after one.
    """
    prev, LAST_STATE[alias] = LAST_STATE.get(alias), state
    if prev is None or prev == state:
        return
    if prev in ("RUNNING", "PAUSE") and state in ENDED:
        set_plate_pending(alias, True)
    elif state in STARTED and prev not in ("RUNNING", "PAUSE", "PREPARE"):
        set_plate_pending(alias, False)


def set_plate_pending(alias, on):
    with DB:
        DB.execute("INSERT OR REPLACE INTO settings VALUES (?, 'plate_pending', ?)", (alias, "1" if on else "0"))


def tool_plate_cleared(args):
    printer_id(args)
    set_plate_pending(args["printer"], False)
    return as_text({"printer": args["printer"], "awaiting_plate_clear": False})


def watch_plates():
    """Every 5 s: the plate flag must not depend on someone polling."""
    while True:
        for alias in list(LINKS):
            try:
                track_plate(alias, str(link_of(alias).snapshot().get("state") or "").upper() or None)
            except Exception as err:  # The next round tries again.
                print(f"piatto {alias}: {err}", file=sys.stderr, flush=True)
        time.sleep(5)


WATCHES = {}


def _switch_on(alias, key):
    """A per-printer BFF switch kept in settings; on unless the user switched it off in the app."""
    try:
        row = DB.execute("SELECT value FROM settings WHERE printer=? AND key=?", (alias, key)).fetchone()
    except (AttributeError, sqlite3.Error):  # no database (status read in tests): the switch stays on
        return True
    return not row or row[0] != "off"


def spaghetti_watch_on(alias):
    """BFF's own spaghetti alarm (Obico), per printer; on unless the user switched it off in the app."""
    return _switch_on(alias, "bff_spaghetti")


def spaghetti_pause_on(alias):
    """Pause the print when the alarm turns to failure, per printer; on by default."""
    return _switch_on(alias, "bff_spaghetti_pause")


FLOW_CALI_MODES = ("auto", "on", "off")


def flow_cali(alias):
    """Flow Dynamics Calibration at print start; on unless the user chose otherwise in the app."""
    try:
        row = DB.execute("SELECT value FROM settings WHERE printer=? AND key='bff_flow_cali'", (alias,)).fetchone()
    except (AttributeError, sqlite3.Error):  # no database (status read in tests): the default applies
        return "on"
    return row[0] if row and row[0] in FLOW_CALI_MODES else "on"


def flow_cali_shown(alias, raw):
    """The firmware flag is not enough: the P1 start gcode has no flow calibration step at all."""
    try:
        model = str(load_profile(alias).get("model") or "")
    except ToolError:
        model = ""
    return is_bambu(alias) and supports_flow_cali(raw) and not model.startswith("P1")


def is_bambu(alias):
    return not isinstance(LINKS.get(alias), MoonrakerLink)  # no link yet (or in tests): not Klipper


def note_spaghetti(alias, watch):
    """Log every verdict change; keep the frame of each alarm to look at afterwards.
    A failure pauses the print once, when the printer's Pause on spaghetti option is on."""
    print(f"spaghetti {alias}: {watch.verdict} score={watch.score} error={watch.error}", file=sys.stderr, flush=True)
    if watch.verdict in ("warning", "failure") and watch.last_jpeg:
        folder = Path(os.environ.get("STATE_DIRECTORY") or ".") / "spaghetti"
        folder.mkdir(exist_ok=True)
        (folder / f"{alias}-{time.strftime('%Y%m%d-%H%M%S')}-{watch.verdict}.jpg").write_bytes(watch.last_jpeg)
    # watch.paused: None (not tried this print), True (paused), False (tried, the pause did not go through).
    if (watch.verdict == "failure" and not watch.muted and watch.state_of() == "RUNNING"
            and spaghetti_pause_on(alias) and getattr(watch, "paused", None) is None):
        watch.paused = True  # at once to the notifier, so its spaghetti alert says the print was paused
        try:  # the same path as the pause tool: Bambu MQTT and Moonraker both
            require_support(alias, "pause")
            reply = execute(alias, "pause", {}, secrets.token_urlsafe(16))
            watch.paused = reply.get("outcome") == "confirmed"
            if not watch.paused:
                print(f"spaghetti {alias}: pause {reply.get('outcome')}: {reply.get('detail')}", file=sys.stderr, flush=True)
        except Exception as err:  # a failed pause is logged; the alarm still notifies
            watch.paused = False
            print(f"spaghetti {alias}: pause failed: {err}", file=sys.stderr, flush=True)


def latest_frame(alias, max_age_s=5.0):
    """The camera's latest frame, pixels already upright for consumers that ignore EXIF (Obico, phones)."""
    try:
        jpeg = feed_of(alias).latest(max_age_s=max_age_s)
    except (ToolError, camera.CameraError):
        return None
    # Obico ignores the EXIF Orientation tag: hand it the pixels already turned. Same bytes go to
    # Obico, to spaghetti.stash and to the saved warning frame.
    link = LINKS.get(alias)  # the feed just read the webcam list: its cached flip is current, no extra request
    return camera.upright(jpeg, camera.exif_orientation(*link.cam_flip) if isinstance(link, MoonrakerLink) else 1)


def test_frame(alias):
    """spaghetti/test-<alias>.jpg in the state folder stands in for the camera while it exists:
    a way to try Pause on spaghetti from afar. Delete the file to go back to the camera."""
    path = Path(os.environ.get("STATE_DIRECTORY") or ".") / "spaghetti" / f"test-{alias}.jpg"
    try:
        return path.read_bytes()
    except OSError:
        return None


def watch_spaghetti():
    """During prints, ask Obico about the latest frame every BFF_OBICO_INTERVAL_S. Report, and pause
    on a failure when the printer's Pause on spaghetti option is on. Never resume."""
    def state_of(alias):
        return str(link_of(alias).snapshot().get("state") or "").upper()

    def detect(jpeg):
        url = f"{CFG['obico_frame_base']}/obico/{spaghetti.stash(jpeg)}.jpg"
        return spaghetti.ask_obico(CFG["obico_url"], CFG["obico_token"], url)

    while True:
        for alias in list(LINKS):
            if not spaghetti_watch_on(alias):
                WATCHES.pop(alias, None)
                continue
            watch = WATCHES.setdefault(alias, spaghetti.Watch(
                lambda a=alias: state_of(a), lambda a=alias: test_frame(a) or latest_frame(a), detect, CFG["obico_sensitivity"],
                lambda a=alias: link_of(a).snapshot().get("progress")))
            before = watch.verdict
            try:
                watch.tick()
                if watch.verdict != before:
                    note_spaghetti(alias, watch)
            except Exception as err:  # The next round tries again.
                print(f"spaghetti {alias}: {err}", file=sys.stderr, flush=True)
        time.sleep(CFG["obico_interval_s"])


def plate_clear_pending(alias):
    row = DB.execute("SELECT value FROM settings WHERE printer=? AND key='plate_pending'", (alias,)).fetchone()
    return bool(row and row[0] == "1")


def set_light(pid, on, light=None):
    link = link_of(alias_of(pid))
    if light is not None:
        names = [l.get("name") for l in link.snapshot().get("lights") or [] if l.get("name")]
        if light not in names:
            raise ToolError(f"Unknown light '{light}'. Available: {', '.join(names) or 'none'}")
    if not link.command("light", on=on, light=light):
        raise ToolError("Comando luce non partito")


def tool_set_light(args):
    on = args.get("on")
    if type(on) is not bool:
        raise ToolError("on deve essere true o false")
    light = args.get("light")
    if light is not None and not isinstance(light, str):
        raise ToolError("light deve essere una stringa")
    set_light(printer_id(args), on, light)
    return as_text({"printer": args["printer"], "light": light or "all", "on": on})


def hms_handling(item):
    return "persona" if (item.get("module"), item.get("code")) in HANDS_ON_HMS else "sicurezza"


def attention(raw):
    found = [{"handling": hms_handling(e), "description": e.get("description")} for e in raw.get("hms_errors") or []]
    # A Klipper error seen while idle (e.g. a probe miss during homing): shown and notified, not an active HMS fault.
    return found + ([{"handling": "sicurezza", "description": raw["idle_error"]}] if raw.get("idle_error") else [])


def tool_status(args):
    return as_text(status_dict(args))


def status_dict(args):
    raw = status_of(args)
    result = {key: raw.get(key) for key in STATUS_FIELDS}
    result["printer"] = args["printer"]
    result["hms_count"] = len(raw.get("hms_errors") or [])
    result["attention"] = attention(raw)
    if DB is not None:
        result["awaiting_plate_clear"] = plate_clear_pending(args["printer"])
    # Material and colour only: the raw tray also carries the spool's RFID uid.
    tray = (raw.get("vt_tray") or [{}])[0] or {}
    result["spool"] = {"material": tray.get("tray_type") or None, "color": tray.get("tray_color") or None}
    spool = {"state": tray.get("tray_type") or "empty"}
    color = str(tray.get("tray_color") or "")[:6]
    if color:
        spool["color"] = f"#{color}"
    if tray.get("tray_sub_brands"):
        spool["name"] = tray["tray_sub_brands"]
    spool["nozzle_temp_min"] = tray.get("nozzle_temp_min")
    spool["nozzle_temp_max"] = tray.get("nozzle_temp_max")
    result["external_spool"] = spool
    trays = onboard.ams_trays(raw.get("ams"))
    if trays:
        result["ams"] = trays
    # Read live on every call (an AMS moves between printers); a unit with every slot empty is still there.
    units = onboard.ams_units(raw.get("ams"), raw.get("tray_now"), raw.get("ams_exist_bits"))
    if units:
        result["ams_units"] = units
    result["options"] = {**(result.get("options") or {}), "bff_spaghetti": spaghetti_watch_on(args["printer"]),
                         "bff_spaghetti_pause": spaghetti_pause_on(args["printer"])}
    if flow_cali_shown(args["printer"], raw):
        result["options"]["bff_flow_cali"] = flow_cali(args["printer"])
    for key, val in raw.items():  # Klipper runout sensors: a switch in Options, filament in or out beside it
        kind, _, name = key.partition(" ")
        if kind in ("filament_switch_sensor", "filament_motion_sensor") and isinstance(val, dict) and "enabled" in val:
            result["options"][f"filament_sensor:{name}"] = bool(val["enabled"])
            result.setdefault("filament_sensors", {})[name] = bool(val.get("filament_detected"))
    if onboard.chamber_sensor(model_code(args["printer"])):
        # Only these models report a real chamber temperature; A1/P1 echo a constant 5 and never publish it.
        result["chamber_temp"] = raw.get("chamber_temp")
        result["door"] = "ON" if raw.get("door_open") else "OFF"
    result["motion_style"] = "jog" if envelope_name_of(args["printer"]) == "KLIPPER" else "absolute"
    try:  # a Klipper printer that has not answered toolhead yet has no limits: greys Motion, no error
        result["motion"] = envelope_of(args["printer"]) or False  # the app's limits; False greys Motion
    except ToolError:
        result["motion"] = False
    watch = WATCHES.get(args["printer"])
    if watch:
        result["spaghetti"] = {"verdict": watch.verdict, "score": watch.score, "error": watch.error,
                               "paused": getattr(watch, "paused", None) is True}
    # ponytail: no link (tests, printer not configured) shows "not homed"
    try:
        axes = axes_of(args["printer"], raw)
        result["axes"] = {"homed": axes["homed"], "pos": axes["pos"]}
        live = raw.get("axes") or {}  # Klipper: per-axis homing and a real position, even when only one axis is homed
        if live:
            result["axes"]["homed_axes"] = live.get("homed") or ""
            if len(live.get("pos") or []) >= 3:
                result["axes"]["pos"] = {axis: round(live["pos"][i], 2) for i, axis in enumerate("xyz")}
    except Exception:
        result["axes"] = {"homed": False, "pos": dict.fromkeys("xyz")}
    return result


LIGHT_LOCK = threading.Lock()
LIGHT_POLLS = 8  # Seconds to wait: the real A1 reports the new light state after about 4 s.
LIGHT_SETTLE = 2  # Seconds with the light on before the photo.


def with_light(pid, action):
    """Run action with the chamber light on, then restore it. Returns (result, lit).

    lit is False only when switching on could not be confirmed. The light is never touched during a print.
    """
    with LIGHT_LOCK:
        raw = read_status(alias_of(pid))
        if raw.get("chamber_light") is True:
            return action(), True
        # chamber_light None = no lights_report in the last push: treat as off (the user, dark photo on Alpha)
        if not CFG.get("control") or raw.get("state") not in ACTIONS["start"]["from"]:
            why = "control off" if not CFG.get("control") else f"state {raw.get('state')}"
            print(f"luce per la foto non accesa: {why}", file=sys.stderr, flush=True)
            return action(), None
        try:
            lit = False
            try:
                set_light(pid, True)
                for _ in range(LIGHT_POLLS):
                    time.sleep(1)
                    lit = read_status(alias_of(pid)).get("chamber_light") is True
                    if lit:
                        time.sleep(LIGHT_SETTLE)  # the user: the camera needs a moment to show the lit plate
                        break
            except ToolError:
                pass
            return action(), lit
        finally:
            try:
                set_light(pid, False)
            except ToolError:
                print("luce non ripristinata", file=sys.stderr, flush=True)


FEEDS = {}
FEEDS_LOCK = threading.Lock()


def model_code(alias):
    """Model code of an app-added printer; "" when the store is not there (tests, nothing app-added)."""
    try:
        return (onboard.load_store(store_path()).get(alias) or {}).get("model", "")
    except KeyError:  # no STATE_DIRECTORY
        return ""


def tested_model(alias):
    """Whether the user tested this model by hand: Expert mode stays locked on anything else."""
    code = model_code(alias)
    # Klipper refuses unhomed and out-of-range moves itself, from printer.cfg: nothing to map by hand.
    if code in onboard.TESTED_MODELS or envelope_name_of(alias) == "KLIPPER":
        return True
    # BFF_EXPERT_MODELS lets a tester unlock Expert mode on a model whose safe area is being mapped.
    expert = os.environ.get("BFF_EXPERT_MODELS") or ""
    return code in {entry.strip() for entry in expert.split(",") if entry.strip()}


def feed_of(alias):
    """One camera reader per printer, shared by the app, the photos and the agent."""
    with FEEDS_LOCK:
        if alias not in FEEDS:
            if isinstance(LINKS.get(alias), MoonrakerLink):  # Klipper: the webcam Moonraker knows about
                link = LINKS[alias]
                FEEDS[alias] = camera.SnapshotFeed(link.snapshot_url, lambda link=link: camera.exif_orientation(*link.cam_flip))
            elif onboard.camera_kind(model_code(alias)) == "rtsp":
                FEEDS[alias] = camera.Go2rtcFeed(alias)  # no JPEG feed on this firmware: ask go2rtc
            else:
                link = link_of(alias)
                pin = lambda der: check_fingerprint(link.fingerprint_dir, f"{alias}-cam", der)  # noqa: E731
                FEEDS[alias] = camera.CameraFeed(link.host, link.access_code, check_cert=pin)
        return FEEDS[alias]


def capture_jpeg(pid):
    try:
        return feed_of(alias_of(pid)).latest()
    except camera.CameraError as err:
        raise ToolError(f"Camera: {err}") from None


def snapshot_jpeg(pid):
    return with_light(pid, lambda: capture_jpeg(pid))[0]


def image_content(jpeg):
    return {"type": "image", "data": base64.b64encode(jpeg).decode(), "mimeType": "image/jpeg"}


def tool_camera_snapshot(args):
    pid = printer_id(args)
    jpeg, lit = with_light(pid, lambda: capture_jpeg(pid))
    return [image_content(jpeg)] + (as_text({"avviso": "luce non confermata: la foto puo' essere scura"}) if lit is False else [])


# What the agent must know to judge the plate from the photo, per model (the camera sees each one differently).
PLATE_COMMON = ("I piatti hanno colori diversi e la superficie puo' avere segni lasciati dalle stampe precedenti: "
                "non sono oggetti, non giudicare dal colore. Se una parte del piatto non si vede o hai un dubbio, la risposta "
                "e' unknown: mostra foto e photo_url all'utente e chiedi a lui.")
PLATE_RULES = {
    "A1": "Decidi dalla foto: clear, occupied o unknown. La A1 e' una bed slinger: il piatto puo' stare in qualsiasi "
          "posizione Y e la scocca scoperta sotto non e' un oggetto. " + PLATE_COMMON,
    # Alpha, 2026-11-12: camera high on the front-left, the whole plate in frame at every Z.
    "P1": "Decidi dalla foto: clear, occupied o unknown. La P1S/P1P e' una CoreXY chiusa: il piatto si muove solo in Z "
          "ed e' sempre tutto inquadrato; la testa e i suoi cavi in alto, il cestello di spurgo dietro e le pareti non sono "
          "oggetti. Se la testa copre una parte del piatto, quella parte non si vede: unknown. " + PLATE_COMMON,
}


def plate_rules(pid):
    """pid is CFG's printer id (what the camera calls take), not the alias: find the alias for its model."""
    alias = next((a for a, p in CFG["printers"].items() if p == pid), pid)
    return PLATE_RULES["P1"] if envelope_of(alias) is ENVELOPES["P1"] else PLATE_RULES["A1"]


# Set per request: the phone app sends X-BFF-App: 1. Its user watches the live camera, so axes prepares skip
# the plate photo there; agent clients (Claude CLI) do not send the header and keep it.
APP_CALL = threading.local()
# Host header of the request being served, so camera_urls can build a URL without extra config.
REQUEST = threading.local()


def request_host():
    """The Host header without its port, or None outside a request."""
    host = getattr(REQUEST, "host", "")
    return host.split(":")[0] or None


def shot_parts(pid):
    if getattr(APP_CALL, "on", False):
        return [], {}
    jpeg, photo = plate_shot(pid)
    return [image_content(jpeg)], photo


def plate_shot(pid):
    """Plate photo with its url and rules, as plate_photo returns it."""
    jpeg, lit = with_light(pid, lambda: capture_jpeg(pid))
    reply = {"photo_url": store_photo(jpeg), "regole": plate_rules(pid)}
    if lit is False:
        reply["avviso"] = "luce non confermata: la foto puo' essere scura, in dubbio la risposta e' unknown"
    return jpeg, reply


def tool_plate_photo(args):
    jpeg, reply = plate_shot(printer_id(args))
    return [image_content(jpeg)] + as_text(reply)


def tool_hms_diagnose(args):
    raw = status_of(args)
    errors = [
        {
            "code": item.get("code"),
            "module": item.get("module"),
            "severity": SEVERITY.get(item.get("severity"), "sconosciuta"),
            "actions": item.get("actions") or [],
            "description": item.get("description"),
            "handling": hms_handling(item),
        }
        for item in raw.get("hms_errors") or []
    ]
    return as_text({"printer": args["printer"], "connected": raw.get("connected"), "errors": errors})


class LockedDB:
    """A sqlite3.Connection safe to share across HTTP threads: one global lock."""

    # ponytail: one global lock, per-thread connections if contention ever matters.
    class Rows(list):
        def __init__(self, rows, lastrowid, rowcount):
            super().__init__(rows)
            self.lastrowid = lastrowid
            self.rowcount = rowcount

        def fetchone(self):
            return self[0] if self else None

        def fetchall(self):
            return list(self)

    def __init__(self, conn):
        self.conn = conn
        self._lock = threading.RLock()

    def execute(self, *a):
        with self._lock:
            cur = self.conn.execute(*a)
            return self.Rows(cur.fetchall(), cur.lastrowid, cur.rowcount)

    def commit(self):
        with self._lock:
            self.conn.commit()

    def __enter__(self):
        self._lock.acquire()
        return self.conn.__enter__()

    def __exit__(self, *e):
        try:
            return self.conn.__exit__(*e)
        finally:
            self._lock.release()

    def __getattr__(self, name):
        return getattr(self.conn, name)


def open_db(path):
    global DB
    DB = LockedDB(sqlite3.connect(path, check_same_thread=False))
    DB.executescript(SCHEMA)
    # Databases made by older versions lack these columns; on newer ones ALTER fails and is skipped.
    for sql in ("ALTER TABLE uploads ADD COLUMN plate TEXT",
                "ALTER TABLE uploads ADD COLUMN feed TEXT",
                "ALTER TABLE print_requests ADD COLUMN status TEXT DEFAULT 'pending'",
                "ALTER TABLE print_requests ADD COLUMN file_id INTEGER",
                "ALTER TABLE print_requests ADD COLUMN note TEXT",
                "ALTER TABLE print_requests ADD COLUMN plates TEXT",
                "ALTER TABLE print_requests ADD COLUMN next_plate INTEGER DEFAULT 0",
                "ALTER TABLE print_requests ADD COLUMN source TEXT DEFAULT 'makerworld'",
                "ALTER TABLE print_requests ADD COLUMN file_name TEXT",
                "ALTER TABLE print_requests ADD COLUMN file_size INTEGER",
                "ALTER TABLE print_requests ADD COLUMN reply TEXT",
                "ALTER TABLE print_requests ADD COLUMN copies INTEGER DEFAULT 1",
                "ALTER TABLE print_requests ADD COLUMN engine TEXT DEFAULT 'orca'",
                "ALTER TABLE print_requests ADD COLUMN choices TEXT",
                "ALTER TABLE print_requests ADD COLUMN slot TEXT",
                "ALTER TABLE print_requests ADD COLUMN facts TEXT",
                "ALTER TABLE print_requests ADD COLUMN stage TEXT",
                "ALTER TABLE print_requests ADD COLUMN slots TEXT"):
        try:
            DB.execute(sql)
        except sqlite3.OperationalError:
            pass
    DB.execute("UPDATE print_requests SET status='done' WHERE done=1 AND status='pending'")
    DB.commit()


def migrate_blobs():
    """Move old `files` BLOBs to the folder, drop uploads rows left without a file."""
    if DB is None:
        return
    with DB:
        for file_id, data in DB.execute("SELECT file_id, data FROM files").fetchall():
            library.save(file_id, data)
        DB.execute("DELETE FROM files")
        for (file_id,) in DB.execute("SELECT file_id FROM uploads").fetchall():
            if not library.exists(file_id):
                DB.execute("DELETE FROM uploads WHERE file_id=?", (file_id,))


def load_profile(alias):
    try:
        return json.loads((Path(CFG["profiles"]) / f"{alias}.json").read_text())
    except (OSError, ValueError, KeyError):
        pass
    entry = onboard.load_store(store_path()).get(alias) if os.environ.get("STATE_DIRECTORY") else None
    profile = entry and onboard.model_profile(entry.get("model"), entry.get("enclosure"), profile_of(alias))
    if profile and entry.get("limits"):  # Klipper: read from printer.cfg when added
        profile = onboard.apply_cfg_limits(profile, entry["limits"])
    if not profile:
        raise ToolError(f"Profilo di {alias} mancante o non valido: comando rifiutato")
    return profile


def require_support(alias, action):
    if load_profile(alias).get("supports", {}).get(action) is not True:
        raise ToolError(f"Il profilo di {alias} non abilita {action}")


def limit_value(limits, name):
    entry = limits.get(name)
    value = entry.get("value") if isinstance(entry, dict) else None
    source = entry.get("source") if isinstance(entry, dict) else None
    if type(value) not in (int, float) or not (isinstance(source, str) and source.strip()):
        raise ToolError(f"Limite {name} senza valore o fonte nel profilo: comando rifiutato")
    return value


def check_temperature(alias, heater, target):
    """Validate against the profile and return the one-step ceiling."""
    if heater not in ("nozzle", "bed"):
        raise ToolError("heater deve essere nozzle o bed")
    if type(target) is not int:
        raise ToolError("target deve essere un intero in gradi Celsius")
    limits = load_profile(alias).get("limits", {})
    maximum = limit_value(limits, f"{heater}_max_c")
    ceiling = limit_value(limits, f"{heater}_confirm_above_c")
    if not 0 <= target <= maximum:
        raise ToolError(f"Temperatura fuori limite: massimo {maximum} gradi per {heater}")
    return ceiling


def assert_unlocked(alias, action=None):
    """Pause and stop are safety commands: an unknown last outcome must never refuse them."""
    if action in ("pause", "stop"):
        return
    if DB.execute("SELECT 1 FROM locks WHERE printer=?", (alias,)).fetchone():
        raise ToolError(f"Esito dell'ultimo comando su {alias} sconosciuto: usa recheck prima di altri comandi")


def blocked_reason(action, raw):
    if raw.get("connected") is not True:
        return "stampante non connessa"
    allowed = ACTIONS[action]["from"]
    if allowed and raw.get("state") not in allowed:
        return f"non applicabile nello stato {raw.get('state')}"
    if action == "start" and raw.get("hms_errors"):
        details = "; ".join(str(e.get("description") or e.get("code")) for e in raw["hms_errors"])
        return f"errori HMS attivi: {details}"
    return None


def observed(action, params, raw):
    if raw.get("connected") is not True:
        return False
    if action == "temperature":
        heater, target = params.get("heater"), params.get("target")
        if heater is None or target is None:
            return False
        value = (raw.get("temperatures") or {}).get(f"{heater}_target")
        return type(value) in (int, float) and abs(value - target) <= 1
    return raw.get("state") in ACTIONS[action]["to"]


def save(key, alias, action, params, outcome, detail):
    DB.execute(
        "INSERT OR REPLACE INTO commands VALUES (?,?,?,?,?,?)",
        (key, alias, action, json.dumps(params, sort_keys=True), outcome, detail),
    )


def finish(key, alias, action, params, outcome, detail):
    with DB:
        save(key, alias, action, params, outcome, detail)
        DB.execute("DELETE FROM locks WHERE printer=?", (alias,))
    return {"printer": alias, "action": action, "outcome": outcome, "detail": detail}


def wait_for_outcome(alias, action, params):
    """Return the last state seen and whether the expected state was observed."""
    profile = load_profile(alias)
    limit = profile.get(f"{action}_timeout_s", profile.get("command_timeout_s", DEFAULT_TIMEOUT_S))
    deadline = time.monotonic() + float(limit)
    state = None
    while time.monotonic() < deadline:
        try:
            raw = read_status(alias)
        except ToolError:
            time.sleep(POLL_S)
            continue
        state = raw.get("state")
        if observed(action, params, raw):
            return state, True
        time.sleep(POLL_S)
    return state, False


def consume_token(token, alias, action):
    if not isinstance(token, str):
        raise ToolError("Token di conferma mancante")
    digest = hashlib.sha256(token.encode()).hexdigest()
    with DB:
        row = DB.execute(
            "SELECT params FROM tokens WHERE hash=? AND printer=? AND action=? AND expires>?",
            (digest, alias, action, time.time()),
        ).fetchone()
        if row:
            DB.execute("DELETE FROM tokens WHERE hash=?", (digest,))
    if row is None:
        raise ToolError("Token di conferma non valido, scaduto o per un altro comando")
    return json.loads(row[0])


def replay_of(key, alias, action, params):
    row = DB.execute("SELECT printer, action, params, outcome, detail FROM commands WHERE key=?", (key,)).fetchone()
    if row is None:
        return None
    same_params = params is None or row[2] == json.dumps(params, sort_keys=True)
    if (row[0], row[1]) != (alias, action) or not same_params:
        raise ToolError("idempotency_key gia' usata per un comando diverso")
    return {"printer": alias, "action": action, "outcome": row[3], "detail": row[4], "replayed": True}


def start_klipper(link, name, data):
    """Upload and start. A sliced 3MF is unpacked to its plate's G-code; a plain .gcode goes as it is."""
    if data[:2] == b"PK":
        plate = sliced_plate(data)
        try:
            images = preview.plate_images(data, plate)  # Fluidd and KlipperScreen read the thumbnail from the G-code header
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                data = preview.klipper_gcode(z.read(f"Metadata/plate_{plate}.gcode"), images)
        except KeyError:
            raise ftps.UploadError("il 3MF non contiene G-code: affettalo per questa stampante") from None
        name = name.removesuffix(".3mf").removesuffix(".gcode") + ".gcode"
    try:
        link.upload(name, data)
    except (OSError, ValueError) as err:
        raise ftps.UploadError(f"upload a Moonraker fallito: {err}") from None  # nothing sent yet
    if not link.start_print(name):  # not sent means not retried: the lock stays and recheck decides
        raise ToolError("Comando non partito")


def send_command(key, alias, action, params, raw):
    if action != "start":
        # Not sent means not retried: the lock stays and recheck decides.
        if not link_of(alias).command(action, **(params or {})):
            raise ToolError("Comando non partito")
        return
    row = DB.execute("SELECT u.name FROM uploads u WHERE file_id=?", (params["file_id"],)).fetchone()
    if row is None:
        raise ftps.UploadError("file non più presente in BFF: ricaricalo")
    try:
        data = library.read(params["file_id"])
    except FileNotFoundError:
        raise ftps.UploadError("file non più presente in BFF: ricaricalo") from None
    link = link_of(alias)
    if isinstance(link, MoonrakerLink):  # Klipper: plain G-code over HTTP, no AMS, no FTPS
        return start_klipper(link, row[0], data)
    feed = None  # None: the external spool, the A1's path unchanged
    try:
        picked = chosen_feed(params["file_id"], raw)
    except ValueError as err:
        raise ftps.UploadError(str(err)) from None  # nothing sent yet
    if picked is not None:
        feed = picked if any(i != 254 for i in picked) else None  # all external: the plain path
    elif onboard.ams_trays(raw.get("ams")):
        filaments = upload_filaments(params["file_id"])
        if len(filaments) >= 2:
            feed = filaments_feed(filaments, raw)  # raises UploadError: nothing sent yet
        else:
            try:
                feed = (feed_for(load_upload(params["file_id"]), raw, upload_color(params["file_id"]))[0],)
            except ValueError as err:
                raise ftps.UploadError(str(err)) from None  # nothing sent yet
    pin = lambda der: check_fingerprint(link.fingerprint_dir, f"{alias}-ftp", der)  # noqa: E731
    ftps.upload(link.host, link.access_code, row[0], data, check_cert=pin)
    # Not sent means not retried: the lock stays and recheck decides.
    plate = sliced_plate(data)
    mode = flow_cali(alias) if supports_flow_cali(getattr(link, "snapshot", dict)() or {}) else "auto"
    # Only links that know the setting get it (Moonraker's start_print has no such argument).
    kw = {"flow_cali": mode} if "flow_cali" in inspect.signature(link.start_print).parameters else {}
    started = (link.start_print(row[0], feed or (254,), plate, **kw) if plate != 1
               else link.start_print(row[0], feed, **kw) if feed
               else link.start_print(row[0], **kw))
    if not started:
        raise ToolError("Comando non partito")


def execute(alias, action, params, key, token=None):
    """Send at most one command for this key. `params` is None when the token carries it."""
    if not (isinstance(key, str) and 8 <= len(key) <= 128):
        raise ToolError("idempotency_key mancante: stringa di 8-128 caratteri")
    with COMMAND_LOCK:
        previous = replay_of(key, alias, action, params)
        if previous:
            return previous
        assert_unlocked(alias, action)
        if token is not None:
            params = consume_token(token, alias, action)
        elif params is None and action in ("start", "stop", "temperature"):
            raise ToolError("token mancante o scaduto: rifai prepare_...")
        raw = read_status(alias)
        if action == "resume" and raw.get("resume_blocked"):
            raise ToolError("Pause expired: Klipper turned off the motors and heaters, this print can't be resumed. Cancel it.")
        reason = blocked_reason(action, raw)
        if reason:
            with DB:
                save(key, alias, action, params, "rejected", reason)
            return {"printer": alias, "action": action, "outcome": "rejected", "detail": reason}
        # Locked before sending: a crash after the POST must not leave the printer free for new commands.
        with DB:
            save(key, alias, action, params, "unknown", UNKNOWN)
            DB.execute("INSERT OR REPLACE INTO locks VALUES (?,?)", (alias, key))
        try:
            send_command(key, alias, action, params, raw)
        except ftps.UploadError as err:
            # Nothing was started: the upload failed before project_file.
            return finish(key, alias, action, params, "rejected", str(err))
        except ToolError:
            return {"printer": alias, "action": action, "outcome": "unknown", "detail": UNKNOWN}
        state, seen = wait_for_outcome(alias, action, params)
        if seen:
            if action == "start" and isinstance(params, dict):
                fid = params.get("file_id")
                if fid is not None:
                    pop_queued(alias, fid)
            return finish(key, alias, action, params, "confirmed", f"osservato stato {state}")
        return {"printer": alias, "action": action, "outcome": "unknown", "detail": UNKNOWN}


def command_reply(result):
    if result["outcome"] != "confirmed":
        raise ToolError(f"{result['action']} su {result['printer']}: {result['outcome']}, {result['detail']}")
    return as_text(result)


def issue_token(alias, action, params, summary, **extra):
    token = secrets.token_urlsafe(32)
    now = time.time()
    with DB:
        DB.execute("DELETE FROM tokens WHERE expires<=?", (now,))
        DB.execute(
            "INSERT INTO tokens VALUES (?,?,?,?,?)",
            (hashlib.sha256(token.encode()).hexdigest(), alias, action, json.dumps(params, sort_keys=True), now + TOKEN_TTL_S),
        )
    return as_text({"printer": alias, "summary": summary, "confirm_token": token, "expires_in_s": TOKEN_TTL_S, **extra})


def simple_command(action):
    def run(args):
        printer_id(args)
        require_support(args["printer"], action)
        return command_reply(execute(args["printer"], action, {}, args.get("idempotency_key")))
    return run


def tool_prepare_stop(args):
    printer_id(args)
    alias = args["printer"]
    require_support(alias, "stop")
    with COMMAND_LOCK:
        assert_unlocked(alias, "stop")
        raw = read_status(alias)
        reason = blocked_reason("stop", raw)
        if reason:
            raise ToolError(f"stop su {alias}: {reason}")
        summary = f"Stop della stampa '{raw.get('subtask_name')}' su {alias}, al {raw.get('progress')}%. Irreversibile."
        return issue_token(alias, "stop", {}, summary)


def tool_confirm_stop(args):
    printer_id(args)
    AXES.pop(args["printer"], None)
    require_support(args["printer"], "stop")
    return command_reply(execute(args["printer"], "stop", None, args.get("idempotency_key"), token=args.get("token")))


def tool_set_temperature(args):
    printer_id(args)
    alias, heater, target = args["printer"], args.get("heater"), args.get("target")
    if target > check_temperature(alias, heater, target):
        raise ToolError("Sopra la soglia del profilo: usa prepare_temperature e chiedi conferma all'utente")
    params = {"heater": heater, "target": target}
    return command_reply(execute(alias, "temperature", params, args.get("idempotency_key")))


def tool_prepare_temperature(args):
    printer_id(args)
    alias, heater, target = args["printer"], args.get("heater"), args.get("target")
    check_temperature(alias, heater, target)
    with COMMAND_LOCK:
        assert_unlocked(alias)
        reason = blocked_reason("temperature", read_status(alias))
        if reason:
            raise ToolError(f"temperatura su {alias}: {reason}")
        return issue_token(alias, "temperature", {"heater": heater, "target": target}, f"{heater} di {alias} a {target} gradi.")


def tool_confirm_temperature(args):
    printer_id(args)
    return command_reply(execute(args["printer"], "temperature", None, args.get("idempotency_key"), token=args.get("token")))


def metadata(archive, name):
    if archive.getinfo(name).file_size > MAX_METADATA:
        raise ToolError("3MF con metadati troppo grandi")
    return archive.read(name)


def weight_from_length(info, settings, filament):
    """Grams from filament length, for slicers that leave the weight empty. None when it cannot be worked out."""
    match = re.search(r'<filament\b[^>]*\sid="(\d+)"[^>]*\sused_m="([\d.]+)"', info)
    try:
        index = int(match[1]) - 1
        density = float(settings["filament_density"][index]) or TYPICAL_DENSITY[filament]
        radius = float(settings["filament_diameter"][index]) / 2
        return round(float(match[2]) * math.pi * radius ** 2 * density, 2)
    except (TypeError, KeyError, IndexError, ValueError):
        return None


def inspect_3mf(data):
    """Facts about what the file was sliced for. Orca leaves printer_model_id empty, project settings are reliable."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            plates = [n for n in archive.namelist() if re.fullmatch(r"Metadata/plate_\d+\.gcode", n)]
            settings = json.loads(metadata(archive, "Metadata/project_settings.config"))
            info = metadata(archive, "Metadata/slice_info.config").decode()
        model, nozzle = settings["printer_model"], settings["nozzle_diameter"][0]
    except (zipfile.BadZipFile, zlib.error, KeyError, IndexError, TypeError, ValueError, OSError, NotImplementedError, RuntimeError):
        raise ToolError("File non valido: serve un 3MF affettato") from None
    if len(plates) != 1:  # one sliced plate, any number: a picked plate of a project keeps its own (2026-11-15)
        raise ToolError("Serve un 3MF affettato con una sola piastra")
    filaments = sorted(set(re.findall(r'<filament\b[^>]*\stype="([^"]+)"', info)))
    if not filaments:
        raise ToolError("File non valido: serve un 3MF affettato")
    prediction = re.search(r'key="prediction" value="(\d+)"', info)
    weight = re.search(r'key="weight" value="([\d.]+)"', info)
    if not (isinstance(model, str) and isinstance(nozzle, str) and prediction):
        raise ToolError("File non valido: serve un 3MF affettato")
    # Only for the summary and the spool warning, and Orca 2.4.2 leaves it empty.
    weight_g = float(weight[1]) if weight else weight_from_length(info, settings, filaments[0])
    plate = settings.get("curr_bed_type")
    return {"model": model, "nozzle": nozzle, "filament": "/".join(filaments), "time_s": int(prediction[1]), "weight_g": weight_g,
            "plate": plate if isinstance(plate, str) and plate else None}


def sliced_plate(data):
    """Number of the one sliced plate in a .gcode.3mf (1 when unreadable: upload_file already checked it)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for n in archive.namelist():
                if m := re.fullmatch(r"Metadata/plate_(\d+)\.gcode", n):
                    return int(m[1])
    except (zipfile.BadZipFile, OSError):
        pass
    return 1


def plate_png(data, path, any_plate=False):
    """Base64 PNG of the plate at `path` in a 3MF (or, with any_plate, its first plate image); None when there is none."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            name = path if path in names or not any_plate else next((n for n in names if re.fullmatch(r"Metadata/plate_\d+\.png", n)), None)
            png = archive.read(name) if name in names else None
            if png is None and any_plate:  # sliced here, so no image from Orca: draw it
                num = next((m.group(1) for n in names if (m := re.fullmatch(r"Metadata/plate_(\d+)\.gcode", n))), None)
                images = preview.plate_images(data, num) if num else {}
                png = images[max(images)] if images else None
    except (KeyError, zipfile.BadZipFile, zlib.error, OSError, NotImplementedError, RuntimeError):
        return None
    return base64.b64encode(png).decode() if png is not None and len(png) < 2_000_000 else None


def tool_plate_preview(args):
    """Thumbnail of one plate of a draft project, for the plate picker."""
    rid, n = args.get("id"), args.get("plate")
    if type(rid) is not int or type(n) is not int:
        raise ToolError("No such plate")
    row = DB.execute("SELECT m.data, r.facts FROM models m JOIN print_requests r ON r.id=m.request_id WHERE m.request_id=?",
                     (rid,)).fetchone()
    if row is None:
        raise ToolError("No such plate")
    info = {p.get("n"): p for p in (json.loads(row[1]) if row[1] else {}).get("plate_info") or []}
    path = (info.get(n) or {}).get("thumb") or f"Metadata/plate_{n}.png"
    return as_text({"png": plate_png(row[0], path)})


def tool_file_preview(args):
    """The sliced plate image inside a stored .gcode.3mf, for the app before printing."""
    file_id = args.get("file_id")
    if type(file_id) is not int:
        raise ToolError("No such file")
    try:
        data = library.read(file_id)
    except OSError:
        raise ToolError("No such file") from None
    return as_text({"png": plate_png(data, f"Metadata/plate_{sliced_plate(data)}.png", any_plate=True)})


def _job_stem(name):
    """A job name without the extensions Moonraker and the printer add, case-insensitive."""
    return re.sub(r"\.gcode\.3mf$|\.3mf$|\.gcode$", "", name or "", flags=re.IGNORECASE).lower()


def bambu_preview(alias, job):
    """The plate image Pygmy stored for this job, if Pygmy started the print that is running; else None."""
    row = DB.execute("SELECT file_id, plates, file_name FROM print_requests "
                     "WHERE printer=? AND status='started' ORDER BY id DESC LIMIT 1", (alias,)).fetchone()
    if row is None or not job:
        return None
    file_id, plates, file_name = row
    ids = json.loads(plates) if plates else ([file_id] if file_id is not None else [])
    for fid in ids:
        up = DB.execute("SELECT name FROM uploads WHERE file_id=?", (fid,)).fetchone()
        if _job_stem(up[0] if up else file_name) != _job_stem(job):
            continue
        try:
            return plate_png(library.read(fid), "", any_plate=True)
        except OSError:
            return None
    return None


def tool_print_preview(args):
    """Image of the job printing now, for the Status tab's second page; None when nothing is printing."""
    printer_id(args)
    alias = args["printer"]
    link = link_of(alias)
    snapshot = link.snapshot()
    job = snapshot.get("subtask_name")
    if snapshot.get("state") not in ("RUNNING", "PAUSE") or not job:
        return as_text({"png": None, "name": None})
    cached = PREVIEWS.get((alias, job))
    if cached:
        return as_text(cached)
    if isinstance(link, MoonrakerLink):
        with link.lock:
            filename = (link.raw.get("print_stats") or {}).get("filename")
        thumb = link.thumbnail(filename) if filename else None
        png = base64.b64encode(thumb).decode() if thumb else None
    else:
        png = bambu_preview(alias, job)
    result = {"png": png, "name": job}
    if png:
        if len(PREVIEWS) > 200:  # ponytail: one entry per job, cleared wholesale; a real cache if it ever matters
            PREVIEWS.clear()
        PREVIEWS[(alias, job)] = result
    return as_text(result)


def upload_file(name, data):
    if not re.fullmatch(r"[\w.-]{1,120}\.gcode\.3mf", name):
        raise ToolError("Nome file non valido: lettere, numeri, . _ - e finale .gcode.3mf")
    facts = inspect_3mf(data)
    with DB:
        DB.execute("INSERT OR IGNORE INTO settings (printer, key, value) SELECT '', 'file_seq', COALESCE(MAX(file_id), 0) FROM uploads")
        file_id = int(DB.execute("SELECT value FROM settings WHERE printer='' AND key='file_seq'").fetchone()[0]) + 1
        DB.execute("UPDATE settings SET value=? WHERE printer='' AND key='file_seq'", (file_id,))
    try:
        library.save(file_id, data)
    except OSError:
        raise ToolError("Could not save the file on the server") from None
    with DB:
        DB.execute(
            "INSERT INTO uploads (file_id, name, model, nozzle, filament, time_s, weight_g, plate) VALUES (?,?,?,?,?,?,?,?)",
            (file_id, name, facts["model"], facts["nozzle"], facts["filament"], facts["time_s"], facts["weight_g"], facts["plate"]),
        )
    ids_newest_first = [r[0] for r in DB.execute("SELECT file_id FROM uploads ORDER BY file_id DESC").fetchall()]
    pruned = library.prune(ids_newest_first, keep=queued_file_ids())
    if pruned:
        marks = ",".join("?" * len(pruned))
        with DB:
            DB.execute(f"DELETE FROM uploads WHERE file_id IN ({marks})", pruned)
    return {"file_id": file_id, "name": name, **facts}


def load_upload(file_id):
    row = None
    if type(file_id) is int:
        row = DB.execute("SELECT name, model, nozzle, filament, time_s, weight_g, plate FROM uploads WHERE file_id=?", (file_id,)).fetchone()
    if row is None:
        raise ToolError("File non caricato tramite questo server: usa prima l'upload")
    return dict(zip(("name", "model", "nozzle", "filament", "time_s", "weight_g", "plate"), row))


def plate_mismatch(alias, upload):
    """The file's plate against the one declared in the app. Files sliced before the check carry no plate."""
    wanted = upload.get("plate")
    if wanted is None or isinstance(link_of(alias), MoonrakerLink):  # Klipper beds are not Bambu plates
        return None
    mounted = plate_type(alias)
    if mounted is None:
        return f"il file richiede il piatto {wanted}: dichiara il piatto montato nella scheda Filament"
    if mounted != wanted:
        return f"piatto montato {mounted}, il file richiede {wanted}"
    return None


def feed_for(upload, raw, color):
    """(feed, note): AMS tray index or 254 (external), for whatever is attached now. ValueError if nothing fits."""
    tray = (raw.get("vt_tray") or [{}])[0] or {}
    return onboard.pick_feed(upload["filament"], color, onboard.ams_trays(raw.get("ams")),
                             {"material": tray.get("tray_type"), "color": tray.get("tray_color")})


def upload_filaments(file_id):
    """Every filament in the stored 3mf: [{"id", "type", "color"}], id 1-based. Empty when unreadable."""
    if DB is None:
        return []
    try:
        data = library.read(file_id)
    except FileNotFoundError:
        return []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            info = metadata(archive, "Metadata/slice_info.config").decode()
    except (zipfile.BadZipFile, KeyError, ToolError, OSError):
        return []
    out = []
    for tag in re.findall(r"<filament\b[^>]*>", info):
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', tag))
        try:
            fid = int(attrs["id"])
        except (KeyError, ValueError):
            fid = len(out) + 1
        out.append({"id": fid, "type": attrs.get("type", ""), "color": attrs.get("color", "")})
    return out


def upload_color(file_id):
    """The plate's filament colour from the stored 3mf, or None."""
    for filament in upload_filaments(file_id):
        match = re.match(r"#[0-9A-Fa-f]{6}", filament["color"])
        if match:
            return match[0]
    return None


def slot_index(slot):
    """'A1'..'D4' -> AMS tray index 0-15, anything else (the external spool) -> 254."""
    if isinstance(slot, str) and re.fullmatch(r"[A-D][1-4]", slot):
        return (ord(slot[0]) - 65) * 4 + int(slot[1]) - 1
    return 254


def chosen_feed(file_id, raw):
    """The trays picked in Print now for this file, checked against what is loaded now; None when nothing was picked.

    ValueError (English, for the app) when a picked slot is now empty or holds another material.
    """
    row = DB.execute("SELECT feed FROM uploads WHERE file_id=?", (file_id,)).fetchone() if DB is not None else None
    if not row or not row[0]:
        return None
    feed = tuple(json.loads(row[0]))
    trays = {t["index"]: t for t in onboard.ams_trays(raw.get("ams"))}
    for f in upload_filaments(file_id):
        if f["id"] > len(feed):
            raise ValueError("This file does not match the spools picked for it: slice it again")
        index = feed[f["id"] - 1]
        if index == 254:
            continue  # external spool or no AMS: one spool, checked by type below
        tray = trays.get(index)
        name = f"AMS slot {'ABCD'[index // 4]}{index % 4 + 1}"
        if tray is None:
            raise ValueError(f"{name} is empty now: load it or slice again")
        if (tray.get("material") or "").upper() != (f["type"] or "").upper():
            raise ValueError(f"{name} now holds {tray.get('material')}, the file needs {f['type']}: slice again")
    return feed


def filaments_feed(filaments, raw):
    """AMS tray per filament id (None: no filament with that id). UploadError if a filament has no slot."""
    trays = onboard.ams_trays(raw.get("ams"))
    tray = (raw.get("vt_tray") or [{}])[0] or {}
    external = {"material": tray.get("tray_type"), "color": tray.get("tray_color")}
    feed = [None] * max(f["id"] for f in filaments)
    for f in filaments:
        try:
            index, _note = onboard.pick_feed(f["type"], f["color"], trays, external)
        except ValueError:
            index = 254
        if index == 254:  # the external spool holds one colour, never the whole plate
            raise ftps.UploadError(f"Nessuno slot AMS per il filamento {f['id']} ({f['type']} {f['color']})")
        feed[f["id"] - 1] = index
    return tuple(feed)


def check_compatible(profile, upload, raw, color=None, file_id=None):
    expected = profile.get("slicer_model")
    # OrcaSlicer's MyKlipper presets write "Generic Klipper Printer" in the file for our "Other Klipper printer"
    same = upload["model"] == expected or (expected, upload["model"]) == ("Other Klipper printer", "Generic Klipper Printer")
    if not expected or not same:
        return f"file affettato per il modello {upload['model']}, serve {expected}"
    if profile.get("kind") == "moonraker":
        return None  # Klipper: no AMS feed or MQTT nozzle to compare; the ACE tool mapping is the user's macro
    ams = onboard.ams_trays(raw.get("ams"))
    if not ams and raw.get("tray_now") == NO_FILAMENT_TRAY:
        return "nessun filamento caricato"
    mounted = (raw.get("nozzles") or [{}])[0].get("nozzle_diameter")
    if mounted != upload["nozzle"]:
        return f"ugello montato {mounted}, il file richiede {upload['nozzle']}"
    if file_id is not None:
        try:
            picked = chosen_feed(file_id, raw)
        except ValueError as err:
            return str(err)
        if picked is not None and any(i != 254 for i in picked):
            return None  # the spools picked in Print now, still loaded
        if picked is not None:
            ams = None  # one external spool: checked below like a printer without AMS
    if ams:
        filaments = upload_filaments(file_id) if file_id is not None else []
        try:
            if len(filaments) >= 2:
                filaments_feed(filaments, raw)
            else:
                feed_for(upload, raw, color)
        except (ValueError, ftps.UploadError) as err:
            return str(err)
        return None
    loaded = (raw.get("vt_tray") or [{}])[0].get("tray_type")
    if loaded != upload["filament"]:
        return f"filamento caricato {loaded}, il file richiede {upload['filament']}"
    return None


def filament_note(alias, upload):
    """Spool check for the summary. It never blocks: the printer reports no grams, so the human checks the spool."""
    return "Residuo bobina non noto: controlla la bobina."


def stored_spool(alias):
    """The external spool set in the app for a printer that cannot store it (Klipper)."""
    if DB is None:
        return {}
    row = DB.execute("SELECT value FROM settings WHERE printer=? AND key='spool'", (alias,)).fetchone()
    try:
        stored = json.loads(row[0]) if row else {}
    except (TypeError, ValueError):
        return {}
    return stored if isinstance(stored, dict) else {}


def external_filament(alias, raw):
    tray = (raw.get("vt_tray") or [{}])[0]
    if not tray.get("tray_type") and isinstance(LINKS.get(alias), MoonrakerLink):
        # Klipper has no tray: the spool is the one set in the app.
        stored = stored_spool(alias)
        tray = {"tray_type": stored.get("material"), "tray_color": stored.get("color"),
                "tray_info_idx": stored.get("profile_id")}
    return {
        "slot": "external",
        "material": tray.get("tray_type") or None,
        "color": tray.get("tray_color") or None,
        "profile_id": tray.get("tray_info_idx") or None,
        "grams_remaining": None,
        "grams_source": "unknown",
    }


def printer_filaments(alias, raw):
    """The spools this printer can run, as exposed by tool_printer_info."""
    trays = onboard.ams_trays(raw.get("ams"))
    if trays:
        ext = external_filament(alias, raw)
        return [{**{k: t[k] for k in ("slot", "material", "color", "profile_id")}, "brand": None, "subtype": t["name"],
                 "color_name": None, "remain_pct": t["remain"], "grams_remaining": None, "grams_source": "unknown"}
                for t in trays] + ([ext] if ext["material"] else [])
    return [] if raw.get("tray_now") == NO_FILAMENT_TRAY else [external_filament(alias, raw)]


def slot_filament(alias, raw, slot):
    """The printer filament whose slot is `slot`, or ToolError. A slot without a filament id cannot be picked."""
    if not isinstance(slot, str) or not slot:
        raise ToolError("Set this spool in Filament first")
    for filament in printer_filaments(alias, raw):
        if filament["slot"] == slot:
            if not filament.get("profile_id"):
                raise ToolError("Set this spool in Filament first")
            return filament
    raise ToolError("Set this spool in Filament first")


def tool_list_printers(_args):
    """The printers BFF manages, for the app's picker: aliases, display names, models. No access codes."""
    names, models = app_names_models()
    try:
        store = onboard.load_store(store_path())
    except KeyError:  # no STATE_DIRECTORY
        store = {}
    custom = sorted(a for a, e in store.items() if model_from_cfg(e.get("model")))
    klipper = sorted(a for a, e in store.items() if onboard.is_klipper(e.get("model")))
    return as_text({"printers": sorted(CFG["printers"]), "names": names, "models": models, "custom": custom,
                    "klipper": klipper, "public_url": CFG.get("public_url", "")})


def tool_rename_printer(args):
    """New display name; for an Other Klipper printer also what it is (label). The alias stays."""
    alias, name = args["printer"], (args.get("name") or "").strip()
    if not name or len(name) > 40:
        raise ToolError("The name is 1 to 40 characters")
    with ADD_LOCK:
        store = app_added(alias)
        entry = store[alias]
        entry["name"] = name
        if model_from_cfg(entry.get("model")) and "label" in args:
            label = (args.get("label") or "").strip()[:60]
            entry.pop("label", None) if not label else entry.update(label=label)
        onboard.save_store(store_path(), store)
    if HA is not None:
        HA.add_alias(alias, name)
    return as_text({"ok": True})


def orca_file(alias):
    return Path(os.environ["STATE_DIRECTORY"]) / "orca" / f"{alias}.json"


def cloud_bundle_path():
    """The Orca Cloud account's filaments and processes: one set for every printer."""
    return Path(os.environ["STATE_DIRECTORY"]) / "orca" / "_cloud.json"


def load_cloud_bundle():
    """{"filaments", "processes"} as user_bundle leaves them; empty when the file is missing or unreadable."""
    empty = {"filaments": {}, "processes": {}}
    try:
        data = json.loads(cloud_bundle_path().read_text())
    except (KeyError, OSError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty
    return {key: (data.get(key) if isinstance(data.get(key), dict) else {}) for key in empty}


def user_chain(name, pool, seen=()):
    """A user preset with the user presets it inherits from merged in; `inherits` ends on a system preset."""
    preset = pool[name]
    parent = preset.get("inherits")
    if parent in pool and parent != name and parent not in seen:
        base = user_chain(parent, pool, seen + (name,))
        return {**base, **{k: v for k, v in preset.items() if k != "inherits"}, "inherits": base.get("inherits")}
    return dict(preset)


FILAMENT_TYPE = re.compile(r"\b(PLA|PETG|ABS|ASA|TPU|PA|PC|PVA|HIPS|PET)\b", re.I)


def filament_type_of(preset):
    ft = preset.get("filament_type")
    if ft:
        return str(ft[0] if isinstance(ft, list) else ft).upper()
    m = FILAMENT_TYPE.search(f"{preset.get('name', '')} {preset.get('inherits', '')}")
    return m.group(1).upper() if m else None


def pick_user_machine(machines, nozzle):
    names = sorted(machines)
    if nozzle:
        want = f"{float(nozzle):g}"
        hit = [n for n in names if want in (user_chain(n, machines).get("nozzle_diameter") or []) or f"{want} nozzle" in n]
        if hit:
            return hit[0]
    return names[0]


def user_preset_pools(printer):
    """(bundle, machines, processes, filaments) for this printer: its own uploaded bundle plus the global
    Orca Cloud one. The printer's own preset wins when both carry the same name."""
    try:
        data = json.loads(orca_file(printer).read_text())
    except (KeyError, OSError, ValueError, AttributeError):
        data = {}
    data = data if isinstance(data, dict) else {}
    cloud = load_cloud_bundle()

    def pool(key):
        own = data.get(key)
        return {**cloud[key], **(own if isinstance(own, dict) else {})}

    machines = data.get("machines")
    return data, machines if isinstance(machines, dict) else {}, pool("processes"), pool("filaments")


def user_presets_for(printer, filaments):
    """The user's Orca presets for this printer, or None (then the built-in profile slices).
    The machine always comes from the printer; only a bundle uploaded for this printer brings a user machine."""
    data, machines, procs, fils = user_preset_pools(printer)
    name = data.get("machine")
    machine = user_chain(name, machines) if name in machines else None
    if machine and not machine.get("inherits"):  # nothing to hang the machine on: the printer's own preset stays
        machine = None
    if machine:
        processes = {n: user_chain(n, procs) for n, p in procs.items() if name in (p.get("compatible_printers") or [])}
    else:
        processes = {n: user_chain(n, procs) for n in procs}
    if not machine and not procs and not fils:
        return None  # no user profile at all: keep the built-in path, which an older slicerd understands
    loaded, chosen = data.get("loaded"), []
    for f in filaments:  # the spool set in Filaments: one of the user's presets, or a system one by id
        fid = f.get("filament_id") or ""
        own = fid[len(USER_SPOOL):] if fid.startswith(USER_SPOOL) else (loaded if not fid else None)  # loaded: before Filaments
        if own in fils:
            flat = user_chain(own, fils)
            chosen.append({**flat, "_type": filament_type_of(flat) or f.get("filament_type") or "PLA"})
        else:
            chosen.append({"_type": f.get("filament_type") or "PLA", "_id": fid or None, "_name": f.get("filament_name")})
    out = {"processes": processes, "filaments": chosen}
    if machine:
        out = {"parent": machine["inherits"], "machine": machine, **out}
    return out


def apply_default_process(printer, user, choices):
    """No process picked in Print now: the printer's default print profile, if it is still one of the user's."""
    if choices.get("process") is None and user:
        name = default_process_of(printer)
        if name in user["processes"]:
            choices["process"] = name


def default_process_of(alias):
    """The print profile Standard uses on this printer, or None (also without a database, as in status tests)."""
    try:
        row = DB.execute("SELECT value FROM settings WHERE printer=? AND key='bff_default_process'", (alias,)).fetchone()
    except (AttributeError, sqlite3.Error):
        return None
    return row[0] if row else None


def tool_orca_profiles(args):
    """Upload (bundle_b64), pick the loaded filament (loaded), or just read the state. Klipper printers only."""
    import base64
    alias = args["printer"]
    store = app_added(alias)
    if not onboard.is_klipper(store[alias].get("model")):
        raise ToolError("Your own Orca profiles are for Klipper printers")
    path = orca_file(alias)
    with ADD_LOCK:
        if args.get("bundle_b64"):
            try:
                raw = base64.b64decode(args["bundle_b64"], validate=True)
            except (ValueError, TypeError):
                raise ToolError("Not an Orca preset bundle") from None
            if len(raw) > 20_000_000:
                raise ToolError("The file is over 20 MB")
            try:
                bundle = slicer.user_bundle(raw)
            except slicer.SliceError as err:
                raise ToolError(str(err)) from None
            wanted = args.get("machine")
            if wanted:
                if wanted not in bundle["machines"]:
                    raise ToolError("Unknown printer profile")
                bundle["machine"] = wanted  # the sync tool knows which preset belongs to this printer
            elif bundle["machines"]:
                bundle["machine"] = pick_user_machine(bundle["machines"], cfg_nozzle(alias))
            else:  # filaments and processes only: they now come from the global Orca Cloud bundle
                raise ToolError("No printer profile in this file: export it from OrcaSlicer with File, Export, Export Preset Bundle")
            try:  # a re-upload keeps the chosen filament when the new bundle still has it
                loaded = json.loads(path.read_text()).get("loaded")
            except (OSError, ValueError):
                loaded = None
            if loaded in bundle["filaments"]:
                bundle["loaded"] = loaded
            path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps(bundle))
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return as_text({"machine": None, "filaments": [], "loaded": None})
        if "loaded" in args:
            name = args.get("loaded") or None
            if name and name not in data["filaments"]:
                raise ToolError("Unknown filament profile")
            data["loaded"] = name
            path.write_text(json.dumps(data))
        if args.get("remove"):
            path.unlink()
            return as_text({"machine": None, "filaments": [], "loaded": None})
    return as_text({"machine": data["machine"], "filaments": sorted(data["filaments"]), "loaded": data.get("loaded")})


ORCA_CLOUD_EVERY_S = 600
_ORCA_CLOUD_DIGEST = {}


def orca_cloud_sync():
    """Keep the account's filaments and processes in _cloud.json; the account is global, machines never come from it."""
    data = slicer.orca_user()
    if not data:
        return
    digest = hashlib.sha256(data).hexdigest()
    if _ORCA_CLOUD_DIGEST.get("bundle") == digest:
        return
    try:
        bundle = slicer.user_bundle(data)
    except slicer.SliceError:
        return
    path = cloud_bundle_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"filaments": bundle["filaments"], "processes": bundle["processes"]}))
    tmp.replace(path)  # atomic: a reader never sees half a file
    _ORCA_CLOUD_DIGEST["bundle"] = digest
    print(f"orca cloud: {len(bundle['filaments'])} filaments, {len(bundle['processes'])} processes", flush=True)


def watch_orca_cloud():
    while True:
        time.sleep(ORCA_CLOUD_EVERY_S)
        try:
            orca_cloud_sync()
        except Exception as err:  # the slicer may be down: try again next round
            print(f"orca cloud: {type(err).__name__}", flush=True)


def tool_orca_cloud(args):
    """Orca Cloud through the slicer: status and the sign-in window. Global, no printer argument."""
    action = args.get("action") or "status"
    try:
        if action == "login":
            slicer.orca_login("start", provider=args.get("provider") or "google",
                              email=args.get("email") or "", password=args.get("password") or "")
        if action == "logout":
            slicer.orca_login("logout")
            _ORCA_CLOUD_DIGEST.clear()
            try:
                cloud_bundle_path().unlink()
            except (KeyError, OSError):
                pass
        if action == "login_paste":
            slicer.orca_login("finish", url=args.get("url") or "")
        if action == "login_done":
            slicer.orca_login("stop")
        status = slicer.orca_status()
        if action == "status" and status.get("logged_in"):
            orca_cloud_sync()  # cheap when nothing changed: the digest skips it
    except slicer.SliceError as err:
        raise ToolError(str(err)) from None
    bundle = load_cloud_bundle()
    return as_text({"logged_in": bool(status.get("logged_in")), "login_open": bool(status.get("login_open")),
                    "login_state": status.get("state"), "login_link": status.get("url"),
                    "login_error": status.get("error"),
                    "filaments": len(bundle["filaments"]), "processes": len(bundle["processes"])})


def cfg_nozzle(printer):
    nozzle = (cfg_machine_of(printer) or {}).get("nozzle_diameter") or [None]
    return float(nozzle[0]) if nozzle[0] else None


def cfg_machine_of(printer):
    """printer.cfg overrides for the slicer, only for an Other Klipper printer."""
    try:
        entry = onboard.load_store(store_path()).get(printer) or {}
    except KeyError:
        return None
    return entry.get("machine") if model_from_cfg(entry.get("model")) else None


def tool_printer_info(args):
    printer_id(args)
    alias = args["printer"]
    raw = read_status(alias)
    try:
        model = load_profile(alias).get("model")
    except ToolError:
        model = None
    try:
        slicer_model = load_profile(alias).get("slicer_model")
    except ToolError:
        slicer_model = None
    reason = blocked_reason("start", raw)
    if reason is None and DB is not None and DB.execute("SELECT 1 FROM locks WHERE printer=?", (alias,)).fetchone():
        reason = "esito dell'ultimo comando sconosciuto: serve recheck"
    nozzle = (raw.get("nozzles") or [{}])[0]
    # The printer keeps reporting the last filament after an unload, so 255 must hide it.
    # ponytail: external spool only, like check_compatible; add AMS trays once one is connected and its data can be seen.
    filaments = printer_filaments(alias, raw)
    names, models = app_names_models()
    options = {}
    if flow_cali_shown(alias, raw):
        options["bff_flow_cali"] = flow_cali(alias)
    return as_text({
        "printer": alias,
        "available_printers": sorted(CFG["printers"]),
        "names": names,
        "models": models,
        "profile": profile_of(alias),
        "model": model,
        "slicer_model": slicer_model,
        "tested": tested_model(alias),
        # Klipper reports no nozzle: the one in its machine config (what the slicer uses) is shown instead.
        "nozzle": {"diameter": nozzle.get("nozzle_diameter") or cfg_nozzle(alias), "type": nozzle.get("nozzle_type") or None},
        "ams_present": bool(raw.get("ams")),
        "filaments": filaments,
        "slicer": slicer.available(),
        "orca_cad": bool(CFG.get("orca_wake_url")),
        "default_slot": filaments[0]["slot"] if filaments else None,
        "connected": raw.get("connected"),
        "state": raw.get("state"),
        "free": reason is None,
        "not_free_reason": reason,
        "awaiting_plate_clear": DB is not None and plate_clear_pending(alias),
        "attention": attention(raw),
        "plate": plate_state(raw),
        "plate_type": plate_type(alias) if DB is not None else None,
        "choices": {"nozzle_diameters": NOZZLE_DIAMETERS, "nozzle_types": NOZZLE_TYPES, "plate_types": PLATE_TYPES,
                    "spool_profiles": spool_profiles(alias),
                    "user_processes": sorted(user_preset_pools(alias)[2]),
                    "default_process": default_process_of(alias)},
        **({"options": options} if options else {}),
    })


def plate_state(raw):
    """No plate sensor: only a running print is known. Otherwise whoever asks looks at plate_photo."""
    if raw.get("connected") is True and raw.get("state") not in ACTIONS["start"]["from"]:
        return {"state": "occupied", "reason": f"stampante nello stato {raw.get('state')}"}
    return {"state": "unknown", "reason": "decidi dalla foto con plate_photo"}


def store_photo(jpeg, base=None):
    now = time.time()
    for photo_id in [k for k, v in PHOTOS.items() if v[0] <= now]:
        del PHOTOS[photo_id]
    photo_id = secrets.token_urlsafe(24)
    PHOTOS[photo_id] = (now + TOKEN_TTL_S, jpeg)
    return f"{base or CFG.get('public_url') or CFG.get('local_url', '')}/photo/{photo_id}"


def tool_prepare_print_start(args):
    pid = printer_id(args)
    alias = args["printer"]
    require_support(alias, "start")
    upload = load_upload(args.get("file_id"))
    with COMMAND_LOCK:
        assert_unlocked(alias)
        raw = read_status(alias)
        color = upload_color(args.get("file_id"))
        reason = blocked_reason("start", raw) or check_compatible(load_profile(alias), upload, raw, color, args.get("file_id")) or plate_mismatch(alias, upload)
        if reason:
            raise ToolError(f"avvio su {alias}: {reason}")
        jpeg = snapshot_jpeg(pid)
        weight = f", {upload['weight_g']} g" if upload["weight_g"] is not None else ""
        summary = (
            f"Avvio di '{upload['name']}' su {alias}: {upload['filament']}, ugello {upload['nozzle']}, "
            f"circa {upload['time_s'] // 60} minuti{weight}. {filament_note(alias, upload)} "
            "Guarda la foto: il piatto deve essere libero."
        )
        text = issue_token(alias, "start", {"file_id": args["file_id"]}, summary, photo_url=store_photo(jpeg))
        return [image_content(jpeg)] + text


def tool_confirm_print_start(args):
    printer_id(args)
    AXES.pop(args["printer"], None)
    require_support(args["printer"], "start")
    return command_reply(execute(args["printer"], "start", None, args.get("idempotency_key"), token=args.get("token")))


# Where the head and bed may go, per model, measured on the real printer with the user watching.
# safe: an XY with nothing under the nozzle, where Z may go down without asking (None: always ask).
# keep_out: (x0, y0, x1, y1) the head must never enter nor cross. first: the first move after homing.
ENVELOPES = {
    # A1, 2026-11-10: head at the left end stop, bed fully forward, nothing under the gantry.
    "A1": {"x": (-48, 256), "y": (0, 256), "z": (0, 256), "safe": {"x": -48, "y": 256}, "keep_out": None,
           "first": {"x": -48, "y": 256, "z": 10}},
    # P1S (Alpha), 2026-11-12: X/Y 0-256 free, the bed bottoms out past Z 250 (noise at 256). The front-left
    # 18x28 corner (Bambu's bed_exclude_area) is the filament cutter: the head hit the wall there. 2 mm margin.
    # The bed is always under the nozzle, so every Z descent asks. P1P has the same frame.
    "P1": {"x": (0, 256), "y": (0, 256), "z": (0, 250), "safe": None, "keep_out": (0, 0, 20, 30),
           "first": {"x": 128, "y": 128, "z": 10},
           # The bed moves in Z (CoreXY): the app names the buttons after the bed.
           "bed_z": True},
    # A1 mini, 2026-11-13: Orca profile 180x180x180, no bed_exclude_area, bed slinger.
    "A1M": {"x": (0, 180), "y": (0, 180), "z": (0, 180), "safe": None, "keep_out": None,
            "first": {"x": 90, "y": 90, "z": 10}},
}
MODEL_ENVELOPE = {"N2S": "A1", "N1": "A1M", "C11": "P1", "C12": "P1"}
# Envelopes whose home_flag bits (X, Y, Z) are verified: there homed comes from the printer, not from BFF.
HOME_FLAG_ENVELOPES = {"P1"}
MOTION_BLOCKED = ("RUNNING", "PAUSE", "PREPARE", "SLICING")
HOME_GCODE = {"xy": "G28 X", "all": "G28"}
HOME_GCODE_KLIPPER = {"xy": "G28 X Y", "all": "G28"}  # `G28 X` alone is the Bambu quirk, not Klipper


def home_gcode(alias, axes):
    """The G-code that homes `axes` on this printer."""
    return (HOME_GCODE_KLIPPER if isinstance(LINKS.get(alias), MoonrakerLink) else HOME_GCODE)[axes]
# Feed rates, mm/min: the move duration the busy guard waits for follows from these.
XY_FEED, Z_FEED = 3000, 600
CLOCK = time.monotonic  # patched in tests
# Alias -> {"homed", "pos", "drops", "busy_until"}. In memory on purpose: a restart forgets the homing.
AXES = {}


def axes_of(alias, raw=None):
    """Axes state, reset whenever the link has dropped since it was written. Read the status first."""
    drops = link_of(alias).drops
    state = AXES.get(alias)
    if state is None or state["drops"] != drops:
        state = AXES[alias] = {"homed": False, "pos": dict.fromkeys("xyz"), "drops": drops}
    if raw is None:
        raw = read_status(alias)
    name = envelope_name_of(alias)
    if name == "KLIPPER":
        # The printer knows: homed_axes is "" until G28, and the position is real only when all three are homed.
        axes = raw.get("axes") or {}
        state["homed"] = set("xyz") <= set(axes.get("homed") or "")
        pos = axes.get("pos") or []
        known = state["homed"] and len(pos) >= 3 and None not in pos[:3]
        # pos known from the printer: the first move can name any single axis, Z is not a guess.
        state["pos"] = {axis: round(pos[i], 2) for i, axis in enumerate("xyz")} if known else dict.fromkeys("xyz")
    elif name in HOME_FLAG_ENVELOPES:
        # The printer knows: bits off mean motors released, so the position is lost exactly like on a drop.
        flag = raw.get("home_flag")
        state["homed"] = isinstance(flag, int) and (flag & 7) == 7
        if not state["homed"]:
            state["pos"] = dict.fromkeys("xyz")
    return state


def envelope_name_of(alias):
    """The measured envelope name of this printer's model, or None (then no motion at all)."""
    added = onboard.load_store(store_path()).get(alias) if os.environ.get("STATE_DIRECTORY") else None
    if added is None:  # not a printer added in the app: motion locked
        return None
    if isinstance(LINKS.get(alias), MoonrakerLink):  # Klipper: the limits come from the printer, per printer
        return "KLIPPER"
    return MODEL_ENVELOPE.get(added.get("model"))


def envelope_of(alias):
    name = envelope_name_of(alias)
    if name == "KLIPPER":
        axes = read_status(alias).get("axes")
        if not axes or len(axes.get("min") or []) < 3 or len(axes.get("max") or []) < 3:
            raise ToolError(f"{alias}: limiti assi non letti dalla stampante")
        (x0, y0, z0), (x1, y1, z1) = axes["min"], axes["max"]
        # The bed is always under the nozzle, so Z never goes negative even when Klipper's min does.
        return {"x": (x0, x1), "y": (y0, y1), "z": (max(0, z0), z1), "safe": None, "keep_out": None,
                "first": {"x": (x0 + x1) / 2, "y": (y0 + y1) / 2, "z": 10}}
    return ENVELOPES[name] if name else None


def motion_measured(alias):
    return envelope_of(alias) is not None


def crosses(start, end, box):
    """True if the straight XY move start -> end touches box (x0, y0, x1, y1) (Liang-Barsky)."""
    (x0, y0), (x1, y1) = start, end
    t0, t1, dx, dy = 0.0, 1.0, x1 - x0, y1 - y0
    for p, q in ((-dx, x0 - box[0]), (dx, box[2] - x0), (-dy, y0 - box[1]), (dy, box[3] - y0)):
        if p == 0:
            if q < 0:
                return False
        else:
            t = q / p
            if p < 0:
                t0 = max(t0, t)
            else:
                t1 = min(t1, t)
            if t0 > t1:
                return False
    return True


def motion_ready(name, alias):
    if not motion_measured(alias):
        # The A1's safe pose (X -48) would drive a P1's head into the frame.
        raise ToolError(f"{alias}: movimenti non ancora verificati su questo modello")
    assert_unlocked(alias)
    raw = read_status(alias)
    if raw.get("connected") is not True:
        raise ToolError(f"{alias} non connessa: movimento rifiutato")
    if raw.get("state") in MOTION_BLOCKED or raw.get("state") is None:
        raise ToolError(f"{alias} nello stato {raw.get('state')}: niente movimenti durante una stampa")
    state = axes_of(alias, raw)
    if CLOCK() < state.get("busy_until", 0):
        raise ToolError("Movimento precedente ancora in corso")
    return state, raw


def plate_unclear(alias, raw):
    """The plate is not marked clear: moving X/Y or homing all would touch it."""
    return plate_clear_pending(alias)


def mm(value):
    return f"{value:.3f}".rstrip("0").rstrip(".")


def plan_move(state, args, confirmed, env=None, plate=False):
    """G-code lines, the position after them and the seconds they take. Order: Z up, then X/Y, then Z down."""
    if not state["homed"]:
        raise ToolError("Assi non azzerati: fai prima prepare_home con axes='all'")
    target = {}
    for axis in "xyz":
        low, high = env[axis]
        value = args.get(axis)
        if value is None:
            continue
        if type(value) not in (int, float) or not low <= value <= high:
            raise ToolError(f"{axis} deve essere un numero tra {low} e {high} mm")
        target[axis] = value
    if not target:
        raise ToolError("Indica almeno un asse: x, y o z")
    if target.get("z", 1) < 1:
        raise ToolError("Z sotto 1 mm: BFF non scende così vicino al piatto")
    pos = state["pos"]
    if None in pos.values() and len(target) < 3:
        raise ToolError("Posizione non nota: il primo movimento dopo l'azzeramento deve dare x, y e z")
    after = {**pos, **target}
    z, current = target.get("z"), pos["z"]
    # Unknown Z (first move after homing): move Z first, and still require confirmation.
    up = z is not None and (current is None or z > current)
    down = z is not None and current is not None and z < current
    if current is None and not confirmed:
        raise ToolError("Primo movimento dopo l'azzeramento: Z non nota, usa prepare_move e confirm_move")
    moves_xy = "x" in target or "y" in target
    # X/Y run at the raised Z when Z goes up first, else at the current Z (also on the first move, Z unknown).
    if moves_xy and ((z if up else current) is None or (z if up else current) < 10):
        raise ToolError("Alza Z ad almeno 10 mm prima di muovere X/Y")
    if moves_xy and plate and not confirmed:
        raise ToolError("Piatto non liberato: muovere X/Y richiede prepare_move e confirm_move")
    box = env["keep_out"]
    if box:
        start = (pos["x"], pos["y"]) if None not in (pos["x"], pos["y"]) else None
        # Unknown start = just homed, head at the centre: from there no straight move reaches the corner box
        # without ending in it, so the end point is enough.
        if crosses(start or (after["x"], after["y"]), (after["x"], after["y"]), box):
            raise ToolError("Movimento nella zona del taglierino (angolo davanti a sinistra): rifiutato")
    safe = env["safe"]
    if down and not confirmed and (safe is None or (after["x"], after["y"]) != (safe["x"], safe["y"])):
        raise ToolError("Discesa di Z fuori dalla posa sicura: usa prepare_move e confirm_move")
    lines = ["G90", "G21"] + ([f"G1 Z{mm(z)} F600"] if up else [])
    xy = " ".join(f"{axis.upper()}{mm(target[axis])}" for axis in "xy" if axis in target)
    lines += ([f"G1 {xy} F3000"] if xy else []) + ([f"G1 Z{mm(z)} F600"] if down else [])
    xy_len = math.hypot(after["x"] - pos["x"], after["y"] - pos["y"]) if None not in (pos["x"], pos["y"]) else 0
    z_len = abs(after["z"] - pos["z"]) if None not in (pos["z"], after["z"]) else 0
    return lines, after, xy_len / (XY_FEED / 60) + z_len / (Z_FEED / 60)


def axes_params(args):
    return {axis: args[axis] for axis in "xyz" if args.get(axis) is not None}


def run_motion(name, args, action, params=None, confirmed=False):
    """Send one homing or move. `params` is None when the token carries it. Sent is the outcome: QoS 0, no echo to wait for."""
    printer_id(args)
    alias, key = args["printer"], args.get("idempotency_key")
    if not (isinstance(key, str) and 8 <= len(key) <= 128):
        raise ToolError("idempotency_key mancante: stringa di 8-128 caratteri")
    with COMMAND_LOCK:
        previous = replay_of(key, alias, action, params)
        if previous:
            if previous["outcome"] != "sent":
                raise ToolError(f"{action} su {alias}: {previous['outcome']}, {previous['detail']}")
            return as_text(previous)
        state, raw = motion_ready(name, alias)
        if params is None:
            params = consume_token(args.get("token"), alias, action)
        plate = plate_unclear(alias, raw)
        if action == "home":
            if params["axes"] == "all" and plate:
                raise ToolError("Piatto da liberare: l'azzeramento di Z tocca il piatto. "
                                "Premi Plate is clear o fai solo Home XY")
            lines, after, seconds = [home_gcode(alias, params["axes"])], dict.fromkeys("xyz"), 0
        else:
            lines, after, seconds = plan_move(state, params, confirmed, envelope_of(alias), plate)
        text = "\n".join(lines)
        sent = link_of(alias).gcode(text)
        with DB:
            save(key, alias, action, params, "sent" if sent else "not_sent", text)
        if not sent:
            raise ToolError("Comando non partito: nessun reinvio automatico, riprova con una chiave nuova")
        state["pos"] = after
        if action == "home" and params["axes"] == "all":
            state["homed"] = True
        if action == "move":  # one move at a time, until the head has had the time to get there
            state["busy_until"] = CLOCK() + seconds + 0.5
        return as_text({"printer": alias, "action": action, "outcome": "sent", "detail": text})


def tool_prepare_home(args):
    pid = printer_id(args)
    alias, axes = args["printer"], args.get("axes")
    with COMMAND_LOCK:
        _, raw = motion_ready("prepare_home", alias)
        if axes not in HOME_GCODE:
            raise ToolError("axes deve essere xy o all")
        if axes == "all" and plate_unclear(alias, raw):
            raise ToolError("Piatto da liberare: l'azzeramento di Z tocca il piatto. "
                            "Premi Plate is clear o fai solo Home XY")
        summary = (
            f"Azzeramento {axes} su {alias}: '{home_gcode(alias, axes)}'. Claude deve giudicare la foto prima di confermare. "
            "axes='xy' azzera solo X e Y, Z non si muove, il piatto scorre nel telaio e mostra oggetti nascosti. "
            "axes='all' esegue 'G28': Z tasta il piatto, che deve essere libero."
        )
        parts, photo = shot_parts(pid)
        return parts + issue_token(alias, "home", {"axes": axes}, summary, **photo)


def tool_confirm_home(args):
    return run_motion("confirm_home", args, "home")


def tool_move(args):
    return run_motion("move", args, "move", axes_params(args))


def tool_prepare_move(args):
    pid = printer_id(args)
    alias, params = args["printer"], axes_params(args)
    with COMMAND_LOCK:
        state, raw = motion_ready("prepare_move", alias)
        lines = plan_move(state, params, confirmed=True, env=envelope_of(alias),
                          plate=plate_unclear(alias, raw))[0]
        summary = f"Movimento su {alias}: {'; '.join(lines)}. Guarda la foto prima di confermare."
        parts, photo = shot_parts(pid)
        return parts + issue_token(alias, "move", params, summary, **photo)


def tool_confirm_move(args):
    return run_motion("confirm_move", args, "move", confirmed=True)


# Jog: relative moves on a Klipper printer, which reports its own position. The app's buttons.
JOG_FEED = {"x": XY_FEED, "y": XY_FEED, "z": Z_FEED}


def tool_jog(args):
    printer_id(args)
    alias, key = args["printer"], args.get("idempotency_key")
    axis, distance = args.get("axis"), args.get("distance")
    if envelope_name_of(alias) != "KLIPPER":
        raise ToolError("jog solo per stampanti Klipper")
    if axis not in JOG_FEED:
        raise ToolError("axis deve essere x, y o z")
    if type(distance) not in (int, float) or not -100 <= distance <= 100:
        raise ToolError("distance deve essere un numero tra -100 e 100 mm")
    if not (isinstance(key, str) and 8 <= len(key) <= 128):
        raise ToolError("idempotency_key mancante: stringa di 8-128 caratteri")
    params = {"axis": axis, "distance": distance}
    with COMMAND_LOCK:
        previous = replay_of(key, alias, "jog", params)
        if previous:
            if previous["outcome"] != "sent":
                raise ToolError(f"jog su {alias}: {previous['outcome']}, {previous['detail']}")
            return as_text(previous)
        assert_unlocked(alias)
        raw = read_status(alias)
        if raw.get("connected") is not True:
            raise ToolError(f"{alias} non connessa: movimento rifiutato")
        if raw.get("state") in MOTION_BLOCKED or raw.get("state") is None:
            raise ToolError(f"{alias} nello stato {raw.get('state')}: niente movimenti durante una stampa")
        state = axes_of(alias, raw)
        if CLOCK() < state.get("busy_until", 0):
            raise ToolError("Movimento precedente ancora in corso")
        live = raw.get("axes") or {}
        if axis not in set(live.get("homed") or ""):
            raise ToolError(f"Asse {axis.upper()} non azzerato: fai Home")
        pos = live.get("pos") or []
        current = pos["xyz".index(axis)] if "xyz".index(axis) < len(pos) else 0
        target = (current if type(current) in (int, float) else 0) + distance
        if axis == "z" and target < 1:
            raise ToolError("Z sotto 1 mm: Pygmy non scende cosi vicino al piatto")
        low, high = envelope_of(alias)[axis]
        if not low <= target <= high:
            raise ToolError(f"{axis.upper()} fuori area: il limite e' {low} - {high} mm")
        feed = JOG_FEED[axis]
        text = f"G91\nG1 {axis.upper()}{mm(distance)} F{feed}\nG90"
        sent = link_of(alias).gcode(text)
        with DB:
            save(key, alias, "jog", params, "sent" if sent else "not_sent", text)
        if not sent:
            raise ToolError("Comando non partito: nessun reinvio automatico, riprova con una chiave nuova")
        state["busy_until"] = CLOCK() + abs(distance) / (feed / 60) + 0.5
        return as_text({"printer": alias, "action": "jog", "outcome": "sent", "detail": text})


def tool_home(args):
    """Direct homing on a Klipper printer: no photo token, the app's tap is the human yes."""
    printer_id(args)
    alias, key = args["printer"], args.get("idempotency_key")
    axes = args.get("axes")
    if envelope_name_of(alias) != "KLIPPER":
        raise ToolError("home solo per stampanti Klipper")
    if axes not in HOME_GCODE:
        raise ToolError("axes deve essere xy o all")
    params = {"axes": axes}
    with COMMAND_LOCK:
        previous = replay_of(key, alias, "home", params)
        if previous:
            if previous["outcome"] != "sent":
                raise ToolError(f"home su {alias}: {previous['outcome']}, {previous['detail']}")
            return as_text(previous)
        _, raw = motion_ready("home", alias)
        if axes == "all" and plate_unclear(alias, raw):
            raise ToolError("Piatto da liberare: l'azzeramento di Z tocca il piatto. "
                            "Premi Plate is clear o fai solo Home XY")
        text = home_gcode(alias, axes)
        sent = link_of(alias).gcode(text)
        with DB:
            save(key, alias, "home", params, "sent" if sent else "not_sent", text)
        if not sent:
            raise ToolError("Comando non partito: nessun reinvio automatico, riprova con una chiave nuova")
        return as_text({"printer": alias, "action": "home", "outcome": "sent", "detail": text})


def tool_motors_off(args):
    printer_id(args)
    alias = args["printer"]
    if envelope_name_of(alias) != "KLIPPER":
        raise ToolError("motors_off solo per stampanti Klipper")
    def send(_):
        return link_of(alias).gcode("M84"), "M84"
    return run_direct("motors_off", args, "motors_off", {}, parked, send,
                      after_sent=lambda name: AXES.pop(name, None))


def parked(s):
    """A state in which the printer is not moving: the opposite of MOTION_BLOCKED."""
    return s is not None and s not in MOTION_BLOCKED


def direct_ready(name, alias, allowed):
    """Common precondition for direct tools: unlocked, connected, allowed state."""
    assert_unlocked(alias)
    raw = read_status(alias)
    if raw.get("connected") is not True:
        raise ToolError(f"{alias} non connessa: movimento rifiutato")
    if callable(allowed):
        ok = allowed(raw.get("state"))
    else:
        ok = allowed is None or raw.get("state") in allowed
    if not ok:
        raise ToolError(f"{alias} nello stato {raw.get('state')}")
    return raw


def run_direct(name, args, action, params=None, allowed=None, send=None, after_sent=None):
    """Send one direct MQTT command with idempotency, replay and token handling.

    `params` is None when the token carries it. `send` returns (sent, detail); it must
    use the link's publish/gcode, which return False when the message did not leave.
    """
    printer_id(args)
    alias, key = args["printer"], args.get("idempotency_key")
    if not (isinstance(key, str) and 8 <= len(key) <= 128):
        raise ToolError("idempotency_key mancante: stringa di 8-128 caratteri")
    with COMMAND_LOCK:
        previous = replay_of(key, alias, action, params)
        if previous:
            if previous["outcome"] != "sent":
                raise ToolError(f"{action} su {alias}: {previous['outcome']}, {previous['detail']}")
            return as_text(previous)
        direct_ready(name, alias, allowed)
        if params is None:
            params = consume_token(args.get("token"), alias, action)
        sent, detail = send(params)
        with DB:
            save(key, alias, action, params, "sent" if sent else "not_sent", detail)
        if not sent:
            raise ToolError("Comando non partito: nessun reinvio automatico, riprova con una chiave nuova")
        if after_sent:
            after_sent(alias)
        return as_text({"printer": alias, "action": action, "outcome": "sent", "detail": detail})


SPEED_LEVELS = (1, 2, 3, 4)
FAN_INDEX = {"part": 1, "aux": 2, "chamber": 3}
DETECTOR_MODULES = (
    "spaghetti_detector", "first_layer_inspector", "printing_monitor",
    "buildplate_marker_detector", "allow_skip_parts", "pileup_detector",
    "clump_detector", "airprint_detector",
)
SENSITIVITIES = ("low", "medium", "high")
PRINT_OPTIONS = ("auto_recovery", "air_print_detect", "filament_tangle_detect",
                 "nozzle_blob_detect", "sound_enable")
CALIBRATION_BITS = {"bed_leveling": 2, "vibration": 4, "motor_noise": 8}
LOAD_FILAMENT = {"print": {"command": "ams_change_filament", "ams_id": 255, "slot_id": 254,
                           "target": 254, "curr_temp": -1, "tar_temp": -1}}


def tool_set_print_speed(args):
    level = args.get("level")
    if type(level) is not int or level not in SPEED_LEVELS:
        raise ToolError("level deve essere un intero tra 1 e 4")
    params = {"level": level}
    def send(_):
        link = link_of(args["printer"])
        if isinstance(link, MoonrakerLink):
            return link.command("speed", level=level), f"M220 S{SPEED_PERCENT[level]}"
        payload = {"print": {"command": "print_speed", "param": str(level)}}
        return link.publish(with_sequence(payload)), json.dumps(payload, ensure_ascii=False)
    return run_direct("set_print_speed", args, "print_speed", params, ("RUNNING", "PAUSE"), send)


def tool_set_fan(args):
    fan, percent = args.get("fan"), args.get("percent")
    if fan not in FAN_INDEX:
        raise ToolError("fan deve essere part, aux o chamber")
    if type(percent) is not int or not 0 <= percent <= 100:
        raise ToolError("percent deve essere un intero tra 0 e 100")
    params = {"fan": fan, "percent": percent}
    gcode = f"M106 P{FAN_INDEX[fan]} S{round(percent * 255 / 100)}"
    def send(_):
        link = link_of(args["printer"])
        if isinstance(link, MoonrakerLink):
            return link.command("fan", fan=fan, percent=percent), f"{fan} {percent}%"
        return link.gcode(gcode), gcode
    return run_direct("set_fan", args, "set_fan", params, None, send)


def tool_set_detector(args):
    module, enabled = args.get("module"), args.get("enabled")
    if module not in DETECTOR_MODULES:
        raise ToolError("module deve essere un modulo xcam valido")
    if type(enabled) is not bool:
        raise ToolError("enabled deve essere true o false")
    sensitivity = args.get("sensitivity")
    if sensitivity is not None and sensitivity not in SENSITIVITIES:
        raise ToolError("sensitivity deve essere low, medium o high")
    payload = {"xcam": {"command": "xcam_control_set", "module_name": module,
                        "control": enabled, "enable": enabled, "print_halt": True}}
    params = {"module": module, "enabled": enabled}
    if sensitivity is not None:
        payload["xcam"]["halt_print_sensitivity"] = sensitivity
        params["sensitivity"] = sensitivity
    def send(_):
        return link_of(args["printer"]).publish(with_sequence(payload)), json.dumps(payload, ensure_ascii=False)
    return run_direct("set_detector", args, "set_detector", params, None, send)


def tool_set_print_option(args):
    option, enabled = args.get("option"), args.get("enabled")
    if str(option).startswith("filament_sensor:") and type(enabled) is bool:  # Klipper: SET_FILAMENT_SENSOR
        printer_id(args)
        link, name = link_of(args["printer"]), option.split(":", 1)[1]
        raw = getattr(link, "raw", {}) or {}
        if not isinstance(link, MoonrakerLink) or not re.fullmatch(r"[A-Za-z0-9_-]+", name) or not any(
                f"{kind} {name}" in raw for kind in ("filament_switch_sensor", "filament_motion_sensor")):
            raise ToolError("Unknown filament sensor")
        if not link.gcode(f"SET_FILAMENT_SENSOR SENSOR={name} ENABLE={1 if enabled else 0}"):
            raise ToolError("The printer did not take the command")
        return as_text({"printer": args["printer"], "option": option, "enabled": enabled, "outcome": "sent"})
    if option == "bff_flow_cali":  # BFF's own print-start setting
        printer_id(args)
        mode = args.get("mode")
        if mode not in FLOW_CALI_MODES:
            raise ToolError("mode deve essere auto, on o off")
        with DB:
            DB.execute("INSERT OR REPLACE INTO settings VALUES (?, ?, ?)", (args["printer"], option, mode))
        return as_text({"printer": args["printer"], "option": option, "mode": mode, "outcome": "saved"})
    if option == "bff_default_process":  # the print profile "Standard" uses on this printer; "" = the built-in one
        printer_id(args)
        process = args.get("process") or ""
        if not isinstance(process, str) or len(process) > 200:
            raise ToolError("process deve essere il nome di un profilo di stampa")
        with DB:
            if process:
                DB.execute("INSERT OR REPLACE INTO settings VALUES (?, ?, ?)", (args["printer"], option, process))
            else:
                DB.execute("DELETE FROM settings WHERE printer=? AND key=?", (args["printer"], option))
        return as_text({"printer": args["printer"], "option": option, "process": process, "outcome": "saved"})
    if option in ("bff_spaghetti", "bff_spaghetti_pause") and type(enabled) is bool:  # BFF's own switches
        printer_id(args)
        with DB:
            DB.execute("INSERT OR REPLACE INTO settings VALUES (?, ?, ?)",
                       (args["printer"], option, "on" if enabled else "off"))
        return as_text({"printer": args["printer"], "option": option, "enabled": enabled, "outcome": "saved"})
    if option not in PRINT_OPTIONS:
        raise ToolError("option deve essere un'opzione di stampa valida")
    if type(enabled) is not bool:
        raise ToolError("enabled deve essere true o false")
    payload = {"print": {"command": "print_option", option: enabled}}
    params = {"option": option, "enabled": enabled}
    def send(_):
        return link_of(args["printer"]).publish(with_sequence(payload)), json.dumps(payload, ensure_ascii=False)
    return run_direct("set_print_option", args, "set_print_option", params, None, send)


def tool_clear_errors(args):
    def send(_):
        link = link_of(args["printer"])
        if isinstance(link, MoonrakerLink):  # Klipper has no printer-side list to clean: forget the stored reason
            link.clear_error()
            return True, "clear_errors"
        sent = link.publish(with_sequence({"print": {"command": "clean_print_error"}}))
        if sent:
            link.publish(PUSHALL)
        return sent, "clean_print_error + pushall"
    return run_direct("clear_errors", args, "clear_errors", {}, None, send)


def tool_dismiss_spaghetti(args):
    printer_id(args)
    watch = WATCHES.get(args["printer"])
    if not watch or watch.print is None:
        raise ToolError("nessuna stampa sotto controllo spaghetti")
    watch.muted = True
    if watch.verdict in ("warning", "failure"):
        watch.verdict = "dismissed"
    return as_text({"printer": args["printer"], "spaghetti": "dismissed until this print ends"})


def tool_prepare_skip_objects(args):
    printer_id(args)
    alias = args["printer"]
    obj_ids = args.get("obj_ids")
    if not isinstance(obj_ids, list) or not obj_ids or not all(type(i) is int for i in obj_ids):
        raise ToolError("obj_ids deve essere una lista non vuota di interi")
    with COMMAND_LOCK:
        direct_ready("prepare_skip_objects", alias, ("RUNNING", "PAUSE"))
        summary = f"Salta gli oggetti {obj_ids} su {alias}. Irreversibile."
        return issue_token(alias, "skip_objects", {"obj_ids": obj_ids}, summary)


def tool_confirm_skip_objects(args):
    def send(params):
        payload = {"print": {"command": "skip_objects", "obj_list": params["obj_ids"]}}
        return link_of(args["printer"]).publish(with_sequence(payload)), json.dumps(payload, ensure_ascii=False)
    return run_direct("confirm_skip_objects", args, "skip_objects", None, ("RUNNING", "PAUSE"), send)


def tool_prepare_calibration(args):
    pid = printer_id(args)
    alias = args["printer"]
    items = args.get("items")
    if not isinstance(items, list) or not items or not all(i in CALIBRATION_BITS for i in items):
        raise ToolError("items deve essere un sottoinsieme non vuoto di bed_leveling, vibration, motor_noise")
    items = sorted(dict.fromkeys(items))
    option = sum(CALIBRATION_BITS[i] for i in items)
    with COMMAND_LOCK:
        direct_ready("prepare_calibration", alias, parked)
        summary = f"Calibrazione {', '.join(items)} su {alias}: gli assi si muovono e il piatto viene sondato."
        jpeg, photo = plate_shot(pid)
        return [image_content(jpeg)] + issue_token(alias, "calibration", {"items": items, "option": option}, summary, **photo)


def tool_confirm_calibration(args):
    def send(params):
        payload = {"print": {"command": "calibration", "option": params["option"]}}
        return link_of(args["printer"]).publish(with_sequence(payload)), json.dumps(payload, ensure_ascii=False)
    return run_direct("confirm_calibration", args, "calibration", None, parked, send,
                      after_sent=lambda alias: AXES.pop(alias, None))


def _unload_temp(raw):
    temp = (raw.get("temperatures") or {}).get("nozzle")
    return temp if isinstance(temp, (int, float)) and temp >= 180 else 210


def tool_prepare_load_filament(args):
    printer_id(args)
    alias = args["printer"]
    with COMMAND_LOCK:
        direct_ready("prepare_load_filament", alias, parked)
        summary = f"Carico della bobina esterna su {alias}: l'ugello si scalda."
        return issue_token(alias, "load_filament", {}, summary)


def tool_confirm_load_filament(args):
    def send(_):
        return link_of(args["printer"]).publish(with_sequence(json.loads(json.dumps(LOAD_FILAMENT)))), json.dumps(LOAD_FILAMENT, ensure_ascii=False)
    return run_direct("confirm_load_filament", args, "load_filament", None, parked, send,
                      after_sent=lambda alias: AXES.pop(alias, None))


def tool_prepare_unload_filament(args):
    printer_id(args)
    alias = args["printer"]
    with COMMAND_LOCK:
        raw = direct_ready("prepare_unload_filament", alias, parked)
        temp = _unload_temp(raw)
        summary = f"Scarico della bobina esterna su {alias}: l'ugello si scalda (temperatura decisa dalla stampante)."
        return issue_token(alias, "unload_filament", {"temp": temp}, summary)


def tool_confirm_unload_filament(args):
    def send(params):
        t = params["temp"]
        payload = {"print": {"command": "ams_change_filament", "ams_id": 255, "slot_id": 255,
                             "target": 255, "curr_temp": t, "tar_temp": t}}
        return link_of(args["printer"]).publish(with_sequence(payload)), json.dumps(payload, ensure_ascii=False)
    return run_direct("confirm_unload_filament", args, "unload_filament", None, parked, send,
                      after_sent=lambda alias: AXES.pop(alias, None))


NOZZLE_DIAMETERS = (0.2, 0.4, 0.6, 0.8)
NOZZLE_TYPES = ("stainless_steel", "hardened_steel")
# Fallback when profiles/filaments.json is not deployed yet. id -> (name, type, min, max)
SPOOL_PROFILES = {
    "GFA00": ("Bambu PLA Basic", "PLA", 190, 230),
    "GFA01": ("Bambu PLA Matte", "PLA", 190, 230),
    "GFL99": ("Generic PLA", "PLA", 190, 230),
    "GFG99": ("Generic PETG", "PETG", 220, 260),
    "GFB99": ("Generic ABS", "ABS", 240, 270),
    "GFB98": ("Generic ASA", "ASA", 240, 270),
    "GFU99": ("Generic TPU", "TPU", 200, 250),
    # The other Generic profiles of Orca-Cad (codes from its printers/bambu_filament_ids.json, ranges from its @base profiles).
    "GFL96": ("Generic PLA Silk", "PLA", 190, 240),
    "GFL95": ("Generic PLA High Speed", "PLA", 190, 240),
    "GFL98": ("Generic PLA-CF", "PLA-CF", 190, 240),
    "GFG96": ("Generic PETG HF", "PETG", 220, 270),
    "GFG98": ("Generic PETG-CF", "PETG-CF", 240, 270),
    "GFG97": ("Generic PCTG", "PCTG", 240, 270),
    "GFN99": ("Generic PA", "PA", 240, 280),
    "GFN98": ("Generic PA-CF", "PA-CF", 260, 300),
    "GFN97": ("Generic PPA-CF", "PPA-CF", 280, 320),
    "GFN96": ("Generic PPA-GF", "PPA-GF", 280, 320),
    "GFC99": ("Generic PC", "PC", 260, 290),
    "GFS99": ("Generic PVA", "PVA", 190, 240),
    "GFS98": ("Generic HIPS", "HIPS", 220, 270),
    "GFS97": ("Generic BVOH", "BVOH", 190, 240),
    "GFR99": ("Generic EVA", "EVA", 175, 220),
    "GFR98": ("Generic PHA", "PHA", 190, 240),
    "GFP99": ("Generic PE", "PE", 175, 220),
    "GFP98": ("Generic PE-CF", "PE-CF", 175, 220),
    "GFP97": ("Generic PP", "PP", 220, 250),
    "GFP96": ("Generic PP-CF", "PP-CF", 220, 250),
    "GFP95": ("Generic PP-GF", "PP-GF", 220, 250),
    "GFT97": ("Generic PPS", "PPS", 300, 340),
    "GFT98": ("Generic PPS-CF", "PPS-CF", 310, 340),
    "GFU98": ("Generic TPU for AMS", "TPU", 200, 250),
    # Asked by Orca-Cad, 2026-11-11: the profiles its slicer can use.
    "GFL05": ("Overture Matte PLA", "PLA", 190, 230),
    "GFL04": ("Overture PLA", "PLA", 190, 230),
    "GFL01": ("PolyTerra PLA", "PLA", 190, 230),
    "GFL00": ("PolyLite PLA", "PLA", 190, 230),
    "GFL03": ("eSUN PLA+", "PLA", 190, 230),
    "GFG60": ("PolyLite PETG", "PETG", 220, 260),
    "GFB60": ("PolyLite ABS", "ABS", 240, 270),
    "GFB61": ("PolyLite ASA", "ASA", 240, 270),
}
VENDORS = {"PolyTerra": "Polymaker", "PolyLite": "Polymaker"}  # Product line -> vendor; otherwise the first word is the vendor.
# The full Orca catalog is generated on a Mac by tools/filament_catalog.py (BFF has no OrcaSlicer) and shipped as
# profiles/filaments.json. Without it the hand-written list above is used, so BFF keeps working.
_CATALOG = {}


def spool_catalog():
    path = CFG.get("profiles") or ""
    if path not in _CATALOG:
        entries = None
        if path:
            try:
                entries = json.loads((Path(path) / "filaments.json").read_text())["filaments"]
            except (OSError, ValueError, KeyError, TypeError):
                entries = None
        _CATALOG[path] = entries if isinstance(entries, list) and entries else [
            {"id": k, "name": v[0], "vendor": VENDORS.get(v[0].split()[0], v[0].split()[0]),
             "type": v[1], "min": v[2], "max": v[3], "models": "all"} for k, v in SPOOL_PROFILES.items()]
    return _CATALOG[path]


USER_SPOOL, USER_VENDOR = "user:", "My Orca profiles"


def user_spool_entries(alias):
    """The user's own Orca filament presets for a printer: its uploaded bundle plus the global Orca Cloud one."""
    try:
        own = json.loads(orca_file(alias).read_text()).get("filaments") or {}
    except (KeyError, OSError, ValueError, AttributeError):
        own = {}
    fils = {**load_cloud_bundle()["filaments"], **(own if isinstance(own, dict) else {})}  # the printer's own wins
    out = []
    for name in sorted(fils):
        flat = user_chain(name, fils)

        def temp(key, default):
            v = flat.get(key)
            try:
                return int(float(v[0] if isinstance(v, list) else v))
            except (TypeError, ValueError, IndexError):
                return default
        out.append({"id": USER_SPOOL + name, "name": name, "vendor": USER_VENDOR, "type": filament_type_of(flat) or "PLA",
                    "min": temp("nozzle_temperature_range_low", 190), "max": temp("nozzle_temperature_range_high", 230),
                    "models": "all"})
    return out


def spool_entries(alias):
    """Catalog entries a spool of this printer can be set to: the user's Orca filaments come first, on any printer."""
    return user_spool_entries(alias) + spool_catalog()


def catalog_name(profile_id):
    """The catalog name of a spool id (GFL04 -> Overture PLA): the slicer matches by name when its ids differ."""
    return next((e["name"] for e in spool_catalog() if e["id"] == profile_id), None) if profile_id else None


def spool_profiles(alias):
    """The filaments this printer can run: the ones for its slicer model plus the universal ("all") ones."""
    try:
        model = load_profile(alias).get("slicer_model")
    except ToolError:
        model = None
    return [{"id": e["id"], "name": e["name"], "material": e["type"], "vendor": e["vendor"], "min": e["min"], "max": e["max"]}
            for e in spool_entries(alias) if e.get("models") == "all" or model in (e.get("models") or [])]


# Orca's curr_bed_type names, so Orca-Cad slices for the plate on the bed. The A1 does not store it: BFF does.
PLATE_TYPES = ("Textured PEI Plate", "High Temp Plate", "Cool Plate", "Supertack Plate", "Engineering Plate", "Textured Cool Plate")
# Free model sites Orca-Cad can download from: the host decides the source.
SOURCE_URL = re.compile(r"https://(www\.)?(makerworld|printables|thingiverse)\.com/[\w./?=&%#~+:-]{1,400}")
# A model sent from the phone; Orca-Cad fetches it from GET /requests/<id>/file.
MODEL_NAME = re.compile(r"[\w .()+-]{1,120}\.(stl|3mf|step|stp)", re.IGNORECASE)


def publish_send(alias, payload):
    def send(_):
        return link_of(alias).publish(with_sequence(payload)), json.dumps(payload, ensure_ascii=False)
    return send


def pushall(alias):
    link_of(alias).publish(PUSHALL)


def tool_set_nozzle(args):
    diameter, kind = args.get("diameter"), args.get("type")
    if type(diameter) not in (int, float) or diameter not in NOZZLE_DIAMETERS:
        raise ToolError("diameter deve essere 0.2, 0.4, 0.6 o 0.8")
    if kind not in NOZZLE_TYPES:
        raise ToolError("type deve essere stainless_steel o hardened_steel")
    payload = {"system": {"command": "set_accessories", "accessory_type": "nozzle", "nozzle_diameter": diameter, "nozzle_type": kind}}
    return run_direct("set_nozzle", args, "set_nozzle", {"diameter": diameter, "type": kind}, parked,
                      publish_send(args.get("printer"), payload), after_sent=pushall)


def tool_set_spool(args):
    profile, color, slot = args.get("profile_id"), args.get("color"), args.get("slot")
    entry = next((e for e in spool_entries(args.get("printer")) if e["id"] == profile), None) if isinstance(profile, str) else None
    if entry is None:
        raise ToolError("profile_id non valido: usa uno di quelli offerti da printer_info")
    if not (isinstance(color, str) and re.fullmatch(r"[0-9A-F]{8}", color)):
        raise ToolError("color deve essere RRGGBBAA, esadecimale maiuscolo")
    material, low, high = entry["type"], entry["min"], entry["max"]
    # ponytail: without slot this is the external spool, ams_id 255 / tray_id 254 as Bambu Studio sends it (checked on the A1).
    # With a slot, ams_id is the AMS unit 0-3 (A-D) and tray_id the slot 0-3 (1-4).
    if slot is None:
        ams_id, tray_id = 255, 254
    elif isinstance(slot, str) and re.fullmatch(r"[A-D][1-4]", slot):
        ams_id, tray_id = ord(slot[0]) - ord("A"), int(slot[1]) - 1
    else:
        raise ToolError("slot deve essere da A1 a D4")
    payload = {"print": {"command": "ams_filament_setting", "ams_id": ams_id, "tray_id": tray_id, "tray_info_idx": profile,
                         "setting_id": "", "tray_color": color, "tray_type": material,
                         "nozzle_temp_min": low, "nozzle_temp_max": high}}
    direct = {"profile_id": profile, "color": color}
    if slot is not None:
        direct["slot"] = slot
    if slot is None and isinstance(LINKS.get(args.get("printer")), MoonrakerLink):
        # Klipper has no tray to write: BFF keeps the external spool for status, slicing and the app.
        with DB:
            DB.execute("INSERT OR REPLACE INTO settings VALUES (?, 'spool', ?)",
                       (args["printer"], json.dumps({"profile_id": profile, "color": color,
                                                     "material": material, "name": entry["name"]})))
        return as_text({"printer": args["printer"], "action": "set_spool", "outcome": "sent",
                        "detail": f"bobina esterna: {material} {color}"})
    # An AMS slot only stores what is in it, like Bambu Studio does mid-print; the external spool keeps the parked rule.
    return run_direct("set_spool", args, "set_spool", direct, None if slot else parked,
                      publish_send(args.get("printer"), payload), after_sent=pushall)


PRINTING = ("RUNNING", "PREPARE", "PAUSE")


def tool_read_filament(args):
    """Ask the AMS to read a slot's RFID tag again (it turns the spool), as Bambu Studio's refresh does."""
    printer_id(args)
    slot = args.get("slot")
    if not (isinstance(slot, str) and re.fullmatch(r"[A-D][1-4]", slot)):
        raise ToolError("slot deve essere da A1 a D4")
    unit, tray = ord(slot[0]) - ord("A"), int(slot[1]) - 1
    raw = read_status(args["printer"])
    now = raw.get("tray_now")
    try:
        now = int(now)
    except (TypeError, ValueError):
        now = 255
    # the user: never while this AMS feeds the print (254/255 = external spool / none).
    if raw.get("state") in PRINTING and now < 254 and now // 4 == unit:
        raise ToolError(f"AMS {slot[0]} sta alimentando la stampa: rileggi a stampa finita")
    payload = {"print": {"command": "ams_get_rfid", "ams_id": unit, "slot_id": tray}}
    return run_direct("read_filament", args, "read_filament", {"slot": slot}, None,
                      publish_send(args.get("printer"), payload), after_sent=pushall)


DRY_MODELS = {"AMS 2 Pro": 65, "AMS HT": 85}  # AMS units with a heater: model -> hottest drying temperature


def ams_unit_of(raw, unit):
    """(report id, unit dict) for a letter A-D or the id the printer reports, or None."""
    for u in raw.get("ams") or []:
        try:
            uid = int(u.get("id"))
        except (TypeError, ValueError):
            continue
        if isinstance(unit, int) and unit == uid:
            return uid, u
        if isinstance(unit, str):
            if unit.isdigit() and int(unit) == uid:
                return uid, u
            if len(unit) == 1 and unit.upper() == "ABCD"[uid % 4]:
                return uid, u
    return None


def tool_ams_dry(args):
    """Start or stop drying on an AMS 2 Pro / AMS HT unit, as Bambu Studio sends it."""
    printer_id(args)
    printer, action = args["printer"], args.get("action")
    if action not in ("start", "stop"):
        raise ToolError("action must be start or stop")
    raw = read_status(printer)
    found = ams_unit_of(raw, args.get("unit"))
    if found is None:
        raise ToolError("No such AMS on this printer")
    uid, unit = found
    model = unit.get("model")
    if model not in DRY_MODELS:
        raise ToolError("This AMS has no heater")
    if not onboard.ams_drying(model_code(printer)):
        raise ToolError("This printer cannot start AMS drying: use the button on the AMS")
    if action == "start" and raw.get("state") in ("RUNNING", "PAUSE"):
        raise ToolError("Drying is not available during a print")
    temp, hours = args.get("temp"), args.get("hours")
    if action == "start":
        if type(temp) is not int or not 45 <= temp <= DRY_MODELS[model]:
            raise ToolError(f"Drying temperature must be between 45 and {DRY_MODELS[model]} °C")
        if type(hours) is not int or not 1 <= hours <= 24:
            raise ToolError("Drying time must be 1 to 24 hours")
        payload_temp, cooling, duration, mode = temp, 45, hours, 1
    else:
        payload_temp, cooling, duration, mode = 0, 40, 0, 0
    payload = {"print": {"command": "ams_filament_drying", "sequence_id": "0", "ams_id": uid,
                         "temp": payload_temp, "cooling_temp": cooling, "duration": duration, "humidity": 0,
                         "mode": mode, "rotate_tray": False, "filament": "", "close_power_conflict": False}}
    letter = "ABCD"[uid % 4]
    detail = f" unit {letter} {temp}°C {hours}h" if action == "start" else f" unit {letter}"
    print(f"{printer}: drying {action}{detail}", file=sys.stderr, flush=True)
    run_direct("ams_dry", args, "ams_dry", {"unit": args.get("unit"), "action": action, "temp": temp, "hours": hours},
               None, publish_send(printer, payload), after_sent=pushall)
    return as_text({"sent": True})


def plate_type(alias):
    row = DB.execute("SELECT value FROM settings WHERE printer=? AND key='plate_type'", (alias,)).fetchone()
    return row[0] if row else None


def tool_set_plate(args):
    printer_id(args)
    plate = args.get("plate")
    if plate not in PLATE_TYPES:
        raise ToolError("plate deve essere uno di " + ", ".join(PLATE_TYPES))
    with DB:
        DB.execute("INSERT OR REPLACE INTO settings VALUES (?, 'plate_type', ?)", (args["printer"], plate))
    return as_text({"printer": args["printer"], "plate_type": plate, "outcome": "saved"})


def tool_ready_files(args):
    printer_id(args)
    rows = DB.execute("SELECT file_id, name, model, nozzle, filament, time_s, weight_g, plate FROM uploads ORDER BY file_id DESC").fetchall()
    keys = ("file_id", "name", "model", "nozzle", "filament", "time_s", "weight_g", "plate")
    return as_text({"files": [dict(zip(keys, row)) for row in rows if library.exists(row[0])]})


def queued_file_ids():
    if DB is None:
        return set()
    return {r[0] for r in DB.execute("SELECT DISTINCT file_id FROM queue").fetchall()}


def pop_queued(alias, file_id):
    """A print starting from the queue drops its oldest entry; the file stays."""
    with DB:
        row = DB.execute("SELECT id FROM queue WHERE printer=? AND file_id=? ORDER BY id", (alias, file_id)).fetchone()
        if row:
            DB.execute("DELETE FROM queue WHERE id=?", (row[0],))


def tool_queue_add(args):
    printer_id(args)
    file_id = args.get("file_id")
    own = args.get("own", False)
    if type(file_id) is not int:
        raise ToolError("file_id must be an integer")
    if type(own) is not bool:
        raise ToolError("own must be true or false")
    if DB.execute("SELECT 1 FROM uploads WHERE file_id=?", (file_id,)).fetchone() is None or not library.exists(file_id):
        raise ToolError("File not found")
    with DB:
        cur = DB.execute("INSERT INTO queue (printer, file_id, own, created) VALUES (?,?,?,?)",
                         (args["printer"], file_id, 1 if own else 0, time.time()))
    return as_text({"id": cur.lastrowid})


def tool_queue_remove(args):
    printer_id(args)
    qid = args.get("id")
    if type(qid) is not int:
        raise ToolError("id must be an integer")
    with DB:
        row = DB.execute("SELECT file_id, own FROM queue WHERE id=? AND printer=?", (qid, args["printer"])).fetchone()
        # The DELETE decides: of two devices removing the same entry only one deletes a row.
        if row is None or not DB.execute("DELETE FROM queue WHERE id=?", (qid,)).rowcount:
            raise ToolError("Already removed")
        file_id, own = row
        left = DB.execute("SELECT COUNT(*) FROM queue WHERE file_id=?", (file_id,)).fetchone()[0]
    if own and left == 0:
        with DB:
            DB.execute("DELETE FROM uploads WHERE file_id=?", (file_id,))
        library.remove(file_id)
    return as_text({"removed": qid})


def tool_queue_list(args):
    printer_id(args)
    rows = DB.execute(
        "SELECT q.id, q.file_id, u.name, u.time_s, u.weight_g, u.filament, u.plate, q.created "
        "FROM queue q JOIN uploads u ON u.file_id=q.file_id "
        "WHERE q.printer=? ORDER BY q.id", (args["printer"],)).fetchall()
    keys = ("id", "file_id", "name", "time_s", "weight_g", "filament", "plate", "created")
    jobs = [dict(zip(keys, row)) for row in rows if library.exists(row[1])]
    gone = [row[0] for row in rows if not library.exists(row[1])]
    if gone:  # a file removed outside BFF: its entries go, so prune and the queue stay honest
        with DB:
            DB.execute(f"DELETE FROM queue WHERE id IN ({','.join('?' * len(gone))})", gone)
    return as_text({"jobs": jobs})


def tool_delete_file(args):
    printer_id(args)
    file_id = args.get("file_id")
    if type(file_id) is not int:
        raise ToolError("file_id must be an integer")
    with DB:
        if DB.execute("SELECT 1 FROM uploads WHERE file_id=?", (file_id,)).fetchone() is None:
            raise ToolError("File not found")
        DB.execute("DELETE FROM uploads WHERE file_id=?", (file_id,))
        DB.execute("DELETE FROM queue WHERE file_id=?", (file_id,))
    library.remove(file_id)
    return as_text({"deleted": file_id})


def tool_rename_file(args):
    printer_id(args)
    file_id = args.get("file_id")
    raw = args.get("name")
    if type(file_id) is not int or type(raw) is not str:
        raise ToolError("file_id and name are required")
    name = raw.strip()
    if name.endswith(".gcode.3mf"):
        name = name[:-len(".gcode.3mf")]
    name = re.sub(r"\s+", "_", name)
    stored = name + ".gcode.3mf"
    if not re.fullmatch(r"[\w.-]{1,120}\.gcode\.3mf", stored):
        raise ToolError("Use letters, numbers, . _ -")
    with DB:
        if DB.execute("SELECT 1 FROM uploads WHERE file_id=?", (file_id,)).fetchone() is None:
            raise ToolError("File not found")
        DB.execute("UPDATE uploads SET name=? WHERE file_id=?", (stored, file_id))
    return as_text({"file_id": file_id, "name": stored})


# Where a request may come from for each new status. Orca-Cad sets working/ready/failed, the app sets started.
# blocked = Orca-Cad waits for the user; its note (English, shown in the app) says what to do.
REQUEST_MOVES = {"working": ("pending", "blocked"), "blocked": ("pending", "working", "blocked"),
                 "ready": ("pending", "working", "blocked"), "failed": ("pending", "working", "blocked"),
                 "done": ("pending", "working", "blocked"), "started": ("ready",), "draft": ("working", "blocked")}
# draft = model fetched, waiting for the app's slice_request; draft -> pending happens only there.
OPEN_REQUESTS = ("pending", "working", "blocked", "ready", "failed", "draft")


def wake_orca(request_id):
    """Tell Orca-Cad the queue changed. Best effort: Orca-Cad also reads the queue when it starts."""
    url = CFG.get("orca_wake_url")
    if not url:
        return None

    def send():
        req = urllib.request.Request(url, data=json.dumps({"request_id": request_id}).encode(), method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Authorization": "Bearer " + CFG.get("orca_wake_token", "")})
        try:
            urllib.request.urlopen(req, timeout=5).close()
        except (urllib.error.URLError, TimeoutError, OSError):
            pass

    thread = threading.Thread(target=send, daemon=True)
    thread.start()
    return thread


def tool_cancel_request(args):
    printer_id(args)
    rid = args.get("request_id")
    if type(rid) is not int:
        raise ToolError("request_id deve essere un intero")
    marks = ",".join("?" * len(OPEN_REQUESTS))
    engine = "orca"
    with DB:
        n = DB.execute(f"UPDATE print_requests SET status='cancelled' WHERE id=? AND printer=? AND status IN ({marks})",
                       (rid, args["printer"], *OPEN_REQUESTS)).rowcount
        if n:
            engine = DB.execute("SELECT COALESCE(engine, 'orca') FROM print_requests WHERE id=? AND printer=?",
                                (rid, args["printer"])).fetchone()[0]
    if not n:
        raise ToolError("Richiesta non trovata o gia' chiusa")
    if engine == "orca":
        wake_orca(rid)
    else:
        SLICE_WAKE.set()
    return as_text({"request_id": rid, "outcome": "cancelled"})


def request_engine(args):
    """engine: "server" by default only with a slot and the slicer up, else "orca"."""
    engine = args.get("engine") or ("server" if args.get("slot") and slicer.available() else "orca")
    if engine not in ("server", "orca"):
        raise ToolError("engine deve essere server o orca")
    return engine


def tool_request_print(args):
    printer_id(args)
    url = args.get("url")
    match = SOURCE_URL.fullmatch(url) if isinstance(url, str) else None
    if not match:
        raise ToolError("Serve un link https://makerworld.com, printables.com o thingiverse.com")
    copies = args.get("copies", 1)
    draft = args.get("draft") is True  # fetch only; the app's slice_request picks spools and slices
    engine = "server" if draft else request_engine(args)
    if type(copies) is not int or not 1 <= copies <= 20:
        raise ToolError("copies: da 1 a 20")
    choices, slot = args.get("choices"), args.get("slot")
    if choices is not None and not isinstance(choices, dict):
        raise ToolError("choices deve essere un oggetto")
    if engine == "server" and not draft:
        raw = read_status(args["printer"])
        slot_filament(args["printer"], raw, slot)
    with DB:
        cur = DB.execute("INSERT INTO print_requests (printer, url, created, status, source, copies, engine, choices, "
                         "slot, stage) VALUES (?,?,?,'pending',?,?,?,?,?,?)",
                         (args["printer"], url, time.time(), match[2], copies, engine,
                          json.dumps(choices) if choices is not None else None, slot, "fetch" if draft else None))
    rid = cur.lastrowid
    if draft:
        SLICE_WAKE.set()
        return as_text({"request_id": rid, "printer": args["printer"], "url": url, "outcome": "fetching"})
    if engine == "orca":
        woke = wake_orca(rid) is not None
        return as_text({"request_id": rid, "printer": args["printer"], "url": url, "copies": copies,
                        "engine": engine, "outcome": "queued for Orca-Cad", "orca_cad_woken": woke})
    SLICE_WAKE.set()
    return as_text({"request_id": rid, "printer": args["printer"], "url": url, "copies": copies,
                    "engine": engine, "outcome": "queued for slicing"})


def store_model(rid, data):
    """Keep the model bytes of request rid (inside a DB transaction); bytes of closed requests go."""
    marks = ",".join("?" * len(OPEN_REQUESTS))
    DB.execute("DELETE FROM models WHERE request_id=?", (rid,))
    DB.execute("INSERT INTO models (request_id, data) VALUES (?,?)", (rid, data))
    DB.execute(f"DELETE FROM models WHERE request_id NOT IN (SELECT id FROM print_requests WHERE status IN ({marks}))",
               OPEN_REQUESTS)


def plates_note(facts):
    return f"This project has {facts['plates']} plates. Open it on the desktop." if facts["plates"] > 1 else None


def upload_model(alias, name, data, engine=None, choices=None, slot=None, draft=False):
    """A model file from the phone becomes a request; sliced on the server or by Orca-Cad.
    draft: stop at status draft with the model's facts; the app slices later with slice_request."""
    printer_id({"printer": alias})
    if not MODEL_NAME.fullmatch(name):
        raise ToolError("Formato non ammesso: serve un file STL, 3MF o STEP")
    if draft:
        facts = slicer.model_facts(name, data)  # several plates: the app asks which one
        with DB:
            cur = DB.execute("INSERT INTO print_requests (printer, url, created, status, source, file_name, file_size, "
                             "engine, facts) VALUES (?,'',?,'draft','upload',?,?,'server',?)",
                             (alias, time.time(), name, len(data), json.dumps(facts)))
            store_model(cur.lastrowid, data)
        return {"id": cur.lastrowid, "facts": facts}
    engine = engine or ("server" if slot and slicer.available() else "orca")
    if engine not in ("server", "orca"):
        raise ToolError("engine deve essere server o orca")
    if choices is not None and not isinstance(choices, dict):
        raise ToolError("choices deve essere un oggetto")
    if engine == "server":
        raw = read_status(alias)
        slot_filament(alias, raw, slot)
    marks = ",".join("?" * len(OPEN_REQUESTS))
    with DB:
        cur = DB.execute("INSERT INTO print_requests (printer, url, created, status, source, file_name, file_size, "
                         "engine, choices, slot) VALUES (?,'',?,'pending','upload',?,?,?,?,?)",
                         (alias, time.time(), name, len(data), engine,
                          json.dumps(choices) if choices is not None else None, slot))
        DB.execute("INSERT INTO models (request_id, data) VALUES (?,?)", (cur.lastrowid, data))
        # Keep the bytes only while their request is open.
        DB.execute(f"DELETE FROM models WHERE request_id NOT IN (SELECT id FROM print_requests WHERE status IN ({marks}))",
                   OPEN_REQUESTS)
    rid = cur.lastrowid
    if engine == "orca":
        woke = wake_orca(rid) is not None
        return {"request_id": rid, "file_name": name, "file_size": len(data), "engine": engine,
                "outcome": "queued for Orca-Cad", "orca_cad_woken": woke}
    SLICE_WAKE.set()
    return {"request_id": rid, "file_name": name, "file_size": len(data), "engine": engine,
            "outcome": "queued for slicing"}


def tool_reply_request(args):
    """the user answers a blocked request (e.g. the MakerWorld e-mail code): it goes back to pending with the reply."""
    printer_id(args)
    rid, text = args.get("request_id"), args.get("reply")
    if type(rid) is not int or not (isinstance(text, str) and 0 < len(text.strip()) <= 300):
        raise ToolError("Serve request_id intero e una risposta di massimo 300 caratteri")
    engine = "orca"
    with DB:
        n = DB.execute("UPDATE print_requests SET status='pending', reply=? WHERE id=? AND printer=? AND status='blocked'",
                       (text.strip(), rid, args["printer"])).rowcount
        if n:
            engine = DB.execute("SELECT COALESCE(engine, 'orca') FROM print_requests WHERE id=? AND printer=?",
                                (rid, args["printer"])).fetchone()[0]
    if not n:
        raise ToolError("Richiesta non trovata o non in attesa di risposta")
    if engine == "orca":
        wake_orca(rid)
    else:
        SLICE_WAKE.set()
    return as_text({"request_id": rid, "outcome": "reply sent to Orca-Cad"})


def tool_slice_request(args):
    """The app's step 4: a draft gets its printer, one spool per model colour and settings, then slices.
    A sliced job not started yet (ready) can be sliced again with other files, copies or settings."""
    printer_id(args)
    printer, rid = args["printer"], args.get("id")
    slots, choices, copies = args.get("slots"), args.get("choices") or {}, args.get("copies", 1)
    if type(rid) is not int:
        raise ToolError("This job is not waiting for slicing")
    row = DB.execute("SELECT facts FROM print_requests WHERE id=? AND status IN ('draft', 'ready')", (rid,)).fetchone()
    if row is None:
        raise ToolError("This job is not waiting for slicing")
    facts = json.loads(row[0]) if row[0] else {}
    colors = facts.get("colors") or []
    plate_no = args.get("plate_no")
    if facts.get("plates", 1) > 1:  # multi-plate project: the app's plate picker says which one (the user 2026-11-15)
        known = [p.get("n") for p in facts.get("plate_info") or []] or list(range(1, facts["plates"] + 1))
        if type(plate_no) is not int or plate_no not in known:
            raise ToolError("Pick a plate to print")
    if not (isinstance(slots, list) and len(slots) == max(1, len(colors)) and all(isinstance(s, str) for s in slots)):
        raise ToolError("Pick one spool per colour")
    if not isinstance(choices, dict):
        raise ToolError("Could not read the print settings")
    files = None
    if facts.get("files"):  # a bundle: which meshes to slice (missing = all of them)
        picked = choices.get("files")
        if picked is None:
            picked = list(range(len(facts["files"])))
        if (not isinstance(picked, list) or not picked
                or not all(type(i) is int and 0 <= i < len(facts["files"]) for i in picked)
                or len(set(picked)) != len(picked)):
            raise ToolError("Pick at least one file")
        files = picked
    if type(copies) is not int or not 1 <= copies <= 20:
        raise ToolError("Copies: from 1 to 20")
    raw = read_status(printer)
    for slot in set(slots) if not isinstance(link_of(printer), MoonrakerLink) else ():  # one spool may serve several colours
        slot_filament(printer, raw, slot)
    if args.get("standard") is True:  # Standard drops the custom settings, not Re-orient or "fresh"
        choices = {k: True for k in ("reorient", "fresh") if choices.get(k) is True}
    if files is None:
        choices.pop("files", None)
    else:
        choices["files"] = files
    choices.pop("plate_no", None)
    if facts.get("plates", 1) > 1:
        choices["plate_no"] = plate_no
    with DB:
        n = DB.execute("UPDATE print_requests SET status='pending', stage='slice', printer=?, slots=?, choices=?, "
                       "copies=?, file_id=NULL, plates=NULL WHERE id=? AND status IN ('draft', 'ready')",
                       (printer, json.dumps(slots), json.dumps(choices), copies, rid)).rowcount
    if not n:
        raise ToolError("This job is not waiting for slicing")
    SLICE_WAKE.set()
    return as_text({"request_id": rid, "printer": printer, "outcome": "queued for slicing"})


def update_request_status(rid, printer, status, note, plates):
    """Move a request to `status`; plates is the ordered file_id list (or None). Returns rowcount."""
    prev = REQUEST_MOVES[status]
    with DB:
        return DB.execute(f"UPDATE print_requests SET status=?, note=?, file_id=COALESCE(?, file_id), "
                          f"plates=COALESCE(?, plates), next_plate=COALESCE(?, next_plate) "
                          f"WHERE id=? AND printer=? AND status IN ({','.join('?' * len(prev))})",
                          (status, note, plates[0] if plates else None, json.dumps(plates) if plates else None,
                           0 if plates else None, rid, printer, *prev)).rowcount


def _plate_stem(file_name):
    r"""Sanitized file stem for plate names: [\w.-], at most 100 chars, fallback "model"."""
    return re.sub(r"[^\w.-]", "", Path(file_name or "").stem)[:100] or "model"


def _nozzle_diameter(raw):
    return (raw.get("nozzles") or [{}])[0].get("nozzle_diameter")


def _other_machines(printer):
    """Machine preset -> alias for every other printer, so a too-big reply can say where it fits."""
    out = {}
    for alias in CFG.get("printers", {}):
        if alias == printer:
            continue
        try:
            profile = load_profile(alias)
            try:
                nozzle = _nozzle_diameter(read_status(alias))
            except Exception:
                nozzle = None
            out[slicer.machine_preset(profile.get("model"), nozzle)] = alias
        except Exception:
            continue
    return out


def _fetch_link(rid, source):
    """(name, data, others) of a link request; makerworld.LoginNeeded bubbles up (-> blocked)."""
    url = DB.execute("SELECT url FROM print_requests WHERE id=?", (rid,)).fetchone()[0]
    if source in ("printables", "thingiverse"):
        return (*fetch.fetch_model(url), None)  # (name, data, others, profile)
    if source == "makerworld":
        return (*makerworld.fetch(DB, url), None)[:4]
    raise ToolError("Links are not supported yet")


def _fetch_request(rid, source):
    """stage='fetch': download the model, keep it, stop at draft with its facts. Never slices."""
    name, data, others, profile = _fetch_link(rid, source)
    facts = slicer.model_facts(name, data)  # several plates: the app asks which one
    if name.endswith(fetch.BUNDLE_EXT):  # several meshes from one page: the app asks which files
        facts["files"] = fetch.bundle_names(data)
    if profile:
        facts["profile"] = profile  # the MakerWorld print profile, shown in step 1
    note = ("Also on this page: " + ", ".join(others))[:300] if others else None
    with DB:
        if DB.execute("UPDATE print_requests SET status='draft', facts=?, file_name=?, file_size=?, note=? "
                      "WHERE id=? AND status='working'",
                      (json.dumps(facts), name, len(data), note, rid)).rowcount:
            store_model(rid, data)


def klipper_filaments(alias, raw, slots):
    """Klipper has no filament ids: the ACE slot picked (A1..A4), else the spool set in the app, else PLA."""
    trays = {t["slot"]: t for t in onboard.ams_trays(raw.get("ams"))}
    stored = stored_spool(alias)
    filaments = []
    for slot in slots:
        ace = (trays.get(slot) or {}).get("material")
        filaments.append({"filament_id": None if ace else stored.get("profile_id"),
                          "filament_type": ace or stored.get("material") or "PLA",
                          "filament_name": None if ace else catalog_name(stored.get("profile_id"))})
    return filaments


def _slice_request(rid, printer, source, file_name, choices_json, slots, copies, stage=None):
    raw = read_status(printer)
    klipper = isinstance(link_of(printer), MoonrakerLink)
    # No AMS (same test as printer_info's ams_present): every colour prints on the one loaded spool.
    if klipper:
        filaments = klipper_filaments(printer, raw, slots)
        feed = [254] * len(slots)
    else:
        if raw.get("ams"):
            spools = [slot_filament(printer, raw, slot) for slot in slots]
        else:
            spools = [slot_filament(printer, raw, slots[0])] * len(slots)
        filaments = [{"filament_id": s.get("profile_id"), "filament_type": s.get("material"),
                      "filament_name": catalog_name(s.get("profile_id"))} for s in spools]
        feed = [slot_index(s) for s in slots] if raw.get("ams") else [254] * len(slots)
    if source == "upload" or stage == "slice":  # a draft already holds its model
        m = DB.execute("SELECT data FROM models WHERE request_id=?", (rid,)).fetchone()
        if m is None:
            raise ToolError("Could not slice this model")
        name, data, others = file_name, m[0], ()
    else:
        name, data, others, _ = _fetch_link(rid, source)
    profile = load_profile(printer)
    machine = slicer.machine_preset(profile.get("model"), cfg_nozzle(printer) or _nozzle_diameter(raw))
    user = user_presets_for(printer, filaments)  # every printer: filaments and processes, machine stays the printer's
    if user and user.get("parent"):  # a bundle uploaded for this printer brings its own machine preset
        machine = user.pop("parent")
    choices = json.loads(choices_json) if choices_json else {}
    apply_default_process(printer, user, choices)
    process = choices.get("process")
    if process is not None and (not isinstance(process, str) or not user or process not in user["processes"]):
        choices.pop("process")  # a process profile that is not the user's is not a user choice
    if user and not (user.get("machine") or choices.get("process")
                     or any((f.get("filament_id") or "").startswith(USER_SPOOL) for f in filaments)):
        user = None  # none of the user's presets is in play: slice with the built-in ones
    if name.endswith(fetch.BUNDLE_EXT) and choices.get("files") is not None:
        name, data = fetch.bundle_pick(data, choices["files"])  # the meshes picked in step 1
    if "plate" not in choices and (mounted := plate_type(printer)):  # Klipper too (E3K, 2026-11-17)
        choices["plate"] = mounted  # Orca falls back to Cool Plate, which refuses PLA
    machines = _other_machines(printer)
    try:
        plates = slicer.slice(data, name, machine, filaments,
                              choices, bool(choices.get("reorient")), copies or 1,
                              other_machines=list(machines),
                              machine_overrides=None if user and user.get("machine") else cfg_machine_of(printer),
                              user_presets=user)
    except slicer.SliceError as exc:
        exc.other_machines = machines  # preset -> alias, for the "Fits on" note
        raise
    file_ids = []
    stem = _plate_stem(name)
    colors = [] if klipper else [s.get("color") for s in spools]  # headless Orca draws no plate image: Pygmy does, in the spools' colours
    for i, plate in enumerate(plates):
        plate_name = f"{stem}_{i + 1}.gcode.3mf" if len(plates) > 1 else f"{stem}.gcode.3mf"
        file_ids.append(upload_file(plate_name, preview.with_previews(plate["gcode_3mf"], colors))["file_id"])
        # The spools picked in step 2, one per model colour: the start sends exactly these.
        DB.execute("UPDATE uploads SET feed=? WHERE file_id=?", (json.dumps(feed), file_ids[-1]))
    note = ("Also on this page: " + ", ".join(others))[:300] if others else None
    if not update_request_status(rid, printer, "ready", note, file_ids):
        # Cancelled while slicing: drop the files nobody will print.
        if file_ids:
            marks = ",".join("?" * len(file_ids))
            with DB:
                DB.execute(f"DELETE FROM uploads WHERE file_id IN ({marks})", file_ids)
            for fid in file_ids:
                library.remove(fid)
        raise ToolError("Could not slice this model")


def slice_once():
    """Slice the oldest pending server-engine request: pending -> working -> ready/failed. True if one ran."""
    row = DB.execute("SELECT id, printer, source, file_name, choices, slot, copies, stage, slots FROM print_requests "
                     "WHERE status='pending' AND engine='server' ORDER BY id LIMIT 1").fetchone()
    if row is None:
        return False
    rid, printer, source, file_name, choices_json, slot, copies, stage, slots = row
    if not DB.execute("UPDATE print_requests SET status='working' WHERE id=? AND status='pending'", (rid,)).rowcount:
        return True
    try:
        if stage == "fetch":
            _fetch_request(rid, source)
            return True
        if slots:
            slots = json.loads(slots)
        else:
            slots = [slot]
        _slice_request(rid, printer, source, file_name, choices_json, slots, copies, stage)
    except makerworld.LoginNeeded:
        with DB:
            DB.execute("UPDATE print_requests SET status='blocked', note='MakerWorld login needed' "
                       "WHERE id=? AND status='working'", (rid,))
        return True
    except Exception as err:  # never the secret: note and log carry no URL or token
        note = "Could not slice this model"
        if isinstance(err, (ToolError, slicer.SliceError, fetch.FetchError, makerworld.LoginFailed)):
            note = str(err) or note
        if isinstance(err, slicer.SliceError) and err.fits_on:
            try:
                names, _ = app_names_models()
            except Exception:
                names = {}
            machines = getattr(err, "other_machines", {})
            note = (note + ". Fits on: " + ", ".join(
                names.get(machines.get(m, m), machines.get(m, m)) for m in err.fits_on))[:300]
        traceback.print_exc()
        print(f"slice {rid}: {note}", file=sys.stderr, flush=True)
        with DB:
            DB.execute("UPDATE print_requests SET status='failed', note=? WHERE id=? AND status='working'", (note, rid))
    return True


def reset_stuck_slices():
    """Requests left 'working' by a restart go back to pending, or nothing slices them again."""
    with DB:
        DB.execute("UPDATE print_requests SET status='pending' WHERE status='working' AND engine='server'")


def slice_worker():
    reset_stuck_slices()
    while True:
        try:
            while slice_once():
                pass
        except Exception as err:
            print(f"slice worker: {err}", file=sys.stderr, flush=True)
        SLICE_WAKE.wait(10)
        SLICE_WAKE.clear()


def tool_print_requests(args):
    printer_id(args)
    printer = args["printer"]
    if args.get("done") is not None:  # Older Orca-Cad call: done=<id>.
        args = {**args, "id": args["done"], "status": "done"}
    status = args.get("status")
    if status is not None:
        rid, note = args.get("id"), args.get("note")
        if type(rid) is not int or status not in REQUEST_MOVES:
            raise ToolError("Serve id intero e status tra " + ", ".join(REQUEST_MOVES))
        if note is not None and not (isinstance(note, str) and len(note) <= 300):
            raise ToolError("note: testo di massimo 300 caratteri")
        plates = None
        if status == "ready":  # One file per plate, in print order.
            plates = args.get("file_ids", [args["file_id"]] if args.get("file_id") is not None else None)
            if not (isinstance(plates, list) and 1 <= len(plates) <= 20 and all(type(f) is int for f in plates)
                    and all(DB.execute("SELECT 1 FROM uploads WHERE file_id=?", (f,)).fetchone() for f in plates)):
                raise ToolError("ready richiede file_id o file_ids: da 1 a 20 file caricati su BFF, in ordine di piatto")
        if status == "started":  # Multi-plate request: the next plate becomes the one on offer.
            row = DB.execute("SELECT plates, next_plate FROM print_requests WHERE id=? AND printer=? AND status='ready'",
                             (rid, printer)).fetchone()
            order = json.loads(row[0]) if row and row[0] else []
            if row and row[1] + 1 < len(order):
                with DB:
                    DB.execute("UPDATE print_requests SET next_plate=?, file_id=? WHERE id=?", (row[1] + 1, order[row[1] + 1], rid))
                status = None
    if status is not None:
        n = update_request_status(rid, printer, status, note, plates)
        if not n:
            row = DB.execute("SELECT status FROM print_requests WHERE id=? AND printer=?", (rid, printer)).fetchone()
            if row is None:
                raise ToolError("Richiesta non trovata")
            if row[0] == "cancelled":
                raise ToolError("Richiesta annullata dall'utente: fermati e non caricare nulla")
            raise ToolError(f"Richiesta in stato {row[0]}, non puo' passare a {status}")
    marks = ",".join("?" * len(OPEN_REQUESTS))
    keys = ("id", "printer", "url", "created", "status", "file_id", "note", "plates", "plate_index",
            "source", "file_name", "file_size", "reply", "copies", "facts")
    rows = DB.execute("SELECT id, printer, url, created, status, file_id, note, plates, COALESCE(next_plate, 0), "
                      "COALESCE(source, 'makerworld'), file_name, file_size, reply, COALESCE(copies, 1), facts "
                      f"FROM print_requests WHERE printer=? AND status IN ({marks}) ORDER BY id", (printer, *OPEN_REQUESTS)).fetchall()
    out = [dict(zip(keys, row)) for row in rows]
    for r in out:
        r["plates"] = json.loads(r["plates"]) if r["plates"] else ([r["file_id"]] if r["file_id"] is not None else [])
        facts = r.pop("facts")
        if r["status"] == "draft":
            r["facts"] = json.loads(facts) if facts else {"plates": 1, "colors": []}
    return as_text({"requests": out})


def tool_prepare_gcode(args):
    pid = printer_id(args)
    alias = args["printer"]
    gcode = args.get("gcode")
    if not isinstance(gcode, str) or not gcode.strip():
        raise ToolError("gcode deve essere una stringa non vuota")
    if len(gcode) > 2000:
        raise ToolError("gcode troppo lungo: massimo 2000 caratteri")
    with COMMAND_LOCK:
        direct_ready("prepare_gcode", alias, parked)
        summary = f"Invio del G-code grezzo a {alias}: '{gcode}'. BFF non controlla i limiti del G-code grezzo."
        jpeg, photo = plate_shot(pid)
        return [image_content(jpeg)] + issue_token(alias, "gcode", {"gcode": gcode}, summary, **photo)


def tool_confirm_gcode(args):
    def send(params):
        text = params["gcode"]
        return link_of(args["printer"]).gcode(text), text
    return run_direct("confirm_gcode", args, "gcode", None, parked, send,
                      after_sent=lambda alias: AXES.pop(alias, None))


def tool_recheck(args):
    printer_id(args)
    alias = args["printer"]
    with COMMAND_LOCK:
        lock = DB.execute("SELECT key FROM locks WHERE printer=?", (alias,)).fetchone()
        if lock is None:
            return as_text({"printer": alias, "locked": False})
        raw = read_status(alias)
        if raw.get("connected") is not True:
            raise ToolError(f"{alias} ancora disconnessa: blocco mantenuto")
        action, params = DB.execute("SELECT action, params FROM commands WHERE key=?", lock).fetchone()
        params = json.loads(params) if params else {}
        outcome = "confirmed" if observed(action, params, raw) else "not_applied"
        result = finish(lock[0], alias, action, params, outcome, f"stato riletto: {raw.get('state')}")
        return as_text({**result, "locked": False})


def schema(**extra):
    props = {"printer": {"type": "string", "description": "Alias della stampante, per esempio a1"}, **extra}
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


KEY = {"idempotency_key": {"type": "string", "description": "Chiave nuova per ogni comando (8-128 caratteri); stessa chiave, stesso esito senza reinvio"}}
LIGHT_SWITCH = {"type": "object", "properties": {
    "printer": {"type": "string", "description": "Alias della stampante, per esempio a1"},
    "on": {"type": "boolean", "description": "true accende, false spegne"},
    "light": {"type": "string", "description": "Nome della luce da status.lights; ometti per tutte"}},
    "required": ["printer", "on"], "additionalProperties": False}
TOKEN = {"token": {"type": "string", "description": "confirm_token restituito dal prepare"}}
TEMP = {"heater": {"type": "string", "enum": ["nozzle", "bed"]}, "target": {"type": "integer", "description": "Gradi Celsius, 0 spegne"}}
FILE_ID = {"file_id": {"type": "integer", "description": "file_id restituito dallo script bff-upload.sh"}}
CONFIRM_RULE = " Mostra il riepilogo all'utente e chiama il confirm solo dopo il suo si' esplicito."
HOME_AXES = {"axes": {"type": "string", "enum": ["xy", "all"], "description": "xy = G28 X (Z fermo), all = G28 (Z tasta il piatto)"}}
HOME_AXES_KLIPPER = {"axes": {"type": "string", "enum": ["xy", "all"], "description": "xy = G28 X Y (Z fermo), all = G28 (Z tasta il piatto)"}}
MOVE_AXES = {axis: {"type": "number", "description": f"{axis.upper()} assoluta in mm, ometti per lasciarla ferma"} for axis in "xyz"}


def optional_props(required, **extra):
    result = schema(**extra)
    result["required"] = ["printer", *required]
    return result


def optional_axes(**extra):
    result = schema(**MOVE_AXES, **extra)
    result["required"] = ["printer", *extra]
    return result


TOOLS = {
    "status": (tool_status, "Stato attuale della stampante: progresso, temperature, errori HMS. attention elenca i messaggi attivi: handling persona = serve un intervento manuale da chiedere all'utente; sicurezza = non rispondere mai da solo.", schema(), False),
    "list_printers": (tool_list_printers, "Elenco delle stampanti gestite da BFF: alias, nome e modello. Nessun access code.", {"type": "object", "properties": {}, "required": []}, False),
    "orca_cloud": (tool_orca_cloud, "Account Orca Cloud, uno solo per tutte le stampanti: stato e accesso con Google, Apple, GitHub o email. Fornisce filamenti e processi; la macchina resta quella della stampante. Azioni: status, login, login_paste, login_done, logout.", {"type": "object", "properties": {"printer": {"type": "string", "description": "Ignorato: l'account e' globale"}, "action": {"type": "string", "enum": ["status", "login", "login_paste", "login_done", "logout"]}, "provider": {"type": "string", "enum": ["google", "apple", "github", "email"]}, "email": {"type": "string"}, "password": {"type": "string"}, "url": {"type": "string", "description": "Indirizzo incollato dall'utente dopo l'accesso nel browser"}}, "required": [], "additionalProperties": False}, True),
    "printer_info": (tool_printer_info, "Dati per lo slicing: modello, ugello, filamento caricato con grammi rimasti, stampanti disponibili, se e' libera. plate = piatto visto ora dalla camera: clear, occupied o unknown con il motivo. null = dato non noto, non indovinarlo.", schema(), False),
    "camera_snapshot": (tool_camera_snapshot, "Foto della camera. Da usare solo se l'utente la chiede esplicitamente.", schema(), False),
    "plate_photo": (tool_plate_photo, "Foto del piatto per decidere se e' libero. Segui le regole nella risposta.", schema(), False),
    "hms_diagnose": (tool_hms_diagnose, "Errori HMS attivi con codice, modulo, gravita', descrizione, azioni possibili e handling (persona o sicurezza).", schema(), False),
    "plate_cleared": (tool_plate_cleared, "Segna il piatto come liberato dopo una stampa finita: solo quando the user lo dice o lo vede dalla foto.", schema(), True),
    "set_light": (tool_set_light, "Accende o spegne la luce della camera.", LIGHT_SWITCH, True),
    "pause": (simple_command("pause"), "Mette in pausa la stampa e verifica lo stato.", schema(**KEY), True),
    "resume": (simple_command("resume"), "Riprende una stampa in pausa e verifica lo stato.", schema(**KEY), True),
    "prepare_stop": (tool_prepare_stop, "Prepara lo stop, irreversibile." + CONFIRM_RULE, schema(), True),
    "confirm_stop": (tool_confirm_stop, "Esegue lo stop preparato, dopo il si' dell'utente.", schema(**TOKEN, **KEY), True),
    "set_temperature": (tool_set_temperature, "Imposta una temperatura entro la soglia del profilo.", schema(**TEMP, **KEY), True),
    "prepare_temperature": (tool_prepare_temperature, "Prepara una temperatura sopra soglia." + CONFIRM_RULE, schema(**TEMP), True),
    "confirm_temperature": (tool_confirm_temperature, "Applica la temperatura preparata, dopo il si' dell'utente.", schema(**TOKEN, **KEY), True),
    "prepare_print_start": (tool_prepare_print_start, "Controlla stampante e file e scatta la foto del piatto. Mostra foto, photo_url e riepilogo completo, avvisi sulla bobina compresi, all'utente e chiama il confirm solo dopo il suo si' esplicito.", schema(**FILE_ID), True),
    "confirm_print_start": (tool_confirm_print_start, "Avvia la stampa preparata, dopo il si' dell'utente sulla foto del piatto.", schema(**TOKEN, **KEY), True),
    "prepare_home": (tool_prepare_home, "Prepara l'azzeramento assi con la foto del piatto: giudica la foto prima di confermare. Mai durante una stampa." + CONFIRM_RULE, schema(**HOME_AXES), True),
    "confirm_home": (tool_confirm_home, "Esegue l'azzeramento preparato." + CONFIRM_RULE, schema(**TOKEN, **KEY), True),
    "move": (tool_move, f"Sposta gli assi in mm assoluti, solo dopo azzeramento completo. Z scende libera solo nella posa sicura X-48 Y256, altrimenti prepare_move e confirm_move.", optional_axes(**KEY), True),
    "prepare_move": (tool_prepare_move, "Prepara un movimento assi con la foto del piatto, anche con discesa di Z." + CONFIRM_RULE, optional_axes(), True),
    "confirm_move": (tool_confirm_move, "Esegue il movimento preparato." + CONFIRM_RULE, schema(**TOKEN, **KEY), True),
    "jog": (tool_jog, "Movimento relativo in mm su una stampante Klipper, un asse alla volta, al massimo 100 mm. Solo con l'asse azzerato.", schema(axis={"type": "string", "enum": ["x", "y", "z"]}, distance={"type": "number", "description": "Millimetri, da -100 a 100: positivo avanti/su, negativo indietro/giu'"}, **KEY), True),
    "home": (tool_home, "Azzeramento diretto su una stampante Klipper, senza foto: 'G28 X Y' o 'G28'. Mai durante una stampa.", schema(**HOME_AXES_KLIPPER, **KEY), True),
    "motors_off": (tool_motors_off, "Rilascia i motori di una stampante Klipper (M84): la posizione va persa e serve un nuovo Home. Mai durante una stampa.", schema(**KEY), True),
    "recheck": (tool_recheck, "Rilegge lo stato dopo un esito sconosciuto e sblocca la stampante. Non reinvia nulla; un avvio rimasto in coda viene annullato.", schema(), True),
    "set_print_speed": (tool_set_print_speed, "Imposta la velocita' di stampa da 1 a 4. Solo durante una stampa o in pausa.", schema(level={"type": "integer", "description": "Velocita' 1-4"}, **KEY), True),
    "set_fan": (tool_set_fan, "Imposta la velocita' di una ventola da 0 a 100%.", schema(fan={"type": "string", "enum": ["part", "aux", "chamber"]}, percent={"type": "integer", "description": "0-100"}, **KEY), True),
    "set_detector": (tool_set_detector, "Attiva o disattiva un sensore xcam. Sensibilita' opzionale per l'arresto.", schema(module={"type": "string", "enum": list(DETECTOR_MODULES)}, enabled={"type": "boolean"}, sensitivity={"type": "string", "enum": list(SENSITIVITIES)}, **KEY), True),
    "set_print_option": (tool_set_print_option, "Attiva o disattiva un'opzione di stampa. Per bff_flow_cali indica mode: auto, on o off (calibrazione del flusso all'avvio).", optional_props(("option", "idempotency_key"), option={"type": "string", "description": f"{', '.join([*PRINT_OPTIONS, 'bff_spaghetti', 'bff_spaghetti_pause', 'bff_flow_cali', 'bff_default_process'])}, or filament_sensor:<name> on Klipper"}, enabled={"type": "boolean"}, process={"type": "string", "description": "Solo per bff_default_process: il profilo di stampa predefinito della stampante, vuoto = quello integrato"}, mode={"type": "string", "enum": list(FLOW_CALI_MODES), "description": "Solo per bff_flow_cali"}, **KEY), True),
    "clear_errors": (tool_clear_errors, "Pulisce gli errori mostrati dalla stampante.", schema(**KEY), True),
    "dismiss_spaghetti": (tool_dismiss_spaghetti, "Falso allarme spaghetti: niente avvisi fino alla fine di questa stampa. Solo su richiesta dell'utente.", schema(), True),
    "prepare_skip_objects": (tool_prepare_skip_objects, "Prepara il salto di oggetti in stampa, irreversibile. Solo durante stampa o pausa." + CONFIRM_RULE, schema(obj_ids={"type": "array", "items": {"type": "integer"}, "description": "Id degli oggetti da saltare"}), True),
    "confirm_skip_objects": (tool_confirm_skip_objects, "Esegue il salto di oggetti preparato, dopo il si' dell'utente." + CONFIRM_RULE, schema(**TOKEN, **KEY), True),
    "prepare_calibration": (tool_prepare_calibration, "Prepara una calibrazione (livellamento, vibrazioni, rumore motori): gli assi si muovono e il piatto viene sondato." + CONFIRM_RULE, schema(items={"type": "array", "items": {"type": "string", "enum": list(CALIBRATION_BITS)}}), True),
    "confirm_calibration": (tool_confirm_calibration, "Esegue la calibrazione preparata, dopo il si' dell'utente." + CONFIRM_RULE, schema(**TOKEN, **KEY), True),
    "prepare_load_filament": (tool_prepare_load_filament, "Prepara il carico della bobina esterna: l'ugello si scalda." + CONFIRM_RULE, schema(), True),
    "confirm_load_filament": (tool_confirm_load_filament, "Esegue il carico della bobina esterna preparato, dopo il si' dell'utente." + CONFIRM_RULE, schema(**TOKEN, **KEY), True),
    "prepare_unload_filament": (tool_prepare_unload_filament, "Prepara lo scarico della bobina esterna: l'ugello si scalda." + CONFIRM_RULE, schema(), True),
    "confirm_unload_filament": (tool_confirm_unload_filament, "Esegue lo scarico della bobina esterna preparato, dopo il si' dell'utente." + CONFIRM_RULE, schema(**TOKEN, **KEY), True),
    "set_nozzle": (tool_set_nozzle, "Dichiara l'ugello montato (diametro e tipo), scritto nella stampante. Solo a stampante ferma.", schema(diameter={"type": "number", "enum": list(NOZZLE_DIAMETERS)}, type={"type": "string", "enum": list(NOZZLE_TYPES)}, **KEY), True),
    "set_spool": (tool_set_spool, "Dichiara un filamento (profilo filamento e colore RRGGBBAA), scritto nella stampante: la bobina esterna (solo a stampante ferma) o lo slot AMS indicato (anche durante la stampa).", optional_props(("profile_id", "color"), profile_id={"type": "string", "description": "Id da printer_info, choices.spool_profiles"}, color={"type": "string", "description": "RRGGBBAA esadecimale maiuscolo"}, slot={"type": "string", "pattern": "^[A-D][1-4]$", "description": "Slot AMS da modificare, per esempio A2. Senza: la bobina esterna."}, **KEY), True),
    "read_filament": (tool_read_filament, "Rilegge il tag RFID di uno slot AMS (la bobina gira). Mai mentre quell'AMS alimenta la stampa.", schema(slot={"type": "string", "pattern": "^[A-D][1-4]$", "description": "Slot AMS, per esempio A1"}, **KEY), True),
    "ams_dry": (tool_ams_dry, "Asciuga un AMS 2 Pro o AMS HT (asciugatura con riscaldatore). Mai durante una stampa; l'AMS HT arriva a 85 °C, gli altri a 65 °C.", optional_props(("unit", "action"), unit={"type": "string", "description": "Lettera AMS A-D oppure l'id riportato dalla stampante"}, action={"type": "string", "enum": ["start", "stop"]}, temp={"type": "integer", "description": "Gradi Celsius, 45-65 oppure 45-85 per AMS HT"}, hours={"type": "integer", "description": "Ore di asciugatura, 1-24"}, **KEY), True),
    "set_plate": (tool_set_plate, "Dichiara il piatto montato (nomi OrcaSlicer). La A1 non lo memorizza: lo tiene BFF e lo riporta in printer_info.", schema(plate={"type": "string", "enum": list(PLATE_TYPES)}), True),
    "delete_file": (tool_delete_file, "Elimina un file pronto da stampare.", schema(file_id={"type": "integer"}), True),
    "rename_file": (tool_rename_file, "Rinomina un file pronto da stampare.", schema(file_id={"type": "integer"}, name={"type": "string", "description": "Nuovo nome senza estensione"}), True),
    "ready_files": (tool_ready_files, "File affettati nella cartella di BFF, i piu recenti prima, pronti per prepare_print_start.", schema(), False),
    "queue_add": (tool_queue_add, "Mette un file in coda per questa stampante. own=true: la voce possiede il file e quando viene rimossa senza altre voci il file viene eliminato.", optional_props(("file_id",), own={"type": "boolean"}), True),
    "queue_remove": (tool_queue_remove, "Rimuove una voce dalla coda. Se own e nessun altro uso del file, elimina anche il file.", schema(id={"type": "integer"}), True),
    "queue_list": (tool_queue_list, "Coda di questa stampante, la piu' vecchia prima.", schema(), False),
    "request_print": (tool_request_print, "Mette in coda un link MakerWorld per Orca-Cad, che lo scarica e lo affetta. draft=true: BFF scarica soltanto e la richiesta si ferma in draft con facts (piatti, colori) finche' l'app non manda slice_request.", optional_props(("url", "copies"), url={"type": "string", "description": "https://makerworld.com/..."}, copies={"type": "integer", "minimum": 1, "maximum": 20, "description": "How many copies of the model Orca-Cad should put on the plate (default 1)"}, draft={"type": "boolean"}), True),
    "slice_request": (tool_slice_request, "Affetta una richiesta in draft: stampante (anche diversa), uno slot per colore del modello, impostazioni, copie. standard=true ignora choices.", optional_props(("id", "slots"), id={"type": "integer"}, slots={"type": "array", "items": {"type": "string"}, "description": "Uno slot per colore, nell'ordine di facts.colors"}, choices={"type": "object"}, copies={"type": "integer", "minimum": 1, "maximum": 20}, standard={"type": "boolean"}), True),
    "print_requests": (tool_print_requests, "Richieste MakerWorld aperte (pending, working, ready, failed). Con id e status la richiesta avanza: working quando Orca-Cad la prende, ready con file_id quando il file e' su BFF, failed con note se non riesce. Una richiesta annullata dall'utente da' errore: fermati.", {"type": "object", "properties": {"printer": {"type": "string", "description": "Alias della stampante, per esempio a1"}, "id": {"type": "integer"}, "status": {"type": "string", "enum": list(REQUEST_MOVES)}, "file_id": {"type": "integer"}, "file_ids": {"type": "array", "items": {"type": "integer"}, "description": "Un file per piatto, in ordine di stampa"}, "note": {"type": "string"}, "done": {"type": "integer", "description": "Vecchia forma di status=done"}}, "required": ["printer"], "additionalProperties": False}, False),
    "reply_request": (tool_reply_request, "Risposta dell'utente a una richiesta blocked (per esempio il codice email MakerWorld): la richiesta torna pending con il campo reply per Orca-Cad.", schema(request_id={"type": "integer"}, reply={"type": "string"}), True),
    "cancel_request": (tool_cancel_request, "Annulla una richiesta MakerWorld aperta.", schema(request_id={"type": "integer"}), True),
    "prepare_gcode": (tool_prepare_gcode, "Prepara l'invio di G-code grezzo. BFF non controlla i limiti del G-code grezzo." + CONFIRM_RULE, schema(gcode={"type": "string", "description": "G-code da inviare, max 2000 caratteri"}), True),
    "confirm_gcode": (tool_confirm_gcode, "Invia il G-code preparato, dopo il si' dell'utente." + CONFIRM_RULE, schema(**TOKEN, **KEY), True),
}


def visible_tools():
    return {name: tool for name, tool in TOOLS.items() if CFG.get("control") or not tool[3]}


def rpc_result(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def rpc_error(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def call_tool(req_id, params):
    entry = visible_tools().get(params.get("name"))
    if entry is None and getattr(APP_CALL, "on", False):
        entry = APP_TOOLS.get(params.get("name"))
    if entry is None:
        return rpc_error(req_id, -32602, "Tool sconosciuto")
    args = params.get("arguments")
    required = entry[2].get("required", ["printer"])  # APP_TOOLS declare no schema but do take printer
    if not isinstance(args, dict) or ("printer" in required and not isinstance(args.get("printer"), str)):
        return rpc_error(req_id, -32602, "Argomento printer mancante")
    try:
        return rpc_result(req_id, {"content": entry[0](args), "isError": False})
    except ToolError as err:
        return rpc_result(req_id, {"content": as_text({"errore": str(err)}), "isError": True})


def dispatch(msg):
    """Return the JSON-RPC response, or None for a notification."""
    if not isinstance(msg, dict) or not isinstance(msg.get("method"), str):
        return rpc_error(None, -32600, "Richiesta non valida")
    if "id" not in msg:
        return None
    req_id, method = msg["id"], msg["method"]
    params = msg.get("params")
    params = params if isinstance(params, dict) else {}
    if method == "initialize":
        return rpc_result(req_id, {
            "protocolVersion": params.get("protocolVersion", DEFAULT_PROTOCOL),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "bff-mcp", "version": "0.3.0"},
        })
    if method == "ping":
        return rpc_result(req_id, {})
    if method == "tools/list":
        tools = [{"name": n, "description": t[1], "inputSchema": t[2]} for n, t in visible_tools().items()]
        return rpc_result(req_id, {"tools": tools})
    if method == "tools/call":
        return call_tool(req_id, params)
    return rpc_error(req_id, -32601, "Metodo non supportato")


def parse_model_put(path):
    """Split /model/<printer>/<file name>[?engine=&slot=&choices=]; no query keeps the plain upload (?draft=1: do_PUT)."""
    raw_path, _, query = path.partition("?")
    alias, _, name = urllib.parse.unquote(raw_path.removeprefix("/model/")).partition("/")
    q = urllib.parse.parse_qs(query)
    engine = (q.get("engine") or [None])[0]
    slot = (q.get("slot") or [None])[0]
    choices = (q.get("choices") or [None])[0]
    if choices is not None:
        try:
            choices = json.loads(choices)
        except ValueError:
            raise ToolError("choices deve essere un oggetto") from None
        if not isinstance(choices, dict):
            raise ToolError("choices deve essere un oggetto")
    return alias, name, engine, slot, choices


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 60

    def log_message(self, *args):
        pass

    def reply(self, code, body=None):
        data = b"" if body is None else json.dumps(body).encode()
        self.send_response(code)
        if data:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def pair(self):
        """No bearer: a new device trades the 6-digit code from Add a device for its key."""
        self.close_connection = True
        try:
            length = int(self.headers.get("Content-Length", ""))
            body = json.loads(self.rfile.read(length)) if 0 <= length <= 1024 else {}
        except (ValueError, RecursionError):
            body = {}
        key = devices.redeem(body.get("code") if isinstance(body, dict) else None)
        return self.reply(200, {"key": key}) if key else self.reply(400, {"errore": "code not valid"})

    def machine(self):
        """Name of the machine owning the bearer token, or None."""
        header = self.headers.get("Authorization", "").encode()
        found = None
        for name, token in CFG["tokens"].items():
            if hmac.compare_digest(header, b"Bearer " + token.encode()):
                found = name
        if found is None and DB_PATH and header.startswith(b"Bearer "):
            name = devices.cached_check(header[7:].decode(errors="replace"))
            if name is not devices.UNKNOWN:
                found = name
            else:
                try:
                    db = sqlite3.connect(DB_PATH)
                    try:
                        found = devices.check(db, header[7:].decode(errors="replace"))
                    finally:
                        db.close()
                except sqlite3.Error:
                    found = None
        return found

    def refuse(self):
        self.close_connection = True
        self.reply(401 if self.machine() is None else 405)

    do_DELETE = refuse

    def send_bytes(self, ctype, data):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in STATIC:  # PWA files: no data in them, no token
            self.close_connection = True
            ctype, name = STATIC[self.path]
            return self.send_bytes(ctype, APP_PAGE.with_name(name).read_bytes())
        if self.path.split("?")[0] == "/":
            # The bare address typed in a browser opens the app (a browser keeps the #k=... part through a redirect).
            self.close_connection = True
            self.send_response(302)
            self.send_header("Location", "/app")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path.split("?")[0] == "/app":
            # Static page, no data in it: everything it shows needs the bearer token.
            self.close_connection = True
            return self.send_bytes("text/html; charset=utf-8", APP_PAGE.read_bytes())
        if self.path.startswith("/camera/"):
            return self.camera_path(self.path.removeprefix("/camera/"))
        if self.path.startswith("/stream/"):
            return self.stream(*self.path.removeprefix("/stream/").partition("/")[::2])
        if self.path.startswith("/requests/") and self.path.endswith("/file"):
            return self.model_file(self.path.removeprefix("/requests/").removesuffix("/file"))
        if not self.path.startswith("/photo/"):
            return self.refuse()
        # No bearer here: a phone opens this link, so the unguessable short-lived id is the credential.
        self.close_connection = True
        photo = PHOTOS.get(self.path.removeprefix("/photo/"))
        if photo is None or photo[0] <= time.time():
            return self.reply(404)
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(photo[1])))
        self.end_headers()
        self.wfile.write(photo[1])

    def stream(self, alias, rest):
        """Camera for Obico and Home Assistant: /stream/<printer>/<token> is MJPEG, .../<token>.jpg one frame.

        Those clients cannot send the bearer, so a separate long token (PYGMY_STREAM_TOKEN) is the credential.
        """
        self.close_connection = True
        token, still = rest.removesuffix(".jpg"), rest.endswith(".jpg")
        want = CFG.get("stream_token", "")
        if len(want) < 16 or not hmac.compare_digest(token.encode(), want.encode()):
            return self.reply(401)
        if alias not in CFG["printers"]:
            return self.reply(404)
        try:
            feed = feed_of(alias)
            jpeg = feed.latest()
        except (ToolError, camera.CameraError):
            return self.reply(502)
        if still:
            return self.send_bytes("image/jpeg", jpeg)
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        last = None
        try:
            while True:
                if jpeg is not last:
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                     + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")
                    self.wfile.flush()
                    last = jpeg
                time.sleep(0.5)
                jpeg = feed.latest(max_age_s=1.0)
        except (OSError, camera.CameraError):
            pass  # Client gone or camera lost: the viewer reconnects.

    def model_file(self, rid):
        """The phone's model for Orca-Cad, bearer only."""
        self.close_connection = True
        if self.machine() is None:
            return self.reply(401)
        row = DB.execute("SELECT r.file_name, m.data FROM models m JOIN print_requests r ON r.id = m.request_id "
                         "WHERE m.request_id=?", (int(rid),)).fetchone() if rid.isdigit() else None
        if row is None:
            return self.reply(404)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", f'attachment; filename="{row[0]}"')
        self.send_header("Content-Length", str(len(row[1])))
        self.end_headers()
        self.wfile.write(row[1])

    def camera_path(self, rest):
        """The app's one-frame fetch (bearer)."""
        path, _, _ = rest.partition("?")
        return self.camera_frame(path)

    def camera_frame(self, alias):
        """One JPEG for the phone page; the light is left as it is."""
        self.close_connection = True
        if self.machine() is None:
            return self.reply(401)
        if alias not in CFG["printers"]:
            return self.reply(404)
        try:
            jpeg = capture_jpeg(CFG["printers"][alias])
        except ToolError:
            return self.reply(502)
        self.send_bytes("image/jpeg", jpeg)

    def do_PUT(self):
        self.close_connection = True
        machine = self.machine()
        if machine is None:
            return self.reply(401)
        model = self.path.startswith("/model/")
        if not CFG.get("control") or not (model or self.path.startswith("/upload/")):
            return self.reply(404)
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            length = -1
        if not 0 < length <= MAX_UPLOAD:
            return self.reply(413)
        print(f"{machine} upload", file=sys.stderr, flush=True)
        try:
            if model:  # /model/<printer>/<file name>[?engine=&slot=&choices=], from the phone.
                alias, name, engine, slot, choices = parse_model_put(self.path)
                draft = urllib.parse.parse_qs(self.path.partition("?")[2]).get("draft") == ["1"]
                result = upload_model(alias, name, self.rfile.read(length), engine=engine, slot=slot, choices=choices,
                                      draft=draft)
            else:
                result = upload_file(self.path.removeprefix("/upload/"), self.rfile.read(length))
        except ToolError as err:
            return self.reply(400, {"errore": str(err)})
        self.reply(200, result)

    def do_POST(self):
        if self.path == "/pair":
            return self.pair()
        machine = self.machine()
        if machine is None:
            self.close_connection = True
            return self.reply(401)
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY:
            self.close_connection = True
            return self.reply(413)
        try:
            msg = json.loads(self.rfile.read(length))
        except ValueError:
            return self.reply(400, rpc_error(None, -32700, "JSON non valido"))
        if isinstance(msg, dict) and msg.get("method") == "tools/call":
            name = (msg.get("params") or {}).get("name") if isinstance(msg.get("params"), dict) else None
            if name != "status":  # The phone page polls status every 2 s.
                print(f"{machine} tools/call {name if name in TOOLS or name in APP_TOOLS else '?'}", file=sys.stderr, flush=True)
        APP_CALL.on = self.headers.get("X-BFF-App") == "1"
        REQUEST.host = self.headers.get("Host", "")
        REQUEST.machine = machine
        response = dispatch(msg)
        self.reply(202) if response is None else self.reply(200, response)


def ha_action(alias, action, value, key):
    """The only commands Home Assistant may send (spec 8.6): never a print start."""
    if action == "light":
        return set_light(CFG["printers"][alias], value)
    if action.startswith("light_"):
        slug = action[len("light_"):]
        light = next((l for l in read_status(alias).get("lights") or []
                      if ha_mqtt.light_slug(l.get("name")) == slug), None)
        if light is None:
            raise ToolError(f"Unknown light '{slug}'")
        return set_light(CFG["printers"][alias], value, light["name"])
    if action == "plate_cleared":
        return set_plate_pending(alias, False)
    if action in ("pause", "resume", "stop"):
        require_support(alias, action)
        if action == "stop":
            AXES.pop(alias, None)
        return command_reply(execute(alias, action, {}, key))
    if action == "speed":
        return tool_set_print_speed({"printer": alias, "level": value, "idempotency_key": key})
    if action in ("fan_part", "fan_aux", "fan_chamber"):
        return tool_set_fan({"printer": alias, "fan": action[4:], "percent": value, "idempotency_key": key})
    if action.startswith("opt_") and action[4:] in ("auto_recovery", "sound_enable",
                                                    "filament_tangle_detect", "bff_spaghetti",
                                                    "bff_spaghetti_pause"):
        return tool_set_print_option({"printer": alias, "option": action[4:], "enabled": value,
                                      "idempotency_key": key})
    if action == "clear_errors":
        return tool_clear_errors({"printer": alias, "idempotency_key": key})
    if action == "dismiss_spaghetti":
        return tool_dismiss_spaghetti({"printer": alias})
    if action.startswith("read_"):
        return tool_read_filament({"printer": alias, "slot": value, "idempotency_key": key})
    if action in ("dry_start", "dry_stop"):
        args = {"printer": alias, "unit": value["letter"], "action": action[4:], "idempotency_key": key}
        args.update({k: value[k] for k in ("temp", "hours") if k in value})
        return tool_ams_dry(args)
    if action == "plate_detection":
        payload = {"xcam": {"command": "xcam_control_set", "module_name": "buildplate_marker_detector",
                            "control": bool(value), "print_halt": True}}
        if not link_of(alias).publish(with_sequence(payload)):
            raise ToolError("Comando non partito")
        return
    raise ToolError(f"azione {action} non ammessa da Home Assistant")


def start_ha():
    """Bridge to HA's broker when BFF_HA_MQTT_HOST is set."""
    env = os.environ
    if not env.get("BFF_HA_MQTT_HOST"):
        return
    from ha_mqtt import HaBridge
    names = parse_pairs(env["BFF_HA_NAMES"]) if env.get("BFF_HA_NAMES") else {}
    names = {**{alias: entry.get("name") for alias, entry in onboard.load_store(store_path()).items()}, **names}

    def snapshot(alias):
        try:
            return feed_of(alias).latest(max_age_s=10.0)
        except Exception:  # (ToolError, camera.CameraError) and the rest: a photo is a bonus
            return None

    global HA
    HA = HaBridge(env["BFF_HA_MQTT_HOST"], int(env.get("BFF_HA_MQTT_PORT", "1883")), env["BFF_HA_MQTT_USER"],
                  env["BFF_HA_MQTT_PASSWORD"], list(CFG["printers"]), lambda alias: status_dict({"printer": alias}),
                  ha_action, names, env.get("BFF_HA_LANG", "en"), snapshot=snapshot, profile=profile_of,
                  ams_drying=lambda alias: onboard.ams_drying(model_code(alias)))
    HA.start()


def tool_devices(name, args):
    if not DB_PATH:
        raise ToolError("devices need BFF in control mode")
    builtin = list(CFG["tokens"])
    db = sqlite3.connect(DB_PATH)
    try:
        added = {}
        if name == "device_add":
            added = devices.add(db, args.get("name"), builtin, args.get("origin"))
        elif name == "device_remove":
            devices.remove(db, args.get("name"), builtin)
        elif name == "device_cancel":
            devices.cancel(args.get("name"))
        return as_text({**added, "devices": devices.listing(db, builtin, getattr(REQUEST, "machine", None))})
    except ValueError as err:
        raise ToolError(str(err))
    finally:
        db.close()


def tool_notify(name, args):
    if not DB_PATH:
        raise ToolError("notifications need BFF in control mode")
    db = sqlite3.connect(DB_PATH)
    try:
        return as_text(notify.app_call(db, name, args))
    except ValueError as err:
        raise ToolError(str(err))
    finally:
        db.close()


def start_notify():
    """BFF's own alerts (Web Push, Telegram): they need state.db for settings and subscriptions."""
    global NOTIFIER
    if not DB_PATH:
        return
    def name(alias):
        return (onboard.load_store(store_path()).get(alias) or {}).get("name")
    NOTIFIER = notify.Notifier(DB_PATH, lambda: list(CFG["printers"]), lambda alias: status_dict({"printer": alias}),
                               name, os.environ.get("BFF_HA_LANG", "en"), os.environ.get("BFF_PUBLIC_URL", ""),
                               # /photo/ needs no token (unguessable id), so the phone opens the push image itself,
                               # on the same public base the notification already uses.
                               photo=lambda alias: latest_frame(alias, 10.0),
                               photo_url=lambda jpeg: store_photo(jpeg, os.environ.get("BFF_PUBLIC_URL") or None))
    NOTIFIER.start()


def parse_pairs(text):
    pairs = dict(item.split("=", 1) for item in text.split(",") if item)
    if not pairs:
        raise ValueError("empty list")
    return pairs


def start_links():
    """One link per printer in the app store (the only source of printers)."""
    for alias, entry in onboard.load_store(store_path()).items():
        if alias not in LINKS:
            add_link(alias, entry)


# --- Printers added from the app (spec 2026-11-12-add-printer). Access codes never leave this section. ---

HA = None
ADD_LOCK = threading.Lock()
REASONS = {"unreachable": "Printer unreachable: check the IP", "not a Bambu printer": "This device does not answer like a supported printer",
           "wrong access code": "Wrong access code: check it on the printer screen", "no report": "The printer answered but sent no data: check LAN-only and Developer Mode",
           "moonraker unreachable": "Moonraker does not answer at that IP and port",
           "moonraker key": "Moonraker asks for an API key: enter it under Advanced",
           "not moonraker": "Something answers at that IP and port, but it is not Moonraker"}


def store_path():
    return Path(os.environ["STATE_DIRECTORY"]) / "printers.json"


def fingerprint_dir():
    d = Path(os.environ["STATE_DIRECTORY"]) / "fingerprints"
    d.mkdir(exist_ok=True)
    return d


def bff_base():
    """BFF's own HTTP address as seen from go2rtc on this host: the loopback /stream/ listener."""
    return f"http://127.0.0.1:{CFG.get('loopback_port', 8767)}"


def cam_orientation(alias):
    """EXIF Orientation of a Klipper webcam (Moonraker's flip/rotation); 1 for every other printer."""
    link = LINKS.get(alias)
    if not isinstance(link, MoonrakerLink):
        return 1
    try:
        link.snapshot_url()  # reads the webcam list, flips included
    except (OSError, ValueError):
        pass
    return camera.exif_orientation(*link.cam_flip)


def register_stream(alias, entry):
    """Tell go2rtc about the printer in the background: BFF starts at once even when go2rtc is down."""
    src = go2rtc.source_for(alias, entry, bff_base(), CFG.get("stream_token"), cam_orientation(alias))
    if src is None:  # jpeg printers need PYGMY_STREAM_TOKEN: nothing to register without it
        return
    threading.Thread(target=go2rtc.add_stream, args=(alias, src), daemon=True).start()


def keep_streams():
    """Re-register every stream each minute: go2rtc keeps them in memory and may have restarted."""
    while True:
        try:
            for alias, entry in onboard.load_store(store_path()).items():
                src = go2rtc.source_for(alias, entry, bff_base(), CFG.get("stream_token"), cam_orientation(alias))
                if src is not None:
                    go2rtc.add_stream(alias, src)
        except Exception as err:  # never the sources: they hold the access codes
            print(f"go2rtc: streams: {type(err).__name__}", file=sys.stderr, flush=True)
        time.sleep(60)


def add_link(alias, entry, der=None):
    if onboard.is_klipper(entry.get("model")):  # Moonraker over HTTP: no certificate, no MQTT, no AMS.
        LINKS[alias] = MoonrakerLink(alias, entry["host"], entry.get("api_key", ""), int(entry.get("port", 7125)),
                                      light=entry.get("light"))
        CFG.setdefault("printers", {}).setdefault(alias, f"app:{alias}")
        LINKS[alias].start()
        if HA is not None:
            HA.add_alias(alias, entry.get("name"))
        return
    if der is not None:  # Pin the certificate the probe just checked.
        check_fingerprint(str(fingerprint_dir()), alias, der)
    LINKS[alias] = PrinterLink(alias, entry["host"], entry["serial"], entry["access_code"], str(fingerprint_dir()),
                               ams_drying=onboard.ams_drying(entry.get("model") or ""))
    CFG.setdefault("printers", {}).setdefault(alias, f"app:{alias}")  # unique: photo and light look the printer up by it
    LINKS[alias].start()
    register_stream(alias, entry)
    if HA is not None:
        HA.add_alias(alias, entry.get("name"))


def drop_link(alias):
    link = LINKS.pop(alias, None)
    if link is not None:
        link.stop()
    FEEDS.pop(alias, None)
    WATCHES.pop(alias, None)


def checked(host, code):
    try:
        return onboard.probe(host, code)
    except onboard.ProbeError as err:
        raise ToolError(REASONS.get(err.reason, err.reason)) from None


def profile_of(alias):
    if DB is None:
        return None
    row = DB.execute("SELECT value FROM settings WHERE printer=? AND key='profile'", (alias,)).fetchone()
    if row is None:
        return None
    profile = json.loads(row[0])
    profile.setdefault("chamber_sensor", onboard.chamber_sensor(model_code(alias)))
    return profile


def learn_profile(alias, snap, raw):
    """Only a print shows what the printer really has (the chamber fan of a P1S runs only then)."""
    if DB is None or str(snap.get("state")).upper() not in ("RUNNING", "PAUSE"):
        return
    profile = onboard.learn(profile_of(alias), snap, raw)
    with DB:
        DB.execute("INSERT OR REPLACE INTO settings VALUES (?, 'profile', ?)", (alias, json.dumps(profile)))


def watch_profiles():
    while True:
        for alias in list(LINKS):
            try:
                learn_profile(alias, LINKS[alias].snapshot(), LINKS[alias].raw)
            except Exception as err:
                print(f"{alias}: profile: {err}", file=sys.stderr)
        time.sleep(10)


def app_names_models():
    """Display names and models of the printers added from the app (no access codes)."""
    try:
        store = onboard.load_store(store_path())
    except KeyError:  # no STATE_DIRECTORY
        return {}, {}
    return ({alias: e["name"] for alias, e in store.items()},
            {alias: e.get("label") or onboard.model_name(e["model"], e.get("enclosure"), profile_of(alias))
             for alias, e in store.items()})


def tool_discover_printers(args):
    managed = {link.serial for link in LINKS.values()}
    rows = [{**p, "brand": "Bambu Lab", "model_name": onboard.model_name(p["model"]), "supported": onboard.supported(p["model"]),
             "managed": p["serial"] in managed} for p in onboard.heard()]
    hosts = {(getattr(link, "host", None), getattr(link, "port", None)) for link in LINKS.values()}
    prefixes = onboard.lan_prefixes([p["ip"] for p in rows] + [h for h, _ in hosts])
    klippers = onboard.klipper_heard(prefixes, force=True) if args.get("scan") else onboard.klipper_cached()
    for k in klippers:
        rows.append({**k, "kind": "klipper", "serial": f"{k['ip']}:{k['port']}", "supported": True,
                     "model_name": onboard.model_name(k["model"]) if k["model"] else "Klipper printer",
                     "managed": (k["ip"], k["port"]) in hosts})
    return as_text({"printers": rows, "klipper_models": onboard.klipper_models()})


def tool_identify_printer(args):
    """What answers at a typed IP: Moonraker (Klipper) or else a printer that wants an access code."""
    host = (args.get("ip") or "").strip()
    if not host:
        raise ToolError("Enter the printer IP")
    for port in onboard.SCAN_PORTS:
        try:
            info = onboard.moonraker_get(host, port, "/printer/info", timeout=3)
        except onboard.ProbeError as err:
            if err.reason != "moonraker key":
                continue
            info = {"state": "?"}  # Moonraker's own refusal: it wants a key
        if "state" in info:
            name = info.get("hostname", "")
            model = onboard.guess_klipper_model(name)
            return as_text({"kind": "klipper", "ip": host, "port": port, "name": name, "model": model,
                            "model_name": onboard.model_name(model) if model else "Klipper printer"})
    return as_text({"kind": "bambu", "ip": host, "name": "", "model_name": "Bambu Lab printer"})  # nominative use only


def model_from_cfg(model):
    return bool(onboard.MODELS.get(model, {}).get("from_cfg"))


def keep_scanning_klipper():
    """Moonraker announces nothing by default: look at the LAN each minute so the app lists Klipper printers at once."""
    while True:
        try:
            ips = [p["ip"] for p in onboard.heard()] + [getattr(link, "host", None) for link in LINKS.values()]
            onboard.klipper_heard(onboard.lan_prefixes([ip for ip in ips if ip]), force=True)
        except Exception as err:
            print(f"klipper scan: {type(err).__name__}", file=sys.stderr, flush=True)
        time.sleep(60)


def add_klipper(args, alias):
    host = (args.get("ip") or "").strip()
    if not host:
        raise ToolError("Enter the printer IP")
    model = args.get("model") or ""
    if not onboard.is_klipper(model):
        raise ToolError("Pick the printer model")
    try:
        port = int(args.get("port") or onboard.MOONRAKER_PORT)
    except (TypeError, ValueError):
        port = 0
    if not 0 < port < 65536:
        raise ToolError("The Moonraker port is a number up to 65535")
    api_key = (args.get("api_key") or "").strip()
    store = onboard.load_store(store_path())
    if any(e.get("host") == host and int(e.get("port") or onboard.MOONRAKER_PORT) == port for e in store.values()) \
            or any(getattr(link, "host", None) == host and getattr(link, "port", None) == port for link in LINKS.values()):
        raise ToolError("This printer is already added")
    try:
        probed = onboard.probe_moonraker(host, port, api_key)
    except onboard.ProbeError as err:
        raise ToolError(REASONS.get(err.reason, err.reason)) from None
    entry = {"host": host, "port": port, "api_key": api_key, "name": args.get("name").strip(), "model": model}
    if model_from_cfg(model) and (set(probed["limits"]) != {"nozzle_max_c", "bed_max_c"} or not probed["machine"]):
        raise ToolError("Klipper is not ready, so its printer.cfg could not be read: try again with the printer on")
    if probed["limits"]:
        entry["limits"] = probed["limits"]
    if model_from_cfg(model):
        entry["machine"] = probed["machine"]
        if (label := (args.get("label") or "").strip()[:60]):
            entry["label"] = label
    store[alias] = entry
    onboard.save_store(store_path(), store)
    add_link(alias, entry)
    print(f"{alias}: Klipper aggiunta dall'app", file=sys.stderr)
    return as_text({"printer": alias, "model_name": entry.get("label") or onboard.model_name(model)})


def tool_add_printer(args):
    try:
        code = None if args.get("kind") == "klipper" else onboard.clean_code(args.get("access_code"))
        alias = onboard.slug(args.get("name"))
    except ValueError as err:
        raise ToolError(str(err)) from None
    with ADD_LOCK:
        if alias in LINKS or alias in CFG.get("printers", {}):
            raise ToolError(f"The name {args.get('name') or alias} is already used")
        if args.get("kind") == "klipper":
            return add_klipper(args, alias)
        heard = {p["serial"]: p for p in onboard.heard()}
        by_ip = {p["ip"]: p for p in heard.values()}
        seen = heard.get(args.get("serial")) or by_ip.get(args.get("ip"))
        host = args.get("ip") or (seen or {}).get("ip")
        if not host:
            raise ToolError("Printer not found on the network: enter its IP")
        if seen and not onboard.supported(seen["model"]):
            raise ToolError(f"{onboard.model_name(seen['model'])} is not supported yet")
        if (seen or {}).get("serial") in {link.serial for link in LINKS.values()}:
            raise ToolError("This printer is already added")
        probed = checked(host, code)
        if probed["serial"] in {link.serial for link in LINKS.values()}:
            raise ToolError("This printer is already added")
        seen = heard.get(probed["serial"]) or seen or {}
        entry = {"host": host, "serial": probed["serial"], "access_code": code, "name": args.get("name").strip(),
                 "model": seen.get("model") or onboard.model_from_serial(probed["serial"]),
                 "enclosure": bool(args.get("enclosure"))}
        store = onboard.load_store(store_path())
        store[alias] = entry
        onboard.save_store(store_path(), store)
        add_link(alias, entry, probed["der"])
    print(f"{alias}: aggiunta dall'app", file=sys.stderr)
    return as_text({"printer": alias, "model_name": onboard.model_name(entry["model"], entry["enclosure"])})


def app_added(alias):
    store = onboard.load_store(store_path())
    if alias not in store:
        raise ToolError("solo le stampanti aggiunte dall'app si gestiscono da qui")
    return store


def tool_set_access_code(args):
    alias = args["printer"]
    try:
        code = onboard.clean_code(args.get("access_code"))
    except ValueError as err:
        raise ToolError(str(err)) from None
    with ADD_LOCK:
        store = app_added(alias)
        entry = store[alias]
        if onboard.is_klipper(entry.get("model")):
            raise ToolError("Klipper printers have no access code")
        if checked(entry["host"], code)["serial"] != entry["serial"]:
            raise ToolError("a quell'IP ora risponde un'altra stampante")
        entry["access_code"] = code
        onboard.save_store(store_path(), store)
        drop_link(alias)
        add_link(alias, entry)
    return as_text({"ok": True})


def tool_remove_printer(args):
    alias = args["printer"]
    with ADD_LOCK:
        store = app_added(alias)
        del store[alias]
        onboard.save_store(store_path(), store)
        drop_link(alias)
        go2rtc.remove_stream(alias)
        CFG.get("printers", {}).pop(alias, None)
        for f in fingerprint_dir().glob(f"{alias}*"):
            if f.name in (alias, f"{alias}-cam", f"{alias}-ftp"):
                f.unlink()
        if HA is not None:
            HA.remove_alias(alias)
    print(f"{alias}: tolta dall'app", file=sys.stderr)
    return as_text({"ok": True})


def tool_set_expert(args):
    """Expert mode is opt-in per device: refused while the model has not been tested by hand."""
    printer_id(args)
    enabled = args.get("enabled") is True
    if enabled and not tested_model(args["printer"]):
        raise ToolError("Modalita expert non disponibile: modello non ancora testato")
    return as_text({"expert": enabled})


def tool_camera_urls(args):
    """Where this printer's camera can be watched from: RTSP for Home Assistant."""
    alias = args["printer"]
    printer_id(args)
    host = os.environ.get("BFF_PUBLIC_HOST") or request_host()
    user, password = os.environ.get("BFF_RTSP_USER"), os.environ.get("BFF_RTSP_PASS")
    rtsp = None
    if user and password and host:  # no credentials or no host: nothing a client could open
        rtsp = (f"rtsp://{urllib.parse.quote(user, safe='')}:{urllib.parse.quote(password, safe='')}"
                f"@{host}:8554/{alias}")
    return as_text({"rtsp": rtsp, "kind": onboard.camera_kind(model_code(alias))})


def tool_camera_webrtc(args):
    """The answer SDP for the app's live view, from go2rtc."""
    printer_id(args)
    alias = args["printer"]
    if onboard.camera_kind(model_code(alias)) != "rtsp":
        raise ToolError("This printer has no video stream")
    offer = args.get("offer")
    if not isinstance(offer, str) or not offer:
        raise ToolError("Missing offer")
    try:
        return as_text({"answer": go2rtc.webrtc_answer(alias, offer)})
    except Exception:  # go2rtc down or refusing: the app falls back to JPEG
        raise ToolError("Video stream unavailable") from None


# Never in tools/list; call_tool reaches them only with the app header (agents never handle access codes).
def tool_mw_login_start(args):
    try:
        makerworld.login_start(DB, args.get("email"))
    except makerworld.LoginFailed as err:
        raise ToolError(str(err))
    return as_text({"outcome": "code sent"})


def tool_mw_login_code(args):
    try:
        makerworld.login_code(DB, args.get("code"))
    except makerworld.LoginFailed as err:
        raise ToolError(str(err))
    with DB:
        n = DB.execute("UPDATE print_requests SET status='pending', note=NULL "
                       "WHERE status='blocked' AND source='makerworld' AND engine='server' "
                       "AND note='MakerWorld login needed'").rowcount
    SLICE_WAKE.set()
    return as_text({"logged_in": True, "requeued": n})


def tool_mw_status(_args):
    return as_text(makerworld.status(DB))


def tool_mw_logout(_args):
    makerworld.logout(DB)
    return as_text({"outcome": "logged out"})


APP_TOOLS = {
    "discover_printers": (tool_discover_printers, "", {}, True),
    "mw_login_start": (tool_mw_login_start, "Invia il codice di accesso MakerWorld per email.",
                       {"type": "object", "properties": {"email": {"type": "string"}}, "required": ["email"]}, True),
    "mw_login_code": (tool_mw_login_code, "Conferma il login MakerWorld con il codice ricevuto per email.",
                      {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}, True),
    "mw_status": (tool_mw_status, "Stato del login MakerWorld.",
                  {"type": "object", "properties": {}, "required": []}, True),
    "mw_logout": (tool_mw_logout, "Disconnette MakerWorld.",
                  {"type": "object", "properties": {}, "required": []}, True),
    "plate_preview": (tool_plate_preview, "Miniatura di un piatto di un progetto multi-piatto.",
                      {"type": "object", "properties": {"id": {"type": "integer"}, "plate": {"type": "integer"}},
                       "required": ["id", "plate"]}, True),
    "file_preview": (tool_file_preview, "Anteprima della piastra affettata di un file.",
                     {"type": "object", "properties": {"file_id": {"type": "integer"}},
                      "required": ["file_id"]}, True),
    "print_preview": (tool_print_preview, "Immagine del lavoro in stampa.", {}, True),
    "add_printer": (tool_add_printer, "", {}, True),
    "identify_printer": (tool_identify_printer, "", {}, True),
    "rename_printer": (tool_rename_printer, "", {}, True),
    "orca_profiles": (tool_orca_profiles, "", {}, True),
    "orca_cloud": (tool_orca_cloud, "", {}, True),
    "set_access_code": (tool_set_access_code, "", {}, True),
    "remove_printer": (tool_remove_printer, "", {}, True),
    "set_expert": (tool_set_expert, "", {}, True),
    "camera_urls": (tool_camera_urls, "", {}, True),
    "camera_webrtc": (tool_camera_webrtc, "", {}, True),
    "ams_dry": (tool_ams_dry, "", {}, True),
    **{name: (lambda args, name=name: tool_notify(name, args), "", {}, True)
       for name in ("notify_get", "notify_set", "push_subscribe", "push_remove", "notify_test")},
    **{name: (lambda args, name=name: tool_devices(name, args), "", {}, True)
       for name in ("device_list", "device_add", "device_remove", "device_cancel")},
}


class StreamOnlyHandler(Handler):
    """The LAN listener: /stream/ and nothing else, so no bearer-protected route is exposed there."""

    def do_GET(self):
        if self.path.startswith("/obico/"):
            # The one-shot nonce is the credential: Obico fetches each frame once, within FRAME_TTL_S.
            self.close_connection = True
            jpeg = spaghetti.pop(self.path[len("/obico/"):].removesuffix(".jpg"))
            return self.send_bytes("image/jpeg", jpeg) if jpeg else self.reply(404)
        if not self.path.startswith("/stream/"):
            self.close_connection = True
            return self.reply(404)
        return super().do_GET()

    def do_POST(self):
        self.close_connection = True
        self.reply(404)

    do_PUT = do_DELETE = do_POST


def main():
    env = os.environ
    tokens = parse_pairs(env["BFF_MCP_TOKENS"])
    if any(len(token) < 32 for token in tokens.values()):
        sys.exit("BFF_MCP_TOKENS: ogni token deve avere almeno 32 caratteri")
    bind, port = env["BFF_MCP_BIND"], int(env.get("BFF_MCP_PORT", "8765"))
    CFG.update(
        printers={},
        tokens=tokens,
        control=env.get("BFF_MCP_CONTROL") == "1",
        port=port,
        public_url=env_public_url(env),
        local_url=f"http://{bind}:{port}",
        orca_wake_url=env.get("BFF_ORCA_WAKE_URL"),
        orca_wake_token=env.get("BFF_ORCA_WAKE_TOKEN", ""),
        stream_token=env.get("BFF_STREAM_TOKEN", ""),
        loopback_port=int(env.get("BFF_LOOPBACK_PORT", "8767")),
        obico_url=env.get("BFF_OBICO_URL", ""),
        obico_token=env.get("BFF_OBICO_TOKEN", ""),
        obico_sensitivity=env.get("BFF_OBICO_SENSITIVITY", "medium"),
        obico_interval_s=float(env.get("BFF_OBICO_INTERVAL_S", "10")),
        obico_frame_base=f"http://{env.get('BFF_LAN_BIND')}:{env.get('BFF_LAN_PORT', '8766')}",
    )
    if CFG["control"]:
        CFG["profiles"] = env["BFF_MCP_PROFILES"]
        # STATE_DIRECTORY comes from StateDirectory= in the systemd unit.
        global DB_PATH
        DB_PATH = str(Path(env["STATE_DIRECTORY"]) / "state.db")
        open_db(DB_PATH)
        library.setup(env.get("BFF_FILES_DIR") or str(Path(env["STATE_DIRECTORY"]) / "files"))
        migrate_blobs()
    start_links()
    threading.Thread(target=keep_streams, daemon=True).start()
    if DB is not None:
        threading.Thread(target=watch_plates, daemon=True).start()
    if CFG["obico_url"] and env.get("BFF_LAN_BIND"):
        threading.Thread(target=watch_spaghetti, daemon=True).start()
    start_ha()
    start_notify()
    threading.Thread(target=onboard.keep_listening, daemon=True).start()
    threading.Thread(target=keep_scanning_klipper, daemon=True).start()
    threading.Thread(target=watch_profiles, daemon=True).start()
    threading.Thread(target=watch_orca_cloud, daemon=True).start()
    slicer.setup(env.get("BFF_SLICER_URL"), env.get("BFF_SLICER_SECRET"))
    fetch.setup_thingiverse(env.get("BFF_THINGIVERSE_TOKEN"))
    if slicer.configured():
        threading.Thread(target=slice_worker, daemon=True).start()
    # No default bind address: this must be the Tailscale IP, never a wildcard by accident.
    if env.get("BFF_LAN_BIND"):
        # Home network listener for Home Assistant: camera stream only, firewalled to HA's address in the CT.
        lan = ThreadingHTTPServer((env["BFF_LAN_BIND"], int(env.get("BFF_LAN_PORT", "8766"))), StreamOnlyHandler)
        threading.Thread(target=lan.serve_forever, daemon=True).start()
    try:  # go2rtc runs on this host and reads the /stream/ source on loopback, whatever the Tailscale bind is.
        local = ThreadingHTTPServer(("127.0.0.1", CFG["loopback_port"]), StreamOnlyHandler)
        threading.Thread(target=local.serve_forever, daemon=True).start()
    except OSError as err:  # already covered by the main bind (0.0.0.0)
        print(f"loopback listener: {err}", file=sys.stderr, flush=True)
    ThreadingHTTPServer((bind, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
