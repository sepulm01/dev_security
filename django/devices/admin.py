from django.contrib import admin
from devices.models import Device


@admin.register(Device)
class DeviceAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "name",
        "host",
        "port",
        "manufacturer",
        "model",
        "is_online",
        "discovered_at",
    )
    list_filter = ("is_online", "manufacturer")
    search_fields = ("name", "host", "serial_number")
    ordering = ("-discovered_at",)
    readonly_fields = ("discovered_at", "last_seen", "stream_uris")
    fieldsets = (
        (None, {"fields": ("name", "host", "port", "username", "password")}),
        (
            "Info",
            {
                "fields": (
                    "manufacturer",
                    "model",
                    "firmware",
                    "serial_number",
                    "hardware_id",
                )
            },
        ),
        ("GPS", {"fields": ("latitude", "longitude")}),
        ("Estado", {"fields": ("is_online", "discovered_at", "last_seen")}),
        ("Streams", {"fields": ("stream_uris",), "classes": ("collapse",)}),
    )
