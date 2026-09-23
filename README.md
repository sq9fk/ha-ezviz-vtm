# EZVIZ for Home Assistant with VTM cloud live view

A `custom_components` override of the Home Assistant core
[EZVIZ integration](https://www.home-assistant.io/integrations/ezviz/) (based on
HA **2026.9.3**) that restores live view for EZVIZ devices without RTSP:
battery cameras, doorbells such as the DP2C, and cameras whose firmware
removed RTSP ([home-assistant/core#125311](https://github.com/home-assistant/core/issues/125311)).

## How it works

The EZVIZ apps show live video through the **VTM cloud relay** (MPEG-PS over
TCP). [pyezvizapi](https://github.com/RenierM26/pyEzvizApi) can open that relay.
This integration:

1. Detects devices without RTSP from the cloud `CONNECTION.localRtspPort == 0`
   (or cameras without RTSP credentials).
2. Serves the relay at a local URL,
   `http://127.0.0.1:<port>/api/ezviz/vtm/<random token>/<serial>.ts`. FFmpeg
   remuxes it to MPEG-TS with codec copy, with no transcoding.
3. Returns that URL from `stream_source()`, so the regular camera card, HLS
   (`stream`) and WebRTC (bundled go2rtc) all work.

RTSP cameras keep using RTSP.

Still images do **not** wake the device. While a live stream is running, the
still image is its latest keyframe. Otherwise it is the last alarm picture.
A battery device is only woken while someone watches live.

## Changes against core 2026.9.3

- `vtm.py` (new): VTM relay → MPEG-TS HTTP view.
- `camera.py`:
  - VTM fallback for the stream source;
  - still images that don't wake the device;
  - no RTSP-credentials discovery prompt for devices without RTSP.
- `manifest.json`: pyezvizapi from GitHub (VTM support is not on PyPI yet); `http` dependency.
- `select.py`: the battery work mode is now reported as a number by pyezvizapi.
- `switch.py`: `SupportExt.SupportFulldayRecord` was renamed `SupportFullDayRecord`.
- `translations/en.json`: generated from `strings.json`. Custom integrations need compiled translations.

## Install

1. Copy `custom_components/ezviz` to `/config/custom_components/ezviz`.
2. Restart Home Assistant. The first start installs pyezvizapi from GitHub, so
   it needs internet access. HA logs a warning that the custom `ezviz`
   integration overrides the core one. That is expected.
3. The existing EZVIZ config entry keeps working; no reconfiguration needed.

To go back to the core integration, delete the folder and restart.

## Requirements and limits

- Live view depends on the EZVIZ cloud and internet access.
- **Video encryption must be off** for the device in the EZVIZ app. Encrypted
  VTM streams are not supported yet.
- The first frame arrives after about 5–10 s: the device wakes up, then the
  stream starts.
- Every viewer opens its own relay session. On battery devices, keep live
  views short.
- Video is passed through as sent (DP2C: HEVC 1080p15 + AAC). WebRTC needs a
  browser with HEVC support. Otherwise the frontend falls back to HLS.

## Tested

HA 2026.9.3 (Docker, Python 3.14) with an EZVIZ DP2C (firmware V5.3.3 build 250911):

- the integration loads all platforms;
- the HA `stream` worker has its first keyframe after about 6 s;
- the go2rtc 1.9.14 source works;
- still images without a stream return the last alarm picture.
