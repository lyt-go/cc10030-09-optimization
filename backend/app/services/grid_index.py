"""网格索引领域服务：按空间层级划分网格单元，生成geohash索引并统计落点数量。

纠偏数据接入后的重建与发布稳定性增强：
- 按区域维护网格索引版本与状态流转（待重建→待复核→正式可读/已失效），
  切换生效时旧版标记为「已归档」并完整保留，可一键回滚恢复；
- 待复核分区不允许发布为正式可读；同一区域同一点位禁止新旧坐标混算，
  每个网格都注明来源规则（规则编号/版本/源/目标坐标系）；
- 利用坐标标准化的逐点变更清单核对点的移动，生成旧格→新格差额，
  供下游对已交接粗格的计数做撤销或补入；
- 切换中途可查询有效时间区间，以及原粗格按采用坐标回算所需的观测。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from app.services.base import BaseModuleService
from app.services.coord_transform import (
    REVIEW_CONFIRMED,
    REVIEW_PENDING,
    REVIEW_REJECTED,
)

# 区域网格版本状态机
STATE_DRAFT = "待重建"
STATE_REVIEW = "待复核"
STATE_ACTIVE = "正式可读"
STATE_ARCHIVED = "已归档"
STATE_FAILED = "重建失败"

GRID_TRANSITIONS: dict[tuple[str, str], str] = {
    (STATE_DRAFT, "提交复核"): STATE_REVIEW,
    (STATE_REVIEW, "确认发布"): STATE_ACTIVE,
    (STATE_REVIEW, "驳回重建"): STATE_DRAFT,
    (STATE_ACTIVE, "归档旧版"): STATE_ARCHIVED,
}


class GridIndexService(BaseModuleService):
    MODULE = "grid_index"
    ENTITY = "网格单元"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["网格编号", "网格层级", "边界范围", "中心坐标", "落点数量", "索引精度", "生成方式", "索引版本"]
    REQUIRED_FIELDS = ["网格编号", "网格层级", "边界范围"]
    STATUS_ORDER = ["草稿", "待生成", "已生成", "已失效"]
    ACTION_RULES = {"启动生成": "待生成", "确认生成": "已生成", "标记失效": "已失效"}
    NEGATIVE_ACTIONS = []

    # ------------------------------------------------------------------
    # 路径与版本目录
    # ------------------------------------------------------------------

    def _region_dir(self, batch_key: str, region: str) -> Path:
        safe = region.replace("/", "_").replace(" ", "_")
        return self._data_dir("grid_versions") / batch_key / safe

    def _versions_index_path(self, batch_key: str, region: str) -> Path:
        return self._region_dir(batch_key, region) / "versions.json"

    def _version_path(self, batch_key: str, region: str, version: str) -> Path:
        return self._region_dir(batch_key, region) / f"{version}.jsonl"

    def _load_versions(self, batch_key: str, region: str) -> dict[str, Any]:
        data = self._read_json(self._versions_index_path(batch_key, region))
        if data is None:
            return {"batch_key": batch_key, "region": region, "versions": [],
                    "active_version": None, "history": []}
        return data

    def _save_versions(self, index: dict[str, Any]) -> Path:
        return self._write_json(
            self._versions_index_path(index["batch_key"], index["region"]), index)

    def _find_version(self, index: dict[str, Any], version: str) -> dict[str, Any] | None:
        for ver in index["versions"]:
            if ver["version"] == version:
                return ver
        return None

    @staticmethod
    def _grid_records(batch_key: str, region: str, points: list[dict[str, Any]],
                      precision: int, rule_tag: dict[str, Any], version: str) -> list[dict[str, Any]]:
        """把一组点按geohash聚合为带来源规则标注的网格记录。"""
        grid_map: dict[str, dict[str, Any]] = {}
        for p in points:
            lat = float(p["trans_lat"])
            lon = float(p["trans_lon"])
            gh = BaseModuleService._geohash_encode(lat, lon, precision)
            bucket = grid_map.setdefault(gh, {
                "point_ids": [], "lats": [], "lons": [], "rule_versions": set(),
                "point_rules": {},
            })
            pid = p["point_id"]
            bucket["point_ids"].append(pid)
            bucket["lats"].append(lat)
            bucket["lons"].append(lon)
            rv = p.get("rule_version", rule_tag.get("rule_version", "v1.0"))
            bucket["rule_versions"].add(rv)
            bucket["point_rules"][str(pid)] = {
                "rule_id": p.get("rule_id", rule_tag.get("rule_id")),
                "rule_version": rv,
                "source_crs": p.get("source_crs", rule_tag.get("source_crs")),
                "target_crs": p.get("target_crs", rule_tag.get("target_crs")),
                "review_state": p.get("review_state", REVIEW_CONFIRMED),
            }

        records = []
        for grid_id, (gh, info) in enumerate(sorted(grid_map.items()), start=1):
            n = len(info["point_ids"])
            records.append({
                "grid_id": grid_id,
                "geohash": gh,
                "batch_key": batch_key,
                "region": region,
                "version": version,
                "level": f"L{precision}",
                "precision": precision,
                "center_lat": round(sum(info["lats"]) / n, 6),
                "center_lon": round(sum(info["lons"]) / n, 6),
                "point_count": n,
                "point_ids": info["point_ids"],
                "source_rule": {
                    "rule_id": rule_tag.get("rule_id"),
                    "rule_versions": sorted(info["rule_versions"]),
                    "source_crs": rule_tag.get("source_crs"),
                    "target_crs": rule_tag.get("target_crs"),
                },
                "point_rules": info["point_rules"],
                "generated_at": time.time(),
            })
        return records

    @staticmethod
    def _fine_grid_moves(points: list[dict[str, Any]], fine_precision: int) -> dict[str, Any]:
        """精细格对照：按原始坐标与采用坐标分别编码，标出转换后发生变化的精细格。"""
        moves = []
        old_cells: dict[str, int] = {}
        new_cells: dict[str, int] = {}
        for p in points:
            old_gh = BaseModuleService._geohash_encode(
                float(p["orig_lat"]), float(p["orig_lon"]), fine_precision)
            new_gh = BaseModuleService._geohash_encode(
                float(p["trans_lat"]), float(p["trans_lon"]), fine_precision)
            old_cells[old_gh] = old_cells.get(old_gh, 0) + 1
            new_cells[new_gh] = new_cells.get(new_gh, 0) + 1
            if old_gh != new_gh:
                moves.append({"point_id": p["point_id"], "old_fine_grid": old_gh,
                              "new_fine_grid": new_gh})
        return {
            "fine_precision": fine_precision,
            "old_fine_grid_count": len(old_cells),
            "new_fine_grid_count": len(new_cells),
            "fine_moved_points": len(moves),
            "fine_grid_moves": moves,
        }

    @staticmethod
    def _diff_grids(old_records: list[dict[str, Any]],
                    new_records: list[dict[str, Any]]) -> dict[str, Any]:
        """旧格→新格差额：按点的实际移动计算每个格的撤销/补入计数。"""
        old_by_gh = {g["geohash"]: g for g in old_records}
        new_by_gh = {g["geohash"]: g for g in new_records}
        old_points = {pid: g["geohash"] for g in old_records for pid in g["point_ids"]}
        new_points = {pid: g["geohash"] for g in new_records for pid in g["point_ids"]}

        old_count = {gh: g["point_count"] for gh, g in old_by_gh.items()}
        new_count = {gh: g["point_count"] for gh, g in new_by_gh.items()}
        delta: dict[str, dict[str, Any]] = {}
        for gh in sorted(set(old_count) | set(new_count)):
            delta[gh] = {
                "old_count": old_count.get(gh, 0),
                "new_count": new_count.get(gh, 0),
                "delta": new_count.get(gh, 0) - old_count.get(gh, 0),
            }

        point_moves = []
        for pid in sorted(set(old_points) | set(new_points), key=lambda x: str(x)):
            before, after = old_points.get(pid), new_points.get(pid)
            if before != after:
                point_moves.append({"point_id": pid, "old_grid": before, "new_grid": after})

        moved = len(point_moves)
        withdraw = {gh: 0 for gh in old_by_gh}
        add_in = {gh: 0 for gh in new_by_gh}
        for mv in point_moves:
            if mv["old_grid"] is not None:
                withdraw[mv["old_grid"]] = withdraw.get(mv["old_grid"], 0) + 1
            if mv["new_grid"] is not None:
                add_in[mv["new_grid"]] = add_in.get(mv["new_grid"], 0) + 1

        adjustments = []
        for gh in sorted(set(withdraw) | set(add_in)):
            w, a = withdraw.get(gh, 0), add_in.get(gh, 0)
            if w or a:
                adjustments.append({
                    "geohash": gh,
                    "withdraw": w,       # 从旧粗格撤销的观测数
                    "add_in": a,         # 向新格补入的实际观测数
                    "net": a - w,
                })
        return {
            "old_grid_count": len(old_records),
            "new_grid_count": len(new_records),
            "moved_points": moved,
            "grid_deltas": delta,
            "point_moves": point_moves,
            "downstream_adjustments": adjustments,
        }

    # ------------------------------------------------------------------
    # 基于变更清单的区域版本化重建
    # ------------------------------------------------------------------

    def rebuild_region(
        self,
        batch_key: str,
        region: str,
        precision: int = 6,
        fine_precision: int | None = None,
        rule_id: int | None = None,
    ) -> dict[str, Any]:
        """依据坐标变更清单重建一个区域的网格索引，生成待复核的新版本。

        - 只用清单中「已确认」的观测建新版；待复核/已驳回观测一律排除，
          因此待复核分区不可能变为正式可读；
        - 校验同一区域内每个观测的来源规则唯一，禁止新旧坐标混算同一个点；
        - 与当前正式版逐点核对移动，产出旧格→新格差额供下游撤销/补入。
        """
        coord_svc = self._safe_import(
            "app.services.coord_transform", "CoordTransformService")()
        manifest = coord_svc.get_change_manifest(batch_key)

        region_points = [c for c in manifest["changes"] if c.get("region") == region]
        if not region_points:
            raise FileNotFoundError(f"批次 {batch_key} 区域 {region} 没有可建索引的观测")

        confirmed = [c for c in region_points if c["review_state"] == REVIEW_CONFIRMED]
        pending = [c for c in region_points if c["review_state"] == REVIEW_PENDING]
        rejected = [c for c in region_points if c["review_state"] == REVIEW_REJECTED]
        if not confirmed:
            raise ValueError(
                f"区域 {region} 暂无已确认新坐标的观测（待复核 {len(pending)} 个），不能重建")

        # 可选：只采用指定规则的观测；缺省采用区域内全部已确认观测
        if rule_id is not None:
            scoped = [c for c in confirmed if c.get("rule_id") == rule_id]
            if not scoped:
                raise ValueError(f"区域 {region} 不存在采用规则 {rule_id} 的已确认观测")
            confirmed = scoped

        # 混算拦截：同一区域同一次建版只允许一套来源规则
        rule_keys = {(c.get("rule_id"), c.get("rule_version"),
                      c.get("source_crs"), c.get("target_crs")) for c in confirmed}
        if len(rule_keys) > 1:
            raise ValueError(
                f"区域 {region} 存在多套来源规则 {sorted(map(str, rule_keys))}，"
                "同一区域禁止用新旧坐标混算同一个点")
        rid, rver, src_crs, tgt_crs = next(iter(rule_keys))
        rule_tag = {"rule_id": rid, "rule_version": rver,
                    "source_crs": src_crs, "target_crs": tgt_crs}

        index = self._load_versions(batch_key, region)
        seq = len(index["versions"]) + 1
        version = f"v{seq}.0"
        while self._find_version(index, version) is not None:
            seq += 1
            version = f"v{seq}.0"

        records = self._grid_records(batch_key, region, confirmed, precision, rule_tag, version)
        self._dump_jsonl(self._version_path(batch_key, region, version), records)

        # 精细格对照：转换更新后选中的精细格可能变化，逐点给出旧精细格→新精细格
        fine_precision = fine_precision or precision
        fine_moves = self._fine_grid_moves(confirmed, fine_precision)

        # 与现行正式版比对，生成旧格→新格差额（旧版可恢复，始终可回算）
        active = self._find_version(index, index["active_version"]) \
            if index["active_version"] else None
        diff = {"old_grid_count": 0, "new_grid_count": len(records), "moved_points": 0,
                "grid_deltas": {}, "point_moves": [], "downstream_adjustments": []}
        if active:
            old_records = self._read_jsonl(
                self._version_path(batch_key, region, active["version"]))
            diff = self._diff_grids(old_records, records)

        meta = {
            "version": version,
            "state": STATE_REVIEW,
            "region": region,
            "precision": precision,
            "fine_precision": fine_precision,
            "rule": rule_tag,
            "total_points": len(confirmed),
            "excluded_pending": len(pending),
            "excluded_rejected": len(rejected),
            "grid_count": len(records),
            "grid_file": str(self._version_path(batch_key, region, version)),
            "diff_from_active": diff,
            "fine_grid_moves": fine_moves,
            "manifest_checksum": manifest["trans_range"]["checksum"],
            "created_at": time.time(),
            "activated_at": None,
            "valid_from": None,
            "valid_to": None,
        }
        index["versions"].append(meta)
        index["history"].append({
            "version": version, "action": "提交复核", "at": time.time(),
            "detail": f"已确认 {len(confirmed)} 点，排除待复核 {len(pending)} / 已驳回 {len(rejected)}",
        })
        self._save_versions(index)
        return {"batch_key": batch_key, "region": region, "version": version,
                "state": STATE_REVIEW, **meta, "diff": diff}

    # ------------------------------------------------------------------
    # 状态流转与发布
    # ------------------------------------------------------------------

    def _transition(self, batch_key: str, region: str, version: str, action: str) -> dict[str, Any]:
        index = self._load_versions(batch_key, region)
        meta = self._find_version(index, version)
        if meta is None:
            raise FileNotFoundError(f"区域 {region} 版本 {version} 不存在")
        target = GRID_TRANSITIONS.get((meta["state"], action))
        if target is None:
            raise ValueError(f"版本 {version} 当前状态「{meta['state']}」不允许执行「{action}」")
        meta["state"] = target
        return index, meta, target

    def submit_region_review(self, batch_key: str, region: str, version: str) -> dict[str, Any]:
        index, meta, target = self._transition(batch_key, region, version, "提交复核")
        index["history"].append({"version": version, "action": "提交复核", "at": time.time()})
        self._save_versions(index)
        return meta

    def publish_region(self, batch_key: str, region: str, version: str) -> dict[str, Any]:
        """发布区域新版：待复核→正式可读，同时把旧正式版归档但完整保留可恢复。"""
        index, meta, target = self._transition(batch_key, region, version, "确认发布")

        # 发布前最后一道闸门：存在待复核观测的分区禁止正式可读
        if meta["excluded_pending"] > 0:
            raise ValueError(
                f"区域 {region} 仍有 {meta['excluded_pending']} 个待复核点，"
                "待复核分区不能变更为正式可读")

        previous = index["active_version"]
        now = time.time()
        if previous and previous != version:
            old_meta = self._find_version(index, previous)
            if old_meta and old_meta["state"] == STATE_ACTIVE:
                old_meta["state"] = STATE_ARCHIVED
                old_meta["valid_to"] = now
                index["history"].append({
                    "version": previous, "action": "归档旧版", "at": now,
                    "detail": f"被 {version} 替换，文件保留可回滚",
                })
        meta["state"] = STATE_ACTIVE
        meta["activated_at"] = now
        meta["valid_from"] = now
        meta["valid_to"] = None
        index["active_version"] = version
        index["history"].append({
            "version": version, "action": "确认发布", "at": now,
            "detail": f"旧版 {previous} 已归档保留",
        })
        self._save_versions(index)
        return {"region": region, "active_version": version, "archived_version": previous, **meta}

    def reject_region_rebuild(self, batch_key: str, region: str, version: str) -> dict[str, Any]:
        index, meta, target = self._transition(batch_key, region, version, "驳回重建")
        index["history"].append({"version": version, "action": "驳回重建", "at": time.time()})
        self._save_versions(index)
        return meta

    def rollback_region(self, batch_key: str, region: str,
                        version: str | None = None) -> dict[str, Any]:
        """回滚到已归档的旧版并恢复为正式可读；当前正式版转为已归档保留。"""
        index = self._load_versions(batch_key, region)
        target_version = version
        if target_version is None:
            archived = [v for v in index["versions"] if v["state"] == STATE_ARCHIVED]
            if not archived:
                raise ValueError(f"区域 {region} 没有可恢复的归档旧版")
            target_version = archived[-1]["version"]
        meta = self._find_version(index, target_version)
        if meta is None:
            raise FileNotFoundError(f"版本 {target_version} 不存在")
        if meta["state"] not in (STATE_ARCHIVED, STATE_ACTIVE):
            raise ValueError(f"版本 {target_version} 状态为「{meta['state']}」，不可恢复")
        if not self._version_path(batch_key, region, target_version).exists():
            raise FileNotFoundError(f"版本 {target_version} 的索引文件已丢失，无法恢复")

        now = time.time()
        current = index["active_version"]
        if current and current != target_version:
            cur_meta = self._find_version(index, current)
            if cur_meta and cur_meta["state"] == STATE_ACTIVE:
                cur_meta["state"] = STATE_ARCHIVED
                cur_meta["valid_to"] = now
        meta["state"] = STATE_ACTIVE
        meta["activated_at"] = now
        meta["valid_from"] = now
        meta["valid_to"] = None
        index["active_version"] = target_version
        index["history"].append({
            "version": target_version, "action": "回滚恢复", "at": now,
            "detail": f"由 {current} 回滚至 {target_version}",
        })
        self._save_versions(index)
        return {"region": region, "active_version": target_version, **meta}

    # ------------------------------------------------------------------
    # 查询：版本目录、中途查询、粗格回算
    # ------------------------------------------------------------------

    def get_region_versions(self, batch_key: str, region: str) -> dict[str, Any]:
        return self._load_versions(batch_key, region)

    def get_region_grids(self, batch_key: str, region: str,
                         version: str | None = None) -> list[dict[str, Any]]:
        """读取指定版本网格；缺省读取当前正式可读版本。"""
        index = self._load_versions(batch_key, region)
        version = version or index["active_version"]
        if not version:
            raise FileNotFoundError(f"区域 {region} 还没有正式可读的网格版本")
        meta = self._find_version(index, version)
        if meta is None:
            raise FileNotFoundError(f"区域 {region} 版本 {version} 不存在")
        return self._read_jsonl(self._version_path(batch_key, region, version))

    def query_during_switch(self, batch_key: str, regions: list[str]) -> dict[str, Any]:
        """切换中途查询：逐区域返回可用版本、可用于后续汇总的时间区间和状态。"""
        result = {"batch_key": batch_key, "queried_at": time.time(), "regions": []}
        for region in regions:
            index = self._load_versions(batch_key, region)
            active = self._find_version(index, index["active_version"]) \
                if index["active_version"] else None
            review = [v["version"] for v in index["versions"] if v["state"] == STATE_REVIEW]
            result["regions"].append({
                "region": region,
                "active_version": index["active_version"],
                "active_state": active["state"] if active else None,
                "valid_from": active["valid_from"] if active else None,
                "valid_to": active["valid_to"] if active else None,
                "summarizable": bool(active and active["state"] == STATE_ACTIVE),
                "pending_versions": review,
                "archived_versions": [v["version"] for v in index["versions"]
                                      if v["state"] == STATE_ARCHIVED],
            })
        return result

    def get_coarse_grid_observations(
        self,
        batch_key: str,
        region: str,
        geohash: str,
        version: str | None = None,
    ) -> dict[str, Any]:
        """返回原粗格按「采用的坐标」回算所需的全部观测（含已交接但移动走的点）。"""
        index = self._load_versions(batch_key, region)
        version = version or index["active_version"]
        if not version:
            raise FileNotFoundError(f"区域 {region} 无可用网格版本")
        grids = self._read_jsonl(self._version_path(batch_key, region, version))
        target = next((g for g in grids if g["geohash"] == geohash), None)
        if target is None:
            raise FileNotFoundError(f"版本 {version} 中不存在粗格 {geohash}")

        coord_svc = self._safe_import(
            "app.services.coord_transform", "CoordTransformService")()
        manifest = coord_svc.get_change_manifest(batch_key)
        by_id = {str(c["point_id"]): c for c in manifest["changes"]
                 if c.get("region") == region}

        observations = []
        for pid in target["point_ids"]:
            change = by_id.get(str(pid))
            if change is None:
                continue
            observations.append({
                "point_id": pid,
                "applied_lat": change["trans_lat"],
                "applied_lon": change["trans_lon"],
                "orig_lat": change["orig_lat"],
                "orig_lon": change["orig_lon"],
                "rule_version": change["rule_version"],
                "review_state": change["review_state"],
                "changed": change["changed"],
            })
        return {
            "batch_key": batch_key, "region": region, "version": version,
            "geohash": geohash, "point_count": len(observations),
            "observations": observations,
        }

    def get_region_diff(self, batch_key: str, region: str,
                        version: str | None = None) -> dict[str, Any]:
        """读取某版本相对当前正式版的旧格→新格差额。"""
        index = self._load_versions(batch_key, region)
        version = version or (index["versions"][-1]["version"] if index["versions"] else None)
        meta = self._find_version(index, version) if version else None
        if meta is None:
            raise FileNotFoundError(f"区域 {region} 没有可比对的版本")
        return {"batch_key": batch_key, "region": region, "version": version,
                **meta["diff_from_active"]}

    # ------------------------------------------------------------------
    # 兼容既有单批次接口（下游 heatmap / spatial_join / density / region 使用）
    # ------------------------------------------------------------------

    def index_points_from_batch(self, batch_key: str, precision: int = 6) -> dict[str, Any]:
        """读取批次转换后的定位点，生成geohash网格索引（保留原单批次行为）。"""
        points_file = self._data_dir("coord_transformed") / f"{batch_key}_transformed.jsonl"
        if not points_file.exists():
            points_file = self._data_dir("geo_points") / f"{batch_key}.jsonl"
        if not points_file.exists():
            raise FileNotFoundError(f"批次 {batch_key} 的定位点数据不存在")

        points = self._read_jsonl(points_file)
        if not points:
            return {"batch_key": batch_key, "grids": 0, "total_points": 0}

        grid_map: dict[str, dict[str, Any]] = {}
        for p in points:
            lat = p.get("trans_lat", p.get("lat", 0))
            lon = p.get("trans_lon", p.get("lon", 0))
            gh = self._geohash_encode(lat, lon, precision)
            if gh not in grid_map:
                grid_map[gh] = {"geohash": gh, "precision": precision,
                                "level": f"L{precision}", "point_count": 0,
                                "lats": [], "lons": []}
            grid_map[gh]["point_count"] += 1
            grid_map[gh]["lats"].append(lat)
            grid_map[gh]["lons"].append(lon)

        grids = []
        grid_id = 0
        for gh, info in grid_map.items():
            grid_id += 1
            center_lat = sum(info["lats"]) / len(info["lats"])
            center_lon = sum(info["lons"]) / len(info["lons"])
            grids.append({
                "grid_id": grid_id,
                "geohash": gh,
                "batch_key": batch_key,
                "level": info["level"],
                "precision": precision,
                "center_lat": round(center_lat, 6),
                "center_lon": round(center_lon, 6),
                "point_count": info["point_count"],
                "generated_at": time.time(),
            })

        output_file = self._data_dir("grid_index") / f"{batch_key}_grid.jsonl"
        written = self._dump_jsonl(output_file, grids)

        for g in grids:
            entry, _ = self.create_entry({
                "网格编号": f"GRID-{batch_key}-{g['grid_id']:03d}",
                "网格层级": g["level"],
                "边界范围": g["geohash"],
                "中心坐标": f"{g['center_lat']},{g['center_lon']}",
                "落点数量": g["point_count"],
                "索引精度": str(precision),
                "生成方式": "geohash",
                "索引版本": "v1.0",
            })
            if entry:
                self.run_action(entry["id"], "确认生成")

        return {"batch_key": batch_key, "total_points": len(points),
                "grids_generated": written, "output_file": str(output_file)}

    def get_grid_index(self, batch_key: str) -> tuple[list[dict[str, Any]], str]:
        """读取指定批次的网格索引数据集文件。"""
        file_path = self._data_dir("grid_index") / f"{batch_key}_grid.jsonl"
        if not file_path.exists():
            raise FileNotFoundError(f"批次 {batch_key} 的网格索引数据集不存在")
        grids = self._read_jsonl(file_path)
        return grids, str(file_path)
