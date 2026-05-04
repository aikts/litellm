# KTS overlay image.
#
# Takes the public overlay image (which already has our open-source code patches)
# and adds the private ap-litellm-modules package from the private PyPI index.
#
# Build args:
#   BASE_IMAGE                   - public overlay image (output of Dockerfile.public),
#                                  e.g. ghcr.io/aikts/litellm-database:v1.83.3-stable
#   EXTRA_INDEX_URL              - private PyPI index URL
#   AP_LITELLM_MODULES_VERSION   - pinned version of ap-litellm-modules to install

ARG BASE_IMAGE
FROM ${BASE_IMAGE}

USER root

ARG EXTRA_INDEX_URL
ARG AP_LITELLM_MODULES_VERSION=0.2.2
RUN pip install --no-cache-dir --extra-index-url "${EXTRA_INDEX_URL}" \
    "ap-litellm-modules==${AP_LITELLM_MODULES_VERSION}"
