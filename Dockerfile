FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

ARG FINCHVOX_GIT_REPO=https://github.com/yydrift-code/finchvox.git
ARG FINCHVOX_GIT_REF=dev

WORKDIR /src

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

RUN git clone "${FINCHVOX_GIT_REPO}" finchvox-src

WORKDIR /src/finchvox-src

RUN git checkout "${FINCHVOX_GIT_REF}" \
    && uv tool install .

ENV PATH="/root/.local/bin:${PATH}"

EXPOSE 4317 3000

CMD ["finchvox", "start", "--data-dir", "/root/.finchvox"]
