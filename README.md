# LCClips

A lightweight replay-buffer clipper for Windows, similar to ShadowPlay "Instant Replay", Medal, or Discord clips.
It keeps the last *N* seconds of your screen, game audio and mic in a rolling buffer. Press a hotkey and those seconds are saved as an `.mp4`.

## Why it's light on game performance

| Stage | How | Cost |
|---|---|---|
| Screen capture | FFmpeg `ddagrab` (Windows Desktop Duplication); frames stay on the GPU as D3D11 textures | ~0 CPU |
| Encoding | Your GPU's dedicated video encoder (NVENC / AMF / QuickSync), auto-detected | separate silicon, 1–3% game FPS |
| Resize (optional) | GPU scaler when available (`scale_d3d11`, `scale_cuda`, `scale_qsv`), otherwise CPU fallback | small |
| Audio | WASAPI loopback (what you hear) + mic, mixed in Python → AAC | <1% CPU |
| Buffer | 1-second segments in a wrap-around ring on disk (`%APPDATA%\LCClips\buffer`) | a few MB/s of writes |
| Saving a clip | Joins the segments with stream copy, no re-encoding | <1 s |

FFmpeg runs at *below-normal* process priority so the game always wins.

## Setup

1. Install **Python 3.10+** from python.org.
2. Double-click `run.bat`. The first run creates a virtual environment and installs the dependencies.
3. **FFmpeg:** if LCClips can't find it, it offers to download it for you, about 150 MB one time. It saves the static
   BtbN build (which includes the NVENC, AMF and QSV encoders) to `%APPDATA%\LCClips\bin`. You can also click
   **Download** next to *FFmpeg path* at any time.
   To install it by hand instead, use FFmpeg 7.1 or newer (8+ recommended), then either put it on your `PATH` or Browse to `ffmpeg.exe` in the app.

Settings are saved to `%APPDATA%\LCClips\config.json`. Logs are in the same folder (`lcclips.log`, `ffmpeg.log`).

## The window

LCClips uses the same look as the other LC apps (LCBot and LCAI): the same theme presets, flat buttons and orange accents. You can switch presets in the **Themes** tab.

- **Clips tab**
  - **Recent clips**: thumbnail cards of your newest clips. Click one to select it; double-click to play it.
  - **All clips**: every clip in your clips folder, with search, length, resolution, size and whether it's shared.
  - **Selected clip** panel: a preview, the clip's details and share link, and the buttons **Share**, **Copy link**,
    **Download** (save a copy anywhere), **Open folder**, **Play** and **Delete** (moves the file to the Recycle Bin).
- **Settings tab**: Capture, Audio, Files & behavior, and Share links.
- **Top bar**: a recording indicator, status messages, your hotkey and a **Save clip** button.

## Settings

- **Hotkey**: default `Alt+F10`. Click *Record…* and press any combination.
- **Clip length**: 5–600 s.
- **Monitor**: which screen to capture. Use *Detect* to list them.
- **Resolution**: Native, or downscale to 1440p, 1080p, 720p and so on.
- **FPS / Bitrate / Encoder**: `auto` picks NVENC → AMF → QSV → x264 (CPU, last resort).
- **Game audio**: loopback of an output device. `Default` follows whatever Windows is playing to, including when you switch headsets.
- **Microphone**: any input device, or *None*.
- **Volumes and audio sync offset**: fine-tune the mix and alignment.

## Share links (like Medal)

LCClips can upload a clip and copy a short link to your clipboard. Discord and X play the video inline from that link.
It uses your own free Cloudflare server. To set it up, follow **[server/README.md](server/README.md)**: you enable R2 once, then run `server\setup.bat`.

- **Share last clip** (in the app or the tray icon): uploads the most recent clip and copies the link.
- **Auto-upload new clips**: every hotkey clip gets a link automatically.
- **Upload bitrate**: before uploading, LCClips makes a smaller copy on the GPU (8 Mbps by default), so uploads are about 2–3× faster. The full-quality original stays on your PC. Set it to 0 to upload the original.
- Copy a clip's link again, or remove it from the server, from the **Selected clip** panel.

## Tray and startup

Clicking **X** hides LCClips to the system tray, and it keeps recording. Left-click the tray icon to open the settings again.
To exit, right-click the tray icon and choose **Close LCClips**. You can turn this off with *Close button hides to tray*.
If you don't see the icon, click the **^** arrow on the taskbar. You can drag the icon out of there so it's always visible.

Start LCClips hidden with `run.bat --minimized`. To launch it at login, put a shortcut to that command in `shell:startup`.

## Standalone .exe

Put `ffmpeg.exe` in `bin\`, then run `build.bat`. The output is in `dist\LCClips\`.

## Troubleshooting

- **Black or frozen video in a game**: use *Borderless Windowed*. True exclusive fullscreen can block Desktop Duplication on some older DX9/DX11 titles.
- **Black video on a laptop**: Desktop Duplication must run on the GPU that drives the display. In Windows Graphics Settings, set `ffmpeg.exe` to the GPU attached to that screen.
- **Hotkey doesn't work in one game**: that game is probably running as administrator. Run LCClips as administrator too.
- **Audio slightly early or late**: adjust *Audio sync offset* (positive values delay the audio).
- **Clips choppy at high settings**: lower the bitrate or resolution, or check that `ffmpeg.log` shows a hardware encoder in use.

## Project layout

```
lcclips/config.py   settings dataclass and JSON persistence
lcclips/audio.py    loopback + mic capture threads, mixer clocked to real time
lcclips/capture.py  FFmpeg command builder, GPU encoder probing, segment ring, clip saving
lcclips/app.py      main window (Clips / Settings / Themes), hotkey, tray icon
lcclips/library.py  clip scanning, metadata cache, thumbnails, file actions
lcclips/gui/theme.py  LC-line theme presets (shared look with LCBot)
lcclips/share.py    upload client (stdlib only), thumbnails, upload history
server/             Cloudflare Worker + R2 share server, one-click setup
```

## Ideas for next steps

- Share hotkey and clip trimming before upload.
- Per-application audio (game only, without Discord or music) using the Windows process-loopback API.
- Separate audio tracks for game and mic.
- HEVC/AV1 output and a trim/preview window.
