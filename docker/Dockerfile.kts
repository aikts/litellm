# KTS overlay image.
#
# Takes the upstream-published litellm-database image of a matching tag and layers:
#   1. Fork-only Python patches (copied directly from the source tree — see KTS
#      PATCHED FILES block below).
#   2. ap-litellm-modules from the private index.
#
# Build args:
#   BASE_IMAGE                   - upstream image, e.g. ghcr.io/berriai/litellm-database:v1.83.3-stable
#   EXTRA_INDEX_URL              - private PyPI index URL
#   AP_LITELLM_MODULES_VERSION   - pinned version of ap-litellm-modules to install
#
# To patch a new file:
#   1. Edit it in the source tree (litellm/<path>).
#   2. Add ONE line below the "KTS PATCHED FILES" marker that mirrors the same
#      relative path into /tmp/kts/. No second copy to keep in sync.

ARG BASE_IMAGE
FROM ${BASE_IMAGE}

USER root

# ---- KTS PATCHED FILES (add one COPY per patched file, keep the source path) ----
COPY litellm/cost_calculator.py /tmp/kts/litellm/cost_calculator.py
# ---- end patched files ----

RUN LITELLM_DIR="$(python -c 'import litellm, os; print(os.path.dirname(litellm.__file__))')" && \
    test -d "$LITELLM_DIR" && \
    cp -rv /tmp/kts/litellm/. "$LITELLM_DIR/" && \
    find "$LITELLM_DIR" -name '__pycache__' -type d -exec rm -rf {} + && \
    rm -rf /tmp/kts

ARG EXTRA_INDEX_URL
ARG AP_LITELLM_MODULES_VERSION=0.2.5
RUN pip install --no-cache-dir --extra-index-url "${EXTRA_INDEX_URL}" \
    "ap-litellm-modules==${AP_LITELLM_MODULES_VERSION}"
