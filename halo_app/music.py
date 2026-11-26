"""Spotify for Halo: "Hey GWM, play Blinding Lights" / "pause" / "next song" / "volume 40".

Online: Spotify Web API remote-controls the Spotify app on this laptop (Spotify Premium needed).
Offline: songs from your saved playlists/liked songs (synced once while online) are opened in the
Spotify app directly, and pause/next/previous use the media keys. On a Mac it uses AppleScript.

One-time setup (developer.spotify.com/dashboard):
  1. Create app -> Redirect URI: http://127.0.0.1:5050/spotify/callback -> tick "Web API" -> Save.
  2. Copy the Client ID into the app's Talk tab -> Music -> Connect Spotify, and log in.
"""
import base64
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(HERE, "spotify_token.json")     # your login (not in git)
CACHE_FILE = os.path.join(HERE, "spotify_library.json")   # your songs for offline use (not in git)
REDIRECT = "http://127.0.0.1:5050/spotify/callback"
SCOPES = ("user-modify-playback-state user-read-playback-state user-read-currently-playing "
          "playlist-read-private playlist-read-collaborative user-library-read")
API = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"


class Music:
    def __init__(self, lamp):
        self.lamp = lamp
        self.tok = self._load(TOKEN_FILE) or {}
        self.lib = self._load(CACHE_FILE) or {"tracks": [], "playlists": [], "synced": 0}
        self.verifier = None
        self.ducked = None          # volume before Halo lowered it to listen/talk
        self.lock = threading.Lock()
        self.last_error = ""

    # ---------- files ----------
    @staticmethod
    def _load(path):
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    @staticmethod
    def _save(path, data):
        with open(path + ".tmp", "w") as f:
            json.dump(data, f)
        os.replace(path + ".tmp", path)

    @property
    def client_id(self):
        return self.lamp.s.get("spotify", {}).get("client_id", "").strip()

    # ---------- login (PKCE: no client secret needed) ----------
    def login_url(self):
        if not self.client_id:
            raise RuntimeError("Paste your Spotify Client ID first (developer.spotify.com/dashboard).")
        self.verifier = secrets.token_urlsafe(64)[:96]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).decode().rstrip("=")
        return "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
            "client_id": self.client_id, "response_type": "code", "redirect_uri": REDIRECT,
            "scope": SCOPES, "code_challenge_method": "S256", "code_challenge": challenge})

    def _token_call(self, form):
        req = urllib.request.Request(TOKEN_URL, data=urllib.parse.urlencode(form).encode(),
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=10) as r:
            t = json.load(r)
        t["expires_at"] = time.time() + t.get("expires_in", 3600) - 60
        if "refresh_token" not in t and self.tok.get("refresh_token"):
            t["refresh_token"] = self.tok["refresh_token"]
        self.tok = t
        self._save(TOKEN_FILE, t)

    def finish_login(self, code):
        if not self.verifier:
            raise RuntimeError("Login expired, press Connect Spotify again.")
        self._token_call({"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
                          "client_id": self.client_id, "code_verifier": self.verifier})
        self.verifier = None
        threading.Thread(target=self.sync, daemon=True).start()     # save your songs for offline right away

    def logout(self):
        self.tok = {}
        try:
            os.remove(TOKEN_FILE)
        except OSError:
            pass

    @property
    def logged_in(self):
        return bool(self.tok.get("refresh_token"))

    # ---------- Web API ----------
    def _api(self, method, path, body=None, query=None):
        if not self.logged_in:
            raise RuntimeError("Spotify isn't connected yet (Talk tab -> Music -> Connect Spotify).")
        if time.time() > self.tok.get("expires_at", 0):
            self._token_call({"grant_type": "refresh_token", "refresh_token": self.tok["refresh_token"],
                              "client_id": self.client_id})
        url = API + path + ("?" + urllib.parse.urlencode(query) if query else "")
        req = urllib.request.Request(url, method=method, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Authorization": "Bearer " + self.tok["access_token"],
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="ignore")
            if e.code == 404 and "NO_ACTIVE_DEVICE" in msg:
                raise RuntimeError("NO_DEVICE")
            if e.code == 403 and "PREMIUM" in msg.upper():
                raise RuntimeError("Spotify Premium is needed to control playback.")
            raise RuntimeError("Spotify error %d: %s" % (e.code, msg[:160]))

    def _device(self):
        """The Spotify app on this laptop (or whatever is active)."""
        devs = self._api("GET", "/me/player/devices").get("devices", [])
        if not devs:
            raise RuntimeError("Open the Spotify app on this laptop first (it must be running).")
        active = [d for d in devs if d.get("is_active")]
        comp = [d for d in devs if d.get("type") == "Computer"]
        return (active or comp or devs)[0]["id"]

    def _play_online(self, query, kind):
        kinds = {"song": "track", "track": "track", "artist": "artist", "album": "album", "playlist": "playlist"}
        k = kinds.get((kind or "song").lower(), "track")
        if k == "playlist":                                   # your own playlists first
            mine = self._match(query, self.lib.get("playlists", []))
            if mine:
                return self._start({"context_uri": mine["uri"]}, mine["name"])
        if k == "track":                                      # your saved songs first, then all of Spotify
            mine = self._match(query, self.lib.get("tracks", []), loose=False)
            if mine:
                return self._start({"uris": [mine["uri"]]}, "%s by %s" % (mine["name"], mine["artist"]))
        q = query
        song, sep, artist = query.lower().partition(" by ")
        if sep and k in ("track", "album"):
            q = "%s:%s artist:%s" % (k, song.strip(), artist.strip())
        res = self._api("GET", "/search", query={"q": q, "type": k, "limit": 1})
        items = res.get(k + "s", {}).get("items", [])
        if not items:
            return "I couldn't find %s on Spotify." % query
        it = items[0]
        if k == "track":
            return self._start({"uris": [it["uri"]]}, "%s by %s" % (it["name"], it["artists"][0]["name"]))
        return self._start({"context_uri": it["uri"]}, it["name"])

    def _start(self, body, what):
        try:
            self._api("PUT", "/me/player/play", body, {"device_id": self._device()})
        except RuntimeError as e:
            if str(e) != "NO_DEVICE":
                raise
            raise RuntimeError("Open the Spotify app on this laptop first (it must be running).")
        return "Playing " + what + "."

    # ---------- your library, saved for offline ----------
    def sync(self):
        """Save your liked songs and playlists (name -> Spotify ID). Needs internet; run at home."""
        tracks, playlists = [], []
        nxt = "/me/tracks"
        q = {"limit": 50}
        while nxt and len(tracks) < 2000:
            r = self._api("GET", nxt, query=q)
            for it in r.get("items", []):
                t = it.get("track") or {}
                if t.get("uri"):
                    tracks.append({"name": t["name"], "artist": (t.get("artists") or [{}])[0].get("name", ""), "uri": t["uri"]})
            nxt, q = self._next(r)
        nxt, q = "/me/playlists", {"limit": 50}
        while nxt and len(playlists) < 300:
            r = self._api("GET", nxt, query=q)
            for p in r.get("items", []):
                if p and p.get("uri"):
                    playlists.append({"name": p["name"], "uri": p["uri"], "id": p["id"]})
            nxt, q = self._next(r)
        for p in playlists[:40]:                                   # songs inside your first 40 playlists too
            nxt, q = "/playlists/%s/tracks" % p["id"], {"limit": 100, "fields": "items(track(name,uri,artists(name))),next"}
            while nxt and len(tracks) < 5000:
                r = self._api("GET", nxt, query=q)
                for it in r.get("items", []):
                    t = (it or {}).get("track") or {}
                    if t.get("uri", "").startswith("spotify:track:"):
                        tracks.append({"name": t["name"], "artist": (t.get("artists") or [{}])[0].get("name", ""), "uri": t["uri"]})
                nxt, q = self._next(r)
        seen, uniq = set(), []
        for t in tracks:
            if t["uri"] not in seen:
                seen.add(t["uri"])
                uniq.append(t)
        self.lib = {"tracks": uniq, "playlists": playlists, "synced": time.time()}
        self._save(CACHE_FILE, self.lib)
        return len(uniq), len(playlists)

    @staticmethod
    def _next(r):
        n = r.get("next")
        return (n.replace(API, ""), None) if n else (None, None)

    @staticmethod
    def _match(query, items, loose=True):
        """Find 'blinding lights', 'blinding lights by the weeknd', 'the weeknd' in your library."""
        q = re.sub(r"[^a-z0-9 ]", "", query.lower()).strip()
        if not q:
            return None
        song, _, artist = q.partition(" by ")
        best, score = None, 0
        for it in items:
            name = re.sub(r"[^a-z0-9 ]", "", it["name"].lower())
            art = re.sub(r"[^a-z0-9 ]", "", it.get("artist", "").lower())
            s = 0
            if name == song:
                s = 3
            elif song and song in name:
                s = 2
            elif loose and song and all(w in name for w in song.split()):
                s = 1
            if s and artist:
                s += 1 if artist in art else -2
            if s > score:
                best, score = it, s
        return best

    # ---------- offline control of the Spotify app ----------
    def _open_uri(self, uri):
        if sys.platform == "darwin":
            subprocess.run(["osascript", "-e", 'tell application "Spotify" to play track "%s"' % uri], timeout=5)
        elif os.name == "nt":
            os.startfile(uri)                       # opens it in the Spotify app
            time.sleep(1.5)
            self._key("play")
        else:
            subprocess.Popen(["xdg-open", uri])

    def _key(self, action):
        if sys.platform == "darwin":
            cmd = {"pause": "pause", "resume": "play", "play": "play", "next": "next track", "previous": "previous track"}[action]
            subprocess.run(["osascript", "-e", 'tell application "Spotify" to %s' % cmd], timeout=5)
        elif os.name == "nt":
            import ctypes
            vk = {"pause": 0xB3, "resume": 0xB3, "play": 0xB3, "next": 0xB0, "previous": 0xB1}[action]
            ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
            ctypes.windll.user32.keybd_event(vk, 0, 2, 0)
        else:
            raise RuntimeError("offline control isn't supported on this computer")

    # ---------- what Halo calls ----------
    def play(self, query, kind="song"):
        try:
            return self._play_online(query, kind)
        except (urllib.error.URLError, OSError, TimeoutError) as e:        # no internet: use the saved library
            self.last_error = str(e)
        except RuntimeError as e:
            if "connected yet" not in str(e) or not self.lib.get("tracks"):
                raise
        items = self.lib.get("playlists", []) if kind == "playlist" else self.lib.get("tracks", [])
        it = self._match(query, items)
        if not it:
            return "No internet, and %s isn't in your saved songs." % query
        try:
            self._open_uri(it["uri"])
        except (OSError, subprocess.SubprocessError) as e:
            return "I couldn't open the Spotify app (%s). Is it installed?" % e
        return "Playing %s%s from your downloads." % (it["name"], " by " + it["artist"] if it.get("artist") else "")

    def control(self, action):
        action = {"stop": "pause", "skip": "next", "back": "previous", "play": "resume", "continue": "resume"}.get(action, action)
        try:
            dev = {"device_id": self._device()}
            if action == "pause":
                self._api("PUT", "/me/player/pause", query=dev)
            elif action == "resume":
                self._api("PUT", "/me/player/play", query=dev)
            elif action == "next":
                self._api("POST", "/me/player/next", query=dev)
            elif action == "previous":
                self._api("POST", "/me/player/previous", query=dev)
            else:
                return "I can pause, resume, skip or go back."
        except (urllib.error.URLError, OSError, TimeoutError, RuntimeError) as e:
            if isinstance(e, RuntimeError) and "Premium" in str(e):
                raise
            try:
                self._key(action)                      # offline: media keys / AppleScript
            except (OSError, RuntimeError, subprocess.SubprocessError) as e2:
                raise RuntimeError("Spotify isn't reachable (%s)" % e2)
        return {"pause": "Paused.", "resume": "Playing.", "next": "Next song.", "previous": "Going back."}[action]

    def volume(self, pct):
        pct = int(max(0, min(100, pct)))
        self._api("PUT", "/me/player/volume", query={"volume_percent": pct})
        return "Volume %d." % pct

    def now(self):
        try:
            r = self._api("GET", "/me/player/currently-playing")
        except (urllib.error.URLError, OSError, RuntimeError):
            return None
        it = (r or {}).get("item")
        if not it:
            return None
        return {"name": it["name"], "artist": it["artists"][0]["name"], "playing": r.get("is_playing", False)}

    def duck(self, on):
        """Turn the music down while Halo listens or talks, and back up after."""
        if not self.logged_in:
            return
        with self.lock:
            try:
                if on and self.ducked is None:
                    st = self._api("GET", "/me/player")
                    if st and st.get("is_playing") and st.get("device", {}).get("volume_percent") is not None:
                        self.ducked = st["device"]["volume_percent"]
                        self._api("PUT", "/me/player/volume", query={"volume_percent": max(5, self.ducked // 4)})
                elif not on and self.ducked is not None:
                    v, self.ducked = self.ducked, None
                    self._api("PUT", "/me/player/volume", query={"volume_percent": v})
            except (urllib.error.URLError, OSError, RuntimeError):
                self.ducked = None if not on else self.ducked

    def status(self):
        return {"client_id": bool(self.client_id), "logged_in": self.logged_in,
                "saved_songs": len(self.lib.get("tracks", [])), "saved_playlists": len(self.lib.get("playlists", [])),
                "synced": self.lib.get("synced", 0)}
