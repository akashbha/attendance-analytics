# Employee Attendance & Analytics API

FastAPI + MongoDB service for punch-in/out, manual corrections with an audit trail, and analytics built on aggregation pipelines. All application code is in `app/main.py`.

## Run
Requires Python 3.11+ and MongoDB 6.0+.

    pip install -r requirements.txt
    export MONGO_URI="mongodb://localhost:27017"
    export MONGO_DB="attendance_db"
    uvicorn app.main:app --port 8000

On Windows use `set` (cmd) or `$env:` (PowerShell) instead of `export`. A local `.env` file also works, and real environment variables take priority over it. Indexes are created at startup, and `GET /health` returns 200 once MongoDB answers.

## Notes
- `/admin/explain/{endpoint}` returns MongoDB `executionStats` for the five endpoints listed in the contract. I checked on about 92k generated records that all five plans use an index scan and no collection scan.
- Trend `moving_avg_7d` averages the rounded 4-decimal rates, and a range of up to 92 days (inclusive) is accepted.

## Missing
Nothing from the assignment is missing.