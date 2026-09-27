import logging

from django.conf import settings

logger = logging.getLogger(__name__)


def collect_device_states():
    """Estado de cámaras del nodo: is_online + claves Redis de FPS/offline."""
    try:
        from devices.models import Device
    except Exception:
        return []

    try:
        import redis as redis_lib

        redis_client = redis_lib.Redis.from_url(settings.REDIS_URL or "redis://localhost:6379/0")
    except Exception:
        redis_client = None

    devices = []
    try:
        queryset = list(Device.objects.all())
    except Exception:
        return []
    for device in queryset:
        state = {
            "id": device.id,
            "name": device.name,
            "is_online": device.is_online,
            "failure_count": device.failure_count,
            "deepstream_pipeline": device.deepstream_pipeline,
            "fps_low": False,
            "fps_zero": False,
            "offline_since": None,
        }
        if redis_client:
            try:
                state["fps_low"] = bool(redis_client.get(f"device:{device.id}:fps_low"))
                state["fps_zero"] = bool(redis_client.get(f"device:{device.id}:fps_zero"))
                state["offline_since"] = redis_client.get(f"device:{device.id}:offline_since")
            except Exception:
                pass
        devices.append(state)
    return devices


def collect_metrics():
    """Reutiliza los colectores de monitoring (con tolerancia a fallos)."""
    metrics = {}
    try:
        from monitoring.collectors.system import collect_system_metrics

        metrics["system"] = collect_system_metrics() or {}
    except Exception as e:
        logger.warning("system metrics: %s", e)
    try:
        from monitoring.collectors.mediamtx import collect_mediamtx_metrics

        metrics["mediamtx"] = collect_mediamtx_metrics() or {}
    except Exception as e:
        logger.warning("mediamtx metrics: %s", e)
    try:
        from monitoring.collectors.deepstream import collect_deepstream_metrics

        metrics["deepstream"] = collect_deepstream_metrics() or {}
    except Exception as e:
        logger.warning("deepstream metrics: %s", e)
    return metrics


def get_version():
    import os
    import subprocess

    env_version = os.environ.get("NODE_VERSION", "")
    if env_version:
        return env_version
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
    except Exception:
        return ""
