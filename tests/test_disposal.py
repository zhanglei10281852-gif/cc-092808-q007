from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.core.clock import FrozenClock
from app.core.errors import ConflictError
from app.database import transaction
from app.forensics.service import ForensicService


def make_clock() -> FrozenClock:
    return FrozenClock(datetime(2026, 1, 15, 8, 0, tzinfo=UTC))


def create_accepted_case(service: ForensicService, suffix: str) -> dict:
    source = service.forensic_cases.create_agency({
        "agency_code": f"ORG-{suffix}", "agency_name": "市公安局物证中心", "jurisdiction_code": "CN",
        "contact_address": "政务区司法路 8 号", "licensed_on": "2025-10-02", "accreditation_no": None,
        "restrictions": {},
    })
    forensic_case = service.forensic_cases.create_forensic_case({
        "case_no": f"CASE-{suffix}", "case_name": "身份关系鉴定", "discipline": "法医物证",
        "entrusted_matter": "亲缘关系鉴定", "agency_id": source["id"], "case_source": "委托",
        "accepted_on": "2025-12-01", "passport": {}, "created_by": "登记员",
    })
    return service.forensic_cases.transition(forensic_case["id"], {
        "target_status": "accepted", "reason": "资料齐全", "expected_version": 1, "actor": "审核员",
    })


def retire_case(service: ForensicService, forensic_case: dict) -> dict:
    current = service.repository.require_forensic_case(forensic_case["id"])
    return service.forensic_cases.transition(current["id"], {
        "target_status": "retired", "reason": "案件结案归档",
        "expected_version": current["version"], "actor": "审核员",
    })


def create_specimen(service: ForensicService, forensic_case: dict, suffix: str, category: str = "常规检材") -> dict:
    location = service.custody.create_location({
        "location_code": f"VAULT-{suffix}", "facility": "检材保管室", "room": "冷藏区", "rack": "R1", "shelf": "S1",
        "capacity_units": 1000, "reference_value": 4, "humidity_percent": 45,
    })
    specimen = service.custody.create_specimen({
        "specimen_no": f"SP-{suffix}", "case_id": forensic_case["id"], "parent_specimen_id": None,
        "category": category, "received_year": 2025, "initial_quantity": 100, "integrity_percent": 100,
        "packaging": "防拆封袋", "sealed_on": "2025-12-02", "created_by": "登记员",
    })
    service.custody.place_specimen({
        "specimen_id": specimen["id"], "location_id": location["id"], "quantity": 100,
        "container_code": f"BOX-{suffix}", "idempotency_key": f"place-{suffix}-0001", "actor": "保管员",
    })
    return service.repository.specimen_detail(specimen["id"])


def create_policy(service: ForensicService, months: int = 6, category: str = "常规检材") -> dict:
    return service.disposal.create_policy({
        "specimen_category": category, "retention_months": months,
        "effective_from": "2020-01-01", "effective_to": None, "created_by": "档案管理员",
    })


def generate(service: ForensicService, as_of: str = "2026-10-01") -> dict:
    return service.disposal.generate_batch({"as_of_date": as_of, "generated_by": "档案管理员"})


def batch_item(batch: dict, specimen_id: int) -> dict:
    return next(item for item in batch["items"] if item["specimen_id"] == specimen_id)


def decide_and_confirm(service: ForensicService, batch_id: int, item_id: int) -> None:
    service.disposal.decide_items(batch_id, [{"item_id": item_id, "action": "destroy"}], "档案管理员")
    service.disposal.confirm(batch_id, item_id, {"role": "custodian", "actor": "保管员甲"})
    service.disposal.confirm(batch_id, item_id, {"role": "supervisor", "actor": "监督员乙"})


def test_snapshot_uses_closure_and_policy_effective_then(client):
    clock = make_clock()
    with transaction(immediate=True) as connection:
        service = ForensicService(connection, clock)
        create_policy(service)
        forensic_case = create_accepted_case(service, "S1")
        specimen = create_specimen(service, forensic_case, "S1")
        retire_case(service, forensic_case)
        batch = generate(service)
        assert batch["batch_no"] == "DSP-2026-10-01-001"
        item = batch_item(batch, specimen["id"])
        assert item["disposition"] == "candidate"
        assert item["exclusion_reasons"] == []
        assert item["basis"]["case_closed_on"] == "2026-01-15"
        assert item["basis"]["policy_version"] == 1
        assert item["basis"]["retention_months"] == 6
        assert item["basis"]["retention_due_on"] == "2026-07-15"
        assert item["basis"]["specimen_status"] == "stored"


def test_snapshot_selects_policy_version_effective_at_closure(client):
    clock = make_clock()
    with transaction(immediate=True) as connection:
        service = ForensicService(connection, clock)
        service.disposal.create_policy({
            "specimen_category": "常规检材", "retention_months": 12,
            "effective_from": "2020-01-01", "effective_to": "2026-06-30", "created_by": "档案管理员",
        })
        service.disposal.create_policy({
            "specimen_category": "常规检材", "retention_months": 1,
            "effective_from": "2026-07-01", "effective_to": None, "created_by": "档案管理员",
        })
        forensic_case = create_accepted_case(service, "S2")
        specimen = create_specimen(service, forensic_case, "S2")
        retire_case(service, forensic_case)
        batch = generate(service, as_of="2026-08-01")
        assert batch["items"] == []  # 结案时有效的策略保存 12 个月，2026-08-01 尚未到期
        batch = generate(service, as_of="2027-02-01")
        item = batch_item(batch, specimen["id"])
        assert item["disposition"] == "candidate"
        assert item["basis"]["policy_version"] == 1
        assert item["basis"]["retention_due_on"] == "2027-01-15"


def test_snapshot_excludes_frozen_examining_reviewing_and_extended(client):
    clock = make_clock()
    with transaction(immediate=True) as connection:
        service = ForensicService(connection, clock)
        create_policy(service)
        forensic_case = create_accepted_case(service, "S3")
        frozen = create_specimen(service, forensic_case, "S3A")
        service.custody.impose_hold({"specimen_id": frozen["id"], "hold_type": "保全", "reason": "诉讼保全", "actor": "审核员"})
        examining = create_specimen(service, forensic_case, "S3B")
        protocol = service.examinations.create_protocol({
            "protocol_code": "DNA-REVIEW", "discipline": "法医物证", "observation_target": 100, "checkpoint_count": 1,
            "reference_value": 0.99, "turnaround_days": 14, "conclusion_rule": "位点质量满足复核阈值", "created_by": "技术负责人",
        })
        service.examinations.schedule_examination({
            "examination_no": "EX-S3B", "specimen_id": examining["id"], "protocol_id": protocol["id"],
            "examination_type": "补充检验", "sample_quantity": 5, "scheduled_for": "2026-10-10",
            "requested_by": "检验员", "idempotency_key": "schedule-s3b-001",
        })
        reviewing = create_specimen(service, forensic_case, "S3C")
        review_policy = service.examinations.create_policy({
            "discipline": "法医物证", "risk_level": "low", "interval_months": 12, "warning_days": 30,
            "minimum_conformity_percent": 75, "effective_from": "2026-01-01", "effective_to": None,
            "created_by": "技术负责人",
        })
        connection.execute(
            "INSERT INTO review_schedules(specimen_id,policy_id,due_on,status,reason,created_at,updated_at) "
            "VALUES(?,?,?,'pending','定期复核','2026-09-01','2026-09-01')",
            (reviewing["id"], review_policy["id"], "2026-11-01"),
        )
        extended = create_specimen(service, forensic_case, "S3D")
        clean = create_specimen(service, forensic_case, "S3E")
        retire_case(service, forensic_case)
        first = generate(service)
        service.disposal.decide_items(first["id"], [
            {"item_id": batch_item(first, extended["id"])["id"], "action": "extend", "extend_to": "2027-12-31",
             "reason": "检察机关要求继续留存"},
        ], "档案管理员")
        batch = generate(service)
        reasons = {item["specimen_no"]: item["exclusion_reasons"] for item in batch["items"]}
        assert reasons[frozen["specimen_no"]] == ["存在未解除的冻结"]
        assert reasons[examining["specimen_no"]] == ["存在未完成的检验"]
        assert reasons[reviewing["specimen_no"]] == ["存在未关闭的复核"]
        assert reasons[extended["specimen_no"]] == ["存在有效的延期决定"]
        assert batch_item(batch, clean["id"])["disposition"] == "candidate"
        for specimen_no in (frozen["specimen_no"], examining["specimen_no"], reviewing["specimen_no"], extended["specimen_no"]):
            item = next(row for row in batch["items"] if row["specimen_no"] == specimen_no)
            assert item["disposition"] == "excluded"


def test_snapshot_flags_missing_policy(client):
    clock = make_clock()
    with transaction(immediate=True) as connection:
        service = ForensicService(connection, clock)
        forensic_case = create_accepted_case(service, "S4")
        specimen = create_specimen(service, forensic_case, "S4", category="特殊毒物")
        retire_case(service, forensic_case)
        batch = generate(service)
        item = batch_item(batch, specimen["id"])
        assert item["disposition"] == "excluded"
        assert item["exclusion_reasons"] == ["缺少当时有效的保存策略"]
        assert item["basis"]["policy_id"] is None


def test_extension_versions_are_retained(client):
    clock = make_clock()
    with transaction(immediate=True) as connection:
        service = ForensicService(connection, clock)
        create_policy(service)
        forensic_case = create_accepted_case(service, "S5")
        specimen = create_specimen(service, forensic_case, "S5")
        retire_case(service, forensic_case)
        batch = generate(service)
        item_id = batch_item(batch, specimen["id"])["id"]
        service.disposal.decide_items(batch["id"], [
            {"item_id": item_id, "action": "extend", "extend_to": "2027-06-30", "reason": "第一次延期"},
        ], "档案管理员")
        result = service.disposal.decide_items(batch["id"], [
            {"item_id": item_id, "action": "extend", "extend_to": "2028-06-30", "reason": "第二次延期"},
        ], "档案管理员")
        assert result["results"][0]["disposition"] == "extended"
        versions = connection.execute(
            "SELECT version,extend_to,reason FROM disposal_extensions WHERE specimen_id=? ORDER BY version",
            (specimen["id"],),
        ).fetchall()
        assert [(row[0], row[1]) for row in versions] == [(1, "2027-06-30"), (2, "2028-06-30")]
        later = generate(service, as_of="2028-01-01")
        assert batch_item(later, specimen["id"])["exclusion_reasons"] == ["存在有效的延期决定"]
        expired = generate(service, as_of="2028-07-01")
        assert batch_item(expired, specimen["id"])["disposition"] == "candidate"


def test_batch_decision_marks_conflict_without_overwriting(client):
    clock = make_clock()
    with transaction(immediate=True) as connection:
        service = ForensicService(connection, clock)
        create_policy(service)
        forensic_case = create_accepted_case(service, "S6")
        specimen = create_specimen(service, forensic_case, "S6")
        retire_case(service, forensic_case)
        batch = generate(service)
        item_id = batch_item(batch, specimen["id"])["id"]
        service.custody.withdraw({
            "specimen_id": specimen["id"], "quantity": 10, "movement_type": "领用",
            "idempotency_key": "withdraw-s6-0001", "actor": "保管员", "reason": "补充检验",
        })
        result = service.disposal.decide_items(batch["id"], [{"item_id": item_id, "action": "destroy"}], "档案管理员")
        assert result["results"][0]["outcome"] == "conflict"
        item = batch_item(result["batch"], specimen["id"])
        assert item["disposition"] == "conflict"
        assert "未覆盖新事实" in item["decision"]["note"]
        current = service.repository.require_specimen(specimen["id"])
        assert current["status"] == "stored"
        assert current["available_quantity"] == 90


def test_dual_confirmation_requires_distinct_people(client):
    clock = make_clock()
    with transaction(immediate=True) as connection:
        service = ForensicService(connection, clock)
        create_policy(service)
        forensic_case = create_accepted_case(service, "S7")
        specimen = create_specimen(service, forensic_case, "S7")
        retire_case(service, forensic_case)
        batch = generate(service)
        item_id = batch_item(batch, specimen["id"])["id"]
        service.disposal.decide_items(batch["id"], [{"item_id": item_id, "action": "destroy"}], "档案管理员")
        service.disposal.confirm(batch["id"], item_id, {"role": "custodian", "actor": "保管员甲"})
        with pytest.raises(ConflictError, match="不同人员"):
            service.disposal.confirm(batch["id"], item_id, {"role": "supervisor", "actor": "保管员甲"})
        with pytest.raises(ConflictError, match="双人确认"):
            service.disposal.execute(batch["id"], {"actor": "档案管理员", "reason": "到期销毁"})
        confirmed = service.disposal.confirm(batch["id"], item_id, {"role": "supervisor", "actor": "监督员乙"})
        assert confirmed["custodian_confirmed_by"] == "保管员甲"
        assert confirmed["supervisor_confirmed_by"] == "监督员乙"


def test_new_hold_during_confirmation_invalidates_item(client):
    clock = make_clock()
    with transaction(immediate=True) as connection:
        service = ForensicService(connection, clock)
        create_policy(service)
        forensic_case = create_accepted_case(service, "S8")
        specimen = create_specimen(service, forensic_case, "S8")
        retire_case(service, forensic_case)
        batch = generate(service)
        item_id = batch_item(batch, specimen["id"])["id"]
        service.disposal.decide_items(batch["id"], [{"item_id": item_id, "action": "destroy"}], "档案管理员")
        service.disposal.confirm(batch["id"], item_id, {"role": "custodian", "actor": "保管员甲"})
        service.disposal.confirm(batch["id"], item_id, {"role": "supervisor", "actor": "监督员乙"})
        service.custody.impose_hold({"specimen_id": specimen["id"], "hold_type": "保全", "reason": "法院补充保全", "actor": "审核员"})
        item = service.repository.require_disposal_item(item_id)
        assert item["disposition"] == "invalidated"
        executed = service.disposal.execute(batch["id"], {"actor": "档案管理员", "reason": "到期销毁"})
        assert executed["summary"] == {"destroyed": [], "invalidated": [], "conflicts": []}
        assert service.repository.require_specimen(specimen["id"])["status"] == "held"


def test_execute_destroys_specimen_placement_and_ledger(client):
    clock = make_clock()
    with transaction(immediate=True) as connection:
        service = ForensicService(connection, clock)
        create_policy(service)
        forensic_case = create_accepted_case(service, "S9")
        first = create_specimen(service, forensic_case, "S9A")
        second = create_specimen(service, forensic_case, "S9B")
        retire_case(service, forensic_case)
        batch = generate(service)
        decide_and_confirm(service, batch["id"], batch_item(batch, first["id"])["id"])
        decide_and_confirm(service, batch["id"], batch_item(batch, second["id"])["id"])
        executed = service.disposal.execute(batch["id"], {"actor": "档案管理员", "reason": "保存期满"})
        assert executed["status"] == "executed"
        assert len(executed["summary"]["destroyed"]) == 2
        for specimen in (first, second):
            current = service.repository.require_specimen(specimen["id"])
            assert current["status"] == "disposed"
            assert current["available_quantity"] == 0
            placements = connection.execute(
                "SELECT COUNT(*) FROM specimen_placements WHERE specimen_id=? AND removed_at IS NULL", (specimen["id"],)
            ).fetchone()[0]
            assert placements == 0
            movement = connection.execute(
                "SELECT * FROM custody_events WHERE specimen_id=? AND movement_type='报废'", (specimen["id"],)
            ).fetchone()
            assert movement is not None and movement["quantity"] == -100
            item = batch_item(executed, specimen["id"])
            assert item["disposition"] == "destroyed"
            assert item["result"]["custody_event_id"] == movement["id"]
            assert item["result"]["destroyed_at"] is not None
            assert item["confirmations"]["custodian"]["actor"] == "保管员甲"
            assert item["confirmations"]["supervisor"]["actor"] == "监督员乙"
        with pytest.raises(ConflictError, match="已经执行过"):
            service.disposal.execute(batch["id"], {"actor": "档案管理员", "reason": "重复执行"})


def test_execute_requires_every_candidate_decided(client):
    clock = make_clock()
    with transaction(immediate=True) as connection:
        service = ForensicService(connection, clock)
        create_policy(service)
        forensic_case = create_accepted_case(service, "S10")
        specimen = create_specimen(service, forensic_case, "S10")
        retire_case(service, forensic_case)
        batch = generate(service)
        with pytest.raises(ConflictError, match="未决定"):
            service.disposal.execute(batch["id"], {"actor": "档案管理员", "reason": "到期销毁"})
        service.disposal.decide_items(
            batch["id"], [{"item_id": batch_item(batch, specimen["id"])["id"], "action": "retain", "note": "继续留存"}],
            "档案管理员",
        )
        executed = service.disposal.execute(batch["id"], {"actor": "档案管理员", "reason": "到期销毁"})
        assert executed["status"] == "executed"
        assert service.repository.require_specimen(specimen["id"])["status"] == "stored"


def test_disposal_api_flow_and_trace(client, admin):
    headers = admin["headers"]
    policy = client.post("/api/forensics/retention-policies", headers=headers, json={
        "specimen_category": "常规检材", "retention_months": 1, "effective_from": "2020-01-01", "created_by": "档案管理员",
    })
    assert policy.status_code == 201, policy.text
    source = client.post("/api/forensics/agencies", headers=headers, json={
        "agency_code": "API-ORG-1", "agency_name": "区公安分局", "jurisdiction_code": "CN", "restrictions": {},
    })
    forensic_case = client.post("/api/forensics/cases", headers=headers, json={
        "case_no": "API-CASE-1", "case_name": "痕迹鉴定", "discipline": "痕迹物证", "agency_id": source.json()["id"],
        "case_source": "委托", "accepted_on": "2025-01-10", "passport": {}, "created_by": "登记员",
    })
    client.post(f"/api/forensics/cases/{forensic_case.json()['id']}/transition", headers=headers, json={
        "target_status": "accepted", "reason": "资料齐全", "expected_version": 1, "actor": "审核员",
    })
    location = client.post("/api/forensics/locations", headers=headers, json={
        "location_code": "API-L1", "facility": "中期库", "room": "一室", "rack": "A", "shelf": "1",
        "capacity_units": 3000, "reference_value": 4, "humidity_percent": 35,
    })
    specimen = client.post("/api/forensics/specimens", headers=headers, json={
        "specimen_no": "API-SP-1", "case_id": forensic_case.json()["id"], "category": "常规检材",
        "received_year": 2025, "initial_quantity": 8, "created_by": "登记员",
    })
    assert specimen.status_code == 201, specimen.text
    client.post("/api/forensics/placements", headers=headers, json={
        "specimen_id": specimen.json()["id"], "location_id": location.json()["id"], "quantity": 8,
        "container_code": "API-BOX-1", "idempotency_key": "api-place-0001", "actor": "保管员",
    })
    retired = client.post(f"/api/forensics/cases/{forensic_case.json()['id']}/transition", headers=headers, json={
        "target_status": "retired", "reason": "案件结案", "expected_version": 2, "actor": "审核员",
    })
    assert retired.status_code == 200, retired.text
    batch = client.post("/api/forensics/disposal-batches", headers=headers, json={
        "as_of_date": "2027-01-01", "generated_by": "档案管理员",
    })
    assert batch.status_code == 201, batch.text
    item = batch.json()["items"][0]
    assert item["disposition"] == "candidate"
    decided = client.post(f"/api/forensics/disposal-batches/{batch.json()['id']}/decisions", headers=headers, json={
        "decisions": [{"item_id": item["id"], "action": "destroy"}], "actor": "档案管理员",
    })
    assert decided.status_code == 200, decided.text
    for role, actor in (("custodian", "保管员甲"), ("supervisor", "监督员乙")):
        confirmed = client.post(
            f"/api/forensics/disposal-batches/{batch.json()['id']}/items/{item['id']}/confirm",
            headers=headers, json={"role": role, "actor": actor},
        )
        assert confirmed.status_code == 200, confirmed.text
    executed = client.post(f"/api/forensics/disposal-batches/{batch.json()['id']}/execute", headers=headers, json={
        "actor": "档案管理员", "reason": "保存期满销毁",
    })
    assert executed.status_code == 200, executed.text
    trace = client.get(f"/api/forensics/disposal-batches/{batch.json()['id']}", headers=headers)
    assert trace.status_code == 200
    traced_item = trace.json()["items"][0]
    assert traced_item["disposition"] == "destroyed"
    assert traced_item["basis"]["retention_due_on"] is not None
    assert traced_item["confirmations"]["custodian"]["actor"] == "保管员甲"
    assert traced_item["result"]["custody_event_id"] is not None
    detail = client.get(f"/api/forensics/specimens/{specimen.json()['id']}", headers=headers)
    assert detail.json()["status"] == "disposed"


def test_disposal_api_requires_permission(client):
    assert client.post("/api/forensics/disposal-batches", json={
        "as_of_date": "2026-10-01", "generated_by": "档案管理员",
    }).status_code == 401
