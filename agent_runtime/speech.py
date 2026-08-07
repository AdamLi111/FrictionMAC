"""
Voice input for the interactive console (optional; typed input stays the default).

Two mic modes, chosen by env:
  - **Misty's built-in mic** (VOICE=1): `capture_speech(requireKeyPhrase=True)` — say the wake
    phrase ("hey misty") then your command; Misty records it, fires a `VoiceRecord` event with
    the WAV filename, and we download + transcribe it. One capture per command, so the wake
    phrase naturally keeps Misty from transcribing its own speech.
  - **Laptop mic** (VOICE=1 + VOICE_LAPTOP_MIC=1): record locally with `sounddevice`; the wake
    word is matched in the transcript once, then each utterance is taken as a command.

Talks to Misty directly over HTTP/websocket via the vendored mistyPy (independent of the robot
MCP server). Transcription uses SpeechRecognition + Google STT (free; needs internet). Only
active in REAL mode (a reachable Misty); ignored in stub mode.

Deps (agent env): `SpeechRecognition websocket-client requests` (Misty mic) and additionally
`sounddevice numpy` (laptop mic).
"""
import io
import os
import queue
import time
import wave
from urllib.parse import quote

import requests
import speech_recognition as sr

WAKE_WORD = os.environ.get("VOICE_WAKE_WORD", "hey misty").lower()


class VoiceInput:
    """Blocking, one-command-at-a-time voice source for the console's input loop.

    `next_command()` blocks until the user speaks a command and returns the transcript (str),
    or "" when stopped. It is meant to be called from a worker thread (the console runs it via
    anyio.to_thread), exactly where `input("you> ")` would be."""

    def __init__(self, robot_ip: str, use_laptop_mic: bool = False):
        self.ip = robot_ip
        self.use_laptop_mic = use_laptop_mic
        self.recognizer = sr.Recognizer()
        self._q: "queue.Queue" = queue.Queue()
        self._stop = False
        self._sample_rate = 16000
        self._channels = 1
        self._wake_detected = False  # laptop mode: wake word only required once
        self.robot = None
        if not use_laptop_mic:
            from robot_tools.vendor.mistyPy.Robot import Robot
            from robot_tools.vendor.mistyPy.Events import Events
            self.robot = Robot(self.ip)
            # mistyPy requires the callback to take EXACTLY one positional arg (it checks
            # `__code__.co_argcount == 1`), so a bound method — which counts `self` — is
            # rejected. Register a one-arg closure and keep a reference so it isn't GC'd.
            def _voice_cb(data):
                self._on_voice_record(data)
            self._voice_cb = _voice_cb
            self.robot.register_event(
                event_type=Events.VoiceRecord, event_name="ponder_voice",
                keep_alive=True, callback_function=_voice_cb)

    # ---------------- public ----------------
    def start(self) -> "VoiceInput":
        if self.use_laptop_mic:
            self._calibrate_ambient_noise()
        return self

    def next_command(self, _timeout=None) -> str:
        return self._next_laptop() if self.use_laptop_mic else self._next_misty()

    def cleanup(self) -> None:
        self._stop = True
        self._q.put("")  # unblock a waiting get()
        if self.robot is not None:
            try:
                self.robot.unregister_event("ponder_voice")
            except Exception:
                pass

    # ---------------- Misty built-in mic ----------------
    def _next_misty(self) -> str:
        while not self._stop:
            try:  # arm one capture: wait for the wake phrase, then record the command
                self.robot.capture_speech(requireKeyPhrase=True, overwriteExisting=False,
                                          maxSpeechLength=15000, silenceTimeout=2000)
            except Exception as e:
                print(f"[voice] capture_speech error: {e}")
                time.sleep(1.0)
                continue
            while not self._stop:  # wait for the VoiceRecord handler to transcribe + enqueue
                try:
                    text = self._q.get(timeout=0.5)
                except queue.Empty:
                    continue
                if text:
                    return text
                break  # transcription failed/empty -> re-arm capture
        return ""

    def _on_voice_record(self, data) -> None:
        """mistyPy WS-thread callback: download the captured WAV, transcribe it, enqueue text."""
        try:
            msg = (data or {}).get("message", {}) or {}
            if msg.get("success", True) is False:
                self._q.put(None)
                return
            fname = msg.get("filename")
            self._q.put(self._download_and_transcribe(fname) if fname else None)
        except Exception as e:
            print(f"[voice] voice-record handler error: {e}")
            self._q.put(None)

    def _download_and_transcribe(self, filename: str):
        time.sleep(0.4)  # let Misty finish writing the file
        url = f"http://{self.ip}/api/audio?FileName={quote(filename)}"
        try:
            resp = requests.get(url, timeout=10)
            if resp.status_code != 200 or len(resp.content) < 200:
                return None
            with sr.AudioFile(io.BytesIO(resp.content)) as src:
                audio = self.recognizer.record(src)
            return self.recognizer.recognize_google(audio)
        except sr.UnknownValueError:
            return None
        except Exception as e:
            print(f"[voice] transcription error: {e}")
            return None

    # ---------------- laptop mic ----------------
    def _calibrate_ambient_noise(self) -> None:
        import numpy as np  # noqa: F401 (kept parallel to _wav_bytes)
        import sounddevice as sd
        try:
            rec = sd.rec(int(1.0 * self._sample_rate), samplerate=self._sample_rate,
                         channels=self._channels, dtype="int16")
            sd.wait()
            with sr.AudioFile(io.BytesIO(self._wav_bytes(rec))) as src:
                self.recognizer.adjust_for_ambient_noise(src, duration=1)
        except Exception as e:
            print(f"[voice] ambient calibration skipped: {e}")

    def _next_laptop(self) -> str:
        while not self._stop:
            text = self._record_and_transcribe()
            if not text:
                continue
            low = text.lower()
            if self._wake_detected:
                return text
            if WAKE_WORD in low:  # first time: require the wake word, strip it
                self._wake_detected = True
                cmd = text[low.find(WAKE_WORD) + len(WAKE_WORD):].strip()
                if cmd:
                    return cmd  # command came with the wake word
            # else: ignore speech until the wake word is heard
        return ""

    def _record_and_transcribe(self, duration: float = 6.0):
        import numpy as np
        import sounddevice as sd
        while not self._stop:  # wait for sound above a threshold, then record
            chunk = sd.rec(int(0.1 * self._sample_rate), samplerate=self._sample_rate,
                           channels=self._channels, dtype="float32")
            sd.wait()
            if np.abs(chunk).mean() > 0.01:
                break
        if self._stop:
            return None
        rec = sd.rec(int(duration * self._sample_rate), samplerate=self._sample_rate,
                     channels=self._channels, dtype="int16")
        sd.wait()
        try:
            with sr.AudioFile(io.BytesIO(self._wav_bytes(rec))) as src:
                audio = self.recognizer.record(src)
            return self.recognizer.recognize_google(audio)
        except sr.UnknownValueError:
            return None
        except Exception as e:
            print(f"[voice] transcription error: {e}")
            return None

    @staticmethod
    def _wav_bytes(arr) -> bytes:
        import numpy as np
        if arr.dtype != np.int16:
            arr = (arr * 32767).astype(np.int16)
        if arr.ndim > 1:
            arr = arr.flatten()
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(arr.tobytes())
        return buf.getvalue()
