import json
import logging

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from central.auth import verify_signature
from central.models import Node, NodeCommand, NodeMetric
from central.services import derive_status, update_node_status

logger = logging.getLogger(__name__)

MAX_METRICS_PER_NODE_SOURCE = 300


def _unauthorized():
    return JsonResponse({"error": "firma inválida o fuera de ventana"}, status=401)


def _get_node(slug):
    try:
        return Node.objects.get(slug=slug, is_active=True)
    except Node.DoesNotExist:
        return None


@csrf_exempt
def telemetry(request, slug):
    if request.method != "POST":
        return JsonResponse({"error": "método no permitido"}, status=405)

    node = _get_node(slug)
    if not node:
        return JsonResponse({"error": "nodo desconocido"}, status=404)

    signature = request.headers.get("X-Node-Signature", "")
    timestamp = request.headers.get("X-Node-Timestamp", "")
    if not verify_signature(node, timestamp, request.body, signature):
        return _unauthorized()

    try:
        payload = json.loads(request.body or b"{}")
    except (ValueError, json.JSONDecodeError):
        return JsonResponse({"error": "body inválido"}, status=400)

    now = timezone.now()
    node.last_seen = now
    version = payload.get("version")
    if version:
        node.version = version[:40]
    node.save(update_fields=["last_seen", "version"])

    metrics = payload.get("metrics") or {}
    node_metrics = {}
    for source, data in metrics.items():
        if not isinstance(data, dict):
            continue
        NodeMetric.objects.create(node=node, source=source[:50], data=data)
        node_metrics[source] = data
    if node_metrics:
        _prune(node)

    node.devices_data = {"devices": payload.get("devices") or [], "updated_at": now.isoformat()}
    node.metrics_summary = node_metrics
    node.save(update_fields=["devices_data", "metrics_summary"])

    status, reasons = derive_status(node_metrics, payload.get("devices") or [])
    update_node_status(node, status, reasons)

    return JsonResponse({"ok": True, "status": node.status})


def _prune(node):
    for source in NodeMetric.objects.filter(node=node).values_list("source", flat=True).distinct():
        ids = list(
            NodeMetric.objects.filter(node=node, source=source)
            .order_by("-created_at")
            .values_list("id", flat=True)[MAX_METRICS_PER_NODE_SOURCE:]
        )
        if ids:
            NodeMetric.objects.filter(id__in=ids).delete()


@csrf_exempt
def command_result(request, slug, command_id):
    if request.method != "POST":
        return JsonResponse({"error": "método no permitido"}, status=405)

    node = _get_node(slug)
    if not node:
        return JsonResponse({"error": "nodo desconocido"}, status=404)

    signature = request.headers.get("X-Node-Signature", "")
    timestamp = request.headers.get("X-Node-Timestamp", "")
    if not verify_signature(node, timestamp, request.body, signature):
        return _unauthorized()

    try:
        command = NodeCommand.objects.get(id=command_id, node=node)
    except NodeCommand.DoesNotExist:
        return JsonResponse({"error": "comando desconocido"}, status=404)

    try:
        payload = json.loads(request.body or b"{}")
    except (ValueError, json.JSONDecodeError):
        return JsonResponse({"error": "body inválido"}, status=400)

    success = bool(payload.get("success"))
    command.status = "ok" if success else "error"
    command.result = payload.get("result") or {}
    command.executed_at = timezone.now()
    command.save(update_fields=["status", "result", "executed_at"])
    return JsonResponse({"ok": True})


@csrf_exempt
def command_poll(request, slug):
    if request.method != "GET":
        return JsonResponse({"error": "método no permitido"}, status=405)

    node = _get_node(slug)
    if not node:
        return JsonResponse({"error": "nodo desconocido"}, status=404)

    signature = request.headers.get("X-Node-Signature", "")
    timestamp = request.headers.get("X-Node-Timestamp", "")
    if not verify_signature(node, timestamp, b"", signature):
        return _unauthorized()

    commands = list(
        NodeCommand.objects.filter(node=node, status="pending").order_by("created_at")[:5]
    )
    result = []
    for command in commands:
        command.status = "sent"
        command.sent_at = timezone.now()
        command.save(update_fields=["status", "sent_at"])
        result.append({"id": command.id, "kind": command.kind, "payload": command.payload})

    return JsonResponse({"commands": result})


@login_required
def dashboard(request):
    from central.models import NodeAlert

    nodes = Node.objects.all()
    alerts = NodeAlert.objects.filter(status="active").order_by("-created_at")[:20]
    return render(request, "central/dashboard.html", {"nodes": nodes, "alerts": alerts})


@login_required
def node_detail(request, slug):
    node = get_object_or_404(Node, slug=slug)
    sources = {}
    for source in ["system", "mediamtx", "deepstream"]:
        rows = NodeMetric.objects.filter(node=node, source=source).order_by("-created_at")[:60]
        sources[source] = [
            {"ts": row.created_at.isoformat(), "data": row.data}
            for row in reversed(list(rows))
        ]
    commands = NodeCommand.objects.filter(node=node)[:10]
    return render(
        request,
        "central/node_detail.html",
        {"node": node, "sources": sources, "commands": commands},
    )


@login_required
def command_create(request, slug):
    if request.method != "POST":
        return redirect("central_dashboard")
    node = get_object_or_404(Node, slug=slug)
    kind = (request.POST.get("kind") or "")[:40]
    if kind in ("get_state", "sync_config", "sync_mediamtx", "restart_pipeline"):
        payload = (
            {"pipeline": (request.POST.get("pipeline") or "")[:40]}
            if kind == "restart_pipeline"
            else {}
        )
        NodeCommand.objects.create(node=node, kind=kind, payload=payload)
    return redirect("central_node_detail", slug=node.slug)
