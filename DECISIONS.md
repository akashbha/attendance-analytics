# DECISIONS.md

1. **Indexes.** employees: unique `emp_code` (identity, blocks duplicates), `(department, emp_code)` for filtered sorted lists, `(department, joined_on)` for headcount. attendance_logs: unique `(emp_code, date)` (one record per day, per-employee lookups) and `(date -1, emp_code 1)`, which matches the listing sort so MongoDB needs no in-memory sort. I rejected a plain ascending `(date, emp_code)` index: the sort is mixed direction, so it could not serve it.

2. **Punch-in race.** Both requests pass validation and call `insert_one`. The unique `(emp_code, date)` index lets the database accept only one; the other raises `DuplicateKeyError`, which I catch and return as 409. There is no find-then-insert, so there is no gap between check and write.

3. **Ties.** I rank with `$rank`, so tied employees share a rank (1, 2, 2, 4). Then I keep `rank <= limit` instead of using `$limit`, so everyone tied at the cutoff is returned. With limit 2, I can return 3 rows, because dropping one tied employee would be arbitrary.

4. **Headcount.** The summary starts from `employees` (`joined_on` on or before month end) and uses `$lookup` with a sub-pipeline and no `$unwind`. An employee with no logs keeps zero totals and still counts.

5. **100x data.** I would pre-aggregate daily per-department counters, updated on punch and correction, so analytics stop scanning raw logs. The trend also passes the employee list into the pipeline as a literal array, which would not scale.