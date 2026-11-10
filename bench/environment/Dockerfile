# syntax=docker/dockerfile:1
# dev-agent-bench — base image: everything in one container (no compose).
# Target platform: linux/arm64 (Docker Desktop on Apple Silicon); amd64 also builds.

FROM ghcr.io/astral-sh/uv:0.12.22@sha256:f513a91fc62fe7c17567eee97230dd198e43edb8a9fbecca843714a4358fe1bc AS uv

FROM python:3.13-slim@sha256:bb2988715db2cf7ace7b53f38f3cffbef7c7046a656bee66245eb0ed386e2e81

ARG TARGETARCH
ARG PG_MAJOR=16
ARG RABBITMQ_VERSION=4.1.8
ARG RABBITMQ_DEB_SHA256=f3e05002f198463bc6e1f2b90677674fbe77d24c82cb00b2f371ef3bed42d6c5
ARG TOXIPROXY_VERSION=2.12.0
ARG TOXIPROXY_SERVER_SHA256_ARM64=53e770c1c3035b5a9f1bc629fce537db1f95f62b26f4ebe6e756afd701cf077c
ARG TOXIPROXY_CLI_SHA256_ARM64=4f2d894c624ee100b5d2df1cc7c6d3a1dbc7563f641bfd937db5de504f211afa
ARG TOXIPROXY_SERVER_SHA256_AMD64=556d891134a3c582dc1e1a3f7335fd55142e5965769855a00b944e13e48302fc
ARG TOXIPROXY_CLI_SHA256_AMD64=26c747024a3758a243c4449ff4f65634983fe98a0ab411d242cc9ff4561312b4

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1

# --- system packages -------------------------------------------------------
# git/make/curl: required by the brief. tini: PID 1 that reaps orphans left by
# the agent. procps: pkill/pgrep for the verifier. tmux + build-essential: what
# Harbor's OpenHands installer would otherwise apt-install at trial time.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl git make gnupg tini procps tmux build-essential \
 && rm -rf /var/lib/apt/lists/*

# --- PostgreSQL 16 (PGDG; Debian trixie ships 17) ---------------------------
RUN install -d /usr/share/postgresql-common/pgdg \
 && curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
      https://www.postgresql.org/media/keys/ACCC4CF8.asc \
 && . /etc/os-release \
 && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends postgresql-${PG_MAJOR} \
 && rm -rf /var/lib/apt/lists/*
COPY postgres-bench.conf /etc/postgresql/${PG_MAJOR}/main/conf.d/bench.conf

# --- RabbitMQ 4.1 (upstream .deb, Erlang from Debian) -----------------------
RUN curl -fsSL -o /tmp/rabbitmq-server.deb \
      "https://github.com/rabbitmq/rabbitmq-server/releases/download/v${RABBITMQ_VERSION}/rabbitmq-server_${RABBITMQ_VERSION}-1_all.deb" \
 && echo "${RABBITMQ_DEB_SHA256}  /tmp/rabbitmq-server.deb" | sha256sum -c - \
 && apt-get update \
 && apt-get install -y --no-install-recommends /tmp/rabbitmq-server.deb \
 && rm -rf /var/lib/apt/lists/* /tmp/rabbitmq-server.deb \
 && printf 'NODENAME=rabbit@localhost\n' > /etc/rabbitmq/rabbitmq-env.conf \
 && chown rabbitmq:rabbitmq /etc/rabbitmq/rabbitmq-env.conf

# --- toxiproxy (server + CLI) ----------------------------------------------
RUN set -eu; arch="${TARGETARCH:-arm64}"; \
    case "$arch" in \
      arm64) s="${TOXIPROXY_SERVER_SHA256_ARM64}"; c="${TOXIPROXY_CLI_SHA256_ARM64}" ;; \
      amd64) s="${TOXIPROXY_SERVER_SHA256_AMD64}"; c="${TOXIPROXY_CLI_SHA256_AMD64}" ;; \
      *) echo "unsupported arch: $arch" >&2; exit 1 ;; \
    esac; \
    base="https://github.com/Shopify/toxiproxy/releases/download/v${TOXIPROXY_VERSION}"; \
    curl -fsSL -o /usr/local/bin/toxiproxy-server "$base/toxiproxy-server-linux-$arch"; \
    curl -fsSL -o /usr/local/bin/toxiproxy-cli "$base/toxiproxy-cli-linux-$arch"; \
    echo "$s  /usr/local/bin/toxiproxy-server" | sha256sum -c -; \
    echo "$c  /usr/local/bin/toxiproxy-cli" | sha256sum -c -; \
    chmod 0755 /usr/local/bin/toxiproxy-server /usr/local/bin/toxiproxy-cli

# --- uv (pinned) ------------------------------------------------------------
COPY --from=uv /uv /uvx /usr/local/bin/

# Insurance against version drift if the agent adds a package.
ENV UV_EXCLUDE_NEWER=2026-10-01T00:00:00Z

# --- verifier toolchain: separate venv, not a dependency of /app ------------
COPY verifier/requirements.txt /opt/verifier-src/requirements.txt
RUN uv venv /opt/verifier --python /usr/local/bin/python3.13 \
 && uv pip install --no-cache --python /opt/verifier/bin/python -r /opt/verifier-src/requirements.txt
COPY verifier/plugins/ /opt/verifier/plugins/
COPY verifier/bench-snapshot verifier/bench-healthcheck /usr/local/bin/
RUN chmod 0755 /usr/local/bin/bench-snapshot /usr/local/bin/bench-healthcheck

# --- unprivileged user the evaluated agent runs as --------------------------
RUN useradd --create-home --uid 1000 --shell /bin/bash agent \
 && install -d -o agent -g agent /app \
 && install -d -m 0700 -o root -g root /var/lib/bench-verifier

# --- /app: pre-initialized project ------------------------------------------
COPY --chown=agent:agent app/ /app/
USER agent
WORKDIR /app
RUN uv sync --frozen \
 && git config --global user.name "dev" \
 && git config --global user.email "dev@localhost" \
 && git config --global init.defaultBranch main \
 && git init -q \
 && git add -A \
 && GIT_AUTHOR_DATE="2026-10-01T00:00:00Z" GIT_COMMITTER_DATE="2026-10-01T00:00:00Z" \
    git commit -q -m "Initial commit"
USER root

# Baseline for C7 (config hash), kept outside /app and not writable by the agent.
RUN install -d /opt/verifier/baseline \
 && cp /app/ruff.toml /app/mypy.ini /opt/verifier/baseline/ \
 && runuser -u agent -- git -C /app rev-parse HEAD > /opt/verifier/baseline/initial_commit \
 && (cd /opt/verifier/baseline && sha256sum ruff.toml mypy.ini > SHA256SUMS) \
 && chmod -R a+rX,go-w /opt/verifier

# Node for the opencode harness. Harbor's opencode installer runs
# `apt-get install nodejs npm` at agent setup of every trial unless `node` and
# `npm` are on PATH; a pinned Node here removes that step and its dependence on
# Debian mirrors. opencode itself runs on the Node that Harbor installs through
# nvm into the agent's home.
ARG NODE_VERSION=22.23.3
ARG NODE_SHA256_ARM64=5ced2d48d1d7198739b7f86804de0171aefb6823b684b12341d3321afc3cb0b2
ARG NODE_SHA256_AMD64=1084aa36196bba4c3a5e69a1ee388a6e4ff729dad09445fbcd434b28fe3c24af
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      arm64) node_arch=arm64; sha="$NODE_SHA256_ARM64" ;; \
      amd64) node_arch=x64; sha="$NODE_SHA256_AMD64" ;; \
      *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/node.tar.gz "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${node_arch}.tar.gz"; \
    echo "${sha}  /tmp/node.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/node.tar.gz -C /usr/local --strip-components=1 --no-same-owner; \
    rm /tmp/node.tar.gz; \
    node --version; npm --version

ENV DATABASE_URL=postgresql+asyncpg://tracksvc:tracksvc@127.0.0.1:5432/tracksvc \
    RABBITMQ_URL=amqp://tracksvc:tracksvc@127.0.0.1:5672/

COPY entrypoint.sh /entrypoint.sh
RUN chmod 0755 /entrypoint.sh

EXPOSE 8000
# tini reaps orphaned processes. entrypoint.sh starts the services and exits 0;
# when a command is given (Harbor passes `sh -c "sleep infinity"`), it execs it.
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
