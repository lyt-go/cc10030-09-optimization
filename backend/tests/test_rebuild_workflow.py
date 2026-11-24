"""纠偏重建稳定性集成测试。

场景（一次重建跨两个区域）：
- 区域A：更正后的坐标已齐备（全部观测复核确认）；
- 区域B：仍有旧规则下的待复核点（部分观测未确认）；
验证逐点变更清单、新旧范围独立核验、区域状态流转、待复核禁发、
旧版保留可回滚、混算拦截、来源规则标注、旧格→新格差额与切换中途查询。
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def setup_data_dir(monkeypatch):
    tmpdir = Path(tempfile.mkdtemp(prefix="geo_rebuild_"))
    import app.services.base as base_mod
    original_root = base_mod.DATA_ROOT
    base_mod.DATA_ROOT = tmpdir
    for sub in ["geo_points", "coord_transformed", "coord_manifests", "grid_index",
                "grid_versions", "checkpoints"]:
        (tmpdir / sub).mkdir(parents=True, exist_ok=True)
    yield tmpdir
    base_mod.DATA_ROOT = original_root
    shutil.rmtree(tmpdir, ignore_errors=True)


BATCH = "REBUILD-BATCH-01"
REGION_A = "区域A"
REGION_B = "区域B"


def _make_rule(coord, rule_id: int, code: str, offset: str, version: str):
    coord.create_entry({
        "规则编号": code,
        "源坐标系": "WGS84",
        "目标坐标系": "GCJ02",
        "偏移参数": offset,
        "精度位数": "6",
        "越界处理": "clamp",
        "规则版本": version,
        "生效批次": "",
    })
    coord.run_action(rule_id, "确认生效")


def _seed_points(geo, region_counts: dict[str, int]):
    """写入坐标确定的两区域定位点，规则一偏移量为0时所有点都落在已知经纬度上。"""
    points = []
    pid = 0
    base = {"lat": 39.900000, "lon": 116.400000}
    for region, count in region_counts.items():
        for i in range(count):
            pid += 1
            points.append({
                "point_id": pid,
                "batch_key": BATCH,
                "region": region,
                "lat": round(base["lat"] + pid * 0.0001, 6),
                "lon": round(base["lon"] + pid * 0.0001, 6),
                "timestamp": 1_700_000_000 + pid * 60,
                "terminal": f"TERM-{pid:03d}",
            })
    geo._dump_jsonl(geo._data_dir("geo_points") / f"{BATCH}.jsonl", points)
    return points


@pytest.fixture()
def two_region_world():
    from app.services.geo_source import GeoSourceService
    from app.services.coord_transform import CoordTransformService
    from app.services.grid_index import GridIndexService

    geo = GeoSourceService()
    coord = CoordTransformService()
    grid = GridIndexService()

    # 清空种子示例行，保证自建规则的 id 确定（1=旧规则，2=纠偏规则）
    from app.store import store
    store.rows("coord_transform").clear()

    # 规则一：零偏移（旧规则，坐标不变 → 全部自动确认）
    _make_rule(coord, 1, "RULE-OLD", "0.0,0.0", "v1.0")
    # 规则二：纠偏规则，偏移足以让部分点跨越geohash格边界
    _make_rule(coord, 2, "RULE-NEW", "0.002,0.002", "v2.0")

    points = _seed_points(geo, {REGION_A: 6, REGION_B: 6})
    return geo, coord, grid, points


class TestChangeManifest:
    """坐标标准化：逐点变更清单与新旧范围核验。"""

    def test_manifest_records_every_point_change_and_rule(self, two_region_world):
        _, coord, _, points = two_region_world
        result = coord.transform_batch(BATCH, rule_id=2)

        assert result["changed_points"] == 12
        assert result["pending_review_points"] == 12

        manifest = coord.get_change_manifest(BATCH)
        assert manifest["total_points"] == 12
        assert manifest["rule_version"] == "v2.0"
        change_by_id = {c["point_id"]: c for c in manifest["changes"]}

        sample = next(p for p in points if p["point_id"] == 1)
        rec = change_by_id[1]
        # 清单逐点记录原始观测、新旧坐标、采用规则、移动距离与复核状态
        assert rec["orig_lat"] == sample["lat"]
        assert rec["orig_lon"] == sample["lon"]
        assert rec["trans_lat"] == round(sample["lat"] + 0.002, 6)
        assert rec["trans_lon"] == round(sample["lon"] + 0.002, 6)
        assert rec["rule_id"] == 2
        assert rec["rule_version"] == "v2.0"
        assert rec["source_crs"] == "WGS84"
        assert rec["target_crs"] == "GCJ02"
        assert rec["changed"] is True
        assert rec["review_state"] == "待复核"
        assert rec["move_distance_m"] > 0
        assert rec["delta_lat"] == pytest.approx(0.002)

        # 新旧范围都独立给出边界与校验值
        for key in ("orig_range", "trans_range"):
            rng = manifest[key]
            assert all(rng[k] is not None for k in
                       ("min_lat", "max_lat", "min_lon", "max_lon", "checksum"))
            assert rng["point_count"] == 12
        assert manifest["orig_range"]["checksum"] != manifest["trans_range"]["checksum"]

    def test_ranges_can_be_verified_independently(self, two_region_world):
        _, coord, _, _ = two_region_world
        coord.transform_batch(BATCH, rule_id=2)
        report = coord.verify_manifest_ranges(BATCH)
        assert report["ok"] is True
        assert report["orig_range"]["match"] is True
        assert report["trans_range"]["match"] is True

    def test_tampered_manifest_range_is_detected(self, two_region_world):
        _, coord, _, _ = two_region_world
        coord.transform_batch(BATCH, rule_id=2)
        manifest_path = coord._manifest_path(BATCH)
        manifest = coord._read_json(manifest_path)
        manifest["trans_range"]["checksum"] = "tampered-value"
        coord._write_json(manifest_path, manifest)

        report = coord.verify_manifest_ranges(BATCH)
        assert report["ok"] is False
        assert report["trans_range"]["match"] is False
        assert "checksum" in report["trans_range"]["mismatched_fields"]

    def test_zero_offset_rule_confirms_points_immediately(self, two_region_world):
        _, coord, _, _ = two_region_world
        result = coord.transform_batch(BATCH, rule_id=1)
        assert result["changed_points"] == 0
        assert result["pending_review_points"] == 0
        assert all(c["review_state"] == "已确认"
                   for c in coord.get_change_manifest(BATCH)["changes"])

    def test_region_confirm_only_clears_that_region(self, two_region_world):
        _, coord, _, _ = two_region_world
        coord.transform_batch(BATCH, rule_id=2)
        out = coord.confirm_region_points(BATCH, REGION_A)
        assert out["confirmed"] == 6
        assert out["pending_review_points"] == 6

        pending = coord.list_pending_review(BATCH)
        assert len(pending) == 6
        assert all(c["region"] == REGION_B for c in pending)
        assert coord.list_pending_review(BATCH, REGION_A) == []


class TestRegionRebuildAndPublish:
    """网格索引：区域版本化重建、状态流转、待复核禁发与来源标注。"""

    def test_pending_region_cannot_become_readable(self, two_region_world):
        _, coord, grid, _ = two_region_world
        coord.transform_batch(BATCH, rule_id=2)
        coord.confirm_region_points(BATCH, REGION_A)
        # 区域B只确认3个，保留3个待复核
        b_pending = coord.list_pending_review(BATCH, REGION_B)
        coord.mark_point_review(BATCH, [c["point_id"] for c in b_pending[:3]], "已确认")

        rebuild_a = grid.rebuild_region(BATCH, REGION_A)
        rebuild_b = grid.rebuild_region(BATCH, REGION_B)
        assert rebuild_a["state"] == "待复核"
        assert rebuild_b["excluded_pending"] == 3

        # 区域A发布成功
        published = grid.publish_region(BATCH, REGION_A, rebuild_a["version"])
        assert published["state"] == "正式可读"
        assert published["valid_from"] is not None

        # 区域B存在待复核分区，禁止变为正式可读
        with pytest.raises(ValueError, match="待复核"):
            grid.publish_region(BATCH, REGION_B, rebuild_b["version"])

        versions_b = grid.get_region_versions(BATCH, REGION_B)
        assert versions_b["active_version"] is None
        assert versions_b["versions"][-1]["state"] == "待复核"

    def test_illegal_transitions_are_rejected(self, two_region_world):
        _, coord, grid, _ = two_region_world
        coord.transform_batch(BATCH, rule_id=1)
        rebuild = grid.rebuild_region(BATCH, REGION_A)
        # 待复核版本不能直接回滚（未归档）
        with pytest.raises(ValueError):
            grid.rollback_region(BATCH, REGION_A, rebuild["version"])
        # 不存在的动作
        with pytest.raises(ValueError):
            grid._transition(BATCH, REGION_A, rebuild["version"], "确认发布x")

    def test_every_grid_carries_source_rule(self, two_region_world):
        _, coord, grid, _ = two_region_world
        coord.transform_batch(BATCH, rule_id=2)
        coord.confirm_region_points(BATCH, REGION_A)
        rebuild = grid.rebuild_region(BATCH, REGION_A)
        grids = grid.get_region_grids(BATCH, REGION_A, rebuild["version"])
        for g in grids:
            assert g["source_rule"]["rule_id"] == 2
            assert g["source_rule"]["rule_versions"] == ["v2.0"]
            assert g["source_rule"]["source_crs"] == "WGS84"
            assert g["source_rule"]["target_crs"] == "GCJ02"
            for rule_info in g["point_rules"].values():
                assert rule_info["rule_version"] == "v2.0"
                assert rule_info["review_state"] == "已确认"

    def test_mixed_rule_in_same_region_is_blocked(self, two_region_world):
        _, coord, grid, _ = two_region_world
        # 先用旧规则转换并全量确认
        coord.transform_batch(BATCH, rule_id=1)
        # 再用新规则转换：区域A全部待复核，区域B逐个确认一半制造混合
        coord.transform_batch(BATCH, rule_id=2)
        coord.confirm_region_points(BATCH, REGION_A)
        b_pending = coord.list_pending_review(BATCH, REGION_B)
        coord.mark_point_review(BATCH, [c["point_id"] for c in b_pending[:3]], "已确认")

        # 把区域B一个已确认点的规则手工改成旧规则，模拟新旧坐标混算同一区域
        manifest = coord.get_change_manifest(BATCH)
        for change in manifest["changes"]:
            if change["region"] == REGION_B and change["review_state"] == "已确认":
                change["rule_version"] = "v1.0"
                change["rule_id"] = 1
                break
        coord._save_manifest(manifest)

        with pytest.raises(ValueError, match="混算"):
            grid.rebuild_region(BATCH, REGION_B)

    def test_rule_id_filter_avoids_mixing(self, two_region_world):
        _, coord, grid, _ = two_region_world
        coord.transform_batch(BATCH, rule_id=2)
        coord.confirm_region_points(BATCH, REGION_A)
        # 指定不存在的规则id应报错而不是静默混用
        with pytest.raises(ValueError):
            grid.rebuild_region(BATCH, REGION_A, rule_id=999)

    def test_fine_grid_selection_changes_are_listed_per_point(self, two_region_world):
        _, coord, grid, _ = two_region_world
        coord.transform_batch(BATCH, rule_id=2)
        coord.confirm_region_points(BATCH, REGION_A)
        rebuild = grid.rebuild_region(BATCH, REGION_A, precision=6, fine_precision=7)
        fine = rebuild["fine_grid_moves"]
        assert fine["fine_precision"] == 7
        # 每个移动点都能在旧/新精细格之间对上号，且新精细格数量与逐点编码一致
        for mv in fine["fine_grid_moves"]:
            assert mv["old_fine_grid"] != mv["new_fine_grid"]
            assert len(mv["new_fine_grid"]) == 7
        assert fine["fine_moved_points"] == len(fine["fine_grid_moves"])


class TestVersionRetentionDiffAndRollback:
    """旧版保留可恢复、旧格→新格差额、粗格按采用坐标回算。"""

    def _publish_v1(self, coord, grid):
        coord.transform_batch(BATCH, rule_id=1)
        v1_a = grid.rebuild_region(BATCH, REGION_A)
        grid.publish_region(BATCH, REGION_A, v1_a["version"])
        return v1_a

    def test_publish_archives_old_version_and_keeps_files(self, two_region_world):
        _, coord, grid, _ = two_region_world
        v1 = self._publish_v1(coord, grid)

        # 切换到纠偏规则并复核确认后重建发布 v2.0
        coord.transform_batch(BATCH, rule_id=2)
        coord.confirm_region_points(BATCH, REGION_A)
        v2 = grid.rebuild_region(BATCH, REGION_A)
        grid.publish_region(BATCH, REGION_A, v2["version"])

        index = grid.get_region_versions(BATCH, REGION_A)
        assert index["active_version"] == v2["version"]
        old_meta = next(v for v in index["versions"] if v["version"] == v1["version"])
        assert old_meta["state"] == "已归档"
        assert old_meta["valid_to"] is not None
        # 旧版文件仍然保留，可独立读取
        old_grids = grid.get_region_grids(BATCH, REGION_A, v1["version"])
        assert len(old_grids) > 0

    def test_old_to_new_grid_diff_supports_withdraw_and_add_in(self, two_region_world):
        _, coord, grid, _ = two_region_world
        v1 = self._publish_v1(coord, grid)
        old_grids = grid.get_region_grids(BATCH, REGION_A, v1["version"])

        coord.transform_batch(BATCH, rule_id=2)
        coord.confirm_region_points(BATCH, REGION_A)
        v2 = grid.rebuild_region(BATCH, REGION_A)
        diff = grid.get_region_diff(BATCH, REGION_A, v2["version"])

        # 差额基于点的实际移动，而不是只比较格数量
        assert diff["moved_points"] >= 0
        total_withdraw = sum(a["withdraw"] for a in diff["downstream_adjustments"])
        total_add = sum(a["add_in"] for a in diff["downstream_adjustments"])
        assert total_withdraw == total_add == diff["moved_points"]
        for gh, delta in diff["grid_deltas"].items():
            assert delta["new_count"] - delta["old_count"] == delta["delta"]

        # 若确有移动，旧粗格出现在撤销清单中，新格出现在补入清单中
        if diff["moved_points"]:
            moved = diff["point_moves"][0]
            adj = {a["geohash"]: a for a in diff["downstream_adjustments"]}
            assert adj[moved["old_grid"]]["withdraw"] >= 1
            assert adj[moved["new_grid"]]["add_in"] >= 1
        else:  # 偏移未跨格时，格集合与计数应保持一致
            assert {g["geohash"] for g in old_grids} == set(diff["grid_deltas"])

    def test_rollback_restores_archived_version_as_readable(self, two_region_world):
        _, coord, grid, _ = two_region_world
        v1 = self._publish_v1(coord, grid)
        coord.transform_batch(BATCH, rule_id=2)
        coord.confirm_region_points(BATCH, REGION_A)
        v2 = grid.rebuild_region(BATCH, REGION_A)
        grid.publish_region(BATCH, REGION_A, v2["version"])

        rolled = grid.rollback_region(BATCH, REGION_A)  # 默认恢复最近归档版
        assert rolled["version"] == v1["version"]
        assert rolled["state"] == "正式可读"

        index = grid.get_region_versions(BATCH, REGION_A)
        assert index["active_version"] == v1["version"]
        v2_meta = next(v for v in index["versions"] if v["version"] == v2["version"])
        assert v2_meta["state"] == "已归档"
        # 当前正式可读数据回到旧版格集合
        active_grids = grid.get_region_grids(BATCH, REGION_A)
        assert all(g["version"] == v1["version"] for g in active_grids)

    def test_coarse_grid_recomputes_with_applied_coordinates(self, two_region_world):
        _, coord, grid, _ = two_region_world
        v1 = self._publish_v1(coord, grid)
        old_grids = grid.get_region_grids(BATCH, REGION_A, v1["version"])
        coarse = old_grids[0]

        # 用新坐标发布后，原粗格仍需按采用的坐标回算已交接观测
        coord.transform_batch(BATCH, rule_id=2)
        coord.confirm_region_points(BATCH, REGION_A)
        v2 = grid.rebuild_region(BATCH, REGION_A)
        grid.publish_region(BATCH, REGION_A, v2["version"])

        replay = grid.get_coarse_grid_observations(
            BATCH, REGION_A, coarse["geohash"], version=v1["version"])
        assert replay["point_count"] == coarse["point_count"]
        manifest = coord.get_change_manifest(BATCH)
        latest_by_id = {c["point_id"]: c for c in manifest["changes"]}
        for obs in replay["observations"]:
            change = latest_by_id[obs["point_id"]]
            # 回算用的是该观测被采用的坐标，同时保留原始坐标可对照
            assert obs["applied_lat"] == change["trans_lat"]
            assert obs["applied_lon"] == change["trans_lon"]
            assert obs["orig_lat"] == change["orig_lat"]
            assert obs["rule_version"] == change["rule_version"]


class TestSwitchMidwayQuery:
    """依次确认两个区域，切换中途发起查询。"""

    def test_switch_query_reports_valid_window_and_recomputable_grid(self, two_region_world):
        _, coord, grid, _ = two_region_world
        coord.transform_batch(BATCH, rule_id=2)
        coord.confirm_region_points(BATCH, REGION_A)
        b_pending = coord.list_pending_review(BATCH, REGION_B)
        coord.mark_point_review(BATCH, [c["point_id"] for c in b_pending[:3]], "已确认")

        rebuild_a = grid.rebuild_region(BATCH, REGION_A)
        rebuild_b = grid.rebuild_region(BATCH, REGION_B)

        # 中途查询：A、B 都还没有正式可读版本
        midway = grid.query_during_switch(BATCH, [REGION_A, REGION_B])
        by_region = {r["region"]: r for r in midway["regions"]}
        assert by_region[REGION_A]["summarizable"] is False
        assert by_region[REGION_B]["pending_versions"] == [rebuild_b["version"]]

        # 先发布A，再次查询：A可用于后续汇总，B仍不可读
        grid.publish_region(BATCH, REGION_A, rebuild_a["version"])
        after_a = grid.query_during_switch(BATCH, [REGION_A, REGION_B])
        by_region = {r["region"]: r for r in after_a["regions"]}
        assert by_region[REGION_A]["summarizable"] is True
        assert by_region[REGION_A]["valid_from"] is not None
        assert by_region[REGION_A]["valid_to"] is None
        assert by_region[REGION_B]["summarizable"] is False
        assert by_region[REGION_B]["active_version"] is None

        # B补齐复核后才能发布
        coord.confirm_region_points(BATCH, REGION_B)
        rebuild_b2 = grid.rebuild_region(BATCH, REGION_B)
        assert rebuild_b2["excluded_pending"] == 0
        grid.publish_region(BATCH, REGION_B, rebuild_b2["version"])

        final = grid.query_during_switch(BATCH, [REGION_A, REGION_B])
        assert all(r["summarizable"] for r in final["regions"])
        # 两个区域各自只有一套规则，未发生新旧坐标混算
        for region in (REGION_A, REGION_B):
            grids = grid.get_region_grids(BATCH, region)
            rules = {g["source_rule"]["rule_id"] for g in grids}
            assert rules == {2}

        # 区域A当前正式版格的观测可按采用坐标复算
        active_a = grid.get_region_grids(BATCH, REGION_A)
        replay = grid.get_coarse_grid_observations(
            BATCH, REGION_A, active_a[0]["geohash"])
        assert replay["point_count"] == active_a[0]["point_count"]
        assert all(o["review_state"] == "已确认" for o in replay["observations"])
