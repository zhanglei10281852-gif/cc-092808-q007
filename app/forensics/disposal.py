from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.forensics.examinations import add_months
from app.forensics.repository import ForensicRepository, record, records

OPEN_REVIEW_STATUSES = ("pending", "notified", "scheduled")
UNFINISHED_EXAMINATION_STATUSES = ("scheduled", "running")
# 生成候选时自动排除的阻断原因
BLOCKER_FROZEN = "冻结"
BLOCKER_EXAMINATION = "未完成检验"
BLOCKER_REVIEW = "未关闭复核"
BLOCKER_EXTENSION = "有延期决定"


class DisposalService:
    """按保存策略生成到期处置清单，并管理逐项决定、双人确认与一次性销毁。"""

    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        self.repository = ForensicRepository(connection)

    # ------------------------------------------------------------------ 保存策略

    def create_retention_policy(self, data: dict[str, Any]) -> dict[str, Any]:
        latest = self.connection.execute(
            "SELECT version FROM retention_policies WHERE policy_code=? ORDER BY version DESC LIMIT 1",
            (data["policy_code"],),
        ).fetchone()
        version = int(latest[0]) + 1 if latest else 1
        timestamp = to_storage(self.clock.now())
        if data.get("effective_to") and data["effective_to"] < data["effective_from"]:
            raise ValidationError("策略失效日期不能早于生效日期")
        cursor = self.connection.execute(
            "INSERT INTO retention_policies(policy_code,version,specimen_category,discipline,retention_months,"
            "effective_from,effective_to,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                data["policy_code"], version, data["specimen_category"], data["discipline"],
                int(data["retention_months"]), data["effective_from"], data.get("effective_to"),
                data["created_by"], timestamp,
            ),
        )
        return self.require_policy(int(cursor.lastrowid))

    def require_policy(self, policy_id: int) -> dict[str, Any]:
        item = record(self.connection.execute(
            "SELECT * FROM retention_policies WHERE id=?", (policy_id,)
        ).fetchone())
        if item is None:
            raise NotFoundError("保存策略不存在")
        return item

    def list_policies(self, category: str | None, discipline: str | None) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if category:
            where.append("specimen_category=?")
            params.append(category)
        if discipline:
            where.append("discipline=?")
            params.append(discipline)
        clause = " WHERE " + " AND ".join(where) if where else ""
        return records(self.connection.execute(
            f"SELECT * FROM retention_policies{clause} ORDER BY specimen_category,discipline,version DESC", params
        ).fetchall())

    def _policy_effective_on(
        self, category: str, discipline: str, on_date: str
    ) -> dict[str, Any] | None:
        """返回案件结案当日有效的保存策略（按版本取最新，留痕策略版本）。"""
        return record(self.connection.execute(
            "SELECT * FROM retention_policies WHERE specimen_category=? AND discipline=? "
            "AND effective_from<=? AND (effective_to IS NULL OR effective_to>=?) ORDER BY version DESC LIMIT 1",
            (category, discipline, on_date, on_date),
        ).fetchone())

    def add_extension(self, data: dict[str, Any]) -> dict[str, Any]:
        """单独登记一份延期决定（处置清单之外的补录入口），同样保留版本。"""
        specimen = self.repository.require_specimen(int(data["specimen_id"]))
        latest = self.connection.execute(
            "SELECT version FROM specimen_retention_extensions WHERE specimen_id=? ORDER BY version DESC LIMIT 1",
            (specimen["id"],),
        ).fetchone()
        version = int(latest[0]) + 1 if latest else 1
        timestamp = to_storage(self.clock.now())
        cursor = self.connection.execute(
            "INSERT INTO specimen_retention_extensions(specimen_id,version,extension_months,reason,decided_by,decided_at) "
            "VALUES(?,?,?,?,?,?)",
            (specimen["id"], version, int(data["extension_months"]), data["reason"], data["decided_by"], timestamp),
        )
        return record(self.connection.execute(
            "SELECT * FROM specimen_retention_extensions WHERE id=?", (cursor.lastrowid,)
        ).fetchone()) or {}

    # ------------------------------------------------------------------ 候选生成

    def generate_batch(self, data: dict[str, Any]) -> dict[str, Any]:
        as_of = self._as_date(data.get("as_of")) or self.clock.now().date()
        as_of_text = as_of.isoformat()
        timestamp = to_storage(self.clock.now())
        batch_no = data.get("batch_no") or f"DISP-{as_of_text}-{self._next_batch_seq(as_of_text)}"
        if self.connection.execute("SELECT 1 FROM disposal_batches WHERE batch_no=?", (batch_no,)).fetchone():
            raise ConflictError("处置清单编号已经存在")
        cursor = self.connection.execute(
            "INSERT INTO disposal_batches(batch_no,status,as_of,created_by,created_at,updated_at) "
            "VALUES(?, 'draft', ?, ?, ?, ?)",
            (batch_no, as_of_text, data["created_by"], timestamp, timestamp),
        )
        batch_id = int(cursor.lastrowid)
        self._event(batch_id, None, "generated", data["created_by"], {"as_of": as_of_text})

        category = data.get("specimen_category") or None
        case_id = int(data["case_id"]) if data.get("case_id") else None
        specimens = self._scope_specimens(category=category, case_id=case_id)
        included = excluded = 0
        for specimen in specimens:
            outcome = self._snapshot_one(batch_id, specimen, as_of, timestamp, data["created_by"])
            if outcome == "candidate":
                included += 1
            elif outcome == "excluded":
                excluded += 1
        self.connection.execute(
            "UPDATE disposal_batches SET updated_at=? WHERE id=?", (timestamp, batch_id)
        )
        return self.batch_detail(batch_id)

    def _next_batch_seq(self, as_of_text: str) -> int:
        return int(self.connection.execute(
            "SELECT COUNT(*) FROM disposal_batches WHERE as_of=?", (as_of_text,)
        ).fetchone()[0]) + 1

    def _scope_specimens(self, *, category: str | None, case_id: int | None) -> list[dict[str, Any]]:
        """已结案（案件退出保存）、仍在库，且未在其他活跃清单中排队销毁的检材。"""
        where = [
            "c.status='retired'",
            "s.status IN ('stored','held')",
            "NOT EXISTS("
            "SELECT 1 FROM disposal_candidates dc JOIN disposal_batches db ON db.id=dc.batch_id "
            "WHERE dc.specimen_id=s.id AND dc.status='queued' "
            "AND db.status IN ('draft','submitted','confirmed'))",
        ]
        params: list[Any] = []
        if category:
            where.append("s.specimen_category=?")
            params.append(category)
        if case_id:
            where.append("c.id=?")
            params.append(case_id)
        clause = " AND ".join(where)
        rows = self.connection.execute(
            "SELECT s.*,c.case_no,c.discipline AS case_discipline FROM specimens s "
            "JOIN forensic_cases c ON c.id=s.case_id "
            f"WHERE {clause} ORDER BY c.case_no,s.specimen_no",
            params,
        ).fetchall()
        return records(rows)

    def _snapshot_one(
        self, batch_id: int, specimen: dict[str, Any], as_of: date, timestamp: str, actor: str
    ) -> str:
        specimen_id = int(specimen["id"])
        category = specimen.get("specimen_category") or ""
        discipline = specimen["case_discipline"]
        closure = self.connection.execute(
            "SELECT id,created_at FROM case_events WHERE case_id=? AND to_status='retired' "
            "ORDER BY id DESC LIMIT 1",
            (specimen["case_id"],),
        ).fetchone()
        if closure is None:
            return "skipped"
        closed_on_text = closure["created_at"][:10]
        closed_on = date.fromisoformat(closed_on_text)
        policy = self._policy_effective_on(category, discipline, closed_on_text)
        if policy is None:
            return "skipped"
        extensions = records(self.connection.execute(
            "SELECT * FROM specimen_retention_extensions WHERE specimen_id=? ORDER BY version",
            (specimen_id,),
        ).fetchall())
        extension_months = sum(int(item["extension_months"]) for item in extensions)
        base_due = add_months(closed_on, int(policy["retention_months"]))
        due_on = add_months(base_due, extension_months)
        if due_on > as_of and not extensions:
            # 基础保存期尚未届满，本次清单不收录
            return "skipped"
        # 延期仍在有效期内（新到期日未到）：形成排除快照而非候选
        extension_in_force = bool(extensions) and due_on > as_of

        max_hold_id, active_holds = self._hold_snapshot(specimen_id)
        open_examination = self.connection.execute(
            "SELECT id,examination_no,status FROM examinations WHERE specimen_id=? "
            "AND status IN ('scheduled','running') ORDER BY id LIMIT 1",
            (specimen_id,),
        ).fetchone()
        open_review = self.connection.execute(
            "SELECT id,status,due_on FROM review_schedules WHERE specimen_id=? "
            f"AND status IN {OPEN_REVIEW_STATUSES} ORDER BY id LIMIT 1",
            (specimen_id,),
        ).fetchone()

        basis = {
            "case_closed_event_id": closure["id"],
            "case_closed_on": closed_on_text,
            "policy": {
                "id": policy["id"], "policy_code": policy["policy_code"], "version": policy["version"],
                "retention_months": policy["retention_months"],
                "effective_from": policy["effective_from"], "effective_to": policy["effective_to"],
            },
            "extensions": [
                {"id": item["id"], "version": item["version"], "extension_months": item["extension_months"],
                 "reason": item["reason"], "decided_by": item["decided_by"], "decided_at": item["decided_at"]}
                for item in extensions
            ],
            "hold_snapshot": {"max_hold_id": max_hold_id, "active_hold_ids": [int(h["id"]) for h in active_holds]},
        }

        blockers: list[str] = []
        blocker_detail: dict[str, Any] = {}
        if active_holds:
            blockers.append(BLOCKER_FROZEN)
            blocker_detail[BLOCKER_FROZEN] = [
                {"id": h["id"], "hold_type": h["hold_type"], "reason": h["reason"]} for h in active_holds
            ]
        if open_examination:
            blockers.append(BLOCKER_EXAMINATION)
            blocker_detail[BLOCKER_EXAMINATION] = {"id": open_examination["id"], "status": open_examination["status"]}
        if open_review:
            blockers.append(BLOCKER_REVIEW)
            blocker_detail[BLOCKER_REVIEW] = {"id": open_review["id"], "status": open_review["status"]}
        if extension_in_force:
            blockers.append(BLOCKER_EXTENSION)
            blocker_detail[BLOCKER_EXTENSION] = basis["extensions"]
        basis["blockers"] = blocker_detail

        status = "excluded" if blockers else "candidate"
        cursor = self.connection.execute(
            "INSERT INTO disposal_candidates(batch_id,specimen_id,status,exclusion_reason,specimen_no,case_id,case_no,"
            "specimen_category,discipline,case_closed_on,policy_id,policy_version,retention_months,base_due_on,"
            "extension_months,due_on,basis_json,snapshot_version,snapshot_status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                batch_id, specimen_id, status, "、".join(blockers), specimen["specimen_no"],
                specimen["case_id"], specimen["case_no"], category, discipline, closed_on_text,
                policy["id"], policy["version"], policy["retention_months"], base_due.isoformat(),
                extension_months, due_on.isoformat(),
                json.dumps(basis, ensure_ascii=False, sort_keys=True),
                specimen["version"], specimen["status"], timestamp, timestamp,
            ),
        )
        candidate_id = int(cursor.lastrowid)
        self._event(batch_id, candidate_id, status, actor, {
            "specimen_no": specimen["specimen_no"], "blockers": blockers,
        })
        return status

    def _hold_snapshot(self, specimen_id: int) -> tuple[int, list[dict[str, Any]]]:
        active = records(self.connection.execute(
            "SELECT * FROM specimen_holds WHERE specimen_id=? AND released_at IS NULL ORDER BY id",
            (specimen_id,),
        ).fetchall())
        row = self.connection.execute(
            "SELECT COALESCE(MAX(id),0) FROM specimen_holds WHERE specimen_id=?", (specimen_id,)
        ).fetchone()
        return int(row[0]), active

    # ------------------------------------------------------------------ 批量决定

    def record_decisions(self, batch_id: int, data: dict[str, Any]) -> dict[str, Any]:
        batch = self.require_batch(batch_id)
        if batch["status"] != "draft":
            raise ConflictError("只有草稿状态的处置清单可以逐项决定")
        applied: list[int] = []
        conflicts: list[dict[str, Any]] = []
        timestamp = to_storage(self.clock.now())
        for raw in data["decisions"]:
            candidate = record(self.connection.execute(
                "SELECT * FROM disposal_candidates WHERE id=? AND batch_id=?",
                (int(raw["candidate_id"]), batch_id),
            ).fetchone())
            if candidate is None:
                raise NotFoundError("处置候选项不存在")
            if candidate["status"] != "candidate":
                raise ConflictError("只有候选状态的条目可以作出决定", context={
                    "candidate_id": candidate["id"], "status": candidate["status"],
                })
            decision = raw["decision"]
            if decision not in {"retain", "extend", "destroy"}:
                raise ValidationError("决定只能是保留、延期或销毁")
            specimen = self.repository.require_specimen(int(candidate["specimen_id"]))

            conflict = self._decision_conflict(candidate, specimen)
            if conflict:
                # 只标出冲突，保留新事实，不覆盖
                self.connection.execute(
                    "UPDATE disposal_candidates SET status='conflicted',conflict_code=?,conflict_json=?,"
                    "conflict_at=?,updated_at=? WHERE id=?",
                    (
                        conflict["code"], json.dumps(conflict, ensure_ascii=False, sort_keys=True),
                        timestamp, timestamp, candidate["id"],
                    ),
                )
                self._event(batch_id, candidate["id"], "conflicted", data["actor"], conflict)
                conflicts.append({"candidate_id": candidate["id"], **conflict})
                continue

            reason = str(raw.get("reason", "")).strip()
            if decision in {"retain", "extend"} and not reason:
                raise ValidationError("保留或延期必须填写原因")
            new_status = {"retain": "retained", "extend": "extended", "destroy": "queued"}[decision]
            extension_months = int(candidate["extension_months"])
            if decision == "extend":
                months = int(raw.get("extension_months", 0))
                if months <= 0:
                    raise ValidationError("延期月数必须为正整数")
                latest = self.connection.execute(
                    "SELECT COALESCE(MAX(version),0) FROM specimen_retention_extensions WHERE specimen_id=?",
                    (specimen["id"],),
                ).fetchone()[0]
                self.connection.execute(
                    "INSERT INTO specimen_retention_extensions(specimen_id,version,extension_months,reason,decided_by,decided_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (specimen["id"], int(latest) + 1, months, reason, data["actor"], timestamp),
                )
                extension_months += months
            self.connection.execute(
                "UPDATE disposal_candidates SET status=?,decision=?,decision_reason=?,decided_by=?,decided_at=?,"
                "extension_months=?,updated_at=? WHERE id=?",
                (
                    new_status, decision, reason, data["actor"], timestamp,
                    extension_months, timestamp, candidate["id"],
                ),
            )
            self._event(batch_id, candidate["id"], f"decision.{decision}", data["actor"], {"reason": reason})
            applied.append(int(candidate["id"]))
        self.connection.execute("UPDATE disposal_batches SET updated_at=? WHERE id=?", (timestamp, batch_id))
        result = self.batch_detail(batch_id)
        result["decided"] = applied
        result["conflicts"] = conflicts
        return result

    def _decision_conflict(
        self, candidate: dict[str, Any], specimen: dict[str, Any]
    ) -> dict[str, Any] | None:
        """决定时刻对照新事实：版本/状态变化或新生阻断事项一律标冲突。"""
        if int(specimen["version"]) != int(candidate["snapshot_version"]):
            return {
                "code": "version_changed",
                "message": "检材版本在快照后已变化",
                "snapshot_version": candidate["snapshot_version"],
                "current_version": specimen["version"],
                "current_status": specimen["status"],
            }
        if specimen["status"] != candidate["snapshot_status"]:
            return {
                "code": "status_changed",
                "message": "检材状态在快照后已变化",
                "snapshot_status": candidate["snapshot_status"],
                "current_status": specimen["status"],
            }
        specimen_id = int(specimen["id"])
        holds = self.repository.active_holds(specimen_id)
        if holds:
            return {
                "code": "blocker_appeared",
                "message": "检材存在新生效的冻结",
                "blocker": BLOCKER_FROZEN,
                "holds": [{"id": h["id"], "hold_type": h["hold_type"]} for h in holds],
            }
        examination = self.connection.execute(
            "SELECT id FROM examinations WHERE specimen_id=? "
            f"AND status IN {UNFINISHED_EXAMINATION_STATUSES} LIMIT 1",
            (specimen_id,),
        ).fetchone()
        if examination:
            return {
                "code": "blocker_appeared",
                "message": "检材出现未完成检验",
                "blocker": BLOCKER_EXAMINATION,
                "examination_id": examination["id"],
            }
        review = self.connection.execute(
            "SELECT id FROM review_schedules WHERE specimen_id=? "
            f"AND status IN {OPEN_REVIEW_STATUSES} LIMIT 1",
            (specimen_id,),
        ).fetchone()
        if review:
            return {
                "code": "blocker_appeared",
                "message": "检材存在未关闭复核",
                "blocker": BLOCKER_REVIEW,
                "review_schedule_id": review["id"],
            }
        return None

    # ------------------------------------------------------------------ 提交与确认

    def submit_batch(self, batch_id: int, data: dict[str, Any]) -> dict[str, Any]:
        batch = self.require_batch(batch_id)
        if batch["status"] != "draft":
            raise ConflictError("只有草稿清单可以提交销毁")
        queued = self.connection.execute(
            "SELECT COUNT(*) FROM disposal_candidates WHERE batch_id=? AND status='queued'", (batch_id,)
        ).fetchone()[0]
        if int(queued) == 0:
            raise ConflictError("清单中没有提交销毁的条目")
        timestamp = to_storage(self.clock.now())
        self.connection.execute(
            "UPDATE disposal_batches SET status='submitted',submitted_by=?,submitted_at=?,"
            "version=version+1,updated_at=? WHERE id=?",
            (data["actor"], timestamp, timestamp, batch_id),
        )
        self._event(batch_id, None, "submitted", data["actor"], {"queued": int(queued)})
        return self.batch_detail(batch_id)

    def confirm_batch(self, batch_id: int, data: dict[str, Any]) -> dict[str, Any]:
        batch = self.require_batch(batch_id)
        role = data["role"]
        if role not in {"custodian", "supervisor"}:
            raise ValidationError("确认角色必须是保管人或监督人")
        if batch["status"] != "submitted":
            raise ConflictError("只有已提交清单可以确认")
        actor = data["actor"]
        custodian = batch["custodian_confirmed_by"]
        supervisor = batch["supervisor_confirmed_by"]
        if role == "custodian":
            if custodian:
                raise ConflictError("保管人已经确认")
            if supervisor and actor == supervisor:
                raise ConflictError("保管人与监督人不能为同一人")
        if role == "supervisor":
            if supervisor:
                raise ConflictError("监督人已经确认")
            if custodian and actor == custodian:
                raise ConflictError("保管人与监督人不能为同一人")
        timestamp = to_storage(self.clock.now())
        if role == "custodian":
            self.connection.execute(
                "UPDATE disposal_batches SET custodian_confirmed_by=?,custodian_confirmed_at=?,updated_at=? WHERE id=?",
                (actor, timestamp, timestamp, batch_id),
            )
        else:
            self.connection.execute(
                "UPDATE disposal_batches SET supervisor_confirmed_by=?,supervisor_confirmed_at=?,updated_at=? WHERE id=?",
                (actor, timestamp, timestamp, batch_id),
            )
        self._event(batch_id, None, f"confirmed.{role}", actor, {})
        # 监督人确认前先清扫一次确认期间出现的保全事件
        self._sweep_invalidations(batch_id, actor, timestamp)
        refreshed = self.require_batch(batch_id)
        if refreshed["custodian_confirmed_at"] and refreshed["supervisor_confirmed_at"]:
            self.connection.execute(
                "UPDATE disposal_batches SET status='confirmed',version=version+1,updated_at=? WHERE id=?",
                (timestamp, batch_id),
            )
            self._event(batch_id, None, "dual_confirmed", actor, {})
        return self.batch_detail(batch_id)

    def _sweep_invalidations(self, batch_id: int, actor: str, timestamp: str) -> int:
        """确认期间出现的任何新保全事件都使待销毁条目失效。"""
        batch = self.require_batch(batch_id)
        window_start = batch["submitted_at"]
        if not window_start:
            return 0
        count = 0
        queued = records(self.connection.execute(
            "SELECT * FROM disposal_candidates WHERE batch_id=? AND status='queued'", (batch_id,)
        ).fetchall())
        for candidate in queued:
            specimen_id = int(candidate["specimen_id"])
            new_hold = self.connection.execute(
                "SELECT id,hold_type,reason,imposed_by,imposed_at FROM specimen_holds "
                "WHERE specimen_id=? AND imposed_at>=? ORDER BY id LIMIT 1",
                (specimen_id, window_start),
            ).fetchone()
            if new_hold:
                detail = {
                    "message": "确认期间出现新的保全事件，销毁条目失效",
                    "hold": dict(new_hold),
                }
                self.connection.execute(
                    "UPDATE disposal_candidates SET status='invalid',invalid_reason=?,"
                    "invalidated_at=?,updated_at=? WHERE id=?",
                    (f"确认期间新生保全：{new_hold['hold_type']}", timestamp, timestamp, candidate["id"]),
                )
                self._event(batch_id, candidate["id"], "invalidated", actor, detail)
                count += 1
        return count

    # ------------------------------------------------------------------ 一次性执行

    def execute_batch(self, batch_id: int, data: dict[str, Any]) -> dict[str, Any]:
        batch = self.require_batch(batch_id)
        if batch["status"] != "confirmed":
            raise ConflictError("只有保管人与监督人均已确认的清单可以执行销毁")
        timestamp = to_storage(self.clock.now())
        # 执行前最后一次清扫与事实核对
        invalidated = self._sweep_invalidations(batch_id, data["actor"], timestamp)
        queued = records(self.connection.execute(
            "SELECT * FROM disposal_candidates WHERE batch_id=? AND status='queued'", (batch_id,)
        ).fetchall())
        destroyed: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        for candidate in queued:
            specimen = self.repository.require_specimen(int(candidate["specimen_id"]))
            conflict = self._execution_conflict(candidate, specimen)
            if conflict:
                self.connection.execute(
                    "UPDATE disposal_candidates SET status='conflicted',conflict_code=?,conflict_json=?,"
                    "conflict_at=?,updated_at=? WHERE id=?",
                    (
                        conflict["code"], json.dumps(conflict, ensure_ascii=False, sort_keys=True),
                        timestamp, timestamp, candidate["id"],
                    ),
                )
                self._event(batch_id, candidate["id"], "conflicted", data["actor"], conflict)
                conflicts.append({"candidate_id": candidate["id"], **conflict})
                continue
            result = self._destroy_one(batch_id, candidate, specimen, data["actor"], timestamp)
            destroyed.append(result)
        if not destroyed:
            # 没有可执行条目时清单保持已确认，等待人工处理失效/冲突项
            raise ConflictError("没有可执行销毁的条目（全部失效或存在冲突）", context={
                "invalidated": invalidated, "conflicts": len(conflicts),
            })
        self.connection.execute(
            "UPDATE disposal_batches SET status='executed',executed_by=?,executed_at=?,"
            "version=version+1,updated_at=? WHERE id=?",
            (data["actor"], timestamp, timestamp, batch_id),
        )
        self._event(batch_id, None, "executed", data["actor"], {
            "destroyed": len(destroyed), "invalidated": invalidated, "conflicted": len(conflicts),
        })
        result = self.batch_detail(batch_id)
        result["destroyed"] = destroyed
        result["skipped_conflicts"] = conflicts
        return result

    def _execution_conflict(
        self, candidate: dict[str, Any], specimen: dict[str, Any]
    ) -> dict[str, Any] | None:
        if specimen["status"] == "disposed":
            return {"code": "already_destroyed", "message": "检材已被销毁"}
        if int(specimen["version"]) != int(candidate["snapshot_version"]):
            return {
                "code": "version_changed", "message": "检材版本在快照后已变化",
                "snapshot_version": candidate["snapshot_version"], "current_version": specimen["version"],
            }
        if specimen["status"] != candidate["snapshot_status"]:
            return {
                "code": "status_changed", "message": "检材状态在快照后已变化",
                "snapshot_status": candidate["snapshot_status"], "current_status": specimen["status"],
            }
        holds = self.repository.active_holds(int(specimen["id"]))
        if holds:
            return {
                "code": "blocker_appeared", "message": "检材存在未解除冻结", "blocker": BLOCKER_FROZEN,
                "holds": [{"id": h["id"], "hold_type": h["hold_type"]} for h in holds],
            }
        return None

    def _destroy_one(
        self,
        batch_id: int,
        candidate: dict[str, Any],
        specimen: dict[str, Any],
        actor: str,
        timestamp: str,
    ) -> dict[str, Any]:
        specimen_id = int(specimen["id"])
        active_placements = records(self.connection.execute(
            "SELECT * FROM specimen_placements WHERE specimen_id=? AND removed_at IS NULL ORDER BY id",
            (specimen_id,),
        ).fetchall())
        containers = [
            {
                "placement_id": p["id"], "location_id": p["location_id"],
                "container_code": p["container_code"], "quantity": p["quantity"],
            }
            for p in active_placements
        ]
        quantity = float(specimen["available_quantity"])
        # 一次性更新：检材状态、容器摆放、流转记录在同一事务内完成
        self.connection.execute(
            "UPDATE specimens SET status='disposed',available_quantity=0,version=version+1,updated_at=? "
            "WHERE id=? AND version=?",
            (timestamp, specimen_id, candidate["snapshot_version"]),
        )
        for placement in active_placements:
            self.connection.execute(
                "UPDATE specimen_placements SET removed_at=?,version=version+1 WHERE id=? AND removed_at IS NULL",
                (timestamp, placement["id"]),
            )
        self.connection.execute(
            "INSERT INTO custody_events(specimen_id,movement_type,quantity,idempotency_key,actor,reason,created_at) "
            "VALUES(?, '报废', ?, ?, ?, ?, ?)",
            (
                specimen_id, -quantity if quantity else 0,
                f"disposal-{batch_id}-{specimen_id}", actor,
                f"依据处置清单 {candidate['batch_id']} 到期销毁", timestamp,
            ),
        )
        result = {
            "candidate_id": candidate["id"], "specimen_id": specimen_id,
            "specimen_no": specimen["specimen_no"], "quantity_cleared": quantity,
            "containers": containers, "destroyed_at": timestamp,
        }
        self.connection.execute(
            "UPDATE disposal_candidates SET status='destroyed',destroyed_at=?,result_json=?,updated_at=? WHERE id=?",
            (
                timestamp, json.dumps(result, ensure_ascii=False, sort_keys=True), timestamp, candidate["id"],
            ),
        )
        self._event(batch_id, candidate["id"], "destroyed", actor, result)
        return result

    # ------------------------------------------------------------------ 查询留痕

    def require_batch(self, batch_id: int) -> dict[str, Any]:
        batch = record(self.connection.execute(
            "SELECT * FROM disposal_batches WHERE id=?", (batch_id,)
        ).fetchone())
        if batch is None:
            raise NotFoundError("处置清单不存在")
        return batch

    def list_batches(self, status: str | None, limit: int, offset: int) -> tuple[list[dict[str, Any]], int]:
        where = ""
        params: list[Any] = []
        if status:
            where = "WHERE status=?"
            params.append(status)
        total = int(self.connection.execute(
            f"SELECT COUNT(*) FROM disposal_batches{where}", params
        ).fetchone()[0])
        rows = self.connection.execute(
            f"SELECT * FROM disposal_batches{where} ORDER BY id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        items = records(rows)
        for item in items:
            item["counts"] = dict(self.connection.execute(
                "SELECT "
                "SUM(status='candidate') AS candidates,"
                "SUM(status='excluded') AS excluded,"
                "SUM(status='queued') AS queued,"
                "SUM(status='retained') AS retained,"
                "SUM(status='extended') AS extended,"
                "SUM(status='conflicted') AS conflicted,"
                "SUM(status='invalid') AS invalid,"
                "SUM(status='destroyed') AS destroyed "
                "FROM disposal_candidates WHERE batch_id=?",
                (item["id"],),
            ).fetchone())
        return items, total

    def batch_detail(self, batch_id: int) -> dict[str, Any]:
        batch = self.require_batch(batch_id)
        batch["candidates"] = records(self.connection.execute(
            "SELECT * FROM disposal_candidates WHERE batch_id=? ORDER BY id", (batch_id,)
        ).fetchall())
        batch["events"] = records(self.connection.execute(
            "SELECT * FROM disposal_events WHERE batch_id=? ORDER BY id", (batch_id,)
        ).fetchall())
        counts = self.connection.execute(
            "SELECT "
            "SUM(status='candidate') AS candidates,"
            "SUM(status='excluded') AS excluded,"
            "SUM(status='queued') AS queued,"
            "SUM(status='retained') AS retained,"
            "SUM(status='extended') AS extended,"
            "SUM(status='conflicted') AS conflicted,"
            "SUM(status='invalid') AS invalid,"
            "SUM(status='destroyed') AS destroyed "
            "FROM disposal_candidates WHERE batch_id=?",
            (batch_id,),
        ).fetchone()
        batch["counts"] = {key: int(value or 0) for key, value in dict(counts).items()}
        return batch

    def candidate_trail(self, candidate_id: int) -> dict[str, Any]:
        """从某份清单的条目追溯候选依据、排除原因、双人确认与销毁结果。"""
        candidate = record(self.connection.execute(
            "SELECT * FROM disposal_candidates WHERE id=?", (candidate_id,)
        ).fetchone())
        if candidate is None:
            raise NotFoundError("处置候选项不存在")
        batch = self.require_batch(int(candidate["batch_id"]))
        candidate["batch"] = {
            key: batch[key] for key in (
                "id", "batch_no", "status", "as_of", "created_by",
                "submitted_by", "submitted_at", "custodian_confirmed_by", "custodian_confirmed_at",
                "supervisor_confirmed_by", "supervisor_confirmed_at", "executed_by", "executed_at",
            )
        }
        candidate["events"] = records(self.connection.execute(
            "SELECT * FROM disposal_events WHERE candidate_id=? ORDER BY id", (candidate_id,)
        ).fetchall())
        return candidate

    def _event(
        self,
        batch_id: int,
        candidate_id: int | None,
        event_type: str,
        data_actor: str | None,
        detail: dict[str, Any],
    ) -> None:
        self.connection.execute(
            "INSERT INTO disposal_events(batch_id,candidate_id,event_type,actor,detail_json,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (
                batch_id, candidate_id, event_type, data_actor or "system",
                json.dumps(detail, ensure_ascii=False, sort_keys=True), to_storage(self.clock.now()),
            ),
        )

    @staticmethod
    def _as_date(value: Any) -> date | None:
        if value is None or isinstance(value, date):
            return value
        if isinstance(value, datetime):
            return value.date()
        return date.fromisoformat(str(value)[:10])
