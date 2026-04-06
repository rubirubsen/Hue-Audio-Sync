# 🎵 Hue × Audio Sync

A lightweight, asynchronous Python script that synchronizes Philips Hue lights in real-time with your audio output. It detects beats, reads the current track via MPRIS (`playerctl`), and dynamically adjusts the lighting mood (color & energy) via the ListenBrainz API.

Optimized for direct light control (`/lights` API) to bypass the strict rate limit of the Hue bridge and enable smooth, low-latency strobe and pulse effects.

I did this because I didn't really find what I was looking for after re-installing into Linux.

## ✨ Features

* **Real-time Beat Detection:** Uses `aubio` for precise onset and tempo detection directly from your system's audio loopback.
* **Mood-based Colors:** Fetches metadata about the current track via *ListenBrainz* / *MusicBrainz* and selects colors based on the energy and mood (valence) of the song. Automatically falls back to a heuristic (title analysis) if the track is not found.
* **BPM Pulse & Beat Flash:** Lights pulse gently to the BPM and flash vividly on hard beats.
* **Setup Wizard:** Integrated assistant (`--setup`) that finds the Hue bridge on your network, generates an API key, and helps select the correct audio input device.
* **Various Color Palettes:** Choose between palettes like `club` (default), `fire`, `ice`, `sunset`, `deep`, or `rainbow`.

## 🛠 Requirements

This script is primarily designed for Linux (e.g., Ubuntu, Raspberry Pi OS, Arch) as it relies on `playerctl` and PulseAudio/PipeWire monitor devices.

### 1. Install System Packages
You need `playerctl` for track detection and `libaubio-dev` to compile the audio analysis package:
    
```bash

sudo apt update
sudo apt install playerctl libaubio-dev
```


### 2. Set Up a Virtual Environment & Install Dependencies

It is highly recommended (and required on modern Linux distros) to use a virtual environment:

```bash
# Create a virtual environment
python3 -m venv venv

# Activate the virtual environment
source venv/bin/activate

# Install the required Python packages
pip install aubio sounddevice aiohttp numpy
```

## 🚀 Setup & Usage
Note: Make sure your virtual environment is activated (source venv/bin/activate) before running the script!

### Initial Setup
Run the integrated wizard to connect your Hue bridge and select your audio device:

```bash
python3 hue_sync.py --setup
```

Follow the on-screen instructions. You will be prompted to press the link button on your Hue bridge.
## List Audio Devices (Optional)

If the automatic audio monitor fails, you can list all available audio devices to find the correct index:

```bash
python3 hue_sync.py --devices
```

Tip: Look for a device that has "Monitor", "Loopback", or "monitor of" in its name.

## Start Syncing

After the setup is complete, simply run the script:

```bash
python3 hue_sync.py
```

Play some music and enjoy the show! 🪩

# ⚙️ Configuration (config.json)

The setup automatically creates a config.json. You can adjust these settings while the script is running (hot-reloading is enabled!).

Interesting parameters under "sync":

    "color_mode": "mood_random" (recommended), "mood", "random", or "rainbow".

    "palette": Choose the color scheme ("club", "fire", "ice", "sunset", "deep").

    "base_brightness": The base brightness level (1-254).

    "beat_brightness_boost": How much brighter the light gets during a beat flash.

    "enabled_color_mood": Toggles the ListenBrainz lookup on or off.

💡 Performance Notes

To avoid overloading the Hue bridge, this script sends commands to the /lights endpoint (max. ~10 requests/second). It is highly recommended to limit the selected group in your Hue app to 1 to a maximum of 3 lights. This keeps the network jitter low and guarantees smooth, responsive flashes.

It's still WiP - so be welcome to use this for your approaches. 