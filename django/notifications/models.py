from django.db import models


class NotificationChannel(models.Model):
    CHANNEL_TYPES = [
        ("telegram", "Telegram"),
        ("webhook", "Webhook"),
    ]

    name = models.CharField(max_length=120)
    channel_type = models.CharField(max_length=40, choices=CHANNEL_TYPES)
    config = models.JSONField(blank=True, default=dict)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.get_channel_type_display()})"


class NotificationRule(models.Model):
    name = models.CharField(max_length=120)
    channel = models.ForeignKey(
        NotificationChannel, on_delete=models.CASCADE, related_name="rules"
    )
    devices = models.ManyToManyField(
        "devices.Device",
        blank=True,
        related_name="notification_rules",
    )
    event_codes = models.JSONField(blank=True, default=list)
    analytics_trigger = models.JSONField(blank=True, default=list)
    min_objects = models.IntegerField(default=0)
    min_confidence = models.FloatField(default=0.25, help_text="Confianza minima de deteccion (0.0-1.0)")
    cooldown_seconds = models.IntegerField(default=0)
    min_duration_seconds = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)
    message_template = models.TextField(blank=True, default="")
    send_immediate = models.BooleanField(default=True)
    send_photo = models.BooleanField(default=False)
    valid_from = models.DateTimeField(null=True, blank=True)
    valid_until = models.DateTimeField(null=True, blank=True)
    schedule = models.JSONField(blank=True, default=dict)
    incident_type = models.ForeignKey(
        "incidents.IncidentType",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notification_rules",
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        scope = "global" if self.device is None else str(self.device)
        return f"{self.name} → {self.channel} ({scope})"
