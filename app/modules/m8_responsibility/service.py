"""M8 编排：责任变更登记、到期迁移、时点试算与责任版本。

迁移器是「切换失败保留旧账、重跑续跑不重复迁移」的落点：
- 只处理 status=pending 且到期的行，active/failed 永不再碰；
- 整批在一个事务里，任一条抛错整体回滚，不出现半生效；
- 单条校验失败把该行置 failed 并留原因，解析器不命中 failed，旧账原样保留。
"""
from __future__ import annotations

import datetime as dt

from ... import config, errors, manifest
from ..m2_url import normalize as normalize_mod
from ..m2_url import scope
from . import ledger

STAMP_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _parse_stamp(value: str, field: str) -> str:
    """生效时点必须是 UTC 秒级 ISO 串（与全库时间戳一致），非法直接 400。"""
    text = (value or "").strip()
    try:
        dt.datetime.strptime(text, STAMP_FMT)
    except ValueError as exc:
        raise errors.BadRequest(f"{field} 必须是 {STAMP_FMT} 格式的 UTC 时间",
                                code="E_BAD_RESP_TIME") from exc
    if text.endswith("Z"):
        return text
    return text + "Z"


def _normalize_key(url: str) -> str:
    norm = normalize_mod.safe_normalize(url)
    if norm is None:
        raise errors.BadRequest(f"地址无法规范化：{url}", code="E_BAD_RESP_URL")
    if not scope.in_scope(norm.url):
        raise errors.BadRequest(f"地址不在站点清单内：{norm.url}",
                                code="E_RESP_OFFSITE")
    return norm.url


def _require_site(code: str, field: str) -> str:
    if code not in config.SITE_BY_CODE:
        raise errors.BadRequest(f"{field} 站点未知：{code}", code="E_BAD_RESP_SITE")
    return code


def register_change(store, url: str, to_site: str, effective_at: str,
                    basis: str = "", from_site: str | None = None) -> dict:
    """登记一条 pending 责任变更。同 (地址, 生效时点) 已存在则幂等返回既有行。"""
    url_key = _normalize_key(url)
    to_code = _require_site(to_site, "to_site")
    stamp = _parse_stamp(effective_at, "effective_at")
    host_site = scope.site_of(url_key)
    from_code = _require_site(from_site, "from_site") if from_site else host_site
    if from_code == to_code:
        raise errors.BadRequest("来源站与接管站相同，无需变更",
                                code="E_RESP_SAME_SITE")
    change_id = ledger.change_id_of(url_key, from_code, to_code, stamp, basis or "")
    existing = store.find_change(url_key, stamp)
    if existing is not None:
        if existing["change_id"] != change_id:
            raise errors.Conflict(
                f"该地址在 {stamp} 已登记变更 {existing['change_id']}",
                code="E_RESP_DUP_TIME")
        return existing

    now = manifest.now_utc()
    row = {"change_id": change_id, "url_key": url_key, "from_site": from_code,
           "to_site": to_code, "effective_at": stamp, "basis": basis or "",
           "status": "pending", "applied_at": None, "failure_reason": "",
           "created_at": now, "updated_at": now}
    return store.insert_change(row)


def _validate_for_apply(row: dict) -> str | None:
    """迁移前再校验一次：站点与地址仍有效才生效，否则返回失败原因。"""
    if row["to_site"] not in config.SITE_BY_CODE:
        return f"接管站 {row['to_site']} 已不在站点清单"
    if row["from_site"] not in config.SITE_BY_CODE:
        return f"来源站 {row['from_site']} 已不在站点清单"
    if not scope.in_scope(row["url_key"]):
        return f"地址 {row['url_key']} 已超出站点清单"
    return None


def apply_pending(store, as_of: str | None = None) -> dict:
    """把到期的 pending 变更推进到 active / failed。幂等：无 pending 时原样返回。"""
    moment = as_of or manifest.now_utc()
    due = store.list_pending_due(moment)
    if not due:
        return {"as_of": moment, "processed": 0, "activated": [], "failed": [],
                "version": ledger.responsibility_version(store.list_active_changes())}

    activated: list[dict] = []
    failed: list[dict] = []
    with store.transaction():
        for row in due:
            reason = _validate_for_apply(row)
            if reason:
                store.update_change_status(row["change_id"], "failed",
                                           manifest.now_utc(), failure_reason=reason)
                failed.append({"change_id": row["change_id"], "reason": reason})
                continue
            store.update_change_status(row["change_id"], "active", manifest.now_utc(),
                                       applied_at=moment)
            activated.append({"change_id": row["change_id"], "url_key": row["url_key"],
                              "to_site": row["to_site"], "effective_at": row["effective_at"]})
    return {"as_of": moment, "processed": len(due), "activated": activated,
            "failed": failed,
            "version": ledger.responsibility_version(store.list_active_changes())}


def resolve_url(store, url: str, as_of: str | None = None) -> dict:
    """时点归属试算：给页面/接口看某个地址在某时点归谁，不写任何数据。"""
    url_key = _normalize_key(url)
    moment = _parse_stamp(as_of, "as_of") if as_of else manifest.now_utc()
    resp = ledger.resolve(store.list_changes(), url_key, moment)
    return {"url_key": url_key, "as_of": moment, **resp.as_dict(),
            "basis_label": ledger.BASIS_LABELS.get(resp.basis_type, resp.basis_type)}


def list_changes(store, status: str | None = None, url_key: str | None = None) -> dict:
    if status and status not in ("pending", "active", "failed"):
        raise errors.BadRequest("status 只支持 pending/active/failed",
                                code="E_BAD_RESP_STATUS")
    rows = store.list_changes(status=status, url_key=url_key)
    summary = {"total": len(rows),
               "by_status": {s: sum(1 for r in rows if r["status"] == s)
                             for s in ("pending", "active", "failed")}}
    return {"rows": rows, "summary": summary,
            "version": ledger.responsibility_version(store.list_active_changes())}


def get_change(store, change_id: str) -> dict:
    row = store.get_change(change_id)
    if row is None:
        raise errors.NotFound(f"责任变更 {change_id} 不存在", code="E_NO_RESP_CHANGE")
    return row


def snapshot_for_run(store, url_keys: list[str], as_of: str) -> dict:
    """巡检批次生成时调用：按批次开始时间解析一次，责任快照冻结进 run payload。

    返回 (version, 每个 key 的责任)。历史批次不再重算，事后改责任账不影响旧账。
    """
    active = store.list_active_changes()
    all_changes = store.list_changes()
    resolved = ledger.resolve_many(all_changes, url_keys, as_of)
    return {"as_of": as_of,
            "version": ledger.responsibility_version(active),
            "responsibilities": {key: resp.as_dict() for key, resp in resolved.items()}}
