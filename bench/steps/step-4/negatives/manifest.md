Expected IDs updated to the actual results of the negatives gate (Harbor, oracle + patch, without C4).
`[judge]` IDs are kept as written by the author: the gate does not run judge criteria.

| file | mistake (one line) | expected failing criterion IDs (from contract §9) | why |
|---|---|---|---|
| `01-no-row-lock.patch` | Mutable aggregates are loaded without a row lock. | `4.10` | Concurrent equal-span requests can both validate stale state and overwrite each other instead of yielding one success. Gate later gate: 4.10 fails 3 runs of 3 (5 x 201, 5 x 409). Before later gate the test of 4.10 let it pass: the app opened its database connections during the burst, so the requests did not overlap; the burst is now preceded by simultaneous requests that read the database. |
| `02-leave-removed-segment-row.patch` | Saving deletes retained labels rather than all previous segment rows. | `4.7`; cascade `3.10` | A removed segment is left in PostgreSQL even though it disappeared from the in-memory aggregate returned by the request. Gate: in 3.10 the GET after `DELETE segments/alpha` returns another body than the DELETE response. |
| `03-unique-conflict-leaks-as-storage-error.patch` | Duplicate track codes use a plain insert and never translate the unique conflict. | `2.8`, `4.9`, `4.J2` | The database exception is mapped as storage failure rather than `409 track_code_taken`. |
| `04-drop-parent-before-child.patch` | Downgrade drops the referenced parent table before the child table. | `4.2`, `4.J4` | PostgreSQL refuses to drop `tracks` while the segment foreign key still references it. |
