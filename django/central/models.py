from django.db import models


class Node(models.Model):
    STATUS_CHOICES = [
        ("online", "Online"),
        ("degraded", "Degradado"),
        ("offline", "Offline"),
    ]

    slug = models.SlugField(max_length=60, unique=True)
    name = models.CharField(max_length=120)
    site = models.ForeignKey(
        "operadores.Site",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="nodes",
    )
    wg_ip = models.GenericIPAddressField(null=True, blank=True)
    token_encrypted = models.TextField(blank=True, default="")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="offline")
    last_seen = models.DateTimeField(null=True, blank=True)
    version = models.CharField(max_length=40, blank=True, default="")
    devices_data = models.JSONField(blank=True, default=dict)
    metrics_summary = models.JSONField(blank=True, default=dict)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class NodeMetric(models.Model):
    node = models.ForeignKey(Node, on_delete=models.CASCADE, related_name="metrics")
    source = models.CharField(max_length=50)
    data = models.JSONField(blank=True, default=dict)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.node.slug}:{self.source}"


class NodeCommand(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pendiente"),
        ("sent", "Enviado"),
        ("ok", "Ejecutado"),
        ("error", "Error"),
    ]

    node = models.ForeignKey(Node, on_delete=models.CASCADE, related_name="commands")
    kind = models.CharField(max_length=40)
    payload = models.JSONField(blank=True, default=dict)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    result = models.JSONField(blank=True, default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    executed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.node.slug}:{self.kind} [{self.status}]"


class NodeAlert(models.Model):
    STATUS_CHOICES = [
        ("active", "Activa"),
        ("resolved", "Resuelta"),
    ]

    node = models.ForeignKey(Node, on_delete=models.CASCADE, related_name="alerts")
    kind = models.CharField(max_length=40)
    message = models.CharField(max_length=240, blank=True, default="")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="active")
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.node.slug}:{self.kind} [{self.status}]"
