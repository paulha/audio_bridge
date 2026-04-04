# audio_bridge — Installation & Usage

Bidirectional radio audio bridge over UDP + Opus.
Streams audio between a radio-connected Win10 machine and a Win11 operator machine in real time.

---

## Architecture overview

```
Win10 (remote / radio machine)                Win11 (local / operator machine)
─────────────────────────────                 ──────────────────────────────────
USB Codec Mic (radio RX out)
    │ sounddevice capture
    ▼
  Opus encode
    │ UDP → rx_port (5001)  ──────────────▶  receive on rx_port (5001)
                                               Opus decode
                                               local speaker / headphones

  receive on tx_port (5002) ◀──────────────  local mic (capture)
  Opus decode                                 Opus encode
    │                                         │ UDP → tx_port (5002)
    ▼
USB Codec Speaker (radio TX in)
```

The same `audio_bridge.py` script runs on both machines.
`config.toml` controls which role each machine plays.

---

## Prerequisites

- **Python 3.11 or later** (recommended; 3.8+ works with `tomli`)
  Download: https://www.python.org/downloads/

- **PortAudio** — required by `sounddevice`:
  Bundled inside the `sounddevice` Windows wheel, usually no separate install needed.

---

## Step 1 — Install Python packages

Run this on **both machines**:

```
pip install -r requirements.txt
```

---

## Step 2 — Opus codec (optional)

**Opus compression is optional.** The bridge works perfectly without it using
raw PCM (~256 kbps uncompressed), which is fine on a local WiFi network.
If Opus is not available you will see a warning in the console but audio will
flow normally.

The script detects Opus in this order and uses the first one it finds:

1. `opus.dll` / `libopus.dll` anywhere on your Windows `PATH`
2. `opuslib` Python package (if installed and its DLL search succeeds)
3. `PyOgg` (only if its `OpusEncoder` is exposed, which requires the DLL)
4. **Raw PCM fallback** (automatic — no action needed)

### Getting the DLL (if you want Opus compression)

<!-- I found the individual codec at https://www.free-codecs.com/opus_audio_codec_download.htm -->

1. Go to **https://opus-codec.org/downloads/**
2. Download the Windows Opus tools ZIP (contains `opusenc.exe`, `opusdec.exe`,
   and `libopus.dll` / `libopus-0.dll`)
3. Extract the ZIP and copy `libopus.dll` (or `libopus-0.dll`) into the same
   folder as `audio_bridge.py`

The script searches for both names automatically and will log on the next run:

```
INFO  codec: Codec: Opus (ctypes)  sr=16000  ch=1  frame=20ms  bitrate=24000
```

### Alternative: conda

If you use Anaconda or Miniconda:

```
conda install -c conda-forge libopus
pip install opuslib
```

### Skipping Opus entirely

If you don't need compression, just leave things as-is. The PCM fallback
produces no audible difference on a local network.

---

## Step 3 — Copy files to both machines

Copy these three files to a convenient folder (e.g. `C:\audio_bridge\`) on **both** machines:

```
audio_bridge.py
config.toml
```

---

## Step 4 — Find audio device names

Run on **each machine separately**:

```
python audio_bridge.py --list-devices
```

Example output:

```
── Audio Devices ──────────────────────────────────────────────────
  idx   in  out  name
  ───   ──  ───  ────
    0    2    0  Microphone (USB Audio CODEC)           ◀ default-in
    1    0    2  Speakers (USB Audio CODEC)             ▶ default-out
    2    2    0  Microphone Array (Realtek)
    3    0    2  Headphones (Realtek)
───────────────────────────────────────────────────────────────────
```

Note the **exact names** of:
- On Win10: the USB Codec Mic (radio RX output) and USB Codec Speaker (radio TX input)
- On Win11: your preferred headset/mic and speaker/headphones

The device name matching is **case-insensitive substring** — `"USB Codec"` matches
`"Microphone (USB Audio CODEC)"` and `"Speakers (USB Audio CODEC)"`.
Use a more specific substring if there are multiple USB audio devices.

---

## Step 5 — Edit config.toml

Open `config.toml` in any text editor.

### On the Win10 (radio) machine:

```toml
[mode]
role = "remote"

[network]
remote_ip = "192.168.1.100"   # ← this machine's IP
local_ip  = "192.168.1.200"   # ← Win11 machine's IP

[audio.remote]
capture_device  = "USB Codec"   # adjust if needed (from --list-devices)
playback_device = "USB Codec"
```

### On the Win11 (operator) machine:

```toml
[mode]
role = "local"

[network]
remote_ip = "192.168.1.100"   # ← Win10 machine's IP
local_ip  = "192.168.1.200"   # ← this machine's IP

[audio.local]
capture_device  = "Headset Microphone"   # your mic
playback_device = "Headphones"           # your speakers/headset
```

> Both machines can share the same config.toml — just flip `role`.

---

## Step 6 — Run the bridge

Open a Command Prompt or PowerShell window and run on **each machine**:

```
python audio_bridge.py config.toml
```

Start the **remote** (radio) machine first, then the **local** (operator) machine.

You should see something like:

```
10:42:01  INFO     bridge: Role: remote  |  Codec backend: opus
10:42:01  INFO     codec: Codec: Opus  sr=16000  ch=1  frame=20ms  bitrate=24000
10:42:01  INFO     Receiver:5002: Listening on UDP port 5002
10:42:01  INFO     Sender→192.168.1.200:5001: Capturing from 'USB Codec' → 192.168.1.200:5001
10:42:01  INFO     bridge: Bridge running – Ctrl+C to stop.
```

After `stats.interval_s` seconds (default 10), you will see a stats block:

```
── Stream Statistics ───────────────────────────────────────────
  Radio-RX → local       rx=  498  lost=   0  ( 0.0%)  ooo=  0  49.8 pkt/s
  Receiver:5002          last packet: 0.2s ago        [OK]
────────────────────────────────────────────────────────────────
```

Press **Ctrl+C** to stop.

---

## Tuning latency

Total one-way latency = capture buffer + encode + network + jitter buffer + playback buffer.

| Parameter | Default | Effect |
|-----------|---------|--------|
| `codec.frame_ms` | 20 | Smaller = lower latency, higher CPU. Try `10` for minimum. |
| `buffer.jitter_ms` | 40 | Smaller = lower latency, more dropouts on WiFi jitter. |
| `codec.sample_rate` | 16000 | Higher rate is not needed for narrow-band radio audio. |

With defaults on a healthy local WiFi network, round-trip latency is typically **80–150 ms**.

### WASAPI exclusive mode (advanced, Windows only)

For the lowest possible latency, you can use WASAPI exclusive mode via sounddevice's
PortAudio host API settings.  Add this near the top of `audio_bridge.py` after importing
`sounddevice`:

```python
import sounddevice as sd
# Force WASAPI exclusive mode for minimum latency (device must support it)
sd.default.extra_settings = sd.WasapiSettings(exclusive=True)
```

Exclusive mode requires that no other application is using the device at the same time.
If it fails, remove the line and use shared mode (the default).

---

## Firewall

Windows Defender Firewall may block UDP on ports 5001/5002.
If you see `SILENT` in the stats, check the firewall.

Allow the ports in PowerShell (run as Administrator):

```powershell
New-NetFirewallRule -DisplayName "AudioBridge RX" -Direction Inbound -Protocol UDP -LocalPort 5001 -Action Allow
New-NetFirewallRule -DisplayName "AudioBridge TX" -Direction Inbound -Protocol UDP -LocalPort 5002 -Action Allow
```

Or open **Windows Defender Firewall → Advanced Settings → Inbound Rules → New Rule → Port → UDP → 5001,5002**.

---

## Auto-start with Windows Task Scheduler

To have the bridge start automatically at login:

1. Open **Task Scheduler** (search Start menu).
2. Click **Create Basic Task…**
3. Name: `AudioBridge`
4. Trigger: **When I log on**
5. Action: **Start a program**
   - Program: `python`
   - Arguments: `C:\audio_bridge\audio_bridge.py C:\audio_bridge\config.toml`
   - Start in: `C:\audio_bridge\`
6. Finish, then right-click the task → **Properties → General**:
   - Check **Run with highest privileges** (helps with audio device access)

To verify it works: right-click the task → **Run**.

Alternatively, create a simple `start_bridge.bat` file:

```bat
@echo off
cd /d C:\audio_bridge
python audio_bridge.py config.toml
pause
```

Double-clicking the `.bat` file will start the bridge in a visible console window.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `Device 'USB Codec' not found` | Device name mismatch | Run `--list-devices` and copy the exact name into config |
| Stats show `SILENT` | Firewall blocking UDP | See Firewall section above |
| Choppy / distorted audio | WiFi congestion or too-small jitter buffer | Increase `buffer.jitter_ms` to 80 or 120 |
| `OSError: [Errno -9996]` | Device in use by another app | Close other audio apps; try WASAPI exclusive mode |
| `opuslib` import error | DLL not found | See Step 2 above; fallback to PCM is automatic |
| One-way audio only | Ports 5001/5002 mixed up | Confirm `rx_port`/`tx_port` are consistent in both configs |

### Audio device names on Yaesu radios

The FT-991A (and similar Yaesu radios) typically appear as:
- `USB Audio CODEC` or `USB Codec` in Windows Device Manager
- In sounddevice: `"Microphone (USB Audio CODEC)"` and `"Speakers (USB Audio CODEC)"`

The partial match `"USB Codec"` will match both. If it doesn't, run `--list-devices`
and paste the exact string into `config.toml`.

---

## Design decisions (recorded)

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Language | Python | Cross-platform, excellent audio/networking libs, fast iteration |
| Transport | UDP | Lowest latency; dropped packets cause brief glitches rather than stalls |
| Codec | Opus (APPLICATION_RESTRICTED_LOWDELAY) | Purpose-built for real-time voice; 10:1 compression; ~3ms algorithmic latency |
| Fallback | Raw PCM | Automatic if Opus DLL is unavailable |
| Architecture | Single script, two roles | One codebase to maintain; role controlled by config |
| Config format | TOML | Readable, supports comments, stdlib in Python 3.11+ |
| Packet monitoring | Sequence numbers + console stats | Zero overhead on hot path; periodic loss% and pkt/s report |
| Connection health | Silence detection + heartbeat | Warns when remote goes silent; heartbeats keep receiver aware during TX silence |
| Reconnect | Automatic (UDP is connectionless) | Streams auto-resume when packets flow again; warnings on silence |
| Sample rate | 16 kHz mono 16-bit | Appropriate for narrow-band radio voice; change to 48 kHz for wideband |
| Jitter buffer | FIFO queue, drop-oldest on overflow | Prefer recency over completeness; configurable depth |
