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
#                                  e.g. ghcr.io/aikts/litellm-database:v1.100.1
#   EXTRA_INDEX_URL              - private PyPI index URL
#   AP_LITELLM_MODULES_VERSION   - pinned version of ap-litellm-modules to install

ARG BASE_IMAGE
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.7

FROM ${UV_IMAGE} AS uvbin

FROM ${BASE_IMAGE}

USER root

COPY --from=uvbin /uv /usr/local/bin/uv

ARG EXTRA_INDEX_URL
ARG AP_LITELLM_MODULES_VERSION=0.8.0
ARG UV_INDEX_STRATEGY=unsafe-best-match

# ap-litellm-modules pins litellm itself. Requiring the litellm version already in the
# base image turns a drifted pin into a resolver error instead of uv silently replacing
# litellm with a clean PyPI build and dropping the overlay patches.
RUN apk add --no-cache curl ca-certificates && \
    mkdir -p /usr/local/share/ca-certificates/Yandex && \
    curl -fsSL "https://storage.yandexcloud.net/cloud-certs/CA.pem" \
        -o /usr/local/share/ca-certificates/Yandex/YandexInternalRootCA.crt && \
    chmod 0644 /usr/local/share/ca-certificates/Yandex/YandexInternalRootCA.crt && \
    update-ca-certificates && \
    LITELLM_VERSION="$(/app/.venv/bin/python -c 'import importlib.metadata as m; print(m.version("litellm"))')" && \
    uv pip install --python /app/.venv/bin/python --no-cache --no-config \
        --extra-index-url "${EXTRA_INDEX_URL}" \
        "ap-litellm-modules==${AP_LITELLM_MODULES_VERSION}" "litellm==${LITELLM_VERSION}" && \
    rm /usr/local/bin/uv
