"""
api.py — FastAPI app for ChronoGraph. Serves the JSON contract + the static SPA.
"""
from __future__ import annotations

import os
from typing import Optional

from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from .datasource import get_source

HERE = os.path.dirname(os.path.abspath(__file__))
VIZ_DIR = os.path.dirname(HERE)
WEB_DIR = os.path.join(VIZ_DIR, "web")


def create_app() -> FastAPI:
    app = FastAPI(title="ChronoGraph — IC Temporal Provenance Visualizer")
    src = get_source()
    app.state.src = src

    @app.get("/api/health")
    def health():
        return {"ok": True, "source": src.kind}

    @app.get("/api/repos")
    def repos():
        return src.repos()

    @app.get("/api/timeline")
    def timeline():
        return src.timeline()

    @app.get("/api/slice")
    def slice_(
        ts: int = Query(..., description="unix timestamp of the playhead"),
        repos: str = Query("or1200", description="comma-separated repo names"),
        projection: str = Query("traceability"),
    ):
        repo_list = [r for r in repos.split(",") if r]
        try:
            return src.slice(ts, repo_list, projection)
        except Exception as e:
            raise HTTPException(500, f"slice failed: {e}")

    @app.get("/api/provenance")
    def provenance(id: str = Query(...)):
        try:
            return src.provenance(id)
        except NotImplementedError as e:
            raise HTTPException(501, str(e))
        except Exception as e:
            raise HTTPException(500, f"provenance failed: {e}")

    @app.get("/api/source")
    def source(kind: str, ref: str, terms: Optional[str] = None):
        term_list = [t for t in (terms or "").split("|") if t]
        return src.source(kind, ref, term_list)

    @app.get("/api/search")
    def search(q: str, repos: Optional[str] = None):
        repo_list = [r for r in (repos or "").split(",") if r] or None
        return src.search(q, repo_list)

    # ---- static SPA ----
    if os.path.isdir(WEB_DIR):
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

        @app.get("/")
        def index():
            return FileResponse(os.path.join(WEB_DIR, "index.html"))

    return app


app = create_app()
