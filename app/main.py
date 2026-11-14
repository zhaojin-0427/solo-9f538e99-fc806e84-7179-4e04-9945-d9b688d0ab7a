"""FastAPI application: daily PM2.5 hourly data publishing service."""
from __future__ import annotations

import re
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Path as PathParam, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from . import config, db
from .validation import CsvValidationError, parse_and_validate


@asynccontextmanager
async def lifespan(_app: FastAPI):
    config.ensure_dirs()
    db.init_db()
    yield


app = FastAPI(
    title="PM2.5 小时数据发布 API",
    description=(
        "按监测点和日期上传/补报每日 24 小时 PM2.5 数据。"
        "整批校验通过才发布；补报携带版本号做乐观并发控制，"
        "成功后整批替换，旧版本原始 CSV 永久留存。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)

STATION_PATTERN = re.compile(r"^[A-Za-z0-9_\-.:]{1,64}$")
MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MiB is far beyond any 24-row CSV


def _validate_station(station_id: str) -> str:
    station_id = station_id.strip()
    if not STATION_PATTERN.match(station_id):
        raise HTTPException(
            status_code=400,
            detail=(
                "station_id must be 1..64 chars of letters, digits or "
                "'_ - . :'"
            ),
        )
    return station_id


@app.exception_handler(CsvValidationError)
async def _csv_error_handler(_request, exc: CsvValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "error": "validation_failed",
            "message": "整批数据未通过校验，没有任何记录被发布",
            "reasons": exc.reasons,
        },
    )


@app.exception_handler(db.VersionConflictError)
async def _conflict_handler(_request, exc: db.VersionConflictError) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "error": "version_conflict",
            "message": "补报版本号与当前版本不一致，已有并发补报先生效",
            "expected_version": exc.expected,
            "current_version": exc.current,
        },
    )


@app.exception_handler(db.PublishError)
async def _publish_error_handler(_request, exc: db.PublishError) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={
            "error": "publish_failed",
            "message": str(exc),
            "detail": "写入失败，上一版本仍完整可查",
        },
    )


@app.post("/stations/{station_id}/dates/{data_date}/uploads", status_code=201)
async def upload_batch(
    station_id: str = PathParam(..., description="监测点标识"),
    data_date: date = PathParam(..., description="数据日期 YYYY-MM-DD"),
    expected_version: int = Form(
        0,
        ge=0,
        description="当前版本号；首次上传传 0（默认），补报必须传当前版本号",
    ),
    file: UploadFile = File(..., description="含 hour,pm25 两列的 CSV"),
):
    """上传或补报某监测点某日的 24 小时数据（整批校验、整批替换）。"""
    station_id = _validate_station(station_id)

    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if not raw:
        raise HTTPException(status_code=400, detail="上传文件为空")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="上传文件超过 5 MiB 限制")

    # Raises CsvValidationError -> 422 with per-row / per-hour reasons.
    batch = parse_and_validate(raw)

    result = db.publish_batch(station_id, data_date, expected_version, batch)
    result["replaced"] = expected_version != 0
    return result


@app.get("/stations/{station_id}/dates/{data_date}/records")
def read_current_records(
    station_id: str = PathParam(...),
    data_date: date = PathParam(...),
):
    """查询当前版本的 24 条小时记录及文件标识。"""
    station_id = _validate_station(station_id)
    data = db.get_current_data(station_id, data_date)
    if data is None:
        raise HTTPException(
            status_code=404,
            detail=f"监测点 {station_id} 在 {data_date.isoformat()} 尚无已发布数据",
        )
    return data


@app.get("/stations/{station_id}/dates/{data_date}/versions")
def read_versions(
    station_id: str = PathParam(...),
    data_date: date = PathParam(...),
):
    """列出该监测点该日期的全部已发布版本（含每版文件标识）。"""
    station_id = _validate_station(station_id)
    versions = db.list_versions(station_id, data_date)
    return {
        "station_id": station_id,
        "data_date": data_date.isoformat(),
        "count": len(versions),
        "versions": versions,
    }


@app.get("/stations/{station_id}/dates/{data_date}/versions/{version}/file")
def download_version_file(
    station_id: str = PathParam(...),
    data_date: date = PathParam(...),
    version: int = PathParam(..., ge=1),
):
    """下载指定版本留存的原始 CSV。"""
    station_id = _validate_station(station_id)
    row = db.get_version_file(station_id, data_date, version)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail="指定版本不存在",
        )

    # Defense in depth: resolve and ensure the path stays inside FILES_DIR.
    target = (config.FILES_DIR / row["file_path"]).resolve()
    root = config.FILES_DIR.resolve()
    if root not in target.parents and target != root:
        raise HTTPException(status_code=400, detail="非法文件路径")
    if not target.is_file():
        raise HTTPException(status_code=410, detail="留存文件已丢失")

    return FileResponse(
        target,
        media_type="text/csv",
        filename=target.name,
        headers={"X-File-Id": row["file_id"], "X-Version": str(version)},
    )


@app.get("/health")
def health():
    return {"status": "ok"}
