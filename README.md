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
2. Get **FFmpeg 7.1 or newer** (a "full" Windows build from gyan.dev or BtbN). Do one of the following:
   - put it on your `PATH`, or
   - put `ffmpeg.exe` in `lcclips\bin\`, or
   - set the path in the app.

   FFmpeg 8+ is recommended, because it adds `scale_d3d11` for GPU-side resizing on NVIDIA and AMD.
3. Double-click `run.bat`. The first run creates a virtual environment and installs the dependencies.

Settings are saved to `%APPDATA%\LCClips\config.json`. Logs are in the same folder (`lcclips.log`, `ffmpeg.log`).

## Settings

- **Hotkey**: default `Alt+F10`. Click *Record…* and press any combination.
- **Clip length**: 5–600 s.
- **Monitor**: which screen to capture. Use *Detect* to list them.
- **Resolution**: Native, or downscale to 1440p, 1080p, 720p and so on.
- **FPS / Bitrate / Encoder**: `auto` picks NVENC → AMF → QSV → x264 (CPU, last resort).
- **Game audio**: loopback of an output device. `Default` follows whatever Windows is playing to, including when you switch headsets.
- **Microphone**: any input device, or *None*.
- **Volumes and audio sync offset**: fine-tune the mix and alignment.

Closing the window keeps LCClips running in the system tray. Start it hidden with `run.bat --minimized`. To launch it at login, put a shortcut to that command in `shell:startup`.

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
lcclips/app.py      tkinter settings UI, global hotkey, tray icon
```

## Ideas for next steps

- Per-application audio (game only, without Discord or music) using the Windows process-loopback API.
- Separate audio tracks for game and mic.
- HEVC/AV1 output and a trim/preview window.
