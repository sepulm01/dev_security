import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone

import redis
from redis import exceptions as redis_exceptions

from django.core.management.base import BaseCommand

logger = logging.getLogger("notification_bridge")


class NotificationBridge:
    def __init__(self, redis_url):
        self.redis_url = redis_url
        self._running = False
        self._thread = None
        self._rules_cache = []
        self._rules_thread = None
        self._preset_cache = {}
        self._preset_thread = None
        self._duration_first_seen = {}
        self._duration_last_seen = {}

    def _refresh_rules(self):
        try:
            from notifications.models import NotificationRule

            self._rules_cache = list(
                NotificationRule.objects.filter(is_active=True)
                .select_related("channel", "incident_type")
            )
        except Exception as e:
            logger.warning("Rule refresh error: %s", e)

    def _rules_refresh_loop(self):
        while self._running:
            self._refresh_rules()
            time.sleep(30)

    def _refresh_presets(self):
        try:
            r = redis.from_url(self.redis_url, decode_responses=True)
            keys = r.keys("device:*:active_preset")
            cache = {}
            for k in keys:
                try:
                    dev_id = int(k.split(":")[1])
                except (IndexError, ValueError):
                    continue
                val = r.get(k)
                if val:
                    cache[dev_id] = val
            self._preset_cache = cache
        except redis_exceptions.ConnectionError:
            pass
        except Exception as e:
            logger.warning("Preset refresh error: %s", e)

    def _preset_refresh_loop(self):
        while self._running:
            self._refresh_presets()
            time.sleep(5)

    def _filter_ivs_event(self, device_id, event_data):
        active_token = self._preset_cache.get(device_id)
        if active_token is None or active_token == "__fixed__":
            return event_data

        data = event_data.get("data", {})
        objects = data.get("Object", [])
        if objects:
            for obj in objects:
                for key in ("roi", "lc", "oc"):
                    vals = obj.get(key, []) or []
                    obj[key] = [v for v in vals if v.startswith(f"{active_token}_")]
                direction = obj.get("direction", "")
                if direction and not direction.startswith(f"{active_token}_"):
                    obj["direction"] = ""

        analytics = data.get("analytics", {})
        if analytics:
            filtered = {}
            for k, v in analytics.items():
                if k.startswith(f"{active_token}_"):
                    filtered[k] = v
            data["analytics"] = filtered

        return event_data

    def _build_event_context(self, device_id, event_data):
        from devices.models import Device

        ctx = {
            "device_id": device_id,
            "device_name": f"Device #{device_id}",
            "code": event_data.get("code", ""),
            "action": event_data.get("action", ""),
            "timestamp": event_data.get("timestamp", ""),
            "data": event_data.get("data", {}),
        }
        try:
            device = Device.objects.only("name").get(id=device_id)
            ctx["device_name"] = device.name
        except Exception:
            pass
        return ctx

    def _rule_matches_event(self, rule, device_id, event_data):
        if rule.devices.exists() and not rule.devices.filter(id=device_id).exists():
            return False

        code = event_data.get("code", "")
        if rule.event_codes and code not in rule.event_codes:
            return False

        if rule.analytics_trigger:
            data = event_data.get("data", {})
            objects = data.get("Object", [])
            analytics = data.get("analytics", {})
            has_trigger = False
            for obj in objects:
                for key in rule.analytics_trigger:
                    vals = obj.get(key, []) or obj.get(key, "")
                    if vals:
                        has_trigger = True
                        break
                if has_trigger:
                    break
            if not has_trigger:
                for k, v in analytics.items():
                    for trigger in rule.analytics_trigger:
                        if trigger in k.lower():
                            if isinstance(v, bool):
                                if v:
                                    has_trigger = True
                            elif isinstance(v, (int, float)):
                                if v > 0:
                                    has_trigger = True
                            elif v:
                                has_trigger = True
                            if has_trigger:
                                break
                    if has_trigger:
                        break
            if not has_trigger and "direction" in rule.analytics_trigger:
                for obj in objects:
                    if obj.get("direction", ""):
                        has_trigger = True
                        break
            if not has_trigger:
                return False

        if rule.min_objects > 0:
            obj_count = len(event_data.get("data", {}).get("Object", []))
            if obj_count < rule.min_objects:
                return False

        if rule.min_confidence > 0:
            objects = event_data.get("data", {}).get("Object", [])
            if objects:
                passed = any(
                    obj.get("confidence", 0) >= rule.min_confidence
                    for obj in objects
                )
                if not passed:
                    return False

        return True

    def _check_cooldown(self, rule, device_id):
        if rule.cooldown_seconds <= 0:
            return True
        try:
            r = redis.from_url(self.redis_url)
            key = f"notif:cooldown:{rule.id}:{device_id}"
            if r.exists(key):
                return False
            r.setex(key, rule.cooldown_seconds, "1")
            return True
        except Exception:
            return True

    def _send_notification(self, rule, device_id, event_data, photo_bytes=None, incident=None):
        from notifications.backends import get_backend

        ctx = self._build_event_context(device_id, event_data)
        if incident:
            ctx["incident_id"] = incident.id
        try:
            backend = get_backend(rule.channel.channel_type)
            if rule.message_template:
                message = backend.format_message(rule.message_template, ctx)
            else:
                message = backend.default_message(ctx)

            if rule.send_photo and photo_bytes:
                try:
                    success = backend.send_with_photo(rule.channel, message, photo_bytes)
                    return success, message
                except Exception as e:
                    logger.warning("Photo capture/send failed, falling back to text: %s", e)

            success = backend.send(rule.channel, message)
            return success, message
        except Exception as e:
            logger.warning("Send notification error: %s", e)
            return False, ""

    def _capture_device_snapshot(self, device_id):
        """
        Fallback: captura un frame via RTSP usando ffmpeg.
        La via principal para snapshots de incidentes es el SnapshotSender
        de DeepStream (GPU → TCP socket → snapshot_receiver), que tiene
        ~50ms de latencia vs 2-8s de esta via. Este metodo se mantiene
        como respaldo cuando el pipeline GPU no esta disponible.
        """
        from devices.models import Device
        from onvif_utils.snapshot import capture_frame_rtsp

        try:
            device = Device.objects.only("stream_uris", "default_profile_token").get(id=device_id)
        except Exception:
            return None
        uri = device.stream_uris.get(device.default_profile_token, "")
        if not uri:
            return None
        return capture_frame_rtsp(uri, timeout=8)

    def _create_incident(self, rule, device_id, event_data, photo_bytes=None):
        if rule.incident_type is None:
            return None

        try:
            from incidents.models import Incident, IncidentLog

            itype = rule.incident_type

            if itype.dedup_window_seconds > 0:
                window_start = datetime.now(timezone.utc).timestamp() - itype.dedup_window_seconds
                existing = Incident.objects.filter(
                    incident_type=itype,
                    device_id=device_id,
                    status="active",
                    created_at__gte=datetime.fromtimestamp(window_start, tz=timezone.utc),
                ).first()
                if existing:
                    return existing

            current_level = 1

            incident = Incident.objects.create(
                incident_type=itype,
                device_id=device_id,
                rule=rule,
                event_data=event_data,
                status="active",
                current_level=current_level,
            )

            IncidentLog.objects.create(
                incident=incident,
                level=current_level,
                action="created",
                detail={"event_code": event_data.get("code")},
            )

            if photo_bytes:
                try:
                    from django.core.files.base import ContentFile

                    incident.snapshot.save(
                        f"incident_{incident.id}.jpg",
                        ContentFile(photo_bytes),
                        save=True,
                    )
                except Exception as e:
                    logger.warning("Failed to save incident snapshot: %s", e)

            self._broadcast_incident(incident, device_id)
            return incident
        except Exception as e:
            logger.warning("Create incident error: %s", e)
            return None

    def _check_schedule(self, rule):
        from notifications.utils import is_rule_active_now

        return is_rule_active_now(rule)

    def _check_min_duration(self, rule, device_id):
        if rule.min_duration_seconds <= 0:
            return True

        now = time.time()
        key = (rule.id, device_id)
        last = self._duration_last_seen.get(key, 0)

        if now - last > 3:
            self._duration_first_seen[key] = now
            self._duration_last_seen[key] = now
            return False

        self._duration_last_seen[key] = now
        first = self._duration_first_seen.get(key, now)
        return now - first >= rule.min_duration_seconds

    def _broadcast_incident(self, incident, device_id):
        try:
            from asgiref.sync import async_to_sync
            from channels.layers import get_channel_layer

            device_name = f"Device #{device_id}"
            try:
                from devices.models import Device

                device = Device.objects.only("name").get(id=device_id)
                device_name = device.name
            except Exception:
                pass

            channel_layer = get_channel_layer()
            if channel_layer is None:
                return
            async_to_sync(channel_layer.group_send)(
                "incidents",
                {
                    "type": "incident_alert",
                    "incident_id": incident.id,
                    "device_id": device_id,
                    "device_name": device_name,
                    "incident_type": incident.incident_type.name,
                    "level": incident.current_level,
                },
            )
        except Exception as e:
            logger.warning("Broadcast incident error: %s", e)

    def _handle_event(self, device_id, event_data):
        event_data = self._filter_ivs_event(device_id, event_data)
        photo_bytes = self._capture_device_snapshot(device_id)

        for rule in self._rules_cache:
            try:
                if not self._check_schedule(rule):
                    continue
                if not self._rule_matches_event(rule, device_id, event_data):
                    continue
                if not self._check_min_duration(rule, device_id):
                    continue
                if not self._check_cooldown(rule, device_id):
                    continue

                incident = self._create_incident(rule, device_id, event_data, photo_bytes)

                if rule.send_immediate:
                    self._send_notification(rule, device_id, event_data, photo_bytes, incident)
                    key = (rule.id, device_id)
                    self._duration_first_seen.pop(key, None)

            except Exception as e:
                logger.warning("Rule processing error for %s: %s", rule.name, e)

    def _run_sync(self):
        self._running = True
        while self._running:
            try:
                client = redis.from_url(self.redis_url, decode_responses=True)
                client.ping()
                pubsub = client.pubsub()
                pubsub.psubscribe("device:*:events")
                logger.info("Notification bridge subscribed to device:*:events")

                while self._running:
                    msg = pubsub.get_message(timeout=1.0)
                    if msg and msg["type"] == "pmessage":
                        channel = msg["channel"]
                        try:
                            device_id = int(channel.split(":")[1])
                            event_data = json.loads(msg["data"])
                            self._handle_event(device_id, event_data)
                        except (IndexError, ValueError, json.JSONDecodeError) as e:
                            logger.warning("Bad message on %s: %s", channel, e)
            except redis_exceptions.ConnectionError as e:
                logger.warning("Redis connection error: %s. Retrying in 5s...", e)
                if self._running:
                    time.sleep(5)
            except Exception as e:
                logger.warning("Bridge error: %s. Retrying in 5s...", e)
                if self._running:
                    time.sleep(5)

    def start(self):
        self._refresh_rules()
        self._refresh_presets()
        self._running = True
        self._thread = threading.Thread(target=self._run_sync, daemon=True)
        self._thread.start()
        self._rules_thread = threading.Thread(target=self._rules_refresh_loop, daemon=True)
        self._rules_thread.start()
        self._preset_thread = threading.Thread(target=self._preset_refresh_loop, daemon=True)
        self._preset_thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
        if self._rules_thread:
            self._rules_thread.join(timeout=2)
        if self._preset_thread:
            self._preset_thread.join(timeout=2)


class Command(BaseCommand):
    help = "Run notification bridge: Redis events → NotificationRules → channels"

    def handle(self, *args, **options):
        redis_url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
        logger.info("Starting notification bridge, connecting to %s", redis_url)
        bridge = NotificationBridge(redis_url)

        def signal_handler(sig):
            logger.info("Received %s, shutting down", sig)
            bridge.stop()
            sys.exit(0)

        try:
            signal.signal(signal.SIGTERM, lambda s, f: signal_handler(s))
            signal.signal(signal.SIGINT, lambda s, f: signal_handler(s))
        except Exception:
            pass

        bridge.start()
        logger.info("Notification bridge started")

        while bridge._running:
            try:
                signal.pause()
            except InterruptedError:
                break
            except AttributeError:
                break
