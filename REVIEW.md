# REVIEW.md

| # | Where | What is wrong | How you'd notice it | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Shift start is built on the punch-in's own clock (naive, server-local), not IST | Server in UTC: 10:05 IST is 04:35 UTC, which is before 09:30, so late = 0 instead of 35 | Replaced with `shift_window()`, which builds shift times in IST |
| 2 | `compute_late_minutes` | Floors to whole minutes before testing the threshold; no seconds truncation; ignores overnight shifts | 09:40:30 gives 10 whole minutes, which is not > 10, so 0; the rule says late (10) | Compare in seconds (> 600), then floor; truncate to whole seconds first |
| 3 | `compute_work_hours` | `round()` on a float is banker's rounding | 32418 s (9.005 h) can come out as 9.0 instead of 9.01 | `Decimal` with `ROUND_HALF_UP` |
| 4 | `compute_overtime` | No 30-minute minimum; shift end built from `date_str` (wrong for overnight); aware minus naive datetime raises `TypeError` | Punch-out 18:45 returns 15, should be 0; 22:00-06:00 shift breaks | Takes the shift-end datetime from `shift_window()`; returns 0 under 30 |
| 5 | `EmployeeIn` | No validation on `emp_code`, email, HH:MM, `joined_on`; `shift_start` can equal `shift_end` | `emp_code: "123"` returns 201; `shift_start: "9:30"` is stored | Patterns, real-date check, shift validator, 422 |
| 6 | `PunchInIn` | `status` is any string; `punched_at` is a loose int | `status: "LEAVE"` accepted; seconds value lands in 1970 | Enum for presence statuses; `StrictInt` within epoch-ms bounds |
| 7 | `/health` | Always returns ok | Stop MongoDB: still 200 | Ping MongoDB; 503 on failure |
| 8 | `create_employee` | Check-then-insert race, no unique index; `created_at` naive local time and returned as a datetime | Two parallel creates both get 201 | Unique index + `DuplicateKeyError` gives 409; `created_at` UTC, returned as epoch ms |
| 9 | `list_employees` | `skip = page * page_size`; `total` ignores the filter; no sort; no bounds | `page=1` returns the second page; total shows all employees | `(page-1)*page_size`, `count_documents(q)`, sort by `emp_code`, `Query` bounds |
| 10 | `punch_in` | Unknown employee: `emp` is `None`, then `emp["shift_start"]` | `emp_code: "EMP9999"` gives 500 | Explicit 404 |
| 11 | `punch_in` | `fromtimestamp()` uses the server's zone; date is not IST; no overnight rule; no seconds truncation | Punch near midnight gets the wrong `date` | UTC conversion, `shift_window()` for date (R1) |
| 12 | `punch_in` | Check-then-insert race | Parallel punch-ins give several 201s | Unique `(emp_code, date)` index, catch `DuplicateKeyError` for 409 |
| 13 | `punch_in` response | Leaks `id`; returns raw datetimes | Response has `id` and ISO strings | `record_out()`: no ids, epoch ms |
| 14 | `list_attendance` | Loads every match into memory, sorts and slices in Python | Slow and memory-heavy at 100k records | Sort, skip, limit and count in MongoDB, with an index |
| 15 | `list_attendance` | No validation; no `emp_code` tiebreak in the sort; leaks `id`; legacy docs missing fields | `date_from > date_to` returns 200; `?status=FOO` accepted | Validate dates, status enum, sort `(date -1, emp_code 1)`, defaults in `record_out()` |
| 16 | Startup | No indexes; default 30 s server selection timeout | Everything slow; `/health` hangs when the DB is down | Indexes created at startup; 2 s timeout |

## Looked at and fine
- `load_dotenv()` does not override real environment variables, which is the required priority.
- `{"_id": 0}` projection in `list_employees` keeps `_id` out of responses.
- Initialising `history` to `[]` on new records matches the data model.