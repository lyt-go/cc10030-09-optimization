# 地理空间轨迹与网格聚合分析平台

面向定位点采集、坐标标准化、网格切分、轨迹分段驻留识别、空间关联聚合与区域维度产出的地理空间数据分析平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 定位源接入 | `geo_source` | 采集批次 | 批次编号、采集终端、定位方式 |
| 坐标标准化 | `coord_transform` | 转换规则 | 规则编号、源坐标系、目标坐标系 |
| 轨迹切分 | `track_segment` | 轨迹分段 | 分段编号、终端编号、起始时间 |
| 驻留识别 | `dwell_detect` | 驻留事件 | 事件编号、分段编号、中心坐标 |
| 网格索引 | `grid_index` | 网格单元 | 网格编号、网格层级、边界范围 |
| 围栏判定 | `geofence_rule` | 电子围栏 | 围栏编号、围栏形状、顶点序列 |
| 空间关联 | `spatial_join` | 关联任务 | 任务编号、左数据集、右数据集 |
| 兴趣点匹配 | `poi_match` | 匹配候选 | 候选编号、驻留事件、兴趣点编号 |
| 区域维度 | `region_dim` | 区域层级 | 区域编号、上级区域、区域名称 |
| 热力聚合 | `heatmap_aggregate` | 聚合窗口 | 窗口编号、网格层级、时间粒度 |
| 起止流量 | `od_matrix` | 流量单元 | 单元编号、起点网格、终点网格 |
| 路径还原 | `route_restore` | 还原路段 | 路段编号、分段编号、路段序列 |
| 密度估算 | `density_estimate` | 密度网格 | 估算编号、网格编号、带宽参数 |
| 轨迹质量 | `geo_quality` | 质量报告 | 报告编号、批次编号、漂移点数 |
| 点位归并 | `dedup_merge` | 归并批次 | 归并编号、批次编号、去重键列 |
| 分析快照 | `geo_snapshot` | 版本快照 | 快照编号、分析口径、网格层级 |
| 历史回补 | `geo_backfill` | 回补任务 | 回补编号、目标数据集、起始日期 |
| 结果导出 | `geo_export` | 导出任务 | 导出编号、快照编号、导出格式 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 纠偏重建工作流（坐标标准化 × 网格索引）

接收坐标纠偏数据后的重建与发布稳定性由 `app/routers/workflow.py` 暴露，
业务约束全部落在 `coord_transform` 与 `grid_index` 两个领域服务：

1. **逐点变更清单**：`POST /api/workflow/coord/{batch}/transform` 除转换数据集外，
   生成 `data/coord_manifests/{batch}_manifest.json`，逐点记录原始观测、新旧坐标、
   采用规则（规则编号/版本/源/目标坐标系）、移动距离与复核状态；换过坐标的观测默认
   「待复核」，未变化的自动「已确认」。
2. **新旧范围独立核验**：清单分别给出新旧坐标的经纬度边界与 SHA256 校验值，
   `POST /api/workflow/coord/{batch}/verify-ranges` 重算原始点与转换结果独立比对，
   篡改清单会被检出。
3. **区域复核**：`POST .../regions/{region}/confirm` 按区域放行，
   `GET .../pending-review?region=` 查看待复核点，`POST .../review` 逐点登记结论。
4. **区域版本化重建**：`POST /api/workflow/grid/{batch}/regions/{region}/rebuild`
   只采用「已确认」观测，版本状态流转为
   `待重建 → 待复核 → 正式可读`（可驳回重建）；待复核分区禁止发布为正式可读。
   同一区域存在多套来源规则时拒绝建版，禁止新旧坐标混算同一个点；每个网格都带
   `source_rule` 与逐点 `point_rules` 来源标注。
5. **旧版保留与回滚**：发布新版时旧正式版转为「已归档」，文件完整保留，
   `POST .../rollback` 可恢复为正式可读。
6. **旧格→新格差额**：版本元数据 `diff_from_active` 基于点的实际移动给出每格
   撤销（withdraw）/补入（add_in）计数与逐点 `point_moves`，
   `GET .../diff` 供下游对已交接粗格计数做撤销或补入。
7. **切换中途查询**：`POST .../switch-query` 返回各区域当前正式版本、
   `valid_from/valid_to` 有效时间区间与是否可用于汇总；
   `GET .../coarse-grid/{geohash}/observations` 返回原粗格按采用坐标回算所需观测。

版本产物目录：`data/grid_versions/{batch}/{region}/`（`versions.json` 目录 + 每版一个 JSONL）。

领域：数据工程
子方向：数据分析与数据应用
