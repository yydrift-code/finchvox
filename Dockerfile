FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

WORKDIR /src/finchvox-src

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/ ./src/
COPY ui/ ./ui/

RUN uv tool install .

ENV PATH="/root/.local/bin:${PATH}"

EXPOSE 4317 3000

CMD ["finchvox", "start", "--data-dir", "/root/.finchvox"]
