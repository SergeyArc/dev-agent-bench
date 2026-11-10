"""Mutation sessions for many units: one queue of containers, the host load decides how many.

    python3 bench-mut/batch.py <runs-root> <out-root> [--repeat <trial-id>] [--skip <unit>]…
                               [--rerun-overloaded] [--jobs N]
    python3 bench-mut/batch.py --stop <out-root>

Units are the step-5 snapshots of `<out-root>/inventory.json` (see inventory.py) that pass
the precondition known from the verifier: C4 passed and the snapshot bundle is readable.
Order: the unit of `--repeat` a second time (suffix `-r2`, the noise of the measurement),
one unit per cell, then the rest.

Per unit: every shard (entry of `src/tracksvc/`) is one container (in_container.sh); when
all shards are done the unit is summarised (collect.py) and its "no tests" mutants are
rechecked with the whole suite (recheck_in_container.sh), in chunks, through the same queue.

Start and stop are chosen by the operator; nothing waits or starts by itself.
  start   refused (exit 2) unless the 1-minute load average is below START_LOAD.
  --stop  writes `<out-root>/batch/STOP`. The running batch then starts no container, asks
          mutmut in the running ones to stop (SIGINT: verdicts so far are kept, the mutant in
          flight is abandoned and run again later), lets the containers finish and exits.
          The next start goes on from the kept verdicts.

Host load. A timeout is a verdict about the mutant only on a quiet machine, so:
  - up to MAX_JOBS containers while the load is at most FULL_LOAD, REDUCED_JOBS up to
    HOLD_LOAD, no new container above it; running containers are never killed for load;
  - the load is sampled every SAMPLE seconds into `<out-root>/batch/load.log`;
  - mutants that timed out in a container during which the load exceeded FULL_LOAD are run
    again (same procedure, own container) until the container saw no overload, at most
    RERUNS times; the last verdict replaces the first (collect.py reads `_rerun/`).
`--rerun-overloaded` goes through the units again, finished ones included, and gives every
shard whose timeouts are still verdicts of an overloaded container RERUNS more attempts
(meant for a quiet machine and a small `--jobs`). Nothing that is finished runs twice.

State of every unit is written to `<out-root>/batch/<unit>.json`; a unit whose suite does
not pass — in the precondition or in mutmut's own clean run — is excluded with the reason
and the batch goes on. Excluded and failed units are looked at again by the next start
(their finished containers are not run again). A shard without a single mutant (mutmut
mutates function bodies only) is an empty layer, not a failure.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tarfile
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
IMAGE = "dev-agent-bench:dev"
CONTAINER_TIMEOUT = 12 * 3600
RECHECK_CHUNK = 40
START_LOAD = 10.0
FULL_LOAD = 12.0
HOLD_LOAD = 15.0
MAX_JOBS = 6
REDUCED_JOBS = 3
SAMPLE = 30
RERUNS = 3
# 255 (pytest cannot even collect: the mutant breaks the import of the application) is a
# kill, not a timeout, and is not run again.
TIMEOUT_CODES = {36, 24, -24, 152}
NO_MUTANTS = "Filtered for specific mutants, but nothing matches"
RECHECK_TIMEOUT_CODES = {124, 137}
LOCK = threading.Lock()


def log(message: str) -> None:
    with LOCK:
        print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)


class Stopped(Exception):
    """The operator asked the batch to stop."""


class Control:
    """Host load, the number of running containers and the stop request."""

    def __init__(self, batch_dir: Path, limits: argparse.Namespace) -> None:
        self.load_log = batch_dir / "load.log"
        self.stop_file = batch_dir / "STOP"
        self.limits = limits
        self.samples: list[tuple[float, float]] = []
        self.running: dict[str, Path] = {}
        self.stopping = False
        self.slot = threading.Condition()
        threading.Thread(target=self._watch, daemon=True).start()

    def _watch(self) -> None:
        last_sample = last_signal = 0.0
        while True:
            now = time.time()
            if now - last_sample >= SAMPLE:
                one, five, _ = os.getloadavg()
                with self.slot:
                    self.samples.append((now, one))
                    running = len(self.running)
                    self.slot.notify_all()
                with self.load_log.open("a") as handle:
                    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
                    handle.write(f"{stamp}\t{one:.2f}\t{five:.2f}\t{running}\n")
                last_sample = now
            if self.stop_file.is_file() and now - last_signal >= 10:
                self._stop()  # repeated: a container still in its clean run has no mutmut yet
                last_signal = now
            time.sleep(2)

    def _stop(self) -> None:
        with self.slot:
            first = not self.stopping
            self.stopping = True
            running = dict(self.running)
            self.slot.notify_all()
        if first:
            log(
                f"STOP requested: no new container; asking {len(running)} running one(s) to finish"
            )
        for name, out in running.items():
            (out / "stopped").write_text(time.strftime("%Y-%m-%dT%H:%M:%S\n"))
            (out / "STOP").write_text("")  # read by recheck_in_container.sh
            subprocess.run(
                [
                    "docker",
                    "exec",
                    name,
                    "pkill",
                    "-INT",
                    "-o",
                    "-u",
                    "agent",
                    "-f",
                    "mutmut run",
                ],
                capture_output=True,
                check=False,
            )

    def allowed(self) -> int:
        one = os.getloadavg()[0]
        if one <= self.limits.full_load:
            return self.limits.jobs
        if one <= self.limits.hold_load:
            return min(self.limits.jobs, REDUCED_JOBS)
        return 0

    def acquire(self, name: str, out: Path) -> None:
        announced = False
        with self.slot:
            while True:
                if self.stopping:
                    raise Stopped
                if len(self.running) < self.allowed():
                    self.running[name] = out
                    return
                if not announced and len(self.running) < self.limits.jobs:
                    log(
                        f"{name}: held, load {os.getloadavg()[0]:.1f}, {len(self.running)} running"
                    )
                    announced = True
                self.slot.wait(timeout=SAMPLE)

    def release(self, name: str) -> None:
        with self.slot:
            self.running.pop(name, None)
            self.slot.notify_all()

    def peak(self, start: float, end: float) -> float:
        with self.slot:
            inside = [
                value
                for moment, value in self.samples
                if start - SAMPLE <= moment <= end + SAMPLE
            ]
        return max([*inside, os.getloadavg()[0]])


CONTROL: Control


def slug(unit: dict[str, object]) -> str:
    pair = re.sub(r"[^a-z0-9]+", "-", str(unit["pair"]).lower()).strip("-")
    trial_id = str(unit["trial"]).split("/")[1].rsplit("-", 1)[1]
    return f"{pair}-{str(unit['variant']).lower()}-{trial_id}"


def docker(
    name: str,
    mounts: dict[Path, str],
    command: list[str],
    env: dict[str, str],
    out: Path,
) -> dict[str, object]:
    """Run one container when the load allows; record its time span and peak load."""
    CONTROL.acquire(name, out)
    args = ["docker", "run", "--rm", "--name", name]
    for source, target in mounts.items():
        args += ["-v", f"{source}:{target}"]
    for key, value in env.items():
        args += ["-e", f"{key}={value}"]
    started = time.time()
    try:
        with (out / "container.log").open("a") as handle:
            try:
                code = subprocess.run(
                    [*args, IMAGE, *command],
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    timeout=CONTAINER_TIMEOUT,
                    check=False,
                ).returncode
            except subprocess.TimeoutExpired:
                subprocess.run(
                    ["docker", "rm", "-f", name], capture_output=True, check=False
                )
                code = -1
    finally:
        CONTROL.release(name)
    ended = time.time()
    span = {
        "exit_code": code,
        "started": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(started)),
        "seconds": round(ended - started),
        "peak_load": round(CONTROL.peak(started, ended), 2),
    }
    with (out / "load.jsonl").open("a") as handle:
        handle.write(json.dumps(span) + "\n")
    # The peak over all invocations of this container (a stopped shard has several).
    spans = [json.loads(line) for line in (out / "load.jsonl").read_text().splitlines()]
    span["peak_load"] = max(item["peak_load"] for item in spans)
    span["seconds"] = sum(item["seconds"] for item in spans)
    span["invocations"] = len(spans)
    (out / "load.json").write_text(json.dumps(span) + "\n")
    log(
        f"{name}: exit {code}, {spans[-1]['seconds']} s, peak load {spans[-1]['peak_load']}"
    )
    if (out / "stopped").is_file():
        raise Stopped
    return span


def read_json(path: Path) -> dict[str, object]:
    try:
        loaded = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def tail(path: Path, lines: int = 3) -> str:
    try:
        text = path.read_text(errors="replace").replace("\r", "\n")
    except OSError:
        return ""
    return " / ".join(
        [line.strip() for line in text.splitlines() if line.strip()][-lines:]
    )[:400]


def timed_out(archive: Path, only: set[str] | None = None) -> list[str]:
    """Mutants with a timeout verdict in the meta files of one container."""
    found: list[str] = []
    if not archive.is_file():
        return found
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            if not member.name.endswith(".py.meta"):
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            for mutant, code in json.load(handle).get("exit_code_by_key", {}).items():
                if code in TIMEOUT_CODES and (only is None or mutant in only):
                    found.append(mutant)
    return found


def peak_of(out: Path) -> float:
    value = read_json(out / "load.json").get("peak_load")
    return float(value) if isinstance(value, (int, float)) else 0.0


class Unit:
    def __init__(
        self, name: str, source: Path | None, meta: dict[str, object], out_root: Path
    ) -> None:
        self.name = name
        self.bundle = source
        self.meta = meta
        self.unit_dir = out_root / "units" / name
        self.run_dir = out_root / "runs" / name
        self.recheck_dir = out_root / "recheck" / name
        self.state_path = out_root / "batch" / f"{name}.json"
        self.started = time.time()
        self.recheck_timeout = 900
        self.setup_cfg = ""

    def mounts(self, out: Path) -> dict[Path, str]:
        return {self.unit_dir: "/unit:ro", HERE: "/bench-mut:ro", out: "/out"}

    def done(self) -> bool:
        return read_json(self.state_path).get("status") == "done"

    def prepare(self) -> list[str]:
        if not (self.unit_dir / "app").is_dir():
            subprocess.run(
                [
                    str(HERE / "prepare_unit.sh"),
                    "bundle",
                    str(self.bundle),
                    str(self.unit_dir),
                ],
                check=True,
                capture_output=True,
            )
        shards = []
        for entry in sorted((self.unit_dir / "app" / "src" / "tracksvc").iterdir()):
            if entry.is_dir() and entry.name != "__pycache__":
                shards.append(entry.name)
            elif entry.suffix == ".py" and entry.name != "__init__.py":
                shards.append(entry.stem)
        shards = list(dict.fromkeys(shards))  # worker/ and worker.py are one layer
        only = CONTROL.limits.shards
        if only:
            shards = [shard for shard in shards if shard in only.split(",")]
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "shards").write_text("\n".join(shards) + "\n")
        return shards

    def shard(self, shard: str) -> None:
        out = self.run_dir / shard
        out.mkdir(parents=True, exist_ok=True)
        finished = (out / "postcheck.json").is_file() and (out / "load.json").is_file()
        failed = read_json(out / "precondition.json").get("exit_code") not in (None, 0)
        if (out / "stopped").is_file() or not (finished or failed):
            limits = CONTROL.limits
            env = {}
            if limits.test_timeouts and not (out / "load.json").is_file():
                env = {
                    "MUTMUT_TIMEOUT_MULTIPLIER": "0.001",
                    "MUTMUT_TIMEOUT_CONSTANT": "0",
                }
            docker(
                f"benchmut-{self.name}-{shard}",
                self.mounts(out),
                [
                    "/bench-mut/in_container.sh",
                    "mutate",
                    limits.glob.format(shard=shard),
                ],
                env,
                out,
            )
        self.rerun_timeouts(shard, more=CONTROL.limits.rerun_overloaded)

    def rerun_timeouts(self, shard: str, *, more: bool) -> None:
        """Run again the mutants that timed out while the host was overloaded.

        Attempts already made are read, not repeated; `more` gives RERUNS new ones.
        """
        last = self.run_dir / shard
        mutants = timed_out(last / "mutants-meta.tar")
        attempt = 0
        budget: int | None = None
        while True:
            out = self.run_dir / "_rerun" / f"{shard}-{attempt + 1}"
            ran = (out / "postcheck.json").is_file() and not (out / "stopped").is_file()
            if not ran:
                if budget is None:  # the new attempts start here
                    budget = RERUNS if more else max(0, RERUNS - attempt)
                overloaded = peak_of(last) > CONTROL.limits.full_load
                if not mutants or not overloaded or not budget:
                    return
                budget -= 1
                out.mkdir(parents=True, exist_ok=True)
                if not (out / "rerun-mutants.txt").is_file():
                    (out / "rerun-mutants.txt").write_text("\n".join(mutants) + "\n")
                names = (out / "rerun-mutants.txt").read_text().split()
                log(
                    f"{self.name} shard {shard}: rerun {attempt + 1} of {len(names)} timed-out mutant(s)"
                )
                docker(
                    f"benchmut-{self.name}-{shard}-rerun{attempt + 1}",
                    self.mounts(out),
                    ["/bench-mut/in_container.sh", "mutate", *names],
                    {},
                    out,
                )
            attempt += 1
            mutants = timed_out(
                out / "mutants-meta.tar",
                set((out / "rerun-mutants.txt").read_text().split()),
            )
            last = out

    def failure(self, shards: list[str]) -> str | None:
        """Why the unit cannot be scored, or None."""
        for shard in shards:
            out = self.run_dir / shard
            pre = read_json(out / "precondition.json")
            if pre.get("exit_code") != 0:
                detail = pre.get("summary") or tail(out / "container.log")
                return f"precondition ({shard}): exit {pre.get('exit_code')}: {detail}"
            run = read_json(out / "mutmut-run.json")
            if run.get("exit_code") != 0 and NO_MUTANTS in tail(out / "mutmut-run.log"):
                continue  # an empty layer
            if run.get("exit_code") != 0:
                return f"mutmut ({shard}): exit {run.get('exit_code')}: {tail(out / 'mutmut-run.log')}"
        return None

    def summarise(self) -> dict[str, object]:
        subprocess.run(
            [
                sys.executable,
                str(HERE / "collect.py"),
                str(self.run_dir),
                "--json",
                str(self.run_dir / "summary.json"),
            ],
            check=True,
            capture_output=True,
        )
        return read_json(self.run_dir / "summary.json")

    def recheck_chunks(
        self, summary: dict[str, object], shards: list[str]
    ) -> list[Path]:
        layers = summary["layers"]
        assert isinstance(layers, list)
        mutants = [m for layer in layers for m in layer["not_killed"]["no tests"]]
        slowest = max(
            float(layer["precondition"].get("seconds") or 0) for layer in layers
        )
        self.recheck_timeout = int(max(120, 4 * slowest + 60))
        self.setup_cfg = (self.run_dir / shards[0] / "setup.cfg").read_text()
        return [
            self.chunk(
                f"{index // RECHECK_CHUNK:02d}", mutants[index : index + RECHECK_CHUNK]
            )
            for index in range(0, len(mutants), RECHECK_CHUNK)
        ]

    def chunk(self, name: str, mutants: list[str]) -> Path:
        chunk = self.recheck_dir / name
        chunk.mkdir(parents=True, exist_ok=True)
        if not (chunk / "mutants.txt").is_file():
            (chunk / "mutants.txt").write_text("\n".join(mutants) + "\n")
            (chunk / "setup.cfg").write_text(self.setup_cfg)
        return chunk

    def recheck(self, chunk: Path) -> None:
        for attempt in range(RERUNS + 1):
            if not (chunk / "control.tsv").is_file():
                (chunk / "stopped").unlink(missing_ok=True)
                docker(
                    f"benchmut-{self.name}-recheck-{chunk.name}",
                    self.mounts(chunk),
                    ["/bench-mut/recheck_in_container.sh"],
                    {"RECHECK_TIMEOUT": str(self.recheck_timeout)},
                    chunk,
                )
            if not (chunk / "control.tsv").is_file():
                raise RuntimeError(
                    f"recheck {chunk.name} did not finish: {tail(chunk / 'setup.log') or tail(chunk / 'container.log')}"
                )
            rows = [
                line.split("\t")
                for line in (chunk / "recheck.tsv").read_text().splitlines()
                if line
            ]
            slow = [row[0] for row in rows if int(row[1]) in RECHECK_TIMEOUT_CODES]
            if (
                not slow
                or peak_of(chunk) <= CONTROL.limits.full_load
                or attempt == RERUNS
            ):
                return
            log(
                f"{self.name} recheck {chunk.name}: rerun of {len(slow)} timed-out mutant(s)"
            )
            chunk = self.chunk(f"{chunk.name.split('-r')[0]}-r{attempt + 1}", slow)

    def save(self, **state: object) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "unit": self.name,
            **self.meta,
            "wall_seconds": round(time.time() - self.started),
            **state,
        }
        self.state_path.write_text(
            json.dumps(payload, indent=1, ensure_ascii=False) + "\n"
        )


def results(futures: list[Future[None]]) -> None:
    """Wait for all; a stop request wins over other outcomes."""
    stopped = False
    for future in futures:
        try:
            future.result()
        except Stopped:
            stopped = True
    if stopped or CONTROL.stopping:
        raise Stopped


def finish_unit(
    unit: Unit, shards: list[str], futures: list[Future[None]], pool: ThreadPoolExecutor
) -> None:
    try:
        results(futures)
        (unit.run_dir / "wall-seconds").write_text(
            f"{round(time.time() - unit.started)}\n"
        )
        reason = unit.failure(shards)
        if reason:
            unit.save(status="excluded", reason=reason)
            log(f"{unit.name}: EXCLUDED — {reason}")
            return
        summary = unit.summarise()
        chunks = unit.recheck_chunks(summary, shards)
        results([pool.submit(unit.recheck, chunk) for chunk in chunks])
        unit.save(status="done", shards=shards, recheck_chunks=len(chunks))
        log(f"{unit.name}: done, {summary['total']['mutants']} mutants")  # type: ignore[index]
    except Stopped:
        unit.save(status="stopped")
        log(f"{unit.name}: stopped, verdicts kept")
    except Exception as error:  # noqa: BLE001 — one unit must not stop the batch
        unit.save(status="error", reason=repr(error))
        log(f"{unit.name}: ERROR {error!r}")


def ordered(
    entries: list[dict[str, object]], repeat: str | None
) -> list[dict[str, object]]:
    """One unit per cell first (the cell of the repeated unit counts as covered), then the rest."""
    first: list[dict[str, object]] = []
    rest: list[dict[str, object]] = []
    covered = {
        (entry["pair"], entry["variant"], entry["turn_limit"])
        for entry in entries
        if repeat and slug(entry).endswith(repeat)
    }
    for entry in entries:
        cell = (entry["pair"], entry["variant"], entry["turn_limit"])
        if cell in covered:
            rest.append(entry)
        else:
            covered.add(cell)
            first.append(entry)
    return first + rest


def main() -> None:
    global CONTROL
    parser = argparse.ArgumentParser()
    parser.add_argument("runs_root", type=Path)
    parser.add_argument("out_root", type=Path, nargs="?")
    parser.add_argument(
        "--stop",
        action="store_true",
        help="ask the running batch of <out-root> to stop",
    )
    parser.add_argument(
        "--repeat",
        help="trial id (last component, e.g. 5411de) to run a second time, first",
    )
    parser.add_argument(
        "--skip",
        action="append",
        default=[],
        help="unit name to leave out (already measured)",
    )
    parser.add_argument(
        "--rerun-overloaded",
        action="store_true",
        help="all units again: more attempts for timeouts of overloaded containers",
    )
    parser.add_argument("--jobs", type=int, default=MAX_JOBS)
    # For checks of the batch itself; a measurement uses the defaults.
    parser.add_argument("--start-load", type=float, default=START_LOAD)
    parser.add_argument("--full-load", type=float, default=FULL_LOAD)
    parser.add_argument("--hold-load", type=float, default=HOLD_LOAD)
    parser.add_argument(
        "--only", action="append", default=[], help="run only these units"
    )
    parser.add_argument("--shards", help="comma-separated shards to run")
    parser.add_argument(
        "--glob", default="tracksvc.{shard}.*", help="mutants of a shard"
    )
    parser.add_argument(
        "--test-timeouts",
        action="store_true",
        help="first run of a shard: every mutant times out",
    )
    args = parser.parse_args()

    if args.stop:
        stop = args.runs_root.expanduser() / "batch" / "STOP"
        stop.write_text(time.strftime("%Y-%m-%dT%H:%M:%S\n"))
        print(f"stop requested: {stop}")
        return
    if args.out_root is None:
        parser.error("out_root is required")
    runs_root, out_root = args.runs_root.expanduser(), args.out_root.expanduser()

    one = os.getloadavg()[0]
    if one >= args.start_load:
        print(f"not started: load {one:.1f} is not below {args.start_load}")
        sys.exit(2)
    busy = subprocess.run(
        ["docker", "ps", "--filter", "name=benchmut-", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    if busy:
        print(
            f"not started: containers of an earlier batch are running: {', '.join(busy)}"
        )
        sys.exit(2)

    (out_root / "batch").mkdir(parents=True, exist_ok=True)
    (out_root / "batch" / "STOP").unlink(missing_ok=True)
    CONTROL = Control(out_root / "batch", args)

    entries = [
        entry
        for entry in json.loads((out_root / "inventory.json").read_text())
        if entry.get("snapshot") == "ok"
        and entry["c4"].get("status") == "pass"
        and not entry["place"].startswith("out")
    ]
    units: list[Unit] = []
    for entry in ordered(entries, args.repeat):
        bundle = (
            runs_root
            / entry["trial"]
            / "steps/step-5/artifacts/logs/artifacts/snapshot-step-5.bundle"
        )
        meta = {
            key: entry[key]
            for key in ("pair", "variant", "turn_limit", "trial", "place")
        }
        name = slug(entry)
        if args.repeat and name.endswith(args.repeat):
            units.insert(
                0, Unit(f"{name}-r2", bundle, {**meta, "repeat": True}, out_root)
            )
        if name not in args.skip:
            units.append(Unit(name, bundle, meta, out_root))
    if args.only:
        units = [unit for unit in units if unit.name in args.only]
    todo = [unit for unit in units if args.rerun_overloaded or not unit.done()]
    log(
        f"load {one:.1f}; {len(todo)} unit(s) to run of {len(units)}: {', '.join(unit.name for unit in todo)}"
    )

    # Containers run in `pool`, queued in this order; the waiters only wait.
    with (
        ThreadPoolExecutor(args.jobs) as pool,
        ThreadPoolExecutor(len(units) + 1) as waiters,
    ):
        waiting: list[Future[None]] = []
        for unit in todo:
            unit.started = time.time()
            try:
                shards = unit.prepare()
            except Exception as error:  # noqa: BLE001
                unit.save(status="error", reason=repr(error))
                log(f"{unit.name}: ERROR {error!r}")
                continue
            futures = [pool.submit(unit.shard, shard) for shard in shards]
            waiting.append(waiters.submit(finish_unit, unit, shards, futures, pool))
        for future in waiting:
            future.result()
    log("batch stopped" if CONTROL.stopping else "batch finished")


if __name__ == "__main__":
    main()
