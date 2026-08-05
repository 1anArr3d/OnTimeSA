from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import bunching, catalog, reliability, vehicles
from app.config import settings

app = FastAPI(title="OnTimeSA API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_methods=["GET"],
    allow_headers=["*"],
)

app.include_router(catalog.router)
app.include_router(reliability.router)
app.include_router(bunching.router)
app.include_router(vehicles.router)


@app.get("/api/health")
def health():
    return {"status": "ok"}
