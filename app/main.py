"""Local API bootstrap with configured authentication for future job routes."""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.auth import get_token_validator


@asynccontextmanager
async def lifespan(app):
    # Validate trust configuration at startup. Signing keys are fetched lazily.
    get_token_validator()
    yield


app = FastAPI(title="ElasticScan", version="0.0.1", description="Development bootstrap; scan operations are not implemented yet.", lifespan=lifespan)


@app.get('/health/live', tags=['health'])
def liveness():
    return {'status': 'ok', 'mode': 'bootstrap'}
