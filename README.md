# 实训学时合规与冻结服务

该服务汇聚学员签到、导师确认和请假修正事件，按培养方案与时区重放学时状态，并保存可追溯的学期冻结快照。项目还提供导师分配、证明材料、豁免复核、规则版本、名额、通知和数据留存等领域模块，供后续业务扩展时复用统一的状态与审计约束。

## 运行方式

默认数据保存在项目目录的 SQLite 文件中。安装依赖后执行 `uvicorn app.main:app --host 127.0.0.1 --port 8000`，健康检查地址为 `/health`，业务接口位于 `/api`。

## 测试

```bash
python3 -m pytest -q
```

## 编译检查

```bash
python3 -m compileall -q app tests
```

测试覆盖事件幂等导入、跨时区与跨日学时合并、实习确认、负向修正、冻结快照和差异查询；运行过程中不需要单独的数据库或网络服务。

## 公开摘要（隐私保护聚合）

冻结快照可进一步聚合为面向社会公开的专业层面学时达标摘要。公开管线在
`app/core/public_aggregation.py` 中实现为纯函数，工作流接口位于
`app/routers_public.py`：

- **最小样本（k-匿名）**：人数低于 `min_cell_size` 的（组织 × 类别）单元格被抑制；
- **互补抑制**：单元格的互补人群（同组织内其他类别）低于
  `min_cell_size - suppression_margin` 时同样抑制，防止用总数减去大格反推小格；
- **一致性规则**：组织内任一类别被抑制，则组织总人数、达标率与该组织全部单元格
  都不发布；分母由已发布单元格之和导出，保证「单元格之和 = 总数」恒成立，
  总体合计只汇总完整发布的组织；
- **发布前隐私影响**：预览接口返回 `privacy_impact`（含被抑制单元格的真实人数、
  缺少组织/类别的学生），提交审批时必须显式确认且文档指纹匹配；
- **规则不可变**：规则集版本（`ruleset_version`）固化进摘要，规则升级不会改写
  旧摘要，需以新的 `summary_id` 重新预览、审批与发布；
- **撤回留痕**：撤回后公开侧立即不可见，但摘要记录、各阶段操作人与哈希链审计
  日志对内部角色完整保留。

生命周期：`preview → pending_approval → approved → published → withdrawn`
（审批人可驳回回 `preview`）。审批人不得审批自己提交的摘要；发布由独立的
`publisher` 角色执行。内部接口通过 `X-Actor-Id` 与 `X-Actor-Roles` 请求头识别
操作人（角色：`privacy_officer` / `approver` / `publisher` / `auditor`），
公开查询接口 `/api/public/...` 不需要身份，且只返回已发布文档（不含隐私影响、
状态、审计或任何学生标识）。公开交叉筛选（组织 + 类别）会剥离组织分母、达标率
与总体合计，防止减法反推。

主要接口：`PUT /api/plans/{pv}/student-profiles`（组织画像）、
`POST .../public-summaries/{id}/preview|submit|approve|reject|publish|withdraw`、
`GET .../public-summaries[?state=...]`（内部）、
`GET /api/public/plans/{pv}/summaries[?organization=&category=]` 与
`GET /api/public/plans/{pv}/summaries/{id}`（公开）。

