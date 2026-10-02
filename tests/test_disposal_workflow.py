from __future__ import annotations

from datetime import date

from app.core.errors import ConflictError
from app.database import transaction
from app.forensics.examinations import add_months
from app.forensics.service import ForensicService
from tests.test_forensics_workflow import create_stored_lot


def _retire_case(service: ForensicService, case_id: int) -> None:
    """把案件推进到结案（退出保存），用于到期处置评估。"""
    case = service.repository.require_forensic_case(case_id)
    service.forensic_cases.transition(case["id"], {
        "target_status": "retired", "reason": "诉讼终结，案件结案",
        "expected_version": case["version"], "actor": "审核员",
    })


def _make_policy(service: ForensicService, category: str = "生物检材", discipline: str = "法医物证", months: int = 1) -> dict:
    return service.disposal.create_retention_policy({
        "policy_code": f"RT-{category}", "specimen_category": category, "discipline": discipline,
        "retention_months": months, "effective_from": "2020-01-01", "effective_to": None,
        "created_by": "档案负责人",
    })


def test_generation_snapshots_basis_and_excludes_blockers(client):
    # 到期判断只依赖清单的 as_of，不依赖系统当前时钟
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        case, specimen, _ = create_stored_lot(service, "101")
        case2, specimen2, _ = create_stored_lot(service, "102")
        case3, specimen3, _ = create_stored_lot(service, "103")
        case4, specimen4, _ = create_stored_lot(service, "104")
        _make_policy(service)
        _retire_case(service, case["id"])
        _retire_case(service, case2["id"])
        _retire_case(service, case3["id"])
        _retire_case(service, case4["id"])

        # 检材2：冻结；检材3：未完成检验；检材4：已登记延期决定
        service.custody.impose_hold({
            "specimen_id": specimen2["id"], "hold_type": "保全", "reason": "诉讼保全", "actor": "审核员",
        })
        protocol = service.examinations.create_protocol({
            "protocol_code": "DNA-X", "discipline": "法医物证", "observation_target": 10, "checkpoint_count": 1,
            "reference_value": 1, "turnaround_days": 3, "conclusion_rule": "规则", "created_by": "技术员",
        })
        service.examinations.schedule_examination({
            "examination_no": "EX-OPEN-1", "specimen_id": specimen3["id"], "protocol_id": protocol["id"],
            "examination_type": "补充检验", "sample_quantity": 1, "scheduled_for": "2027-02-01",
            "requested_by": "检验员", "idempotency_key": "open-exam-103",
        })
        service.disposal.add_extension({
            "specimen_id": specimen4["id"], "extension_months": 60,
            "reason": "当事人申请长期留存", "decided_by": "档案负责人",
        })

        batch = service.disposal.generate_batch({
            "as_of": "2027-06-01", "created_by": "档案管理员",
        })
        by_no = {item["specimen_no"]: item for item in batch["candidates"]}
        closure_event = connection.execute(
            "SELECT created_at FROM case_events WHERE case_id=? AND to_status='retired' ORDER BY id DESC LIMIT 1",
            (case["id"],),
        ).fetchone()[0][:10]
        assert by_no["SP-101"]["status"] == "candidate"
        basis = by_no["SP-101"]["basis"]
        assert basis["case_closed_on"] == closure_event
        assert basis["policy"]["retention_months"] == 1
        # 到期日 = 结案日 + 策略保存月数
        assert by_no["SP-101"]["base_due_on"] == add_months(date.fromisoformat(closure_event), 1).isoformat()
        assert by_no["SP-102"]["status"] == "excluded"
        assert "冻结" in by_no["SP-102"]["exclusion_reason"]
        assert by_no["SP-103"]["status"] == "excluded"
        assert "未完成检验" in by_no["SP-103"]["exclusion_reason"]
        assert by_no["SP-104"]["status"] == "excluded"
        assert "有延期决定" in by_no["SP-104"]["exclusion_reason"]
        assert batch["counts"]["candidates"] == 1
        assert batch["counts"]["excluded"] == 3


def test_decisions_retain_extend_destroy_and_conflict(client):
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        case, specimen, _ = create_stored_lot(service, "201")
        case2, specimen2, _ = create_stored_lot(service, "202")
        _make_policy(service)
        _retire_case(service, case["id"])
        _retire_case(service, case2["id"])
        batch = service.disposal.generate_batch({"as_of": "2027-06-01", "created_by": "档案管理员"})
        c1 = next(c for c in batch["candidates"] if c["specimen_no"] == "SP-201")
        c2 = next(c for c in batch["candidates"] if c["specimen_no"] == "SP-202")

        # 在批量决定前，检材2 出现新生保全事件 -> 只能标冲突，不覆盖新事实
        service.custody.impose_hold({
            "specimen_id": specimen2["id"], "hold_type": "保全", "reason": "新的诉讼保全", "actor": "审核员",
        })
        result = service.disposal.record_decisions(batch["id"], {
            "actor": "档案管理员",
            "decisions": [
                {"candidate_id": c1["id"], "decision": "destroy"},
                {"candidate_id": c2["id"], "decision": "destroy"},
            ],
        })
        assert result["decided"] == [c1["id"]]
        assert len(result["conflicts"]) == 1
        # 新生保全同时带动了检材版本前进，表现为版本/阻断冲突之一，但都只标记不覆盖
        assert result["conflicts"][0]["code"] in {"version_changed", "blocker_appeared"}
        refreshed = {c["specimen_no"]: c for c in result["candidates"]}
        assert refreshed["SP-201"]["status"] == "queued"
        assert refreshed["SP-202"]["status"] == "conflicted"
        # 检材2 的冻结事实仍然保留
        assert service.repository.active_holds(specimen2["id"])

        # 保留与延期路径
        case3, specimen3, _ = create_stored_lot(service, "203")
        _retire_case(service, case3["id"])
        batch2 = service.disposal.generate_batch({"as_of": "2027-06-01", "created_by": "档案管理员"})
        c3 = next(c for c in batch2["candidates"] if c["specimen_no"] == "SP-203")
        decided = service.disposal.record_decisions(batch2["id"], {
            "actor": "档案管理员",
            "decisions": [
                {"candidate_id": c3["id"], "decision": "extend", "reason": "家属申请留存", "extension_months": 24},
            ],
        })
        decided_candidate = next(c for c in decided["candidates"] if c["id"] == c3["id"])
        assert decided_candidate["status"] == "extended"
        versions = connection.execute(
            "SELECT version,extension_months FROM specimen_retention_extensions WHERE specimen_id=?",
            (specimen3["id"],),
        ).fetchall()
        assert [tuple(row) for row in versions] == [(1, 24)]


def test_version_change_between_snapshot_and_decision_is_conflict(client):
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        case, specimen, _ = create_stored_lot(service, "301")
        _make_policy(service)
        _retire_case(service, case["id"])
        batch = service.disposal.generate_batch({"as_of": "2027-06-01", "created_by": "档案管理员"})
        c1 = batch["candidates"][0]
        # 快照后发生一次取样，检材版本前进（状态仍为 stored）
        service.custody.withdraw({
            "specimen_id": specimen["id"], "quantity": 10, "movement_type": "取样",
            "idempotency_key": "sample-301-0001", "actor": "技术员", "reason": "补充取样",
        })
        result = service.disposal.record_decisions(batch["id"], {
            "actor": "档案管理员",
            "decisions": [{"candidate_id": c1["id"], "decision": "destroy"}],
        })
        assert result["conflicts"][0]["code"] == "version_changed"
        assert result["candidates"][0]["status"] == "conflicted"


def test_dual_confirmation_new_hold_invalidates_then_execute(client):
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        case, specimen, placement = create_stored_lot(service, "401")
        _make_policy(service)
        _retire_case(service, case["id"])
        batch = service.disposal.generate_batch({"as_of": "2027-06-01", "created_by": "档案管理员"})
        c1 = batch["candidates"][0]
        service.disposal.record_decisions(batch["id"], {
            "actor": "档案管理员", "decisions": [{"candidate_id": c1["id"], "decision": "destroy"}],
        })
        service.disposal.submit_batch(batch["id"], {"actor": "档案管理员"})

        # 同一人不能同时充当保管人与监督人
        service.disposal.confirm_batch(batch["id"], {"role": "custodian", "actor": "甲"})
        try:
            service.disposal.confirm_batch(batch["id"], {"role": "supervisor", "actor": "甲"})
        except ConflictError as exc:
            assert "同一人" in exc.message
        else:
            raise AssertionError("双人确认不能由同一人完成")

        # 第二位确认（监督人）之前出现新保全事件：确认时该待销毁条目失效
        service.custody.impose_hold({
            "specimen_id": specimen["id"], "hold_type": "保全", "reason": "再审保全", "actor": "法院",
        })
        confirmed = service.disposal.confirm_batch(batch["id"], {"role": "supervisor", "actor": "乙"})
        candidate = next(c for c in confirmed["candidates"] if c["id"] == c1["id"])
        assert candidate["status"] == "invalid"
        assert "保全" in candidate["invalid_reason"]

        # 即便随后解除冻结，失效条目也不会在本清单被销毁；清单无可销毁条目时应报错
        hold = service.repository.active_holds(specimen["id"])[0]
        service.custody.release_hold(hold["id"], "法院", "再审结束解除保全")
        try:
            service.disposal.execute_batch(batch["id"], {"actor": "档案管理员"})
        except ConflictError as exc:
            assert "没有可执行" in exc.message
        else:
            raise AssertionError("存在失效条目且无有效待销毁项时不应执行")


def test_full_execution_updates_specimen_placement_and_movement_once(client):
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        case, specimen, placement = create_stored_lot(service, "501")
        _make_policy(service)
        _retire_case(service, case["id"])
        batch = service.disposal.generate_batch({"as_of": "2027-06-01", "created_by": "档案管理员"})
        c1 = batch["candidates"][0]
        service.disposal.record_decisions(batch["id"], {
            "actor": "档案管理员", "decisions": [{"candidate_id": c1["id"], "decision": "destroy"}],
        })
        service.disposal.submit_batch(batch["id"], {"actor": "档案管理员"})
        service.disposal.confirm_batch(batch["id"], {"role": "custodian", "actor": "保管员A"})
        service.disposal.confirm_batch(batch["id"], {"role": "supervisor", "actor": "监督员B"})
        executed = service.disposal.execute_batch(batch["id"], {"actor": "档案管理员"})
        assert executed["status"] == "executed"

        destroyed_specimen = service.repository.require_specimen(specimen["id"])
        assert destroyed_specimen["status"] == "disposed"
        assert destroyed_specimen["available_quantity"] == 0
        removed = service.repository.require_placement(placement["id"])
        assert removed["removed_at"] is not None
        movement = connection.execute(
            "SELECT movement_type,reason FROM custody_events WHERE specimen_id=? AND movement_type='报废' ORDER BY id DESC LIMIT 1",
            (specimen["id"],),
        ).fetchone()
        assert movement is not None
        # 报废流水与处置清单关联，可回溯
        assert "处置清单" in movement["reason"]

        # 追溯 API：候选依据、排除原因（无）、双人确认、销毁结果
        trail = service.disposal.candidate_trail(c1["id"])
        assert trail["batch"]["custodian_confirmed_by"] == "保管员A"
        assert trail["batch"]["supervisor_confirmed_by"] == "监督员B"
        assert trail["basis"]["policy"]["retention_months"] == 1
        assert trail["status"] == "destroyed"
        assert trail["result"]["containers"][0]["container_code"] == placement["container_code"]
        event_types = [event["event_type"] for event in trail["events"]]
        assert "destroyed" in event_types


def test_retention_policy_versions_use_rule_effective_at_closure(client):
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        # 旧策略：结案时有效，保存 3 个月；新策略事后生效为 60 个月
        service.disposal.create_retention_policy({
            "policy_code": "RT-生物检材", "specimen_category": "生物检材", "discipline": "法医物证",
            "retention_months": 3, "effective_from": "2020-01-01", "effective_to": "2027-01-01",
            "created_by": "档案负责人",
        })
        service.disposal.create_retention_policy({
            "policy_code": "RT-生物检材", "specimen_category": "生物检材", "discipline": "法医物证",
            "retention_months": 60, "effective_from": "2027-01-02", "effective_to": None,
            "created_by": "档案负责人",
        })
        case, specimen, _ = create_stored_lot(service, "601")
        _retire_case(service, case["id"])  # 结案于 2026-09-30
        batch = service.disposal.generate_batch({"as_of": "2027-02-01", "created_by": "档案管理员"})
        item = batch["candidates"][0]
        # 即便清单在 2027 年生成，仍应采用结案当日有效的 3 个月策略
        assert item["basis"]["policy"]["retention_months"] == 3
        assert item["status"] == "candidate"
