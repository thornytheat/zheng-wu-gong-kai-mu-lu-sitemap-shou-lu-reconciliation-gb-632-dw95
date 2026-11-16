"""M8 入口：责任变更登记、到期迁移、变更链查询、时点试算与责任账页面。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ... import config
from ...web import Ctx, get_ctx, render
from . import service

router = APIRouter(tags=["M8 地址责任账"])


@router.post("/api/responsibility/changes")
def api_register_change(payload: dict | None = None,
                        ctx: Ctx = Depends(get_ctx)) -> dict:
    body = payload or {}
    change = service.register_change(
        ctx.store, url=str(body.get("url") or ""),
        to_site=str(body.get("to_site") or ""),
        effective_at=str(body.get("effective_at") or ""),
        basis=str(body.get("basis") or ""),
        from_site=body.get("from_site"))
    return {"change": change, "version": service.list_changes(ctx.store)["version"]}


@router.post("/api/responsibility/apply")
def api_apply_pending(payload: dict | None = None,
                      ctx: Ctx = Depends(get_ctx)) -> dict:
    body = payload or {}
    return service.apply_pending(ctx.store, as_of=body.get("as_of"))


@router.get("/api/responsibility/changes")
def api_list_changes(status: str | None = None, url: str | None = None,
                     ctx: Ctx = Depends(get_ctx)) -> dict:
    return service.list_changes(ctx.store, status=status, url_key=url)


@router.get("/api/responsibility/changes/{change_id}")
def api_get_change(change_id: str, ctx: Ctx = Depends(get_ctx)) -> dict:
    return {"change": service.get_change(ctx.store, change_id)}


@router.get("/api/responsibility/resolve")
def api_resolve(url: str, as_of: str | None = None,
                ctx: Ctx = Depends(get_ctx)) -> dict:
    return service.resolve_url(ctx.store, url, as_of)


@router.get("/responsibility")
def page_responsibility(request: Request, status: str | None = None):
    ctx: Ctx = get_ctx(request)
    data = service.list_changes(ctx.store, status=status)
    return render(request, "responsibility.html", active="responsibility",
                  data=data, rows=data["rows"], version=data["version"],
                  status_filter=status or "", sites=config.SITES)
