"""ElasticScan API with authenticated durable scan submission."""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes.scans import router as scans_router
from app.core.auth import get_token_validator
from app.core.team import get_team_settings
from app.core.target_policy import get_target_policy
from app.db.session import make_engine, make_session_factory


@asynccontextmanager
async def lifespan(app):
    # Validate trust configuration at startup. Signing keys are fetched lazily.
    get_token_validator()
    get_team_settings()
    get_target_policy()
    engine = make_engine()
    app.state.sessions = make_session_factory(engine)
    try:
        yield
    finally:
        engine.dispose()


app = FastAPI(
    title="ElasticScan",
    version="0.0.1",
    description="Authenticated durable scan submission; execution is pending.",
    lifespan=lifespan,
)
app.include_router(scans_router)


@app.get('/health/live', tags=['health'])
def liveness():
    return {'status': 'ok', 'mode': 'bootstrap'}
