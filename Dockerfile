ARG PYTHON_VERSION=3.12

FROM python:$PYTHON_VERSION-slim AS build

ENV PYTHONUNBUFFERED=1

WORKDIR /code

# Pinned rather than "latest" so builds are reproducible and never drift
# below what this fork's features need: xray_hot_reload.py requires
# `xray api adu`/`rmu`, shipped starting v25.7.26. v26.5.9 is verified
# (manually, against a real binary) to work with hot reload, the
# readiness check, and the remote-update path.
ARG XRAY_VERSION=v26.5.9

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl unzip gcc python3-dev libpq-dev \
    && curl -L https://github.com/Gozargah/Marzban-scripts/raw/master/install_latest_xray.sh | bash -s -- "$XRAY_VERSION" \
    && rm -rf /var/lib/apt/lists/*

COPY ./requirements.txt /code/
RUN python3 -m pip install --upgrade pip setuptools \
    && pip install --no-cache-dir --upgrade -r /code/requirements.txt

FROM python:$PYTHON_VERSION-slim

# Baked into the VERSION file below so version_check.py's self-update
# check reports the actual released version, not whatever was committed
# in the repo at build time. Passed by CI as VERSION=<tag without the
# leading "v">; defaults to "dev" for local/manual builds.
ARG VERSION=dev

ENV PYTHON_LIB_PATH=/usr/local/lib/python${PYTHON_VERSION%.*}/site-packages
WORKDIR /code

# ipset + iptables/ip6tables are required by ip_block.py's temporary IP
# blocking (see docker-compose.yml for the NET_ADMIN/NET_RAW capabilities
# that let this container actually use them).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ipset iptables \
    && rm -rf /var/lib/apt/lists/*

RUN rm -rf $PYTHON_LIB_PATH/*

COPY --from=build $PYTHON_LIB_PATH $PYTHON_LIB_PATH
COPY --from=build /usr/local/bin /usr/local/bin
COPY --from=build /usr/local/share/xray /usr/local/share/xray

COPY . /code
RUN echo "${VERSION#v}" > /code/VERSION

CMD ["bash", "-c", "python main.py"]