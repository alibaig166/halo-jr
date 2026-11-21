"""Halo Jr. lamp logic: settings, pose <-> servo maths, moves, and the USB link to the ESP32."""
import json
import math
import os
import threading
import time

try:
    import serial
    import serial.tools.list_ports
except ImportError:          # the app still runs (simulator only) without pyserial
    serial = None

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(HERE, "settings.json")            # your saved settings (not in git)
DEFAULTS_FILE = os.path.join(HERE, "settings.default.json")    # defaults that come with updates
JOINTS = ["base", "upper", "pushrod", "head"]
SMALL = ["iris", "cam_pan", "cam_tilt"]


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def load_settings():
    """Defaults first, then your saved values on top, so a git pull never wipes your settings
    and new options from an update still show up."""
    with open(DEFAULTS_FILE) as f:
        s = json.load(f)
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE) as f:
            mine = json.load(f)
        for k, v in mine.items():
            if isinstance(v, dict) and isinstance(s.get(k), dict):
                s[k] = {**s[k], **v}
            else:
                s[k] = v
    return s


def save_settings(s):
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=2)
    os.replace(tmp, SETTINGS_FILE)


class SerialLink:
    """USB serial connection to the ESP32. Without a port it runs as a simulator."""

    def __init__(self):
        self.ser = None
        self.port = ""
        self.lock = threading.Lock()
        self.log = []                 # last lines sent / received, shown in the app
        self.running = True
        threading.Thread(target=self._reader, daemon=True).start()

    @staticmethod
    def ports():
        if serial is None:
            return []
        out = []
        for p in serial.tools.list_ports.comports():
            name = p.device
            if "Bluetooth" in name or "debug-console" in name or "wlan-debug" in name:
                continue
            if name.startswith("/dev/tty."):      # Mac lists every port twice; /dev/cu.* is the one to use
                continue
            out.append({"device": name, "desc": p.description or ""})
        return out

    def connect(self, port):
        self.disconnect()
        if serial is None:
            raise RuntimeError("pyserial is not installed (pip install pyserial)")
        if not port:
            raise RuntimeError("no port picked. Plug in the ESP32 and press the refresh button")
        s = serial.Serial(port, 115200, timeout=0.05)
        time.sleep(1.8)               # the ESP32 resets when the port opens
        with self.lock:
            self.ser, self.port = s, port
        self._note("connected to " + port)

    def disconnect(self):
        with self.lock:
            if self.ser is not None:
                try:
                    self.ser.close()
                except Exception:
                    pass
            self.ser, self.port = None, ""

    @property
    def connected(self):
        return self.ser is not None

    def send(self, line):
        self._note("> " + line)
        with self.lock:
            if self.ser is None:
                return
            try:
                self.ser.write((line + "\n").encode())
            except Exception as e:
                self._note("serial error: %s" % e)
                try:
                    self.ser.close()
                except Exception:
                    pass
                self.ser, self.port = None, ""

    def _note(self, text):
        self.log.append(time.strftime("%H:%M:%S ") + text)
        del self.log[:-60]

    def _reader(self):
        buf = b""
        while self.running:
            s = self.ser
            if s is None:
                time.sleep(0.1)
                continue
            try:
                data = s.read(256)
            except Exception:
                time.sleep(0.1)
                continue
            if not data:
                continue
            buf += data
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                text = raw.decode(errors="replace").strip()
                if text and text != "OK":
                    self._note("< " + text)


class Lamp:
    """Pose = lamp angles: yaw (base turn), lean (upper arm), fore (forearm), trim (beam up/down).
    Servo values are the real angles sent to each PCA9685 channel."""

    def __init__(self, link):
        self.link = link
        self.s = load_settings()
        self.lock = threading.Lock()
        lv = self.s["levels"][self.s["start_level"] - 1]
        self.target = {"yaw": 0.0, "lean": lv[0], "fore": lv[1], "trim": 0.0}
        start = self.servos_for(self.target)
        self.from_sv, self.to_sv = dict(start), dict(start)
        self.t0, self.dur = time.time(), {j: 0.0 for j in JOINTS}   # each servo has its own move time
        self.light_on = False
        self.brightness = self.s["light"]["brightness"]
        self.move_thread = None
        self.move_stop = threading.Event()
        self.status = "ready"
        self.sent_once = False
        self.small = dict(self.s["small"]["start"])

    # ---------- maths ----------
    def _inv(self, joint, a):
        return 180 - a if self.s["invert"][joint] else a

    def servos_for(self, p):
        s = self.s
        base = s["offsets"]["base"] + p["yaw"]
        upper = p["lean"] + s["offsets"]["upper"]
        push = p["fore"] + s["offsets"]["pushrod"]
        head = s["head"]["center"] + p["trim"]
        if s["head"]["gimbal"]:
            head -= p["fore"] - s["head"]["fore_at_level1"]
        raw = {"base": base, "upper": upper, "pushrod": push, "head": head}
        return {j: round(self._inv(j, clamp(raw[j], 0, 180)), 1) for j in JOINTS}

    def pose_for(self, sv):
        s = self.s
        base, upper, push, head = (self._inv(j, sv[j]) for j in JOINTS)
        fore = push - s["offsets"]["pushrod"]
        trim = head - s["head"]["center"]
        if s["head"]["gimbal"]:
            trim += fore - s["head"]["fore_at_level1"]
        # beam direction for the 3D view: 90 = straight ahead
        aim = 90 + (head - s["head"]["center"]) + (fore - s["head"]["fore_at_level1"])
        return {"yaw": base - s["offsets"]["base"], "lean": upper - s["offsets"]["upper"],
                "fore": fore, "trim": trim, "aim": aim}

    def check(self, p):
        """Return an error string if the pose would jam the arm, else None."""
        L = self.s["limits"]
        elbow = p["fore"] - p["lean"]
        if not L["lean"][0] <= p["lean"] <= L["lean"][1]:
            return "upper arm out of range (%d)" % p["lean"]
        if not L["fore"][0] <= p["fore"] <= L["fore"][1]:
            return "forearm out of range (%d)" % p["fore"]
        if not L["elbow"][0] <= elbow <= L["elbow"][1]:
            return "elbow would be %d deg (must be %d..%d, the arm would jam)" % (elbow, *L["elbow"])
        return None

    def legal(self, p):
        L = self.s["limits"]
        p = dict(p)
        p["yaw"] = clamp(p["yaw"], *L["yaw"])
        p["lean"] = clamp(p["lean"], *L["lean"])
        p["fore"] = clamp(p["fore"], max(L["fore"][0], p["lean"] + L["elbow"][0]), min(L["fore"][1], p["lean"] + L["elbow"][1]))
        p["trim"] = clamp(p["trim"], *L["trim"])
        return p

    # ---------- current state (interpolated like the ESP32 does) ----------
    def current_servos(self):
        with self.lock:
            out = {}
            for j in JOINTS:
                if self.dur[j] <= 0:
                    out[j] = self.to_sv[j]
                    continue
                u = clamp((time.time() - self.t0) / self.dur[j], 0, 1)
                k = 0.5 - 0.5 * math.cos(math.pi * u)
                out[j] = self.from_sv[j] + (self.to_sv[j] - self.from_sv[j]) * k
            return out

    def moving(self):
        return time.time() - self.t0 < max(self.dur.values())

    # ---------- commands ----------
    def speed_of(self, name):
        """deg/s for one servo (camera = both camera servos)."""
        return max(1, self.s.get("speeds", {}).get(name, self.s["speed"]))

    def _send_targets(self, sv, ms_by_joint):
        ch = self.s["channels"]
        if not self.sent_once:
            p = self.s["pulse"]
            self.link.send("C %d %d" % (p["min"], p["max"]))
            if not self.link.connected:
                ms_by_joint = {j: 0 for j in JOINTS}
            self.sent_once = self.link.connected
        groups = {}                                   # servos with the same move time go in one command
        for j in JOINTS:
            groups.setdefault(int(ms_by_joint[j]), []).append(j)
        for ms, joints in groups.items():
            vals = [-1] * 8
            for j in joints:
                vals[ch[j]] = sv[j]
            self.link.send("T %d %s" % (ms, " ".join("%.1f" % v if v >= 0 else "-1" for v in vals)))

    def go_servos(self, sv, ms=None):
        now_sv = self.current_servos()
        if ms is None:
            # each servo uses its own speed from Settings
            ms_j = {}
            for j in JOINTS:
                d = abs(sv[j] - now_sv[j])
                ms_j[j] = 0 if d < 0.05 else max(self.s["min_move_ms"], d / self.speed_of(j) * 1000)
            # upper arm and push rod must finish together, or the elbow can jam in the middle of a move
            pair = max(ms_j["upper"], ms_j["pushrod"])
            if pair:
                ms_j["upper"] = ms_j["pushrod"] = pair
        else:
            ms_j = {j: ms for j in JOINTS}
        with self.lock:
            self.from_sv, self.to_sv = now_sv, dict(sv)
            self.t0, self.dur = time.time(), {j: ms_j[j] / 1000.0 for j in JOINTS}
        self._send_targets(sv, ms_j)
        return max(ms_j.values())

    def go(self, pose, ms=None):
        p = self.legal({**self.target, **pose})
        self.target = p
        return self.go_servos(self.servos_for(p), ms)

    def set_servo(self, joint, value):
        sv = dict(self.to_sv)
        sv[joint] = clamp(float(value), 0, 180)
        p = self.pose_for(sv)
        if joint in ("upper", "pushrod"):
            err = self.check(p)
            if err:
                return "Refused: " + err
        self.target = {k: p[k] for k in ("yaw", "lean", "fore", "trim")}
        self.go_servos(sv)
        return None

    def set_small(self, name, value, ms=None):
        """Iris and camera servos: moved on their own, no arm maths."""
        v = clamp(float(value), 0, 180)
        if ms is None:
            d = abs(v - self.small[name])
            ms = d / self.speed_of("iris" if name == "iris" else "camera") * 1000
        self.small[name] = v
        vals = [-1] * 8
        vals[self.s["small"]["channels"][name]] = v
        self.link.send("T %d %s" % (ms, " ".join("%.1f" % x if x >= 0 else "-1" for x in vals)))

    def iris(self, open_):
        self.set_small("iris", self.s["small"]["iris_open" if open_ else "iris_closed"])

    def level(self, n):
        lean, fore = self.s["levels"][n - 1]
        self.stop_move()
        self.go({"lean": lean, "fore": fore})

    def light(self, on=None, brightness=None):
        if on is not None:
            self.light_on = bool(on)
        if brightness is not None:
            self.brightness = int(clamp(brightness, 0, 255))
        self.link.send("L %d" % (self.brightness if self.light_on else 0))

    def release(self):
        self.stop_move()
        self.link.send("X")
        self.sent_once = False

    def resync(self):
        """Re-send pulse range and where every servo should be (after connecting or changing settings)."""
        self.sent_once = False
        self.go_servos(self.servos_for(self.target), self.s["min_move_ms"])
        for name in SMALL:
            self.set_small(name, self.small[name])
        self.light()

    # ---------- moves ----------
    def moves(self):
        return ["wake", "sleep", "nod", "shake", "look", "stretch"]

    def _keys(self, name):
        t = dict(self.target)
        lv = self.s["levels"]
        if name == "wake":
            return [({"lean": lv[0][0], "fore": lv[0][1], "trim": -20}, 1500), ({"lean": lv[2][0], "fore": lv[2][1], "trim": 15}, 2500),
                    ({"trim": 0}, 900), ({"yaw": -35}, 1400), ({"yaw": 25}, 1800), ({"yaw": 0}, 1200)]
        if name == "sleep":
            return [({"yaw": 0, "lean": lv[0][0], "fore": lv[0][1], "trim": 0}, 3000), ({"trim": -35}, 1800)]
        if name == "nod":
            return [({"trim": t["trim"] + 15}, 350), ({"trim": t["trim"] - 10}, 350)] * 3 + [({"trim": t["trim"]}, 400)]
        if name == "shake":
            return [({"yaw": t["yaw"] - 18}, 350), ({"yaw": t["yaw"] + 18}, 450)] * 3 + [({"yaw": t["yaw"]}, 400)]
        if name == "look":
            return [({"trim": t["trim"] + 10, "yaw": -50}, 2000), ({"yaw": 50}, 3500), ({"yaw": t["yaw"], "trim": t["trim"]}, 2000)]
        if name == "stretch":
            return [({"lean": lv[3][0], "fore": lv[3][1], "trim": 25}, 2500), ({"trim": -10}, 1200), ({"trim": 0}, 900)]
        return []

    def run_move(self, name):
        self.stop_move()
        keys = self._keys(name)
        if name == "wake":
            self.light(on=True)
        stop = self.move_stop = threading.Event()

        def worker():
            self.status = name
            for pose, ms in keys:
                if stop.is_set():
                    return
                self.go(pose, ms)
                if stop.wait(ms / 1000.0):
                    return
            if name == "sleep":
                self.light(on=False)
            self.status = "ready"

        self.move_thread = threading.Thread(target=worker, daemon=True)
        self.move_thread.start()

    def stop_move(self):
        self.move_stop.set()
        self.status = "ready"

    def moving_by_move(self):
        return self.move_thread is not None and self.move_thread.is_alive() and not self.move_stop.is_set()

    # ---------- state for the web page ----------
    def state(self):
        sv = self.current_servos()
        return {"servos": {j: round(sv[j], 1) for j in JOINTS}, "target_servos": self.to_sv,
                "pose": {k: round(v, 1) for k, v in self.pose_for(sv).items()},
                "target": {k: round(v, 1) for k, v in self.target.items()},
                "small": {k: round(v, 1) for k, v in self.small.items()},
                "moving": self.moving(), "status": self.status,
                "light": {"on": self.light_on, "brightness": self.brightness}}
