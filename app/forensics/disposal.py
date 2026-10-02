from __future__ import annotations

import json
import sqlite3
from datetime import date
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, DomainError, ValidationError
from app.forensics.examinations import add_months
from app.forensics.repository import ForensicRepository

OPEN_REVIEW_STATUSES = ("pending", "notified", "scheduled")
OPEN_EXAMINATION_STATUSES = ("scheduled", "running")
TERMINAL_DISPOSITIONS = {"excluded", "conflict", "invalidated", "destroyed"}


class DisposalService:
    """按保存策略生成到期处置快照，并管理决定、双人确认与销毁执行。"""

    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        self.repository = ForensicRepository(connection)

    def create_policy(self, data: dict[str, Any]) -> dict[str, Any]:
        latest = self.connection.execute(
            "SELECT version FROM retention_policies WHERE specimen_category=? ORDER BY version DESC LIMIT 1",
            (data["specimen_category"],),
        ).fetchone()
        version = int(latest[0]) + 1 if latest else 1
        timestamp = to_storage(self.clock.now())
        cursor = self.connection.execute(
            "INSERT INTO retention_policies(specimen_category,retention_months,effective_from,effective_to,version,"
            "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
            (
                data["specimen_category"], data["retention_months"], data["effective_from"],
                data.get("effective_to"), version, data["created_by"], timestamp,
            ),
        )
        return self.repository.require_retention_policy(int(cursor.lastrowid))

    def generate_batch(self, data: dict[str, Any]) -> dict[str, Any]:
        as_of_date = str(data["as_of_date"])
        timestamp = to_storage(self.clock.now())
        batch_no = data.get("batch_no") or self._next_batch_no(as_of_date)
        try:
            cursor = self.connection.execute(
                "INSERT INTO disposal_batches(batch_no,as_of_date,status,generated_by,created_at,updated_at) "
                "VALUES(?,?,'open',?,?,?)",
                (batch_no, as_of_date, data["generated_by"], timestamp, timestamp),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("处置批次编号已经存在") from exc
        batch_id = int(cursor.lastrowid)
        closures = {
            int(row[0]): str(row[1])[:10]
            for row in self.connection.execute(
                "SELECT case_id,MAX(created_at) FROM case_events WHERE to_status='retired' GROUP BY case_id"
            ).fetchall()
        }
        hold_counts = self._count_by_specimen("SELECT specimen_id,COUNT(*) FROM specimen_holds WHERE released_at IS NULL GROUP BY specimen_id")
        examination_counts = self._count_by_specimen(
            f"SELECT specimen_id,COUNT(*) FROM examinations WHERE status IN ({','.join('?' * len(OPEN_EXAMINATION_STATUSES))}) "
            "GROUP BY specimen_id",
            OPEN_EXAMINATION_STATUSES,
        )
        review_counts = self._count_by_specimen(
            f"SELECT specimen_id,COUNT(*) FROM review_schedules WHERE status IN ({','.join('?' * len(OPEN_REVIEW_STATUSES))}) "
            "GROUP BY specimen_id",
            OPEN_REVIEW_STATUSES,
        )
        extensions = {
            int(row[0]): str(row[1])
            for row in self.connection.execute(
                "SELECT e.specimen_id,e.extend_to FROM disposal_extensions e "
                "JOIN (SELECT specimen_id,MAX(version) AS v FROM disposal_extensions GROUP BY specimen_id) latest "
                "ON latest.specimen_id=e.specimen_id AND latest.v=e.version"
            ).fetchall()
        }
        specimens = self.connection.execute(
            "SELECT s.id,s.specimen_no,s.category,s.status,s.version,s.case_id FROM specimens s "
            "WHERE s.status NOT IN ('depleted','disposed') ORDER BY s.id"
        ).fetchall()
        for specimen in specimens:
            case_id = int(specimen["case_id"])
            closed_on = closures.get(case_id)
            if closed_on is None:
                continue
            category = str(specimen["category"])
            policy = self.repository.applicable_retention_policy(category, closed_on)
            due_on: str | None = None
            reasons: list[str] = []
            if policy is None:
                reasons.append("缺少当时有效的保存策略")
            else:
                due_on = add_months(date.fromisoformat(closed_on), int(policy["retention_months"])).isoformat()
                if due_on > as_of_date:
                    continue
                if hold_counts.get(int(specimen["id"])):
                    reasons.append("存在未解除的冻结")
                if examination_counts.get(int(specimen["id"])):
                    reasons.append("存在未完成的检验")
                if review_counts.get(int(specimen["id"])):
                    reasons.append("存在未关闭的复核")
                extend_to = extensions.get(int(specimen["id"]))
                if extend_to is not None and extend_to >= as_of_date:
                    reasons.append("存在有效的延期决定")
            self.connection.execute(
                "INSERT INTO disposal_items(batch_id,specimen_id,specimen_no,specimen_category,case_id,case_closed_on,"
                "policy_id,policy_version,retention_months,retention_due_on,specimen_status,specimen_version,disposition,"
                "exclusion_reasons_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    batch_id, int(specimen["id"]), specimen["specimen_no"], category, case_id, closed_on,
                    policy["id"] if policy else None, policy["version"] if policy else None,
                    policy["retention_months"] if policy else None, due_on,
                    specimen["status"], int(specimen["version"]),
                    "excluded" if reasons else "candidate",
                    json.dumps(reasons, ensure_ascii=False), timestamp, timestamp,
                ),
            )
        return self.repository.disposal_batch_detail(batch_id)

    def decide_items(self, batch_id: int, decisions: list[dict[str, Any]], actor: str) -> dict[str, Any]:
        batch = self.repository.require_disposal_batch(batch_id)
        if batch["status"] != "open":
            raise ConflictError("处置批次已执行，不能再登记决定")
        seen: set[int] = set()
        for decision in decisions:
            item_id = int(decision["item_id"])
            if item_id in seen:
                raise ValidationError("同一明细不能在同一批决定中重复出现")
            seen.add(item_id)
        timestamp = to_storage(self.clock.now())
        results: list[dict[str, Any]] = []
        for index, decision in enumerate(decisions, start=1):
            marker = f"disposal_decision_{index}"
            self.connection.execute(f"SAVEPOINT {marker}")
            try:
                outcome = self._decide_one(batch, decision, actor, timestamp)
                self.connection.execute(f"RELEASE SAVEPOINT {marker}")
                results.append(outcome)
            except DomainError as exc:
                self.connection.execute(f"ROLLBACK TO SAVEPOINT {marker}")
                self.connection.execute(f"RELEASE SAVEPOINT {marker}")
                results.append({"item_id": decision.get("item_id"), "outcome": "error", "message": exc.message})
        return {"batch": self.repository.disposal_batch_detail(batch_id), "results": results}

    def confirm(self, batch_id: int, item_id: int, data: dict[str, Any]) -> dict[str, Any]:
        batch = self.repository.require_disposal_batch(batch_id)
        if batch["status"] != "open":
            raise ConflictError("处置批次已执行，不能再确认")
        item = self.repository.require_disposal_item(item_id)
        if int(item["batch_id"]) != int(batch["id"]):
            raise ValidationError("明细不属于该处置批次")
        if item["disposition"] != "destruction_pending":
            raise ConflictError("该明细当前不在待销毁确认状态")
        timestamp = to_storage(self.clock.now())
        self._refresh_item_state(item, timestamp)
        role = data["role"]
        actor = data["actor"]
        if role == "custodian":
            if item["custodian_confirmed_by"]:
                raise ConflictError("保管人已经确认过该明细")
            if item["supervisor_confirmed_by"] == actor:
                raise ConflictError("保管人与监督人必须为不同人员")
            self.connection.execute(
                "UPDATE disposal_items SET custodian_confirmed_by=?,custodian_confirmed_at=?,updated_at=? WHERE id=?",
                (actor, timestamp, timestamp, item_id),
            )
        else:
            if item["supervisor_confirmed_by"]:
                raise ConflictError("监督人已经确认过该明细")
            if item["custodian_confirmed_by"] == actor:
                raise ConflictError("保管人与监督人必须为不同人员")
            self.connection.execute(
                "UPDATE disposal_items SET supervisor_confirmed_by=?,supervisor_confirmed_at=?,updated_at=? WHERE id=?",
                (actor, timestamp, timestamp, item_id),
            )
        return self.repository.require_disposal_item(item_id)

    def execute(self, batch_id: int, data: dict[str, Any]) -> dict[str, Any]:
        batch = self.repository.require_disposal_batch(batch_id)
        if batch["status"] != "open":
            raise ConflictError("处置批次已经执行过")
        items = self.connection.execute(
            "SELECT * FROM disposal_items WHERE batch_id=? ORDER BY id", (batch_id,)
        ).fetchall()
        undecided = [int(row["id"]) for row in items if row["disposition"] == "candidate"]
        if undecided:
            raise ConflictError("仍有未决定的候选明细", context={"item_ids": undecided})
        pending = [row for row in items if row["disposition"] == "destruction_pending"]
        incomplete = [
            int(row["id"]) for row in pending
            if not (row["custodian_confirmed_by"] and row["supervisor_confirmed_by"])
        ]
        if incomplete:
            raise ConflictError("仍有明细未完成保管人与监督人双人确认", context={"item_ids": incomplete})
        timestamp = to_storage(self.clock.now())
        destroyed: list[int] = []
        invalidated: list[int] = []
        conflicts: list[int] = []
        for row in pending:
            item = self.repository.require_disposal_item(int(row["id"]))
            specimen = self.repository.require_specimen(int(item["specimen_id"]))
            if self._preservation_events_since(int(specimen["id"]), str(item["confirmation_started_at"])):
                self._mark_item(item, "invalidated", "确认期间出现新的保全或冻结事件，销毁失效", timestamp)
                invalidated.append(int(item["id"]))
                continue
            reason = self._current_exclusion_reason(int(specimen["id"]), str(batch["as_of_date"]))
            if reason:
                self._mark_item(item, "invalidated", f"执行复核时发现{reason}，销毁失效", timestamp)
                invalidated.append(int(item["id"]))
                continue
            if int(specimen["version"]) != int(item["specimen_version"]) or specimen["status"] != item["specimen_status"]:
                self._mark_item(
                    item, "conflict",
                    f"检材状态或版本已变化（当前 {specimen['status']} v{specimen['version']}），未覆盖新事实",
                    timestamp,
                )
                conflicts.append(int(item["id"]))
                continue
            self._destroy_specimen(item, specimen, batch, data, timestamp)
            destroyed.append(int(item["id"]))
        self.connection.execute(
            "UPDATE disposal_batches SET status='executed',version=version+1,updated_at=? WHERE id=? AND status='open'",
            (timestamp, batch_id),
        )
        detail = self.repository.disposal_batch_detail(batch_id)
        detail["summary"] = {"destroyed": destroyed, "invalidated": invalidated, "conflicts": conflicts}
        return detail

    def _decide_one(self, batch: dict[str, Any], decision: dict[str, Any], actor: str, timestamp: str) -> dict[str, Any]:
        item = self.repository.require_disposal_item(int(decision["item_id"]))
        if int(item["batch_id"]) != int(batch["id"]):
            raise ValidationError("明细不属于该处置批次")
        if item["disposition"] in TERMINAL_DISPOSITIONS:
            raise ConflictError(f"明细已处于{item['disposition']}状态，不能在本批次再决定")
        specimen = self.repository.require_specimen(int(item["specimen_id"]))
        if int(specimen["version"]) != int(item["specimen_version"]) or specimen["status"] != item["specimen_status"]:
            note = f"检材状态或版本已变化（当前 {specimen['status']} v{specimen['version']}），未覆盖新事实"
            self._mark_item(item, "conflict", note, timestamp, actor=actor)
            return {"item_id": item["id"], "outcome": "conflict", "disposition": "conflict", "message": note}
        action = decision["action"]
        if action not in {"retain", "extend", "destroy"}:
            raise ValidationError("决定类型必须是保留、延期或提交销毁")
        note = decision.get("note") or None
        if action == "retain":
            self._mark_item(item, "retained", note, timestamp, actor=actor)
        elif action == "extend":
            extend_to = decision.get("extend_to")
            if not extend_to:
                raise ValidationError("延期决定必须提供延期日期")
            if str(extend_to) <= str(batch["as_of_date"]):
                raise ValidationError("延期日期必须晚于处置基准日")
            extension_id = self._create_extension(item, batch, str(extend_to), decision.get("reason") or note or "到期处置延期", actor, timestamp)
            self._mark_item(item, "extended", note, timestamp, actor=actor, extension_id=extension_id)
        else:
            self._mark_item(item, "destruction_pending", note, timestamp, actor=actor, restart_confirmation=True)
        refreshed = self.repository.require_disposal_item(int(item["id"]))
        return {"item_id": item["id"], "outcome": "decided", "disposition": refreshed["disposition"]}

    def _create_extension(
        self, item: dict[str, Any], batch: dict[str, Any], extend_to: str, reason: str, actor: str, timestamp: str
    ) -> int:
        latest = self.repository.latest_disposal_extension(int(item["specimen_id"]))
        version = int(latest["version"]) + 1 if latest else 1
        cursor = self.connection.execute(
            "INSERT INTO disposal_extensions(specimen_id,version,extend_to,reason,decided_by,batch_id,item_id,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (int(item["specimen_id"]), version, extend_to, reason, actor, batch["id"], item["id"], timestamp),
        )
        return int(cursor.lastrowid)

    def _mark_item(
        self,
        item: dict[str, Any],
        disposition: str,
        note: str | None,
        timestamp: str,
        *,
        actor: str | None = None,
        extension_id: int | None = None,
        restart_confirmation: bool = False,
    ) -> None:
        assignments = ["disposition=?", "decision_note=?", "updated_at=?"]
        params: list[Any] = [disposition, note, timestamp]
        if actor is not None:
            assignments.extend(["decided_by=?", "decided_at=?"])
            params.extend([actor, timestamp])
        if extension_id is not None:
            assignments.append("extension_id=?")
            params.append(extension_id)
        if restart_confirmation:
            assignments.append(
                "confirmation_started_at=?,custodian_confirmed_by=NULL,custodian_confirmed_at=NULL,"
                "supervisor_confirmed_by=NULL,supervisor_confirmed_at=NULL"
            )
            params.append(timestamp)
        params.append(int(item["id"]))
        self.connection.execute(f"UPDATE disposal_items SET {','.join(assignments)} WHERE id=?", params)

    def _refresh_item_state(self, item: dict[str, Any], timestamp: str) -> None:
        specimen = self.repository.require_specimen(int(item["specimen_id"]))
        if int(specimen["version"]) != int(item["specimen_version"]) or specimen["status"] != item["specimen_status"]:
            self._mark_item(item, "conflict", f"检材状态或版本已变化（当前 {specimen['status']} v{specimen['version']}）", timestamp)
            raise ConflictError("检材状态或版本已变化，明细已标记冲突")
        if self._preservation_events_since(int(specimen["id"]), str(item["confirmation_started_at"])):
            self._mark_item(item, "invalidated", "确认期间出现新的保全或冻结事件，销毁失效", timestamp)
            raise ConflictError("确认期间出现新的保全事件，明细已失效")

    def _preservation_events_since(self, specimen_id: int, since: str) -> bool:
        row = self.connection.execute(
            "SELECT COUNT(*) FROM specimen_holds WHERE specimen_id=? AND (released_at IS NULL OR imposed_at>?)",
            (specimen_id, since),
        ).fetchone()
        return int(row[0]) > 0

    def _current_exclusion_reason(self, specimen_id: int, as_of_date: str) -> str | None:
        placeholders = ",".join("?" * len(OPEN_EXAMINATION_STATUSES))
        if int(self.connection.execute(
            f"SELECT COUNT(*) FROM examinations WHERE specimen_id=? AND status IN ({placeholders})",
            (specimen_id, *OPEN_EXAMINATION_STATUSES),
        ).fetchone()[0]):
            return "存在未完成的检验"
        placeholders = ",".join("?" * len(OPEN_REVIEW_STATUSES))
        if int(self.connection.execute(
            f"SELECT COUNT(*) FROM review_schedules WHERE specimen_id=? AND status IN ({placeholders})",
            (specimen_id, *OPEN_REVIEW_STATUSES),
        ).fetchone()[0]):
            return "存在未关闭的复核"
        if self.repository.active_disposal_extension(specimen_id, as_of_date):
            return "存在有效的延期决定"
        return None

    def _destroy_specimen(
        self,
        item: dict[str, Any],
        specimen: dict[str, Any],
        batch: dict[str, Any],
        data: dict[str, Any],
        timestamp: str,
    ) -> None:
        specimen_id = int(specimen["id"])
        self.connection.execute(
            "UPDATE specimen_placements SET removed_at=?,version=version+1 WHERE specimen_id=? AND removed_at IS NULL",
            (timestamp, specimen_id),
        )
        remaining = float(specimen["available_quantity"])
        cursor = self.connection.execute(
            "INSERT INTO custody_events(specimen_id,movement_type,quantity,idempotency_key,actor,reason,created_at) "
            "VALUES(?,'报废',?,?,?,?,?)",
            (
                specimen_id, -remaining, f"disposal-execute-{item['id']}", data["actor"],
                f"处置批次 {batch['batch_no']} 到期销毁：{data.get('reason', '')}".strip("："), timestamp,
            ),
        )
        self.connection.execute(
            "UPDATE specimens SET status='disposed',available_quantity=0,version=version+1,updated_at=? WHERE id=?",
            (timestamp, specimen_id),
        )
        self.connection.execute(
            "UPDATE disposal_items SET disposition='destroyed',destroyed_at=?,custody_event_id=?,updated_at=? WHERE id=?",
            (timestamp, int(cursor.lastrowid), timestamp, int(item["id"])),
        )

    def _count_by_specimen(self, query: str, params: tuple = ()) -> dict[int, int]:
        return {
            int(row[0]): int(row[1])
            for row in self.connection.execute(query, params).fetchall()
        }

    def _next_batch_no(self, as_of_date: str) -> str:
        count = int(self.connection.execute("SELECT COUNT(*) FROM disposal_batches").fetchone()[0])
        return f"DSP-{as_of_date}-{count + 1:03d}"
