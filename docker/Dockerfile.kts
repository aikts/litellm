# KTS overlay: installs ap-litellm-modules on top of the upstream litellm-database image.
#
# Build args:
#   BASE_IMAGE       - the litellm-database image built in a prior job
#   EXTRA_INDEX_URL  - pip index URL for private packages (ap-litellm-modules)

ARG BASE_IMAGE
FROM ${BASE_IMAGE}

USER root

ARG EXTRA_INDEX_URL
ARG AP_LITELLM_MODULES_VERSION=0.2.2
RUN pip install --no-cache-dir --extra-index-url "${EXTRA_INDEX_URL}" "ap-litellm-modules==${AP_LITELLM_MODULES_VERSION}"
