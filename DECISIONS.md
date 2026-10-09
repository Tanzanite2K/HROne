# DECISIONS.md

1. **Indexes.** `attendance_logs`: unique `(emp_code, date)`, so one record per employee per day;
   `(date desc, emp_code)` for the list's sort and month ranges; `(emp_code, punch_in desc)` for punch-out;
   `(status, date, emp_code)` for the status filter. `employees`: unique `emp_code`, plus `(department, emp_code)`,
   `(department, joined_on)` and `joined_on` for filters. I skipped `late_minutes`: the month filter already narrows
   the data, and every punch-in would pay for it.

2. **Punch-in race.** Both requests try to insert the same `(emp_code, date)`. I don't check first; the unique index
   lets MongoDB accept one insert and reject the other with a duplicate-key error, which I turn into a 409.

3. **Ties.** I rank with `$rank`, so equal totals share a rank and the next rank is skipped (1, 2, 2, 4). The limit
   is applied to the rank, not the row count, so everyone tied at the cutoff is returned, ordered by `emp_code`.

4. **Headcount.** The pipeline starts from `employees`, not from logs. Each employee who joined by month end is one
   row, even with no logs, so counting rows counts them.

5. **At 100x data.** I would keep a small daily summary per employee, updated on each punch, and run analytics on
   that instead of the raw logs.
