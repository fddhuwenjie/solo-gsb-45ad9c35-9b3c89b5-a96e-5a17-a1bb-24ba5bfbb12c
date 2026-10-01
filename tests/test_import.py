"""End-to-end tests for POST /versions/import (in-memory SQLite)."""
import copy
import os
import re
from decimal import Decimal

import pytest

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)


@pytest.fixture(scope="session", autouse=True)
def _startup():
    with client:
        yield


def _payload(code="import-demo-v1", **over):
    """A fully-covering, structurally valid two-rule version."""
    base = {
        "code": code,
        "currency": "USD",
        "energy_unit": "kWh",
        "description": "导入演示版本",
        "holidays": [
            {"day": "2024-07-04", "kind": "holiday",
             "name": "Independence Day"},
            {"day": "2024-12-25", "kind": "holiday", "name": "Christmas"},
        ],
        "rules": [
            {
                "code": "day",
                "name": "白天",
                "effective_from": "2024-01-01T00:00:00",
                "effective_to": "2025-01-01T00:00:00",
                "timezone": "America/New_York",
                "weekdays": "",
                "day_scope": "all",
                "time_on": "08:00",
                "time_off": "20:00",
                "tiers": [
                    {"ordinal": 1, "cumulative_cap": "100",
                     "unit_price": "0.20"},
                    {"ordinal": 2, "cumulative_cap": None,
                     "unit_price": "0.30"},
                ],
            },
            {
                "code": "night",
                "name": "夜间",
                "effective_from": "2024-01-01T00:00:00",
                "effective_to": "2025-01-01T00:00:00",
                "timezone": "America/New_York",
                "weekdays": "",
                "day_scope": "all",
                "time_on": "20:00",
                "time_off": "08:00",
                "tiers": [
                    {"ordinal": 1, "cumulative_cap": None,
                     "unit_price": "0.10"},
                ],
            },
        ],
    }
    base.update(over)
    return base


def _import(payload):
    return client.post("/versions/import", json=payload)


# ------------------------------------------------------------------ success


def test_import_created_and_immediately_usable():
    r = _import(_payload())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["code"] == "import-demo-v1"
    assert re.fullmatch(r"[0-9a-f]{64}", body["content_digest"])
    # 规范化：规则按代码排序、日历按日期排序
    assert [rule["code"] for rule in body["rules"]] == ["day", "night"]
    assert [h["day"] for h in body["holidays"]] == ["2024-07-04", "2024-12-25"]

    # 立即可通过既有接口读取
    r = client.get("/versions/import-demo-v1")
    assert r.status_code == 200
    assert [rule["code"] for rule in r.json()["rules"]] == ["day", "night"]
    r = client.get("/versions/import-demo-v1/issues")
    assert r.json()["ok"] is True

    # 立即可报价：周一 10:00-12:00（纽约）命中 day 规则一档
    r = client.post("/quote", json={
        "version": "import-demo-v1",
        "intervals": [{
            "start": "2024-06-03T10:00:00-04:00",
            "end": "2024-06-03T12:00:00-04:00",
            "timezone": "America/New_York",
            "energy": "10",
        }],
    })
    assert r.status_code == 200, r.text
    quote = r.json()
    assert quote["matched_rules"] == ["day"]
    assert Decimal(quote["total_amount"]) == Decimal("2.00")

    # 立即可做版本对比
    r = client.post("/quote-diff", json={
        "before_version": "import-demo-v1",
        "after_version": "import-demo-v1",
        "intervals": [{
            "start": "2024-06-03T10:00:00-04:00",
            "end": "2024-06-03T12:00:00-04:00",
            "timezone": "America/New_York",
            "energy": "10",
        }],
    })
    assert r.status_code == 200, r.text
    assert Decimal(r.json()["total_amount_diff"]) == 0

    # 立即可排程（两项任务都在白天窗口内）
    r = client.post("/shift-plan", json={
        "version": "import-demo-v1",
        "timezone": "America/New_York",
        "tasks": [
            {"earliest_start": "2024-06-03T12:00:00-04:00",
             "latest_end": "2024-06-03T20:00:00-04:00",
             "duration_minutes": 60, "energy": "2"},
            {"earliest_start": "2024-06-03T12:00:00-04:00",
             "latest_end": "2024-06-03T20:00:00-04:00",
             "duration_minutes": 60, "energy": "2"},
        ],
    })
    assert r.status_code == 200, r.text
    plan = r.json()
    assert plan["feasible"] is True
    assert Decimal(plan["total_amount"]) == Decimal("0.80")


def test_import_replay_returns_original_result():
    first = _import(_payload(code="import-replay-v1"))
    assert first.status_code == 201
    replay = _import(_payload(code="import-replay-v1"))
    assert replay.status_code == 200
    assert replay.json() == first.json()
    # 没有产生重复版本
    codes = [v["code"] for v in client.get("/versions").json()]
    assert codes.count("import-replay-v1") == 1


def test_import_list_order_does_not_change_digest():
    first = _import(_payload(code="import-order-v1"))
    assert first.status_code == 201

    shuffled = _payload(code="import-order-v1")
    shuffled["rules"] = list(reversed(shuffled["rules"]))
    shuffled["holidays"] = list(reversed(shuffled["holidays"]))
    for rule in shuffled["rules"]:
        rule["tiers"] = list(reversed(rule["tiers"]))
    r = _import(shuffled)
    assert r.status_code == 200
    assert r.json() == first.json()


def test_import_equivalent_field_forms_keep_digest():
    p1 = _payload(code="import-forms-v1")
    p1["rules"][0]["weekdays"] = "1,2,5"
    p1["rules"][0]["time_on"] = "8:00"
    p1["rules"][0]["tiers"][0]["unit_price"] = "0.20"
    p1["rules"][0]["tiers"][0]["cumulative_cap"] = "100"
    first = _import(p1)
    assert first.status_code == 201, first.text

    p2 = _payload(code="import-forms-v1")
    p2["rules"][0]["weekdays"] = [5, 1, 2]      # 数组、乱序 -> "1,2,5"
    p2["rules"][0]["time_on"] = "08:00"         # 零填充等价
    p2["rules"][0]["tiers"][0]["unit_price"] = 0.2       # 数字 -> "0.2"
    p2["rules"][0]["tiers"][0]["cumulative_cap"] = 100   # 数字 -> "100"
    r = _import(p2)
    assert r.status_code == 200
    assert r.json() == first.json()


def test_import_duplicate_holiday_entries_deduped():
    p1 = _payload(code="import-holiday-dedupe-v1")
    first = _import(p1)
    assert first.status_code == 201

    p2 = _payload(code="import-holiday-dedupe-v1")
    # 同日期同类型的重复条目被去重（保留名称字典序最小者），摘要不变
    p2["holidays"].append({"day": "2024-12-25", "kind": "holiday",
                           "name": "Xmas"})
    r = _import(p2)
    assert r.status_code == 200
    assert r.json() == first.json()


# ------------------------------------------------------------------ conflict


def test_import_same_code_different_digest_409():
    first = _import(_payload(code="import-conflict-v1"))
    assert first.status_code == 201
    digest = first.json()["content_digest"]

    changed = _payload(code="import-conflict-v1")
    changed["rules"][1]["tiers"][0]["unit_price"] = "0.11"
    r = _import(changed)
    assert r.status_code == 409
    error = r.json()["error"]
    assert error["code"] == "version_conflict"
    assert error["details"]["path"] == "code"
    assert error["details"]["existing_digest"] == digest
    assert error["details"]["received_digest"] != digest

    # 原版本内容未被改动
    body = client.get("/versions/import-conflict-v1").json()
    night = [rule for rule in body["rules"] if rule["code"] == "night"][0]
    assert night["tiers"][0]["unit_price"] == "0.1"


def test_import_seed_code_with_different_content_409():
    # 预置版本没有导入登记，也要按内容摘要拒绝覆盖
    r = _import(_payload(code="us-ny-demo-v2024"))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "version_conflict"


# ------------------------------------------------------------ field errors


def _errors(resp):
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["error"]["code"] == "invalid_import"
    return body["error"]["details"]["errors"]


def test_import_duplicate_rule_code_422():
    p = _payload(code="import-dup-v1")
    p["rules"].append(copy.deepcopy(p["rules"][0]))
    errors = _errors(_import(p))
    dup = [e for e in errors if e["code"] == "duplicate_rule_code"]
    assert dup and dup[0]["path"] == "rules[2].code"
    assert "rules[0]" in dup[0]["message"]


def test_import_invalid_timezone_422():
    p = _payload(code="import-tz-v1")
    p["rules"][0]["timezone"] = "Not/AZone"
    errors = _errors(_import(p))
    tz = [e for e in errors if e["code"] == "invalid_timezone"]
    assert tz and tz[0]["path"] == "rules[0].timezone"


def test_import_timezone_mismatch_422():
    p = _payload(code="import-tzm-v1")
    p["rules"][1]["timezone"] = "Asia/Shanghai"
    errors = _errors(_import(p))
    mm = [e for e in errors if e["code"] == "timezone_mismatch"]
    assert mm and mm[0]["path"] == "rules[1].timezone"
    assert "America/New_York" in mm[0]["message"]


def test_import_invalid_effective_range_422():
    p = _payload(code="import-eff-v1")
    p["rules"][0]["effective_to"] = "2024-01-01T00:00:00"
    errors = _errors(_import(p))
    eff = [e for e in errors if e["code"] == "invalid_effective_range"]
    assert eff and eff[0]["path"] == "rules[0].effective_to"


def test_import_aware_effective_time_422():
    p = _payload(code="import-aware-v1")
    p["rules"][0]["effective_from"] = "2024-01-01T00:00:00+08:00"
    errors = _errors(_import(p))
    dt = [e for e in errors if e["code"] == "invalid_datetime"]
    assert dt and dt[0]["path"] == "rules[0].effective_from"


def test_import_invalid_time_window_422():
    p = _payload(code="import-window-v1")
    p["rules"][0]["time_on"] = "24:00"      # 仅 time_off 允许 24:00
    p["rules"][1]["time_off"] = "25:00"     # 非法时刻
    errors = _errors(_import(p))
    win = [e for e in errors if e["code"] == "invalid_time_window"]
    assert {e["path"] for e in win} == {"rules[0].time_on",
                                        "rules[1].time_off"}


def test_import_holiday_conflict_422():
    p = _payload(code="import-hc-v1")
    p["holidays"].append({"day": "2024-07-04", "kind": "workday",
                          "name": "调休上班"})
    errors = _errors(_import(p))
    hc = [e for e in errors if e["code"] == "holiday_conflict"]
    assert hc and hc[0]["path"] == "holidays[2].kind"
    assert "2024-07-04" in hc[0]["message"]


def test_import_invalid_holiday_kind_422():
    p = _payload(code="import-hk-v1")
    p["holidays"][0]["kind"] = "festival"
    errors = _errors(_import(p))
    hk = [e for e in errors if e["code"] == "invalid_holiday_kind"]
    assert hk and hk[0]["path"] == "holidays[0].kind"


def test_import_tier_errors_422():
    # 序号跳号
    p = _payload(code="import-tier-v1")
    p["rules"][0]["tiers"][1]["ordinal"] = 3
    errors = _errors(_import(p))
    assert any(e["code"] == "tier_gap"
               and e["path"] == "rules[0].tiers[1].ordinal" for e in errors)

    # 累计上限不递增
    p = _payload(code="import-tier-v1")
    p["rules"][0]["tiers"][0]["cumulative_cap"] = "100"
    p["rules"][0]["tiers"][1]["ordinal"] = 2
    p["rules"][0]["tiers"][1]["cumulative_cap"] = "50"
    p["rules"][0]["tiers"].append(
        {"ordinal": 3, "cumulative_cap": None, "unit_price": "0.4"})
    errors = _errors(_import(p))
    assert any("必须大于前一档上限" in e["message"] for e in errors)

    # 中间档开放
    p = _payload(code="import-tier-v1")
    p["rules"][0]["tiers"][0]["cumulative_cap"] = None
    errors = _errors(_import(p))
    assert any("中间档位不得开放" in e["message"] for e in errors)

    # 最后一档封闭
    p = _payload(code="import-tier-v1")
    p["rules"][0]["tiers"][1]["cumulative_cap"] = "300"
    errors = _errors(_import(p))
    assert any("最后一档必须开放" in e["message"] for e in errors)


def test_import_empty_rules_422():
    p = _payload(code="import-empty-v1", rules=[])
    errors = _errors(_import(p))
    assert any(e["code"] == "empty_rules" and e["path"] == "rules"
               for e in errors)


def test_import_multiple_errors_reported_together():
    p = _payload(code="import-multi-v1")
    p["rules"][0]["timezone"] = "Not/AZone"
    p["rules"][1]["tiers"] = []
    p["holidays"].append({"day": "2024-07-04", "kind": "workday"})
    errors = _errors(_import(p))
    codes = {e["code"] for e in errors}
    assert {"invalid_timezone", "tier_gap", "holiday_conflict"} <= codes
    assert all(e["path"] for e in errors)


# ------------------------------------------------------- structural conflict


def test_import_rule_overlap_409_and_no_partial_writes():
    p = _payload(code="import-overlap-v1")
    p["rules"][1] = copy.deepcopy(p["rules"][0])
    p["rules"][1]["code"] = "day-2"  # 与 day 完全重叠
    r = _import(p)
    assert r.status_code == 409, r.text
    error = r.json()["error"]
    assert error["code"] == "rule_conflict"
    issues = error["details"]
    overlap = [i for i in issues if i["type"] == "rule_overlap"]
    assert overlap and overlap[0]["rules"] == ["day", "day-2"]
    assert overlap[0]["rule_indices"] == [0, 1]

    # 数据库不得留下半成品：版本与其规则/阶梯/日历都不可见
    assert client.get("/versions/import-overlap-v1").status_code == 404
    assert client.get(
        "/versions/import-overlap-v1/issues").status_code == 404
    codes = {v["code"] for v in client.get("/versions").json()}
    assert "import-overlap-v1" not in codes


def test_import_validation_failure_leaves_no_partial_data():
    p = _payload(code="import-atomic-v1")
    p["holidays"].append({"day": "2024-07-04", "kind": "workday"})
    assert _import(p).status_code == 422
    assert client.get("/versions/import-atomic-v1").status_code == 404
    codes = {v["code"] for v in client.get("/versions").json()}
    assert "import-atomic-v1" not in codes
