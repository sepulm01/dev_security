import http.client
import logging
import os
import socket

import redis
from django.apps import apps

logger = logging.getLogger(__name__)

CONFIG_YML_PATH = os.environ.get(
    "CONFIG_YML_PATH", "/opt/computer_vision/config/config.yml"
)
COMPUTER_VISION_CONTAINER = "mediamtx-manager-computer-vision-1"

PIPELINE_INSTANCES = {
    "main": [
        "mediamtx-manager-computer-vision-1",
        "mediamtx-manager-computer-vision-2-1",
        "mediamtx-manager-computer-vision-3-1",
        "mediamtx-manager-computer-vision-4-1",
    ],
    "retinaface": [
        "mediamtx-manager-computer-vision-retinaface-1",
        "mediamtx-manager-computer-vision-retinaface-2-1",
        "mediamtx-manager-computer-vision-retinaface-3-1",
        "mediamtx-manager-computer-vision-retinaface-4-1",
    ],
    "yolov9": [
        "mediamtx-manager-computer-vision-yolov9-1",
        "mediamtx-manager-computer-vision-yolov9-2-1",
        "mediamtx-manager-computer-vision-yolov9-3-1",
        "mediamtx-manager-computer-vision-yolov9-4-1",
    ],
    "trafficcamnet_lpr": [
        "mediamtx-manager-computer-vision-lpr-1",
        "mediamtx-manager-computer-vision-lpr-2-1",
        "mediamtx-manager-computer-vision-lpr-3-1",
        "mediamtx-manager-computer-vision-lpr-4-1",
    ],
}


def get_active_preset_for_device(device):
    specs = device.camera_specs or {}
    has_ptz = bool(specs.get("ptz_caps"))

    if not has_ptz:
        return device.analytics_presets.filter(preset_token="__fixed__").first()

    profile_token = device.default_profile_token
    if not profile_token:
        return None

    try:
        from onvif_utils.client import OnvifClient
        from onvif_utils.ptz import PTZService

        client = OnvifClient(device.host, device.port, device.username, device.password)
        ptz = PTZService(client)
        status = ptz.get_status(profile_token)
        presets = ptz.get_presets(profile_token)

        def get_ptz_values(s):
            if not s:
                return 0, 0, 0
            pos = getattr(s, "Position", None)
            if not pos:
                return 0, 0, 0
            pan_tilt = getattr(pos, "PanTilt", None)
            zoom = getattr(pos, "Zoom", None)
            px = getattr(pan_tilt, "x", 0) if pan_tilt else 0
            py = getattr(pan_tilt, "y", 0) if pan_tilt else 0
            z = getattr(zoom, "x", 0) if zoom else 0
            return px, py, z

        current_pan, current_tilt, current_zoom = get_ptz_values(status)

        best_match = None
        best_dist = float("inf")
        for p in presets:
            ptoken = getattr(p, "token", "") or getattr(p, "_token", "")
            preset_obj = device.analytics_presets.filter(preset_token=ptoken).first()
            if not preset_obj:
                continue
            stored_pos = getattr(preset_obj, "ptz_position", None) or {}
            pp = stored_pos.get("pan", current_pan)
            pt_val = stored_pos.get("tilt", current_tilt)
            pz = stored_pos.get("zoom", current_zoom)
            dist = (
                abs(pp - current_pan)
                + abs(pt_val - current_tilt)
                + abs(pz - current_zoom)
            )
            if dist < best_dist:
                best_dist = dist
                best_match = preset_obj

        if best_match is None:
            fallback = device.analytics_presets.first()
            if fallback:
                return fallback

        return best_match
    except Exception as e:
        logger.warning(
            "Error determining active preset for device %s: %s", device.id, e
        )
        return None


def _get_redis():
    return redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"))


def restart_computer_vision(container_name=None):
    if container_name is None:
        container_name = COMPUTER_VISION_CONTAINER
    _docker_control(container_name, "restart")


def _docker_control(container_name, action):
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(15)
        sock.connect("/var/run/docker.sock")
        conn = http.client.HTTPConnection("localhost")
        conn.sock = sock
        conn.request("POST", f"/containers/{container_name}/{action}")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        if resp.status in (204, 304):
            logger.info("Container %s %s successfully", container_name, action)
        else:
            logger.error("Docker %s returned status %d for %s", action, resp.status, container_name)
    except Exception as e:
        logger.error("Failed to %s container %s: %s", action, container_name, e)


def regenerate_config_and_restart(pipeline_id=None):
    from devices.config_generator import (
        MAX_INSTANCES,
        PIPELINE_CONFIGS,
        generate_all_configs,
        get_pipeline_filename,
    )
    from onvif_utils.mediamtx_api import MediaMTXAPI

    Device = apps.get_model("devices", "Device")

    online_devices = list(
        Device.objects.filter(
            is_online=True, stream_uris__isnull=False, source_type="rtsp"
        ).exclude(stream_uris={})
    )
    file_devices = list(
        Device.objects.filter(
            stream_uris__isnull=False, source_type="file"
        ).exclude(stream_uris={})
    )

    all_devices = online_devices + file_devices

    if not all_devices:
        logger.warning("No devices with stream URIs, stopping all pipeline containers")
        for pid in PIPELINE_INSTANCES:
            for container_name in PIPELINE_INSTANCES[pid]:
                _docker_control(container_name, "stop")
        return

    mtx = MediaMTXAPI()
    for device in online_devices:
        uri = device.stream_uris.get(device.default_profile_token, "")
        if uri:
            try:
                mtx.ensure_camera_streams(
                    device.id, [device.default_profile_token], [uri]
                )
            except Exception:
                pass

    for device in file_devices:
        try:
            mtx.ensure_file_stream(device)
        except Exception:
            pass

    config_dir = os.path.dirname(CONFIG_YML_PATH)
    generate_all_configs(config_dir)

    r = _get_redis()
    for pipeline_id_key in PIPELINE_INSTANCES:
        pipeline_devices = [
            d for d in all_devices if d.deepstream_pipeline == pipeline_id_key
        ]
        pipeline_cfg = PIPELINE_CONFIGS[pipeline_id_key]
        max_per_instance = pipeline_cfg["max_devices_per_instance"]
        instances_needed = min(
            max((len(pipeline_devices) + max_per_instance - 1) // max_per_instance, 1),
            MAX_INSTANCES,
        )

        for n in range(MAX_INSTANCES):
            instance = n + 1
            container_name = PIPELINE_INSTANCES[pipeline_id_key][n]
            sources_key = f"deepstream:sources:{pipeline_id_key}:{instance}"

            r.delete(sources_key)

            if instance <= instances_needed and pipeline_devices:
                my_devices = pipeline_devices[n :: instances_needed]
                for idx, device in enumerate(my_devices):
                    uri = device.stream_uris.get(device.default_profile_token, "")
                    r.hset(sources_key, str(idx), str(device.id))
                    r.hset(sources_key, f"{idx}:camera_id", str(device.id))
                    r.hset(sources_key, f"{idx}:url", uri)

                if pipeline_id and pipeline_id != pipeline_id_key:
                    continue
                _docker_control(container_name, "restart")
            else:
                _docker_control(container_name, "stop")

    logger.info("Configs regenerated for %d devices across all pipelines", len(all_devices))

    logger.info("Configs regenerated for %d devices across all pipelines", len(all_devices))
