from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("django.contrib.auth.urls")),
    path("", include("devices.urls")),
    path("live/", include("live.urls")),
    path("api/ptz/", include("ptz.urls")),
    path("", include("notifications.urls")),
    path("", include("incidents.urls")),
    path("", include("operadores.urls")),
    path("", include("detections.urls")),
    path("monitoring/", include("monitoring.urls")),
    path("central/", include("central.urls")),
]
