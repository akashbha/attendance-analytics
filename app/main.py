"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Any, Dict, Optional, Tuple, Union

import bson.json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

logger = logging.getLogger("app")

IST = timezone(timedelta(hours=5, minutes=30))

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=2000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]


class Status(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class PresenceStatus(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class ExplainEndpoint(str, Enum):
    attendance_list = "attendance_list"
    employee_monthly = "employee_monthly"
    department_summary = "department_summary"
    late_leaderboard = "late_leaderboard"
    department_trend = "department_trend"


def init_indexes():
    """Create indexes idempotently at startup. Logs any failure."""
    indexes_to_create = [
        (db.employees, [("emp_code", 1)], {"unique": True}),
        (db.employees, [("department", 1), ("emp_code", 1)], {}),
        (db.employees, [("department", 1), ("joined_on", 1)], {}),
        (db.employees, [("joined_on", 1)], {}),
        (db.attendance_logs, [("emp_code", 1), ("date", 1)], {"unique": True}),
        (db.attendance_logs, [("date", -1), ("emp_code", 1)], {}),
    ]

    for coll, keys, kwargs in indexes_to_create:
        try:
            coll.create_index(keys, **kwargs)
        except Exception as e:
            logger.error("Failed to create index %s on %s: %s", keys, coll.name, e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
DATE_REGEX = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")


def parse_date(value: str) -> str:
    """
    Validates that a string strictly matches YYYY-MM-DD (zero-padded)
    and represents a real calendar date. Raises HTTPException(422) otherwise.
    """
    if not DATE_REGEX.match(value):
        raise HTTPException(status_code=422, detail="Invalid date format, expected YYYY-MM-DD")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid calendar date")
    return value


MONTH_REGEX = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def parse_month(value: str) -> str:
    """
    Validates that a string strictly matches YYYY-MM (zero-padded).
    Raises HTTPException(422) otherwise.
    """
    if not MONTH_REGEX.match(value):
        raise HTTPException(status_code=422, detail="Invalid month format, expected YYYY-MM")
    return value


def compute_working_days(month_str: str, joined_on_str: str) -> int:
    """
    Calculates the number of working days (Mon-Fri) in the given month,
    starting from max(first day of month, joined_on). Returns 0 if joined after the month.
    """
    y, m = map(int, month_str.split("-"))
    first_day = datetime(y, m, 1).date()
    if m == 12:
        last_day = (datetime(y + 1, 1, 1) - timedelta(days=1)).date()
    else:
        last_day = (datetime(y, m + 1, 1) - timedelta(days=1)).date()

    joined_on = datetime.strptime(joined_on_str, "%Y-%m-%d").date()
    start_date = max(first_day, joined_on)

    if start_date > last_day:
        return 0

    working_days = 0
    curr = start_date
    while curr <= last_day:
        if curr.weekday() < 5:  # Mon=0, Tue=1, Wed=2, Thu=3, Fri=4
            working_days += 1
        curr += timedelta(days=1)
    return working_days


def build_employee_monthly_pipeline(emp_code: str, month: str) -> list:
    """
    Builds the MongoDB aggregation pipeline for monthly employee attendance summary.
    Reused by the /admin/explain endpoint.
    """
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": f"{month}-01", "$lte": f"{month}-31"},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$in": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$date"
                                                    }
                                                }
                                            },
                                            [2, 3, 4, 5, 6],
                                        ]
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    {"$ifNull": ["$half_day", False]},
                                    0.5,
                                    1.0,
                                ]
                            },
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]


def build_department_summary_pipeline(
    month: str, department: Optional[str] = None
) -> list:
    """
    Builds the aggregation pipeline for monthly department summary per openapi.yaml.
    Starts from employees to include zero-log employees (R9).
    Uses $lookup on attendance_logs to compute per-employee metrics without unwinding.
    Reused by the /admin/explain endpoint.
    """
    y, m = map(int, month.split("-"))
    if m == 12:
        last_date = datetime(y + 1, 1, 1).date() - timedelta(days=1)
    else:
        last_date = datetime(y, m + 1, 1).date() - timedelta(days=1)
    last_day_str = last_date.isoformat()

    match_filter: Dict[str, Any] = {"joined_on": {"$lte": last_day_str}}
    if department is not None:
        match_filter["department"] = department

    return [
        {"$match": match_filter},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", f"{month}-01"]},
                                    {"$lte": ["$date", last_day_str]},
                                ]
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_days": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$status",
                                                        ["PRESENT", "WFH", "ON_DUTY"],
                                                    ]
                                                },
                                                {
                                                    "$in": [
                                                        {
                                                            "$dayOfWeek": {
                                                                "$dateFromString": {
                                                                    "dateString": "$date"
                                                                }
                                                            }
                                                        },
                                                        [2, 3, 4, 5, 6],
                                                    ]
                                                },
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {"$ifNull": ["$half_day", False]},
                                                0.5,
                                                1.0,
                                            ]
                                        },
                                        0.0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "total_late_minutes": {
                                "$sum": {"$ifNull": ["$late_minutes", 0]}
                            },
                            "leave_count": {
                                "$sum": {
                                    "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                                }
                            },
                            "on_duty_count": {
                                "$sum": {
                                    "$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]
                                }
                            },
                            "work_hours_sum": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$status",
                                                        ["PRESENT", "WFH", "ON_DUTY"],
                                                    ]
                                                },
                                                {"$ne": ["$work_hours", None]},
                                            ]
                                        },
                                        "$work_hours",
                                        0.0,
                                    ]
                                }
                            },
                            "work_hours_n": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$status",
                                                        ["PRESENT", "WFH", "ON_DUTY"],
                                                    ]
                                                },
                                                {"$ne": ["$work_hours", None]},
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "stats",
            }
        },
        {"$addFields": {"stat": {"$arrayElemAt": ["$stats", 0]}}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": {"$ifNull": ["$stat.present_days", 0.0]}},
                "late_count": {"$sum": {"$ifNull": ["$stat.late_count", 0]}},
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$stat.total_late_minutes", 0]}
                },
                "leave_count": {"$sum": {"$ifNull": ["$stat.leave_count", 0]}},
                "on_duty_count": {"$sum": {"$ifNull": ["$stat.on_duty_count", 0]}},
                "total_work_hours": {
                    "$sum": {"$ifNull": ["$stat.work_hours_sum", 0.0]}
                },
                "total_work_hours_n": {
                    "$sum": {"$ifNull": ["$stat.work_hours_n", 0]}
                },
            }
        },
        {"$sort": {"_id": 1}},
    ]


def build_late_leaderboard_pipeline(
    month: str, limit: int = 10, department: Optional[str] = None
) -> list:
    """
    Builds the aggregation pipeline for late leaderboard per openapi.yaml.
    Matches attendance_logs with late_minutes > 0 in month, groups by emp_code,
    looks up employees (dropping nonexistent ones), filters by department if provided,
    ranks with $setWindowFields and $rank, filters rank <= limit, and sorts.
    Reused by the /admin/explain endpoint.
    """
    y, m = map(int, month.split("-"))
    if m == 12:
        last_date = datetime(y + 1, 1, 1).date() - timedelta(days=1)
    else:
        last_date = datetime(y, m + 1, 1).date() - timedelta(days=1)
    last_day_str = last_date.isoformat()

    pipeline: list = [
        {
            "$match": {
                "date": {"$gte": f"{month}-01", "$lte": last_day_str},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])

    return pipeline


def build_department_trend_pipeline(
    department: str,
    from_date: str,
    to_date: str,
    employees: Optional[list] = None,
) -> list:
    """
    Builds the MongoDB aggregation pipeline for daily department trend per openapi.yaml.
    Matches attendance_logs for department employees between from_date and to_date.
    Densifies date gaps to ensure exactly one row per calendar day.
    Computes is_working_day, headcount, attendance_rate, and moving_avg_7d.
    Reused by the /admin/explain endpoint.
    """
    if employees is None:
        employees = list(
            db.employees.find(
                {"department": department},
                {"_id": 0, "emp_code": 1, "joined_on": 1},
            )
        )

    emp_codes = [e["emp_code"] for e in employees]
    joined_on_list = [e["joined_on"] for e in employees]

    from_dt = datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    to_plus_one_dt = (datetime.strptime(to_date, "%Y-%m-%d") + timedelta(days=1)).replace(
        tzinfo=timezone.utc
    )

    return [
        {
            "$match": {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": from_date, "$lte": to_date},
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "date",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_plus_one_dt],
                },
            }
        },
        {
            "$addFields": {
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "date_str": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}},
                "is_working_day": {"$in": [{"$dayOfWeek": "$date"}, [2, 3, 4, 5, 6]]},
            }
        },
        {
            "$addFields": {
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": joined_on_list,
                            "as": "j",
                            "cond": {"$lte": ["$$j", "$date_str"]},
                        }
                    }
                }
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    {"$divide": ["$present_count", "$headcount"]},
                                                    10000,
                                                ]
                                            },
                                            0.5,
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "raw_moving_avg": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$addFields": {
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$raw_moving_avg", None]},
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {"$multiply": ["$raw_moving_avg", 10000]},
                                            0.5,
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$date_str",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]





def ms_to_dt(ms: int) -> datetime:
    """
    Convert integer epoch milliseconds to UTC datetime.
    Truncates sub-second milliseconds to whole seconds (Rule R1).
    """
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """
    Convert datetime to integer epoch milliseconds.
    Returns None if input is None.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp()) * 1000


def shift_window(
    punch_in: datetime, shift_start: str, shift_end: str
) -> Tuple[str, datetime, datetime]:
    """
    Given a punch_in instant and shift times ('HH:MM' in IST),
    returns (attendance_date_str, shift_start_dt, shift_end_dt) all aligned in IST.
    Handles overnight shifts per rule R1.
    """
    pin_ist = punch_in.astimezone(IST) if punch_in.tzinfo else punch_in.replace(tzinfo=timezone.utc).astimezone(IST)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))

    is_overnight = (eh, em) <= (sh, sm)

    if is_overnight and (pin_ist.hour, pin_ist.minute) < (eh, em):
        # Morning punch-in before shift_end belongs to the shift starting the previous day (R1)
        shift_date = pin_ist.date() - timedelta(days=1)
    else:
        shift_date = pin_ist.date()

    start_dt = datetime(shift_date.year, shift_date.month, shift_date.day, sh, sm, tzinfo=IST)
    if is_overnight:
        end_dt = datetime(shift_date.year, shift_date.month, shift_date.day, eh, em, tzinfo=IST) + timedelta(days=1)
    else:
        end_dt = datetime(shift_date.year, shift_date.month, shift_date.day, eh, em, tzinfo=IST)

    return shift_date.isoformat(), start_dt, end_dt


def compute_late_minutes(punch_in: datetime, start: Union[datetime, str]) -> int:
    """
    Computes late minutes per rule R2:
    Grace period is 10 min. Late only if punch_in is strictly > 10:00 (600s) after shift_start.
    When late, late_minutes = floor whole minutes from shift_start. Otherwise 0.
    """
    if isinstance(start, str):
        _, start_dt, _ = shift_window(punch_in, start, "18:30")
    else:
        start_dt = start
    diff_sec = int((punch_in - start_dt).total_seconds())
    if diff_sec <= 10 * 60:
        return 0
    return diff_sec // 60


def compute_overtime(punch_out: datetime, end_dt: datetime) -> int:
    """
    Computes overtime minutes per rule R3:
    Floor whole minutes between shift_end datetime and punch_out, only if >= 30 min. Otherwise 0.
    """
    diff_sec = int((punch_out - end_dt).total_seconds())
    minutes = diff_sec // 60
    return minutes if minutes >= 30 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """
    Computes work hours per rule R4:
    (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    sec = int((punch_out - punch_in).total_seconds())
    hours = Decimal(sec) / Decimal(3600)
    rounded = hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(rounded)


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    Computes half_day per rule R5:
    True when rounded work_hours < 4.50. Otherwise False.
    """
    if work_hours is None:
        return False
    return work_hours < 4.50


def record_out(doc: Dict[str, Any]) -> Dict[str, Any]:
    """
    Prepares an attendance document for API response:
    - Removes internal MongoDB '_id' and 'id'
    - Converts datetime fields to epoch milliseconds (punch_in, punch_out)
    - Converts datetime fields inside history entries and history changes to epoch ms
    - Defaults missing half_day to False, late_minutes/overtime_minutes to 0, history to []
    """
    res = dict(doc)
    res.pop("_id", None)
    res.pop("id", None)

    if "punch_in" in res and isinstance(res["punch_in"], datetime):
        res["punch_in"] = dt_to_ms(res["punch_in"])
    if "punch_out" in res and isinstance(res["punch_out"], datetime):
        res["punch_out"] = dt_to_ms(res["punch_out"])

    res.setdefault("half_day", False)
    res.setdefault("late_minutes", 0)
    res.setdefault("overtime_minutes", 0)

    history = res.get("history") or []
    formatted_history = []
    for item in history:
        h = dict(item)
        if "at" in h and isinstance(h["at"], datetime):
            h["at"] = dt_to_ms(h["at"])
        if "changes" in h and isinstance(h["changes"], dict):
            new_changes = {}
            for k, change in h["changes"].items():
                c = dict(change)
                if isinstance(c.get("from"), datetime):
                    c["from"] = dt_to_ms(c["from"])
                if isinstance(c.get("to"), datetime):
                    c["to"] = dt_to_ms(c["to"])
                new_changes[k] = c
            h["changes"] = new_changes
        formatted_history.append(h)

    res["history"] = formatted_history
    return res


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        if not DATE_REGEX.match(v):
            raise ValueError("joined_on must match strict YYYY-MM-DD format")
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError("joined_on must be a valid calendar date")
        return v

    @model_validator(mode="after")
    def validate_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(
        default=None,
        ge=100000000000,
        le=4102444800000,
        description="Epoch milliseconds",
    )
    status: PresenceStatus = PresenceStatus.PRESENT


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(
        default=None,
        ge=100000000000,
        le=4102444800000,
        description="Epoch milliseconds",
    )


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[StrictInt] = Field(
        default=None,
        ge=100000000000,
        le=4102444800000,
        description="Epoch milliseconds",
    )
    punch_out: Optional[StrictInt] = Field(
        default=None,
        ge=100000000000,
        le=4102444800000,
        description="Epoch milliseconds",
    )
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    model_config = {"extra": "ignore"}

    @model_validator(mode="before")
    @classmethod
    def check_no_explicit_nulls(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for key in ("status", "punch_in", "punch_out"):
                if key in data and data[key] is None:
                    raise ValueError(f"Explicit null is not allowed for '{key}'")
        return data


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    now_dt = datetime.now(timezone.utc).replace(microsecond=0)
    doc["created_at"] = now_dt
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    doc.pop("_id", None)
    doc["created_at"] = dt_to_ms(now_dt)
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q: Dict[str, Any] = {}
    if department is not None:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)
    items = []
    for emp in cursor:
        if "created_at" in emp and isinstance(emp["created_at"], datetime):
            emp["created_at"] = dt_to_ms(emp["created_at"])
        items.append(emp)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    ts = (
        ms_to_dt(body.punched_at)
        if body.punched_at is not None
        else datetime.now(timezone.utc).replace(microsecond=0)
    )
    date_str, start_dt, end_dt = shift_window(ts, emp["shift_start"], emp["shift_end"])

    status_val = body.status.value if isinstance(body.status, PresenceStatus) else str(body.status)
    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": status_val,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, start_dt),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")

    return record_out(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    pout = (
        ms_to_dt(body.punched_at)
        if body.punched_at is not None
        else datetime.now(timezone.utc).replace(microsecond=0)
    )

    # Find the employee's most recent record whose punch_in <= punched_at
    log = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": pout}},
        sort=[("punch_in", -1)],
    )
    if not log or log.get("punch_in") is None:
        raise HTTPException(404, "No matching punch-in found")

    if log.get("punch_out") is not None:
        raise HTTPException(409, "Record already punched out")

    pin = log["punch_in"]
    if pin.tzinfo is None:
        pin = pin.replace(tzinfo=timezone.utc)

    diff_sec = (pout - pin).total_seconds()
    if diff_sec <= 0:
        raise HTTPException(422, "punch_out must be strictly after punch_in")
    if diff_sec > 24 * 3600:
        raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")

    _, _, end_dt = shift_window(pin, emp["shift_start"], emp["shift_end"])
    work_hours = compute_work_hours(pin, pout)
    overtime_minutes = compute_overtime(pout, end_dt)
    half_day = compute_half_day(work_hours)

    # Atomic update to guarantee race-safety: two concurrent punch-outs yield exactly one 200 and one 409
    res = db.attendance_logs.update_one(
        {"_id": log["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": pout,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
    )
    if res.matched_count == 0:
        raise HTTPException(409, "Record already punched out")

    updated = db.attendance_logs.find_one({"_id": log["_id"]})
    return record_out(updated)


@app.patch("/attendance/{emp_code}/{date}", status_code=200)
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date)

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    log = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not log:
        raise HTTPException(404, "Attendance record not found for given date")

    old_status = log.get("status")
    old_pin = log.get("punch_in")
    old_pout = log.get("punch_out")
    old_late = log.get("late_minutes", 0)
    old_work = log.get("work_hours")
    old_ot = log.get("overtime_minutes", 0)
    old_half = log.get("half_day", False)

    if old_pin is not None and old_pin.tzinfo is None:
        old_pin = old_pin.replace(tzinfo=timezone.utc)
    if old_pout is not None and old_pout.tzinfo is None:
        old_pout = old_pout.replace(tzinfo=timezone.utc)

    # Target status
    new_status = body.status.value if body.status is not None else old_status

    # Rule: ABSENT or LEAVE clears both punch times; supplying punch_in or punch_out with them is 422
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "Cannot supply punch_in or punch_out with ABSENT or LEAVE status")
        new_pin = None
        new_pout = None
    else:
        # Presence status requires punch_in
        if body.punch_in is not None:
            new_pin = ms_to_dt(body.punch_in)
        else:
            new_pin = old_pin

        if new_pin is None:
            raise HTTPException(422, "Presence status requires a punch_in")

        if body.punch_out is not None:
            new_pout = ms_to_dt(body.punch_out)
        else:
            new_pout = old_pout

    # Rule: punch_in must stay on the record's attendance date per R1
    if new_pin is not None:
        rec_date, start_dt, end_dt = shift_window(new_pin, emp["shift_start"], emp["shift_end"])
        if rec_date != date:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")

    # Rule: punch_out must be after punch_in and within 24 hours
    if new_pout is not None:
        if new_pin is None:
            raise HTTPException(422, "punch_out cannot exist without punch_in")
        diff_sec = (new_pout - new_pin).total_seconds()
        if diff_sec <= 0:
            raise HTTPException(422, "punch_out must be strictly after punch_in")
        if diff_sec > 24 * 3600:
            raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")

    # Recompute derived fields
    if new_status in ("ABSENT", "LEAVE"):
        new_late = 0
        new_work = None
        new_ot = 0
        new_half = False
    else:
        new_late = compute_late_minutes(new_pin, start_dt)
        if new_pout is not None:
            new_work = compute_work_hours(new_pin, new_pout)
            new_ot = compute_overtime(new_pout, end_dt)
            new_half = compute_half_day(new_work)
        else:
            new_work = None
            new_ot = 0
            new_half = False

    # Build history.changes with ONLY fields that actually changed
    changes = {}
    if new_status != old_status:
        changes["status"] = {"from": old_status, "to": new_status}
    if new_pin != old_pin:
        changes["punch_in"] = {"from": old_pin, "to": new_pin}
    if new_pout != old_pout:
        changes["punch_out"] = {"from": old_pout, "to": new_pout}
    if new_work != old_work:
        changes["work_hours"] = {"from": old_work, "to": new_work}
    if new_late != old_late:
        changes["late_minutes"] = {"from": old_late, "to": new_late}
    if new_ot != old_ot:
        changes["overtime_minutes"] = {"from": old_ot, "to": new_ot}
    if new_half != old_half:
        changes["half_day"] = {"from": old_half, "to": new_half}

    if not changes:
        raise HTTPException(422, "Request made no changes to the attendance record")

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    history_list = log.get("history") or []
    n = len(history_list)

    update_set = {
        "status": new_status,
        "punch_in": new_pin,
        "punch_out": new_pout,
        "work_hours": new_work,
        "late_minutes": new_late,
        "overtime_minutes": new_ot,
        "half_day": new_half,
    }

    query_filter = {
        "_id": log["_id"],
        "punch_in": log.get("punch_in"),
        "punch_out": log.get("punch_out"),
        f"history.{n}": {"$exists": False},
    }

    res = db.attendance_logs.update_one(
        query_filter,
        {
            "$set": update_set,
            "$push": {"history": history_entry},
        },
    )
    if res.matched_count == 0:
        raise HTTPException(409, "Concurrent modification conflict")

    updated = db.attendance_logs.find_one({"_id": log["_id"]})
    return record_out(updated)


def build_attendance_list_query(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Union[Status, str]] = None,
    page: int = 1,
    page_size: int = 20,
) -> Tuple[Dict[str, Any], list, int, int]:
    """
    Builds (filter_dict, sort_spec, skip, limit) for list_attendance.
    Shared by list_attendance and /admin/explain/attendance_list.
    """
    q: Dict[str, Any] = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        q["date"] = {}
        if date_from is not None:
            q["date"]["$gte"] = date_from
        if date_to is not None:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status.value if isinstance(status, Status) else status

    sort_spec = [("date", -1), ("emp_code", 1)]
    skip = (page - 1) * page_size
    limit = page_size
    return q, sort_spec, skip, limit


def build_department_employees_query(
    department: str,
) -> Tuple[Dict[str, Any], Dict[str, int]]:
    """
    Builds the filter and projection for department employees lookup.
    Shared by department_trend and /admin/explain.
    """
    return {"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if date_from is not None:
        parse_date(date_from)
    if date_to is not None:
        parse_date(date_to)
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(422, "date_from cannot be greater than date_to")

    q, sort_spec, skip, limit = build_attendance_list_query(
        emp_code=emp_code,
        date_from=date_from,
        date_to=date_to,
        status=status,
        page=page,
        page_size=page_size,
    )

    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q)
        .sort(sort_spec)
        .skip(skip)
        .limit(limit)
    )
    items = [record_out(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}



@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(...)):
    parse_month(month)

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    pipeline = build_employee_monthly_pipeline(emp_code, month)
    agg_res = list(db.attendance_logs.aggregate(pipeline))

    if agg_res:
        row = agg_res[0]
        present_days = float(row.get("present_days", 0.0))
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late_minutes = int(row.get("total_late_minutes", 0))
        total_overtime_minutes = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    working_days = compute_working_days(month, emp["joined_on"])

    if working_days == 0:
        attendance_pct = None
    else:
        pct = (Decimal(str(present_days)) / Decimal(str(working_days))) * Decimal(100)
        attendance_pct = float(pct.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    # Round present_days half-up per R8
    present_days_rounded = float(Decimal(str(present_days)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days_rounded,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(...),
    department: Optional[str] = Query(default=None),
):
    parse_month(month)

    pipeline = build_department_summary_pipeline(month, department)
    cursor = db.employees.aggregate(pipeline)

    items = []
    for doc in cursor:
        headcount = int(doc["headcount"])
        if headcount == 0:
            continue

        pres_days_dec = Decimal(str(doc.get("present_days", 0.0)))
        present_days = float(
            pres_days_dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        )

        n_hours = int(doc.get("total_work_hours_n", 0))
        if n_hours == 0:
            avg_work_hours = None
        else:
            hours_sum = Decimal(str(doc.get("total_work_hours", 0.0)))
            avg_val = hours_sum / Decimal(str(n_hours))
            avg_work_hours = float(
                avg_val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            )

        items.append({
            "department": doc["_id"],
            "headcount": headcount,
            "present_days": present_days,
            "avg_work_hours": avg_work_hours,
            "late_count": int(doc.get("late_count", 0)),
            "total_late_minutes": int(doc.get("total_late_minutes", 0)),
            "leave_count": int(doc.get("leave_count", 0)),
            "on_duty_count": int(doc.get("on_duty_count", 0)),
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(...),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = Query(default=None),
):
    parse_month(month)
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    cursor = db.attendance_logs.aggregate(pipeline)
    items = list(cursor)
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    parse_date(from_date)
    parse_date(to_date)

    from_d = datetime.strptime(from_date, "%Y-%m-%d").date()
    to_d = datetime.strptime(to_date, "%Y-%m-%d").date()

    if to_d < from_d:
        raise HTTPException(status_code=422, detail="'to' date must be >= 'from' date")
    if (to_d - from_d).days + 1 > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    emp_filter, emp_proj = build_department_employees_query(department)
    employees = list(db.employees.find(emp_filter, emp_proj))
    if not employees:
        raise HTTPException(status_code=404, detail="Department not found")

    pipeline = build_department_trend_pipeline(
        department=department,
        from_date=from_date,
        to_date=to_date,
        employees=employees,
    )
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainEndpoint,
    emp_code: Optional[str] = Query(default=None),
    month: Optional[str] = Query(default=None),
    department: Optional[str] = Query(default=None),
    limit: Optional[int] = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
    status: Optional[Status] = Query(default=None),
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
    page: Optional[int] = Query(default=1, ge=1),
    page_size: Optional[int] = Query(default=20, ge=1, le=100),
):
    if endpoint == ExplainEndpoint.attendance_list:
        collection_name = "attendance_logs"
        if date_from is not None:
            parse_date(date_from)
        if date_to is not None:
            parse_date(date_to)
        if date_from is not None and date_to is not None and date_from > date_to:
            raise HTTPException(422, "date_from cannot be greater than date_to")

        q, sort_spec, skip, limit_val = build_attendance_list_query(
            emp_code=emp_code,
            date_from=date_from,
            date_to=date_to,
            status=status,
            page=page or 1,
            page_size=page_size or 20,
        )
        cmd = {
            "explain": {
                "find": collection_name,
                "filter": q,
                "sort": dict(sort_spec),
                "skip": skip,
                "limit": limit_val,
            },
            "verbosity": "executionStats",
        }

    elif endpoint == ExplainEndpoint.employee_monthly:
        collection_name = "attendance_logs"
        if not emp_code or not month:
            raise HTTPException(422, "Missing required parameters: emp_code and month")
        parse_month(month)
        pipeline = build_employee_monthly_pipeline(emp_code, month)
        cmd = {
            "explain": {
                "aggregate": collection_name,
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }

    elif endpoint == ExplainEndpoint.department_summary:
        collection_name = "employees"
        if not month:
            raise HTTPException(422, "Missing required parameter: month")
        parse_month(month)
        pipeline = build_department_summary_pipeline(month, department)
        cmd = {
            "explain": {
                "aggregate": collection_name,
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }

    elif endpoint == ExplainEndpoint.late_leaderboard:
        collection_name = "attendance_logs"
        if not month:
            raise HTTPException(422, "Missing required parameter: month")
        parse_month(month)
        pipeline = build_late_leaderboard_pipeline(month, limit or 10, department)
        cmd = {
            "explain": {
                "aggregate": collection_name,
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }

    elif endpoint == ExplainEndpoint.department_trend:
        collection_name = "attendance_logs"
        if not department or not from_date or not to_date:
            raise HTTPException(
                422, "Missing required parameters: department, from, to"
            )
        parse_date(from_date)
        parse_date(to_date)
        from_d = datetime.strptime(from_date, "%Y-%m-%d").date()
        to_d = datetime.strptime(to_date, "%Y-%m-%d").date()
        if to_d < from_d:
            raise HTTPException(422, "'to' date must be >= 'from' date")
        if (to_d - from_d).days + 1 > 92:
            raise HTTPException(422, "Date range cannot exceed 92 days")

        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        cmd = {
            "explain": {
                "aggregate": collection_name,
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }

    res = db.command(cmd)
    explain_data = json.loads(bson.json_util.dumps(res))
    return {
        "endpoint": endpoint.value,
        "collection": collection_name,
        "explain": explain_data,
    }

