Expected IDs updated to the actual results of the negatives gate (Harbor, oracle + patch, without C4).
`[judge]` IDs are kept as written by the author: the gate does not run judge criteria.

| file | mistake (one line) | expected failing criterion IDs (from contract §9) | why |
|---|---|---|---|
| `01-coerce-json-types.patch` | Pydantic coercion accepts non-exact JSON scalar types. | `2.6` | A string such as `"5"` is converted to an integer instead of producing `422 validation_error`. |
| `02-prefixed-validation-fields.patch` | Validation locations retain FastAPI's `body.` / `path.` prefix. | `2.5`, `2.6` | The contract requires `capacity`, `track_id`, or exactly `body`, not transport-prefixed paths. Gate: 2.7 does not fail — for a body that is not JSON or not an object the location is exactly `body`, so nothing is prefixed. |
| `03-null-details-on-all-errors.patch` | Non-validation errors serialize `details: null`. | `2.9`; cascade `1.7`, `1.11`, `1.12` | For every non-validation code the inner error object must contain exactly `code` and `message`. Gate: the step-1 criteria that check the 503 body of `/ready` fail too. |
| `04-conflict-overwrites-first-track.patch` | On a duplicate code the rejected track is written over the stored one before the conflict is raised. | `2.8` | The request returns the expected conflict, but `GET` of the first track then returns the rejected aggregate (another `id`, the capacity of the second request). |

**Judge only (no patch file).** The first version of 04 (`04-write-before-conflict-check.patch`, commit `8ddc962`) stored the
rejected track under its own id before the conflict check: `self._tracks_by_id[track.id] = track` moved above the check.
The answer is the correct 409 and the first track is unchanged; the stray track has an id that the server generated and
never returned, so nothing in the step-2 API can show it (the negatives gate: only C4 failed, on the oracle's own
repository test). No `[det]` criterion can catch it at steps 2–3; it is a finding for the judge reading the repository code.
