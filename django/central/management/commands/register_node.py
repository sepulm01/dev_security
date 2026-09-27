from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from central.auth import encrypt_token, issue_token
from central.models import Node


class Command(BaseCommand):
    help = "Registra un nodo en la central y emite su token de acceso (solo se muestra una vez)."

    def add_arguments(self, parser):
        parser.add_argument("slug", type=str)
        parser.add_argument("--name", type=str, default="")
        parser.add_argument("--wg-ip", type=str, default="")
        parser.add_argument("--reissue", action="store_true", help="regenera el token de un nodo existente")

    def handle(self, *args, **options):
        slug = options["slug"]
        node, created = Node.objects.get_or_create(slug=slug, defaults={"name": options["name"] or slug})
        if not created and options["reissue"] is False and node.token_encrypted:
            raise CommandError(f"El nodo '{slug}' ya existe con token. Usa --reissue para regenerarlo.")

        if options["name"]:
            node.name = options["name"]
        if options["wg_ip"]:
            node.wg_ip = options["wg_ip"]
        node.is_active = True
        node.save(update_fields=["name", "wg_ip", "is_active"])

        token = issue_token()
        node.token_encrypted = encrypt_token(token)
        node.save(update_fields=["token_encrypted"])

        self.stdout.write(self.style.SUCCESS(f"Nodo '{node.slug}' registrado."))
        self.stdout.write(f"TOKEN={token}")
        self.stdout.write("Guárdalo en el .env del nodo: CENTRAL_API_URL, NODE_SLUG, NODE_TOKEN")
