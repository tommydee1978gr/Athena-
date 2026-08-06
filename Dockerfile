ARG CLIPROXY_IMAGE=eceasy/cli-proxy-api:latest
FROM ${CLIPROXY_IMAGE} AS cliproxy

FROM python:3.12-slim-bookworm

ARG VCS_REF=unknown
ARG BUILD_DATE=unknown
ARG SOURCE_URL=https://github.com/tommydee1978gr/Athena-
ARG CLIPROXY_IMAGE=eceasy/cli-proxy-api:latest
LABEL org.opencontainers.image.title="ATHENA" \
      org.opencontainers.image.description="Unified private family assistant for Unraid with router-for-me/CLIProxyAPI brain" \
      org.opencontainers.image.source="$SOURCE_URL" \
      org.opencontainers.image.revision="$VCS_REF" \
      org.opencontainers.image.created="$BUILD_DATE" \
      org.opencontainers.image.version="2.3.0-beta2-athena-brain-router" \
      io.athena.cliproxy.image-ref="$CLIPROXY_IMAGE"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    ATHENA_CONFIG_DIR=/config \
    ATHENA_MEDIA_DIR=/media \
    ATHENA_CLIPROXY_BASE_URL=http://127.0.0.1:8317 \
    HF_HOME=/config/models/huggingface \
    TORCH_HOME=/config/models/torch

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl ffmpeg libsndfile1 tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=cliproxy /CLIProxyAPI/CLIProxyAPI /usr/local/bin/CLIProxyAPI
COPY --from=cliproxy /CLIProxyAPI/config.example.yaml /opt/cliproxy/config.example.yaml

WORKDIR /opt/athena
COPY requirements.txt ./
RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install --index-url https://download.pytorch.org/whl/cpu torch==2.10.0 torchaudio==2.10.0 \
    && python -m pip install -r requirements.txt

COPY app ./app
COPY entrypoint.sh ./entrypoint.sh
RUN python -m compileall -q /opt/athena/app \
    && python -c "import fastapi,httpx,cryptography,argon2,PIL,numpy,faster_whisper,piper,speechbrain,torch,torchaudio,sentence_transformers" \
    && /usr/local/bin/CLIProxyAPI --help >/dev/null \
    && ffprobe -version >/dev/null \
    && chmod 0755 /opt/athena/entrypoint.sh /usr/local/bin/CLIProxyAPI \
    && mkdir -p /config /media /opt/cliproxy

EXPOSE 8000 8317 8085 1455 54545 51121 11451
VOLUME ["/config", "/media"]
HEALTHCHECK --interval=30s --timeout=10s --start-period=45s --retries=5 \
  CMD curl --fail --silent http://127.0.0.1:8000/health >/dev/null && python -c "import socket; s=socket.create_connection(('127.0.0.1',8317),3); s.close()" || exit 1
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/athena/entrypoint.sh"]
