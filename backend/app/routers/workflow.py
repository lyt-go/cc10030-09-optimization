"""纠偏重建工作流接口：坐标变更清单核验与网格索引区域版本化重建/发布。

路由只做参数透传，状态流转与业务约束全部在 coord_transform / grid_index 服务内。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.coord_transform import CoordTransformService
from app.services.grid_index import GridIndexService

router = APIRouter(prefix="/api/workflow", tags=["纠偏重建工作流"])


class TransformPayload(BaseModel):
    rule_id: int = 1
    region_map: dict[str, str] = Field(default_factory=dict)


class ReviewPayload(BaseModel):
    point_ids: list[Any] = Field(default_factory=list)
    state: str = "已确认"


class RebuildPayload(BaseModel):
    precision: int = 6
    fine_precision: int | None = None
    rule_id: int | None = None


class SwitchQueryPayload(BaseModel):
    regions: list[str]


class VersionPayload(BaseModel):
    version: str | None = None


def _coord() -> CoordTransformService:
    return CoordTransformService()


def _grid() -> GridIndexService:
    return GridIndexService()


def _guard(exc: Exception) -> HTTPException:
    status = 404 if isinstance(exc, FileNotFoundError) else 409
    return HTTPException(status_code=status, detail=str(exc))


# ---------------- 坐标标准化：逐点变更清单 ----------------

@router.post("/coord/{batch_key}/transform")
def transform(batch_key: str, payload: TransformPayload) -> dict[str, Any]:
    try:
        return _coord().transform_batch(batch_key, rule_id=payload.rule_id,
                                        region_map=payload.region_map)
    except FileNotFoundError as exc:
        raise _guard(exc) from exc


@router.get("/coord/{batch_key}/manifest")
def get_manifest(batch_key: str) -> dict[str, Any]:
    try:
        return _coord().get_change_manifest(batch_key)
    except FileNotFoundError as exc:
        raise _guard(exc) from exc


@router.post("/coord/{batch_key}/verify-ranges")
def verify_ranges(batch_key: str) -> dict[str, Any]:
    try:
        return _coord().verify_manifest_ranges(batch_key)
    except (FileNotFoundError, ValueError) as exc:
        raise _guard(exc) from exc


@router.get("/coord/{batch_key}/pending-review")
def pending_review(batch_key: str, region: str | None = None) -> dict[str, Any]:
    try:
        items = _coord().list_pending_review(batch_key, region)
        return {"batch_key": batch_key, "region": region, "total": len(items), "items": items}
    except FileNotFoundError as exc:
        raise _guard(exc) from exc


@router.post("/coord/{batch_key}/review")
def review_points(batch_key: str, payload: ReviewPayload) -> dict[str, Any]:
    try:
        return _coord().mark_point_review(batch_key, payload.point_ids, payload.state)
    except (FileNotFoundError, ValueError) as exc:
        raise _guard(exc) from exc


@router.post("/coord/{batch_key}/regions/{region}/confirm")
def confirm_region(batch_key: str, region: str) -> dict[str, Any]:
    try:
        return _coord().confirm_region_points(batch_key, region)
    except FileNotFoundError as exc:
        raise _guard(exc) from exc


# ---------------- 网格索引：区域版本化重建与发布 ----------------

@router.post("/grid/{batch_key}/regions/{region}/rebuild")
def rebuild_region(batch_key: str, region: str, payload: RebuildPayload) -> dict[str, Any]:
    try:
        return _grid().rebuild_region(
            batch_key, region, precision=payload.precision,
            fine_precision=payload.fine_precision, rule_id=payload.rule_id)
    except (FileNotFoundError, ValueError) as exc:
        raise _guard(exc) from exc


@router.post("/grid/{batch_key}/regions/{region}/versions/{version}/submit-review")
def submit_review(batch_key: str, region: str, version: str) -> dict[str, Any]:
    try:
        return _grid().submit_region_review(batch_key, region, version)
    except (FileNotFoundError, ValueError) as exc:
        raise _guard(exc) from exc


@router.post("/grid/{batch_key}/regions/{region}/versions/{version}/publish")
def publish(batch_key: str, region: str, version: str) -> dict[str, Any]:
    try:
        return _grid().publish_region(batch_key, region, version)
    except (FileNotFoundError, ValueError) as exc:
        raise _guard(exc) from exc


@router.post("/grid/{batch_key}/regions/{region}/versions/{version}/reject")
def reject(batch_key: str, region: str, version: str) -> dict[str, Any]:
    try:
        return _grid().reject_region_rebuild(batch_key, region, version)
    except (FileNotFoundError, ValueError) as exc:
        raise _guard(exc) from exc


@router.post("/grid/{batch_key}/regions/{region}/rollback")
def rollback(batch_key: str, region: str, payload: VersionPayload) -> dict[str, Any]:
    try:
        return _grid().rollback_region(batch_key, region, payload.version)
    except (FileNotFoundError, ValueError) as exc:
        raise _guard(exc) from exc


@router.get("/grid/{batch_key}/regions/{region}/versions")
def list_versions(batch_key: str, region: str) -> dict[str, Any]:
    return _grid().get_region_versions(batch_key, region)


@router.get("/grid/{batch_key}/regions/{region}/grids")
def region_grids(batch_key: str, region: str, version: str | None = None) -> dict[str, Any]:
    try:
        records = _grid().get_region_grids(batch_key, region, version)
        return {"batch_key": batch_key, "region": region,
                "version": version, "total": len(records), "items": records}
    except FileNotFoundError as exc:
        raise _guard(exc) from exc


@router.get("/grid/{batch_key}/regions/{region}/diff")
def region_diff(batch_key: str, region: str, version: str | None = None) -> dict[str, Any]:
    try:
        return _grid().get_region_diff(batch_key, region, version)
    except FileNotFoundError as exc:
        raise _guard(exc) from exc


@router.get("/grid/{batch_key}/regions/{region}/coarse-grid/{geohash}/observations")
def coarse_observations(batch_key: str, region: str, geohash: str,
                        version: str | None = None) -> dict[str, Any]:
    try:
        return _grid().get_coarse_grid_observations(batch_key, region, geohash, version)
    except FileNotFoundError as exc:
        raise _guard(exc) from exc


@router.post("/grid/{batch_key}/switch-query")
def switch_query(batch_key: str, payload: SwitchQueryPayload) -> dict[str, Any]:
    return _grid().query_during_switch(batch_key, payload.regions)
