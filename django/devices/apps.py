import os
import threading
import time

from django.apps import AppConfig


class DevicesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "devices"

    def ready(self):
        import devices.signals  # noqa: F401

        def check_and_sync():
            from devices.models import Device
            from devices.utils import regenerate_config_and_restart
            from onvif_utils.client import OnvifClient
            from onvif_utils.mediamtx_api import MediaMTXAPI

            for device in Device.objects.all():
                if device.source_type == "file":
                    continue
                if not device.username or not device.password:
                    continue
                try:
                    client = OnvifClient(
                        device.host, device.port, device.username, device.password
                    )
                    client.get_device_info()
                    device.is_online = True
                    device.failure_count = 0
                except Exception:
                    device.is_online = False
                device.save(update_fields=["is_online", "failure_count"])

            mtx = MediaMTXAPI()
            for _ in range(20):
                try:
                    mtx.list_paths()
                    break
                except Exception:
                    time.sleep(3)

            from onvif_utils.media import MediaService

            for device in Device.objects.filter(is_online=True):
                if device.source_type == "file":
                    continue
                if not device.username or not device.password:
                    continue
                try:
                    client = OnvifClient(
                        device.host, device.port, device.username, device.password
                    )
                    svc = MediaService(client)
                    profiles = svc.get_profiles()
                    stream_uris = {}
                    tokens = []
                    uris = []
                    for p in profiles:
                        uri = svc.get_stream_uri(
                            p["token"],
                            username=device.username,
                            password=device.password,
                        )
                        if uri:
                            stream_uris[p["token"]] = uri
                            tokens.append(p["token"])
                            uris.append(uri)
                    if stream_uris:
                        device.stream_uris = stream_uris
                        if (
                            not device.default_profile_token
                            or device.default_profile_token not in stream_uris
                        ):
                            device.default_profile_token = tokens[0]
                        device._skip_stream_refresh = True
                        device.save(
                            update_fields=["stream_uris", "default_profile_token"]
                        )
                        delattr(device, "_skip_stream_refresh")
                        default_uri = stream_uris.get(device.default_profile_token, "")
                        if default_uri:
                            mtx.ensure_camera_streams(
                                device.id,
                                [device.default_profile_token],
                                [default_uri],
                            )
                            mtx.ensure_raw_paths(
                                device.id,
                                device.default_profile_token,
                                default_uri,
                            )
                except Exception:
                    pass

            print("[Startup] ONVIF refreshed, MediaMTX synced")
            if os.environ.get("RUN_STARTUP_SYNC", "").lower() in ("1", "true"):
                regenerate_config_and_restart()
                print("[Startup] Config generated and DS restart triggered")
            else:
                print("[Startup] Skipping DS restart (RUN_STARTUP_SYNC not set)")

        thread = threading.Thread(target=check_and_sync, daemon=True)
        thread.start()
