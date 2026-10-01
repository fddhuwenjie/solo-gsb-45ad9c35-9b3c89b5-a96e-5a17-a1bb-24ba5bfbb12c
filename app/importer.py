"""完整费率版本导入：规范化、内容摘要、字段校验与单事务落库。

流程
----
1. **规范化**：规则按业务键（规则代码）排序、阶梯按序号排序、日历按日期
   排序并去重；数值、星期掩码、HH:MM、本地时间全部转为规范字符串形式。
2. **字段校验**：版本/规则代码唯一、IANA 时区有效且全版本一致、生效区间
   与日内窗口合法、holiday 日期与类型无冲突、阶梯序号与累计上限连续。
   所有错误一次性收集，每条都带 JSON 定位路径（如 ``rules[1].tiers[0]``）。
3. **结构校验**：复用 :func:`pricing.validate_version` 的规则重叠/阶梯
   缺口检查（与 ``/quote`` 前置校验同一套逻辑）。
4. **内容摘要**：对规范 JSON（键排序、无空白）取 SHA-256；列表顺序变化
   不影响摘要。
5. **落库**：单个 SQLite 事务写入版本 + 规则 + 阶梯 + 日历 + 导入登记，
   任一失败整体回滚，绝不留下半成品。同 code 同摘要重放首次响应（200），
   同 code 不同摘要拒绝（409）。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import pricing
from .database import (
    Holiday,
    RateRule,
    TariffVersion,
    Tier,
    VersionImport,
    engine,
)
from .pricing import PricingError
from .schemas import VersionImportRequest

DAY_KINDS = {"holiday", "workday"}
DAY_SCOPES = {"all", "workdays", "holidays", "non_workdays", "weekends"}
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")

# 规范化后文档中保留的字段（顺序即响应中的展示顺序）
_RULE_KEYS = ("code", "name", "effective_from", "effective_to", "timezone",
              "weekdays", "day_scope", "time_on", "time_off")


# ------------------------------------------------------------- field parsing


def _err(errors: list[dict], path: str, code: str, message: str) -> None:
    errors.append({"path": path, "code": code, "message": message})


def _canon_text(value, path: str, errors: list[dict], *, max_len: int,
                label: str) -> str | None:
    if not isinstance(value, str) or not value.strip():
        _err(errors, path, "invalid_field", f"{label}必须是非空字符串")
        return None
    text = value.strip()
    if len(text) > max_len:
        _err(errors, path, "invalid_field",
             f"{label}长度 {len(text)} 超过上限 {max_len}")
        return None
    return text


def _canon_decimal(value, path: str, errors: list[dict],
                   *, allow_none: bool = False) -> str | None:
    """数值规范形：``0.20``/``0.2``/0.2 统一为 ``"0.2"``，科学计数法展开。"""
    if value is None:
        if allow_none:
            return None
        _err(errors, path, "invalid_decimal", "缺少数值")
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        _err(errors, path, "invalid_decimal", f"数值无法解析: {value!r}")
        return None
    if not d.is_finite():
        _err(errors, path, "invalid_decimal", f"数值必须有限: {value!r}")
        return None
    if d == 0:
        return "0"  # 避免 "-0" 与 "0" 摘要不同
    return format(d.normalize(), "f")


def _canon_date(value, path: str, errors: list[dict]) -> str | None:
    try:
        return date.fromisoformat(str(value).strip()).isoformat()
    except ValueError:
        _err(errors, path, "invalid_date",
             f"日期格式无效（应为 YYYY-MM-DD）: {value!r}")
        return None


def _canon_local_datetime(value, path: str, errors: list[dict]) -> str | None:
    if not isinstance(value, str):
        _err(errors, path, "invalid_datetime", "生效时间必须是 ISO-8601 字符串")
        return None
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        _err(errors, path, "invalid_datetime", f"时间格式无效: {value!r}")
        return None
    if dt.tzinfo is not None:
        _err(errors, path, "invalid_datetime",
             "生效时间必须是无时区的本地时间（由规则的 timezone 解释）")
        return None
    return dt.isoformat()


def _canon_time(value, path: str, errors: list[dict],
                *, allow_24: bool) -> str | None:
    """日内窗口时刻规范形 ``HH:MM``；仅 ``time_off`` 允许 ``24:00``。"""
    if not isinstance(value, str):
        _err(errors, path, "invalid_time_window", "时间必须是 HH:MM 字符串")
        return None
    m = _TIME_RE.match(value.strip())
    if not m:
        _err(errors, path, "invalid_time_window",
             f"时间格式无效（应为 HH:MM）: {value!r}")
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if mm > 59 or hh > 24 or (hh == 24 and (mm != 0 or not allow_24)):
        hint = "（仅 time_off 允许 24:00）" if hh == 24 else ""
        _err(errors, path, "invalid_time_window",
             f"日内窗口时刻非法: {value!r}{hint}")
        return None
    return f"{hh:02d}:{mm:02d}"


def _canon_weekdays(value, path: str, errors: list[dict]) -> str | None:
    """星期掩码规范形：``"5,1,2"`` / ``[1,2,5]`` 统一为 ``"1,2,5"``。"""
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",") if p.strip()]
    elif isinstance(value, (list, tuple)):
        parts = list(value)
    else:
        _err(errors, path, "invalid_weekdays",
             "weekdays 必须是逗号分隔字符串或整数数组")
        return None
    days: set[int] = set()
    for p in parts:
        if isinstance(p, bool):
            _err(errors, path, "invalid_weekdays", f"星期值非法: {p!r}（只允许 1-7）")
            return None
        try:
            d = int(p)
        except (TypeError, ValueError):
            _err(errors, path, "invalid_weekdays", f"星期值非法: {p!r}（只允许 1-7）")
            return None
        if not 1 <= d <= 7:
            _err(errors, path, "invalid_weekdays", f"星期值非法: {p!r}（只允许 1-7）")
            return None
        days.add(d)
    return ",".join(str(d) for d in sorted(days))


def _canon_timezone(value, path: str, errors: list[dict]) -> str | None:
    if not isinstance(value, str) or not value.strip():
        _err(errors, path, "invalid_timezone", "时区必须是非空的 IANA 名称")
        return None
    name = value.strip()
    try:
        tz = ZoneInfo(name)
    except ZoneInfoNotFoundError:
        _err(errors, path, "invalid_timezone", f"无效 IANA 时区: {name}")
        return None
    return str(tz)


# ----------------------------------------------------------- normalization


def _canon_holidays(raw_list: list[dict], errors: list[dict]) -> list[dict]:
    items: list[dict] = []
    for i, h in enumerate(raw_list):
        path = f"holidays[{i}]"
        day = _canon_date(h.get("day"), f"{path}.day", errors)
        kind = h.get("kind") or "holiday"
        if kind not in DAY_KINDS:
            _err(errors, f"{path}.kind", "invalid_holiday_kind",
                 f"holiday 类型非法: {kind!r}（只允许 holiday / workday）")
            kind = None
        name = h.get("name") or ""
        items.append({"day": day, "kind": kind, "name": str(name), "_i": i})

    valid = [it for it in items if it["day"] and it["kind"]]
    # 日期与类型冲突：同一日期不得既是 holiday 又是 workday
    by_day: dict[str, list[dict]] = {}
    for it in valid:
        by_day.setdefault(it["day"], []).append(it)
    for day, group in by_day.items():
        if len({g["kind"] for g in group}) > 1:
            first = group[0]
            for g in group[1:]:
                _err(errors, f"holidays[{g['_i']}].kind", "holiday_conflict",
                     f"日期 {day} 的类型冲突：holidays[{first['_i']}] 为 "
                     f"{first['kind']!r}，此处为 {g['kind']!r}")
    # 业务键（日期）排序；同日期同类型的重复条目去重（保留名称字典序最小者）
    valid.sort(key=lambda g: (g["day"], g["kind"], g["name"]))
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for g in valid:
        key = (g["day"], g["kind"])
        if key in seen:
            continue
        seen.add(key)
        out.append({"day": g["day"], "kind": g["kind"], "name": g["name"]})
    return out


def _canon_tiers(raw_list: list[dict], rule_path: str, rule_label: str,
                 errors: list[dict]) -> list[dict]:
    items: list[dict] = []
    for j, t in enumerate(raw_list):
        tpath = f"{rule_path}.tiers[{j}]"
        ordinal = t.get("ordinal")
        if isinstance(ordinal, bool) or not isinstance(ordinal, int) \
                or ordinal < 1:
            _err(errors, f"{tpath}.ordinal", "tier_gap",
                 f"阶梯序号必须是 ≥1 的整数，收到 {ordinal!r}")
            ordinal = None
        cap = _canon_decimal(t.get("cumulative_cap"),
                             f"{tpath}.cumulative_cap", errors, allow_none=True)
        price = _canon_decimal(t.get("unit_price"), f"{tpath}.unit_price",
                               errors)
        if price is not None and Decimal(price) < 0:
            _err(errors, f"{tpath}.unit_price", "invalid_tier",
                 f"规则 {rule_label} 第 {ordinal or j + 1} 档单价不能为负")
        items.append({"ordinal": ordinal, "cumulative_cap": cap,
                      "unit_price": price, "_i": j})
    if not items:
        _err(errors, f"{rule_path}.tiers", "tier_gap",
             f"规则 {rule_label} 没有任何价格阶梯")
        return []

    tiers = [t for t in items if t["ordinal"] is not None]
    tiers.sort(key=lambda t: t["ordinal"])
    # 序号从 1 开始连续（重复或跳号都会在此暴露）
    for pos, t in enumerate(tiers, start=1):
        if t["ordinal"] != pos:
            _err(errors, f"{rule_path}.tiers[{t['_i']}].ordinal", "tier_gap",
                 f"规则 {rule_label} 的阶梯序号必须从 1 开始连续"
                 f"（第 {pos} 档的序号为 {t['ordinal']}）")
            break
    # 累计上限严格递增、中间档不得开放、最后一档必须开放
    prev = Decimal("0")
    for k, t in enumerate(tiers):
        cap = t["cumulative_cap"]
        last = k == len(tiers) - 1
        tpath = f"{rule_path}.tiers[{t['_i']}].cumulative_cap"
        if cap is None:
            if not last:
                _err(errors, tpath, "tier_gap",
                     f"规则 {rule_label} 第 {t['ordinal']} 档之后仍有阶梯，"
                     "但该档未设累计上限（中间档位不得开放）")
            continue
        d = Decimal(cap)
        if d <= 0:
            _err(errors, tpath, "tier_gap",
                 f"规则 {rule_label} 第 {t['ordinal']} 档累计上限必须为正")
        elif d <= prev:
            _err(errors, tpath, "tier_gap",
                 f"规则 {rule_label} 第 {t['ordinal']} 档累计上限 {cap} "
                 f"必须大于前一档上限 {prev}")
        if last:
            _err(errors, tpath, "tier_gap",
                 f"规则 {rule_label} 的最后一档必须开放（cumulative_cap 为空），"
                 "否则超出上限的用量无价格可依")
        prev = d
    return [{"ordinal": t["ordinal"], "cumulative_cap": t["cumulative_cap"],
             "unit_price": t["unit_price"]} for t in tiers]


def _canon_rules(raw_list: list[dict], errors: list[dict]) -> list[dict]:
    items: list[dict] = []
    for i, r in enumerate(raw_list):
        path = f"rules[{i}]"
        code = _canon_text(r.get("code"), f"{path}.code", errors,
                           max_len=64, label="规则代码")
        label = code or f"#{i}"
        eff_from = _canon_local_datetime(r.get("effective_from"),
                                         f"{path}.effective_from", errors)
        eff_to = _canon_local_datetime(r.get("effective_to"),
                                       f"{path}.effective_to", errors)
        if eff_from and eff_to and eff_to <= eff_from:
            _err(errors, f"{path}.effective_to", "invalid_effective_range",
                 f"规则 {label} 的生效区间无效：结束必须晚于开始（半开区间）")
        scope = r.get("day_scope") or "all"
        if scope not in DAY_SCOPES:
            _err(errors, f"{path}.day_scope", "invalid_day_scope",
                 f"规则 {label} 的 day_scope 非法: {scope!r}（允许 "
                 "all / workdays / holidays / non_workdays / weekends）")
        items.append({
            "code": code,
            "name": str(r.get("name") or ""),
            "effective_from": eff_from,
            "effective_to": eff_to,
            "timezone": _canon_timezone(r.get("timezone"),
                                        f"{path}.timezone", errors),
            "weekdays": _canon_weekdays(r.get("weekdays"),
                                        f"{path}.weekdays", errors),
            "day_scope": scope,
            "time_on": _canon_time(r.get("time_on"), f"{path}.time_on",
                                   errors, allow_24=False),
            "time_off": _canon_time(r.get("time_off"), f"{path}.time_off",
                                    errors, allow_24=True),
            "tiers": _canon_tiers(r.get("tiers") or [], path, label, errors),
            "_i": i,
        })

    # 规则代码唯一（业务键）
    seen: dict[str, int] = {}
    for it in items:
        c = it["code"]
        if c is None:
            continue
        if c in seen:
            _err(errors, f"rules[{it['_i']}].code", "duplicate_rule_code",
                 f"规则代码 {c!r} 与 rules[{seen[c]}] 重复；"
                 "同一版本内规则代码必须唯一")
        else:
            seen[c] = it["_i"]

    # IANA 时区一致：同一版本内所有规则必须共用同一时区
    zones = [it["timezone"] for it in items if it["timezone"]]
    if zones:
        base = zones[0]
        for it in items[1:]:
            if it["timezone"] and it["timezone"] != base:
                _err(errors, f"rules[{it['_i']}].timezone", "timezone_mismatch",
                     f"规则 {it['code'] or it['_i']} 的时区 "
                     f"{it['timezone']!r} 与版本基准时区 {base!r} 不一致；"
                     "同一版本内所有规则必须使用同一 IANA 时区")

    # 业务键（规则代码）排序，使列表顺序不影响摘要
    items.sort(key=lambda it: (it["code"] is None, it["code"] or ""))
    return [{k: it[k] for k in _RULE_KEYS} | {"tiers": it["tiers"]}
            for it in items]


def normalize_raw(raw: dict) -> tuple[dict, list[dict]]:
    """把原始字典（请求或数据库行）规范化，返回 ``(规范文档, 错误列表)``。

    规范文档的列表已按业务键排序、数值与时间已转为规范字符串，可直接
    序列化取摘要；错误列表非空时文档不可用。
    """
    errors: list[dict] = []
    code = _canon_text(raw.get("code"), "code", errors,
                       max_len=64, label="版本号")
    currency = _canon_text(raw.get("currency"), "currency", errors,
                           max_len=8, label="币种")
    energy_unit = _canon_text(raw.get("energy_unit"), "energy_unit", errors,
                              max_len=16, label="能量单位")
    description = raw.get("description") or ""
    holidays = _canon_holidays(raw.get("holidays") or [], errors)
    rules = _canon_rules(raw.get("rules") or [], errors)
    if not raw.get("rules"):
        _err(errors, "rules", "empty_rules", "版本至少需要一条规则")
    doc = {
        "code": code or "",
        "currency": currency or "",
        "energy_unit": energy_unit or "",
        "description": str(description),
        "holidays": holidays,
        "rules": rules,
    }
    return doc, errors


def _raw_from_request(req: VersionImportRequest) -> dict:
    return {
        "code": req.code,
        "currency": req.currency,
        "energy_unit": req.energy_unit,
        "description": req.description,
        "holidays": [{"day": h.day, "kind": h.kind, "name": h.name}
                     for h in req.holidays],
        "rules": [{
            "code": r.code, "name": r.name,
            "effective_from": r.effective_from, "effective_to": r.effective_to,
            "timezone": r.timezone, "weekdays": r.weekdays,
            "day_scope": r.day_scope, "time_on": r.time_on,
            "time_off": r.time_off,
            "tiers": [{"ordinal": t.ordinal, "cumulative_cap": t.cumulative_cap,
                       "unit_price": t.unit_price} for t in r.tiers],
        } for r in req.rules],
    }


def _raw_from_row(v: TariffVersion) -> dict:
    return {
        "code": v.code,
        "currency": v.currency,
        "energy_unit": v.energy_unit,
        "description": v.description or "",
        "holidays": [{"day": h.day.isoformat(), "kind": h.kind,
                      "name": h.name or ""} for h in v.holidays],
        "rules": [{
            "code": r.code, "name": r.name,
            "effective_from": r.effective_from, "effective_to": r.effective_to,
            "timezone": r.timezone, "weekdays": r.weekdays,
            "day_scope": r.day_scope, "time_on": r.time_on,
            "time_off": r.time_off,
            "tiers": [{"ordinal": t.ordinal, "cumulative_cap": t.cumulative_cap,
                       "unit_price": t.unit_price} for t in r.tiers],
        } for r in v.rules],
    }


# ------------------------------------------------------------------- digest


def canonical_json(doc: dict) -> str:
    """规范 JSON：键排序、无空白、UTF-8；列表顺序已在规范化时固定。"""
    return json.dumps(doc, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def content_digest(doc: dict) -> str:
    return hashlib.sha256(canonical_json(doc).encode("utf-8")).hexdigest()


# --------------------------------------------------------------- persistence


def _rows_from_doc(doc: dict) -> TariffVersion:
    """由规范文档构建（暂态的）ORM 对象树；加入会话即整体落库。"""
    version = TariffVersion(
        code=doc["code"], currency=doc["currency"],
        energy_unit=doc["energy_unit"], description=doc["description"],
    )
    for h in doc["holidays"]:
        version.holidays.append(
            Holiday(day=date.fromisoformat(h["day"]), kind=h["kind"],
                    name=h["name"]))
    for r in doc["rules"]:
        rule = RateRule(
            code=r["code"], name=r["name"],
            effective_from=r["effective_from"],
            effective_to=r["effective_to"],
            timezone=r["timezone"], weekdays=r["weekdays"],
            day_scope=r["day_scope"], time_on=r["time_on"],
            time_off=r["time_off"],
        )
        for t in r["tiers"]:
            rule.tiers.append(
                Tier(ordinal=t["ordinal"], cumulative_cap=t["cumulative_cap"],
                     unit_price=t["unit_price"]))
        version.rules.append(rule)
    return version


def _loaded_from_rows(version: TariffVersion) -> pricing.LoadedVersion:
    """复用引擎的规则解析，把 ORM 对象树转成引擎的只读模型。"""
    return pricing.LoadedVersion(
        code=version.code, currency=version.currency,
        energy_unit=version.energy_unit,
        description=version.description or "",
        rules=tuple(pricing._parse_rule(r) for r in version.rules),
        holidays={h.day: h.kind for h in version.holidays},
        holiday_names={h.day: h.name for h in version.holidays},
    )


def _response_body(doc: dict, digest: str) -> dict:
    return {
        "code": doc["code"],
        "content_digest": digest,
        "currency": doc["currency"],
        "energy_unit": doc["energy_unit"],
        "description": doc["description"],
        "holidays": doc["holidays"],
        "rules": doc["rules"],
    }


def _existing_import(session: Session, code: str) -> tuple[str, dict] | None:
    """已存在的同 code 记录：``(摘要, 响应体)``；不存在返回 None。

    没有导入登记的版本（如预置数据）按同一规范化流程现算摘要。
    """
    row = session.get(VersionImport, code)
    if row is not None:
        return row.content_digest, json.loads(row.response_json)
    version = session.get(TariffVersion, code)
    if version is None:
        return None
    doc, _ = normalize_raw(_raw_from_row(version))
    digest = content_digest(doc)
    return digest, _response_body(doc, digest)


def _conflict(code: str, existing_digest: str, received_digest: str
              ) -> PricingError:
    return PricingError(
        f"版本 {code} 已存在且内容摘要不同（已存在 {existing_digest}，"
        f"本次 {received_digest}）；版本内容不可变，请更换版本号",
        code="version_conflict", status_code=409,
        details={"path": "code", "code": code,
                 "existing_digest": existing_digest,
                 "received_digest": received_digest},
    )


def import_version(req: VersionImportRequest) -> tuple[dict, bool]:
    """导入一个完整版本，返回 ``(响应体, 是否新建)``。

    重放（同 code 同摘要）返回 ``(首次响应体, False)``；所有校验/冲突
    错误以 :class:`PricingError` 抛出，且不会写入任何数据。
    """
    doc, errors = normalize_raw(_raw_from_request(req))
    if errors:
        raise PricingError(
            f"导入内容存在 {len(errors)} 处字段错误，未写入任何数据",
            code="invalid_import", status_code=422,
            details={"errors": errors},
        )

    # 复用引擎的结构校验（规则重叠 / 阶梯缺口），与 /quote 前置校验一致
    version_row = _rows_from_doc(doc)
    issues = pricing.validate_version(_loaded_from_rows(version_row))
    if issues:
        index = {r["code"]: i for i, r in enumerate(doc["rules"])}
        for issue in issues:
            if "rule" in issue:
                issue["rule_index"] = index.get(issue["rule"])
            if "rules" in issue:
                issue["rule_indices"] = [index.get(c) for c in issue["rules"]]
        raise PricingError(
            f"版本 {doc['code']} 存在 {len(issues)} 处结构问题"
            "（时间重叠或阶梯缺口），未写入任何数据",
            code="rule_conflict", status_code=409, details=issues,
        )

    digest = content_digest(doc)
    body = _response_body(doc, digest)

    try:
        with Session(engine) as session:
            with session.begin():  # 单个 SQLite 事务：全部层级一起提交
                existing = _existing_import(session, doc["code"])
                if existing is not None:
                    old_digest, old_body = existing
                    if old_digest == digest:
                        return old_body, False  # 幂等重放：返回原结果
                    raise _conflict(doc["code"], old_digest, digest)
                session.add(version_row)
                session.add(VersionImport(
                    version_code=doc["code"], content_digest=digest,
                    response_json=json.dumps(body, ensure_ascii=False),
                ))
    except IntegrityError as exc:
        # 并发下同 code 被抢先提交：按重放/冲突重新归类
        with Session(engine) as session:
            existing = _existing_import(session, doc["code"])
        if existing is not None and existing[0] == digest:
            return existing[1], False
        raise _conflict(doc["code"],
                        existing[0] if existing else "<unknown>",
                        digest) from exc
    return body, True
