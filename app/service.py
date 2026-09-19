"""Application service: request -> domain objects -> persistence payloads."""

from __future__ import annotations

from .schemas import WakeRequest, matrix_to_nested
from .separation import (
    Capacity,
    ClosureConflict,
    ClosureWindow,
    Flight,
    TrialRunwayResult,
    Violation,
    build_matrix,
    find_capacity_violations,
    find_closure_conflicts,
    find_violations,
    sort_flights,
    sort_windows,
    trial_reschedule,
)
from .timeutils import ensure_utc


def _build_runway_flights(req: WakeRequest) -> dict[str, list[Flight]]:
    """Normalise every flight to UTC and return per-runway sorted lists."""
    payloads: dict[str, list[Flight]] = {}
    for rw in req.runways:
        flights = [
            Flight(
                flight_no=f.flight_no,
                scheduled_off=ensure_utc(f.scheduled_off),
                wake=f.wake.value,
                locked=f.locked,
            )
            for f in rw.flights
        ]
        payloads[rw.runway] = sort_flights(flights)
    return payloads


def _runways_with_locked_input(req: WakeRequest) -> set[str]:
    """Runways whose request explicitly carried the ``locked`` field on at
    least one flight — only those runways echo lock detail, so requests that
    never mention ``locked`` keep the legacy response shape."""
    runways: set[str] = set()
    for rw in req.runways:
        if any("locked" in f.model_fields_set for f in rw.flights):
            runways.add(rw.runway)
    return runways


def _locked_flights_by_runway(
    payloads: dict[str, list[Flight]],
) -> dict[str, list[str]]:
    """Stable-order flight numbers of the locked flights per runway."""
    return {
        runway: [f.flight_no for f in flights if f.locked]
        for runway, flights in payloads.items()
    }


def _build_runway_windows(req: WakeRequest) -> dict[str, list[ClosureWindow]]:
    """Normalise every closure window to UTC, sorted per runway."""
    windows: dict[str, list[ClosureWindow]] = {}
    for rw in req.runways:
        windows[rw.runway] = sort_windows(
            [
                ClosureWindow(start=ensure_utc(w.start), end=ensure_utc(w.end))
                for w in rw.closures
            ]
        )
    return windows


def _build_runway_capacities(req: WakeRequest) -> dict[str, Capacity]:
    """Return per-runway rolling capacity limits (runways without one omitted)."""
    return {
        rw.runway: Capacity(
            window_seconds=rw.capacity.window_seconds,
            max_departures=rw.capacity.max_departures,
        )
        for rw in req.runways
        if rw.capacity is not None
    }


def _matrix_from_request(req: WakeRequest) -> dict[tuple[str, str], int]:
    if req.matrix is None:
        return build_matrix(None)
    return build_matrix(
        {(e.leader.value, e.follower.value): e.seconds for e in req.matrix}
    )


def run_validate(req: WakeRequest) -> tuple[
    dict[str, list[Flight]],
    dict[tuple[str, str], int],
    dict[str, list[ClosureWindow]],
    dict[str, Capacity],
    dict[str, list[Violation]],
    dict[str, list[ClosureConflict]],
    dict[str, list],
    set[str],
    dict[str, list[str]],
]:
    payloads = _build_runway_flights(req)
    windows = _build_runway_windows(req)
    capacities = _build_runway_capacities(req)
    matrix = _matrix_from_request(req)
    violations = {
        runway: find_violations(flights, matrix)
        for runway, flights in payloads.items()
    }
    conflicts = {
        runway: find_closure_conflicts(flights, windows[runway])
        for runway, flights in payloads.items()
    }
    capacity_violations = {
        runway: find_capacity_violations(flights, capacities.get(runway))
        for runway, flights in payloads.items()
    }
    locked_runways = _runways_with_locked_input(req)
    locked_flights = _locked_flights_by_runway(payloads)
    return (
        payloads,
        matrix,
        windows,
        capacities,
        violations,
        conflicts,
        capacity_violations,
        locked_runways,
        locked_flights,
    )


def run_trial(req: WakeRequest) -> tuple[
    dict[str, list[Flight]],
    dict[tuple[str, str], int],
    dict[str, list[ClosureWindow]],
    dict[str, Capacity],
    dict[str, TrialRunwayResult],
    set[str],
    dict[str, list[str]],
]:
    payloads = _build_runway_flights(req)
    windows = _build_runway_windows(req)
    capacities = _build_runway_capacities(req)
    matrix = _matrix_from_request(req)
    trials = {
        runway: trial_reschedule(
            flights,
            matrix,
            windows[runway],
            capacities.get(runway),
        )
        for runway, flights in payloads.items()
    }
    locked_runways = _runways_with_locked_input(req)
    locked_flights = _locked_flights_by_runway(payloads)
    return payloads, matrix, windows, capacities, trials, locked_runways, locked_flights


def nested_matrix(matrix: dict[tuple[str, str], int]) -> dict[str, dict[str, int]]:
    return matrix_to_nested(matrix)
