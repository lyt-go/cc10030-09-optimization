"""坐标标准化领域服务：维护坐标系转换规则、偏移参数与越界处理，并对采集批次执行坐标归一化。

纠偏稳定性增强：
- 每次转换同时产出「逐点变更清单」，记录每个原始观测的新旧坐标、采用的规则与复核状态；
- 清单对新旧坐标范围分别给出边界与校验值，转换端与消费端都可独立重算核验；
- 换过坐标的观测默认进入「待复核」，复核确认前网格索引不得将其作为正式可读数据发布。
"""
from __future__ import annotations

import hashlib
import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 复核状态常量，供网格索引等下游模块共享判断。
REVIEW_PENDING = "待复核"
REVIEW_CONFIRMED = "已确认"
REVIEW_REJECTED = "已驳回"


class CoordTransformService(BaseModuleService):
    MODULE = "coord_transform"
    ENTITY = "转换规则"
    OPERATION_TERM = "转换"
    LIST_FIELDS = ["规则编号", "源坐标系", "目标坐标系", "偏移参数", "精度位数", "越界处理", "规则版本", "生效批次"]
    REQUIRED_FIELDS = ["规则编号", "源坐标系", "目标坐标系"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已废弃"]
    ACTION_RULES = {"提交校验": "待校验", "确认生效": "已生效", "标记废弃": "已废弃"}
    NEGATIVE_ACTIONS = []

    # -- 内部工具 --

    def _load_rule(self, rule_id: int) -> dict[str, Any] | None:
        rule = store.find(self.MODULE, rule_id)
        return rule

    @staticmethod
    def _rule_params(rule: dict[str, Any] | None) -> tuple[float, float, int]:
        offset_lat = 0.0
        offset_lon = 0.0
        precision = 6
        if rule:
            try:
                parts = str(rule.get("偏移参数", "0.0,0.0")).split(",")
                offset_lat = float(parts[0]) if len(parts) > 0 else 0.0
                offset_lon = float(parts[1]) if len(parts) > 1 else 0.0
                precision = int(rule.get("精度位数", "6"))
            except (ValueError, IndexError):
                pass
        return offset_lat, offset_lon, precision

    @staticmethod
    def _range_of(points: list[dict[str, Any]], lat_key: str, lon_key: str) -> dict[str, Any]:
        """计算一组点在指定坐标字段上的经纬度边界，并对逐点坐标串求校验值。"""
        if not points:
            return {"min_lat": None, "max_lat": None, "min_lon": None, "max_lon": None,
                    "point_count": 0, "checksum": ""}
        lats = [float(p[lat_key]) for p in points]
        lons = [float(p[lon_key]) for p in points]
        ordered = sorted(points, key=lambda p: str(p.get("point_id")))
        coord_lines = "\n".join(f"{p[lat_key]},{p[lon_key]}" for p in ordered)
        checksum = hashlib.sha256(coord_lines.encode("utf-8")).hexdigest()[:16]
        return {
            "min_lat": round(min(lats), 8),
            "max_lat": round(max(lats), 8),
            "min_lon": round(min(lons), 8),
            "max_lon": round(max(lons), 8),
            "point_count": len(points),
            "checksum": checksum,
        }

    def _manifest_path(self, batch_key: str):
        return self._data_dir("coord_manifests") / f"{batch_key}_manifest.json"

    # -- 转换主流程 --

    def transform_batch(
        self,
        batch_key: str,
        rule_id: int = 1,
        region_map: dict[Any, str] | None = None,
    ) -> dict[str, Any]:
        """读取采集批次定位点文件，解析原始坐标并执行坐标系转换。

        除转换后定位点数据集外，同时生成逐点变更清单：
        每个原始观测记录新旧坐标、采用规则、是否换过坐标及复核状态，
        并分别给出新旧坐标范围与校验值，供转换端和网格索引端独立核验。
        """
        points_file = self._data_dir("geo_points") / f"{batch_key}.jsonl"
        if not points_file.exists():
            raise FileNotFoundError(f"批次 {batch_key} 的定位点数据文件不存在，请先接入采集批次")
        points = self._read_jsonl(points_file)

        rule = self._load_rule(rule_id)
        offset_lat, offset_lon, precision = self._rule_params(rule)
        rule_version = rule.get("规则版本", "v1.0") if rule else "v1.0"
        source_crs = rule.get("源坐标系", "UNKNOWN") if rule else "UNKNOWN"
        target_crs = rule.get("目标坐标系", "UNKNOWN") if rule else "UNKNOWN"

        region_map = region_map or {}
        transformed: list[dict[str, Any]] = []
        changes: list[dict[str, Any]] = []
        changed_count = 0

        for p in points:
            region = p.get("region") or region_map.get(p.get("point_id")) or region_map.get(str(p.get("point_id")))
            new_lat = round(p["lat"] + offset_lat, precision)
            new_lon = round(p["lon"] + offset_lon, precision)
            # 越界处理
            if new_lat > 90 or new_lat < -90:
                new_lat = max(-90, min(90, new_lat))
            if new_lon > 180 or new_lon < -180:
                new_lon = max(-180, min(180, new_lon))

            is_changed = (new_lat != round(p["lat"], precision)) or (new_lon != round(p["lon"], precision))
            if is_changed:
                changed_count += 1
            review_state = REVIEW_PENDING if is_changed else REVIEW_CONFIRMED

            base_rec = {
                "point_id": p["point_id"],
                "batch_key": batch_key,
                "region": region,
                "orig_lat": p["lat"],
                "orig_lon": p["lon"],
                "trans_lat": new_lat,
                "trans_lon": new_lon,
                "timestamp": p.get("timestamp"),
                "terminal": p.get("terminal"),
                "accuracy": p.get("accuracy"),
                "bearing": p.get("bearing"),
                "speed": p.get("speed"),
                "rule_id": rule_id,
                "rule_version": rule_version,
                "source_crs": source_crs,
                "target_crs": target_crs,
                "changed": is_changed,
                "review_state": review_state,
            }
            transformed.append(dict(base_rec))

            change_rec = dict(base_rec)
            if is_changed:
                change_rec["delta_lat"] = round(new_lat - p["lat"], precision)
                change_rec["delta_lon"] = round(new_lon - p["lon"], precision)
                change_rec["move_distance_m"] = round(
                    self._haversine(p["lat"], p["lon"], new_lat, new_lon), 3)
            else:
                change_rec["delta_lat"] = 0.0
                change_rec["delta_lon"] = 0.0
                change_rec["move_distance_m"] = 0.0
            changes.append(change_rec)

        # 转换后数据集（覆盖写，避免重复追加造成同一观测多版本并存）
        output_file = self._data_dir("coord_transformed") / f"{batch_key}_transformed.jsonl"
        written = self._dump_jsonl(output_file, transformed)

        # 新旧范围各自独立计算并附校验值
        orig_range = self._range_of(points, "lat", "lon")
        trans_range = self._range_of(transformed, "trans_lat", "trans_lon")
        pending_count = sum(1 for c in changes if c["review_state"] == REVIEW_PENDING)
        regions = sorted({c["region"] for c in changes if c.get("region")})

        manifest = {
            "batch_key": batch_key,
            "rule_id": rule_id,
            "rule_version": rule_version,
            "source_crs": source_crs,
            "target_crs": target_crs,
            "generated_at": time.time(),
            "total_points": len(points),
            "changed_points": changed_count,
            "unchanged_points": len(points) - changed_count,
            "pending_review_points": pending_count,
            "regions": regions,
            "orig_range": orig_range,
            "trans_range": trans_range,
            "changes": changes,
        }
        self._write_json(self._manifest_path(batch_key), manifest)

        # 更新记录条数
        if rule:
            rule["生效批次"] = batch_key

        return {
            "batch_key": batch_key,
            "input_points": len(points),
            "transformed_points": written,
            "changed_points": changed_count,
            "pending_review_points": pending_count,
            "rule_id": rule_id,
            "rule_version": rule_version,
            "output_file": str(output_file),
            "manifest_file": str(self._manifest_path(batch_key)),
        }

    # -- 变更清单查询与核验 --

    def get_change_manifest(self, batch_key: str) -> dict[str, Any]:
        """读取批次的逐点变更清单；不存在时提示先执行坐标标准化。"""
        manifest = self._read_json(self._manifest_path(batch_key))
        if manifest is None:
            raise FileNotFoundError(f"批次 {batch_key} 的逐点变更清单不存在，请先执行坐标标准化")
        return manifest

    def verify_manifest_ranges(self, batch_key: str) -> dict[str, Any]:
        """独立核验：重新读取原始点和转换结果重算新旧范围与校验值，与清单声明逐项比对。"""
        manifest = self.get_change_manifest(batch_key)
        raw_file = self._data_dir("geo_points") / f"{batch_key}.jsonl"
        trans_file = self._data_dir("coord_transformed") / f"{batch_key}_transformed.jsonl"
        if not raw_file.exists() or not trans_file.exists():
            raise FileNotFoundError(f"批次 {batch_key} 的原始/转换数据集不完整，无法核验")

        raw_points = self._read_jsonl(raw_file)
        trans_points = self._read_jsonl(trans_file)
        computed_orig = self._range_of(raw_points, "lat", "lon")
        computed_trans = self._range_of(trans_points, "trans_lat", "trans_lon")

        def _compare(declared: dict[str, Any], computed: dict[str, Any]) -> dict[str, Any]:
            keys = ("min_lat", "max_lat", "min_lon", "max_lon", "point_count", "checksum")
            mismatches = [k for k in keys if declared.get(k) != computed.get(k)]
            return {"declared": declared, "computed": computed, "match": not mismatches,
                    "mismatched_fields": mismatches}

        orig_check = _compare(manifest["orig_range"], computed_orig)
        trans_check = _compare(manifest["trans_range"], computed_trans)
        return {
            "batch_key": batch_key,
            "ok": orig_check["match"] and trans_check["match"],
            "orig_range": orig_check,
            "trans_range": trans_check,
        }

    def list_pending_review(self, batch_key: str, region: str | None = None) -> list[dict[str, Any]]:
        """列出待复核观测，可按区域过滤。"""
        manifest = self.get_change_manifest(batch_key)
        items = [c for c in manifest["changes"] if c["review_state"] == REVIEW_PENDING]
        if region:
            items = [c for c in items if c.get("region") == region]
        return items

    def _save_manifest(self, manifest: dict[str, Any]) -> None:
        batch_key = manifest["batch_key"]
        pending = sum(1 for c in manifest["changes"] if c["review_state"] == REVIEW_PENDING)
        manifest["pending_review_points"] = pending
        self._write_json(self._manifest_path(batch_key), manifest)
        # 同步刷新转换数据集里的复核状态，保证两个产物口径一致
        trans_file = self._data_dir("coord_transformed") / f"{batch_key}_transformed.jsonl"
        if trans_file.exists():
            state_by_id = {c["point_id"]: c["review_state"] for c in manifest["changes"]}
            records = self._read_jsonl(trans_file)
            for rec in records:
                if rec["point_id"] in state_by_id:
                    rec["review_state"] = state_by_id[rec["point_id"]]
            self._dump_jsonl(trans_file, records)

    def mark_point_review(
        self,
        batch_key: str,
        point_ids: list[Any],
        state: str = REVIEW_CONFIRMED,
    ) -> dict[str, Any]:
        """对指定观测逐条登记复核结论（已确认/待复核/已驳回），返回更新条数。"""
        if state not in (REVIEW_CONFIRMED, REVIEW_PENDING, REVIEW_REJECTED):
            raise ValueError(f"不支持的复核状态：{state}")
        manifest = self.get_change_manifest(batch_key)
        wanted = {pid if not isinstance(pid, str) else pid for pid in point_ids}
        updated = 0
        for change in manifest["changes"]:
            if change["point_id"] in wanted or str(change["point_id"]) in {str(w) for w in wanted}:
                change["review_state"] = state
                updated += 1
        self._save_manifest(manifest)
        return {"batch_key": batch_key, "updated": updated, "state": state,
                "pending_review_points": manifest["pending_review_points"]}

    def confirm_region_points(self, batch_key: str, region: str) -> dict[str, Any]:
        """确认一个区域内全部待复核观测的新坐标（区域级复核放行）。"""
        manifest = self.get_change_manifest(batch_key)
        updated = 0
        for change in manifest["changes"]:
            if change.get("region") == region and change["review_state"] == REVIEW_PENDING:
                change["review_state"] = REVIEW_CONFIRMED
                updated += 1
        self._save_manifest(manifest)
        return {"batch_key": batch_key, "region": region, "confirmed": updated,
                "pending_review_points": manifest["pending_review_points"]}

    # -- 兼容既有调用 --

    def get_transformed_points(self, batch_key: str) -> tuple[list[dict[str, Any]], str]:
        """读取指定批次的坐标转换结果数据集，返回转换后的定位点记录和文件路径。"""
        file_path = self._data_dir("coord_transformed") / f"{batch_key}_transformed.jsonl"
        if not file_path.exists():
            raise FileNotFoundError(f"批次 {batch_key} 的转换数据集文件不存在")
        points = self._read_jsonl(file_path)
        return points, str(file_path)

    def apply_rule_to_all(self, rule_id: int) -> dict[str, Any]:
        """对已接入的全部采集批次应用指定转换规则，批量生成坐标标准化数据集。"""
        rule = store.find(self.MODULE, rule_id)
        if rule is None:
            return {"error": f"转换规则 {rule_id} 不存在"}
        geo_svc = self._safe_import("app.services.geo_source", "GeoSourceService")()
        active_batches = geo_svc.list_active_batches()
        results = {}
        for batch in active_batches:
            batch_key = batch["批次编号"]
            try:
                results[batch_key] = self.transform_batch(batch_key, rule_id)
            except Exception as e:
                results[batch_key] = {"error": str(e)}
        return {"rule_id": rule_id, "batches_processed": len(results), "results": results}
