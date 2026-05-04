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
#                                  e.g. ghcr.io/aikts/litellm-database:v1.83.10-stable
#   EXTRA_INDEX_URL              - private PyPI index URL
#   AP_LITELLM_MODULES_VERSION   - pinned version of ap-litellm-modules to install

ARG BASE_IMAGE
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.10.9@sha256:10902f58a1606787602f303954cea099626a4adb02acbac4c69920fe9d278f82

FROM ${UV_IMAGE} AS uvbin

FROM ${BASE_IMAGE}

USER root

COPY --from=uvbin /uv /usr/local/bin/uv

ARG EXTRA_INDEX_URL
ARG AP_LITELLM_MODULES_VERSION=0.3.1
ARG UV_INDEX_STRATEGY=unsafe-best-match

RUN uv pip install --python /app/.venv/bin/python --no-cache --no-config \
        --extra-index-url "${EXTRA_INDEX_URL}" \
        "ap-litellm-modules==${AP_LITELLM_MODULES_VERSION}" && \
    rm /usr/local/bin/uv
