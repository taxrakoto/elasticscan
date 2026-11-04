"""Local API bootstrap. Scan routes and JWT enforcement arrive in their stories."""
from fastapi import FastAPI

app = FastAPI(title="ElasticScan", version="0.0.1", description="Development bootstrap; scan operations are not implemented yet.")


@app.get('/health/live', tags=['health'])
def liveness():
    return {'status': 'ok', 'mode': 'bootstrap'}
