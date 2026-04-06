#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════╗
║   Hue × Audio Sync  –  Beat + Mood              ║
║   aubio (Echtzeit) + ListenBrainz + playerctl   ║
╚══════════════════════════════════════════════════╝

Abhängigkeiten:
    sudo apt install playerctl libaubio-dev
    pip install aubio sounddevice aiohttp

Einrichtung:
    python3 hue_sync.py --setup
"""

import asyncio
import aiohttp
import aubio
import sounddevice as sd
import numpy as np
import json
import sys
import time
import signal
import subprocess
import threading
import queue
from colorsys import hsv_to_rgb
from pathlib import Path

# ══════════════════════════════════════════════════════════════════
# KONFIGURATION
# ══════════════════════════════════════════════════════════════════

CONFIG_FILE = Path("config.json")

DEFAULT_CONFIG = {
    "hue": {
        "bridge_ip": "",
        "api_key": "",
        "group_id": "1",
        "latency_ms": 80
    },
    "audio": {
        "device": None,
        "samplerate": 44100,
        "hop_size": 256,
        "onset_threshold": 0.3
    },
    "sync": {
        "base_brightness": 140,
        "beat_brightness_boost": 90,
        "beat_flash_duration_ms": 120,
        "mood_transition_ms": 1500,
        "enabled_beat_sync": True,
        "enabled_color_mood": True,
        "mood_update_interval": 30,
        "color_mode": "mood_random",
        "color_randomness": 0.35,
        "beat_color_shift": True,
        "palette": "club",
        "bpm_pulse": True,
        "bpm_pulse_strength": 35
    }
}


def load_config():
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
        for section, values in DEFAULT_CONFIG.items():
            cfg.setdefault(section, {})
            for k, v in values.items():
                cfg[section].setdefault(k, v)
        return cfg
    else:
        with open(CONFIG_FILE, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        print("⚠  config.json erstellt – bitte ausfüllen oder --setup nutzen!")
        sys.exit(1)


# ══════════════════════════════════════════════════════════════════
# AUDIO DEVICE HELPER
# ══════════════════════════════════════════════════════════════════

def list_monitor_devices():
    """Findet PulseAudio/PipeWire Monitor-Quellen (Loopback)"""
    devices = sd.query_devices()
    monitors = []
    for i, d in enumerate(devices):
        name = d["name"].lower()
        is_monitor = (
            "monitor" in name or
            "loopback" in name or
            "pipewire" in name or
            "pulse" in name
        )
        if d["max_input_channels"] > 0 and is_monitor:
            monitors.append((i, d["name"], d["max_input_channels"]))
    return monitors


def find_best_monitor():
    """Automatisch besten Monitor-Device wählen"""
    monitors = list_monitor_devices()
    if not monitors:
        # Fallback: alle Input-Devices zeigen
        return None
    # Bevorzuge "monitor of" (PulseAudio typisch)
    for idx, name, ch in monitors:
        if "monitor of" in name.lower():
            return idx
    return monitors[0][0]


def print_all_devices():
    print("\n📻 Verfügbare Audio-Eingabegeräte:\n")
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            marker = ""
            name_l = d["name"].lower()
            if "monitor" in name_l or "loopback" in name_l:
                marker = "  ← Monitor/Loopback"
            print(f"   [{i:2d}] {d['name']}{marker}")
    print()


# ══════════════════════════════════════════════════════════════════
# PLAYERCTL – TRACK INFO VIA MPRIS
# ══════════════════════════════════════════════════════════════════

class PlayerCtl:
    """Holt aktuelle Track-Info über playerctl (MPRIS)"""

    def get_current_track(self):
        """Gibt (artist, title) zurück oder (None, None)"""
        try:
            artist = subprocess.check_output(
                ["playerctl", "metadata", "artist"],
                stderr=subprocess.DEVNULL, timeout=2
            ).decode().strip()
            title = subprocess.check_output(
                ["playerctl", "metadata", "title"],
                stderr=subprocess.DEVNULL, timeout=2
            ).decode().strip()
            status = subprocess.check_output(
                ["playerctl", "status"],
                stderr=subprocess.DEVNULL, timeout=2
            ).decode().strip()
            if status == "Playing" and title:
                return artist or "Unknown", title
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
                FileNotFoundError):
            pass
        return None, None

    def is_playing(self):
        try:
            status = subprocess.check_output(
                ["playerctl", "status"],
                stderr=subprocess.DEVNULL, timeout=2
            ).decode().strip()
            return status == "Playing"
        except Exception:
            return False

    def is_available(self):
        try:
            subprocess.check_output(
                ["playerctl", "--version"],
                stderr=subprocess.DEVNULL, timeout=2
            )
            return True
        except (FileNotFoundError, subprocess.CalledProcessError):
            return False


# ══════════════════════════════════════════════════════════════════
# LISTENBRAINZ / ACOUSTICBRAINZ MOOD LOOKUP
# ══════════════════════════════════════════════════════════════════

class MoodLookup:
    """
    Holt Mood/Energy-Daten via MusicBrainz + ListenBrainz.
    Fallback: schätzt Mood aus dem Tracknamen (Heuristik).
    """

    MB_BASE  = "https://musicbrainz.org/ws/2"
    LB_BASE  = "https://api.listenbrainz.org/1"

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self._cache = {}

    async def get_mood(self, artist: str, title: str) -> dict:
        """
        Gibt dict mit energy (0-1), valence (0-1), tempo_hint zurück.
        Fällt bei Fehler auf Heuristik zurück.
        """
        key = f"{artist.lower()}|{title.lower()}"
        if key in self._cache:
            return self._cache[key]

        mood = await self._try_listenbrainz(artist, title)
        if not mood:
            mood = self._heuristic(title)

        self._cache[key] = mood
        return mood

    async def _try_listenbrainz(self, artist, title):
        """
        Sucht MBID via MusicBrainz, dann Metadata via ListenBrainz
        """
        try:
            # 1. MBID finden
            params = {
                "query": f'recording:"{title}" AND artist:"{artist}"',
                "fmt": "json",
                "limit": 1
            }
            headers = {"User-Agent": "hue-sync/1.0 (linux)"}
            async with self.session.get(
                f"{self.MB_BASE}/recording",
                params=params, headers=headers,
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                recordings = data.get("recordings", [])
                if not recordings:
                    return None
                mbid = recordings[0].get("id")
                if not mbid:
                    return None

            # 2. ListenBrainz Metadata
            async with self.session.get(
                f"{self.LB_BASE}/metadata/recording",
                params={"recording_mbids": mbid, "inc": "tag"},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                rec = data.get(mbid, {})
                tags = rec.get("recording", {}).get("tag", {}).get("recording", [])

                # Tags → Energy/Valence schätzen
                energy, valence = self._tags_to_mood(tags)
                return {"energy": energy, "valence": valence, "source": "listenbrainz"}

        except Exception:
            return None

    def _tags_to_mood(self, tags):
        """MusicBrainz Tags → Energy + Valence"""
        tag_names = {t.get("tag_name", "").lower() for t in tags}

        # Energy
        energy = 0.5
        if tag_names & {"metal", "hard rock", "punk", "drum and bass",
                        "gabber", "hardcore", "techno", "industrial"}:
            energy = 0.85
        elif tag_names & {"electronic", "dance", "house", "trance",
                          "edm", "hip-hop", "pop", "rock"}:
            energy = 0.65
        elif tag_names & {"ambient", "classical", "acoustic", "folk",
                          "jazz", "blues", "sleep", "meditation"}:
            energy = 0.3

        # Valence
        valence = 0.5
        if tag_names & {"happy", "upbeat", "fun", "party", "dance",
                        "uplifting", "euphoric", "cheerful"}:
            valence = 0.8
        elif tag_names & {"sad", "melancholic", "dark", "depressive",
                          "melancholy", "gloomy", "doom"}:
            valence = 0.2
        elif tag_names & {"energetic", "aggressive", "angry", "intense"}:
            valence = 0.35

        return energy, valence

    def _heuristic(self, title: str) -> dict:
        """
        Fallback: Mood aus Schlüsselwörtern im Titel schätzen.
        Besser als nichts.
        """
        title_l = title.lower()
        energy, valence = 0.5, 0.5

        dark_words = ["dark", "night", "shadow", "die", "death", "cry",
                      "pain", "fear", "ghost", "evil", "fallen", "broken"]
        happy_words = ["sun", "love", "happy", "joy", "dance", "party",
                       "good", "life", "feel", "free", "light", "beautiful"]
        hard_words = ["metal", "fire", "rage", "fight", "war", "bang",
                      "destroy", "kill", "blast", "thunder", "storm"]

        if any(w in title_l for w in dark_words):
            valence -= 0.25
        if any(w in title_l for w in happy_words):
            valence += 0.25
        if any(w in title_l for w in hard_words):
            energy += 0.3

        return {
            "energy": max(0.1, min(0.9, energy)),
            "valence": max(0.1, min(0.9, valence)),
            "source": "heuristic"
        }


# ══════════════════════════════════════════════════════════════════
# FARB-MAPPING
# ══════════════════════════════════════════════════════════════════

def rgb_to_xy(r, g, b):
    def gamma(v):
        return ((v + 0.055) / 1.055) ** 2.4 if v > 0.04045 else v / 12.92
    r, g, b = gamma(r), gamma(g), gamma(b)
    X = r * 0.664511 + g * 0.154324 + b * 0.162028
    Y = r * 0.283881 + g * 0.668433 + b * 0.047685
    Z = r * 0.000088 + g * 0.072310 + b * 0.986039
    total = X + Y + Z
    if total == 0:
        return [0.3127, 0.3290]
    return [round(X / total, 4), round(Y / total, 4)]


import random as _random

_hue_offset = 0.0          # globaler Hue-Drift
_rainbow_pos = 0.0         # Rainbow-Position

# Paletten: Liste von Hue-Werten (0.0-1.0)
PALETTES = {
    "club":    [0.75, 0.83, 0.67, 0.0,  0.83, 0.75, 0.67],  # Lila Magenta Blau Rot
    "fire":    [0.0,  0.04, 0.08, 0.92, 0.0,  0.96, 0.04],  # Rot Orange Gelb
    "ice":     [0.55, 0.60, 0.67, 0.72, 0.55, 0.65, 0.70],  # Cyan Blau Violett
    "sunset":  [0.0,  0.83, 0.75, 0.08, 0.92, 0.83, 0.0 ],  # Rot Pink Lila Orange
    "deep":    [0.67, 0.72, 0.75, 0.78, 0.83, 0.70, 0.75],  # Blau→Lila→Magenta
    "rainbow": [x/12 for x in range(12)],                    # Alle Farben
}

def mood_to_base_hue(energy: float, valence: float,
                     palette: str = "club") -> float:
    """Wählt Hue aus der Palette basierend auf valence"""
    hues = PALETTES.get(palette, PALETTES["club"])
    idx = int(valence * (len(hues) - 1))
    idx = max(0, min(len(hues) - 1, idx))
    return hues[idx]


def mood_to_color(energy: float, valence: float,
                  mode: str = "mood_random",
                  randomness: float = 0.35,
                  palette: str = "club") -> list:
    """Energy + Valence → Hue CIE XY"""
    global _rainbow_pos

    base_hue = mood_to_base_hue(energy, valence, palette)
    saturation = 0.85 + energy * 0.15

    if mode == "mood":
        hue = base_hue
    elif mode == "mood_random":
        shift = (_random.random() - 0.5) * 2 * randomness * 0.4
        hue = base_hue + shift
    elif mode == "random":
        hues = PALETTES.get(palette, PALETTES["club"])
        hue = _random.choice(hues) + (_random.random() - 0.5) * 0.05
    elif mode == "rainbow":
        _rainbow_pos = (_rainbow_pos + 0.06) % 1.0
        hue = _rainbow_pos
    else:
        hue = base_hue

    r, g, b = hsv_to_rgb(hue % 1.0, saturation, 1.0)
    return rgb_to_xy(r, g, b)


def beat_color_shift(current_xy: list, energy: float,
                     randomness: float = 0.35,
                     palette: str = "club") -> list:
    """Pro Beat: zufällige Farbe aus der Palette"""
    hues = PALETTES.get(palette, PALETTES["club"])
    hue = _random.choice(hues) + (_random.random() - 0.5) * 0.04
    sat = 0.85 + energy * 0.15
    r, g, b = hsv_to_rgb(hue % 1.0, sat, 1.0)
    return rgb_to_xy(r, g, b)


def bpm_to_brightness_curve(bpm: float, energy: float, base_bri: int) -> int:
    """Basis-Helligkeit energy-abhängig, garantiertes Minimum"""
    bri = int(base_bri * (0.6 + energy * 0.4))
    return max(80, min(220, bri))


# ══════════════════════════════════════════════════════════════════
# PHILIPS HUE CLIENT – OPTIMIERT AUF /lights
# ══════════════════════════════════════════════════════════════════

class HueClient:
    def __init__(self, bridge_ip, api_key, group_id, session, latency_ms=80):
        self.base = f"http://{bridge_ip}/api/{api_key}"
        self.group_id = group_id
        self.session = session
        self.latency = latency_ms / 1000.0
        self.light_ids = []  # Speichert die Lampen aus der Gruppe
        self._queue = asyncio.Queue(maxsize=20)
        self._worker_task = None

    def start(self):
        self._worker_task = asyncio.create_task(self._worker())

    def stop(self):
        if self._worker_task:
            self._worker_task.cancel()

    async def _worker(self):
        """Iteriert über einzelne Lichter, um API-Limits zu schonen"""
        while True:
            try:
                payload = await self._queue.get()
                try:
                    # An jede einzelne Lampe der Gruppe senden
                    for l_id in self.light_ids:
                        url = f"{self.base}/lights/{l_id}/state"
                        async with self.session.put(
                            url, json=payload,
                            timeout=aiohttp.ClientTimeout(total=0.5)
                        ) as resp:
                            pass # Errors ignorieren, um Fluss nicht aufzuhalten
                        # Kleiner Delay zwischen Lampen
                        await asyncio.sleep(0.01)
                except Exception:
                    pass
                finally:
                    self._queue.task_done()
                
                # Minimum Zeit zwischen zwei Events aus der Queue
                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                break

    def _send(self, payload):
        """Non-blocking: wirft älteste Nachricht raus wenn Queue voll"""
        try:
            self._queue.put_nowait(payload)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(payload)
            except Exception:
                pass

    async def set_color(self, xy, brightness, transition_100ms=15):
        self._send({
            "on": True,
            "xy": xy,
            "bri": max(1, min(254, brightness)),
            "transitiontime": transition_100ms
        })

    async def beat_flash(self, xy, base_bri, boost_bri, flash_ms=120):
        self._send({
            "on": True,
            "xy": xy,
            "bri": max(1, min(254, boost_bri)),
            "transitiontime": 0
        })
        await asyncio.sleep(flash_ms / 1000.0)
        self._send({
            "bri": max(1, min(254, base_bri)),
            "transitiontime": 1
        })

    async def verify(self):
        url = f"{self.base}/groups/{self.group_id}"
        try:
            async with self.session.get(
                url, timeout=aiohttp.ClientTimeout(total=3)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self.light_ids = data.get('lights', [])
                    print(f"✅ Hue Bridge  │  Gruppe: '{data.get('name')}' ({len(self.light_ids)} Lampen registriert)")
                    if not self.light_ids:
                        print("❌ Keine Lampen in dieser Gruppe gefunden!")
                        return False
                    return True
                print(f"❌ Hue Gruppe {self.group_id} nicht gefunden")
                return False
        except Exception as e:
            print(f"❌ Hue Bridge nicht erreichbar: {e}")
            return False


# ══════════════════════════════════════════════════════════════════
# AUBIO BEAT DETECTOR
# ══════════════════════════════════════════════════════════════════

class BeatDetector:
    """Lauscht auf Audio-Monitor und erkennt Beats in Echtzeit."""
    def __init__(self, device_idx, samplerate, hop_size, threshold, beat_queue):
        self.device_idx  = device_idx
        self.samplerate  = samplerate
        self.hop_size    = hop_size
        self.threshold   = threshold
        self.beat_queue  = beat_queue
        self._stop_event = threading.Event()
        self._thread     = None
        self._bpm        = 0.0

    def start(self):
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()

    def get_bpm(self):
        return self._bpm

    def _run(self):
        samplerate = self.samplerate
        hop_size   = self.hop_size

        onset = aubio.onset("energy", hop_size * 4, hop_size, samplerate)
        onset.set_threshold(self.threshold)
        onset.set_minioi_ms(250)

        tempo = aubio.tempo("default", hop_size * 8, hop_size, samplerate)

        def callback(indata, frames, time_info, status):
            if self._stop_event.is_set():
                raise sd.CallbackStop()

            samples = indata.mean(axis=1).astype(np.float32)

            if onset(samples):
                self.beat_queue.put_nowait(("beat", time.time()))

            if tempo(samples):
                bpm = tempo.get_bpm()
                if bpm > 0:
                    self._bpm = bpm
                    self.beat_queue.put_nowait(("bpm", bpm))

        device_info = sd.query_devices(self.device_idx)
        channels = min(device_info["max_input_channels"], 8)
        channels = max(channels, 1)

        try:
            with sd.InputStream(
                device=self.device_idx,
                channels=channels,
                samplerate=samplerate,
                blocksize=hop_size,
                dtype="float32",
                callback=callback
            ):
                print(f"🎙  Beat-Detection aktiv  │  Device [{self.device_idx}]")
                while not self._stop_event.is_set():
                    self._stop_event.wait(timeout=0.5)
        except sd.CallbackStop:
            pass
        except Exception as e:
            print(f"\n⚠  Audio-Thread Fehler: {e}")
            self.beat_queue.put_nowait(("error", str(e)))


# ══════════════════════════════════════════════════════════════════
# SYNC ENGINE
# ══════════════════════════════════════════════════════════════════

class SyncEngine:
    def __init__(self, hue: HueClient, mood_lookup: MoodLookup,
                 player: PlayerCtl, cfg: dict):
        self.hue      = hue
        self.mood     = mood_lookup
        self.player   = player
        self.cfg      = cfg["sync"]
        self.audio_cfg = cfg["audio"]

        self._current_xy     = [0.3127, 0.3290]
        self._current_energy = 0.5
        self._base_bri       = self.cfg["base_brightness"]
        self._current_track  = None
        self._last_mood_check = 0
        self._last_beat_time  = 0
        self._running        = False
        self._beat_queue     = queue.Queue()
        self._detector       = None

    async def _update_mood(self, artist, title):
        mood = await self.mood.get_mood(artist, title)
        self._current_energy = mood["energy"]
        mode = self.cfg.get("color_mode", "mood_random")
        randomness = self.cfg.get("color_randomness", 0.35)
        palette = self.cfg.get("palette", "club")
        xy = mood_to_color(mood["energy"], mood["valence"], mode, randomness, palette)
        self._current_xy = xy
        bri = max(100, bpm_to_brightness_curve(
            self._detector.get_bpm() if self._detector else 120,
            mood["energy"],
            self.cfg.get("base_brightness", 160)
        ))
        self._base_bri = bri
        transition = self.cfg["mood_transition_ms"] // 100
        await self.hue.set_color(xy, bri, transition_100ms=transition)

        source_label = {"listenbrainz": "🎵", "heuristic": "~"}.get(
            mood.get("source", ""), "?"
        )
        print(f"  {source_label} Mood │ "
              f"Energy: {mood['energy']:.2f}  "
              f"Valence: {mood['valence']:.2f}  "
              f"│ Quelle: {mood.get('source', '?')}")

    def _draw_beat(self, bpm: float):
        bar_len = 28
        filled = "█" * bar_len
        bpm_str = f"{bpm:5.1f} BPM" if bpm > 0 else "  ??? BPM"
        print(f"\r  🥁 {filled} {bpm_str} ●", end="", flush=True)

    def _draw_idle(self, bpm: float):
        bar_len = 28
        bpm_str = f"{bpm:5.1f} BPM" if bpm > 0 else "  ??? BPM"
        idle_bar = "▒" * bar_len
        print(f"\r  🎵 {idle_bar} {bpm_str}  ", end="", flush=True)

    async def _handle_beat_events(self):
        while True:
            try:
                event, value = self._beat_queue.get_nowait()
                if event == "beat" and self.cfg["enabled_beat_sync"]:
                    now_beat = time.time()
                    min_gap = self.cfg.get("beat_flash_duration_ms", 120) / 1000.0 + 0.05
                    if now_beat - self._last_beat_time < min_gap:
                        continue
                    self._last_beat_time = now_beat
                    boost = min(254, self._base_bri + self.cfg["beat_brightness_boost"])
                    
                    flash_xy = self._current_xy
                    if self.cfg.get("beat_color_shift", True):
                        flash_xy = beat_color_shift(
                            self._current_xy,
                            self._current_energy,
                            self.cfg.get("color_randomness", 0.35),
                            self.cfg.get("palette", "club")
                        )
                    asyncio.create_task(
                        self.hue.beat_flash(
                            flash_xy,
                            self._base_bri,
                            boost,
                            self.cfg["beat_flash_duration_ms"]
                        )
                    )
                    bpm = self._detector.get_bpm() if self._detector else 0
                    self._draw_beat(bpm)
                    asyncio.create_task(self._reset_bar_after(0.15))
                elif event == "error":
                    print(f"\n⚠  Audio-Fehler: {value}")
            except queue.Empty:
                break

    async def _reset_bar_after(self, delay: float):
        await asyncio.sleep(delay)
        bpm = self._detector.get_bpm() if self._detector else 0
        self._draw_idle(bpm)

    async def run(self, device_idx):
        self._running = True

        self._detector = BeatDetector(
            device_idx=device_idx,
            samplerate=self.audio_cfg["samplerate"],
            hop_size=self.audio_cfg["hop_size"],
            threshold=self.audio_cfg["onset_threshold"],
            beat_queue=self._beat_queue
        )
        self._detector.start()

        playerctl_ok = self.player.is_available()
        if not playerctl_ok:
            print("⚠  playerctl nicht gefunden – kein Track-Tracking, nur Beat-Sync")
        else:
            print("✅ playerctl verfügbar")

        print("\n🚀 Sync läuft – Musik abspielen!\n")

        pulse_task = None
        if self.cfg.get("bpm_pulse", True):
            pulse_task = asyncio.create_task(self._bpm_pulse_loop())

        config_mtime = Path("config.json").stat().st_mtime if Path("config.json").exists() else 0
        while self._running:
            try:
                if Path("config.json").exists():
                    mtime = Path("config.json").stat().st_mtime
                    if mtime != config_mtime:
                        config_mtime = mtime
                        try:
                            new_cfg = load_config()
                            self.cfg = new_cfg["sync"]
                            print("\n🔄 Config neu geladen!")
                        except Exception as e:
                            print(f"\n⚠  Config-Reload Fehler: {e}")

                await self._handle_beat_events()

                now = time.time()
                if playerctl_ok and now - self._last_mood_check > self.cfg["mood_update_interval"]:
                    self._last_mood_check = now
                    artist, title = self.player.get_current_track()
                    if title:
                        track_key = f"{artist}|{title}"
                        if track_key != self._current_track:
                            self._current_track = track_key
                            bpm = self._detector.get_bpm()
                            bpm_str = f"{bpm:.0f} BPM" if bpm > 0 else "? BPM"
                            print(f"\n🎵 {artist} – {title}  [{bpm_str}]")
                            if self.cfg["enabled_color_mood"]:
                                await self._update_mood(artist, title)

                await asyncio.sleep(0.005)

            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"\n⚠  Engine-Fehler: {e}")
                await asyncio.sleep(1)

        self._detector.stop()
        if pulse_task:
            pulse_task.cancel()

    async def _bpm_pulse_loop(self):
        last_bpm = 0.0
        interval = 0.5
        while self._running:
            try:
                bpm = self._detector.get_bpm() if self._detector else 0
                if bpm > 30:
                    interval = 60.0 / bpm
                    if abs(bpm - last_bpm) > 3:
                        last_bpm = bpm

                strength = self.cfg.get("bpm_pulse_strength", 35)
                pulse_bri = min(254, self._base_bri + strength)

                self.hue._send({
                    "bri": pulse_bri,
                    "transitiontime": max(2, int(interval * 5))
                })
                await asyncio.sleep(interval * 0.5)

                self.hue._send({
                    "bri": max(1, self._base_bri - int(strength * 0.3)),
                    "transitiontime": max(2, int(interval * 5))
                })
                await asyncio.sleep(interval * 0.5)

            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(0.5)

    def stop(self):
        self._running = False
        if self._detector:
            self._detector.stop()


# ══════════════════════════════════════════════════════════════════
# SETUP WIZARD
# ══════════════════════════════════════════════════════════════════

async def create_hue_user(bridge_ip, session):
    url = f"http://{bridge_ip}/api"
    print("\n💡 Drücke jetzt den Knopf auf der Hue Bridge...")
    input("   Dann ENTER: ")
    async with session.post(
        url, json={"devicetype": "hue_sync#linux"},
        timeout=aiohttp.ClientTimeout(total=10)
    ) as resp:
        data = await resp.json()
        if data and "success" in data[0]:
            key = data[0]["success"]["username"]
            print(f"✅ Hue API Key: {key}")
            return key
        print(f"❌ Fehler: {data}")
        return None


async def list_hue_groups(bridge_ip, api_key, session):
    url = f"http://{bridge_ip}/api/{api_key}/groups"
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
        groups = await resp.json()
        print("\n📡 Hue Gruppen:")
        for gid, info in groups.items():
            print(f"   [{gid}] {info.get('name')} ({info.get('type')}) "
                  f"– {len(info.get('lights', []))} Lampe(n)")


async def setup_wizard():
    print("\n══════════════════════════════════════")
    print("   Hue × Audio Sync – Setup")
    print("══════════════════════════════════════\n")

    cfg = json.loads(json.dumps(DEFAULT_CONFIG))

    print("── Audio Device ─────────────────────")
    print_all_devices()
    best = find_best_monitor()
    hint = f" (Empfehlung: [{best}])" if best is not None else ""
    idx_str = input(f"  Device-Index für Monitor/Loopback{hint}: ").strip()
    cfg["audio"]["device"] = int(idx_str) if idx_str else best

    print("\n── Philips Hue ──────────────────────")
    bridge_ip = input("  Bridge IP (z.B. 192.168.178.26): ").strip()
    cfg["hue"]["bridge_ip"] = bridge_ip

    async with aiohttp.ClientSession() as session:
        has_key = input("  Hast du schon einen API-Key? (j/n): ").strip().lower()
        if has_key == "j":
            cfg["hue"]["api_key"] = input("  API-Key: ").strip()
        else:
            key = await create_hue_user(bridge_ip, session)
            if key:
                cfg["hue"]["api_key"] = key

        await list_hue_groups(bridge_ip, cfg["hue"]["api_key"], session)
        cfg["hue"]["group_id"] = input("\n  Gruppen-ID: ").strip()

    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)
    print(f"\n✅ config.json gespeichert")
    print("   Starte mit: python3 hue_sync.py\n")


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════

async def main():
    if "--setup" in sys.argv:
        await setup_wizard()
        return

    if "--devices" in sys.argv:
        print_all_devices()
        best = find_best_monitor()
        if best is not None:
            print(f"  → Empfohlenes Device: [{best}] {sd.query_devices(best)['name']}")
        return

    cfg = load_config()

    missing = [k for k in ["bridge_ip", "api_key"] if not cfg["hue"].get(k)]
    if missing:
        print(f"❌ Fehlende Hue-Config: {missing}")
        print("   → python3 hue_sync.py --setup")
        sys.exit(1)

    device_idx = cfg["audio"].get("device")
    if device_idx is None:
        device_idx = find_best_monitor()
        if device_idx is None:
            print("❌ Kein Monitor-Device gefunden!")
            print("   → python3 hue_sync.py --devices")
            sys.exit(1)
        print(f"   Audio-Device: [{device_idx}] {sd.query_devices(device_idx)['name']}")

    print("╔══════════════════════════════════════╗")
    print("║   Hue × Audio Sync                   ║")
    print("╚══════════════════════════════════════╝\n")

    async with aiohttp.ClientSession() as session:
        hue    = HueClient(
            cfg["hue"]["bridge_ip"],
            cfg["hue"]["api_key"],
            cfg["hue"]["group_id"],
            session,
            cfg["hue"]["latency_ms"]
        )
        if not await hue.verify():
            sys.exit(1)
        hue.start()

        mood   = MoodLookup(session)
        player = PlayerCtl()
        engine = SyncEngine(hue, mood, player, cfg)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, engine.stop)

        await engine.run(device_idx)
        hue.stop()
        print("\n👋 Sync beendet.")


if __name__ == "__main__":
    asyncio.run(main())