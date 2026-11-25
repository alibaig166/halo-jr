# Halo Jr. Control

One web page that controls the whole lamp: 3D model, levels, moves, single-servo test,
light, settings, face follow and hand gestures. The ESP32 code is uploaded **once**; after
that every change happens in the page and is saved to `settings.json`.

```
Browser (localhost:5050)  <->  Python on the Mac (server.py)  <- USB ->  ESP32  ->  PCA9685  ->  servos
                                       ^
                                     webcam
```

## 1. Upload the ESP32 code (once)
1. Open `firmware/HaloJr_firmware/HaloJr_firmware.ino` in Arduino IDE.
2. Board **ESP32 Dev Module**, port **/dev/cu.usbserial-…**, Upload Speed **115200**.
3. Upload, then **close the Arduino IDE Serial Monitor** (only one program can use the port).

## 2. Install Python packages (once)
In Terminal, inside this folder:
```
pip3 install -r requirements.txt
```
(MediaPipe needs Python 3.9–3.12 on some Macs. If it fails, use the conda env you already use for PAI-PROJECT.)

## 3. Run
```
python3 server.py
```
Open **http://localhost:5050** in Chrome, pick the `/dev/cu.usbserial-…` port, click **Connect**.

## Wiring (same as before)
| PCA9685 | Connect to |
|---|---|
| VCC / GND | ESP32 3V3 / GND |
| SDA / SCL | ESP32 D21 / D22 |
| V+ | 6.5 V buck + (thick wire), buck − to GND |
| ch0 | base turn |
| ch2 | upper arm |
| ch4 | push rod |
| ch6 | head |
| ch8 | iris (MG90S) |
| ch10 | camera left/right (MG90S) |
| ch12 | camera up/down (MG90S) |

Optional light: logic-level MOSFET gate on ESP32 **GPIO 25** (brightness from the page).

## Camera
Camera tab → pick the camera number (Mac: 0 built-in, 1 USB webcam) → Start camera.
The first time, macOS asks to allow camera access for Terminal (System Settings → Privacy & Security → Camera).

- **Follow my face**: the two camera servos (ch5 left/right, ch6 up/down) keep your face in the middle.
- **Hand gestures** (hold still ~0.5 s): open palm = light on/off, thumbs up = nod,
  thumbs down = shake, fist = sleep / wake.

Face and gesture code come from the PAI-PROJECT `Testers/face_follow_test.py` and `Hand.ipynb`.

## Talk to Halo (local AI with Ollama)
1. Install Ollama from **ollama.com** and open it.
2. In Terminal: `ollama pull qwen2.5:3b` (about 2 GB, once).
3. Page → **Talk** tab: hold the button (or Space) and speak, or turn on **"Hey Halo"**.
Halo answers out loud and can move itself, change the light and the WiZ colour.
Voice recognition: **Whisper** runs on your laptop with no internet. Install it once with
`pip install -r requirements-voice.txt` and open the Talk tab once with internet so the model downloads
(base is about 140 MB). If Whisper isn't installed the page falls back to Chrome's recognition (needs internet).

## Files
| File | What it does |
|---|---|
| `server.py` | web server and API |
| `lamp.py` | lamp maths, levels, moves, settings, USB link (OOP: `Lamp`, `SerialLink`) |
| `vision.py` | camera, face follow, gestures (`Vision`) |
| `settings.json` | everything you set in the page |
| `static/index.html` | the control page |
| `static/bench.html` | the 3D lamp |
| `firmware/` | ESP32 code |
