from __future__ import annotations


def test_http_intake_and_custody_flow(client, admin):
    headers = admin["headers"]
    source = client.post("/api/forensics/agencies", headers=headers, json={
        "agency_code": "HTTP-ORG-1", "agency_name": "区公安分局", "jurisdiction_code": "CN", "contact_address": "司法路 12 号",
        "restrictions": {},
    })
    assert source.status_code == 201, source.text
    forensic_case = client.post("/api/forensics/cases", headers=headers, json={
        "case_no": "HTTP-CASE-1", "case_name": "交通事故痕迹鉴定", "discipline": "痕迹物证",
        "entrusted_matter": "车辆碰撞痕迹比对", "agency_id": source.json()["id"], "case_source": "委派",
        "accepted_on": "2026-09-20", "passport": {}, "created_by": "登记员",
    })
    assert forensic_case.status_code == 201, forensic_case.text
    accepted = client.post(f"/api/forensics/cases/{forensic_case.json()['id']}/transition", headers=headers, json={
        "target_status": "accepted", "reason": "手续齐全", "expected_version": 1, "actor": "审核员",
    })
    assert accepted.status_code == 200, accepted.text
    location = client.post("/api/forensics/locations", headers=headers, json={
        "location_code": "HTTP-L1", "facility": "中期库", "room": "一室", "rack": "A", "shelf": "1",
        "capacity_units": 3000, "reference_value": 4, "humidity_percent": 35,
    })
    assert location.status_code == 201, location.text
    specimen = client.post("/api/forensics/specimens", headers=headers, json={
        "specimen_no": "HTTP-SP-1", "case_id": accepted.json()["id"], "received_year": 2026,
        "initial_quantity": 8, "integrity_percent": 100, "packaging": "独立封装", "created_by": "登记员",
    })
    assert specimen.status_code == 201, specimen.text
    placed = client.post("/api/forensics/placements", headers=headers, json={
        "specimen_id": specimen.json()["id"], "location_id": location.json()["id"], "quantity": 8,
        "container_code": "HTTP-BOX-1", "idempotency_key": "http-place-0001", "actor": "保管员",
    })
    assert placed.status_code == 201, placed.text
    detail = client.get(f"/api/forensics/specimens/{specimen.json()['id']}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["status"] == "stored"
    assert detail.json()["placements"][0]["container_code"] == "HTTP-BOX-1"


def test_api_rejects_unauthenticated_business_request(client):
    response = client.get("/api/forensics/dashboard")
    assert response.status_code == 401


def test_api_validation_error_has_structured_body(client, admin):
    response = client.post("/api/forensics/locations", headers=admin["headers"], json={
        "location_code": "BAD", "facility": "库", "room": "一室", "rack": "A", "shelf": "1",
        "capacity_units": -1, "reference_value": 4, "humidity_percent": 35,
    })
    assert response.status_code == 422
    assert response.json()["detail"]


def test_disposal_flow_over_http_requires_authentication(client, admin):
    # 未认证不能查看处置清单
    assert client.get("/api/forensics/disposal-batches").status_code == 401

    headers = admin["headers"]
    source = client.post("/api/forensics/agencies", headers=headers, json={
        "agency_code": "DISP-ORG", "agency_name": "处置流程分局", "jurisdiction_code": "CN",
        "contact_address": "司法路 1 号", "restrictions": {},
    }).json()
    forensic_case = client.post("/api/forensics/cases", headers=headers, json={
        "case_no": "DISP-CASE-1", "case_name": "到期处置鉴定", "discipline": "法医物证",
        "agency_id": source["id"], "case_source": "委托", "accepted_on": "2026-01-10",
        "created_by": "登记员",
    }).json()
    client.post(f"/api/forensics/cases/{forensic_case['id']}/transition", headers=headers, json={
        "target_status": "accepted", "reason": "齐全", "expected_version": 1, "actor": "审核员",
    })
    location = client.post("/api/forensics/locations", headers=headers, json={
        "location_code": "DISP-L1", "facility": "库", "room": "室", "rack": "A", "shelf": "1",
        "capacity_units": 100, "reference_value": 4, "humidity_percent": 40,
    }).json()
    specimen = client.post("/api/forensics/specimens", headers=headers, json={
        "specimen_no": "DISP-SP-1", "case_id": forensic_case["id"], "specimen_category": "生物检材",
        "received_year": 2026, "initial_quantity": 20, "packaging": "封装", "created_by": "登记员",
    }).json()
    client.post("/api/forensics/placements", headers=headers, json={
        "specimen_id": specimen["id"], "location_id": location["id"], "quantity": 20,
        "container_code": "DISP-BOX-1", "idempotency_key": "disp-place-0001", "actor": "保管员",
    })
    client.post("/api/forensics/retention-policies", headers=headers, json={
        "policy_code": "RT-HTTP", "specimen_category": "生物检材", "discipline": "法医物证",
        "retention_months": 1, "effective_from": "2020-01-01", "created_by": "档案负责人",
    })
    client.post(f"/api/forensics/cases/{forensic_case['id']}/transition", headers=headers, json={
        "target_status": "retired", "reason": "结案", "expected_version": 2, "actor": "审核员",
    })

    batch = client.post("/api/forensics/disposal-batches", headers=headers, json={
        "as_of": "2027-06-01", "created_by": "档案管理员",
    })
    assert batch.status_code == 201, batch.text
    batch_id = batch.json()["id"]
    candidate_id = batch.json()["candidates"][0]["id"]

    decided = client.post(f"/api/forensics/disposal-batches/{batch_id}/decisions", headers=headers, json={
        "actor": "档案管理员", "decisions": [{"candidate_id": candidate_id, "decision": "destroy"}],
    })
    assert decided.status_code == 200, decided.text
    client.post(f"/api/forensics/disposal-batches/{batch_id}/submit", headers=headers, json={"actor": "档案管理员"})
    client.post(f"/api/forensics/disposal-batches/{batch_id}/confirm", headers=headers,
                json={"role": "custodian", "actor": "保管员A"})
    client.post(f"/api/forensics/disposal-batches/{batch_id}/confirm", headers=headers,
                json={"role": "supervisor", "actor": "监督员B"})
    executed = client.post(f"/api/forensics/disposal-batches/{batch_id}/execute", headers=headers,
                           json={"actor": "档案管理员"})
    assert executed.status_code == 200, executed.text
    assert executed.json()["status"] == "executed"

    trail = client.get(f"/api/forensics/disposal-candidates/{candidate_id}/trail", headers=headers)
    assert trail.status_code == 200
    body = trail.json()
    assert body["batch"]["custodian_confirmed_by"] == "保管员A"
    assert body["batch"]["supervisor_confirmed_by"] == "监督员B"
    assert body["result"]["containers"][0]["container_code"] == "DISP-BOX-1"
    assert body["status"] == "destroyed"
