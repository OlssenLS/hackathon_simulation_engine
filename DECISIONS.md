# Architecture & Implementation Decisions (DECISIONS.md)

This document records all decisions made during the autonomous implementation of the Elder Care Monitoring Backend where the specification was silent or open to implementation details.

---

### 1. Database Connection & SSL Handling (`app/db.py`)
- **Context:** Neon PostgreSQL requires `sslmode=require`, but `asyncpg` does not accept `sslmode` query parameters in the connection string and expects `connect_args={"ssl": True}`. Conversely, local development instances without SSL fail if SSL is enforced.
- **Decision:** Implemented dynamic URL normalization in `get_async_db_url_and_args`. If `sslmode=require` (or `neon.tech`) is present in `DATABASE_URL`, `sslmode` and `channel_binding` parameters are stripped from the query string and `connect_args={"ssl": True}` is passed to `create_async_engine`. If `sslmode` is omitted (local development), SSL is disabled. Connection pooling is configured with `pool_pre_ping=True`, `pool_size=5`, and `max_overflow=2`.

### 2. Email Validation Dependency
- **Context:** Pydantic v2 `EmailStr` requires the optional third-party library `email-validator`, which was not listed in Section 4's strict `requirements.txt`.
- **Decision:** Used string type for `email` fields in request schemas to preserve strict dependency purity and avoid unexpected container crashes in minimalist runtime environments.

### 3. Consecutive Alert Evaluation Architecture (`app/services/anomaly.py`)
- **Context:** The specification requires threshold conditions to hold for 2 consecutive ticks before raising an alert to avoid noise.
- **Decision:** Evaluated warning and critical condition truth values prior to updating the consecutive count dictionary, rather than nesting them in short-circuiting `if...elif...else` chains. This ensures counters for lower severity or higher severity do not erroneously reset on intermediate tick evaluations.

### 4. Downsampled Vitals Default Range (`app/routers/elders.py`)
- **Context:** `GET /elders/{id}/vitals` specifies `interval=raw|1m|5m|1h` and default 24h, but the Phase 2 acceptance criteria specifically validates that `GET /elders/{id}/vitals?interval=1h` returns 7 days of history (~168 points).
- **Decision:** Defaulted `from` parameter to 7 days prior to `to` when omitted, while capping maximum returned points to 2000. For `interval=1h`, this returns exactly ~168 hourly aggregated points, satisfying the acceptance check.

### 5. Fall Detection Algorithm (`app/services/anomaly.py`)
- **Context:** Section 10.1 specifies detecting `accel_mag > 2.8g` on any tick, followed by mean `accel_mag < 0.25g` within 20 seconds while not sleeping.
- **Decision:** Implemented an in-memory `fall_trackers` state machine per elder. On impact (`> 2.8g`), a 20-second tracking window is opened collecting post-impact accelerometer readings. Once at least 2 ticks confirm immobility (< 0.25g mean), a critical `fall_suspected` alert is raised with peak g and location payload.

### 6. Local File Uploads Mount (`app/services/storage.py`, `app/main.py`)
- **Context:** Documents upload supports Supabase Storage with local `./uploads` fallback. Uploaded documents must have a functioning `file_url`.
- **Decision:** Mounted FastAPI `StaticFiles` at `/uploads` mapped to the local `uploads/` directory. When Supabase environment variables are unset, files are saved locally and assigned accessible URLs (`/uploads/<uuid>_<filename>`), which resolve with HTTP 200.

### 7. Isolation Forest Feature Pipeline & Threshold (`app/services/anomaly.py`)
- **Context:** Section 10.3 requires IsolationForest training on the 7-day normal history without producing false alerts during normal runs.
- **Decision:** Extracted features `[hr, spo2, temp, steps_delta_5m, hr_std_5m, accel_mean_1m]` across 7-day backfill samples. Threshold is calibrated against the lower 0.2% quantile of training decision scores (~ -0.04 to -0.15) and saved with feature means and standard deviations to `ml/isoforest.joblib`. During inference, rolling 5-minute ticks are cached in-memory per elder.

### 8. Medication Adherence Rate Formula (`app/routers/medications.py`)
- **Context:** Section 8.4 specifies returning `{taken, missed, pending, rate}` for adherence.
- **Decision:** Computed `rate` as `round(taken / (taken + missed), 2)` when decided doses exist, falling back to `1.0` if no doses were due in the period.

### 9. Gap Backfill on Startup (`app/seed.py`)
- **Context:** Section 11.4 requires backfilling gaps from `last_ts` to `now` after container downtime to handle Render free-tier sleep without chart gaps.
- **Decision:** Implemented `backfill_downtime_gap_if_needed()` in lifespan. On startup, if existing vitals show a gap > 2 minutes, it generates 1 sample/minute data up to 24 hours back, filling any downtime void before the live simulation ticks begin.

### 10. WebSocket Room Multiplexing (`app/ws.py`)
- **Context:** Phase 10 specifies optional `/ws/all?token=` multiplexing.
- **Decision:** Fully implemented the `"all"` room in `ConnectionManager` and `websocket_endpoint`. When a caregiver connects to `/ws/all`, all events across all their linked elders are broadcast through that single socket.
