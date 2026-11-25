"""Offline speech-to-text with Whisper (faster-whisper), running on this laptop.

Install once:  pip install -r requirements-voice.txt
The first time a model is used it is downloaded (base.en is about 140 MB), so do that once with internet.
After that it works with no internet at all.
"""
import threading

import numpy as np

try:
    from faster_whisper import WhisperModel
except ImportError:          # voice package not installed: the app still runs, Chrome recognition still works
    WhisperModel = None


class Ears:
    def __init__(self, lamp):
        self.lamp = lamp
        self.model = None
        self.model_name = None
        self.loading = False
        self.error = ""
        self.lock = threading.Lock()

    @property
    def cfg(self):
        return self.lamp.s.get("llm", {})

    def _name(self):
        name = self.cfg.get("whisper_model", "base.en")
        if not self.cfg.get("lang", "en-US").startswith("en"):
            name = name.replace(".en", "")              # Urdu etc. need the multilingual model
        return name

    def status(self):
        if WhisperModel is None:
            return {"installed": False, "ready": False,
                    "error": "Whisper is not installed. In the terminal: pip install -r requirements-voice.txt"}
        return {"installed": True, "ready": self.model is not None and self.model_name == self._name(),
                "loading": self.loading, "model": self._name(), "error": self.error}

    def load(self):
        """Load (and the first time download) the model. Safe to call many times."""
        if WhisperModel is None:
            raise RuntimeError(self.status()["error"])
        name = self._name()
        with self.lock:
            if self.model is not None and self.model_name == name:
                return
            self.loading, self.error = True, ""
            try:
                self.model = WhisperModel(name, device="cpu", compute_type="int8")
                self.model_name = name
            except Exception as e:                       # usually: no internet on the very first download
                self.error = "Could not load Whisper %s: %s" % (name, e)
                raise RuntimeError(self.error)
            finally:
                self.loading = False

    def preload(self):
        threading.Thread(target=lambda: self._try_load(), daemon=True).start()

    def _try_load(self):
        try:
            self.load()
        except RuntimeError:
            pass

    def hear(self, pcm16, rate=16000):
        """pcm16: raw little-endian 16-bit mono audio at 16 kHz. Returns the text."""
        self.load()
        audio = np.frombuffer(pcm16, dtype="<i2").astype(np.float32) / 32768.0
        if rate != 16000 and len(audio):
            idx = np.arange(0, len(audio), rate / 16000.0)
            audio = np.interp(idx, np.arange(len(audio)), audio).astype(np.float32)
        if len(audio) < 16000 * 0.3:
            return ""
        lang = self.cfg.get("lang", "en-US")[:2]
        wake = self.cfg.get("wake_word", "hey gwm").split(",")[0].strip()
        segments, _ = self.model.transcribe(
            audio, language=lang, beam_size=1, vad_filter=False, condition_on_previous_text=False,
            initial_prompt=wake.upper() if len(wake.replace("hey", "").strip()) <= 5 else wake)   # helps it spell the wake word
        text = " ".join(s.text.strip() for s in segments).strip()
        # Whisper sometimes "hears" these in silence
        if text.lower().strip(" .!") in ("", "you", "thank you", "thanks for watching", "bye"):
            return ""
        return text
