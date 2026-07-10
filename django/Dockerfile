FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/static/vendor/tabler /app/static/vendor/tabler-icons/fonts /app/static/vendor/p5 \
  && curl -sfL -o /app/static/vendor/tabler/tabler.min.css \
       "https://cdn.jsdelivr.net/npm/@tabler/core@1.4.0/dist/css/tabler.min.css" \
  && curl -sfL -o /app/static/vendor/tabler/tabler.min.js \
       "https://cdn.jsdelivr.net/npm/@tabler/core@1.4.0/dist/js/tabler.min.js" \
  && curl -sfL -o /app/static/vendor/tabler-icons/tabler-icons.min.css \
       "https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@3.43.0/dist/tabler-icons.min.css" \
  && curl -sfL -o /app/static/vendor/tabler-icons/fonts/tabler-icons.woff2 \
       "https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@3.43.0/dist/fonts/tabler-icons.woff2" \
  && curl -sfL -o /app/static/vendor/tabler-icons/fonts/tabler-icons.woff \
       "https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@3.43.0/dist/fonts/tabler-icons.woff" \
  && curl -sfL -o /app/static/vendor/p5/p5.min.js \
       "https://cdn.jsdelivr.net/npm/p5@1.11.3/lib/p5.min.js" \
  && mkdir -p /app/media /app/static/img

RUN python manage.py collectstatic --noinput 2>&1 | grep -v "^$" || true

EXPOSE 8000 8001

COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "4"]
