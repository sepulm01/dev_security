import base64
import json
import logging
import os
from datetime import datetime, timezone

import requests

from django.shortcuts import render, get_object_or_404
from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.decorators import login_required
from devices.models import Device, IVSRule, AnalyticsPreset, Patrol
from live.views import DEFAULT_CAMERA_SPECS
from onvif_utils.client import OnvifClient
from onvif_utils.discovery import DeviceDiscovery
from onvif_utils.drivers import get_driver
from onvif_utils.drivers.base import DriverError
from onvif_utils.media import MediaService
from onvif_utils.mediamtx_api import MediaMTXAPI
from onvif_utils.ptz import PTZService

logger = logging.getLogger(__name__)


@login_required
def dashboard(request):
    devices = Device.objects.all()
    return render(request, "devices/dashboard.html", {"devices": devices})


@login_required
def device_detail(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    from live.views import build_stream_context

    profile_token = device.default_profile_token or ""
    ctx = build_stream_context(device, profile_token, request.get_host())
    ctx["device"] = device
    ctx["profile_token"] = profile_token
    ctx["camera_specs_json"] = json.dumps(device.camera_specs or {})
    from operadores.models import Site

    ctx["sites"] = Site.objects.filter(is_active=True)
    ctx["stream_uris_json"] = json.dumps(device.stream_uris or {}, indent=2)
    return render(request, "devices/device_detail.html", ctx)


@login_required
@csrf_exempt
def discover(request):
    if request.method == "POST":
        timeout = int(request.POST.get("timeout", 10))
        devices = DeviceDiscovery(timeout=timeout).discover()
        return JsonResponse(devices, safe=False)
    return render(request, "devices/discover.html")


@login_required
@csrf_exempt
def probe(request):
    if request.method == "POST":
        data = json.loads(request.body)
        host = data.get("host", "").strip()
        port = int(data.get("port", 80))
        if not host:
            return JsonResponse({"error": "host required"}, status=400)
        result = DeviceDiscovery.probe_ip(host, port)
        return JsonResponse(result)
    return JsonResponse({"error": "POST required"}, status=405)


@login_required
@csrf_exempt
def add_device(request):
    if request.method == "POST":
        data = json.loads(request.body)
        host = data.get("host", "")
        port = int(data.get("port", 80))
        username = data.get("username", "")
        password = data.get("password", "")

        if not username or not password:
            return JsonResponse(
                {"error": "username and password are required"},
                status=400,
            )

        try:
            client = OnvifClient(host, port, username, password)
            info = client.get_device_info()
            manufacturer = info["manufacturer"]
            model = info["model"]
            firmware = info["firmware"]
            serial_number = info["serial_number"]
            hardware_id = info["hardware_id"]
        except Exception as e:
            return JsonResponse(
                {"error": f"ONVIF connection failed: {e}"},
                status=400,
            )

        device = Device(
            name=data.get("name", host or ""),
            host=host,
            port=port,
            username=username,
            password=password,
            manufacturer=manufacturer,
            model=model,
            firmware=firmware,
            serial_number=serial_number,
            hardware_id=hardware_id,
            xaddrs=json.dumps(data.get("xaddrs", [])),
            scopes=json.dumps(data.get("scopes", [])),
            is_online=True,
            camera_specs=dict(DEFAULT_CAMERA_SPECS),
        )
        device._skip_stream_refresh = True
        device.save()

        from devices.tasks import refresh_device_streams

        refresh_device_streams.delay(device.id)

        return JsonResponse({"ok": True, "id": device.id})
    return JsonResponse({"error": "POST required"}, status=405)


@login_required
@csrf_exempt
def delete_device(request, device_id):
    if request.method == "POST":
        device = get_object_or_404(Device, id=device_id)
        mtx = MediaMTXAPI()
        mtx.delete_camera_paths(device.id)
        pipeline = device.deepstream_pipeline
        device.delete()
        from devices.utils import regenerate_config_and_restart

        regenerate_config_and_restart()
        return JsonResponse({"ok": True})
    return JsonResponse({"error": "POST required"}, status=405)


@login_required
def device_profiles(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    try:
        client = OnvifClient(device.host, device.port, device.username, device.password)
        svc = MediaService(client)
        profiles = svc.get_profiles()
        for p in profiles:
            uri = svc.get_stream_uri(
                p["token"], username=device.username, password=device.password
            )
            p["stream_uri"] = uri
        return JsonResponse(profiles, safe=False)
    except Exception as e:
        if device.stream_uris:
            profiles = []
            for token, uri in device.stream_uris.items():
                profiles.append({
                    "token": token,
                    "name": token,
                    "stream_uri": uri,
                    "ptz": bool(
                        isinstance(device.camera_specs, dict)
                        and device.camera_specs.get("ptz_caps")
                    ),
                })
            return JsonResponse(profiles, safe=False)
        return JsonResponse({"error": str(e)}, status=500)


@login_required
@csrf_exempt
def scan_device(request, device_id):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    device = get_object_or_404(Device, id=device_id)
    try:
        client = OnvifClient(device.host, device.port, device.username, device.password)
        specs = dict(DEFAULT_CAMERA_SPECS)

        try:
            info = client.get_device_info()
            device.manufacturer = info["manufacturer"]
            device.model = info["model"]
            device.firmware = info["firmware"]
            device.serial_number = info["serial_number"]
            device.hardware_id = info["hardware_id"]
        except Exception:
            pass

        try:
            specs["onvif_profiles"] = client.get_services()
        except Exception:
            pass
        try:
            specs["video_source"] = client.get_video_sources()
        except Exception:
            pass
        try:
            specs["media_caps"] = client.get_media_capabilities()
        except Exception:
            pass
        try:
            specs["ptz_caps"] = client.get_ptz_capabilities()
        except Exception:
            pass
        try:
            net = client.get_network_interfaces()
            if net:
                specs["network"] = net[0]
        except Exception:
            pass
        try:
            specs["hostname"] = client.get_hostname()
        except Exception:
            pass
        try:
            specs["dns"] = client.get_dns()
        except Exception:
            pass
        try:
            ntp = client.get_ntp()
            if ntp:
                specs["ntp"] = ntp
        except Exception:
            pass
        try:
            specs["system_time"] = client.get_system_date_time()
        except Exception:
            pass

        specs["last_scan"] = datetime.now(timezone.utc).isoformat()
        device.camera_specs = specs
        device.save()
        return JsonResponse({"ok": True, "camera_specs": specs})
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@login_required
@csrf_exempt
def sync_time(request, device_id=None):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    now = datetime.now(timezone.utc)
    devices_qs = (
        Device.objects.filter(id=device_id) if device_id else Device.objects.all()
    )

    results = []
    for device in devices_qs:
        try:
            client = OnvifClient(
                device.host, device.port, device.username, device.password
            )
            specs = device.camera_specs or {}
            tz = None
            if isinstance(specs, dict) and specs.get("system_time"):
                tz = specs["system_time"].get("tz", None)
            client.set_system_date_time(utc_dt=now, tz=tz)
            if isinstance(specs, dict):
                specs["last_time_sync"] = now.isoformat()
                device.camera_specs = specs
                device.save()
            results.append({"id": device.id, "name": device.name, "ok": True})
        except Exception as e:
            results.append(
                {"id": device.id, "name": device.name, "ok": False, "error": str(e)}
            )

    return JsonResponse({"results": results})


@login_required
@csrf_exempt
def set_default_profile(request, device_id):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    device = get_object_or_404(Device, id=device_id)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "JSON required"}, status=400)
    device.default_profile_token = data.get("profile_token", "")
    device.save()
    return JsonResponse(
        {"ok": True, "default_profile_token": device.default_profile_token}
    )


@login_required
@csrf_exempt
def device_motion_config(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    driver = get_driver(device)

    if request.method == "GET":
        try:
            config = driver.get_motion_config()
            return JsonResponse(config)
        except DriverError as e:
            return JsonResponse({"error": str(e)}, status=500)

    elif request.method == "POST":
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "JSON required"}, status=400)
        try:
            driver.set_motion_config(data)
            return JsonResponse({"ok": True})
        except DriverError as e:
            return JsonResponse({"error": str(e)}, status=500)

    return JsonResponse({"error": "GET or POST required"}, status=405)


@login_required
@csrf_exempt
def device_ivs_config(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    driver = get_driver(device)

    if request.method == "GET":
        try:
            rules = driver.get_ivs_rules()
            return JsonResponse({"rules": rules})
        except DriverError as e:
            return JsonResponse({"error": str(e)}, status=500)

    elif request.method == "POST":
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "JSON required"}, status=400)

        rules = data.get("rules", [])
        camera_write_ok = False
        camera_error = None
        try:
            driver.set_ivs_rules(rules)
            camera_write_ok = True
        except DriverError as e:
            camera_error = str(e)
            if "does not support IVS config via CGI" not in camera_error:
                return JsonResponse({"error": camera_error}, status=500)

        IVSRule.objects.filter(device=device).delete()
        for idx, rule in enumerate(rules):
            IVSRule.objects.update_or_create(
                device=device,
                index=idx,
                defaults={
                    "name": rule.get("name", f"Rule {idx}"),
                    "rule_type": rule.get("type", "CrossLine"),
                    "enable": rule.get("enable", True),
                    "direction": rule.get("direction", "Both"),
                    "detect_line": rule.get("detect_line", ""),
                    "detect_region": rule.get("detect_region", ""),
                    "event_handler": rule.get("event_handler", {}),
                    "camera_rule_id": idx,
                },
            )
        if camera_write_ok:
            return JsonResponse({"ok": True})
        else:
            return JsonResponse(
                {
                    "ok": True,
                    "warning": "Rules saved locally only. Camera does not support IVS config via CGI.",
                }
            )

    return JsonResponse({"error": "GET or POST required"}, status=405)


@login_required
@csrf_exempt
def device_events(request, device_id):
    device = get_object_or_404(Device, id=device_id)

    if request.method != "GET":
        return JsonResponse({"error": "GET required"}, status=405)

    limit = int(request.GET.get("limit", 100))
    events = device.events.all()[:limit]
    return JsonResponse(
        [
            {
                "id": e.id,
                "code": e.code,
                "action": e.action,
                "index": e.index,
                "data": e.data,
                "timestamp": e.timestamp.isoformat(),
            }
            for e in events
        ],
        safe=False,
    )


@login_required
@csrf_exempt
def device_event_listener_toggle(request, device_id):
    device = get_object_or_404(Device, id=device_id)

    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "JSON required"}, status=400)

    enabled = bool(data.get("enable", False))
    device.event_listener_enabled = enabled
    device.save()
    return JsonResponse({"ok": True, "event_listener_enabled": enabled})


ANALYTICS_CONFIG_PATH = os.environ.get(
    "DEEPSTREAM_ANALYTICS_CONFIG", "/opt/deepstream-app/config/config_nvdsanalytics.txt"
)
DEEPSTREAM_REDIS_CHANNEL = "deepstream:commands"


def _parse_analytics_config(content):
    sections = {}
    current_section = None
    current_key = None

    for line in content.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        if line.startswith("[") and line.endswith("]"):
            current_section = line[1:-1]
            sections[current_section] = {}
            current_key = None
        elif "=" in line and current_section:
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()

            if ";" in value:
                value = [v.strip() for v in value.split(";")]

            if current_key and isinstance(
                sections[current_section].get(current_key), list
            ):
                if key == current_key:
                    existing = sections[current_section][current_key]
                    if isinstance(existing, list):
                        existing.append(value if isinstance(value, list) else [value])
                    else:
                        sections[current_section][current_key] = [
                            existing,
                            value if isinstance(value, list) else [value],
                        ]
                else:
                    sections[current_section][key] = value
            else:
                sections[current_section][key] = value
            current_key = key
        elif current_section:
            sections[current_section][current_key] = line

    return sections


def _serialize_analytics_config(sections):
    lines = [
        "[property]",
        "enable=1",
        "config-width=1280",
        "config-height=720",
        "osd-mode=0",
        "",
    ]

    for section, props in sections.items():
        if section == "property":
            continue
        lines.append(f"[{section}]")
        for key, val in props.items():
            if isinstance(val, list):
                for v in val:
                    if isinstance(v, list):
                        v = ";".join(str(x) for x in v)
                    else:
                        v = str(v)
                    lines.append(f"{key}={v}")
            else:
                lines.append(f"{key}={val}")
        lines.append("")

    return "\n".join(lines)


@login_required
@csrf_exempt
def device_analytics_config(request, device_id):
    get_object_or_404(Device, id=device_id)

    if request.method == "GET":
        try:
            with open(ANALYTICS_CONFIG_PATH, "r") as f:
                content = f.read()
        except FileNotFoundError:
            return JsonResponse({"error": "Config file not found"}, status=404)
        except IOError as e:
            return JsonResponse({"error": str(e)}, status=500)

        sections = _parse_analytics_config(content)
        return JsonResponse({"sections": sections})

    elif request.method == "POST":
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "JSON required"}, status=400)

        sections = data.get("sections", {})
        config_content = _serialize_analytics_config(sections)

        try:
            with open(ANALYTICS_CONFIG_PATH, "w") as f:
                f.write(config_content)
        except IOError as e:
            return JsonResponse({"error": f"Failed to write config: {e}"}, status=500)

        try:
            import redis
            from urllib.parse import urlparse

            redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
            parsed = urlparse(redis_url)
            r = redis.Redis(
                host=parsed.hostname or "localhost",
                port=parsed.port or 6379,
                db=parsed.path.lstrip("/") if parsed.path else 0,
                password=parsed.password or None,
            )
            r.publish(
                DEEPSTREAM_REDIS_CHANNEL, json.dumps({"action": "reload_analytics"})
            )
        except Exception as e:
            logger.warning("Failed to publish reload_analytics to Redis: %s", e)

        return JsonResponse({"ok": True})

    return JsonResponse({"error": "GET or POST required"}, status=405)


@login_required
def analytics_editor(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    specs = device.camera_specs or {}
    has_ptz = bool(specs.get("ptz_caps"))
    profile = device.default_profile_token or ""
    return render(
        request,
        "devices/analytics_editor.html",
        {
            "device": device,
            "has_ptz": has_ptz,
            "default_profile": profile,
        },
    )


@login_required
@csrf_exempt
def analytics_snapshot(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    profile_token = request.GET.get("profile_token") or device.default_profile_token
    if not profile_token:
        return JsonResponse({"error": "profile_token required"}, status=400)

    try:
        from onvif_utils.client import OnvifClient
        from onvif_utils.media import MediaService

        client = OnvifClient(device.host, device.port, device.username, device.password)
        media = MediaService(client)
        snapshot_url = media.get_snapshot_url(
            profile_token,
            username=device.username,
            password=device.password,
        )
        if not snapshot_url:
            return JsonResponse({"error": "No snapshot URI available"}, status=404)

        import requests

        resp = requests.get(snapshot_url, timeout=5)
        resp.raise_for_status()
        return HttpResponse(resp.content, content_type="image/jpeg")
    except Exception as e:
        logger.warning("Failed to get snapshot for device %s: %s", device_id, e)
        return JsonResponse({"error": str(e)}, status=500)


@login_required
@csrf_exempt
def analytics_capture_snapshot(request, device_id):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    device = get_object_or_404(Device, id=device_id)

    data = json.loads(request.body or "{}")
    preset_token = data.get("preset_token") or request.GET.get("preset_token", "")
    profile_token = data.get("profile_token") or device.default_profile_token

    if not preset_token:
        return JsonResponse({"error": "preset_token required"}, status=400)
    if not profile_token:
        return JsonResponse({"error": "profile_token required"}, status=400)

    rtsp_uri = device.stream_uris.get(profile_token, "")
    if not rtsp_uri:
        return JsonResponse({"error": f"No stream URI for profile {profile_token}"}, status=400)

    try:
        from onvif_utils.snapshot import capture_frame_rtsp

        frame_bytes = capture_frame_rtsp(rtsp_uri, timeout=10)
        snapshot_b64 = base64.b64encode(frame_bytes).decode()

        preset = AnalyticsPreset.objects.filter(
            device=device, preset_token=preset_token
        ).first()
        if preset:
            preset.snapshot = snapshot_b64
            preset.save()
        else:
            AnalyticsPreset.objects.create(
                device=device,
                preset_token=preset_token,
                snapshot=snapshot_b64,
            )

        return JsonResponse({"snapshot": snapshot_b64})
    except Exception as e:
        logger.warning("Snapshot capture failed for device %s: %s", device_id, e)
        return JsonResponse({"error": str(e)}, status=500)


@login_required
@csrf_exempt
def analytics_presets(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    profile_token = request.GET.get("profile_token") or device.default_profile_token
    if not profile_token:
        return JsonResponse({"error": "profile_token required"}, status=400)

    specs = device.camera_specs or {}
    has_ptz = bool(specs.get("ptz_caps"))

    if has_ptz:
        try:
            from onvif_utils.client import OnvifClient
            from onvif_utils.ptz import PTZService

            client = OnvifClient(
                device.host, device.port, device.username, device.password
            )
            ptz = PTZService(client)
            presets = ptz.get_presets(profile_token)

            all_db = AnalyticsPreset.objects.filter(device=device)
            stored = {ap.preset_token: ap for ap in all_db}

            result = [
                {
                    "token": getattr(p, "token", "") or getattr(p, "_token", ""),
                    "name": getattr(p, "Name", "") or f"Preset {i + 1}",
                    "snapshot": (
                        stored[getattr(p, "token", "") or getattr(p, "_token", "")].snapshot
                        if (getattr(p, "token", "") or getattr(p, "_token", "")) in stored
                        else ""
                    ),
                }
                for i, p in enumerate(presets)
            ]

            camera_tokens = {r["token"] for r in result}
            for ap in all_db:
                if ap.preset_token not in camera_tokens:
                    result.append({
                        "token": ap.preset_token,
                        "name": ap.preset_name or ap.preset_token,
                        "snapshot": ap.snapshot or "",
                    })

            if not result:
                result.append({
                    "token": "__fixed__",
                    "name": "Cámara fija",
                    "snapshot": "",
                })

            return JsonResponse({"presets": result, "has_ptz": True})
        except Exception as e:
            return JsonResponse(
                {"presets": [], "has_ptz": True, "error": str(e)}, status=500
            )
    else:
        ap = AnalyticsPreset.objects.filter(
            device=device, preset_token="__fixed__"
        ).first()
        return JsonResponse(
            {
                "presets": [
                    {
                        "token": "__fixed__",
                        "name": "Cámara fija",
                        "snapshot": ap.snapshot if ap else "",
                    }
                ],
                "has_ptz": False,
            }
        )


@login_required
@csrf_exempt
def analytics_shapes(request, device_id, preset_token):
    device = get_object_or_404(Device, id=device_id)

    preset = AnalyticsPreset.objects.filter(
        device=device, preset_token=preset_token
    ).first()

    if request.method == "GET":
        if preset:
            return JsonResponse(
                {"shapes": preset.shapes, "preset_name": preset.preset_name}
            )
        return JsonResponse({"shapes": [], "preset_name": ""})

    elif request.method == "POST":
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "JSON required"}, status=400)

        shapes = data.get("shapes", [])
        preset_name = data.get("preset_name", "")

        if preset:
            preset.shapes = shapes
            preset.preset_name = preset_name
            preset.save()
        else:
            preset = AnalyticsPreset.objects.create(
                device=device,
                preset_token=preset_token,
                preset_name=preset_name,
                shapes=shapes,
            )
        return JsonResponse({"ok": True})

    return JsonResponse({"error": "GET or POST required"}, status=405)


def _get_active_preset_for_device(device):
    from devices.utils import get_active_preset_for_device

    return get_active_preset_for_device(device)


@login_required
@csrf_exempt
def analytics_apply(request, device_id):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    device = get_object_or_404(Device, id=device_id)

    preset_token = request.GET.get("preset_token") or (
        json.loads(request.body).get("preset_token") if request.body else None
    )

    if preset_token:
        active_preset = device.analytics_presets.filter(
            preset_token=preset_token
        ).first()
        if not active_preset:
            return JsonResponse(
                {"error": f"Preset '{preset_token}' not found"}, status=404
            )
    else:
        active_preset = _get_active_preset_for_device(device)
        if not active_preset:
            return JsonResponse(
                {"error": "No active preset found. Define presets first."}, status=400
            )

    from devices.utils import regenerate_config_and_restart

    regenerate_config_and_restart()

    return JsonResponse(
        {
            "ok": True,
            "active_preset": active_preset.preset_name or active_preset.preset_token,
            "shapes_count": len(active_preset.shapes or []),
        }
    )


@login_required
@csrf_exempt
def analytics_disable(request, device_id):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    get_object_or_404(Device, id=device_id)

    from devices.utils import regenerate_config_and_restart

    regenerate_config_and_restart()

    return JsonResponse({"ok": True})


@login_required
@csrf_exempt
def analytics_goto_and_apply(request, device_id):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    device = get_object_or_404(Device, id=device_id)

    data = json.loads(request.body or "{}")
    preset_token = data.get("preset_token") or request.GET.get("preset_token", "")

    if not preset_token:
        return JsonResponse({"error": "preset_token required"}, status=400)

    specs = device.camera_specs or {}
    if specs.get("ptz_caps"):
        profile_token = data.get("profile_token") or device.default_profile_token
        if profile_token:
            try:
                client = OnvifClient(
                    device.host, device.port, device.username, device.password
                )
                ptz = PTZService(client)
                ptz.goto_preset(profile_token, preset_token)
                ptz.wait_until_idle(profile_token, timeout=30, interval=0.5)

                try:
                    from onvif_utils.snapshot import capture_frame_rtsp

                    uri = device.stream_uris.get(device.default_profile_token, "")
                    if uri:
                        import base64

                        frame_bytes = capture_frame_rtsp(uri, timeout=10)
                        snapshot_b64 = base64.b64encode(frame_bytes).decode()
                        AnalyticsPreset.objects.update_or_create(
                            device=device,
                            preset_token=preset_token,
                            defaults={"snapshot": snapshot_b64},
                        )
                except Exception as se:
                    logger.warning(
                        "PTZ snapshot failed for device %s preset %s: %s",
                        device_id, preset_token, se,
                    )
            except Exception as e:
                logger.warning(
                    "PTZ goto/wait failed for device %s: %s", device_id, e
                )

    active_preset = device.analytics_presets.filter(
        preset_token=preset_token
    ).first()

    from devices.utils import regenerate_config_and_restart

    regenerate_config_and_restart()

    return JsonResponse(
        {
            "ok": True,
            "preset": preset_token,
            "shapes_count": len(active_preset.shapes) if active_preset else 0,
        }
    )


def _get_redis():
    import redis
    from urllib.parse import urlparse

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    parsed = urlparse(redis_url)
    return redis.Redis(
        host=parsed.hostname or "localhost",
        port=parsed.port or 6379,
        db=parsed.path.lstrip("/") if parsed.path else 0,
        password=parsed.password or None,
    )


DEEPSTREAM_REDIS_CHANNEL = "deepstream:commands"


def _publish_deepstream_command(payload):
    try:
        r = _get_redis()
        r.publish(DEEPSTREAM_REDIS_CHANNEL, json.dumps(payload))
    except Exception as e:
        logger.warning("Failed to publish to deepstream: %s", e)


@login_required
@csrf_exempt
def deepstream_preview_start(request, device_id):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    device = get_object_or_404(Device, id=device_id)
    profile_token = device.default_profile_token
    stream_uri = ""
    if profile_token and device.stream_uris:
        stream_uri = device.stream_uris.get(profile_token, "")
    _publish_deepstream_command(
        {
            "action": "start_preview",
            "device_id": device_id,
            "camera_id": str(device_id),
            "rtsp_uri": stream_uri,
            "camera_name": device.name,
        }
    )
    return JsonResponse({"ok": True, "stream_uri": stream_uri})


@login_required
@csrf_exempt
def deepstream_preview_stop(request, device_id):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    _publish_deepstream_command({"action": "stop_preview", "device_id": device_id})
    return JsonResponse({"ok": True})


@login_required
@csrf_exempt
def deepstream_preview_keepalive(request, device_id):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    key = f"deepstream_preview:{device_id}"
    try:
        r = _get_redis()
        is_new = not r.exists(key)
        r.setex(key, 30, "1")
        if is_new:
            device = get_object_or_404(Device, id=device_id)
            profile_token = device.default_profile_token
            stream_uri = ""
            if profile_token and device.stream_uris:
                stream_uri = device.stream_uris.get(profile_token, "")
            _publish_deepstream_command(
                {
                    "action": "start_preview",
                    "device_id": device_id,
                    "camera_id": str(device_id),
                    "rtsp_uri": stream_uri,
                    "camera_name": device.name,
                }
            )
    except Exception as e:
        logger.warning("Keepalive failed: %s", e)
    return JsonResponse({"ok": True})


@login_required
@csrf_exempt
def assign_pipeline(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    if request.method == "POST":
        data = json.loads(request.body) if request.body else {}
        pipeline = data.get("pipeline", "main")
        if pipeline not in dict(Device.PIPELINE_CHOICES):
            return JsonResponse({"error": "Invalid pipeline"}, status=400)
        device.deepstream_pipeline = pipeline
        device.save(update_fields=["deepstream_pipeline"])
        from devices.utils import regenerate_config_and_restart

        regenerate_config_and_restart(pipeline_id=pipeline)
        return JsonResponse({"ok": True})
    return JsonResponse({"error": "POST required"}, status=405)


@login_required
@csrf_exempt
def edit_stream_uris(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    if request.method == "POST":
        data = json.loads(request.body) if request.body else {}
        stream_uris = data.get("stream_uris")
        if not isinstance(stream_uris, dict):
            return JsonResponse({"error": "stream_uris must be a JSON object"}, status=400)
        device.stream_uris = stream_uris
        device.save(update_fields=["stream_uris"])
        return JsonResponse({"ok": True})
    return JsonResponse({"error": "POST required"}, status=405)


@login_required
@csrf_exempt
def configure_snmp(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    if request.method == "POST":
        data = json.loads(request.body) if request.body else {}
        device.snmp_enabled = data.get("snmp_enabled", False)
        device.snmp_community = data.get("snmp_community", "public")
        device.snmp_port = data.get("snmp_port", 161)
        device.save(update_fields=["snmp_enabled", "snmp_community", "snmp_port"])
        return JsonResponse({"ok": True})
    return JsonResponse({"error": "POST required"}, status=405)


@login_required
@csrf_exempt
def set_gps(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    if request.method == "POST":
        data = json.loads(request.body) if request.body else {}
        device.latitude = data.get("latitude") if data.get("latitude") is not None else None
        device.longitude = data.get("longitude") if data.get("longitude") is not None else None
        device.save(update_fields=["latitude", "longitude"])
        return JsonResponse({"ok": True})
    return JsonResponse({"error": "POST required"}, status=405)


@login_required
def patrol_list(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    patrols = device.patrols.all()

    from onvif_utils.ptz import PTZService
    from onvif_utils.client import OnvifClient

    presets = []
    ptz_supported = bool(device.camera_specs.get("ptz_caps"))
    if ptz_supported:
        try:
            client = OnvifClient(device.host, device.port, device.username, device.password)
            ptz = PTZService(client)
            presets = ptz.get_presets(device.default_profile_token)
        except Exception:
            ptz_supported = False

    return render(
        request,
        "devices/patrol_list.html",
        {
            "device": device,
            "patrols": patrols,
            "presets": presets,
            "ptz_supported": ptz_supported,
        },
    )


@login_required
def patrol_form(request, device_id, patrol_id=None):
    device = get_object_or_404(Device, id=device_id)
    patrol = None
    if patrol_id:
        patrol = get_object_or_404(Patrol, id=patrol_id, device=device)

    from onvif_utils.ptz import PTZService
    from onvif_utils.client import OnvifClient

    presets = []
    try:
        client = OnvifClient(device.host, device.port, device.username, device.password)
        ptz = PTZService(client)
        presets = ptz.get_presets(device.default_profile_token)
    except Exception:
        pass

    return render(
        request,
        "devices/patrol_form.html",
        {
            "device": device,
            "patrol": patrol,
            "presets": presets,
            "schedule_json": json.dumps(patrol.schedule) if patrol else "{}",
            "preset_order_json": json.dumps(patrol.preset_order) if patrol else "[]",
        },
    )


@login_required
@csrf_exempt
def patrol_save(request, device_id, patrol_id=None):
    device = get_object_or_404(Device, id=device_id)
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    data = json.loads(request.body) if request.body else {}

    name = data.get("name", "").strip()
    if not name:
        return JsonResponse({"error": "name required"}, status=400)

    if patrol_id:
        patrol = get_object_or_404(Patrol, id=patrol_id, device=device)
    else:
        patrol = Patrol(device=device)

    patrol.name = name
    patrol.is_active = data.get("is_active", True)
    patrol.valid_from = data.get("valid_from") or None
    patrol.valid_until = data.get("valid_until") or None
    patrol.schedule = data.get("schedule", {})
    patrol.dwell_seconds = data.get("dwell_seconds", 10)
    patrol.speed = float(data.get("speed", 1.0))
    patrol.preset_order = data.get("preset_order", [])
    patrol.save()

    return JsonResponse({"ok": True, "patrol_id": patrol.id})


@login_required
@csrf_exempt
def patrol_delete(request, device_id, patrol_id):
    device = get_object_or_404(Device, id=device_id)
    patrol = get_object_or_404(Patrol, id=patrol_id, device=device)
    if request.method == "POST":
        patrol.delete()
        return JsonResponse({"ok": True})
    return JsonResponse({"error": "POST required"}, status=405)


@login_required
@csrf_exempt
def patrol_toggle(request, device_id, patrol_id):
    device = get_object_or_404(Device, id=device_id)
    patrol = get_object_or_404(Patrol, id=patrol_id, device=device)
    if request.method == "POST":
        data = json.loads(request.body) if request.body else {}
        patrol.is_active = data.get("is_active", not patrol.is_active)
        patrol.save(update_fields=["is_active"])
        return JsonResponse({"ok": True, "is_active": patrol.is_active})
    return JsonResponse({"error": "POST required"}, status=405)
