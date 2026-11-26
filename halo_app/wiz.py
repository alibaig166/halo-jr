"""Philips WiZ bulb over the local Wi-Fi (UDP port 38899). No API key, no cloud.

The bulb and this computer must be on the same Wi-Fi, and "Allow local communication"
must be ON in the WiZ app (Settings > Security).
"""
import json
import socket
import threading

PORT = 38899


class WizBulb:
    def __init__(self):
        self.ip = ""
        self.relay = None          # set by the lamp: send through the ESP32 instead of this computer's Wi-Fi
        self.error = ""
        self._lock = threading.Lock()

    # ---------- low level ----------
    def _send(self, msg, wait=0.0):
        """Send one JSON message to the bulb. With wait > 0, return its reply (or None)."""
        if not self.ip:
            self.error = "no bulb IP set"
            return None
        data = json.dumps(msg).encode()
        with self._lock:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.settimeout(wait or 0.3)
                for _ in range(2 if not wait else 3):          # UDP can drop a packet, so send twice
                    s.sendto(data, (self.ip, PORT))
                    if wait:
                        try:
                            reply, _ = s.recvfrom(2048)
                            self.error = ""
                            return json.loads(reply.decode())
                        except socket.timeout:
                            continue
                self.error = "" if not wait else "bulb did not answer"
            except OSError as e:
                self.error = str(e)
            finally:
                s.close()
        return None

    def _bg(self, msg):
        if self.relay:
            self.relay(msg)
            return
        threading.Thread(target=self._send, args=(msg,), daemon=True).start()

    # ---------- what the app uses ----------
    def apply(self, on, brightness_255, mode="temp", temp=2700, color="#ffb060"):
        """Turn the bulb on/off and set brightness plus warm/cool white or a colour."""
        if not self.ip:
            return
        if not on:
            self._bg({"method": "setPilot", "params": {"state": False}})
            return
        p = {"state": True, "dimming": max(10, min(100, round(brightness_255 / 255 * 100)))}
        if mode == "color":
            c = color.lstrip("#")
            p.update(r=int(c[0:2], 16), g=int(c[2:4], 16), b=int(c[4:6], 16))
        else:
            p["temp"] = int(max(2200, min(6500, temp)))
        self._bg({"method": "setPilot", "params": p})

    def test(self):
        """Ask the bulb for its state. Returns True if it answered."""
        return self._send({"method": "getPilot", "params": {}}, wait=0.8) is not None

    @staticmethod
    def find(timeout=2.5):
        """Shout on the Wi-Fi and list every WiZ bulb that answers: [{'ip':..., 'mac':...}]."""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.settimeout(0.4)
        found = {}
        msg = json.dumps({"method": "getPilot", "params": {}}).encode()
        try:
            import time
            end = time.time() + timeout
            while time.time() < end:
                try:
                    s.sendto(msg, ("255.255.255.255", PORT))
                except OSError:
                    break
                try:
                    while True:
                        reply, addr = s.recvfrom(2048)
                        try:
                            mac = json.loads(reply.decode()).get("result", {}).get("mac", "")
                        except ValueError:
                            mac = ""
                        found[addr[0]] = mac
                except socket.timeout:
                    pass
        finally:
            s.close()
        return [{"ip": ip, "mac": mac} for ip, mac in found.items()]
