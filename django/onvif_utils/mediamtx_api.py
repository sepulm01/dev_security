import os
from urllib.parse import urlparse, urlunparse, quote

import requests


class MediaMTXAPI:
    """Client for the MediaMTX REST API.

    Manages camera stream paths (raw RTSP + transcoded H264) via
    MediaMTX's /v3/config/paths/ endpoints on port 9997.
    """

    def __init__(self, base_url=None, api_key=None):
        self.base_url = base_url or os.environ.get(
            "MEDIAMTX_API_URL", "http://127.0.0.1:9997"
        )
        self.api_key = api_key or os.environ.get("MEDIAMTX_API_KEY", "")

    def _headers(self):
        import base64

        creds = base64.b64encode(b"admin:mediamtx_admin_pass").decode()
        return {"Authorization": f"Basic {creds}"}

    def _post(self, path, **kwargs):
        """POST to path on MediaMTX API, return parsed JSON."""
        resp = requests.post(
            f"{self.base_url}{path}",
            headers=self._headers(),
            **kwargs,
        )
        resp.raise_for_status()
        return resp.json()

    def _get(self, path, **kwargs):
        """GET from path on MediaMTX API, return parsed JSON."""
        resp = requests.get(
            f"{self.base_url}{path}",
            headers=self._headers(),
            **kwargs,
        )
        resp.raise_for_status()
        return resp.json()

    def _delete(self, path, **kwargs):
        """DELETE path on MediaMTX API, return parsed JSON."""
        resp = requests.delete(
            f"{self.base_url}{path}",
            headers=self._headers(),
            **kwargs,
        )
        resp.raise_for_status()
        return resp.json()

    def add_path(
        self,
        name,
        source=None,
        run_on_init=None,
        run_on_init_restart=False,
        run_on_ready=None,
        run_on_ready_restart=False,
        run_on_demand=None,
        run_on_demand_restart=False,
    ):
        """Register a new stream path in MediaMTX.

        Args:
            name: Path name (e.g. "cam_1_profile0").
            source: RTSP source URL for pull mode, or "publisher" for push.
            run_on_init: Command to run when path is created.
            run_on_init_restart: Restart command if it exits.
            run_on_ready: Command when source is ready (used for ffmpeg transcoding).
            run_on_ready_restart: Restart command if it exits.
            run_on_demand: Command when at least one reader connects.
            run_on_demand_restart: Restart command if it exits.
        """
        body = {}
        if source is not None:
            body["source"] = source
        if run_on_init is not None:
            body["runOnInit"] = run_on_init
            body["runOnInitRestart"] = run_on_init_restart
        if run_on_ready is not None:
            body["runOnReady"] = run_on_ready
            body["runOnReadyRestart"] = run_on_ready_restart
        if run_on_demand is not None:
            body["runOnDemand"] = run_on_demand
            body["runOnDemandRestart"] = run_on_demand_restart
        return self._post(f"/v3/config/paths/add/{name}", json=body)

    def delete_path(self, name):
        """Remove a stream path by name."""
        return self._delete(f"/v3/config/paths/delete/{name}")

    def list_paths(self):
        """Return list of all configured stream paths."""
        data = self._get("/v3/config/paths/list")
        return data.get("items", [])

    def camera_paths(self, device_id):
        """Return only the stream paths belonging to a device."""
        prefix = f"cam_{device_id}_"
        return [p for p in self.list_paths() if p.get("name", "").startswith(prefix)]

    def delete_camera_paths(self, device_id):
        """Remove all stream paths for a device."""
        paths = self.camera_paths(device_id)
        for p in paths:
            try:
                self.delete_path(p["name"])
            except requests.RequestException as e:
                print(f"Error deleting path {p['name']}: {e}")

    def _encode_rtsp_url(self, uri):
        """Percent-encode username/password in an RTSP URL.

        MediaMTX's internal RTSP client rejects special chars (e.g. ``+``)
        in credentials, so we encode them via ``urllib.parse.quote``.
        """
        parsed = urlparse(uri)
        if parsed.username:
            encoded_username = quote(parsed.username, safe="")
            encoded_password = (
                quote(parsed.password, safe="") if parsed.password else ""
            )
            netloc = f"{encoded_username}:{encoded_password}@{parsed.hostname}"
            if parsed.port:
                netloc += f":{parsed.port}"
            return urlunparse(
                (
                    parsed.scheme,
                    netloc,
                    parsed.path,
                    parsed.params,
                    parsed.query,
                    parsed.fragment,
                )
            )
        return uri

    def ensure_raw_paths(self, device_id, profile_token, rtsp_uri):
        """Create a raw RTSP proxy path in MediaMTX.

        MediaMTX pulls the RTSP stream from the camera and serves it at
        rtsp://mediamtx:8554/cam_{id}_{token}. DeepStream consumes this
        stable local path instead of the unreliable camera directly.
        """
        path_name = f"cam_{device_id}_{profile_token}"
        encoded_source = self._encode_rtsp_url(rtsp_uri)
        try:
            self.add_path(path_name, source=encoded_source)
        except requests.RequestException as e:
            print(f"Error adding raw path {path_name}: {e}")

    def ensure_camera_streams(self, device_id, profiles, stream_uris):
        """Create a single transcoded ``_hw`` path per device for WebRTC.

        The DeepStream pipeline pulls RTSP directly from the camera, so no raw
        MediaMTX path is needed.  The ``_hw`` path uses ``runOnDemand``:
        ffmpeg transcodes only when a WebRTC viewer is connected.

        Old raw paths (without ``_hw`` suffix) are cleaned up.
        """
        prefix = f"cam_{device_id}_"
        all_paths = self.list_paths()
        existing = {p["name"] for p in all_paths}

        requested_hw = {f"cam_{device_id}_{t}_hw" for t in profiles}

        for p in all_paths:
            name = p["name"]
            if name.startswith(prefix) and name not in requested_hw and name.endswith("_hw"):
                try:
                    self.delete_path(name)
                    existing.discard(name)
                except requests.RequestException:
                    pass

        for profile_token, stream_uri in zip(profiles, stream_uris):
            hw_name = f"cam_{device_id}_{profile_token}_hw"
            if hw_name in existing:
                continue

            encoded_source = self._encode_rtsp_url(stream_uri)
            ffmpeg_cmd = (
                f"ffmpeg -rtsp_transport tcp -i {encoded_source} "
                f"-c:v libx264 -preset ultrafast -tune zerolatency -c:a copy "
                f'-f rtsp "rtsp://127.0.0.1:8554/{hw_name}"'
            )

            try:
                self.add_path(
                    hw_name,
                    source="publisher",
                    run_on_demand=ffmpeg_cmd,
                    run_on_demand_restart=True,
                )
                existing.add(hw_name)
            except requests.RequestException as e:
                print(f"Error adding path {hw_name}: {e}")

    def ensure_file_stream(self, device):
        """Publish an MP4 file as a continuous RTSP stream via MediaMTX.

        The file is looped with ``-stream_loop -1`` and published to
        ``cam_{device_id}_{token}``. Both DeepStream and WebRTC consume
        this single source, keeping detections in sync with the preview.
        """
        token = device.default_profile_token or "main"
        file_uri = device.stream_uris.get(token, "")
        if not file_uri:
            return

        path_name = f"cam_{device.id}_{token}"
        all_paths = self.list_paths()
        existing = {p["name"] for p in all_paths}

        if path_name in existing:
            return

        file_path = file_uri.replace("file://", "")
        ffmpeg_cmd = (
            f"ffmpeg -re -stream_loop -1 -i {file_path} "
            f"-c:v libx264 -preset ultrafast -tune zerolatency -bf 0 -g 30 "
            f"-c:a aac -b:a 128k "
            f'-f rtsp "rtsp://127.0.0.1:8554/{path_name}"'
        )

        try:
            self.add_path(
                path_name,
                source="publisher",
                run_on_init=ffmpeg_cmd,
                run_on_init_restart=True,
            )
        except requests.RequestException as e:
            print(f"Error adding file path {path_name}: {e}")
