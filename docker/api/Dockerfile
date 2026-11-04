FROM python:3.12.8-slim-bookworm
COPY --from=astral/uv:0.13.7 /uv /bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock .
RUN --mount=type=cache,target=/root/.cache/uv uv pip install --system --require-hashes -r requirements.lock
RUN useradd --uid 10001 --create-home app
COPY app app
COPY scripts scripts
COPY docker/keycloak/realm.json /app/docker/keycloak/realm.json
USER app
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
