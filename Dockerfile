# syntax=docker/dockerfile:1
# Multi-arch (amd64 + arm64): corre igual en Oracle A1.Flex (ARM) que en x86.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY app ./app
RUN uv pip install --system --no-cache .

# Estado mutable fuera de la imagen: SQLite + DuckDB + logs en /app/data
RUN useradd -r -u 10001 engine \
    && mkdir -p /app/data \
    && chown -R engine:engine /app/data
USER engine
VOLUME ["/app/data"]

# El engine escribe engine.log cada minuto (health): si el fichero envejece
# mas de 3 min, el contenedor no esta sano.
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
  CMD python -c "import os,time,sys; sys.exit(0 if os.path.exists('engine.log') and time.time()-os.path.getmtime('engine.log')<180 else 1)"

CMD ["python", "-m", "app.main"]
