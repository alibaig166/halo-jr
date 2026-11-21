"""Camera: face follow (camera pan/tilt servos ch5/ch6) and hand gestures, using the MediaPipe models from the PAI-PROJECT repo."""
import math
import os
import threading
import time

os.environ.setdefault("GLOG_minloglevel", "2")
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")

try:
    import cv2
except ImportError:
    cv2 = None
try:
    import mediapipe as mp
    from mediapipe.tasks import python as mp_python
    from mediapipe.tasks.python import vision as mp_vision
except ImportError:
    mp = None

HERE = os.path.dirname(os.path.abspath(__file__))
FACE_MODEL = os.path.join(HERE, "models", "blaze_face_short_range.tflite")
GESTURE_MODEL = os.path.join(HERE, "models", "gesture_recognizer.task")

HOLD_TIME = 0.6        # hold a hand sign this long to trigger it
SIGN_COOLDOWN = 2.0


def _dist(a, b):
    return math.hypot(a.x - b.x, a.y - b.y)


def get_sign(hand):
    """Same finger logic as Hand.ipynb."""
    thumb = _dist(hand[4], hand[17]) > _dist(hand[3], hand[17])
    index = _dist(hand[8], hand[0]) > _dist(hand[6], hand[0])
    middle = _dist(hand[12], hand[0]) > _dist(hand[10], hand[0])
    ring = _dist(hand[16], hand[0]) > _dist(hand[14], hand[0])
    pinky = _dist(hand[20], hand[0]) > _dist(hand[18], hand[0])
    fingers = [thumb, index, middle, ring, pinky]
    if index and middle and not ring and not pinky:
        return "Beam"
    if index and not middle and not ring and not pinky:
        return "Point"
    if fingers == [True] * 5:
        return "Open_Palm"
    if fingers == [False] * 5:
        return "Fist"
    if fingers == [True, False, False, False, False]:
        return "Thumbs_Up" if hand[4].y < hand[2].y else "Thumbs_Down"
    return "None"


class Vision:
    def __init__(self, lamp):
        self.lamp = lamp
        self.cap = None
        self.cam_index = None
        self.running = False
        self.follow = False
        self.last_ts = 0
        self.gestures = False
        self.jpeg = None
        self.info = {"camera": "off", "face": None, "sign": "None", "fps": 0, "last_action": ""}
        self.error = ""
        self.lock = threading.Lock()
        self.face_det = None
        self.gesture_rec = None
        self.thread = None

    @property
    def available(self):
        return cv2 is not None

    @property
    def ai_available(self):
        return mp is not None and os.path.exists(FACE_MODEL) and os.path.exists(GESTURE_MODEL)

    def start(self, index):
        self.stop()
        if cv2 is None:
            self.error = "OpenCV not installed (pip install opencv-python)"
            return False
        cap = cv2.VideoCapture(int(index))
        ok = cap.isOpened()
        if ok:
            for _ in range(30):
                ok, _ = cap.read()
                if ok:
                    break
                time.sleep(0.05)
        if not ok:
            cap.release()
            self.error = "Camera %s did not open. Try another number, and allow camera access for Terminal in System Settings > Privacy & Security > Camera." % index
            return False
        self.error = ""
        self.cap, self.cam_index = cap, int(index)
        if self.ai_available and self.face_det is None:
            self.face_det = mp_vision.FaceDetector.create_from_options(mp_vision.FaceDetectorOptions(
                base_options=mp_python.BaseOptions(model_asset_path=FACE_MODEL),
                running_mode=mp_vision.RunningMode.VIDEO, min_detection_confidence=0.4))
            self.gesture_rec = mp_vision.GestureRecognizer.create_from_options(mp_vision.GestureRecognizerOptions(
                base_options=mp_python.BaseOptions(model_asset_path=GESTURE_MODEL),
                running_mode=mp_vision.RunningMode.VIDEO, num_hands=1))
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        return True

    def stop(self):
        self.running = False
        if self.thread is not None:
            self.thread.join(timeout=2)
        self.thread = None
        if self.cap is not None:
            self.cap.release()
        self.cap = None
        self.jpeg = None
        self.follow = False
        self.info["camera"] = "off"

    def _act(self, action):
        lamp = self.lamp
        self.info["last_action"] = action
        if action == "light":
            lamp.light(on=not lamp.light_on)
        elif action == "sleep":
            lamp.run_move("wake" if lamp.status == "ready" and lamp.target["trim"] <= -30 else "sleep")
        elif action in lamp.moves():
            lamp.run_move(action)

    def _loop(self):
        start = time.time()
        last = time.time()
        fx = fy = None
        held, held_since, fired, last_fire = "None", 0.0, False, 0.0
        frames, fps_t = 0, time.time()
        frame_no = 0
        while self.running:
            ok, frame = self.cap.read()
            if not ok:
                self.error = "Camera stopped sending frames"
                break
            frame = cv2.flip(frame, 1)
            h, w = frame.shape[:2]
            now = time.time()
            dt = min(now - last, 0.1)
            last = now
            frame_no += 1
            # MediaPipe needs timestamps that only go up, even after switching cameras
            ts = max(int(time.monotonic() * 1000), self.last_ts + 1)
            self.last_ts = ts
            T = self.lamp.s["tracking"]
            face = None

            if self.face_det is not None:
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                try:
                    res = self.face_det.detect_for_video(img, ts)
                except ValueError:
                    continue
                if res.detections:
                    box = max(res.detections, key=lambda d: d.bounding_box.width * d.bounding_box.height).bounding_box
                    cx, cy = box.origin_x + box.width / 2, box.origin_y + box.height / 2
                    a = T["smooth"]
                    fx, fy = (cx, cy) if fx is None else (a * fx + (1 - a) * cx, a * fy + (1 - a) * cy)
                    ex, ey = fx / w - 0.5, fy / h - 0.5
                    face = {"x": round(ex, 3), "y": round(ey, 3)}
                    cv2.rectangle(frame, (box.origin_x, box.origin_y), (box.origin_x + box.width, box.origin_y + box.height), (46, 138, 242), 2)
                    cv2.line(frame, (w // 2, h // 2), (int(fx), int(fy)), (46, 138, 242), 1)
                    if self.follow:
                        dz = T["deadzone"]
                        ox = ex * T["fov_x"] if abs(ex) > dz else 0.0
                        oy = ey * T["fov_y"] if abs(ey) > dz else 0.0
                        lim = min(T["max_speed"], self.lamp.speed_of("camera")) * dt
                        mx = max(-lim, min(lim, ox * T["gain"] * dt))
                        my = max(-lim, min(lim, oy * T["gain"] * dt))
                        if mx or my:
                            # same as face_follow_test.py: move the two camera servos (ch5 pan, ch6 tilt)
                            sm = self.lamp.small
                            tlo, thi = self.lamp.s["small"].get("cam_tilt_range", [30, 150])
                            pan = max(0, min(180, sm["cam_pan"] + (-mx if T["pan_invert"] else mx)))
                            tilt = max(tlo, min(thi, sm["cam_tilt"] + (-my if T["tilt_invert"] else my)))
                            self.lamp.set_small("cam_pan", pan, ms=60)
                            self.lamp.set_small("cam_tilt", tilt, ms=60)
                else:
                    fx = fy = None

                if self.gestures and self.gesture_rec is not None and frame_no % 2 == 0:
                    try:
                        g = self.gesture_rec.recognize_for_video(img, ts)
                    except ValueError:
                        g = None
                    if g is None:
                        continue
                    sign = get_sign(g.hand_landmarks[0]) if g.hand_landmarks else "None"
                    self.info["sign"] = sign
                    if sign != held:
                        held, held_since, fired = sign, now, False
                    action = self.lamp.s["gestures"].get(sign)
                    if action and not fired:
                        prog = min(1.0, (now - held_since) / HOLD_TIME)
                        cv2.rectangle(frame, (10, h - 40), (10 + int(200 * prog), h - 28), (46, 138, 242), -1)
                        if prog >= 1 and now - last_fire >= SIGN_COOLDOWN:
                            fired, last_fire = True, now
                            self._act(action)
                elif not self.gestures:
                    self.info["sign"] = "None"

            dzw, dzh = int(T["deadzone"] * w), int(T["deadzone"] * h)
            cv2.rectangle(frame, (w // 2 - dzw, h // 2 - dzh), (w // 2 + dzw, h // 2 + dzh), (220, 220, 220), 1)
            if self.gestures:
                cv2.putText(frame, "Sign: " + self.info["sign"], (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (46, 138, 242), 2)

            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
            if ok:
                with self.lock:
                    self.jpeg = buf.tobytes()
            frames += 1
            if now - fps_t >= 1:
                self.info["fps"] = frames
                frames, fps_t = 0, now
            self.info["face"] = face
            self.info["camera"] = "on"
        self.info["camera"] = "off"

    def frame(self):
        with self.lock:
            return self.jpeg

    def state(self):
        return {**self.info, "follow": self.follow, "gestures": self.gestures, "index": self.cam_index,
                "error": self.error, "ai": self.ai_available, "opencv": self.available}
