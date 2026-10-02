# 司法鉴定检材流转与复核服务

本项目是面向司法鉴定机构的 Python 后端服务，用于登记委托或移送案件、接收带封识的检材、记录保管位置与流转、执行专业检验、安排复核并处理环境和质量告警。案件、检材、检验记录和领用审批都保存在本地 SQLite 中，关键写入带版本或幂等键，适合在单个 Linux 应用容器内运行。

## 运行环境

- Python 3.11
- FastAPI 与 Uvicorn
- SQLite 3，由 Python 标准库提供

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

默认数据库位于 `data/forensics.db`，也可以通过 `FORENSICS_DATABASE_PATH` 指向其他 `.db`、`.sqlite` 或 `.sqlite3` 文件。

## 初始化与启动

```bash
python -m app.cli init-db
python -m app.cli check-db
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康检查为 `GET /api/system/health`。首次使用可调用 `POST /api/auth/bootstrap` 创建管理员，再通过 `POST /api/auth/login` 取得 Bearer 会话令牌。鉴定业务接口统一位于 `/api/forensics`。

## 测试与构建检查

```bash
python -m pytest
python -m compileall -q app tests
```

下面两条命令分别检查 HTTP 入口和完整的入库演示链路：

```bash
python -m app.cli smoke
python -m app.cli demo
```

## 业务边界

- `app/forensics/cases.py` 管理委托机构、案件档案、委托资料与受理状态。
- `app/forensics/custody.py` 管理检材、库位容量、容器摆放、流转、领用和冻结。
- `app/forensics/examinations.py` 管理检验规程、取样、观察记录、检验结果与复核日程。
- `app/forensics/quality.py` 管理温湿度读数、偏离告警和检材领用审批。
- `app/forensics/disposal.py` 管理版本化保存策略、延期决定与到期处置清单（候选快照、逐项决定、双人确认、一次性销毁）。
- `app/api`、`app/services` 和 `app/repositories` 提供身份、权限、审计、后台作业及维护能力。

## 到期处置流程

档案管理员对超过保存期限的检材，按规则而非直接报废流水进行处置：

1. **保存策略**：`POST /api/forensics/retention-policies` 按检材类别与鉴定专业登记保存月数，同一策略编码保留多个版本与生效区间。
2. **生成候选快照**：`POST /api/forensics/disposal-batches` 以已结案（案件退出保存）检材为范围，依据检材类别、案件结案时间和**结案当时有效**的保存策略计算到期日，形成不可变候选快照（`basis_json` 记录结案事件、策略版本、历次延期与冻结快照）。自动排除四类：冻结、未完成检验、未关闭复核、仍在有效期内的延期决定，并写明排除原因。
3. **逐项决定**：`POST .../{id}/decisions` 可对每个候选保留、延期或提交销毁；延期另写版本化延期记录。决定时刻若检材版本、状态已变化或出现新阻断事项，只把该条目标记为 `conflicted`，不覆盖新事实。
4. **提交与双人确认**：`submit` 后由保管人与监督人分别 `confirm`，二者不能为同一人；确认期间（提交之后）出现任何新保全事件，对应待销毁条目立即失效（`invalid`）。
5. **一次性销毁**：`execute` 在单一事务内把检材置为 `disposed`、可用数量清零、移除全部在架容器摆放，并写入与清单关联的「报废」流转记录；执行前再次核对版本、状态与冻结。
6. **追溯**：`GET .../disposal-candidates/{id}/trail` 可从清单条目追到候选依据、排除原因、双人确认与实际销毁结果。`disposal_events` 保留全过程操作轨迹。

## 一致性约定

SQLite 连接启用外键、WAL、忙等待和即时写事务。案件档案、库位、容器摆放、检验任务和领用申请采用版本号防止旧请求覆盖新状态；入库、移库、取样和传感读数使用业务键去重。专业检验保留采用的规程版本和每个检查点的观察记录，完成后可依据鉴定专业及风险策略生成下一次复核日期。到期处置清单冻结候选时的检材版本、状态与策略版本，批量决定和最终销毁均对照快照校验，状态或版本变化只标记冲突；保存策略与延期决定保留全部版本。会话令牌只保存摘要，审计记录不保存明文密码或令牌。
