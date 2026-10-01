"""Pydantic request/response models."""
from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class QuoteIntervalIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: str = Field(
        ..., description="ISO-8601 开始时间；建议携带偏移量（如 2024-03-10T01:00:00-05:00）"
    )
    end: str = Field(..., description="ISO-8601 结束时间，半开区间 [start, end)")
    timezone: str = Field(
        ..., description="解释朴素时间所用的 IANA 时区，如 America/New_York"
    )
    energy: Decimal = Field(..., gt=0, description="该区间累计用量（如 kWh）")

    def energy_decimal(self) -> Decimal:
        return self.energy


class QuoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = Field(..., description="规则版本号，如 us-ny-demo-v2024")
    intervals: list[QuoteIntervalIn] = Field(..., min_length=1)


class QuoteDiffRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    before_version: str = Field(..., description="基准规则版本号")
    after_version: str = Field(..., description="对比目标规则版本号")
    intervals: list[QuoteIntervalIn] = Field(
        ..., min_length=1, description="与 /quote 相同的用量区间格式"
    )


class DiffSideOut(BaseModel):
    rule_code: str
    rule_name: str
    tier_ordinal: int
    unit_price: str
    energy: str
    amount: str


class DiffSegmentOut(BaseModel):
    interval_index: int
    start: str
    end: str
    duration_seconds: int
    before: DiffSideOut | None
    after: DiffSideOut | None
    energy_diff: str
    amount_diff: str
    reasons: list[str]


class QuoteDiffResponse(BaseModel):
    before_version: str
    after_version: str
    currency: str
    energy_unit: str
    total_energy: str
    before_total_amount: str
    after_total_amount: str
    total_amount_diff: str
    before_matched_rules: list[str]
    after_matched_rules: list[str]
    segments: list[DiffSegmentOut]


class SegmentOut(BaseModel):
    interval_index: int
    start: str
    end: str
    start_local: str
    end_local: str
    timezone: str
    duration_seconds: int
    day_kind: str
    energy: str
    energy_unit: str
    rule_code: str
    rule_name: str
    tier_ordinal: int
    cumulative_from: str
    cumulative_to: str
    unit_price: str
    currency: str
    amount: str


class QuoteResponse(BaseModel):
    version: str
    currency: str
    energy_unit: str
    total_energy: str
    total_amount: str
    matched_rules: list[str]
    segments: list[SegmentOut]


class TierOut(BaseModel):
    ordinal: int
    cumulative_cap: str | None
    unit_price: str


class RuleOut(BaseModel):
    code: str
    name: str
    effective_from: str
    effective_to: str
    timezone: str
    weekdays: str
    day_scope: str
    time_on: str
    time_off: str
    tiers: list[TierOut]


class VersionOut(BaseModel):
    code: str
    currency: str
    energy_unit: str
    description: str
    rules: list[RuleOut]


# ------------------------------------------------------------- cost allocation


class AllocationMemberIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    member_id: str = Field(
        ..., min_length=1,
        description="用能方唯一标识（非空字符串），成员之间不得重复",
    )
    intervals: list[QuoteIntervalIn] = Field(
        ..., min_length=1,
        description="与 /quote 相同的用量区间格式；同一成员内部区间不得"
        "重叠，不同成员之间允许重叠",
    )


class CostAllocationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = Field(..., description="规则版本号，如 us-ny-demo-v2024")
    members: list[AllocationMemberIn] = Field(
        ..., min_length=2, max_length=6,
        description="2～6 名共享电表后的用能方，按输入顺序编号；归属金额"
        "也按此顺序量化，最后一名吸收舍入残差",
    )


class AllocationMemberOut(BaseModel):
    member_index: int = Field(..., description="成员在请求 members 中的下标（0 起）")
    member_id: str
    energy: str = Field(
        ..., description="该成员申报用量（精确值），等于其各原子段分摊能量之和"
    )
    standalone_amount: str = Field(
        ..., description="独立报价 v({i})：仅该成员用能时的费用（六位小数）"
    )
    allocated_amount: str = Field(
        ..., description="Shapley 归属金额（六位小数）；全体成员之和严格"
        "等于 total_amount"
    )
    difference: str = Field(
        ..., description="standalone_amount − allocated_amount（六位小数）；"
        "正值表示合并后少付，负值表示为阶梯叠加承担更多"
    )


class CostAllocationResponse(BaseModel):
    version: str
    currency: str
    energy_unit: str
    total_energy: str = Field(
        ..., description="全体成员用量之和，等于合并报价的总用量"
    )
    total_amount: str = Field(
        ..., description="全体成员合并报价 v(N)（六位小数）"
    )
    coalitions_evaluated: int = Field(
        ..., description="枚举的联盟总数 2^n（含费用为 0 的空联盟）"
    )
    members: list[AllocationMemberOut]


# ------------------------------------------------------------- version import


class HolidayIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day: str = Field(..., description="ISO 日期，如 2024-01-01")
    kind: str = Field(
        "holiday", description="holiday=法定假日；workday=调休上班日"
    )
    name: str = Field("", description="节日名称（可选）")


class TierIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ordinal: int = Field(..., description="阶梯序号，从 1 开始连续编号")
    cumulative_cap: Decimal | None = Field(
        None, description="累计用量上限；最后一档必须为 null（开放）"
    )
    unit_price: Decimal = Field(..., description="单价（每能量单位，不得为负）")


class RuleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(..., description="规则代码，版本内唯一")
    name: str = ""
    effective_from: str = Field(
        ..., description="生效起点（规则时区的本地朴素时间，ISO-8601）"
    )
    effective_to: str = Field(
        ..., description="生效终点（本地朴素时间，半开区间）"
    )
    timezone: str = Field(
        ..., description="IANA 时区；同一版本内所有规则必须一致"
    )
    weekdays: str | list[int] = Field(
        "", description="ISO 星期掩码：逗号分隔字符串（如 \"1,2,3\"）或整数数组；空=每天"
    )
    day_scope: str = Field(
        "all", description="all / workdays / holidays / non_workdays / weekends"
    )
    time_on: str = Field(..., description="日内窗口起点 HH:MM")
    time_off: str = Field(
        ..., description="日内窗口终点 HH:MM（半开；可用 24:00 表示日末；"
        "off <= on 表示跨午夜）"
    )
    tiers: list[TierIn] = Field(default_factory=list)


class VersionImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(..., description="版本号，全局唯一且内容不可变")
    currency: str = Field(..., description="币种，如 USD / CNY")
    energy_unit: str = Field(..., description="能量单位，如 kWh")
    description: str = ""
    holidays: list[HolidayIn] = Field(default_factory=list)
    rules: list[RuleIn] = Field(default_factory=list)


class HolidayOut(BaseModel):
    day: str
    kind: str
    name: str


class VersionImportResponse(BaseModel):
    code: str
    content_digest: str = Field(
        ..., description="规范化内容的 SHA-256（十六进制）；列表顺序不影响摘要"
    )
    currency: str
    energy_unit: str
    description: str
    holidays: list[HolidayOut]
    rules: list[RuleOut]


class ShiftTaskIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    earliest_start: str = Field(
        ...,
        description="最早可开机的 ISO-8601 时刻，必须携带 UTC 偏移量（如 "
        "2024-07-06T20:00:00-04:00 或 2024-07-07T00:00:00Z）",
    )
    latest_end: str = Field(
        ...,
        description="最晚完工的 ISO-8601 时刻，必须携带 UTC 偏移量；"
        "任务须在此时刻之前结束（半开区间）",
    )
    duration_minutes: int = Field(
        ..., gt=0, description="任务连续运行时长（整数分钟）"
    )
    energy: Decimal = Field(..., gt=0, description="该任务的正用量（如 kWh）")

    @field_validator("duration_minutes", mode="before")
    @classmethod
    def _reject_non_integer(cls, value):
        # bool is an int subclass; JSON 60.0 would also be coerced silently.
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("duration_minutes 必须是整数分钟")
        return value


class ShiftPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = Field(..., description="规则版本号，如 us-ny-demo-v2024")
    timezone: str = Field(
        ..., description="回显本地时间所用的 IANA 时区，如 America/New_York"
    )
    tasks: list[ShiftTaskIn] = Field(
        ...,
        min_length=2,
        max_length=2,
        description="恰好两项可移时用电任务，按输入顺序编号为 0、1",
    )


class ShiftTaskOut(BaseModel):
    task_index: int
    start: str
    end: str
    start_local: str
    end_local: str
    timezone: str
    duration_minutes: int
    energy: str


class ShiftSegmentOut(BaseModel):
    # task_index 标明该计价段归属哪一项任务（即输入顺序 0/1）
    task_index: int
    start: str
    end: str
    start_local: str
    end_local: str
    timezone: str
    duration_seconds: int
    day_kind: str
    energy: str
    energy_unit: str
    rule_code: str
    rule_name: str
    tier_ordinal: int
    cumulative_from: str
    cumulative_to: str
    unit_price: str
    currency: str
    amount: str


class ShiftPlanResponse(BaseModel):
    version: str
    currency: str
    energy_unit: str
    timezone: str
    feasible: bool
    total_energy: str
    total_amount: str
    tasks: list[ShiftTaskOut]
    segments: list[ShiftSegmentOut]
    candidates_evaluated: int
    non_overlapping_pairs: int
    rule_gap_pairs: int
    reason_code: str | None = Field(
        None, description="无可行组合时的机器可读原因"
    )
    reason: str | None = Field(None, description="无可行组合时的说明")
    gaps: list[dict] | None = Field(
        None, description="被规则缺口淘汰的候选组合示例（最多 5 条）"
    )


# ---------------------------------------------------------- meter reconcile


class MeterReadingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    at: str = Field(
        ..., description="读数时刻（ISO-8601），必须携带 UTC 偏移量（如 Z / +08:00）"
    )
    value: Decimal = Field(
        ..., allow_inf_nan=False,
        description="累计计数器读数，必须落在 [0, modulus) 内",
    )


class MeterReconcileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    readings: list[MeterReadingIn] = Field(
        ..., min_length=2, max_length=500,
        description="2~500 条累计读数；顺序任意，服务端按 UTC 时刻排序处理",
    )
    modulus: Decimal = Field(
        ..., gt=0, allow_inf_nan=False,
        description="计数器模数（正数）：读数达到 modulus 后回卷为 0",
    )
    max_interval_seconds: int = Field(
        ..., gt=0,
        description="最大采样间隔（正整数秒）；相邻读数间隔超过它只记为 gap",
    )
    resets: list[str] = Field(
        default_factory=list,
        description="可选的计数器 reset 时刻（ISO-8601，必须携带 UTC 偏移量）；"
        "每个 reset 都必须命中某条读数，命中处序列断开",
    )

    @field_validator("max_interval_seconds", mode="before")
    @classmethod
    def _reject_non_integer(cls, value):
        # bool is an int subclass; JSON 60.0 would also be coerced silently.
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("max_interval_seconds 必须是正整数秒")
        return value


class MeterIntervalOut(BaseModel):
    start: str = Field(..., description="UTC 半开区间起点（Z 后缀）")
    end: str = Field(..., description="UTC 半开区间终点（Z 后缀）")
    duration_seconds: int
    from_index: int = Field(..., description="起点读数在请求 readings 中的原始下标")
    to_index: int = Field(..., description="终点读数在请求 readings 中的原始下标")
    wrapped: bool = Field(..., description="是否按一次 modulus 回卷计算")
    energy: str = Field(..., description="该区间精确用量（未舍入）")


class MeterGapOut(BaseModel):
    start: str
    end: str
    duration_seconds: int
    from_index: int
    to_index: int
    reason: str = Field(..., description="reset | max_interval_exceeded")


class MeterReconcileResponse(BaseModel):
    modulus: str
    reading_count: int = Field(..., description="去重后的读数条数")
    duplicate_count: int = Field(..., description="被去重丢弃的重复读数条数")
    total_energy: str = Field(
        ..., description="全部区间用量之和（精确值），等于各连续段净增量之和"
    )
    intervals: list[MeterIntervalOut]
    gaps: list[MeterGapOut]


# ------------------------------------------------------------- demand charge


class DemandChargeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timezone: str = Field(
        ..., description="窗口对齐所用的 IANA 时区，如 America/New_York"
    )
    window_minutes: int = Field(
        ..., description="需量窗固定时长（分钟），仅允许 15 / 30 / 60"
    )
    unit_price: Decimal = Field(
        ..., gt=0, allow_inf_nan=False,
        description="最大需量正单价（每 kW，按六位小数需量计费）",
    )
    intervals: list[QuoteIntervalIn] = Field(
        ..., min_length=1,
        description="与 /quote 相同的用量区间格式（可不等长、不重叠、"
        "首尾相接允许）；能量按实际时长精确分摊到各窗口",
    )

    @field_validator("window_minutes")
    @classmethod
    def _allowed_window(cls, value):
        if value not in (15, 30, 60):
            raise ValueError("window_minutes 仅允许 15 / 30 / 60")
        return value


class DemandAllocationOut(BaseModel):
    interval_index: int = Field(
        ..., description="获选窗口内该分摊片段所属输入区间的原始下标"
    )
    start: str = Field(..., description="片段 UTC 起点（Z 后缀）")
    end: str = Field(..., description="片段 UTC 终点（Z 后缀）")
    start_local: str
    end_local: str
    duration_seconds: int
    energy: str = Field(..., description="按实际时长分摊到该输入的能量（六位小数）")
    demand_kw: str = Field(..., description="该分摊片段对应的平均功率（六位小数）")
    amount: str = Field(
        ..., description="按能量占比分摊的费用（六位小数；合计严格等于费用）"
    )


class DemandWindowOut(BaseModel):
    window_index: int
    start: str = Field(..., description="窗口 UTC 起点（Z 后缀）")
    end: str = Field(..., description="窗口 UTC 终点（Z 后缀）")
    start_local: str
    end_local: str
    duration_seconds: int
    energy: str = Field(..., description="窗口内各输入分摊能量之和（六位小数）")
    average_power_kw: str = Field(..., description="窗口平均功率 kW（六位小数）")
    allocations: list[DemandAllocationOut]


class DemandIntervalSliceOut(BaseModel):
    window_index: int = Field(
        ..., description="切片所属需量窗口的编号（与 selected_window / "
        "excluded_windows 的 window_index 一致）"
    )
    start: str = Field(..., description="切片 UTC 起点（Z 后缀）")
    end: str = Field(..., description="切片 UTC 终点（Z 后缀）")
    start_local: str
    end_local: str
    duration_seconds: int
    energy: str = Field(
        ..., description="该输入按实际时长分摊到该窗口的能量（六位小数）"
    )


class DemandIntervalShareOut(BaseModel):
    interval_index: int = Field(..., description="原始输入区间的下标")
    energy: str = Field(
        ..., description="该输入全部切片能量之和，严格等于其申报能量"
    )
    slices: list[DemandIntervalSliceOut] = Field(
        ..., description="该输入在全部重叠窗口中的切片，按 UTC 时间递增"
    )


class ExcludedWindowOut(BaseModel):
    window_index: int
    start: str
    end: str
    start_local: str
    end_local: str
    duration_seconds: int
    reasons: list[str] = Field(
        ...,
        description="未参评原因：partial_coverage=仅被输入部分覆盖；"
        "not_covered=完全落在输入缺口/边缘；incomplete_alignment="
        "本地日 UTC 跨度不足一个完整窗口的碎片",
    )


class DemandChargeResponse(BaseModel):
    timezone: str
    window_minutes: int
    unit_price: str
    window_count: int = Field(..., description="与输入范围相交的对齐窗口总数")
    eligible_window_count: int = Field(..., description="被完整覆盖、参评的窗口数")
    selected_window: DemandWindowOut | None = Field(
        None, description="平均功率最高的窗口；同值取 UTC 起点最早者；"
        "无完整窗口时为 null"
    )
    demand_kw: str = Field(..., description="计费需量 kW（六位小数四舍五入）")
    amount: str = Field(..., description="需量费用（六位小数四舍五入）")
    interval_shares: list[DemandIntervalShareOut] = Field(
        ..., description="逐输入在全部重叠窗口中的切片（按 interval_index "
        "分组，与获选窗口无关）；每组能量之和严格等于该输入的申报能量"
    )
    reason_code: str | None = Field(
        None, description="无完整参评窗口时为 no_complete_window"
    )
    reason: str | None
    excluded_windows: list[ExcludedWindowOut]


# ------------------------------------------------------------ usage estimate


class UsageEstimateIntervalIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: str = Field(
        ..., description="ISO-8601 开始时间；建议携带偏移量（如 2024-07-06T00:00:00-04:00）"
    )
    end: str = Field(..., description="ISO-8601 结束时间，半开区间 [start, end)")
    timezone: str = Field(
        ..., description="解释朴素时间所用的 IANA 时区，如 America/New_York"
    )


class UsageEstimateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = Field(..., description="规则版本号，如 us-ny-demo-v2024")
    interval: UsageEstimateIntervalIn = Field(
        ..., description="单段时间区间（与 /quote 区间同格式，但不含 energy）"
    )
    billed_amount: Decimal = Field(
        ..., ge=0, allow_inf_nan=False, decimal_places=2,
        description="账单金额：非负、最多两位小数（已舍入到分的金额）",
    )
    max_energy: Decimal = Field(
        ..., gt=0, le=1000000, allow_inf_nan=False, decimal_places=6,
        description="用量搜索上界：0 < max_energy ≤ 1000000，最多六位小数",
    )


class EstimateGridPointOut(BaseModel):
    energy: str = Field(..., description="网格用量（六位小数）")
    amount: str = Field(..., description="该用量对应的账单金额（两位小数）")


class UsageEstimateResponse(BaseModel):
    version: str
    currency: str
    energy_unit: str
    billed_amount: str = Field(..., description="回显目标账单金额（两位小数）")
    solvable: bool = Field(
        ..., description="是否存在使账单金额等于目标的网格用量"
    )
    min_energy: str | None = Field(
        None, description="有解时：目标金额平台的左边界（最小用量，六位小数）"
    )
    max_energy: str | None = Field(
        None, description="有解时：目标金额平台的右边界（最大用量，六位小数，"
        "不超过请求的 max_energy 上界）"
    )
    matched_rules: list[str] = Field(
        default_factory=list, description="有解时：两端报价命中规则的并集"
    )
    min_quote: QuoteResponse | None = Field(
        None, description="有解时：最小用量端的完整报价（与 /quote 同结构；"
        "零用量端为零费用、无计价段的空报价）"
    )
    max_quote: QuoteResponse | None = Field(
        None, description="有解时：最大用量端的完整报价（与 /quote 同结构）"
    )
    quotes_evaluated: int = Field(
        ..., description="二分过程中实际执行的报价次数"
    )
    reason_code: str | None = Field(
        None, description="无解时的机器可读原因：amount_above_reachable / "
        "rounding_jump"
    )
    reason: str | None = Field(None, description="无解时的说明")
    lower_neighbor: EstimateGridPointOut | None = Field(
        None, description="无解时：夹住目标金额的下侧相邻网格点及其账单金额"
    )
    upper_neighbor: EstimateGridPointOut | None = Field(
        None, description="无解时：夹住目标金额的上侧相邻网格点及其账单金额"
        "（amount_above_reachable 时为 null）"
    )


# ------------------------------------------------------------- settlement


class SettlementBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = Field(
        ..., description="规则版本号；账期首次提交时锁定，后续批次必须一致"
    )
    idempotency_key: str = Field(
        ..., min_length=1, max_length=128,
        description="幂等键：同一账期内同键同内容重放返回首次响应",
    )
    expected_revision: int = Field(
        ..., ge=0,
        description="客户端已知的当前 revision；首次必须为 0，之后为上一次"
        "响应的 revision；用于并发乐观锁",
    )
    intervals: list[QuoteIntervalIn] = Field(
        ..., min_length=1,
        description="本批用量区间，与 /quote 完全同格式；账期内全部区间"
        "（含历史批次）按 UTC 合并且禁止重叠",
    )


class SettlementBatchResponse(BaseModel):
    cycle_id: str = Field(..., description="账期 ID（路径参数回显）")
    version: str = Field(..., description="账期锁定的规则版本号")
    currency: str
    energy_unit: str
    revision: int = Field(
        ..., description="本次提交成功后的新 revision（首次为 1，逐批加一）"
    )
    batch_amount: str = Field(
        ..., description="本批金额：账期整体重算后累计金额 − 重算前累计金额，"
        "两位小数（到分）"
    )
    total_energy: str = Field(..., description="账期全部批次的累计用量（精确值）")
    total_amount: str = Field(..., description="账期累计总额，两位小数（到分）")
    quote: QuoteResponse = Field(
        ..., description="账期全部区间合并后用计价引擎整体重算的完整报价"
    )


# ------------------------------------------------------------------ backup


class BackupResponse(BaseModel):
    backup_id: str = Field(..., description="备份 ID（路径参数回显）")
    filename: str = Field(..., description="备份目录中已原子发布的文件名")
    download_url: str = Field(
        ..., description="可下载副本的相对地址（GET 即返回 SQLite 文件）"
    )
    size_bytes: int = Field(..., description="已发布副本的字节数")
    sha256: str = Field(
        ..., description="副本文件的 SHA-256（十六进制），供离线核对"
    )
    created_at: str = Field(..., description="发布时间（UTC ISO-8601）")

