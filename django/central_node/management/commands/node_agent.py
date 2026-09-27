import logging
import os
import time

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)

DEFAULT_TELEMETRY_INTERVAL = 30
DEFAULT_POLL_INTERVAL = 10


class Command(BaseCommand):
    help = "Agente del nodo: envía telemetría a la central y ejecuta comandos remotos (loop infinito)."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="un solo ciclo y salir")
        parser.add_argument("--telemetry-interval", type=int, default=DEFAULT_TELEMETRY_INTERVAL)
        parser.add_argument("--poll-interval", type=int, default=DEFAULT_POLL_INTERVAL)

    def handle(self, *args, **options):
        base_url = os.environ.get("CENTRAL_API_URL", "")
        slug = os.environ.get("NODE_SLUG", "")
        token = os.environ.get("NODE_TOKEN", "")
        if not all([base_url, slug, token]):
            self.stderr.write("Faltan CENTRAL_API_URL, NODE_SLUG o NODE_TOKEN en el entorno.")
            return

        from central_node.api import CentralAPIClient
        from central_node.collectors import collect_device_states, collect_metrics, get_version
        from central_node.executors import execute_command

        client = CentralAPIClient(base_url, slug, token)
        self.stdout.write(f"Agente iniciado para nodo '{slug}' -> {base_url}")

        telemetry_interval = options["telemetry_interval"]
        poll_interval = options["poll_interval"]
        last_telemetry = 0.0

        while True:
            now = time.monotonic()
            try:
                if now - last_telemetry >= telemetry_interval:
                    payload = {
                        "version": get_version(),
                        "metrics": collect_metrics(),
                        "devices": collect_device_states(),
                    }
                    response = client.send_telemetry(payload)
                    last_telemetry = now
                    if response.get("status_code") == 200:
                        self.stdout.write(f"telemetría OK ({response.get('data', {}).get('status', '')})")
                    else:
                        self.stdout.write(f"telemetría: HTTP {response.get('status_code')} {response.get('error', '')}")

                for command in client.poll_commands():
                    self.stdout.write(f"comando #{command['id']} {command['kind']}")
                    try:
                        result = execute_command(command["kind"], command.get("payload") or {})
                        client.send_command_result(command["id"], True, result)
                    except Exception as e:
                        logger.exception("comando %s falló", command["kind"])
                        client.send_command_result(command["id"], False, {"error": str(e)})
            except Exception:
                logger.exception("ciclo del agente")

            if options["once"]:
                break
            time.sleep(min(poll_interval, telemetry_interval))
