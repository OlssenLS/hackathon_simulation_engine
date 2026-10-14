from contextlib import asynccontextmanager
from datetime import datetime, timezone
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.db import engine, Base
from app.schemas import HealthResponse, HealthSimulatorStatus
from app.routers import (
    auth,
    elders,
    vitals,
    alerts,
    sim,
    medications,
    appointments,
    reports,
    documents,
)
from app.ws import ws_router
from app.seed import seed_database_if_empty, backfill_downtime_gap_if_needed
from app.services.simulator import simulator
from app.services.med_scheduler import med_scheduler
from app.services.anomaly import anomaly_engine
from fastapi.staticfiles import StaticFiles
import os

# Ensure uploads directory exists
os.makedirs("uploads", exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Create tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # 2. Seed data if DB is empty, or backfill downtime gap
    await seed_database_if_empty()
    await backfill_downtime_gap_if_needed()

    # 3. Load or train IsolationForest & compute baseline
    await anomaly_engine.train_or_load_isolation_forest()
    await anomaly_engine.recompute_baselines()

    # 4. Start simulator
    await simulator.start()

    # 5. Start medication scheduler
    await med_scheduler.start()

    yield

    # Shutdown
    await med_scheduler.stop()
    await simulator.stop()
    await engine.dispose()


app = FastAPI(
    title="Elder Care Backend",
    description="Backend monitoring platform for elderly people and family caregivers",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS
origins = [origin.strip() for origin in settings.CORS_ORIGINS.split(",") if origin.strip()]
if not origins or "*" in origins:
    origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(auth.router)
app.include_router(auth.me_router)
app.include_router(elders.router)
app.include_router(vitals.router)
app.include_router(alerts.router)
app.include_router(sim.router)
app.include_router(medications.router)
app.include_router(appointments.router)
app.include_router(reports.router)
app.include_router(documents.router)
app.include_router(ws_router)

# Mount local uploads directory
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")


@app.get("/health", response_model=HealthResponse, tags=["health"])
async def health():
    return HealthResponse(
        status="ok",
        time=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        simulator=HealthSimulatorStatus(active_elders=simulator.get_active_elders_count()),
    )
