from django.urls import path

from central import views

urlpatterns = [
    path("", views.dashboard, name="central_dashboard"),
    path("nodos/<slug:slug>/", views.node_detail, name="central_node_detail"),
    path("nodos/<slug:slug>/comando", views.command_create, name="central_command_create"),
    path("api/v1/nodos/<slug:slug>/telemetria", views.telemetry, name="central_telemetry"),
    path("api/v1/nodos/<slug:slug>/comandos", views.command_poll, name="central_command_poll"),
    path(
        "api/v1/nodos/<slug:slug>/comandos/<int:command_id>/resultado",
        views.command_result,
        name="central_command_result",
    ),
]
