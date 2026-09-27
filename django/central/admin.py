from django.contrib import admin

from central.models import Node, NodeAlert, NodeCommand, NodeMetric


@admin.register(Node)
class NodeAdmin(admin.ModelAdmin):
    list_display = ("slug", "name", "status", "last_seen", "version", "is_active")
    list_filter = ("status", "is_active")
    search_fields = ("slug", "name")
    readonly_fields = ("token_encrypted",)


@admin.register(NodeMetric)
class NodeMetricAdmin(admin.ModelAdmin):
    list_display = ("node", "source", "created_at")
    list_filter = ("source",)


@admin.register(NodeCommand)
class NodeCommandAdmin(admin.ModelAdmin):
    list_display = ("node", "kind", "status", "created_at", "executed_at")
    list_filter = ("kind", "status")


@admin.register(NodeAlert)
class NodeAlertAdmin(admin.ModelAdmin):
    list_display = ("node", "kind", "status", "created_at")
    list_filter = ("kind", "status")
