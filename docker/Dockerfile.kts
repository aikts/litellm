# KTS overlay image.
#
# Takes the public overlay image (which already has our open-source code patches)
# and adds the private ap-litellm-modules package from the private PyPI index.
#
# Upstream switched to a Wolfi base with a pre-built /app/.venv and no pip in
# the runtime image — install via uv (copied from the official uv image)
# targeting that venv directly.
#
# Build args:
#   BASE_IMAGE                   - public overlay image (output of Dockerfile.public),
#                                  e.g. ghcr.io/aikts/litellm-database:v1.104.0
#   EXTRA_INDEX_URL              - private PyPI index URL
#   AP_LITELLM_MODULES_VERSION   - pinned version of ap-litellm-modules to install

ARG BASE_IMAGE
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.7

FROM ${UV_IMAGE} AS uvbin

FROM ${BASE_IMAGE}

USER root

COPY --from=uvbin /uv /usr/local/bin/uv

ARG EXTRA_INDEX_URL
ARG AP_LITELLM_MODULES_VERSION=0.11.0
ARG UV_INDEX_STRATEGY=unsafe-best-match

# ap-litellm-modules pins litellm itself. Requiring the litellm version already in the
# base image turns a drifted pin into a resolver error instead of uv silently replacing
# litellm with a clean PyPI build and dropping the overlay patches.
RUN /app/.venv/bin/python - <<'PYCA'
import hashlib
import ssl
import urllib.request
from pathlib import Path

with urllib.request.urlopen("https://storage.yandexcloud.net/cloud-certs/CA.pem", timeout=30) as response:
    certificate = response.read()
if hashlib.sha256(certificate).hexdigest() != "6d148f85b5213445b23ad22ff45e47e1aa2be968f183f9bd6ff39de54d47a8ef":
    raise SystemExit("Yandex root CA checksum mismatch")
bundle = Path("/etc/ssl/certs/ca-certificates.crt")
trusted_roots = Path(ssl.get_default_verify_paths().cafile).read_bytes()
bundle.parent.mkdir(parents=True, exist_ok=True)
bundle.write_bytes(trusted_roots + b"\n" + certificate)
PYCA

ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

RUN LITELLM_VERSION="$(/app/.venv/bin/python -c 'import importlib.metadata as m; print(m.version("litellm"))')" && \
    uv pip install --python /app/.venv/bin/python --no-cache --no-config \
        --extra-index-url "${EXTRA_INDEX_URL}" \
        "ap-litellm-modules==${AP_LITELLM_MODULES_VERSION}" "litellm==${LITELLM_VERSION}" && \
    rm /usr/local/bin/uv
