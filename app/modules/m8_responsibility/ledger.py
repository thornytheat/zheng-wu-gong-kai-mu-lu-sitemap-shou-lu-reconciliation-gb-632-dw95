"""M8 责任账：地址责任变更链的时点解析（纯函数，不发请求、不写库）。

解析规则见 docs/RESPONSIBILITY-LEDGER.md：
- 只看 active 且 effective_at <= as_of 的变更，取生效时点最大的一条；
- pending / failed 不参与，切换没成功就保留旧账；
- 没有命中变更时回退主机归属（scope.site_of），依据类型 host。

同址始终只有一条键，责任账只决定「账记在哪个站」，绝不把同址拆成两笔。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from ..m2_url import scope


@dataclass(frozen=True)
class Responsibility:
    site: str
    basis_type: str   # host = 主机清单；transfer = 移交变更
    ref: str          # host 时为主机名；transfer 时为变更号 rc-xxxx
    change_id: str = ""
    basis: str = ""   # 移交依据原文（公文号/移交单）
    effective_at: str = ""

    def as_dict(self) -> dict:
        return {"site": self.site, "basis_type": self.basis_type, "ref": self.ref,
                "change_id": self.change_id, "basis": self.basis,
                "effective_at": self.effective_at}


def host_responsibility(url_key: str) -> Responsibility | None:
    """无变更命中时的默认归属：主机在站点清单内才有责任站。"""
    site = scope.site_of(url_key)
    if site is None:
        return None
    from ... import config
    host = config.SITE_BY_CODE[site].host
    return Responsibility(site=site, basis_type="host", ref=host)


def resolve(changes: list[dict], url_key: str, as_of: str) -> Responsibility:
    """按 as_of 时点解析地址责任站。changes 为该地址（或全部）的变更行。"""
    fallback = host_responsibility(url_key)
    if fallback is None:
        raise ValueError(f"地址不在站点清单内：{url_key}")
    candidates = [
        row for row in changes
        if row.get("url_key") == url_key and row.get("status") == "active"
        and (row.get("effective_at") or "") <= as_of
    ]
    if not candidates:
        return fallback
    winners = sorted(candidates, key=lambda row: (row["effective_at"], row["change_id"]))
    top = winners[-1]
    return Responsibility(site=top["to_site"], basis_type="transfer",
                          ref=top["change_id"], change_id=top["change_id"],
                          basis=top.get("basis", ""), effective_at=top["effective_at"])


def resolve_many(changes: list[dict], url_keys: list[str], as_of: str) -> dict[str, Responsibility]:
    """批量解析，供巡检阶段一次性给所有 key 打责任快照。"""
    out: dict[str, Responsibility] = {}
    for url_key in url_keys:
        out[url_key] = resolve(changes, url_key, as_of)
    return out


def change_id_of(url_key: str, from_site: str, to_site: str,
                 effective_at: str, basis: str) -> str:
    """变更内容哈希：同内容同号，重放登记不产生第二行。"""
    stem = "|".join([url_key, from_site, to_site, effective_at, basis or ""])
    return "rc-" + hashlib.sha256(stem.encode("utf-8")).hexdigest()[:12]


def responsibility_version(active_changes: list[dict]) -> str:
    """责任版本：active 变更排序后取 sha256；无变更为 none。

    纳入批次键后，责任账一动，同输入重跑就成为新批次、按新责任出账；
    责任账不动时版本不变，重跑仍命中旧批次键，幂等复用。
    """
    if not active_changes:
        return "none"
    lines = [
        "|".join([row["url_key"], row["from_site"], row["to_site"],
                  row["effective_at"], row.get("basis", ""), row["change_id"]])
        for row in sorted(active_changes,
                          key=lambda r: (r["url_key"], r["effective_at"], r["change_id"]))
    ]
    return "rv-" + hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:12]


BASIS_LABELS = {"host": "主机清单", "transfer": "移交变更"}
