"""FastAPI application for time-of-use tiered tariff quoting."""
from __future__ import annotations

from contextlib import asynccontextmanager
from decimal import ROUND_HALF_UP
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.orm import Session

from . import (
    allocation, backup, demand, diff, estimate, importer, meter, pricing,
    settlement, shiftplan,
)
from .database import engine, init_db
from .pricing import (
    Q2,
    Q6,
    PricingError,
    load_version,
    validate_version,
)
from .schemas import (
    AllocationMemberOut,
    BackupResponse,
    CostAllocationRequest,
    CostAllocationResponse,
    DemandAllocationOut,
    DemandChargeRequest,
    DemandChargeResponse,
    DemandIntervalShareOut,
    DemandIntervalSliceOut,
    DemandWindowOut,
    DiffSegmentOut,
    DiffSideOut,
    EstimateGridPointOut,
    ExcludedWindowOut,
    MeterGapOut,
    MeterIntervalOut,
    MeterReconcileRequest,
    MeterReconcileResponse,
    QuoteDiffRequest,
    QuoteDiffResponse,
    QuoteRequest,
    QuoteResponse,
    RuleOut,
    SettlementBatchRequest,
    SettlementBatchResponse,
    ShiftPlanRequest,
    ShiftPlanResponse,
    ShiftSegmentOut,
    ShiftTaskOut,
    TierOut,
    UsageEstimateRequest,
    UsageEstimateResponse,
    VersionImportRequest,
    VersionImportResponse,
    VersionOut,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # 对齐上次崩溃可能留下的孤儿登记行/临时副本，保证启动即干净。
    backup.reconcile_backups()
    yield


app = FastAPI(
    title="分时阶梯计价 API",
    description=(
        "基于版本化费率规则（生效时间段/时区/工作日节假日条件/分段单价/"
        "累计用量上限）的报价服务。所有时间区间均为半开 [start, end)。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(PricingError)
async def pricing_error_handler(request: Request, exc: PricingError):
    body: dict = {"error": {
        "code": exc.code,
        "message": str(exc),
    }}
    if exc.details is not None:
        body["error"]["details"] = exc.details
    return JSONResponse(status_code=exc.status_code, content=body)


@app.get("/health")
def health():
    return {"status": "ok"}


# ------------------------------------------------------------------ backups


@app.post("/backups/{backup_id}", response_model=BackupResponse, tags=["backups"])
def post_backup(backup_id: str, response: Response):
    """服务不暂停的前提下生成可迁移 SQLite 快照。

    通过 SQLite Online Backup API 在**独立连接**上把在线库增量复制到备份
    目录的临时文件；复制期间 ``/quote``、``/cycles/{id}/batches`` 等请求
    照常工作。副本完成后用另一条独立连接执行 ``PRAGMA integrity_check``，
    并计算 SHA-256 与字节数；校验通过后以**原子链接**发布为
    ``<backup_id>.db``，返回下载地址与摘要。

    首次成功返回 **201**；同一 ``backup_id`` 的重放（含并发请求）返回
    **首次元数据（200）**，已发布备份不可被覆盖（409）；任何复制或校验
    失败都会删除临时文件，不留下半成品。
    """
    result = backup.create_backup(backup_id)
    if not result.created:
        response.status_code = 200
    else:
        response.status_code = 201
    return result.metadata


@app.get("/backups/{backup_id}", response_model=BackupResponse, tags=["backups"])
def get_backup(backup_id: str):
    """查询已发布备份的元数据（下载地址、SHA-256、字节数）；不存在 404。"""
    meta = backup.get_backup(backup_id)
    if meta is None:
        raise PricingError(
            f"备份不存在: {backup_id}", code="backup_not_found",
            status_code=404, details={"backup_id": backup_id})
    return meta


@app.get("/backups/{backup_id}/download", tags=["backups"])
def download_backup(backup_id: str):
    """下载已发布的 SQLite 副本（``application/vnd.sqlite3``）。

    仅在备份目录内按登记文件名发送；未发布的备份返回 404。
    """
    import os

    from .database import DatabaseBackup

    backup.validate_backup_id(backup_id)
    with Session(engine) as session:
        row = session.get(DatabaseBackup, backup_id)
        if row is None:
            raise PricingError(
                f"备份不存在: {backup_id}", code="backup_not_found",
                status_code=404, details={"backup_id": backup_id})
        path = os.path.join(backup.backup_dir(), row.filename)
        if not os.path.isfile(path):
            raise PricingError(
                f"备份文件缺失: {backup_id}", code="backup_file_missing",
                status_code=410, details={"backup_id": backup_id})
        return FileResponse(
            path,
            media_type="application/vnd.sqlite3",
            filename=row.filename,
            headers={
                "Content-Length": str(row.size_bytes),
                "X-Backup-SHA256": row.sha256,
            },
        )


@app.get("/versions", response_model=list[VersionOut], tags=["versions"])
def list_versions():
    with Session(engine) as session:
        versions = pricing.load_all_versions(session)
        out = []
        for v in versions:
            out.append(_version_out(v, include_rules=True))
        return out


@app.get("/versions/{version_code}", response_model=VersionOut,
         tags=["versions"])
def get_version(version_code: str):
    with Session(engine) as session:
        return _version_out(load_version(session, version_code),
                            include_rules=True)


@app.get("/versions/{version_code}/issues", tags=["versions"])
def get_version_issues(version_code: str):
    """显式返回版本的规则重叠 / 阶梯缺口等结构性问题。"""
    with Session(engine) as session:
        v = load_version(session, version_code)
        issues = validate_version(v)
        return {"version": version_code, "ok": not issues, "issues": issues}


@app.post("/versions/import", response_model=VersionImportResponse,
          status_code=201, tags=["versions"])
def post_version_import(req: VersionImportRequest, response: Response):
    """导入完整费率版本（含 holidays / rules / tiers）。

    首次导入在单个事务内写入全部层级并返回 201；同 code 同摘要的重放
    返回首次的结果（200）；同 code 不同摘要拒绝（409）。导入成功后
    版本立即可用于 /quote、/quote-diff 与 /shift-plan。
    """
    body, created = importer.import_version(req)
    if not created:
        response.status_code = 200
    return body


def _quote_response(version, result) -> QuoteResponse:
    """Build the /quote response body; ``result=None`` is a zero-usage quote
    (zero fee, no segments) used by /usage-estimate plateau endpoints."""
    return settlement.build_quote_response(version, result)


@app.post("/quote", response_model=QuoteResponse, tags=["quote"])
def post_quote(req: QuoteRequest):
    with Session(engine) as session:
        version = load_version(session, req.version)
        result = pricing.quote(version, req.intervals)
    return _quote_response(version, result)


@app.post("/cycles/{cycle_id}/batches", response_model=SettlementBatchResponse,
          status_code=201, tags=["settlement"])
def post_cycle_batch(cycle_id: str, req: SettlementBatchRequest,
                     response: Response):
    """账期分批用量的持久化增量结算（用量会分批到达）。

    每次提交携带规则版本、幂等键、``expected_revision`` 与一批用量区间。
    首次提交 expected_revision 必须为 0，成功后锁定规则版本、revision 加一；
    新旧区间按 UTC 合并且禁止重叠，账期整体用计价引擎重算，累计阶梯贯穿
    各批次。先判幂等重放再比 expected_revision：同键同内容返回首次响应，
    同键异内容或旧 revision 冲突返回 409。两个并发请求使用同一 revision
    时只有一个生效；任何区间冲突、规则缺口或计价失败都不会写入批次、
    幂等记录或 revision。状态持久化在 SQLite，重启后可接续。
    """
    result = settlement.submit_batch(cycle_id, req)
    if result.replayed:
        response.status_code = 200
    return result.body


@app.get("/cycles/{cycle_id}", tags=["settlement"])
def get_cycle(cycle_id: str):
    """查看账期当前结算状态：revision、账期总量/总额、各批次摘要与全部
    已合并的 UTC 用量区间（可用于重启后接续与对账）。"""
    return settlement.get_cycle(cycle_id)


@app.post("/usage-estimate", response_model=UsageEstimateResponse,
          tags=["quote"])
def post_usage_estimate(req: UsageEstimateRequest):
    """账单金额已知、电表用量缺失时，反推与账单一致的用量区间。

    假定区间内功率恒定（能量按实际 UTC 时长比例分布，与 /quote 对单段
    区间的分摊一致），在 0.000001 能量单位的离散网格上用 Decimal 二分
    分别定位"账单金额 == billed_amount"平台的左右边界；零用量视为零
    费用。无解时区分超过可达上界（amount_above_reachable）与相邻网格
    点之间的舍入跳跃（rounding_jump），并给出夹住目标的网格点。
    """
    with Session(engine) as session:
        version = load_version(session, req.version)
        result = estimate.estimate(version, req)

    def _point_out(p):
        if p is None:
            return None
        return EstimateGridPointOut(
            energy=str(p.energy.quantize(Q6)),
            amount=str(p.amount.quantize(Q2)),
        )

    return UsageEstimateResponse(
        version=version.code,
        currency=version.currency,
        energy_unit=version.energy_unit,
        billed_amount=str(result.billed_amount.quantize(Q2)),
        solvable=result.solvable,
        min_energy=(str(result.min_energy.quantize(Q6))
                    if result.min_energy is not None else None),
        max_energy=(str(result.max_energy.quantize(Q6))
                    if result.max_energy is not None else None),
        matched_rules=result.matched_rules,
        min_quote=(_quote_response(version, result.min_quote)
                   if result.min_energy is not None else None),
        max_quote=(_quote_response(version, result.max_quote)
                   if result.max_energy is not None else None),
        quotes_evaluated=result.quotes_evaluated,
        reason_code=result.reason_code,
        reason=result.reason,
        lower_neighbor=_point_out(result.lower),
        upper_neighbor=_point_out(result.upper),
    )


@app.post("/quote-diff", response_model=QuoteDiffResponse, tags=["quote"])
def post_quote_diff(req: QuoteDiffRequest):
    with Session(engine) as session:
        before = load_version(session, req.before_version)
        after = load_version(session, req.after_version)
        result = diff.quote_diff(before, after, req.intervals)

    segments = [
        DiffSegmentOut(
            interval_index=s.interval_index,
            start=s.start.isoformat().replace("+00:00", "Z"),
            end=s.end.isoformat().replace("+00:00", "Z"),
            duration_seconds=int((s.end - s.start).total_seconds()),
            before=_diff_side_out(s.before),
            after=_diff_side_out(s.after),
            energy_diff=str(s.energy_diff.quantize(Q6)),
            amount_diff=str(s.amount_diff.quantize(Q6)),
            reasons=s.reasons,
        )
        for s in result.segments
    ]
    before_total = result.before.total_amount.quantize(
        Q2, rounding=ROUND_HALF_UP)
    after_total = result.after.total_amount.quantize(
        Q2, rounding=ROUND_HALF_UP)
    return QuoteDiffResponse(
        before_version=before.code,
        after_version=after.code,
        currency=before.currency,
        energy_unit=before.energy_unit,
        total_energy=str(result.before.total_energy),
        before_total_amount=str(before_total),
        after_total_amount=str(after_total),
        total_amount_diff=str(after_total - before_total),
        before_matched_rules=result.before.matched_rules,
        after_matched_rules=result.after.matched_rules,
        segments=segments,
    )


def _diff_side_out(side: "diff.DiffSide | None") -> DiffSideOut | None:
    if side is None:
        return None
    return DiffSideOut(
        rule_code=side.rule_code,
        rule_name=side.rule_name,
        tier_ordinal=side.tier_ordinal,
        unit_price=str(side.unit_price),
        energy=str(side.energy.quantize(Q6)),
        amount=str(side.amount.quantize(Q6)),
    )


@app.post("/meter-readings/reconcile", response_model=MeterReconcileResponse,
          tags=["meter"])
def post_meter_readings_reconcile(req: MeterReconcileRequest):
    """把累计表计读数整理成可直接提交 /quote 的 UTC 半开用量区间。

    不触碰数据库；所有校验失败都返回统一的 error 响应体。
    """
    result = meter.reconcile(req)
    return MeterReconcileResponse(
        modulus=str(req.modulus),
        reading_count=result.reading_count,
        duplicate_count=result.duplicate_count,
        total_energy=str(result.total_energy),
        intervals=[
            MeterIntervalOut(
                start=meter.to_utc_z(i.start),
                end=meter.to_utc_z(i.end),
                duration_seconds=int((i.end - i.start).total_seconds()),
                from_index=i.from_index,
                to_index=i.to_index,
                wrapped=i.wrapped,
                energy=str(i.energy),
            )
            for i in result.intervals
        ],
        gaps=[
            MeterGapOut(
                start=meter.to_utc_z(g.start),
                end=meter.to_utc_z(g.end),
                duration_seconds=int((g.end - g.start).total_seconds()),
                from_index=g.from_index,
                to_index=g.to_index,
                reason=g.reason,
            )
            for g in result.gaps
        ],
    )


@app.post("/demand-charge", response_model=DemandChargeResponse,
          tags=["quote"])
def post_demand_charge(req: DemandChargeRequest):
    """按本地午夜对齐的固定分钟需量窗结算最大需量费用。

    纯计算、不触碰数据库：窗口沿 UTC 时间线递增枚举（DST 重复的本地边界
    分别成窗），能量按实际时长精确分摊；只有被输入完整覆盖的窗口参评。
    """
    result = demand.settle(req)
    tz = ZoneInfo(result.timezone)

    def _local(dt):
        return dt.astimezone(tz).isoformat()

    def _seconds(a, b):
        return int((b - a).total_seconds())

    selected = None
    if result.winner is not None:
        w = result.winner
        selected = DemandWindowOut(
            window_index=w.index,
            start=demand.to_utc_z(w.start),
            end=demand.to_utc_z(w.end),
            start_local=_local(w.start),
            end_local=_local(w.end),
            duration_seconds=_seconds(w.start, w.end),
            energy=str(w.energy.quantize(Q6)),
            average_power_kw=str(result.demand_kw),
            allocations=[
                DemandAllocationOut(
                    interval_index=a.interval_index,
                    start=demand.to_utc_z(a.start),
                    end=demand.to_utc_z(a.end),
                    start_local=_local(a.start),
                    end_local=_local(a.end),
                    duration_seconds=a.microseconds // 1_000_000,
                    energy=str(a.energy.quantize(Q6)),
                    demand_kw=str(a.demand_kw),
                    amount=str(a.amount),
                )
                for a in result.allocations
            ],
        )
    excluded = [
        ExcludedWindowOut(
            window_index=w.index,
            start=demand.to_utc_z(w.start),
            end=demand.to_utc_z(w.end),
            start_local=_local(w.start),
            end_local=_local(w.end),
            duration_seconds=int((w.end - w.start).total_seconds()),
            reasons=w.reasons,
        )
        for w in result.windows if not w.eligible
    ]
    return DemandChargeResponse(
        timezone=result.timezone,
        window_minutes=result.window_minutes,
        unit_price=str(result.unit_price),
        window_count=result.window_count,
        eligible_window_count=result.eligible_count,
        selected_window=selected,
        demand_kw=str(result.demand_kw.quantize(Q6)),
        amount=str(result.amount.quantize(Q6)),
        interval_shares=[
            DemandIntervalShareOut(
                interval_index=share.interval_index,
                energy=str(share.energy.quantize(Q6)),
                slices=[
                    DemandIntervalSliceOut(
                        window_index=s.window_index,
                        start=demand.to_utc_z(s.start),
                        end=demand.to_utc_z(s.end),
                        start_local=_local(s.start),
                        end_local=_local(s.end),
                        duration_seconds=s.microseconds // 1_000_000,
                        energy=str(s.energy.quantize(Q6)),
                    )
                    for s in share.slices
                ],
            )
            for share in result.interval_shares
        ],
        reason_code=result.reason_code,
        reason=result.reason,
        excluded_windows=excluded,
    )


@app.post("/cost-allocation", response_model=CostAllocationResponse,
          tags=["quote"])
def post_cost_allocation(req: CostAllocationRequest):
    """共享电表下多成员阶梯电费的公平拆分（Shapley 值）。

    取全部成员全部区间的 UTC 端点并集切成原子段，能量按实际时长分摊后
    在每个原子段上合并同段负荷，再复用计价引擎对所有 2^n 个联盟精确
    报价；每名成员的归属额为其全部加入顺序下边际增费的平均值。归属金额
    按输入顺序量化到六位小数，末位成员取全体费用减去前项之和，合计严格
    等于全体合并报价。
    """
    with Session(engine) as session:
        version = load_version(session, req.version)
        result = allocation.allocate(version, req)

    return CostAllocationResponse(
        version=version.code,
        currency=version.currency,
        energy_unit=version.energy_unit,
        total_energy=str(result.total_energy),
        total_amount=str(result.total_amount),
        coalitions_evaluated=result.coalitions_evaluated,
        members=[
            AllocationMemberOut(
                member_index=m.index,
                member_id=m.member_id,
                energy=str(m.energy),
                standalone_amount=str(m.standalone_amount.quantize(Q6)),
                allocated_amount=str(m.allocated_amount.quantize(Q6)),
                difference=str(m.difference.quantize(Q6)),
            )
            for m in result.members
        ],
    )


@app.post("/shift-plan", response_model=ShiftPlanResponse, tags=["quote"])
def post_shift_plan(req: ShiftPlanRequest):
    with Session(engine) as session:
        version = load_version(session, req.version)
        plan = shiftplan.plan_shift(version, req)

    if not plan.feasible:
        return ShiftPlanResponse(
            version=version.code,
            currency=version.currency,
            energy_unit=version.energy_unit,
            timezone=req.timezone,
            feasible=False,
            total_energy="0.000000",
            total_amount="0.00",
            tasks=[],
            segments=[],
            candidates_evaluated=plan.candidates_evaluated,
            non_overlapping_pairs=plan.non_overlapping_pairs,
            rule_gap_pairs=plan.rule_gap_pairs,
            reason_code=plan.reason_code,
            reason=plan.reason,
            gaps=plan.gaps or None,
        )

    starts = plan.best_starts
    tasks_out = []
    for task, start in zip(plan.tasks, starts):
        end = start + task.duration
        tasks_out.append(ShiftTaskOut(
            task_index=task.index,
            start=shiftplan.to_utc_z(start),
            end=shiftplan.to_utc_z(end),
            start_local=start.astimezone(task.tz).isoformat(),
            end_local=end.astimezone(task.tz).isoformat(),
            timezone=str(task.tz),
            duration_minutes=int(task.duration.total_seconds() // 60),
            energy=str(task.energy.quantize(Q6)),
        ))
    # interval_index == task index (payload is built in input order); the
    # field name makes task ownership explicit on each priced segment.
    segments = []
    for s in plan.result.segments:
        fields = s.to_dict(version.energy_unit, version.currency)
        fields.pop("interval_index")
        segments.append(ShiftSegmentOut(task_index=s.interval_index, **fields))
    total_amount = plan.result.total_amount.quantize(
        Q2, rounding=ROUND_HALF_UP)
    return ShiftPlanResponse(
        version=version.code,
        currency=version.currency,
        energy_unit=version.energy_unit,
        timezone=req.timezone,
        feasible=True,
        total_energy=str(plan.result.total_energy),
        total_amount=str(total_amount),
        tasks=tasks_out,
        segments=segments,
        candidates_evaluated=plan.candidates_evaluated,
        non_overlapping_pairs=plan.non_overlapping_pairs,
        rule_gap_pairs=plan.rule_gap_pairs,
    )


def _version_out(v, *, include_rules: bool) -> dict:
    return {
        "code": v.code,
        "currency": v.currency,
        "energy_unit": v.energy_unit,
        "description": v.description,
        "rules": [
            RuleOut(
                code=r.code, name=r.name,
                effective_from=r.effective_from.isoformat(),
                effective_to=r.effective_to.isoformat(),
                timezone=str(r.tz),
                weekdays=",".join(str(d) for d in sorted(r.weekdays)),
                day_scope=r.day_scope,
                time_on=r.time_on.isoformat(),
                time_off=(
                    "24:00" if r.end_of_day else r.time_off.isoformat()
                ),
                tiers=[
                    TierOut(
                        ordinal=t.ordinal,
                        cumulative_cap=(str(t.cumulative_cap)
                                        if t.cumulative_cap is not None else None),
                        unit_price=str(t.unit_price),
                    )
                    for t in r.tiers
                ],
            )
            for r in v.rules
        ],
    }
