"""FastAPI application: runway departure wake-separation validation."""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .database import get_db, init_db
from .repository import load_batch, save_batch
from .schemas import (
    BatchResponse,
    ChosenSwapOut,
    HealthOut,
    LockedFlightOut,
    MatrixOut,
    RunwayReportOut,
    RunwaySwapOut,
    RunwayTrialOut,
    SwapMetricsOut,
    SwapResponse,
    TrialBlockedOut,
    TrialResponse,
    ValidateResponse,
    WakeRequest,
    default_matrix_nested,
)
from .separation import ClosureWindow
from .service import nested_matrix, run_best_adjacent_swap, run_trial, run_validate
from .config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="跑道离场尾流间隔校验 API",
    description=(
        "提交按跑道分组的航班计划（可附带跑道临时关闭窗口与滚动容量约束），"
        "校验相邻离场航班的尾流间隔违规、落入关闭窗口的航班与滚动容量超限，"
        "并支持在不改动原计划的前提下试排（向后级联延误，自动满足容量并"
        "避开关闭窗口）。航班可选 locked=true 标记为不可移动：试排中必须"
        "保持 scheduled_off，否则在跑道级返回首个不可行航班、冲突约束与"
        "所需最早时刻，仅持久化可行前缀。此外提供相邻交换优化试排"
        "（best-adjacent-swap）：以稳定排序为基线枚举每一对相邻航班的交换，"
        "按总延误、最大单航班延误、交换位置选取最优顺序（不接受锁定航班）。"
    ),
    version="1.4.0",
    lifespan=lifespan,
)


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
    """Pydantic 之外可能抛出的领域校验错误统一为 400。"""
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.get("/", tags=["meta"])
def root() -> dict:
    return {
        "service": "runway-wake-separation",
        "docs": "/docs",
        "health": "/health",
        "default_matrix": "/matrix/default",
        "endpoints": {
            "validate": "POST /api/v1/validate",
            "trial": "POST /api/v1/trial",
            "best_adjacent_swap": "POST /api/v1/trial/best-adjacent-swap",
            "batch": "GET /api/v1/batches/{batch_id}",
        },
    }


@app.get("/health", response_model=HealthOut, tags=["meta"])
def health() -> HealthOut:
    return HealthOut(status="ok")


@app.get("/matrix/default", response_model=MatrixOut, tags=["meta"])
def default_matrix() -> MatrixOut:
    return MatrixOut(matrix=default_matrix_nested())


def _window_out(window: ClosureWindow) -> dict:
    return {"start": window.start, "end": window.end}


def _capacity_out(capacity) -> dict:
    return {
        "window_seconds": capacity.window_seconds,
        "max_departures": capacity.max_departures,
    }


def _trial_item_out(item, *, has_capacity: bool, has_locks: bool = False) -> dict:
    """Serialisable dict for one trial item (shared by trial and swap)."""
    return {
        "position": item.position,
        "flight_no": item.flight_no,
        "wake": item.wake,
        "scheduled_off": item.original_time,
        "adjusted_off": item.adjusted_time,
        "delay_seconds": item.delay_seconds,
        "triggered_by": item.triggered_by,
        "closure_hit": item.closure_hit,
        "closure_window": (
            _window_out(item.closure_window) if item.closure_window else None
        ),
        "locked": item.locked,
        "capacity_hit": item.capacity_hit,
        "capacity_window_seconds": item.capacity_window_seconds,
        "capacity_max_departures": item.capacity_max_departures,
        "capacity_anchor": item.capacity_anchor,
        "capacity_window": (
            {
                "start": item.capacity_window_start,
                "end": item.capacity_window_end,
            }
            if item.capacity_window_start is not None
            else None
        ),
        "has_capacity": has_capacity,
        "has_locks": has_locks,
    }


@app.post("/api/v1/validate", response_model=ValidateResponse, tags=["wake"])
def validate(
    req: WakeRequest, db: sqlite3.Connection = Depends(get_db)
) -> ValidateResponse:
    """按（计划时刻, 航班号）稳定排序，列出各跑道相邻航班的间隔违规、
    落入关闭窗口的航班及其命中窗口，以及滚动容量约束的超限航班、
    触发窗口与最早合规时刻。"""
    (
        payloads,
        matrix,
        windows,
        capacities,
        violations,
        conflicts,
        capacity_violations,
        locked_runways,
        locked_flights,
    ) = run_validate(req)
    batch_id = save_batch(
        db,
        mode="validate",
        matrix=matrix,
        runway_payloads=payloads,
        closures_by_runway=windows,
        capacities_by_runway=capacities,
        violations_by_runway=violations,
        closure_conflicts_by_runway=conflicts,
        capacity_violations_by_runway=capacity_violations,
        locked_runways=locked_runways,
    )

    runway_reports: list[RunwayReportOut] = []
    total_locked = 0
    for runway, flights in payloads.items():
        vlist = violations[runway]
        clist = conflicts[runway]
        capacity = capacities.get(runway)
        cvlist = capacity_violations[runway]
        runway_has_locks = runway in locked_runways
        runway_locked = locked_flights.get(runway, [])
        if runway_has_locks:
            total_locked += len(runway_locked)
        runway_reports.append(
            RunwayReportOut(
                runway=runway,
                order=[f.flight_no for f in flights],
                total=len(flights),
                closures=[_window_out(w) for w in windows[runway]],
                violation_count=len(vlist),
                violations=[
                    {
                        "leader": v.leader_no,
                        "follower": v.follower_no,
                        "leader_wake": v.leader_wake,
                        "follower_wake": v.follower_wake,
                        "leader_scheduled_off": v.leader_time,
                        "follower_scheduled_off": v.follower_time,
                        "required_seconds": v.required_seconds,
                        "actual_seconds": v.actual_seconds,
                        "shortfall_seconds": v.shortfall_seconds,
                        "leader_position": v.leader_position,
                        "follower_position": v.follower_position,
                    }
                    for v in vlist
                ],
                closure_conflict_count=len(clist),
                closure_conflicts=[
                    {
                        "flight_no": c.flight_no,
                        "wake": c.wake,
                        "scheduled_off": c.scheduled_off,
                        "position": c.position,
                        "window_index": c.window_index,
                        "window_start": c.window_start,
                        "window_end": c.window_end,
                    }
                    for c in clist
                ],
                has_capacity=capacity is not None,
                capacity=_capacity_out(capacity) if capacity is not None else None,
                capacity_violation_count=len(cvlist),
                capacity_violations=[
                    {
                        "flight_no": cv.flight_no,
                        "wake": cv.wake,
                        "scheduled_off": cv.scheduled_off,
                        "position": cv.position,
                        "window_seconds": cv.window_seconds,
                        "max_departures": cv.max_departures,
                        "departures_in_window": cv.departures_in_window,
                        "anchor_flight_no": cv.anchor_flight_no,
                        "anchor_position": cv.anchor_position,
                        "window_start": cv.anchor_off,
                        "window_end": cv.window_end,
                        "earliest_compliant_off": cv.earliest_compliant_off,
                    }
                    for cv in cvlist
                ],
                has_locks=runway_has_locks,
                locked_flights=[
                    LockedFlightOut(
                        position=pos,
                        flight_no=lf.flight_no,
                        scheduled_off=lf.scheduled_off,
                        wake=lf.wake,
                    )
                    for pos, lf in enumerate(flights, start=1)
                    if lf.flight_no in set(runway_locked)
                ],
            )
        )

    return ValidateResponse(
        batch_id=batch_id,
        matrix=nested_matrix(matrix),
        total_runways=len(payloads),
        total_flights=sum(len(f) for f in payloads.values()),
        total_violations=sum(len(v) for v in violations.values()),
        total_closure_conflicts=sum(len(c) for c in conflicts.values()),
        has_capacity=bool(capacities),
        total_capacity_violations=sum(
            len(cv) for cv in capacity_violations.values()
        ),
        has_locks=bool(locked_runways),
        total_locked=total_locked,
        runways=runway_reports,
    )


def _blocked_out(blocked, *, has_capacity: bool) -> TrialBlockedOut:
    capacity_window = None
    if blocked.capacity_window_start is not None:
        capacity_window = {
            "start": blocked.capacity_window_start,
            "end": blocked.capacity_window_end,
        }
    return TrialBlockedOut(
        flight_no=blocked.flight_no,
        wake=blocked.wake,
        scheduled_off=blocked.scheduled_off,
        position=blocked.position,
        constraint=blocked.constraint,
        required_earliest_off=blocked.required_earliest_off,
        triggered_by=blocked.triggered_by,
        required_gap_seconds=blocked.required_gap_seconds,
        closure_hit=blocked.closure_hit,
        closure_window=(
            _window_out(blocked.closure_window) if blocked.closure_window else None
        ),
        has_capacity=has_capacity,
        capacity_hit=blocked.capacity_hit,
        capacity_window_seconds=blocked.capacity_window_seconds,
        capacity_max_departures=blocked.capacity_max_departures,
        capacity_anchor=blocked.capacity_anchor,
        capacity_window=capacity_window,
    )


@app.post("/api/v1/trial", response_model=TrialResponse, tags=["wake"])
def trial(
    req: WakeRequest, db: sqlite3.Connection = Depends(get_db)
) -> TrialResponse:
    """保持原排序逐项推迟到最早安全时刻（满足尾流间隔、滚动容量约束且
    避开关闭窗口）；延误向后级联，原计划不变。

    锁定航班（locked=true）不可移动：必须保持 scheduled_off。当稳定顺序
    计算中尾流/关闭/容量约束会把首个锁定航班推迟时，该跑道在此停止，仅
    返回可行前缀与该航班（冲突约束、所需最早时刻），其后不再给出建议。
    """
    payloads, matrix, windows, capacities, trials, locked_runways, locked_flights = (
        run_trial(req)
    )
    batch_id = save_batch(
        db,
        mode="trial",
        matrix=matrix,
        runway_payloads=payloads,
        closures_by_runway=windows,
        capacities_by_runway=capacities,
        trial_by_runway=trials,
        locked_runways=locked_runways,
    )

    runway_trials: list[RunwayTrialOut] = []
    total_delayed = 0
    total_delay = 0
    total_locked = 0
    for runway, result in trials.items():
        items = result.items
        delayed = [i for i in items if i.delay_seconds > 0]
        rw_delay = sum(i.delay_seconds for i in items)
        total_delayed += len(delayed)
        total_delay += rw_delay
        capacity = capacities.get(runway)
        flights = payloads[runway]
        runway_has_locks = runway in locked_runways
        locked_no_set = set(locked_flights.get(runway, []))
        if runway_has_locks:
            total_locked += len(locked_no_set)
        runway_trials.append(
            RunwayTrialOut(
                runway=runway,
                order=[f.flight_no for f in flights],
                total=len(flights),
                closures=[_window_out(w) for w in windows[runway]],
                delayed_count=len(delayed),
                total_delay_seconds=rw_delay,
                max_delay_seconds=max((i.delay_seconds for i in items), default=0),
                has_capacity=capacity is not None,
                capacity=_capacity_out(capacity) if capacity is not None else None,
                items=[
                    _trial_item_out(
                        i,
                        has_capacity=capacity is not None,
                        has_locks=runway_has_locks,
                    )
                    for i in items
                ],
                has_locks=runway_has_locks,
                locked_flights=[
                    {
                        "position": pos,
                        "flight_no": f.flight_no,
                        "scheduled_off": f.scheduled_off,
                        "wake": f.wake,
                    }
                    for pos, f in enumerate(flights, start=1)
                    if f.flight_no in locked_no_set
                ],
                blocked=(
                    _blocked_out(result.blocked, has_capacity=capacity is not None)
                    if runway_has_locks and result.blocked is not None
                    else None
                ),
            )
        )

    return TrialResponse(
        batch_id=batch_id,
        matrix=nested_matrix(matrix),
        total_runways=len(trials),
        total_flights=sum(len(f) for f in payloads.values()),
        total_delayed=total_delayed,
        total_delay_seconds=total_delay,
        has_locks=bool(locked_runways),
        total_locked=total_locked,
        runways=runway_trials,
    )


@app.post(
    "/api/v1/trial/best-adjacent-swap",
    response_model=SwapResponse,
    tags=["wake"],
)
def trial_best_adjacent_swap(
    req: WakeRequest, db: sqlite3.Connection = Depends(get_db)
) -> SwapResponse:
    """以稳定排序为基线，枚举「不交换」及每一对相邻航班的交换
    （scheduled_off 不变，仅处理顺序不同），对每种顺序按现有尾流、
    关闭窗口与滚动容量规则从头试排；先最小化总延误秒数，再最小化
    最大单航班延误，仍并列时取交换位置最小者，与「不交换」并列时
    优先不交换。各跑道独立求解；请求含任意 locked=true 航班时返回 422。
    """
    locked = [f.flight_no for rw in req.runways for f in rw.flights if f.locked]
    if locked:
        raise HTTPException(
            status_code=422,
            detail="相邻交换优化要求全部航班可移动，请求包含 locked=true 的航班: "
            + ", ".join(locked),
        )
    payloads, matrix, windows, capacities, swaps = run_best_adjacent_swap(req)
    batch_id = save_batch(
        db,
        mode="best_adjacent_swap",
        matrix=matrix,
        runway_payloads=payloads,
        closures_by_runway=windows,
        capacities_by_runway=capacities,
        trial_by_runway={runway: r.optimized for runway, r in swaps.items()},
        swap_by_runway=swaps,
    )

    runway_outs: list[RunwaySwapOut] = []
    baseline_total = 0
    optimized_total = 0
    for runway, result in swaps.items():
        capacity = capacities.get(runway)
        baseline_total += result.baseline_total_delay
        optimized_total += result.optimized_total_delay
        chosen = result.chosen_swap
        runway_outs.append(
            RunwaySwapOut(
                runway=runway,
                total=len(payloads[runway]),
                baseline_order=[f.flight_no for f in payloads[runway]],
                optimized_order=result.optimized_order,
                closures=[_window_out(w) for w in windows[runway]],
                baseline=SwapMetricsOut(
                    delayed_count=result.baseline_delayed_count,
                    total_delay_seconds=result.baseline_total_delay,
                    max_delay_seconds=result.baseline_max_delay,
                ),
                optimized=SwapMetricsOut(
                    delayed_count=result.optimized_delayed_count,
                    total_delay_seconds=result.optimized_total_delay,
                    max_delay_seconds=result.optimized_max_delay,
                ),
                chosen_swap=(
                    ChosenSwapOut(
                        position=chosen.position,
                        flights=[chosen.flight_a, chosen.flight_b],
                    )
                    if chosen is not None
                    else None
                ),
                saved_seconds=result.saved_seconds,
                items=[
                    _trial_item_out(i, has_capacity=capacity is not None)
                    for i in result.optimized.items
                ],
                has_capacity=capacity is not None,
                capacity=_capacity_out(capacity) if capacity is not None else None,
            )
        )

    return SwapResponse(
        batch_id=batch_id,
        matrix=nested_matrix(matrix),
        total_runways=len(swaps),
        total_flights=sum(len(f) for f in payloads.values()),
        baseline_total_delay_seconds=baseline_total,
        optimized_total_delay_seconds=optimized_total,
        total_saved_seconds=baseline_total - optimized_total,
        runways=runway_outs,
    )


@app.get(
    "/api/v1/batches/{batch_id}",
    response_model=BatchResponse,
    tags=["wake"],
)
def get_batch(
    batch_id: str, db: sqlite3.Connection = Depends(get_db)
) -> BatchResponse:
    """读取已保存的批次（validate、trial 或 best_adjacent_swap 均可）。"""
    data = load_batch(db, batch_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"批次不存在: {batch_id}")
    return BatchResponse(**data)
