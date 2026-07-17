import logging
import time

from asgiref.sync import async_to_sync
from celery import shared_task
from channels.layers import get_channel_layer

from devices.models import Device
from devices.utils import _get_redis, regenerate_config_and_restart

logger = logging.getLogger(__name__)

MAX_FAILURE_COUNT = 3
FPS_MIN_THRESHOLD = 6
FPS_ZERO_CYCLES = 30  # 30 × 5s = 150s — enough for engine rebuild + stream connect
FPS_LOW_CYCLES = 36   # 36 × 5s = 180s — give time for stable FPS
OFFLINE_RESTART_SECONDS = 120


def _broadcast_device_status(device_id, online):
    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        f"device_{device_id}",
        {
            "type": "device_status",
            "device_id": str(device_id),
            "online": online,
        },
    )


@shared_task
def orchestrate_cameras():
    r = _get_redis()
    from devices.config_generator import MAX_INSTANCES, PIPELINE_CONFIGS

    sources = {}
    for pipeline_id in ("main", "retinaface", "yolov9", "trafficcamnet_lpr"):
        for n in range(1, MAX_INSTANCES + 1):
            data = r.hgetall(f"deepstream:sources:{pipeline_id}:{n}")
            for k, v in data.items():
                if isinstance(k, bytes):
                    k = k.decode()
                sources[k] = v

    need_restart = False

    for device in Device.objects.all():
        cid = str(device.id)

        if device.source_type == "file":
            device.is_online = True
            Device.objects.filter(id=device.id).update(is_online=True)
            continue

        if not device.username or not device.password:
            continue

        try:
            from onvif_utils.drivers import get_driver

            driver = get_driver(device)
            ping_result = driver.ping()
            online = ping_result["online"]
            last_seen = ping_result["last_seen"]
        except Exception:
            online = False
            last_seen = None

        status_changed = False
        if online:
            if device.failure_count > 0 or not device.is_online:
                device.failure_count = 0
                if not device.is_online:
                    device.is_online = True
                    status_changed = True
                    _broadcast_device_status(device.id, True)
                    r.delete(f"device:{cid}:offline_since")
                Device.objects.filter(id=device.id).update(
                    is_online=True, failure_count=0, last_seen=last_seen
                )
                if not status_changed:
                    device.is_online = True
                    device.failure_count = 0
            if last_seen:
                device.last_seen = last_seen
            if not status_changed:
                device.save(
                    update_fields=["is_online", "failure_count", "last_seen"]
                )
            r.delete(f"device:{cid}:offline_since")
        else:
            device.failure_count += 1
            if device.failure_count >= MAX_FAILURE_COUNT and device.is_online:
                device.is_online = False
                status_changed = True
                _broadcast_device_status(device.id, False)
                r.setex(f"device:{cid}:offline_since", 86400, str(int(time.time())))
            device.save(update_fields=["failure_count", "is_online"])

        source_id = None
        for k, v in sources.items():
            if isinstance(k, bytes):
                k = k.decode()
            if k.endswith(":camera_id") or k.endswith(":fps") or k.endswith(":url"):
                continue
            if isinstance(v, bytes):
                v = v.decode()
            if v == cid:
                source_id = int(k)
                break

        if device.is_online and source_id is not None and device.deepstream_pipeline:
            pipeline = device.deepstream_pipeline
            current_fps = 0
            for n in range(1, MAX_INSTANCES + 1):
                fps = r.hget(f"deepstream:sources:{pipeline}:{n}", f"{source_id}:fps")
                if fps:
                    current_fps = int(fps)
                    break

            if current_fps == 0:
                key = f"device:{cid}:fps_zero"
                count = r.incr(key)
                if count == 1:
                    r.expire(key, 180)
                if count >= FPS_ZERO_CYCLES:
                    r.delete(key)
                    r.setex(f"device:{cid}:pending_restart", 3600, "1")
                    need_restart = True
                    logger.warning(
                        "Device %s FPS=0 for %d cycles, triggering restart",
                        cid, FPS_ZERO_CYCLES,
                    )
            elif current_fps < FPS_MIN_THRESHOLD:
                key = f"device:{cid}:fps_low"
                count = r.incr(key)
                if count == 1:
                    r.expire(key, 180)
                if count >= FPS_LOW_CYCLES:
                    r.delete(key)
                    r.setex(f"device:{cid}:pending_restart", 3600, "1")
                    need_restart = True
                    logger.warning(
                        "Device %s FPS=%d < %d for %d cycles, triggering restart",
                        cid, current_fps, FPS_MIN_THRESHOLD, FPS_LOW_CYCLES,
                    )
            else:
                r.delete(f"device:{cid}:fps_zero")
                r.delete(f"device:{cid}:fps_low")

        elif not device.is_online:
            offline_since = r.get(f"device:{cid}:offline_since")
            if offline_since:
                elapsed = time.time() - int(offline_since)
                if elapsed > OFFLINE_RESTART_SECONDS:
                    pending = r.get(f"device:{cid}:pending_restart")
                    if not pending:
                        r.setex(f"device:{cid}:pending_restart", 3600, "1")
                        need_restart = True

        specs = device.camera_specs or {}
        if specs.get("ptz_caps"):
            try:
                from devices.utils import get_active_preset_for_device

                active = get_active_preset_for_device(device)
                token = active.preset_token if active else ""
                r.setex(f"device:{cid}:active_preset", 120, token)
            except Exception:
                r.setex(f"device:{cid}:active_preset", 120, "")
        else:
            r.setex(f"device:{cid}:active_preset", 120, "__fixed__")

    if need_restart:
        regenerate_config_and_restart()
        r = _get_redis()
        for device in Device.objects.all():
            cid = str(device.id)
            r.delete(f"device:{cid}:fps_zero")
            r.delete(f"device:{cid}:fps_low")
            r.delete(f"device:{cid}:pending_restart")


@shared_task
def patrol_controller():
    from devices.models import Patrol
    from django.utils import timezone

    r = _get_redis()
    now = timezone.localtime()

    for patrol in Patrol.objects.filter(is_active=True).select_related("device"):
        if not patrol.preset_order:
            continue

        if not _patrol_in_schedule(patrol, now):
            r.delete(f"patrol:{patrol.id}:index")
            continue

        device = patrol.device
        if not device.is_online or not device.username:
            continue

        specs = device.camera_specs or {}
        if not specs.get("ptz_caps"):
            continue

        cid = str(device.id)
        lock_key = f"patrol:lock:{cid}"
        lock = r.set(lock_key, str(patrol.id), nx=True, ex=15)
        if not lock:
            continue

        try:
            token = device.default_profile_token
            if not token:
                continue

            from onvif_utils.client import OnvifClient
            from onvif_utils.ptz import PTZService

            client = OnvifClient(device.host, device.port, device.username, device.password)
            ptz = PTZService(client)

            if ptz.is_moving(token):
                r.setex(f"patrol:{cid}:moving", 30, "1")
                continue

            r.delete(f"patrol:{cid}:moving")

            presets = patrol.preset_order
            index_key = f"patrol:{patrol.id}:index"
            current_idx = int(r.get(index_key) or 0) % len(presets)

            next_move_key = f"patrol:{patrol.id}:next_move"
            next_move = r.get(next_move_key)
            if next_move and float(next_move) > time.time():
                continue

            preset_token = presets[current_idx]
            ptz.goto_preset(token, preset_token, patrol.speed)

            next_idx = (current_idx + 1) % len(presets)
            r.set(index_key, str(next_idx))
            r.setex(next_move_key, patrol.dwell_seconds + 60, str(time.time() + patrol.dwell_seconds))

            logger.info(
                "Patrol %s device=%s preset=%s (%d/%d) dwell=%ds",
                patrol.name, device.id, preset_token,
                current_idx + 1, len(presets), patrol.dwell_seconds,
            )
        except Exception as e:
            logger.warning("patrol_controller %s: %s", patrol.name, e)
        finally:
            r.delete(lock_key)


def _patrol_in_schedule(patrol, now):
    if patrol.valid_from and now < patrol.valid_from:
        return False
    if patrol.valid_until and now > patrol.valid_until:
        return False
    if patrol.schedule:
        today = now.strftime("%a").lower()[:3]
        blocks = patrol.schedule.get(today, [])
        if not blocks:
            return False
        t = now.strftime("%H:%M")
        if not any(start <= t < end for start, end in blocks):
            return False
    return True


@shared_task
def refresh_device_streams(device_id):
    try:
        device = Device.objects.get(id=device_id)
    except Device.DoesNotExist:
        return

    if not device.username or not device.password:
        return

    from onvif_utils.client import OnvifClient
    from onvif_utils.media import MediaService
    from onvif_utils.mediamtx_api import MediaMTXAPI

    try:
        client = OnvifClient(
            device.host, device.port, device.username, device.password
        )
        svc = MediaService(client)
        profiles = svc.get_profiles()
    except Exception as e:
        logger.warning("refresh_device_streams(%s) ONVIF failed: %s", device_id, e)
        return

    stream_uris = {}
    profiles_tokens = []
    uris = []
    for p in profiles:
        try:
            uri = svc.get_stream_uri(
                p["token"], username=device.username, password=device.password
            )
            if uri:
                stream_uris[p["token"]] = uri
                profiles_tokens.append(p["token"])
                uris.append(uri)
        except Exception as e:
            logger.warning(
                "refresh_device_streams(%s) get_stream_uri(%s) failed: %s",
                device_id, p["token"], e,
            )

    if not stream_uris:
        logger.warning("refresh_device_streams(%s) no stream URIs obtained", device_id)
        return

    if not device.default_profile_token:
        device.default_profile_token = profiles_tokens[0]

    device._skip_stream_refresh = True
    device.stream_uris = stream_uris
    device.save(update_fields=["stream_uris", "default_profile_token"])
    delattr(device, "_skip_stream_refresh")

    try:
        mtx = MediaMTXAPI()
        default_uri = stream_uris.get(device.default_profile_token, "")
        if default_uri:
            mtx.ensure_camera_streams(
                device.id, [device.default_profile_token], [default_uri]
            )
            mtx.ensure_raw_paths(
                device.id, device.default_profile_token, default_uri
            )
    except Exception as e:
        logger.warning(
            "refresh_device_streams(%s) MediaMTX sync failed: %s", device_id, e
        )
        return

    try:
        from devices.models import AnalyticsPreset
        from onvif_utils.snapshot import capture_frame_rtsp

        default_uri = stream_uris.get(device.default_profile_token, "")
        if default_uri:
            frame_bytes = capture_frame_rtsp(default_uri, timeout=10)
            import base64

            snapshot_b64 = base64.b64encode(frame_bytes).decode()
            AnalyticsPreset.objects.update_or_create(
                device=device,
                preset_token="__fixed__",
                defaults={"snapshot": snapshot_b64},
            )
    except Exception as e:
        logger.warning(
            "refresh_device_streams(%s) snapshot capture failed: %s", device_id, e
        )

    regenerate_config_and_restart()
