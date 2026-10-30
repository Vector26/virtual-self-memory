FROM python:3.12-slim-bookworm

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl rclone \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system vsm && useradd --system --gid vsm --home /app vsm

COPY pyproject.toml README.md ./
COPY src ./src
COPY migrations ./migrations
COPY scripts/archive-flow-watchdog.sh ./scripts/archive-flow-watchdog.sh

RUN pip install --no-cache-dir -e ".[dev]" \
    && mkdir -p /app/.state && chown -R vsm:vsm /app

ENV VSM_API_HOST=0.0.0.0
ENV VSM_API_PORT=8090

USER vsm
EXPOSE 8090
CMD ["virtual-self", "serve"]
