import os

NVDSANALYTICS_CONFIG_FILE = "config_nvdsanalytics.txt"
FRAME_WIDTH = 1280
FRAME_HEIGHT = 720

MAX_INSTANCES = 1

PIPELINE_CONFIGS = {
    "main": {
        "models_dir": "../models/peoplenet",
        "max_batch": 64,
        "max_devices_per_instance": 64,
        "filename_template": "",
    },
    "retinaface": {
        "models_dir": "../models/retinaface_det10g",
        "max_batch": 1,
        "max_devices_per_instance": 1,
        "extra_yaml": "face-class-id: 0\n",
    },
    "yolov9": {
        "models_dir": "../models/yolov9",
        "max_batch": 3,
        "max_devices_per_instance": 3,
    },
    "trafficcamnet_lpr": {
        "models_dir": "../models/trafficcamnet",
        "max_batch": 1,
        "max_devices_per_instance": 1,
        "sgie_sections": (
            "secondary-gie0:\n"
            "  plugin-type: 0\n"
            "  config-file-path: ../models/trafficcamnet/lpd/sgie_config.yml\n"
            "\n"
            "secondary-gie1:\n"
            "  plugin-type: 0\n"
            "  config-file-path: ../models/trafficcamnet/lpr/sgie_config.yml\n"
        ),
    },
}

CONTAINER_BASE = "mediamtx-manager-computer-vision"
PIPELINE_CONTAINER_SUFFIX = {
    "main": "computer-vision",
    "retinaface": "computer-vision-retinaface",
    "yolov9": "computer-vision-yolov9",
    "trafficcamnet_lpr": "computer-vision-lpr",
}


def get_pipeline_filename(pipeline_id, instance=1):
    basename = PIPELINE_CONFIGS[pipeline_id].get("filename_template", pipeline_id)
    if basename:
        base = f"config_{basename}"
    else:
        base = "config"
    if instance == 1:
        return f"{base}.yml"
    return f"{base}_{instance}.yml"


def get_default_filename(pipeline_id):
    return get_pipeline_filename(pipeline_id, instance=1)


def get_pipeline_containers(pipeline_id):
    suffix = PIPELINE_CONTAINER_SUFFIX[pipeline_id]
    names = []
    for n in range(1, MAX_INSTANCES + 1):
        if n == 1:
            names.append(f"mediamtx-manager-{suffix}-1")
        else:
            names.append(f"mediamtx-manager-{suffix}-{n}-1")
    return names


def get_pipeline_container(pipeline_id):
    return get_pipeline_containers(pipeline_id)[0]


def _read_labels(pipeline_id, config_dir):
    pipeline_cfg = PIPELINE_CONFIGS[pipeline_id]
    models_dir = pipeline_cfg.get("models_dir", "../models/peoplenet")
    labels_path = os.path.normpath(os.path.join(config_dir, models_dir, "labels.txt"))
    with open(labels_path) as f:
        return ";".join(line.strip() for line in f if line.strip())


def _shapes_to_nvdsanalytics(shapes, stream_idx=0, prefix=""):
    sections = {}
    for shape in shapes:
        obj_type = shape.get("object", "")
        name = shape.get("name", "unnamed")
        shape_type = shape.get("type", "")

        if obj_type == "polygon" and shape_type == "RF":
            pts = shape.get("points", [])
            if len(pts) >= 4:
                coords = ";".join(
                    f"{round(p['x'] * FRAME_WIDTH)};{round(p['y'] * FRAME_HEIGHT)}"
                    for p in pts
                )
                key = f"roi-{name}" if not prefix else f"roi-{prefix}_{name}"
                section = f"roi-filtering-stream-{stream_idx}"
                if section not in sections:
                    sections[section] = {"enable": "1", "class-id": "-1"}
                sections[section][key] = coords

        elif obj_type == "polygon" and shape_type == "OC":
            pts = shape.get("points", [])
            if len(pts) >= 4:
                coords = ";".join(
                    f"{round(p['x'] * FRAME_WIDTH)};{round(p['y'] * FRAME_HEIGHT)}"
                    for p in pts
                )
                key = f"roi-{name}" if not prefix else f"roi-{prefix}_{name}"
                section = f"overcrowding-stream-{stream_idx}"
                if section not in sections:
                    sections[section] = {
                        "enable": "1", "class-id": "-1", "object-threshold": "3"
                    }
                sections[section][key] = coords

        elif obj_type == "line" and shape_type == "cross":
            x1 = round(shape["x1"] * FRAME_WIDTH)
            y1 = round(shape["y1"] * FRAME_HEIGHT)
            x2 = round(shape["x2"] * FRAME_WIDTH)
            y2 = round(shape["y2"] * FRAME_HEIGHT)
            key = f"line-crossing-{name}" if not prefix else f"line-crossing-{prefix}_{name}"
            section = f"line-crossing-stream-{stream_idx}"
            if section not in sections:
                sections[section] = {"enable": "1", "class-id": "0", "mode": "loose"}
            sections[section][key] = f"{x1};{y1};{x2};{y2}"

        elif obj_type == "line" and shape_type == "direction":
            x1 = round(shape["x1"] * FRAME_WIDTH)
            y1 = round(shape["y1"] * FRAME_HEIGHT)
            x2 = round(shape["x2"] * FRAME_WIDTH)
            y2 = round(shape["y2"] * FRAME_HEIGHT)
            key = f"direction-{name}" if not prefix else f"direction-{prefix}_{name}"
            section = f"direction-detection-stream-{stream_idx}"
            if section not in sections:
                sections[section] = {"enable": "1", "class-id": "0"}
            sections[section][key] = f"{x1};{y1};{x2};{y2}"

    return sections


def _serialize_nvdsanalytics(sections):
    lines = [
        "[property]",
        "enable=1",
        "config-width=1280",
        "config-height=720",
        "osd-mode=1",
        "",
    ]
    for section, props in sections.items():
        lines.append(f"[{section}]")
        for key, val in props.items():
            lines.append(f"{key}={val}")
        lines.append("")
    return "\n".join(lines)


def generate_nvdsanalytics_config(devices, config_dir, output_filename=None):
    from django.apps import apps

    AnalyticsPreset = apps.get_model("devices", "AnalyticsPreset")

    if output_filename is None:
        output_filename = NVDSANALYTICS_CONFIG_FILE

    all_sections = {}
    stream_idx = 0
    for device in devices:
        presets = AnalyticsPreset.objects.filter(
            device=device, shapes__isnull=False
        ).exclude(shapes=[])
        if not presets:
            stream_idx += 1
            continue
        for preset in presets:
            prefix = preset.preset_token if preset.preset_token != "__fixed__" else ""
            sections = _shapes_to_nvdsanalytics(preset.shapes, stream_idx, prefix)
            for sec, props in sections.items():
                if sec not in all_sections:
                    all_sections[sec] = dict(props)
                else:
                    for k, v in props.items():
                        if k not in ("enable", "class-id", "mode", "object-threshold"):
                            all_sections[sec][k] = v
        stream_idx += 1

    content = _serialize_nvdsanalytics(all_sections)
    output_path = os.path.join(config_dir, output_filename)
    os.makedirs(config_dir, exist_ok=True)
    with open(output_path, "w") as f:
        f.write(content)


def _write_pgie_config(config_dir, models_dir, output_name, batch_size):
    template_path = os.path.normpath(os.path.join(config_dir, models_dir, "pgie_config.yml"))
    output_path = os.path.join(config_dir, output_name)
    with open(template_path) as f:
        content = f.read()
    import re
    content = re.sub(r'batch-size:\s*\d+', f'batch-size: {batch_size}', content)
    # Fix relative paths that don't start with / or ../
    for key in ('onnx-file', 'labelfile-path', 'int8-calib-file'):
        content = re.sub(
            rf'({key}:\s*)(?!/|\.\./)(\S+)',
            rf'\1{models_dir}/\2',
            content,
        )
    with open(output_path, "w") as f:
        f.write(content)


def _ensure_tcp(uri):
    if not uri or uri.startswith("file://"):
        return uri
    if "rtsp_transport" in uri:
        return uri
    sep = "&" if "?" in uri else "?"
    return uri + sep + "rtsp_transport=tcp"


def generate_config(devices, output_path, pipeline_id="main", nvdsanalytics_filename=None, batch_size=1, pgie_config_name=None):
    if nvdsanalytics_filename is None:
        nvdsanalytics_filename = NVDSANALYTICS_CONFIG_FILE
    if pgie_config_name is None:
        pgie_config_name = f"pgie_config_{pipeline_id}.yml"
    uris = []
    for device in devices:
        if not device.stream_uris:
            continue
        if device.source_type == "rtsp" and not device.is_online:
            continue
        if not device.default_profile_token:
            continue
        uri = device.stream_uris.get(device.default_profile_token, "")
        if not uri:
            continue
        if device.source_type == "file":
            token = device.default_profile_token
            uri = f"rtsp://mediamtx:8554/cam_{device.id}_{token}"
        else:
            uri = device.stream_uris.get(device.default_profile_token, "")
        uris.append(uri)

    source_list = ";".join(uris) + ";" if uris else ""

    effective_batch = min(batch_size, len(uris)) if uris else 1

    pipeline_cfg = PIPELINE_CONFIGS[pipeline_id]

    models_dir = pipeline_cfg.get("models_dir", "../models/peoplenet")
    sgie_sections = pipeline_cfg.get("sgie_sections", "")
    extra_yaml = pipeline_cfg.get("extra_yaml", "")

    config_dir = os.path.dirname(output_path)
    os.makedirs(config_dir, exist_ok=True)

    labels = _read_labels(pipeline_id, config_dir)

    config = f"""{extra_yaml}source-list:
  list: "{source_list}"

streammux:
  batch-size: {effective_batch}
  batched-push-timeout: 40000
  width: 1920
  height: 1080

labels: {labels}

primary-gie:
  plugin-type: 0
  config-file-path: ./{pgie_config_name}

{sgie_sections}analytics:
  enable: 1
  config-file: {nvdsanalytics_filename}

osd:
  process-mode: 0
  display-text: 1

tiler:
  width: 1280
  height: 720

sink:
  qos: 0
"""

    with open(output_path, "w") as f:
        f.write(config)

    return uris


def generate_all_configs(config_dir=None):
    from django.apps import apps

    Device = apps.get_model("devices", "Device")

    if config_dir is None:
        config_dir = os.path.dirname(
            os.environ.get("CONFIG_YML_PATH", "/opt/computer_vision/config/config.yml")
        )

    for pipeline_id in PIPELINE_CONFIGS:
        pipeline_devices = list(
            Device.objects.filter(
                deepstream_pipeline=pipeline_id,
                is_online=True,
                stream_uris__isnull=False,
                source_type="rtsp",
            ).exclude(stream_uris={})
        )
        pipeline_devices += list(
            Device.objects.filter(
                deepstream_pipeline=pipeline_id,
                stream_uris__isnull=False,
                source_type="file",
            ).exclude(stream_uris={})
        )

        pipeline_cfg = PIPELINE_CONFIGS[pipeline_id]
        filename = get_pipeline_filename(pipeline_id, instance=1)
        output_path = os.path.join(config_dir, filename)

        if pipeline_devices:
            batch = min(len(pipeline_devices), pipeline_cfg.get("max_batch", 1))
            pgie_name = f"pgie_config_{pipeline_id}.yml"
            _write_pgie_config(config_dir, pipeline_cfg["models_dir"],
                               pgie_name, batch)
            generate_config(pipeline_devices, output_path, pipeline_id,
                            batch_size=batch, pgie_config_name=pgie_name)
            generate_nvdsanalytics_config(pipeline_devices, config_dir)
        else:
            write_empty_config(output_path, pipeline_id)



def write_empty_config(output_path, pipeline_id):
    pipeline_cfg = PIPELINE_CONFIGS[pipeline_id]
    models_dir = pipeline_cfg.get("models_dir", "../models/peoplenet")
    sgie_sections = pipeline_cfg.get("sgie_sections", "")
    extra_yaml = pipeline_cfg.get("extra_yaml", "")

    config_dir = os.path.dirname(output_path)
    os.makedirs(config_dir, exist_ok=True)

    labels = _read_labels(pipeline_id, config_dir)

    config = f"""{extra_yaml}source-list:
  list: ""

streammux:
  batch-size: 1
  batched-push-timeout: 40000
  width: 1920
  height: 1080

labels: {labels}

primary-gie:
  plugin-type: 0
  config-file-path: {models_dir}/pgie_config.yml

{sgie_sections}analytics:
  enable: 1
  config-file: {NVDSANALYTICS_CONFIG_FILE}

osd:
  process-mode: 0
  display-text: 1

tiler:
  width: 1280
  height: 720

sink:
  qos: 0
"""

    with open(output_path, "w") as f:
        f.write(config)
