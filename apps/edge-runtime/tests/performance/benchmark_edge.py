"""当前 WSL/CPU 可运行的 Edge S1/S2 基准; 仅输出基线, 不替代 500ms 目标设备验收。

复用单元/集成测试的合成事实与公共接缝; 普通 unittest discovery 不收集此文件。
"""

from __future__ import annotations

import argparse
import json
import math
import platform
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from statistics import mean, median, pstdev
from tempfile import TemporaryDirectory
from time import perf_counter_ns

from harness import STEPS as CORE_STEPS
from harness import opening_state as core_opening_state
from store_harness import ANCHOR, STATION
from store_harness import STEPS as STORE_STEPS
from store_harness import opening_state as store_opening_state

from edge_runtime.judgment.core import advance
from edge_runtime.judgment.model import (
    Decision,
    EvidenceSpan,
    HostInstant,
    JudgmentState,
    Lifecycle,
    Observation,
    Ordering,
    Violation,
)
from edge_runtime.judgment.reasons import ReasonCode, Verdict
from edge_runtime.local_state import QueueCapacity, StationStore, open_local_state

MIN_PERCENTILE_SAMPLES = 1000
ACKNOWLEDGED_AT = "2026-10-16T00:00:00+00:00"


def _summary(samples: list[int]) -> dict[str, object]:
    """nearest-rank 分位数; 样本不足时不提供看似精确的尾部指标。"""
    values = sorted(samples)
    n = len(values)

    def percentile(ratio: float) -> float:
        return round(values[math.ceil(n * ratio) - 1] / 1_000_000, 6)

    ready = n >= MIN_PERCENTILE_SAMPLES
    return {
        "samples": n,
        "unit": "ms",
        "percentile_method": "nearest-rank",
        "percentile_status": "valid" if ready else "insufficient_samples",
        "p50": percentile(0.50) if ready else None,
        "p95": percentile(0.95) if ready else None,
        "p99": percentile(0.99) if ready else None,
        "max": round(values[-1] / 1_000_000, 6),
        "mean": round(mean(values) / 1_000_000, 6),
    }


def _trial_stats(groups: list[list[int]]) -> dict[str, object]:
    trials = [_summary(items) for items in groups]
    averages = [mean(items) for items in groups]
    center = mean(averages)
    cv = pstdev(averages) / center if center else 0.0
    return {
        "trials": trials,
        "trial_median_means_ms": round(median(averages) / 1_000_000, 6),
        "trial_mean_cv": round(cv, 6),
        "baseline_stability": "unstable" if cv > 0.20 else "observed",
        "budget": None,
        "pass_basis": "existing_correctness_assertions_only",
    }


def _core_cases() -> tuple[tuple[str, tuple[Observation, ...], Decision], ...]:
    rework_events = tuple(
        Observation(
            signal=CORE_STEPS[idx], at=HostInstant(float(second)), source_time=float(second)
        )
        for second, idx in enumerate((0, 1, 2, 1, 3, 4), start=1)
    )
    skipped_events = tuple(
        Observation(
            signal=CORE_STEPS[idx], at=HostInstant(float(second)), source_time=float(second)
        )
        for second, idx in enumerate((0, 1, 3), start=1)
    )
    anchor = EvidenceSpan.at(HostInstant(3.0))
    return (
        (
            "closed_rework_pass",
            rework_events,
            Decision(
                instance_id=1,
                verdict=Verdict.PASS,
                reasons=(),
                violations=(),
                lifecycle=Lifecycle.CLOSED_BY_COMPLETE_SET,
                evidence=EvidenceSpan.at(HostInstant(6.0)),
            ),
        ),
        (
            "arrival_ordered_skip",
            skipped_events,
            Decision(
                instance_id=1,
                verdict=Verdict.FAIL,
                reasons=(ReasonCode.MISSED_STEP, ReasonCode.WRONG_STEP),
                violations=(
                    Violation(
                        reason=ReasonCode.WRONG_STEP, steps=(CORE_STEPS[3],), evidence=anchor
                    ),
                    Violation(
                        reason=ReasonCode.MISSED_STEP, steps=(CORE_STEPS[2],), evidence=anchor
                    ),
                ),
                lifecycle=Lifecycle.STAYS_OPEN,
                evidence=anchor,
            ),
        ),
    )


def _judgment(*, samples: int, warmup: int, trials: int) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, events, expected in _core_cases():
        groups: list[list[int]] = []
        for _ in range(trials):
            times: list[int] = []
            for iteration in range(warmup + samples):
                state = core_opening_state(Ordering.ORDERED)
                decisions: list[Decision] = []
                start = perf_counter_ns()
                for event in events:
                    outcome = advance(state, event)
                    state = outcome.state
                    decisions.extend(outcome.decisions)
                elapsed = perf_counter_ns() - start
                if tuple(decisions) != (expected,):
                    raise AssertionError(f"{name}: judgment differs from golden test vector")
                if iteration >= warmup:
                    times.append(elapsed)
            groups.append(times)
        result[name] = {"event_count": len(events), **_trial_stats(groups)}
    return result


def _reaction(index: int) -> tuple[JudgmentState, tuple[Decision, ...], HostInstant]:
    """由真实 core 合成一次跳步违规, 供 SQLite 公共 commit 接缝使用。"""
    at = ANCHOR + index * 4.0
    state = replace(store_opening_state(), next_instance_id=index + 1)
    started = advance(
        state,
        Observation(signal=STORE_STEPS[0], at=HostInstant(at), source_time=0.0),
    )
    skipped = advance(
        started.state,
        Observation(signal=STORE_STEPS[2], at=HostInstant(at + 1), source_time=1.0),
    )
    (decision,) = skipped.decisions
    if decision.verdict is not Verdict.FAIL or not decision.violations:
        raise AssertionError("SQLite workload must contain an actual latched violation")
    return skipped.state, skipped.decisions, HostInstant(at + 1)


def _commit(station: StationStore, index: int) -> None:
    state, decisions, at = _reaction(index)
    station.commit(
        state=state,
        decisions=decisions,
        evidence=(),
        closed_instances=(),
        report_provenance={},
        latched_at=ACKNOWLEDGED_AT,
        latched_monotonic=at,
    )


def _sqlite(
    *,
    samples: int,
    warmup: int,
    trials: int,
    backlogs: tuple[int, ...],
    artifacts: Path,
) -> dict[str, object]:
    outputs: dict[str, object] = {}
    for backlog in backlogs:
        groups: list[list[int]] = []
        trial_extra: list[dict[str, object]] = []
        for _ in range(trials):
            with TemporaryDirectory(dir=artifacts, prefix="sqlite-bench-") as directory:
                path = Path(directory) / "state.sqlite"
                # 使用真实文件、WAL、FULL; 不以 :memory: 替代 fsync。
                db = open_local_state(
                    str(path),
                    queue_capacity=QueueCapacity(
                        reports=3 * (backlog + warmup + samples + 1),
                        evidence=10,
                        observations=10,
                        health=10,
                    ),
                )
                try:
                    station = db.station(STATION)
                    for i in range(backlog + warmup):
                        _commit(station, i)
                    initial_depth = {item.name: item.depth for item in db.queue_status()}["report"]
                    times: list[int] = []
                    for i in range(backlog + warmup, backlog + warmup + samples):
                        # 时间仅包含 SQLite 事务, 不包含生成合成判定和断言。
                        state, decisions, at = _reaction(i)
                        start = perf_counter_ns()
                        station.commit(
                            state=state,
                            decisions=decisions,
                            evidence=(),
                            closed_instances=(),
                            report_provenance={},
                            latched_at=ACKNOWLEDGED_AT,
                            latched_monotonic=at,
                        )
                        times.append(perf_counter_ns() - start)
                    last_instance = backlog + warmup + samples
                    if (
                        station.latched_violations(instance_id=last_instance)
                        != decisions[0].violations
                    ):
                        raise AssertionError("violation lost during SQLite commit")
                    pending_before = {item.name: item.depth for item in db.queue_status()}["report"]
                    wal = Path(f"{path}-wal")
                    bytes_written = path.stat().st_size + (
                        wal.stat().st_size if wal.exists() else 0
                    )
                finally:
                    db.close()

                start = perf_counter_ns()
                reopened = open_local_state(str(path))
                reopen_ns = perf_counter_ns() - start
                try:
                    pending_after = {item.name: item.depth for item in reopened.queue_status()}[
                        "report"
                    ]
                    if pending_after != pending_before:
                        raise AssertionError("pending reports were lost on reopening SQLite")
                    owner = reopened.station(STATION)
                    if (
                        owner.latched_violations(instance_id=last_instance)
                        != decisions[0].violations
                    ):
                        raise AssertionError("latched violation was lost on SQLite reopen")
                    reports = owner.pending_reports()
                    drain_start = perf_counter_ns()
                    for report in reports:
                        owner.mark_reported(
                            report.queue_id,
                            at=HostInstant(ANCHOR),
                            acknowledged_at=ACKNOWLEDGED_AT,
                        )
                    drain_ns = perf_counter_ns() - drain_start
                    if {item.name: item.depth for item in reopened.queue_status()}["report"] != 0:
                        raise AssertionError("drain did not acknowledge all pending reports")
                finally:
                    reopened.close()

                groups.append(times)
                trial_extra.append(
                    {
                        "initial_pending": initial_depth,
                        "pending_before_restart": pending_before,
                        "reopen_ms": round(reopen_ns / 1_000_000, 6),
                        "drain_rows": len(reports),
                        "drain_rows_per_second": round(
                            len(reports) / (drain_ns / 1_000_000_000), 2
                        ),
                        "sqlite_and_wal_bytes": bytes_written,
                    }
                )
        outputs[str(backlog)] = {
            "commit": _trial_stats(groups),
            "recovery_and_drain_trials": trial_extra,
        }
    return outputs


def _metadata(artifacts: Path) -> dict[str, object]:
    def optional_text(path: Path) -> str | None:
        try:
            return path.read_text() if path.exists() else None
        except OSError:
            return None

    def command(*args: str) -> str | None:
        proc = subprocess.run(args, check=False, capture_output=True, text=True)
        return proc.stdout.strip() if proc.returncode == 0 else None

    governor = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    cpu_name = ""
    cpuinfo = Path("/proc/cpuinfo")
    cpu_details = optional_text(cpuinfo)
    if cpu_details:
        for line in cpu_details.splitlines():
            if line.startswith("model name"):
                cpu_name = line.partition(":")[2].strip()
                break
    root = Path(__file__).resolve().parents[4]
    return {
        "git_head": command("git", "-C", str(root), "rev-parse", "HEAD"),
        "git_origin_main": command("git", "-C", str(root), "rev-parse", "origin/main"),
        "git_dirty": bool(command("git", "-C", str(root), "status", "--porcelain")),
        "python": sys.version,
        "platform": platform.platform(),
        "cpu": cpu_name or platform.processor(),
        "cpu_governor": optional_text(governor),
        "sqlite_filesystem": command("stat", "-f", "-c", "%T", str(artifacts)),
        "storage_path": str(artifacts.resolve()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Edge CPU/SQLite baseline; never a 500ms acceptance test"
    )
    parser.add_argument("--samples", type=int, default=1000)
    parser.add_argument("--warmup", type=int, default=100)
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--backlogs", type=int, nargs="+", default=[0, 100])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not (1 <= args.samples <= 3000 and 0 <= args.warmup <= 300 and 1 <= args.trials <= 5):
        parser.error("resource bound: samples 1..3000, warmup 0..300, trials 1..5")
    if not 1 <= len(args.backlogs) <= 8 or any(
        value < 0 or value > 1000 for value in args.backlogs
    ):
        parser.error("provide 1..8 backlog values, each in 0..1000")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result = {
        "schema": "nvsop-edge-software-baseline/v1",
        "status": "baseline_only_not_500ms_acceptance",
        "scope": "Edge judgment core + real SQLite; no GPU, video, HTTP, or Center",
        "parameters": {
            "samples_per_bucket_and_trial": args.samples,
            "warmup": args.warmup,
            "trials": args.trials,
            "backlogs": args.backlogs,
            "minimum_percentile_samples": MIN_PERCENTILE_SAMPLES,
        },
        "environment": _metadata(args.output.parent),
        "s1_judgment": _judgment(samples=args.samples, warmup=args.warmup, trials=args.trials),
        "s2_sqlite": _sqlite(
            samples=args.samples,
            warmup=args.warmup,
            trials=args.trials,
            backlogs=tuple(args.backlogs),
            artifacts=args.output.parent,
        ),
    }
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    sys.stdout.write(f"Edge software baseline written to {args.output}\n")
    sys.stdout.write("Baseline only: correctness passed; timing SLO not evaluated.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
