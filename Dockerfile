FROM node:22-bookworm-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392 AS frontend-build
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM ghcr.io/astral-sh/uv:0.11.31@sha256:ecd4de2f060c64bea0ff8ecb182ddf46ba3fcccdc8a60cfdbaf20d1a047d7437 AS uv-bin
FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258 AS backend-build
COPY --from=uv-bin /uv /usr/local/bin/uv
WORKDIR /app/backend
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY backend/ ./
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258 AS runtime
RUN groupadd --gid 10001 screening && useradd --uid 10001 --gid screening --create-home screening \
    && mkdir -p /data && chown screening:screening /data
WORKDIR /app/backend
COPY --from=backend-build /app/backend /app/backend
COPY --from=frontend-build /build/frontend/dist /app/frontend/dist
COPY fixtures/ /app/fixtures/
ENV PATH="/app/backend/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    SCREENING_TOOL_DATABASE_URL=sqlite:////data/screening.db \
    SCREENING_TOOL_FRONTEND_DIST=/app/frontend/dist
USER screening
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
