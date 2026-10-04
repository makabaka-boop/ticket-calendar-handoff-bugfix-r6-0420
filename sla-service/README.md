# 工单时限计时服务

FastAPI + SQLite 的工单 SLA 计时后端，React 前台展示每一次时限裁决的依据。

## 运行

```bash
# 后端（首次启动自动建库并写入演示数据）
cd backend
../.venv/bin/python -m uvicorn app.main:app --port 8000

# 前端已构建，直接由后端托管：http://localhost:8000
# 前端开发模式：cd frontend && npm run dev   （代理 /api 到 8000）

# 测试
cd backend && ../.venv/bin/python -m pytest tests/ -v
```

删除 `backend/sla.db` 可重置演示数据。

## 领域模型

- **工作日历由 UTC 区间给出**：`calendar_interval` 存 `work`（工作区间）与 `holiday`（假日切口）两类半开区间；有效日历 = 工作区间 − 假日切口（`app/intervals.py` 的 `subtract`）。
- **策略版本**：`policy_version` 携带警告/升级阈值（有效工作分钟）与日历。工单创建时固定当时的最新版本（`ticket.policy_version_id`），之后发布新版本不影响旧工单。
- **计时**：`ticket_segment` 记录运行段；等待客户 = 关闭当前段，恢复 = 开启新段，段间空隙即暂停区间。有效工作分钟 = 运行段 ∩ 有效日历 的总时长，重新打开后在既有分钟上继续累积。
- **截止时刻**：从 now 起在有效日历上 `advance` 剩余分钟得到；暂停期间时钟冻结，页面展示“若现在恢复”的预计截止。

## 事务、幂等与并发

- 所有写路径（用户状态操作、后台扫描裁决、策略迁移）都走 `BEGIN IMMEDIATE` 事务（`app/db.py:tx`），SQLite 将并发写者串行化——**后台计时器与用户状态操作按同一事务顺序裁决**：解决先提交则扫描不再登记，扫描先提交则裁决留痕、解决照常生效（`tests/test_race.py`）。
- **只登记一次**：`adjudication` 上有 `UNIQUE(ticket_id, kind, policy_version_id)`，配合 `INSERT OR IGNORE`，重启与重复扫描不产生重复记录。
- **乐观锁**：`ticket.revision` 每次变更自增；状态操作与迁移要求 `expected_revision`，过期提交返回 **409** 及当前 revision。
- **裁决依据**：每条警告/升级保存 `basis_json`（裁决时点、累计分钟、阈值、计入/暂停区间明细），前端可展开查看。

## 策略迁移

- `GET /api/tickets/{id}/migration-preview?to_version_id=` 预览旧/新差异（累计分钟、警告/升级截止、位移秒数），不落库。
- `POST /api/tickets/{id}/migrate` 显式迁移：差异证据写入 `policy_migration.diff_json` 后同事务切换固定版本。历史裁决保留；迁移后按新版本重新裁决（唯一键含版本，不会与旧记录冲突）。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/tickets` | 列表（累计分钟、升级截止、警告/升级标记） |
| POST | `/api/tickets` | 创建并固定最新策略版本 |
| GET | `/api/tickets/{id}` | 详情：截止时刻、已计入区间、暂停区间、升级记录、迁移证据 |
| POST | `/api/tickets/{id}/status` | `{action: wait/resume/resolve, expected_revision}` |
| GET/POST | `/api/policies`, `/api/policies/{id}/versions` | 策略与版本 |
| GET | `/api/tickets/{id}/migration-preview` | 迁移差异预览 |
| POST | `/api/tickets/{id}/migrate` | 显式迁移并保存证据 |
| POST | `/api/scan` | 手动触发一轮裁决（后台每 15s 自动扫描） |

所有端点支持 `?now=<ISO>` 指定裁决时点，便于演示与测试。

## 测试（backend/tests）

| 文件 | 覆盖 |
| --- | --- |
| `test_intervals.py` | 假日切口：区间相减、跨假日 advance、工单截止跳过假日 |
| `test_pause.py` | 暂停跨界：跨周末/跨假日暂停、恢复后续累积、暂停中预计截止 |
| `test_race.py` | 到期与解决竞争：两种确定顺序 + 30 轮并发交错 + 重启后重复扫描幂等 |
| `test_migration.py` | 策略迁移：新版本不影响旧工单、差异预览、证据保存、迁移后重新裁决、迁移冲突 |
| `test_handoff.py` | 队列转派：历史按原队列日历逐段计入、等待状态延续、多队列接力、裁决不重复、无效转派整体回滚、API 409 与证据 |
| `test_api.py` | 过期修订 409、API 扫描幂等、页面字段完整性 |

## 目录

```
backend/app/intervals.py   区间运算（合并/相减/交集/advance）
backend/app/timing.py      计时引擎（累计分钟、截止时刻、计入/暂停区间）
backend/app/services.py    事务化业务操作（状态、裁决、迁移）
backend/app/main.py        FastAPI 路由 + 后台扫描器 + 静态托管
backend/app/seed.py        演示数据（两个策略版本、五种状态工单）
frontend/src/              React 前台（Vite 构建，dist 由后端托管）
```

## Queue handoff
`POST /api/tickets/{id}/handoff` uses the migration request shape but is a distinct operation: it moves the ticket to another queue's policy version (thresholds + work calendar) **without rewriting history**.

- **历史按当时队列计入**：`handoff` 表只记录边界时刻 `(from_version → to_version, at)`。计时引擎（`app/timing.py`）用 `calendar_parts` 把每个运行段在所有转派边界处切开，每一段都用该时刻所属队列版本的有效日历求交集，因此先前已计入的工作分钟不会消失，也不会被新日历重新增加；`counted_parts` 给出逐队列的分钟明细。
- **后续服从新队列**：转派时刻之后的运行段只与新队列日历求交，剩余分钟按新阈值 `advance` 得到截止/预计截止。
- **暂停延续**：等待客户期间转派不新增也不关闭任何运行段，工单仍为 `waiting_customer`；段间唯一的空隙继续作为暂停区间，恢复后在新队列日历上继续计时。
- **裁决只发一次**：裁决按 `kind`（warning/escalation）跟随工单，不随版本重置。已经发出的警告或升级，转派后（含重复扫描与重启）不会再次发出；新队列下尚未发过的裁决在累计达到其阈值时首次登记。
- **整体成功或整体回滚**：转派与 revision 校验在同一个 `BEGIN IMMEDIATE` 事务内。过期 revision 返回 409；已解决工单、目标版本不存在、目标等于当前版本、转派时刻早于工单/上一次转派都会拒绝，且不留半条 `handoff` 记录、不 bump revision。
- **证据**：每条转派保存 `from/to` 两侧策略快照与计时快照（含逐队列 `counted_parts`）及当时状态，详情 API 按时间顺序返回 `handoffs`。
- 已经发生过转派的工单不能再走“策略迁移”接口（那会按单一日历重算整段历史，破坏逐段归属）；策略迁移仍用于从未转派过的工单。
