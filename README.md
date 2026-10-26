# hero4-session-webcam

Use a **GoPro HERO4 Session** as a regular webcam on Ubuntu (Discord, Zoom, Google Meet, OBS, browsers...).

The camera has no webcam mode. This project takes the live preview that the camera sends over its own WiFi network and turns it into a virtual webcam device (`/dev/video10`, named "GoPro Session") that any application can select. After a one-time setup, the workflow is: turn on the camera's WiFi, connect the PC to it, and the script handles everything else.

---

## Contents

- [How to use it (day to day)](#how-to-use-it-day-to-day)
- [Requirements](#requirements)
- [Installation](#installation)
- [Why this method](#why-this-method)
- [How it works](#how-it-works)
- [Image quality: the hard limit](#image-quality-the-hard-limit)
- [Features](#features)
- [Quality profiles and filters](#quality-profiles-and-filters)
- [All options](#all-options)
- [Launcher commands](#launcher-commands)
- [Running at login](#running-at-login)
- [Troubleshooting](#troubleshooting)
- [Limitations](#limitations)
- [Safety notes](#safety-notes)

---

## How to use it (day to day)

1. Start the script (or let it start automatically at login, see [Running at login](#running-at-login)):
   ```bash
   ./start_gopro_cam.sh
   ```
   From this moment the "GoPro Session" webcam is available in your applications. Until the camera is connected it shows a standby image (`conlost.png`).
2. On the camera, press the **rear button** repeatedly until the screen shows **APP**.
3. Press the **record (shutter) button**. This turns on the camera's WiFi network.
4. Connect the PC to the camera's WiFi network.
5. That is all. The script detects the camera, starts the stream and the webcam switches from the standby image to live video within a few seconds. If the camera turns off, goes out of range or the stream stalls, the webcam goes back to the standby image and recovers on its own when the camera returns.

A microSD card is **not required**. The live stream works without a card in the camera.

### Recommended: a second network adapter

The camera creates its own WiFi network, and it has no internet access. While your PC is connected to it, a single WiFi adapter cannot be connected to your home network at the same time.

- **If you need internet while using the webcam** (video calls, streaming): use a second network adapter. A cheap USB WiFi dongle connected to the camera while the built-in WiFi (or Ethernet) stays on your home network works well. Linux routes the camera's `10.5.5.x` traffic through the dongle and everything else through your normal connection, without extra configuration.
- **If you do not need internet** (local recording in OBS, testing), a single adapter is fine.

---

## Requirements

- Ubuntu (or another Linux distribution with systemd/v4l2), x86_64.
- `python3` (standard library only, no pip packages).
- `ffmpeg`.
- `v4l2loopback` kernel module (provides the virtual webcam).
- A GoPro **HERO4 Session** with its WiFi enabled. Other HERO4 models expose a similar API and may work, but they are untested.

```bash
sudo apt install ffmpeg python3 v4l2loopback-dkms
```

---

## Installation

```bash
git clone https://github.com/<your-user>/hero4-session-webcam.git
cd hero4-session-webcam
chmod +x start_gopro_cam.sh
```

Recommended one-time step: make `v4l2loopback` load automatically at boot with the right parameters, so the launcher never needs `sudo` again (required for running at login, where no password can be typed):

```bash
./start_gopro_cam.sh --install-module
```

This writes `/etc/modprobe.d/gopro-v4l2loopback.conf` and `/etc/modules-load.d/gopro-v4l2loopback.conf`. Reboot once afterwards (or just run `./start_gopro_cam.sh`, which loads the module for the current session).

Optional: put a PNG called `conlost.png` (or `lost.png`) next to the script. It is shown whenever the camera is not sending video.

---

## Why this method

The obvious approaches do not work on the HERO4 Session, which is why this project uses the WiFi preview:

| Approach | Result |
|---|---|
| USB cable as webcam (UVC) | Not possible. Over USB the camera only exposes MTP (file transfer, "External Memory"). Its firmware has no UVC (webcam) support and no USB networking. Official USB webcam mode only exists on HERO8 and later. |
| HDMI output + capture card | Not possible. The Session has no HDMI port. |
| Firmware modification | Theoretically possible, but there is no known working method to get a video output this way, and the risk of making the camera unusable is real. Not worth it. |
| Connect the camera to the home WiFi as a client | The HERO4 Session firmware only works as an access point, so the PC must join the camera's network. |
| **WiFi live preview (this project)** | Works with the stock firmware, no app, no internet, no modification of the camera. The only cost is image quality (see below). |

The official GoPro app uses exactly the same live preview, so this project gives you the same image you see in the app, just as a webcam.

---

## How it works

```
GoPro (10.5.5.9)                         PC
  HTTP API  <------- start stream / status polling / keepalive ------- Camera control
  UDP 8554  ------- MPEG-TS (H.264, 432x240) -------> ffmpeg decoder (reader)
                                                            |
                                                   raw frames (pipe)
                                                            v
                                                   Python pump  <-- standby image
                                                            |
                                                            v
                                         ffmpeg writer (permanent) --> /dev/video10
                                                                         (v4l2loopback)
                                                                              |
                                                          Discord / Zoom / OBS / browser
```

- **Camera control.** The script talks to the camera's HTTP API at `10.5.5.9`. It asks for the live stream (`gpStream`, protocol v2), polls the camera status, and detects when the camera disappears or comes back.
- **Keepalive.** The camera only keeps sending video while it believes a client is watching. Two heartbeats are needed:
  - A small UDP packet (`_GPHD_:0:0:2:0.000000`, the same format the camera's own streaming software uses) sent to port 8554 every second. Without it the camera stops the stream after a few seconds.
  - HTTP requests to the status endpoint, like the official app makes. The Session does not start sending video until it registers a connected client (status `31` >= 1), which only happens after the status endpoint is polled. And while streaming, if it receives no HTTP requests for about 5 minutes, it considers the app gone: its screen shows **CONNECTION LOST** and it restarts its WiFi. The script therefore polls the status every 2.5 seconds for the whole session (`--app-poll`).
- **Decoder (reader).** An `ffmpeg` process receives the UDP MPEG-TS stream, decodes the H.264 video, applies the image filters and scales it to a fixed output size. This process is disposable: it is restarted whenever the stream stalls.
- **Pump.** A Python thread forwards every decoded frame to the writer as soon as it arrives (low-latency mode). If no fresh frame has arrived for 1.5 seconds, it sends the standby image instead, at the configured frame rate, so applications never see a frozen or dead camera.
- **Writer.** A second `ffmpeg` process owns `/dev/video10` for the entire life of the script. The device format never changes, so applications do not break when the camera disconnects, reconnects or restarts its stream. Because the writer starts immediately, the webcam is listed in applications even before the camera is turned on.
- **Watchdog.** If no video arrives for `--stall-timeout` seconds (6 by default), the stream is restarted. `ffmpeg` never exits on its own when a UDP source goes silent, so without this the bridge would hang forever. If the camera stops answering HTTP entirely, the script goes back to waiting for it.

---

## Image quality: the hard limit

**The live stream of the HERO4 Session is 432x240 pixels at about 0.7 Mbit/s.** This is measured (`--info`), and it is fixed by the camera's firmware:

- The recording settings (1080p, 30 fps, Wide, etc.) only affect what is recorded to the SD card. The live preview is a separate, small, heavily compressed video that the camera produces only for previewing.
- The camera reports two settings that would control the preview, `62` (stream bitrate, value 700000) and `64` (stream window size, value 2 = "240, 3:4 subsample"). The Session accepts requests to change them (HTTP 200) but silently ignores them. This was tested with the stream stopped, while streaming, and in video mode.

So the webcam image is soft and blocky by nature, especially when scaled up to 1280x720 (a 3x enlargement). The filters in this project ([Quality profiles](#quality-profiles-and-filters)) reduce compression blocks and noise and make the upscale as clean as possible, but they cannot recover detail that the camera never sends. If you need real 1080p live video, you need a different camera (a GoPro HERO8 or later, a regular webcam, or any camera with clean HDMI output and a capture card).

---

## Features

- **Plug-and-play reconnection.** Turn the camera on and off as many times as you want. The script waits, detects it, starts the stream, and recovers from stalls automatically.
- **Always-available webcam.** The virtual webcam exists and is selectable in applications as long as the script runs, even with the camera off.
- **Standby image.** Shows `conlost.png` (or `lost.png`) from the script's folder when there is no video; falls back to a "sin senal" ("no signal") text frame if no image exists. The image is scaled without distortion (black borders if needed; transparency is flattened onto black). Changing or adding the image while the script runs takes effect within about 3 seconds, no restart needed. Use `--standby-image` to point to another file.
- **Low-latency mode (default).** No input buffering in `ffmpeg`, minimal probing, single-threaded decoding, and frames forwarded the moment they are decoded instead of waiting for a clock tick. On a test bench this part of the pipeline adds only tens of milliseconds; most of the remaining delay comes from the camera's own encoder and the WiFi link.
- **Image quality profiles** (deblocking, denoise, high-quality scaler, sharpening), see below.
- **Orientation.** Mirror (`--mirror`) or rotate 180 degrees (`--rotate180`, useful when the camera is mounted upside down).
- **Any output size and frame rate**, plus three aspect-ratio handling modes (`--fit`).
- **Stream analysis** (`--info`): measures what the camera really sends (resolution, frame rate, bitrate) and tells you how much it is being enlarged.
- **Camera status dump** (`--dump`): prints all status values and settings of the camera as JSON.
- **Single instance.** A second copy of the script on the same device refuses to start and tells you the PID of the one already running, instead of fighting over the device.
- **Clear diagnostics.** If the virtual webcam cannot be opened, the script lists the programs that are holding it (read directly from `/proc`, no extra tools needed) or tells you that the module is stuck and how to fix it.
- **ffmpeg compatibility.** Detects which options the installed `ffmpeg` supports (`-fps_mode` on recent versions, `-vsync` on older ones) and which filters exist, and skips anything unavailable instead of failing. If `ffmpeg` rejects the low-latency options, the script falls back to a conservative mode automatically. A different `ffmpeg` binary can be selected with the `GOPRO_FFMPEG` environment variable.
- **Wake-on-LAN.** The camera's MAC address is learned automatically and saved; while waiting, the script periodically sends a Wake-on-LAN packet (it can also be given with `--mac`). Whether the Session actually wakes up from it depends on its state.
- **Experimental camera controls**: try to disable auto power-off, stop streaming if the camera reports overheating, try to set the stream bitrate. These come from community documentation of other HERO4 models and are not verified on the Session.

---

## Quality profiles and filters

Selected with `--quality` (default `high`). All filters work frame by frame, so they add practically no latency (on the test bench the difference between `fast` and `high` was about 5 ms).

| Profile | What it does |
|---|---|
| `fast` | Fast bilinear scaling, nothing else. Lowest CPU use. |
| `balanced` | Lanczos scaling plus a gentle luminance sharpening (`unsharp`, amount 0.6). |
| `high` (default) | Everything in `balanced`, plus **deblocking and deringing** (`pp=hb/vb/dr`) and a light **spatial/temporal denoise** (`hqdn3d`), both applied at the native 432x240 resolution before scaling, so compression artifacts are not enlarged. |

Fine-tuning:

- `--sharpen X` overrides the sharpening amount (`0` disables it; values between 0.3 and 1.0 are reasonable; too high exaggerates compression blocks).
- `--scaler {fast_bilinear,bilinear,bicubic,lanczos}` overrides the scaling algorithm chosen by the profile.
- `--fit` controls what happens when the camera's aspect ratio differs from the output size:
  - `stretch` (default): fill the frame. The Session's 432x240 is almost exactly 16:9, so the distortion is invisible.
  - `pad`: keep the aspect ratio and add black bars.
  - `crop`: keep the aspect ratio and crop the excess.
- `--size`: the output resolution. `1280x720` (default) is what most applications expect. `--size 432x240` uses the camera's native size, saves CPU, and lets the application do the scaling.

If the installed `ffmpeg` lacks a filter (for example, the `pp` deblocking filter was removed in ffmpeg 8), that filter is skipped automatically and the rest still apply.

---

## All options

Options are passed to `start_gopro_cam.sh` (which forwards them to the Python script) or directly to `gopro_session_webcam.py`.

| Option | Default | Description |
|---|---|---|
| `--ip IP[:PORT]` | `10.5.5.9` | Address of the camera's HTTP API. |
| `--device DEV` | `/dev/video10` | v4l2loopback device to write to. |
| `--size WxH` | `1280x720` | Fixed output resolution (even numbers). |
| `--fps N` | `30` | Output frame rate (used for the standby image and in conservative mode). |
| `--pixfmt {yuv420p,yuyv422}` | `yuv420p` | Pixel format of the virtual device. Use `yuyv422` if an application does not accept the webcam. |
| `--quality {fast,balanced,high}` | `high` | Image processing profile. |
| `--sharpen X` | profile | Sharpening amount (`0` = off). |
| `--scaler NAME` | profile | Scaling algorithm. |
| `--fit {pad,crop,stretch}` | `stretch` | Aspect-ratio handling. |
| `--standby-image PNG` | `conlost.png` / `lost.png` | Image shown when there is no video. |
| `--mirror` | off | Horizontal mirror. |
| `--rotate180` | off | Rotate 180 degrees. |
| `--no-low-latency` | off | Disable the low-latency optimizations (more conservative buffering and timing). |
| `--stall-timeout S` | `6` | Seconds without video before restarting the stream. |
| `--first-frame-timeout S` | `25` | Seconds to wait for the first frame after requesting the stream. |
| `--app-poll S` | `2.5` | Poll the camera status every S seconds while streaming, like the official app. Prevents the periodic CONNECTION LOST. `0` disables it. |
| `--info` / `--probe` | | Measure what the camera sends (resolution, fps, bitrate) and exit. |
| `--dump` | | Print the camera's full status and settings as JSON and exit. |
| `--stream-bitrate BPS` | | Experimental: request a stream bitrate (setting 62). Ignored by the Session in testing. |
| `--mac MAC` | learned | Camera MAC address for Wake-on-LAN. |
| `--no-autooff` | off | Experimental: try to disable the camera's auto power-off (setting 59, unverified). |
| `--thermal-guard` | off | Experimental: stop streaming for a while if the camera reports overheating (status 6, unverified). |
| `--clean-sd` | off | **Destructive**: deletes all media on the camera's SD card when it connects. Only relevant if a card is inserted. |
| `-v`, `--verbose` | off | Debug logging, including ffmpeg messages. |

Environment variables:

- `GOPRO_FFMPEG`: path of the `ffmpeg` binary to use.
- `GOPRO_DEV`: device used by the launcher (default `/dev/video10`), for example `GOPRO_DEV=/dev/video11 ./start_gopro_cam.sh`.

---

## Launcher commands

`start_gopro_cam.sh` checks dependencies, loads `v4l2loopback` with the right parameters if needed (`video_nr=10`, `card_label="GoPro Session"`, `exclusive_caps=1`), verifies that `/dev/video10` really is the virtual webcam and not a real camera, and starts the script.

```bash
./start_gopro_cam.sh                    # start the webcam (any option above can be appended)
./start_gopro_cam.sh --quality balanced --mirror
./start_gopro_cam.sh --info             # measure the camera's stream and exit
./start_gopro_cam.sh --stop             # stop the webcam (including the one started at login)
./start_gopro_cam.sh --reset            # stop everything, reload v4l2loopback, start again
./start_gopro_cam.sh --install-module   # load v4l2loopback automatically at every boot
```

`exclusive_caps=1` is needed for Chrome-based applications (Discord, Meet in Chrome, etc.) to list the device as a webcam.

---

## Running at login

Ubuntu "Startup Applications": add an entry with this command (adjust the path):

```
/home/<user>/hero4-session-webcam/start_gopro_cam.sh
```

Run `./start_gopro_cam.sh --install-module` once beforehand. At login there is no terminal to type a `sudo` password, so the module must already be loaded by the system.

To stop the instance started at login: `./start_gopro_cam.sh --stop`. To disable autostart, remove the entry in Startup Applications (or its file in `~/.config/autostart/`).

---

## Troubleshooting

**`ioctl(VIDIOC_G_FMT): Invalid argument` repeated in the log**

The virtual webcam cannot accept a new writer. The script prints a hint with the cause:
- *Another program has it open*: usually a second copy of the script, or an application (Discord, OBS, a browser tab) that kept the webcam open while the previous writer stopped. Close it and the script recovers on its own.
- *Nobody has it open*: `v4l2loopback` got stuck. Close any video call and run `./start_gopro_cam.sh --reset`.

**"Ya hay otra copia del script usando /dev/video10"**

A copy is already running (often the one started at login). Run `./start_gopro_cam.sh --stop` first.

**The webcam shows the standby image but the camera is on**

- Check that the PC is connected to the camera's WiFi: `ping 10.5.5.9`.
- Run with `-v` to see what happens.
- If the camera's WiFi is on but the stream never starts, turn the WiFi off and on again on the camera (rear button to APP, record button).

**The webcam does not appear in an application**

- The script must be running (`pgrep -af gopro_session_webcam`).
- Some applications only scan for cameras at startup: restart the application.
- Try `--pixfmt yuyv422`.

**The camera shows CONNECTION LOST every few minutes**

The camera drops clients that stop making HTTP requests. Make sure `--app-poll` is not set to `0`. If it still happens, run with `-v`: a warning `Estado 31 (clientes registrados) = 0 durante el stream` means the camera stopped counting the PC as a client; also check `journalctl -b -u NetworkManager` around the time of the drop for WiFi disconnections, and disable power saving on the WiFi adapter used for the camera.

**Laggy or choppy image**

- Keep the camera close to the WiFi adapter; the stream is UDP and lost packets show up as artifacts or stutter.
- A second WiFi adapter dedicated to the camera avoids sharing airtime with your internet traffic.

**Inspect the camera**

```bash
./start_gopro_cam.sh --dump      # full status/settings JSON
./start_gopro_cam.sh --info      # what the stream really is
```

---

## Limitations

- **Image quality** is capped at 432x240 and about 0.7 Mbit/s by the camera's firmware. See [Image quality: the hard limit](#image-quality-the-hard-limit).
- **WiFi only.** USB carries no video, and the PC must join the camera's own network.
- **No audio.** Only video is bridged (any audio in the stream is discarded); use the PC's microphone.
- **Latency.** Low, but not zero: the camera's preview encoder and the WiFi link add delay that no software on the PC can remove.
- **Battery and heat.** Streaming over WiFi drains the battery and warms the camera. For long sessions keep it on a charger and well ventilated.
- **Linux only.** It relies on `v4l2loopback`.
- **Experimental options** (`--stream-bitrate`, `--no-autooff`, `--thermal-guard`) use setting/status IDs documented by the community for other HERO4 models and are not verified on the Session.

---

## Safety notes

- This project does not modify the camera's firmware or files. It only uses the camera's normal WiFi API, the same one the official app uses.
- `--clean-sd` deletes all recordings on the inserted SD card. It is never enabled by default.
- Do not leave files such as `cal.txt` or `no_shutdown.txt` on the SD card from firmware-hacking guides. They change how the camera boots and can prevent the WiFi/app mode from working normally.
