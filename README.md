# Elder Care Monitoring: Backend Engine

A backend platform for elderly monitoring (**lansia**) and family caregivers built with **FastAPI**, **PostgreSQL (Neon / asyncpg)**, **SQLAlchemy 2.0 (Async)**, **WebSockets**, and a three-layer **Anomaly Detection Engine** (Threshold Rules, Circadian Z-score Baselines, and scikit-learn Isolation Forest).

Includes a realistic **Simulator Engine** emitting continuous vitals and GPS coordinates every 3 seconds, automatic seed data generation, 7-day historical backfill, downtime gap recovery, and AI-powered **Smart Health Reports** in Bahasa Indonesia.

---

## 1. Stack & Architecture

- **Web Framework:** FastAPI + Uvicorn (Single worker architecture)
- **Database & ORM:** PostgreSQL (Neon / local) via SQLAlchemy 2.0 async + `asyncpg`
- **Real-Time Streaming:** Native FastAPI WebSockets with room broadcasting and multiplexing (`/ws/{elder_id}` and `/ws/all`)
- **Machine Learning:** `scikit-learn` IsolationForest (`ml/isoforest.joblib`), `numpy`, `joblib`
- **LLM Engine:** Provider-agnostic REST client for **Gemini API** / **Groq** with automatic deterministic template fallback
- **File Storage:** Local `./uploads` static file server with automatic Supabase Storage REST support
- **Timezone:** `Asia/Jakarta` (UTC+7) for all local circadian schedules and medication routines

---

## 2. Quickstart: Local Development

### Prerequisites
- Python 3.11+
- PostgreSQL 14+ (Local PostgreSQL or free Neon database)

### Installation

1. **Clone repository & setup virtual environment:**
   ```bash
   python -m venv .venv
   # Windows:
   .\.venv\Scripts\activate
   # Linux/macOS:
   source .venv/bin/activate
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure Environment:**
   Create `.env` from `.env.example`:
   ```bash
   cp .env.example .env
   ```
   For local PostgreSQL:
   ```env
   DATABASE_URL=postgresql://postgres:password@localhost:5432/eldercare
   JWT_SECRET=super-secret-key-1234
   TZ_LOCAL=Asia/Jakarta
   SIM_TICK_SECONDS=3
   ```

4. **Start the Backend:**
   ```bash
   uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
   ```

5. **Access Endpoints:**
   - Interactive Swagger API Documentation: [http://localhost:8000/docs](http://localhost:8000/docs)
   - Alternative ReDoc: [http://localhost:8000/redoc](http://localhost:8000/redoc)
   - Health check: [http://localhost:8000/health](http://localhost:8000/health)

On first startup, the application automatically:
- Creates all database tables (`Base.metadata.create_all`).
- Seeds demo accounts (Caregiver `caregiver@demo.id`, Pak Budi `budi@demo.id`, Bu Sari `sari@demo.id`) with password `demo1234`.
- Generates 7 days of 1-sample/minute realistic history for both demo elders.
- Trains or loads the Isolation Forest model into `ml/isoforest.joblib`.
- Computes circadian baselines per hour and activity.
- Starts the 3-second simulation loop and medication scheduler in the background.

---

## 3. Demo Accounts & Credentials

Password for all seeded accounts: `demo1234`

| Role | Name | Email | Details |
|---|---|---|---|
| **Caregiver** | Rina (anak) | `caregiver@demo.id` | Phone `+6281200000001`, linked to Pak Budi & Bu Sari |
| **Lansia** | Pak Budi (72 th) | `budi@demo.id` | Kondisi: Hipertensi, Kode tautan: `BUDI23`, Bekas Area |
| **Lansia** | Bu Sari (68 th) | `sari@demo.id` | Kondisi: Diabetes tipe 2, Kode tautan: `SARI45` |

---

## 4. Live Demo Presentation Script (Section 18)

To conduct a live demonstration on stage or for hackathon judges:

1. **Dashboard Baseline:**
   - Log in as `caregiver@demo.id`.
   - Open Pak Budi's dashboard: observe real-time vitals ticking every 3s over WebSocket (`hr`, `spo2`, `steps`, `temp`, `resp_rate`).
   - Cards show latest Blood Pressure (~138/86 mmHg) and Glucose, map displays Pak Budi at home inside the 150m geofence.

2. **Trigger Hypoxia Incident:**
   ```bash
   curl -X POST http://localhost:8000/sim/{budi_id}/trigger \
     -H "Authorization: Bearer <TOKEN>" \
     -H "Content-Type: application/json" \
     -d '{"scenario": "hypoxia", "duration_s": 25}'
   ```
   - SpO2 drops down to 87%.
   - Within 6 seconds (2 consecutive ticks), a critical `spo2_low` alert arrives over WebSocket: *"Kadar oksigen sangat rendah (SpO2 87.0%). Segera berikan bantuan!"*.

3. **Trigger Fall Incident (Auto-SOS):**
   ```bash
   curl -X POST http://localhost:8000/sim/{budi_id}/trigger \
     -H "Authorization: Bearer <TOKEN>" \
     -H "Content-Type: application/json" \
     -d '{"scenario": "fall", "duration_s": 30}'
   ```
   - Impact spike occurs (`accel_mag = 3.4g`), followed by immobility (`accel_mag < 0.25g`) and elevated HR.
   - Within 15 seconds, a critical `fall_suspected` alert with `source="sos"` is triggered: *"Kemungkinan jatuh terdeteksi. Segera periksa kondisi Pak Budi."*.

4. **Trigger Wandering Incident (Geofence Breach):**
   ```bash
   curl -X POST http://localhost:8000/sim/{budi_id}/trigger \
     -H "Authorization: Bearer <TOKEN>" \
     -H "Content-Type: application/json" \
     -d '{"scenario": "wander", "duration_s": 30}'
   ```
   - GPS drifts steadily away from home. Once past 150m, a `geofence_exit` warning alert is broadcast over WebSocket.

5. **Trigger Hypertensive Spike:**
   ```bash
   curl -X POST http://localhost:8000/sim/{budi_id}/trigger \
     -H "Authorization: Bearer <TOKEN>" \
     -H "Content-Type: application/json" \
     -d '{"scenario": "hypertensive", "duration_s": 15}'
   ```
   - A blood pressure reading of `192/124 mmHg` is inserted; critical `bp_high` alert pops up immediately.

6. **Medication Schedule & Adherence:**
   - Inspect today's medication logs via `GET /elders/{id}/med-logs`.
   - Confirm a dose via `POST /med-logs/{log_id}/confirm`.
   - Inspect 7-day adherence stats via `GET /elders/{id}/medications/adherence?days=7`.

7. **Smart Health Report Generation:**
   ```bash
   curl -X POST http://localhost:8000/elders/{budi_id}/reports/generate?days=7 \
     -H "Authorization: Bearer <TOKEN>"
   ```
   - Produces a comprehensive Markdown report in Bahasa Indonesia with 7 structured sections, aggregated statistics, and mandatory medical disclaimers in under 1 second (via template fallback or Gemini LLM).

8. **Reset to Normal:**
   ```bash
   curl -X POST http://localhost:8000/sim/{budi_id}/trigger \
     -H "Authorization: Bearer <TOKEN>" \
     -H "Content-Type: application/json" \
     -d '{"scenario": "normal", "duration_s": 1}'
   ```

---

## 5. Deployment: Render + Neon (Free Tier)

### 1. Database (Neon)
1. Sign up at [neon.tech](https://neon.tech) and create a free PostgreSQL project.
2. Copy the connection string: `postgresql://user:password@ep-xyz.ap-southeast-1.aws.neon.tech/eldercare?sslmode=require`.

### 2. Web Service (Render)
1. Sign up at [render.com](https://render.com).
2. Connect your repository and select **Blueprint** deployment (reads `render.yaml`) or create a **Web Service** with Docker runtime.
3. Configure Environment Variables:
   - `DATABASE_URL`: Your Neon connection string
   - `JWT_SECRET`: Random 32+ character string
   - `LLM_PROVIDER`: `gemini` (or `groq`)
   - `LLM_API_KEY`: Your free Gemini API key (optional; template works without it)
   - `LLM_MODEL`: `gemini-1.5-flash`

### 3. Keep-Alive Pinger (UptimeRobot)
1. Create a free account at [uptimerobot.com](https://uptimerobot.com).
2. Add a new **HTTP(s) Monitor**:
   - URL: `https://your-service.onrender.com/health`
   - Monitoring Interval: Every 5 minutes
3. This prevents Render's free tier from spinning down during the hackathon judging period.
4. If the service does experience cold restart, `backfill_downtime_gap_if_needed()` automatically fills the data gap on boot up to 24 hours back.

---

## 6. WebSocket Protocol Contract

- **Endpoint:** `ws://<host>/ws/{elder_id}?token=<jwt>` (or `ws://<host>/ws/all?token=<jwt>` for multiplexed monitoring).
- **Reject Code:** 4401 (Invalid token), 4403 (Forbidden access).
- **Ping / Pong:** Client sends `{"type": "ping"}`; server replies `{"type": "pong"}`. Server sends a keep-alive ping every 25 seconds.

### Envelope Shape
```json
{
  "type": "vitals" | "location" | "measurement" | "alert" | "med" | "scenario",
  "elder_id": "UUID",
  "ts": "2026-10-14T15:30:00Z",
  "data": { ... }
}
```

---

## 7. Anomaly Detection Architecture

The backend executes a 3-tier anomaly detection pipeline:

1. **Layer 1: Threshold Rules (Deterministic & Fast)**
   - Vitals: SpO2, HR, Skin Temperature, Respiratory Rate evaluated against medical threshold standards. Condition must hold for 2 consecutive ticks to eliminate sensor noise.
   - Fall Detection: Impact peak (`accel_mag > 2.8g`) + subsequent immobility (< 0.25g) triggers automatic emergency SOS within 20s.
   - Geofence: Distance from home > `geofence_m` triggers warning; > `3x geofence_m` triggers critical alert.
   - Inactivity: Resting state during waking hours (07:00 - 21:00) exceeding `INACTIVITY_MIN`.

2. **Layer 2: Circadian Baseline (Z-Score)**
   - Groups 7-day historical readings by `(elder_id, hour_of_day, activity_state)`.
   - Computes personal baseline mean and standard deviation for each slot.
   - Triggers `anomaly_baseline` warning with explainable natural language message when `|z| > 3` for 3 consecutive ticks.

3. **Layer 3: Isolation Forest (Multivariate Pattern Detection)**
   - Unsupervised multivariate anomaly model trained on 6 continuous features (`[hr, spo2, temp, steps_delta_5m, hr_std_5m, accel_mean_1m]`).
   - Evaluates multi-dimensional decision scores every tick.
   - Identifies non-linear correlated anomalies with explainable top deviating features (`top_features`).
