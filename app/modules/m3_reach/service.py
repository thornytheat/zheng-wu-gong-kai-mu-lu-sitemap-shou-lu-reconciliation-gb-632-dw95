"""M3 编排：对每条 key 探测一次，分类一次。

探测与分类分开是为了让分类可单测：classify 只看 Observation，不发请求。
probe_stage 只负责把 key 变成 Observation 并落缺陷行。
"""
from __future__ import annotations

import time

from ... import manifest, reasons
from . import classify, probe


def probe_stage(settings, fetcher, prepared: dict, responsibility: dict | None = None) -> dict:
    """responsibility: M8 责任快照 {key: {site, basis_type, ref, ...}}，按批次时点冻结。

    传了就给每条观测打责任站；不传（旧调用/单测）时责任站回退来源站，
    保证责任账是叠加能力，不改写既有探测契约。
    """
    started = time.monotonic()
    rows: list[dict] = []
    failures = 0
    resp_index = (responsibility or {}).get("responsibilities") or responsibility or {}
    for slot in prepared["keys"]:
        result = probe.probe(fetcher, slot["url"], settings.max_hops)
        resp = resp_index.get(slot["key"]) or {}
        responsible_site = resp.get("site") or slot["site"]
        obs = classify.Observation(
            key=slot["key"], site=slot["site"], url=slot["url"],
            in_catalog=len(slot["catalog"]), in_sitemap=len(slot["sitemap"]),
            status=result.status, hops=result.hop_count,
            raw_forms=list(slot["raw_forms"]), notes=list(slot["notes"]))
        category, priority = classify.classify(obs)
        if category != "OK":
            failures += 1
        rows.append({
            "key": slot["key"], "site": slot["site"], "url": slot["url"],
            "responsible_site": responsible_site,
            "responsibility_basis": resp.get("basis_type", "host"),
            "responsibility_ref": resp.get("ref", ""),
            "responsibility_change": resp.get("change_id", ""),
            "in_catalog": obs.in_catalog, "in_sitemap": obs.in_sitemap,
            "status": result.status, "final_url": result.final_url,
            "hops": result.hops, "hop_count": result.hop_count,
            "error": result.error, "reason": result.reason,
            "reason_label": reasons.label(result.reason) if result.reason else "",
            "category": category, "category_label": reasons.label(category),
            "priority": priority,
            "changed": obs.changed, "notes": list(slot["notes"]),
            "evidence": classify.evidence(obs),
            "elapsed_ms": result.elapsed_ms,
        })
    elapsed = int((time.monotonic() - started) * 1000)
    stage = manifest.stage("probe", len(prepared["keys"]), len(rows) - failures,
                           failures, "done", elapsed,
                           f"key {len(prepared['keys'])} · 缺陷 {failures}")
    return {"rows": rows, "stage": stage}


def summary(rows: list[dict]) -> dict:
    buckets: dict[str, int] = {}
    for row in rows:
        buckets[row["category"]] = buckets.get(row["category"], 0) + 1
    return buckets
