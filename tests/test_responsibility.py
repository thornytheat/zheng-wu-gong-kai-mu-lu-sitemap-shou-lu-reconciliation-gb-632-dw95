"""M8 地址责任账：带生效时点的责任变更、旧账只读、新账归新站、失败保留、幂等。

诉求与测试对应：
- 按地址存带生效时点的责任变更，同址不拆两笔：test_change_chain_is_temporal
- 切换后新观测进新责任站、切换前缺陷保留原站：test_old_ledger_frozen_new_observations_follow
- 切换失败保留旧账：test_failed_change_keeps_old_responsibility
- 重跑/续跑不重复迁移：test_migration_is_idempotent / test_resume_keeps_snapshot
- 页面与导出同一责任版本：test_page_csv_and_manifest_share_version
"""
from __future__ import annotations

import pytest

from app import errors
from app.modules.m5_batches import service as batch_service
from app.modules.m6_defects import service as defect_service
from app.modules.m8_responsibility import ledger, service as resp_service
from fixtures.expected import FINDINGS

# 市府门户 site-a 的一个真实地址，下季度移交部门站 site-b 维护
TRANSFER_URL = "http://www.hz.gov.cn/site-a/notice/n-2024-001"
TRANSFER_KEY = "http://www.hz.gov.cn/site-a/notice/n-2024-001"
# 生效时点早于批次真实开始时间（今天 2026-11-16），巡检时变更已到期；
# BEFORE 用来验证生效前仍归原站的时态行为
SWITCH_AT = "2026-10-01T00:00:00Z"
BEFORE = "2026-09-01T00:00:00Z"
AFTER = "2026-11-15T00:00:00Z"
BASIS = "河政办移交〔2026〕12 号"


# ---------- 纯领域：时点解析 ----------

def test_default_responsibility_is_host_site():
    resp = ledger.resolve([], TRANSFER_KEY, AFTER)
    assert resp.site == "site-a"
    assert resp.basis_type == "host"


def test_change_chain_is_temporal_and_keeps_single_key():
    change = {"url_key": TRANSFER_KEY, "from_site": "site-a", "to_site": "site-b",
              "effective_at": SWITCH_AT, "status": "active", "change_id": "rc-abc",
              "basis": BASIS}
    before = ledger.resolve([change], TRANSFER_KEY, BEFORE)
    after = ledger.resolve([change], TRANSFER_KEY, AFTER)
    assert before.site == "site-a" and before.basis_type == "host"
    assert after.site == "site-b" and after.basis_type == "transfer"
    assert after.change_id == "rc-abc" and after.basis == BASIS
    # 同一把键在两个时点解析，绝不产生第二个地址
    assert before is not after and ledger.host_responsibility(TRANSFER_KEY).site == "site-a"


def test_pending_and_failed_never_take_effect():
    for status in ("pending", "failed"):
        change = {"url_key": TRANSFER_KEY, "from_site": "site-a", "to_site": "site-b",
                  "effective_at": "2020-01-01T00:00:00Z", "status": status,
                  "change_id": f"rc-{status}", "basis": ""}
        resp = ledger.resolve([change], TRANSFER_KEY, AFTER)
        assert resp.site == "site-a" and resp.basis_type == "host"


def test_latest_effective_wins_and_version_changes_with_ledger():
    first = {"url_key": TRANSFER_KEY, "from_site": "site-a", "to_site": "site-b",
             "effective_at": SWITCH_AT, "status": "active", "change_id": "rc-1", "basis": ""}
    second = {"url_key": TRANSFER_KEY, "from_site": "site-b", "to_site": "site-c",
              "effective_at": "2027-06-01T00:00:00Z", "status": "active",
              "change_id": "rc-2", "basis": ""}
    assert ledger.resolve([first, second], TRANSFER_KEY, "2027-07-01T00:00:00Z").site == "site-c"
    v_none = ledger.responsibility_version([])
    v_one = ledger.responsibility_version([first])
    v_two = ledger.responsibility_version([first, second])
    assert v_none == "none" and v_one != v_two and v_one != "none"


# ---------- 服务层：登记校验 ----------

def test_register_normalizes_url_and_defaults_from_site(store):
    row = resp_service.register_change(
        store, "http://www.hz.gov.cn/site-a/Notice/N-2024-001?utm_source=x",
        "site-b", SWITCH_AT, BASIS)
    assert row["url_key"] == TRANSFER_KEY
    assert row["from_site"] == "site-a" and row["to_site"] == "site-b"
    assert row["status"] == "pending" and row["basis"] == BASIS
    assert row["change_id"].startswith("rc-")


def test_register_rejects_bad_inputs(store):
    with pytest.raises(errors.BadRequest):
        resp_service.register_change(store, "javascript:alert(1)", "site-b", SWITCH_AT)
    with pytest.raises(errors.BadRequest):
        resp_service.register_change(store, TRANSFER_URL, "site-z", SWITCH_AT)
    with pytest.raises(errors.BadRequest):
        resp_service.register_change(store, TRANSFER_URL, "site-a", SWITCH_AT)
    with pytest.raises(errors.BadRequest):
        resp_service.register_change(store, TRANSFER_URL, "site-b", "2026/12/01")
    offsite = "http://zwfw.other.gov.cn/site-b/notice/n-9"
    with pytest.raises(errors.BadRequest):
        resp_service.register_change(store, offsite, "site-b", SWITCH_AT)


def test_register_same_content_is_idempotent(store):
    first = resp_service.register_change(store, TRANSFER_URL, "site-b", SWITCH_AT, BASIS)
    second = resp_service.register_change(store, TRANSFER_URL, "site-b", SWITCH_AT, BASIS)
    assert first["change_id"] == second["change_id"]
    assert len(store.list_changes()) == 1


def test_register_conflict_on_same_time_different_content(store):
    resp_service.register_change(store, TRANSFER_URL, "site-b", SWITCH_AT, BASIS)
    with pytest.raises(errors.Conflict):
        resp_service.register_change(store, TRANSFER_URL, "site-c", SWITCH_AT, BASIS)


# ---------- 迁移器：到期、失败保留、幂等 ----------

def test_apply_activates_only_due_pending(store):
    resp_service.register_change(store, TRANSFER_URL, "site-b", SWITCH_AT, BASIS)
    early = resp_service.apply_pending(store, as_of=BEFORE)
    assert early["processed"] == 0 and store.list_changes()[0]["status"] == "pending"
    due = resp_service.apply_pending(store, as_of=AFTER)
    assert due["processed"] == 1 and len(due["activated"]) == 1 and due["failed"] == []
    assert store.list_changes()[0]["status"] == "active"
    assert store.list_changes()[0]["applied_at"] == AFTER


def test_migration_is_idempotent(store):
    resp_service.register_change(store, TRANSFER_URL, "site-b", SWITCH_AT, BASIS)
    first = resp_service.apply_pending(store, as_of=AFTER)
    second = resp_service.apply_pending(store, as_of=AFTER)
    assert first["processed"] == 1 and second["processed"] == 0
    assert len(store.list_changes()) == 1
    assert store.list_changes()[0]["status"] == "active"


def test_failed_change_keeps_old_responsibility(store, monkeypatch):
    row = resp_service.register_change(store, TRANSFER_URL, "site-b", SWITCH_AT, BASIS)
    # 模拟切换那一刻接管站被移出清单：迁移判失败，置 failed 并留原因
    monkeypatch.setattr(resp_service.config, "SITE_BY_CODE",
                        {k: v for k, v in resp_service.config.SITE_BY_CODE.items()
                         if k != "site-b"}, raising=False)
    outcome = resp_service.apply_pending(store, as_of=AFTER)
    assert len(outcome["failed"]) == 1 and outcome["activated"] == []
    stored = store.get_change(row["change_id"])
    assert stored["status"] == "failed" and stored["failure_reason"]
    # 解析器不命中 failed：旧账仍是原站 site-a
    resp = ledger.resolve(store.list_changes(), TRANSFER_KEY, AFTER)
    assert resp.site == "site-a" and resp.basis_type == "host"


def test_apply_rolls_back_on_unexpected_error(store):
    resp_service.register_change(store, TRANSFER_URL, "site-b", SWITCH_AT, BASIS)
    original = store.update_change_status

    def boom(change_id, status, updated_at, applied_at=None, failure_reason=""):
        raise RuntimeError("disk full")

    store.update_change_status = boom
    with pytest.raises(RuntimeError):
        resp_service.apply_pending(store, as_of=AFTER)
    store.update_change_status = original
    # 事务回滚：仍是 pending，没有半生效
    assert store.list_changes()[0]["status"] == "pending"


def test_resolve_endpoint_preview(store):
    resp_service.register_change(store, TRANSFER_URL, "site-b", SWITCH_AT, BASIS)
    resp_service.apply_pending(store, as_of=AFTER)
    preview = resp_service.resolve_url(store, TRANSFER_URL, AFTER)
    assert preview["site"] == "site-b" and preview["basis_label"] == "移交变更"
    assert preview["ref"].startswith("rc-")


# ---------- 巡检集成：旧账只读、新账归新站、同址不拆 ----------

def _defect_for(run: dict, key: str) -> dict:
    matches = [d for d in run["defects"] if d["url"] == key]
    return matches[0] if matches else None


def _probe_for(run: dict, key: str) -> dict:
    return next(p for p in run["probes"] if p["key"] == key)


def test_run_without_ledger_uses_host_responsibility(settings, store):
    run = batch_service.run(settings, store)["run"]
    assert run["responsibility"]["version"] == "none"
    probe = _probe_for(run, TRANSFER_KEY)
    assert probe["site"] == probe["responsible_site"] == "site-a"
    assert probe["responsibility_basis"] == "host"
    assert len(run["probes"]) == 23  # 同址仍只有一笔，责任账不拆账


def test_old_ledger_frozen_new_observations_follow(settings, store):
    # 第一批：责任账为空，缺陷按主机归属记在 site-a
    first = batch_service.run(settings, store)["run"]
    first_probe = _probe_for(first, TRANSFER_KEY)
    assert first_probe["responsible_site"] == "site-a"
    first_version = first["responsibility"]["version"]
    assert first_version == "none"

    # 登记下季度移交并生效
    resp_service.register_change(store, TRANSFER_URL, "site-b", SWITCH_AT, BASIS)
    resp_service.apply_pending(store, as_of=AFTER)

    # 同输入重跑：责任版本变了 → 新批次、新 run_id
    second = batch_service.run(settings, store)["run"]
    assert second["run_id"] != first["run_id"]
    assert second["responsibility"]["version"] != first_version
    assert store.count_runs() == 2

    # 新观测进新责任站，来源站仍是 site-a（地址还是从市府门户采到的）
    second_probe = _probe_for(second, TRANSFER_KEY)
    assert second_probe["site"] == "site-a"
    assert second_probe["responsible_site"] == "site-b"
    assert second_probe["responsibility_basis"] == "transfer"
    assert second_probe["responsibility_change"].startswith("rc-")

    # 同址不拆两笔：两批的 key 数量一致
    assert len(second["probes"]) == len(first["probes"])

    # 旧账只读：第一批冻结的责任站仍是 site-a，不受事后变更影响
    frozen = store.get_run(first["run_id"])
    assert _probe_for(frozen, TRANSFER_KEY)["responsible_site"] == "site-a"
    old_defect = _defect_for(frozen, TRANSFER_KEY)
    if old_defect is not None:
        assert old_defect["responsible_site"] == "site-a"
    new_defect = _defect_for(second, TRANSFER_KEY)
    if new_defect is not None:
        assert new_defect["responsible_site"] == "site-b"
        assert new_defect["site"] == "site-a"  # 来源站不变


def test_rerun_after_ledger_settles_is_still_idempotent(settings, store):
    resp_service.register_change(store, TRANSFER_URL, "site-b", SWITCH_AT, BASIS)
    resp_service.apply_pending(store, as_of=AFTER)
    first = batch_service.run(settings, store)["run"]
    second = batch_service.run(settings, store)
    assert second["reused"] is True
    assert second["run"]["run_id"] == first["run_id"]
    assert store.count_runs() == 1
    # 迁移器没有重复处理
    assert store.list_changes()[0]["status"] == "active"


def test_resume_keeps_frozen_responsibility_snapshot(settings, store):
    resp_service.register_change(store, TRANSFER_URL, "site-b", SWITCH_AT, BASIS)
    resp_service.apply_pending(store, as_of=AFTER)
    partial = batch_service.run(settings, store, stop_after="crawl")["run"]
    version = partial["responsibility"]["version"]
    resumed = batch_service.run(settings, store, mode="resume",
                               resume_of=partial["run_id"])["run"]
    assert resumed["responsibility"]["version"] == version
    assert _probe_for(resumed, TRANSFER_KEY)["responsible_site"] == "site-b"
    assert store.count_runs() == 1  # 续跑不产生新批次、不重复迁移


# ---------- 页面与导出同一责任版本 ----------

def test_page_csv_and_manifest_share_version(settings, store, client):
    resp_service.register_change(store, TRANSFER_URL, "site-b", SWITCH_AT, BASIS)
    resp_service.apply_pending(store, as_of=AFTER)
    client.post("/api/crawl", json={})

    api = client.get("/api/defects").json()
    csv_text = client.get("/api/defects.csv").text
    manifest = client.get("/api/export/manifest.json").json()
    page = client.get("/defects")
    ledger_page = client.get("/responsibility")

    run = store.latest_run()
    version = run["responsibility"]["version"]
    assert manifest["responsibility_version"] == version
    # 变更已在巡检前手动迁移生效，巡检内迁移器无 pending 可处理（不重复迁移）
    assert manifest["responsibility_applied"]["processed"] == 0
    assert manifest["responsibility_applied"]["version"] == version

    # 责任站在 API、CSV、页面三处一致
    api_row = next(r for r in api["rows"] if r["url"] == TRANSFER_KEY)
    assert api_row["responsible_site"] == "site-b"
    assert "site-b" in csv_text and "移交变更 rc-" in csv_text
    assert "责任 site-b" in page.text
    assert version in ledger_page.text and BASIS in ledger_page.text
    # 责任站筛选生效
    filtered = client.get("/api/defects", params={"responsible_site": "site-b"}).json()
    assert all(r["responsible_site"] == "site-b" for r in filtered["rows"])
    assert any(r["url"] == TRANSFER_KEY for r in filtered["rows"])


def test_watch_snapshot_uses_responsible_site(settings, store):
    from app.modules.m7_watch import diff
    resp_service.register_change(store, TRANSFER_URL, "site-b", SWITCH_AT, BASIS)
    resp_service.apply_pending(store, as_of=AFTER)
    run = batch_service.run(settings, store)["run"]
    snap = diff.snapshot(run)
    row = next(r for r in snap["rows"] if r["url"] == TRANSFER_KEY)
    assert row["site"] == "site-b"            # 责任站
    assert row["source_site"] == "site-a"     # 来源站保留


def test_responsibility_breakdown_moves_findings(settings, store):
    resp_service.register_change(store, TRANSFER_URL, "site-b", SWITCH_AT, BASIS)
    resp_service.apply_pending(store, as_of=AFTER)
    run = batch_service.run(settings, store)["run"]
    source = {row["site"]: row for row in run["by_site"]}
    responsible = {row["site"]: row for row in run["by_responsible_site"]}
    # 来源站视图里该地址仍在 site-a；责任站视图里它被计入 site-b
    assert "site-a" in source and "site-b" in responsible
    total_source = sum(r["total"] for r in run["by_site"])
    total_resp = sum(r["total"] for r in run["by_responsible_site"])
    assert total_source == total_resp == 23  # 总量守恒，只是归属视角不同
    # 缺陷总数不因为责任迁移而变化
    assert run["totals"]["findings_total"] == FINDINGS
