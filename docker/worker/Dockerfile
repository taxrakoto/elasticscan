FROM zaproxy/zap-stable:2.16.1
WORKDIR /app
COPY --chown=zap:zap app app
# Bootstrap only: no queue consumption or scanner execution until worker tasks land.
USER zap
ENTRYPOINT ["python3", "-m", "app.bootstrap", "--role", "worker"]
