"""Play event recordings from the device storage of EZVIZ cameras on demand.

The ``ezviz.get_clip_url`` action returns a link for one event. Opening it shows
a small player page and starts the download: the device is woken, the
recording that covers the event is found in its storage index and fetched
through the EZVIZ cloud (pyezvizapi cloud playback), then remuxed to MP4 and
kept in the media folder. Nothing is downloaded until someone opens the link,
so the battery of the device is only used for recordings that are watched.

Links are signed per event with a secret kept in HA storage, so they keep
working after restarts and can't be changed to reach other recordings.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import hmac
import html
import logging
from pathlib import Path
import secrets
import subprocess
import time
from zoneinfo import ZoneInfo

from aiohttp import web
from pyezvizapi.client import EzvizClient
from pyezvizapi.exceptions import PyEzvizError
from pyezvizapi.streaming.capture import ClipOptions, CloudPlaybackClipSource

from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

CLIP_URL = "/api/ezviz/clip/{serial}/{name}"
STAMP_FORMAT = "%Y%m%dT%H%M%S"
# Recordings older than this are deleted from the media folder.
KEEP_DAYS = 30
# A recording may start a little before the event and is only listed once
# the device has finished writing it.
SEARCH_BEFORE = timedelta(minutes=2)
SEARCH_AFTER = timedelta(minutes=3)
MATCH_SLACK = timedelta(seconds=10)
FIND_ATTEMPTS = 4
FIND_RETRY_DELAY = 20.0
WAKE_DELAY = 5.0

STORAGE_KEY = f"{DOMAIN}.clips"
STORAGE_VERSION = 1


@dataclass
class ClipCamera:
    """A camera whose recordings can be played."""

    client: EzvizClient


@dataclass
class ClipData:
    """Shared state of the clip view."""

    secret: bytes
    cameras: dict[str, ClipCamera]
    downloads: dict[str, asyncio.Task[Path]]


DATA_CLIPS: HassKey[ClipData] = HassKey(f"{DOMAIN}_clips")
_SETUP_LOCK: HassKey[asyncio.Lock] = HassKey(f"{DOMAIN}_clips_lock")


async def async_register_clip_camera(
    hass: HomeAssistant, entry: ConfigEntry, serial: str, client: EzvizClient
) -> None:
    """Make the recordings of a camera playable until its entry unloads."""
    lock = hass.data.setdefault(_SETUP_LOCK, asyncio.Lock())
    async with lock:
        if (data := hass.data.get(DATA_CLIPS)) is None:
            store: Store[dict[str, str]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
            stored = await store.async_load() or {}
            if not (secret := stored.get("secret")):
                secret = secrets.token_hex(32)
                await store.async_save({"secret": secret})
            data = hass.data[DATA_CLIPS] = ClipData(bytes.fromhex(secret), {}, {})
            hass.http.register_view(EzvizClipView())

    camera = data.cameras[serial] = ClipCamera(client)

    @callback
    def _unregister() -> None:
        if data.cameras.get(serial) is camera:
            del data.cameras[serial]

    entry.async_on_unload(_unregister)


def clip_url(hass: HomeAssistant, serial: str, event_time: datetime) -> str:
    """Return the signed link to the recording of one event.

    The link is absolute when Home Assistant knows its URL. The companion
    apps open relative links in their dashboard view, which only shows the
    Home Assistant frontend, and absolute links in the browser.
    """
    if (data := hass.data.get(DATA_CLIPS)) is None or serial not in data.cameras:
        raise HomeAssistantError(f"Recordings of {serial} are not available")
    if event_time.tzinfo is not None:
        event_time = dt_util.as_local(event_time)
    stamp = event_time.strftime(STAMP_FORMAT)
    path = CLIP_URL.format(serial=serial, name=stamp)
    link = f"{path}?sig={_sign(data.secret, serial, stamp)}"
    try:
        base_url = get_url(hass, prefer_external=True)
    except NoURLAvailableError:
        return link
    return f"{base_url}{link}"


def _sign(secret: bytes, serial: str, stamp: str) -> str:
    digest = hmac.new(secret, f"{serial}/{stamp}".encode(), hashlib.sha256)
    return digest.hexdigest()[:32]


def _clip_dir(hass: HomeAssistant, serial: str) -> Path:
    media = hass.config.media_dirs.get("local") or hass.config.path("media")
    return Path(media) / "ezviz" / serial


class EzvizClipView(HomeAssistantView):
    """Player page, download status and MP4 file of one event recording."""

    url = CLIP_URL
    name = "api:ezviz:clip"
    requires_auth = False

    async def get(
        self, request: web.Request, serial: str, name: str
    ) -> web.StreamResponse:
        """Serve the page (``<stamp>``), status (``.json``) or file (``.mp4``)."""
        hass = request.app[KEY_HASS]
        stamp, _, kind = name.partition(".")
        data = hass.data.get(DATA_CLIPS)
        sig = request.query.get("sig", "")
        if (
            data is None
            or serial not in data.cameras
            or kind not in ("", "json", "mp4")
            or not hmac.compare_digest(
                sig.encode(), _sign(data.secret, serial, stamp).encode()
            )
        ):
            raise web.HTTPNotFound
        try:
            event_time = datetime.strptime(stamp, STAMP_FORMAT)
        except ValueError as err:
            raise web.HTTPNotFound from err

        target = _clip_dir(hass, serial) / f"{stamp}.mp4"
        if kind == "mp4":
            if not await hass.async_add_executor_job(target.is_file):
                raise web.HTTPNotFound
            return web.FileResponse(target, headers={"Cache-Control": "no-store"})

        if await hass.async_add_executor_job(target.is_file):
            status = {"state": "ready"}
        else:
            status = _status(_get_download(hass, data, serial, event_time, target))
        if kind == "json":
            return web.json_response(status)
        return web.Response(
            text=_page(serial, stamp, sig), content_type="text/html", charset="utf-8"
        )


@callback
def _get_download(
    hass: HomeAssistant,
    data: ClipData,
    serial: str,
    event_time: datetime,
    target: Path,
) -> asyncio.Task[Path]:
    """Return the download of a recording, (re)starting it if needed."""
    key = f"{serial}/{target.stem}"
    task = data.downloads.get(key)
    if task is not None and not (task.done() and task.exception() is not None):
        return task
    task = hass.async_create_background_task(
        _async_download(hass, data.cameras[serial].client, serial, event_time, target),
        f"ezviz clip {key}",
    )
    data.downloads[key] = task
    task.add_done_callback(
        lambda _: hass.loop.call_later(600, data.downloads.pop, key, None)
    )
    return task


def _status(task: asyncio.Task[Path]) -> dict[str, str]:
    if not task.done():
        return {"state": "downloading"}
    if task.cancelled():
        return {"state": "error", "message": "Download cancelled"}
    if (err := task.exception()) is not None:
        return {"state": "error", "message": str(err)}
    return {"state": "ready"}


async def _async_download(
    hass: HomeAssistant,
    client: EzvizClient,
    serial: str,
    event_time: datetime,
    target: Path,
) -> Path:
    """Find, fetch and remux the recording that covers an event."""
    ffmpeg = get_ffmpeg_manager(hass).binary
    time_zone = hass.config.time_zone
    try:
        return await hass.async_add_executor_job(
            _download, client, serial, event_time, time_zone, ffmpeg, target
        )
    except PyEzvizError as err:
        _LOGGER.warning("%s: cannot download recording: %s", serial, err)
        raise HomeAssistantError(f"Cannot download the recording: {err}") from err


def _download(
    client: EzvizClient,
    serial: str,
    event_time: datetime,
    time_zone: str,
    ffmpeg: str,
    target: Path,
) -> Path:
    begin, end = _find_recording(client, serial, event_time)
    zone = ZoneInfo(time_zone)
    begin_utc = begin.replace(tzinfo=zone).astimezone(dt_util.UTC)
    end_utc = end.replace(tzinfo=zone).astimezone(dt_util.UTC)
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = target.with_suffix(".ps.part")
    part = target.with_suffix(".mp4.part")
    try:
        _LOGGER.debug("%s: downloading recording %s - %s", serial, begin, end)
        client.save_clip_with_options(
            serial,
            str(raw),
            ClipOptions(
                source=CloudPlaybackClipSource(
                    begin_utc.strftime("%Y%m%dT%H%M%SZ"),
                    end_utc.strftime("%Y%m%dT%H%M%SZ"),
                    timezone=time_zone,
                    timeout=30.0,
                )
            ),
        )
        _remux(ffmpeg, raw, part)
        part.replace(target)
    finally:
        raw.unlink(missing_ok=True)
        part.unlink(missing_ok=True)
    _cleanup(target.parent)
    return target


def _find_recording(
    client: EzvizClient, serial: str, event_time: datetime
) -> tuple[datetime, datetime]:
    """Return the local begin and end of the recording covering the event."""
    start = (event_time - SEARCH_BEFORE).strftime("%Y-%m-%d %H:%M:%S")
    stop = (event_time + SEARCH_AFTER).strftime("%Y-%m-%d %H:%M:%S")
    last_error: PyEzvizError | None = None
    for attempt in range(FIND_ATTEMPTS):
        if attempt:
            time.sleep(FIND_RETRY_DELAY if last_error is None else WAKE_DELAY)
        try:
            # A sleeping battery device doesn't answer the index search.
            client.get_detection_sensibility(serial)
            response = client.search_records(serial, 1, serial, start, stop, size=20)
        except PyEzvizError as err:
            last_error = err
            continue
        last_error = None
        if (
            match := _match_record(client.extract_record_list(response), event_time)
        ) is not None:
            return match
    if last_error is not None:
        raise last_error
    raise PyEzvizError(f"No recording found for {event_time:%Y-%m-%d %H:%M:%S}")


def _match_record(
    records: list, event_time: datetime
) -> tuple[datetime, datetime] | None:
    spans = []
    for item in records:
        if not isinstance(item, dict):
            continue
        try:
            begin = datetime.fromisoformat(str(item.get("begin") or item.get("B")))
            end = datetime.fromisoformat(str(item.get("end") or item.get("E")))
        except ValueError:
            continue
        spans.append((begin.replace(tzinfo=None), end.replace(tzinfo=None)))
    covering = [
        span
        for span in spans
        if span[0] - MATCH_SLACK <= event_time <= span[1] + MATCH_SLACK
    ]
    if covering:
        return min(covering, key=lambda span: abs(span[0] - event_time))
    later = [
        span
        for span in spans
        if event_time <= span[0] <= event_time + timedelta(minutes=1)
    ]
    return min(later) if later else None


def _remux(ffmpeg: str, source: Path, target: Path) -> None:
    """Copy the streams into MP4 that phones and browsers can play."""
    base = [ffmpeg, "-v", "error", "-y", "-i", str(source), "-c", "copy"]
    tail = ["-movflags", "+faststart", "-f", "mp4", str(target)]
    # hvc1 lets Apple players play HEVC; it is rejected for other codecs.
    for extra in (["-tag:v", "hvc1"], []):
        result = subprocess.run(
            [*base, *extra, *tail], capture_output=True, check=False, timeout=120
        )
        if result.returncode == 0:
            return
    raise PyEzvizError(
        f"FFmpeg cannot remux the recording: {result.stderr.decode(errors='replace')[-300:]}"
    )


def _cleanup(folder: Path) -> None:
    cutoff = time.time() - KEEP_DAYS * 86400
    for path in folder.glob("*.mp4"):
        if path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)


def _page(serial: str, stamp: str, sig: str) -> str:
    base = html.escape(CLIP_URL.format(serial=serial, name=stamp))
    sig = html.escape(sig)
    when = datetime.strptime(stamp, STAMP_FORMAT).strftime("%Y-%m-%d %H:%M:%S")
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(serial)} {when}</title>
<style>
body{{margin:0;background:#000;color:#ddd;font:16px system-ui,sans-serif;
display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:100vh}}
video{{width:100%;max-height:90vh;display:none}}p{{padding:0 16px;text-align:center}}
</style></head><body>
<video id="v" controls playsinline autoplay muted></video>
<p id="m">Downloading the recording of {when} from the device. This takes about a minute.</p>
<script>
const v=document.getElementById("v"),m=document.getElementById("m");
async function poll(){{
  try{{
    const r=await fetch("{base}.json?sig={sig}",{{cache:"no-store"}});
    const s=await r.json();
    if(s.state==="ready"){{v.src="{base}.mp4?sig={sig}";v.style.display="block";m.textContent="{when}";return;}}
    if(s.state==="error"){{m.textContent="Cannot download the recording: "+s.message;return;}}
  }}catch(e){{}}
  setTimeout(poll,2000);
}}
poll();
</script></body></html>
"""
