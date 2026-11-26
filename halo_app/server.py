"""Halo Jr. control app. Run:  python3 server.py   then open  http://localhost:5050"""
import logging
import os
import threading
import time

from flask import Flask, Response, jsonify, redirect, request, send_from_directory

from lamp import JOINTS, SMALL, Lamp, SerialLink, save_settings
from vision import Vision
from wiz import WizBulb
from brain import Brain
from ears import Ears
from music import Music

HERE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=os.path.join(HERE, "static"), static_url_path="/static")

logging.getLogger("werkzeug").setLevel(logging.ERROR)   # stop printing every page refresh

link = SerialLink()
lamp = Lamp(link)
vision = Vision(lamp)
music = Music(lamp)
brain = Brain(lamp, music)
ears = Ears(lamp)


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
    for key in ("channels", "invert", "offsets", "head", "limits", "pulse", "levels", "tracking", "light", "gestures", "small", "wiz", "llm", "spotify"):
        if key in new and not isinstance(new[key], type(lamp.s[key])):
            return bad("bad value for " + key)
    lamp.s.update(new)
    save_settings(lamp.s)
    lamp.resync()
    return ok()


@app.get("/api/ports")
def ports():
    return jsonify(SerialLink.ports(lamp.s.get("wifi_host", "192.168.4.1")))


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
    # old firmware (v1/v2 had only channels 0-7, v1 used D21/D22) can't drive the spaced-out pinout
    fw = getattr(link, "firmware", "")
    used = list(lamp.s["channels"].values()) + list(lamp.s["small"]["channels"].values())
    if fw and ("v1" in fw) and max(used) > 7:
        link._note("! This ESP32 has old firmware (%s). It only drives channels 0-7, but your pinout uses up to %d. "
                   "Upload the new firmware, or Settings -> Pinout -> 'Use 0-6' and move the plugs back." % (fw.split("<")[-1].strip(), max(used)))
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


@app.get("/api/llm")
def llm_info():
    m = brain.models()
    if isinstance(m, str):
        return jsonify({"ok": False, "error": m, "models": []})
    return jsonify({"ok": True, "models": m, "model": lamp.s.get("llm", {}).get("model")})


@app.get("/api/music")
def music_state():
    st = music.status()
    st["now"] = music.now() if music.logged_in and request.args.get("now") else None
    return jsonify(st)


@app.post("/api/music")
def music_do():
    b = request.get_json(force=True)
    try:
        if "client_id" in b:
            lamp.s.setdefault("spotify", {})["client_id"] = str(b["client_id"]).strip()
            save_settings(lamp.s)
            return ok()
        if b.get("logout"):
            music.logout()
            return ok()
        if b.get("sync"):
            n, p = music.sync()
            return ok(say="Saved %d songs and %d playlists for offline." % (n, p))
        if "duck" in b:
            threading.Thread(target=music.duck, args=(bool(b["duck"]),), daemon=True).start()
            return ok()
        if b.get("play"):
            return ok(say=music.play(str(b["play"]), b.get("kind", "song")))
        if b.get("action"):
            return ok(say=music.control(str(b["action"])))
        if "volume" in b:
            return ok(say=music.volume(int(b["volume"])))
    except RuntimeError as e:
        return bad(str(e))
    except OSError as e:
        return bad("No internet for Spotify (%s)" % e)
    return bad("nothing to do")


@app.get("/spotify/login")
def spotify_login():
    try:
        return redirect(music.login_url())
    except RuntimeError as e:
        return str(e), 400


@app.get("/spotify/callback")
def spotify_callback():
    if request.args.get("error"):
        return "<h2>Spotify said: %s</h2>" % request.args["error"], 400
    try:
        music.finish_login(request.args.get("code", ""))
    except Exception as e:
        return "<h2>Spotify login failed</h2><p>%s</p>" % e, 400
    return ("<body style='font:18px system-ui;background:#111;color:#eee;padding:40px'>"
            "<h2>Spotify connected ✓</h2><p>Halo is saving your songs for offline use. You can close this tab.</p></body>")


@app.get("/api/stt")
def stt_status():
    if request.args.get("load"):
        ears.preload()
    return jsonify(ears.status())


@app.post("/api/stt")
def stt():
    """Raw 16-bit 16 kHz mono audio in, text out (Whisper, offline)."""
    try:
        text = ears.hear(request.get_data(), int(request.args.get("rate", 16000)))
    except RuntimeError as e:
        return bad(str(e))
    return ok(text=text)


@app.post("/api/chat")
def chat():
    body = request.get_json(force=True)
    if body.get("forget"):
        brain.forget()
        return ok()
    try:
        out = brain.chat(body.get("text", ""))
    except RuntimeError as e:
        return bad(str(e))
    return ok(**out)


@app.post("/api/wiz")
def wiz():
    """find = list bulbs on the Wi-Fi; ip/mode/temp/color = change and save; test = ask the bulb."""
    body = request.get_json(force=True)
    w = lamp.s.setdefault("wiz", {})
    if "route" in body:
        w["route"] = body["route"]
        save_settings(lamp.s)
    if "esp_wifi" in body:
        try:
            lamp.esp_wifi(body["esp_wifi"].get("name", ""), body["esp_wifi"].get("password", ""))
        except RuntimeError as e:
            return bad(str(e))
        return ok()
    if body.get("find"):
        route = w.get("route", "auto")
        if route == "esp" or (route == "auto" and link.connected):
            try:
                found = lamp.find_wiz_via_esp()
            except RuntimeError as e:
                if route == "esp":
                    return bad(str(e))
                found = []
            if found or route == "esp":
                return ok(bulbs=found, via="esp")
        return ok(bulbs=WizBulb.find(), via="laptop")
    for k in ("ip", "mode", "temp", "color"):
        if k in body:
            w[k] = body[k]
    save_settings(lamp.s)
    lamp.light()
    if body.get("test"):
        lamp.wiz.ip = w.get("ip", "")
        if lamp.wiz_via_esp():
            try:
                found = [b["ip"] for b in lamp.find_wiz_via_esp()]
            except RuntimeError as e:
                return bad(str(e))
            if w.get("ip") not in found:
                return bad("The ESP32 can't see a bulb at %s. Bulbs on its Wi-Fi: %s" % (w.get("ip") or "(no IP)", ", ".join(found) or "none"))
            return ok()
        if not lamp.wiz.test():
            return bad("WiZ bulb did not answer at %s: %s" % (w.get("ip") or "(no IP)", lamp.wiz.error))
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
    if lamp.s.get("llm", {}).get("stt") == "whisper":
        ears.preload()                       # load Whisper now so the first sentence isn't slow
    port = lamp.s.get("serial_port")
    if port and any(p["device"] == port for p in SerialLink.ports(lamp.s.get("wifi_host", "192.168.4.1"))) and not port.startswith("wifi://"):
        try:
            link.connect(port)
            lamp.resync()
        except Exception as e:
            print("Could not reconnect to", port, "-", e)
    print("\n  Halo Jr. control app:  http://localhost:5050\n")
    app.run(host="127.0.0.1", port=5050, threaded=True, debug=False)
