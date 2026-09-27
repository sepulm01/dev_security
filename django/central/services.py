import logging
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)

NODE_OFFLINE_AFTER_SECONDS = 90

GPU_MEM_THRESHOLD = 85.0
DISK_THRESHOLD = 85.0


def derive_status(metrics_summary: dict, devices: list) -> str:
    """Deriva online/degradado/offline desde el resumen de métricas."""
    reasons = []
    for device in devices or []:
        if device.get("is_online") is False:
            reasons.append(f"cámara {device.get('name') or device.get('id')} caída")
        if device.get("fps_low"):
            reasons.append(f"FPS bajo en cámara {device.get('name') or device.get('id')}")

    system = metrics_summary.get("system") or {}
    gpu = system.get("gpu") or {}
    gpus = gpu.get("gpus") or []
    for g in gpus:
        mem = g.get("memory_utilization_pct")
        if isinstance(mem, (int, float)) and mem > GPU_MEM_THRESHOLD:
            reasons.append(f"GPU {g.get('name', '')} al {mem:.0f}%")

    partitions = system.get("disk", {}).get("partitions") or []
    EXCLUDED_ROOTS = {"proc", "sys", "dev", "run", "etc", "snap", "boot", "tmp", "mnt", "media"}
    for part in partitions:
        mountpoint = part.get("mountpoint", "")
        if mountpoint != "/":
            if mountpoint.count("/") != 1:
                continue
            root = mountpoint.strip("/")
            if root in EXCLUDED_ROOTS:
                continue
        percent = part.get("percent")
        if isinstance(percent, (int, float)) and percent > DISK_THRESHOLD:
            reasons.append(f"disco {part.get('mountpoint', '')} al {percent:.0f}%")

    deepstream = metrics_summary.get("deepstream") or {}
    fps = deepstream.get("fps") or {}
    for pipeline, value in fps.items():
        if isinstance(value, dict):
            total = value.get("total_fps")
            if total is not None and total <= 0:
                reasons.append(f"pipeline {pipeline} sin FPS")

    if reasons:
        return "degraded", reasons[:5]
    return "online", []


def update_node_status(node, status: str, reasons: list | None = None):
    from central.models import NodeAlert

    reasons = reasons or []
    if node.status == status:
        return
    previous = node.status
    node.status = status
    node.save(update_fields=["status"])

    if status == "offline":
        NodeAlert.objects.create(
            node=node,
            kind="nodo_offline",
            message=f"Nodo sin telemetría por más de {NODE_OFFLINE_AFTER_SECONDS}s",
        )
    elif status == "degraded":
        NodeAlert.objects.create(
            node=node,
            kind="nodo_degradado",
            message="; ".join(reasons) or "estado degradado",
        )
    elif status == "online" and previous in ("offline", "degraded"):
        NodeAlert.objects.filter(node=node, kind__in=["nodo_offline", "nodo_degradado"], status="active").update(
            status="resolved", resolved_at=timezone.now()
        )
    logger.info("Nodo %s: %s -> %s (%s)", node.slug, previous, status, "; ".join(reasons))


def mark_offline_nodes():
    from central.models import Node

    cutoff = timezone.now() - timedelta(seconds=NODE_OFFLINE_AFTER_SECONDS)
    for node in Node.objects.filter(is_active=True).exclude(last_seen=None):
        if node.last_seen < cutoff and node.status != "offline":
            update_node_status(node, "offline")
