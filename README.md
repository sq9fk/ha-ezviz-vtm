# EZVIZ for Home Assistant with VTM cloud live view

A `custom_components` override of the Home Assistant core
[EZVIZ integration](https://www.home-assistant.io/integrations/ezviz/) (based on
HA **2026.11.2**) that restores live view for EZVIZ devices without RTSP:
battery cameras, doorbells such as the DP2C, and cameras whose firmware
removed RTSP ([home-assistant/core#125311](https://github.com/home-assistant/core/issues/125311)).

## How it works

The EZVIZ apps show live video through the **EZVIZ cloud**.
[pyezvizapi](https://github.com/RenierM26/pyEzvizApi) 2.6.0 turns that into a
ready MPEG-TS stream (`open_live_mpegts_stream`), handling the relay, the FFmpeg
remux and encryption checks. This integration:

1. Uses the cloud stream for every camera without configured RTSP credentials
   (no camera config entry). Configured RTSP credentials always win. When the
   cloud reports no RTSP port (`reported_local_rtsp_port == 0`), it doesn't ask
   for RTSP credentials either.
2. Serves the stream on the HA internal URL,
   `<internal URL>/api/ezviz/vtm/<serial>.ts?auth=<per-camera token>`.
3. Returns that URL from `stream_source()`, so the regular camera card, HLS
   (`stream`) and WebRTC (bundled go2rtc) all work.

Cameras with configured RTSP keep using RTSP.

Still images do **not** wake the device. While a live stream is running, the
still image is its latest keyframe. Otherwise it is the last alarm picture.
A battery device is only woken while someone watches live.

## Changes against core 2026.11.2

- `vtm.py` (new): HTTP view serving the pyezvizapi live MPEG-TS stream.
- `camera.py`:
  - cloud stream fallback for the stream source;
  - still images that don't wake the device;
  - no RTSP-credentials discovery prompt for devices without RTSP.
- `manifest.json`: pyezvizapi 1.0.5.0 → 2.6.0 (adds the live MPEG-TS stream API); `http` dependency.
- `translations/en.json`: generated from `strings.json`. Custom integrations need compiled translations.

## Install

Requires Home Assistant 2026.11 or newer.

### With HACS

1. In HACS, open the menu (⋮) > **Custom repositories**.
2. Add `https://github.com/sq9fk/ha-ezviz-vtm` with the type **Integration**.
3. Search for **EZVIZ (VTM live view)**, select **Download**, and restart Home Assistant.

HACS then offers updates of this repository like any other integration.

### Manually

1. Copy `custom_components/ezviz` to `/config/custom_components/ezviz`.
2. Restart Home Assistant. The first start installs pyezvizapi 2.6.0 from PyPI, so
   it needs internet access. HA logs a warning that the custom `ezviz`
   integration overrides the core one. That is expected.
3. The existing EZVIZ config entry keeps working; no reconfiguration needed.

To go back to the core integration, delete the folder and restart.

## Requirements and limits

- Live view depends on the EZVIZ cloud and internet access.
- **Video encryption must be off** for the device in the EZVIZ app. Encrypted
  streams are not supported yet.
- The first frame arrives after about 6–10 s: a sleeping battery device needs
  about 10 s to wake up, so the stream allows 30 s to start.
- Every viewer opens its own cloud session. On battery devices, keep live
  views short.
- Video is passed through as sent (DP2C: HEVC 1080p15 + AAC). WebRTC needs a
  browser with HEVC support. Otherwise the frontend falls back to HLS.

## Tested

Home Assistant stable (Docker, Python 3.14) with pyezvizapi 2.6.0 and an
EZVIZ DP2C (firmware V5.3.3 build 250911):

- the integration loads all platforms;
- direct playback of the stream URL: first frame after about 9 s from sleep;
- the HA `stream` worker has its first keyframe after about 7 s;
- the bundled go2rtc source returns a frame after about 6 s;
- still images without a stream return the last alarm picture.
