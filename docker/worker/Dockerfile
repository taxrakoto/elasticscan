FROM zaproxy/zap-stable:2.16.1
COPY --from=astral/uv:0.13.7 /uv /bin/uv
USER root
WORKDIR /app
COPY requirements.lock /tmp/requirements.lock
RUN uv pip install --system --break-system-packages \
      --require-hashes -r /tmp/requirements.lock \
    && mkdir -p /zap/wrk \
    && chown zap:zap /zap/wrk

COPY --chown=zap:zap \
    app/__init__.py app/bootstrap.py \
    app/
COPY --chown=zap:zap \
    app/workers/__init__.py app/workers/zap.py \
    app/workers/
COPY --chown=zap:zap \
    app/scanners/__init__.py app/scanners/base.py \
    app/scanners/scope.py app/scanners/baseline.py \
    app/scanners/baseline_hooks.py \
    app/scanners/
COPY --chown=zap:zap \
    app/core/__init__.py app/core/target_policy.py \
    app/core/team.py app/core/auth.py \
    app/core/
COPY --chown=zap:zap \
    app/schemas/__init__.py app/schemas/submission.py \
    app/schemas/job_message.py \
    app/schemas/
COPY --chown=zap:zap \
    app/models/__init__.py app/models/job.py app/models/dispatch.py \
    app/models/
COPY --chown=zap:zap \
    app/db/__init__.py app/db/base.py app/db/session.py \
    app/db/
COPY --chown=zap:zap \
    app/repositories/__init__.py app/repositories/lifecycle.py \
    app/repositories/

# Actual SQS consumption remains a later integration.
USER zap
ENTRYPOINT ["python3", "-m", "app.bootstrap", "--role", "worker"]
