FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

ENV LIBRARY_DIR=/library \
    STATE_DIR=/data \
    PYTHONUNBUFFERED=1

# Not root. compose.yml overrides this with PUID:PGID, the owner of the host directories.
USER 1000:1000

HEALTHCHECK --interval=5m --timeout=10s --start-period=2m --retries=2 CMD ["shayantv-dl", "health"]

ENTRYPOINT ["shayantv-dl"]
CMD ["daemon"]
