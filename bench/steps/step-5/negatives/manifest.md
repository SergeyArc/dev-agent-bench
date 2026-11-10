Expected IDs updated to the actual results of the negatives gate (Harbor, oracle + patch, without C4).
`[judge]` IDs are kept as written by the author: the gate does not run judge criteria.

| file | mistake (one line) | expected failing criterion IDs (from contract §9) | why |
|---|---|---|---|
| `01-ack-before-processing.patch` | The delivery is acknowledged before database work and publishing finish. | `5.16`, `5.J1` | A broker loss or worker death after the early ack can permanently lose an unapplied message. |
| `02-retry-one-too-many.patch` | The retry boundary uses `<= MAX` and performs one extra attempt. | `5.7`, `5.8`, `5.9`, `5.10` | A delivery arriving at `MAX` is republished as `MAX + 1` instead of going straight to dead-letter. |
| `03-validate-before-duplicate.patch` | Header and body validation run before the idempotency lookup. | `5.19` | A recorded id with an invalid body is dead-lettered instead of being acknowledged as a duplicate with no publication. |
| `04-exit-after-broker-loss.patch` | The consumer returns after a broker error instead of reconnecting. | `5.15`, `5.16` | The PID may remain only briefly during shutdown, but the worker never resumes consumption after the broker recovers. Gate: the worker exits with code 0 during the outage of 5.16 as well. |
| `05-processed-row-second-commit.patch` | The `processed_messages` row is written in a second commit, after the capacity change is committed. | `5.6`, `5.J4` | Deliveries of one `message_id` processed at once all pass the duplicate check before any row exists, so the capacity changes more than once; duplicates that arrive one after another are still rejected (5.5 passes). |
