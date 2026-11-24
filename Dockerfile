# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 bot

COPY requirements.txt ./
RUN pip install --no-cache-dir --requirement requirements.txt

COPY --chown=bot:bot . .
RUN mkdir -p /app/data && chown bot:bot /app /app/data

COPY --chmod=0755 <<'EOF' /usr/local/bin/session-shop-entrypoint
#!/bin/sh
set -eu

# A mounted data volume can replace the directory and ownership from the image.
if [ "$(id -u)" -eq 0 ]; then
    chown -hR bot:bot /app/data
    exec gosu bot "$@"
fi
exec "$@"
EOF

ENTRYPOINT ["/usr/local/bin/session-shop-entrypoint"]
CMD ["python", "app.py"]
