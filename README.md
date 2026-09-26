# 实训学时合规与冻结服务

该服务汇聚学员签到、导师确认和请假修正事件，按培养方案与时区重放学时状态，并保存可追溯的学期冻结快照。项目还提供导师分配、证明材料、豁免复核、规则版本、名额、通知和数据留存等领域模块，供后续业务扩展时复用统一的状态与审计约束。

## 专业层面公开摘要

冻结快照可通过公开摘要对外发布：先维护学生到 (组织, 类别) 的目录映射（`PUT /api/plans/{plan_version}/directory`），再按版本化的隐私规则（`POST /api/privacy-rules`，创建后不可变）对冻结快照聚合。聚合应用最小样本抑制与一致性补充抑制，被抑制单元不携带任何数值；`POST .../public-summaries/preview` 在发布前展示隐私影响（抑制面、未映射学生、风险标记），`POST .../public-summaries` 将摘要固化为草稿，规则升级不会回写旧摘要。摘要经 `approve`、`publish`、`withdraw` 流转，撤回后公开查询立即失效，但内部视图保留完整审计轨迹。公开查询 `GET /api/public/summaries/{summary_id}` 无需身份，支持按组织与类别交叉筛选；内部接口要求 `X-Actor-Id` 与 `X-Actor-Role` 头（analyst/privacy_officer/auditor）。

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
