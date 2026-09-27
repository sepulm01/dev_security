import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("SECRET_KEY", "django-insecure-&p8e%y1o3u%8y7+h%003^v!4l)rw@qai-u3^acdzy)(q8)27i*")
DEBUG = os.environ.get("DEBUG", "True") == "True"
ALLOWED_HOSTS = [host for host in os.environ.get("ALLOWED_HOSTS", "*").split(",") if host]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "channels",
    "django_celery_beat",
    "devices.apps.DevicesConfig",
    "live",
    "ptz",
    "detections.apps.DetectionsConfig",
    "notifications.apps.NotificationsConfig",
    "incidents.apps.IncidentsConfig",
    "operadores.apps.OperadoresConfig",
    "monitoring.apps.MonitoringConfig",
    "central.apps.CentralConfig",
    "central_node.apps.CentralNodeConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
CSRF_TRUSTED_ORIGINS = [
    origin for origin in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",") if origin
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "mediamtx_manager"),
        "USER": os.environ.get("POSTGRES_USER", "mediamtx"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
        "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
        "PORT": 5432,
    }
}

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [os.environ.get("REDIS_URL", "redis://localhost:6379/0")],
        },
    },
}

CELERY_BROKER_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_BEAT_SCHEDULE = {
    "orchestrate-cameras-every-5s": {
        "task": "devices.tasks.orchestrate_cameras",
        "schedule": 5.0,
    },
    "incident-manager-every-5s": {
        "task": "incidents.tasks.incident_manager",
        "schedule": 5.0,
    },
    "monitoring-system-every-30s": {
        "task": "monitoring.tasks.collect_system",
        "schedule": 30.0,
    },
    "monitoring-mediamtx-every-30s": {
        "task": "monitoring.tasks.collect_mediamtx",
        "schedule": 30.0,
    },
    "monitoring-snmp-every-60s": {
        "task": "monitoring.tasks.collect_snmp",
        "schedule": 60.0,
    },
    "monitoring-deepstream-every-30s": {
        "task": "monitoring.tasks.collect_deepstream",
        "schedule": 30.0,
    },
    "patrol-controller-every-10s": {
        "task": "devices.tasks.patrol_controller",
        "schedule": 10.0,
    },
}

if os.environ.get("CENTRAL_MODE", "") == "1":
    CELERY_BEAT_SCHEDULE["central-check-every-30s"] = {
        "task": "central.tasks.central_check",
        "schedule": 30.0,
    }

AUTH_PASSWORD_VALIDATORS = []

LANGUAGE_CODE = "es-cl"
TIME_ZONE = "America/Santiago"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": (
            "whitenoise.storage.CompressedStaticFilesStorage"
            if os.environ.get("CENTRAL_MODE", "") == "1"
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        ),
    },
}

MEDIAMTX_URL = os.environ.get("MEDIAMTX_URL", "http://mediamtx:8889")

LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/accounts/login/"

FACE_MATCH_COOLDOWN_SECONDS = 10
FACE_MATCH_COSINE_THRESHOLD = 0.35
FACE_QUALITY_MIN_SCORE = 1500
