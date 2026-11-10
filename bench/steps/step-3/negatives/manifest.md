Expected IDs updated to the actual results of the negatives gate (Harbor, oracle + patch, without C4).
`[judge]` IDs are kept as written by the author: the gate does not run judge criteria.

| file | mistake (one line) | expected failing criterion IDs (from contract §9) | why |
|---|---|---|---|
| `01-touch-counts-as-overlap.patch` | Closed-interval comparisons treat touching spans as overlapping. | `3.3` | `[0, 10)` followed by `[10, 20)` is rejected even though half-open spans do not overlap. |
| `02-preserve-insertion-order.patch` | Segment responses preserve insertion order instead of sorting by start. | `3.2`, `3.3`, `3.10` | The happy path works until segments are added out of spatial order. Gate: 3.3 and 3.10 compare the order of segments as well. |
| `03-reject-end-at-capacity.patch` | A span ending exactly at capacity is considered out of bounds. | `3.1`, `3.5` | The contract accepts the inclusive boundary `end == capacity`. Gate: 3.1 adds `[0, 1)` on capacity 1, which also ends at the capacity. |
| `04-reject-eighth-segment.patch` | The segment limit check is off by one and rejects the eighth segment. | `3.8`, `3.9` | At most eight means the eighth succeeds and only the ninth is rejected. Gate: 3.9 cannot build its eight-segment setup. |
