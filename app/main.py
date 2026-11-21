"""FastAPI 入口：OCI 镜像清理预演网页。"""

from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.datastructures import UploadFile

from . import db
from .oci import InventoryError, parse_inventory, plan_deletion, validate_download

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = os.environ.get("OCI_GC_DB", str(BASE_DIR.parent / "data" / "ocigc.db"))


def humansize(n: int) -> str:
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def short_digest(digest: str) -> str:
    algo, _, hexd = digest.partition(":")
    return f"{algo}:{hexd[:12]}…" if hexd else digest


def create_app(db_path: str | None = None) -> FastAPI:
    path = db_path or DEFAULT_DB
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db.init_db(path)

    app = FastAPI(title="OCI 镜像清理预演")
    app.state.db_path = path
    templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
    templates.env.filters["humansize"] = humansize
    templates.env.filters["shortdigest"] = short_digest

    def get_con():
        con = db.connect(app.state.db_path)
        try:
            yield con
        finally:
            con.close()

    def wants_json(request: Request) -> bool:
        accept = request.headers.get("accept", "")
        return "application/json" in accept and "text/html" not in accept

    def _error_page(request: Request, status: int, title: str, detail):
        if isinstance(detail, str):
            detail = [detail]
        if wants_json(request):
            return JSONResponse({"error": title, "detail": detail}, status_code=status)
        return templates.TemplateResponse(
            request, "error.html", {"title": title, "errors": detail}, status_code=status
        )

    # ---------- 页面 ----------

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, con=Depends(get_con)):
        inv = db.current_inventory(con)
        repos = db.tags_grouped(con, inv["id"]) if inv else {}
        missing = json.loads(inv["missing_json"]) if inv else []
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "inv": inv,
                "missing": missing,
                "repos": repos,
                "inventories": db.list_inventories(con),
                "rehearsals": db.list_rehearsals(con),
            },
        )

    @app.post("/inventory")
    async def import_inventory(request: Request, con=Depends(get_con)):
        form = await request.form()
        text = ""
        upload = form.get("file")
        if isinstance(upload, UploadFile) and upload.filename:
            text = (await upload.read()).decode("utf-8", errors="replace")
        elif str(form.get("payload", "")).strip():
            text = str(form.get("payload"))
        if not text:
            return _error_page(request, 400, "未提供盘点内容", "请上传 JSON 文件或粘贴盘点内容。")
        try:
            parsed = parse_inventory(text)
        except InventoryError as exc:
            return _error_page(request, 400, "盘点校验失败", exc.errors)
        with con:
            inv_id = db.insert_inventory(con, parsed)
        return RedirectResponse(url=f"/?imported={inv_id}", status_code=303)

    @app.post("/rehearsal")
    async def create_rehearsal(request: Request, con=Depends(get_con)):
        inv = db.current_inventory(con)
        if inv is None:
            return _error_page(request, 400, "尚未导入盘点", "请先导入全仓盘点 JSON。")
        form = await request.form()
        selected = form.getlist("selected")
        pairs: set[tuple[str, str]] = set()
        bad = []
        for item in selected:
            repo, sep, tag = str(item).partition("=")
            if not sep or not repo or not tag:
                bad.append(item)
            else:
                pairs.add((repo, tag))
        if bad:
            return _error_page(request, 400, "标签选择无效", [f"无法解析: {b}" for b in bad])
        if not pairs:
            return _error_page(request, 400, "未选择标签", "请至少勾选一个待退役标签。")

        graph = db.load_graph(con, inv["id"])
        try:
            deletes, total = plan_deletion(graph, pairs)
        except InventoryError as exc:
            return _error_page(request, 400, "预演失败", exc.errors)

        selected_tags = [
            {"repo": r, "tag": t, "digest": graph.tags[(r, t)]} for r, t in sorted(pairs)
        ]
        with con:
            rid = db.create_rehearsal(con, inv["id"], selected_tags, deletes, total)
        return RedirectResponse(url=f"/rehearsal/{rid}", status_code=303)

    @app.get("/rehearsal/{rid}", response_class=HTMLResponse)
    def rehearsal_detail(rid: int, request: Request, con=Depends(get_con)):
        view = db.get_rehearsal(con, rid)
        if view is None:
            return _error_page(request, 404, "预演不存在", f"没有找到预演 #{rid}。")
        head = db.get_rehearsal_head(con, rid)
        current = db.current_inventory(con)
        total = sum(d["size"] for d in view.deletes)
        return templates.TemplateResponse(
            request,
            "rehearsal.html",
            {
                "r": view,
                "head": head,
                "current": current,
                "total": total,
                "current_missing": json.loads(current["missing_json"]) if current else [],
            },
        )

    @app.get("/rehearsal/{rid}/manifest.json")
    def download_manifest(rid: int, request: Request, con=Depends(get_con)):
        view = db.get_rehearsal(con, rid)
        if view is None:
            return _error_page(request, 404, "预演不存在", f"没有找到预演 #{rid}。")
        current = db.current_inventory(con)
        if current is None:
            return _error_page(request, 409, "无可用盘点", "尚未导入任何盘点，无法复核。")

        # 盘点不完整 -> 暂停发单，直到导入完整盘点
        if not current["complete"]:
            missing = json.loads(current["missing_json"])
            body = {
                "error": "inventory_incomplete",
                "message": f"最新盘点 #{current['id']} 不完整：{len(missing)} 个被引用对象缺失，"
                "清理清单暂停发放，请导入完整盘点后重试。",
                "missing": missing,
            }
            if wants_json(request):
                return JSONResponse(body, status_code=409)
            return templates.TemplateResponse(
                request, "blocked.html", {"title": "暂停发单", "message": body["message"], "missing": missing, "rid": rid}, status_code=409
            )

        graph = db.load_graph(con, current["id"])
        conflicts = validate_download(view, graph)
        if conflicts:
            body = {
                "error": "conflict",
                "message": f"最新盘点 #{current['id']} 与预演 #{rid} 存在 {len(conflicts)} 处冲突，整份拒发。",
                "conflicts": conflicts,
            }
            if wants_json(request):
                return JSONResponse(body, status_code=409)
            return templates.TemplateResponse(
                request, "conflict.html", {"message": body["message"], "conflicts": conflicts, "rid": rid}, status_code=409
            )

        manifest = {
            "rehearsal_id": view.rehearsal_id,
            "generated_at": db.utcnow(),
            "validated_against_inventory": current["id"],
            "tags": [
                {"repo": t["repo"], "tag": t["tag"], "digest": t["digest"]} for t in view.tags
            ],
            "delete": [d["digest"] for d in view.deletes],
            "total_bytes": sum(d["size"] for d in view.deletes),
        }
        return Response(
            content=json.dumps(manifest, ensure_ascii=False, indent=2),
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="cleanup-rehearsal-{rid}.json"'
            },
        )

    return app


app = create_app()
