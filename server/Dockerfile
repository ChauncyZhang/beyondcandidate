FROM python:3.12.11-slim@sha256:47ae396f09c1303b8653019811a8498470603d7ffefc29cb07c88f1f8cb3d19f AS runtime

ARG DEBIAN_MIRROR=http://deb.debian.org/debian
ARG DEBIAN_SECURITY_MIRROR=http://deb.debian.org/debian-security
ARG PYPI_INDEX_URL=https://pypi.org/simple

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /opt/beyondcandidate
RUN sed -i \
        -e "s|http://deb.debian.org/debian-security|${DEBIAN_SECURITY_MIRROR}|g" \
        -e "s|http://deb.debian.org/debian|${DEBIAN_MIRROR}|g" \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        fonts-noto-cjk \
        libharfbuzz-subset0 \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
    && rm -rf /var/lib/apt/lists/*
COPY server/requirements.txt /opt/beyondcandidate/server/requirements.txt
RUN pip install --no-cache-dir --index-url "${PYPI_INDEX_URL}" -r server/requirements.txt
COPY server /opt/beyondcandidate/server
USER 65532:65532
CMD ["uvicorn", "server.app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

FROM runtime AS test
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*
COPY server/requirements-dev.txt /opt/beyondcandidate/server/requirements-dev.txt
COPY deploy /opt/beyondcandidate/deploy
COPY THIRD_PARTY_NOTICES.md /opt/beyondcandidate/THIRD_PARTY_NOTICES.md
RUN pip install --no-cache-dir -r server/requirements-dev.txt
CMD ["python", "-m", "pytest", "server/tests", "-q"]
