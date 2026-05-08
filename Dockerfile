# praxdaily — single-process container.
#
# Layout:
#   /app           — source (mounted or COPY'd)
#   /workspace     — runtime data dir (.prax/ lives here, mount as volume)
#
# Why a multi-stage build: keeps the runtime image small (only python +
# the praxdaily install), no apt cache, no build toolchain.

# ─────────────────────────────────────────────────────────────────────
# Stage 1: build wheels
# ─────────────────────────────────────────────────────────────────────
FROM python:3.13-slim AS build

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src

# Build praxdaily as a wheel, no extras (praxagent is npm-only — runtime
# import is conditional, see bridge/send.py).
RUN pip install --no-cache-dir --upgrade pip build && \
    pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .

# ─────────────────────────────────────────────────────────────────────
# Stage 2: runtime
# ─────────────────────────────────────────────────────────────────────
FROM python:3.13-slim AS runtime

# fastapi + uvicorn + pyyaml + httpx are runtime hard deps; sqlite3
# ships with Python; everything else is optional.
RUN pip install --no-cache-dir \
    "fastapi>=0.110" "uvicorn>=0.27" "pyyaml>=6.0" "httpx>=0.27"

# Install our wheel.
COPY --from=build /wheels/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl

# Workspace dir (mount your .prax/ as /workspace/.prax via -v).
WORKDIR /workspace
RUN mkdir -p /workspace/.prax

EXPOSE 7878

# Default: serve the dashboard on 0.0.0.0:7878. Override via CMD if you
# want the one-shot pipeline (`praxdaily run-now`) instead.
CMD ["python3", "-m", "praxdaily", "serve", \
     "--host", "0.0.0.0", "--port", "7878", \
     "--no-open", "--cwd", "/workspace"]
