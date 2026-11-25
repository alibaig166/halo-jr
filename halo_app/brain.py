"""Halo's brain: a local LLM through Ollama (http://localhost:11434), no API key.

The model answers in short spoken sentences and can move the lamp through "tools"
(level, move, light, colour, look, iris). Simple commands like "level 3" or
"lights off" are handled instantly without asking the model.
"""
import json
import re
import threading
import urllib.error
import urllib.request

SYSTEM = (
    "You are Halo Jr., a small robot desk lamp with a personality: curious, cheerful and a bit cheeky. "
    "Your owner talks to you by voice and your answer is read out loud, so reply in one or two short "
    "spoken sentences, no lists, no emojis, no markdown. "
    "You have a body: use the tools to move when it fits (nod when you agree, shake for no, "
    "look around when curious, change your light when asked). Levels go from 1 (low, folded) to 7 (tall). "
    "Never invent tools. If you cannot do something, say so briefly."
)

TOOLS = [
    {"type": "function", "function": {
        "name": "set_level", "description": "Move the lamp arm to a height level from 1 (lowest) to 7 (highest).",
        "parameters": {"type": "object", "properties": {"level": {"type": "integer", "description": "1 to 7"}}, "required": ["level"]}}},
    {"type": "function", "function": {
        "name": "do_move", "description": "Play a body animation.",
        "parameters": {"type": "object", "properties": {"name": {"type": "string",
                       "enum": ["wake", "sleep", "nod", "shake", "look", "stretch"]}}, "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "light", "description": "Turn the lamp light on or off and optionally set brightness in percent.",
        "parameters": {"type": "object", "properties": {"on": {"type": "boolean"},
                       "brightness": {"type": "integer", "description": "0 to 100"}}, "required": ["on"]}}},
    {"type": "function", "function": {
        "name": "set_color", "description": "Set the bulb colour. Use 'warm' or 'cool' for white light, or a colour name or hex like #ff0000.",
        "parameters": {"type": "object", "properties": {"color": {"type": "string"}}, "required": ["color"]}}},
    {"type": "function", "function": {
        "name": "turn", "description": "Turn the lamp left or right. Degrees from -90 (left) to 90 (right), 0 = straight ahead.",
        "parameters": {"type": "object", "properties": {"degrees": {"type": "integer"}}, "required": ["degrees"]}}},
    {"type": "function", "function": {
        "name": "iris", "description": "Open or close the iris on the lamp head.",
        "parameters": {"type": "object", "properties": {"open": {"type": "boolean"}}, "required": ["open"]}}},
]

COLORS = {"red": "#ff0000", "green": "#00ff40", "blue": "#0040ff", "purple": "#a020f0", "pink": "#ff4fa0",
          "orange": "#ff7a00", "yellow": "#ffd000", "white": "#ffffff", "cyan": "#00e5ff", "teal": "#00b3a0"}
WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7}


class Brain:
    def __init__(self, lamp):
        self.lamp = lamp
        self.history = []
        self.busy = False
        self.lock = threading.Lock()

    # ---------- settings ----------
    @property
    def cfg(self):
        return self.lamp.s.get("llm", {})

    def models(self):
        """Models installed in Ollama, or an error string."""
        try:
            with urllib.request.urlopen(self.cfg.get("url", "http://localhost:11434") + "/api/tags", timeout=3) as r:
                return [m["name"] for m in json.load(r).get("models", [])]
        except (urllib.error.URLError, OSError, ValueError) as e:
            return "Ollama is not running (%s). Open the Ollama app or run: ollama serve" % getattr(e, "reason", e)

    # ---------- lamp actions ----------
    def run_tool(self, name, a):
        L = self.lamp
        try:
            if name == "set_level":
                n = int(a.get("level", 1))
                n = max(1, min(len(L.s["levels"]), n))
                L.stop_move()
                L.target["trim"] = 0.0              # undo any half-finished nod
                L.level(n)
                return "moved to level %d" % n
            if name == "do_move":
                if a.get("name") in L.moves():
                    L.run_move(a["name"])
                    return "doing " + a["name"]
                return "unknown move"
            if name == "light":
                b = a.get("brightness")
                L.light(on=bool(a.get("on", True)), brightness=None if b is None else int(b) * 255 // 100)
                return "light " + ("on" if L.light_on else "off")
            if name == "set_color":
                c = str(a.get("color", "")).lower().strip()
                w = L.s.setdefault("wiz", {})
                if c in ("warm", "warm white"):
                    w.update(mode="temp", temp=2700)
                elif c in ("cool", "cold", "cool white", "daylight"):
                    w.update(mode="temp", temp=6000)
                elif c in COLORS or re.fullmatch(r"#[0-9a-f]{6}", c):
                    w.update(mode="color", color=COLORS.get(c, c))
                else:
                    return "unknown colour"
                L.light(on=True)
                return "colour set to " + c
            if name == "turn":
                d = max(-90, min(90, int(a.get("degrees", 0))))
                L.stop_move()
                L.go({"yaw": d})
                return "turned to %d" % d
            if name == "iris":
                L.iris(bool(a.get("open", True)))
                return "iris " + ("open" if a.get("open", True) else "closed")
        except (TypeError, ValueError) as e:
            return "error: %s" % e
        return "unknown tool"

    def quick(self, text):
        """Instant commands without the LLM. Returns (reply, actions) or None."""
        t = text.lower()
        m = re.search(r"\blevel\s+(\d|one|two|three|four|five|six|seven)\b", t)
        if m:
            g = m.group(1)
            n = int(g) if g.isdigit() else WORDS[g]
            return "Level %d." % n, [self.run_tool("set_level", {"level": n})]
        if re.search(r"\b(lights?|lamp)\s+(off)\b|\bturn (the )?lights? off\b|\bturn off (the )?lights?\b", t):
            return "Lights off.", [self.run_tool("light", {"on": False})]
        if re.search(r"\b(lights?|lamp)\s+on\b|\bturn (the )?lights? on\b|\bturn on (the )?lights?\b", t):
            return "Lights on.", [self.run_tool("light", {"on": True})]
        for mv in ("nod", "shake", "stretch", "sleep", "wake"):
            if re.fullmatch(r"(please )?(%s)( up)?( please)?[.!]?" % mv, t.strip()):
                return {"sleep": "Goodnight.", "wake": "I'm up!"}.get(mv, "Okay."), [self.run_tool("do_move", {"name": mv})]
        return None

    # ---------- talking ----------
    def _ollama(self, messages, tools=True):
        body = {"model": self.cfg.get("model", "qwen2.5:3b"), "messages": messages, "stream": False,
                "options": {"temperature": 0.7, "num_predict": 160}}
        if tools:
            body["tools"] = TOOLS
        req = urllib.request.Request(self.cfg.get("url", "http://localhost:11434") + "/api/chat",
                                     data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.load(r)["message"]
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="ignore")
            if tools and "does not support tools" in msg:      # older models: talk only, no body control
                return self._ollama(messages, tools=False)
            if "not found" in msg:
                raise RuntimeError("Model %s is not downloaded. In Terminal run: ollama pull %s" % (body["model"], body["model"]))
            raise RuntimeError("Ollama error: " + msg[:200])
        except (urllib.error.URLError, OSError) as e:
            raise RuntimeError("Can't reach Ollama. Open the Ollama app (or run: ollama serve). %s" % getattr(e, "reason", e))

    def chat(self, text):
        """Returns {"reply": str, "actions": [str]}."""
        text = (text or "").strip()
        if not text:
            return {"reply": "", "actions": []}
        q = self.quick(text)
        if q:
            self.history += [{"role": "user", "content": text}, {"role": "assistant", "content": q[0]}]
            self.history = self.history[-12:]
            return {"reply": q[0], "actions": q[1]}
        with self.lock:
            self.busy = True
            try:
                msgs = [{"role": "system", "content": self.cfg.get("persona") or SYSTEM}] + self.history + [{"role": "user", "content": text}]
                actions = []
                reply = ""
                for _ in range(3):                                 # model -> tools -> model, at most 3 rounds
                    m = self._ollama(msgs)
                    calls = m.get("tool_calls") or []
                    if m.get("content"):
                        reply = m["content"].strip()
                    if not calls:
                        break
                    msgs.append(m)
                    for c in calls:
                        f = c.get("function", {})
                        args = f.get("arguments") or {}
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except ValueError:
                                args = {}
                        res = self.run_tool(f.get("name", ""), args)
                        actions.append(res)
                        msgs.append({"role": "tool", "content": res})
                    if reply:                                       # it already said something: don't wait for a 2nd answer
                        break
                reply = re.sub(r"[*_#`]", "", reply) or ("Done." if actions else "Hmm.")
                self.history += [{"role": "user", "content": text}, {"role": "assistant", "content": reply}]
                self.history = self.history[-12:]
                return {"reply": reply, "actions": actions}
            finally:
                self.busy = False

    def forget(self):
        self.history = []
