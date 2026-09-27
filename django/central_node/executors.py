import logging

logger = logging.getLogger(__name__)


def execute_get_state(payload: dict) -> dict:
    from central_node.collectors import collect_device_states, collect_metrics, get_version

    return {
        "version": get_version(),
        "metrics": collect_metrics(),
        "devices": collect_device_states(),
    }


def execute_sync_config(payload: dict) -> dict:
    from devices.utils import regenerate_config_and_restart

    regenerate_config_and_restart()
    return {"detail": "configs regeneradas y contenedores reiniciados"}


def execute_sync_mediamtx(payload: dict) -> dict:
    from devices.models import Device
    from onvif_utils.mediamtx_api import MediaMTXAPI

    mtx = MediaMTXAPI()
    count = 0
    for device in Device.objects.filter(stream_uris__isnull=False).exclude(stream_uris={}):
        uri = device.stream_uris.get(device.default_profile_token, "")
        if not uri:
            continue
        try:
            if device.source_type == "file":
                mtx.ensure_file_stream(device)
            else:
                mtx.ensure_camera_streams(device.id, [device.default_profile_token], [uri])
            count += 1
        except Exception as e:
            logger.warning("sync_mediamtx device %s: %s", device.id, e)
    return {"detail": f"paths sincronizados para {count} dispositivos"}


def execute_restart_pipeline(payload: dict) -> dict:
    from devices.utils import PIPELINE_INSTANCES, _docker_control

    pipeline = payload.get("pipeline") or "main"
    if pipeline not in PIPELINE_INSTANCES:
        return {"success": False, "detail": f"pipeline desconocido: {pipeline}"}
    for container_name in PIPELINE_INSTANCES[pipeline]:
        _docker_control(container_name, "restart")
    return {"detail": f"restart enviado a {pipeline}"}


EXECUTORS = {
    "get_state": execute_get_state,
    "sync_config": execute_sync_config,
    "sync_mediamtx": execute_sync_mediamtx,
    "restart_pipeline": execute_restart_pipeline,
}


def execute_command(kind: str, payload: dict):
    executor = EXECUTORS.get(kind)
    if not executor:
        raise ValueError(f"comando desconocido: {kind}")
    return executor(payload or {})
