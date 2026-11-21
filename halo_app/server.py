"""Halo Jr. control app. Run:  python3 server.py   then open  http://localhost:5050"""
import logging
import os
import time

from flask import Flask, Response, jsonify, request, send_from_directory

from lamp import JOINTS, SMALL, Lamp, SerialLink, save_settings
from vision import Vision

HERE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=os.path.join(HERE, "static"), static_url_path="/static")

logging.getLogger("werkzeug").setLevel(logging.ERROR)   # stop printing every page refresh

link = SerialLink()
lamp = Lamp(link)
vision = Vision(lamp)


def ok(**extra):
    return jsonify({"ok": True, **extra})


def bad(msg):
    print("  !", msg)
    return jsonify({"ok": False, "error": msg}), 400


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/state")
def state():
    return jsonify({"connected": link.connected, "port": link.port, "lamp": lamp.state(),
                    "vision": vision.state(), "log": link.log[-14:]})


@app.get("/api/settings")
def get_settings():
    return jsonify(lamp.s)


@app.post("/api/settings")
def set_settings():
    new = request.get_json(force=True)
    for key in ("channels", "invert", "offsets", "head", "limits", "pulse", "levels", "tracking", "light", "gestures", "small"):
        if key in new and not isinstance(new[key], type(lamp.s[key])):
            return bad("bad value for " + key)
    lamp.s.update(new)
    save_settings(lamp.s)
    lamp.resync()
    return ok()


@app.get("/api/ports")
def ports():
    return jsonify(SerialLink.ports())


@app.post("/api/connect")
def connect():
    port = request.get_json(force=True).get("port", "")
    try:
        link.connect(port)
    except Exception as e:
        return bad("Could not open %s: %s" % (port, e))
    lamp.s["serial_port"] = port
    save_settings(lamp.s)
    lamp.resync()
    return ok()


@app.post("/api/disconnect")
def disconnect():
    link.disconnect()
    return ok()


@app.post("/api/level")
def level():
    n = int(request.get_json(force=True)["n"])
    if not 1 <= n <= len(lamp.s["levels"]):
        return bad("no such level")
    lamp.level(n)
    return ok()


@app.post("/api/move")
def move():
    name = request.get_json(force=True)["name"]
    if name == "stop":
        lamp.stop_move()
        return ok()
    if name not in lamp.moves():
        return bad("unknown move")
    lamp.run_move(name)
    return ok()


@app.post("/api/pose")
def pose():
    body = request.get_json(force=True)
    p = {k: float(body[k]) for k in ("yaw", "lean", "fore", "trim") if k in body}
    lamp.stop_move()
    want = {**lamp.target, **p}
    err = lamp.check(want)
    if err:
        return bad("Refused: " + err)
    lamp.go(p)
    return ok()


@app.post("/api/servo")
def servo():
    body = request.get_json(force=True)
    joint = body["joint"]
    if joint in SMALL:
        lamp.set_small(joint, float(body["value"]))
        return ok()
    if joint not in JOINTS:
        return bad("unknown servo")
    lamp.stop_move()
    err = lamp.set_servo(joint, float(body["value"]))
    return bad(err) if err else ok()


@app.post("/api/iris")
def iris():
    body = request.get_json(force=True)
    if "save" in body:
        key = "iris_open" if body["save"] == "open" else "iris_closed"
        lamp.s["small"][key] = round(lamp.small["iris"])
        save_settings(lamp.s)
    else:
        lamp.iris(bool(body.get("open")))
    return ok()


@app.post("/api/light")
def light():
    body = request.get_json(force=True)
    lamp.light(body.get("on"), body.get("brightness"))
    return ok()


@app.post("/api/release")
def release():
    lamp.release()
    return ok()


@app.post("/api/camera")
def camera():
    body = request.get_json(force=True)
    if body.get("on"):
        idx = int(body.get("index", lamp.s["tracking"]["camera_index"]))
        if not vision.start(idx):
            return bad(vision.error)
        lamp.s["tracking"]["camera_index"] = idx
        save_settings(lamp.s)
    elif body.get("on") is False:
        vision.stop()
    if "follow" in body:
        vision.follow = bool(body["follow"]) and vision.cap is not None
        if vision.follow:
            lamp.stop_move()
    if "gestures" in body:
        vision.gestures = bool(body["gestures"]) and vision.cap is not None
    return ok()


@app.get("/video")
def video():
    def gen():
        while True:
            f = vision.frame()
            if f is None:
                time.sleep(0.2)
                if vision.cap is None:
                    return
                continue
            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + f + b"\r\n"
            time.sleep(1 / 25)
    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")


if __name__ == "__main__":
    port = lamp.s.get("serial_port")
    if port and any(p["device"] == port for p in SerialLink.ports()):
        try:
            link.connect(port)
            lamp.resync()
        except Exception as e:
            print("Could not reconnect to", port, "-", e)
    print("\n  Halo Jr. control app:  http://localhost:5050\n")
    app.run(host="127.0.0.1", port=5050, threaded=True, debug=False)
