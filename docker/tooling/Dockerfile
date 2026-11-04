FROM python:3.12.8-slim-bookworm
COPY --from=astral/uv:0.13.7 /uv /bin/uv
WORKDIR /work
ENTRYPOINT ["uv"]
