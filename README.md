# JobPilot

[![jobpilot CI](https://github.com/Galilieo/jobpilot/actions/workflows/jobpilot.yml/badge.svg)](https://github.com/Galilieo/jobpilot/actions/workflows/jobpilot.yml)
[![web CI](https://github.com/Galilieo/jobpilot/actions/workflows/web.yml/badge.svg)](https://github.com/Galilieo/jobpilot/actions/workflows/web.yml)

面向 AI 应用 / Agent 开发实习求职的个人项目：**实习求职助手 Agent**（开发中）。

> 当前状态（2026-11-16）：**设计已冻结，第 0 周骨架开发中。** 实际进度见 [06 V1 实施任务书](notes/求职助手Agent/06-V1实施任务书.md)，设计文档见 [notes/求职助手Agent](notes/求职助手Agent/README.md)。

## 求职助手 Agent 是什么

通过对话提供简历和岗位资料，帮正在找技术类实习的学生完成「看 JD → 对照简历 → 按岗位改简历 → 准备面试 → 跟踪投递」。

核心约束是**改写简历时不编造经历**：简历先拆成用户确认过的「经历事实」，改写的每一条都必须引用事实，再经过三层依据检查（引用合法性、数字与实体比对、语义支持判断）。效果用自建评测集和对照实验衡量。

V1 定位为个人学习项目，本人求职时在本地使用；暂不上线和推广，但用户表、登录与用户隔离照常实现，为以后开放预留。

| 设计文档 | 内容 |
| --- | --- |
| [00 总览与决策](notes/求职助手Agent/00-总览与决策.md) | 为什么选这个题目、范围、完成标准 |
| [01 功能设计](notes/求职助手Agent/01-功能设计.md) | 经历事实库、JD 解析、匹配、改写与依据检查、模拟面试、记忆等 |
| [02 技术架构与数据模型](notes/求职助手Agent/02-技术架构与数据模型.md) | 架构决定、LangGraph 流程、数据表、RAG、记忆与上下文管理 |
| [03 可信、安全与隐私](notes/求职助手Agent/03-可信安全与隐私.md) | 防编造、提示注入防御、个人信息保护 |
| [04 评测方案](notes/求职助手Agent/04-评测方案.md) | 评测集、指标与对照实验 |
| [05 落地与里程碑](notes/求职助手Agent/05-落地与里程碑.md) | 每周计划、风险 |

### 计划技术栈

- 前端：React + Vite + TypeScript + Ant Design + Ant Design X（展示组件）
- 后端：Python 3.12、FastAPI（异步）、SQLAlchemy 2.1 + Alembic
- 数据库：PostgreSQL + pgvector（本地用 Docker 启动）
- Agent：LangGraph 编排，自写模型调用网关（DeepSeek）
- 评测：自建评测集与对照实验

## 仓库结构

```text
./
├── jobpilot/     # 求职助手后端，独立 uv 项目
├── web/          # 求职助手前端，独立 pnpm 项目
├── docker/       # 本地数据库初始化脚本（配合 compose.yaml）
├── notes/
│   ├── 求职助手Agent/   # 当前项目的设计文档
│   ├── history/        # 已被取代的规划与历史记录
│   └── ...             # 知识笔记，索引见 notes/README.md
└── README.md
```

求职助手的代码位于 `jobpilot/`（后端）和 `web/`（前端），依赖各自独立。前端启动与目录说明见 [web/README.md](web/README.md)，后端测试说明见 [jobpilot/tests/README.md](jobpilot/tests/README.md)。

登录后可在「账号设置」导出你的业务数据。

## 本地启动

以下命令从仓库根目录执行。macOS 需要 Colima、Docker CLI 与 Compose 插件、uv、Node.js 22.22 或更新版本，以及 pnpm 11.7。把仓库放在 Colima 能挂载的目录，例如用户主目录下。用 `docker compose version` 检查 Compose，用 `uv --version`、`node --version` 和 `pnpm --version` 查看其余版本。后端用 Python 3.12，缺少时 `uv sync` 会按项目约束安装。

### 配置并启动

第一次启动时复制配置。已有 `.env` 时保留本地配置，参照示例补齐字段。

```bash
cp .env.example .env
```

- 在 `.env` 给 `POSTGRES_PASSWORD` 填一个本地专用密码。单机运行可使用示例的其他 PostgreSQL 值。
- `OPENAI_API_KEY` 用于简历拆分、JD 解析、匹配和改写。只验证账号、导出、数据库或运行假模型测试时可以留空。使用非默认模型服务时，同时核对 `OPENAI_BASE_URL` 和对应的 `JOBPILOT_*__MODEL_NAME`。

安装项目依赖并启动开发环境。

```bash
colima start --cpu 2 --memory 4
uv sync --project jobpilot --locked
pnpm --dir web install --frozen-lockfile
./scripts/dev.sh
```

`scripts/dev.sh` 检查工具和 Colima，启动 PostgreSQL 并等待健康检查通过，再对开发库执行 `alembic upgrade head`。最后并行启动 FastAPI 和 Vite。打开 [JobPilot](http://127.0.0.1:5174) 注册登录，服务连通性可从 [健康检查](http://127.0.0.1:5174/api/health) 确认。按 Ctrl+C 停止前后端，数据库容器继续运行。

### 测试与检查

```bash
(cd jobpilot && uv run --locked pytest -q)
(cd jobpilot && uv run --locked ruff check && uv run --locked ruff format --check)
pnpm --dir web format:check
pnpm --dir web lint
pnpm --dir web test
pnpm --dir web build
```

后端测试自动迁移独立的本机 `jobpilot_test` 库，并在测试前后清空该库的业务表。模型调用使用假上游。前端 `build` 同时执行 TypeScript 检查。

### 常见问题

| 现象 | 处理 |
| --- | --- |
| 提示 Colima 未运行，或 Docker 无法连接 | 执行 `colima start`，再用 `docker --context colima compose ps` 查看数据库。 |
| 5432、8000 或 5174 端口已被占用 | 用 `lsof -nP -iTCP:5432 -iTCP:8000 -iTCP:5174 -sTCP:LISTEN` 确认进程。可在 `.env` 调整 `POSTGRES_PORT`，再运行 `UVICORN_PORT=8001 JOBPILOT_WEB_PORT=5175 ./scripts/dev.sh`。Vite 会转发到同一后端端口。 |
| 数据库可连，服务启动却报缺表或列 | 执行 `(cd jobpilot && uv run --locked alembic upgrade head)`，再重启后端。 |
| 修改 `.env` 后数据库密码不匹配 | Compose 只在数据卷首次初始化时设置账号和密码。连接已有卷时使用创建它的凭据。 |
| Docker 数据库没有 `jobpilot_test` | 检查 Colima 是否挂载了 `docker/initdb/`。已有卷可用 `docker --context colima compose exec db sh -c 'createdb -U "$POSTGRES_USER" jobpilot_test'` 补建测试库，之后 pytest 会启用扩展并跑迁移。 |

手动清理已过恢复期的账号可运行 `uv run --project jobpilot python -m jobpilot.scripts.purge_accounts`。备份还原步骤见 [05 的本地备份恢复演练](notes/求职助手Agent/05-落地与里程碑.md#本地备份和还原)。

## 早期机制实验（已归档）

这个仓库最初是 AI 应用机制的学习实验场，按阶段手写并验证了：

- 模型调用与调用日志，SQLite 多轮会话与会话归属检查
- Tool Calling 与原生 Agent 循环（调用预算、中途失败的持久化）
- MCP Server / Client 接线
- 分层长期记忆（[Memory V1.1](notes/Memory-V1.1轻量分层长期记忆方案.md)）：提取、选择、注入、删除防重建
- RAG：BGE-M3 Embedding、Chroma、引用 ID 校验
- LangChain 与原生实现对照；LangGraph 的条件路由、检查点、人工确认与失败恢复
- SSE 流式输出与浏览器消费
- 固定合成题评测与有限重试

这部分已于 2026-11-07 收口（离线全量测试 `174 passed`），2026-11-18 从 main 移除，仓库同日由 `agent-lab` 改名为 `jobpilot`（旧笔记里的 agent-lab 指的就是这一阶段）。代码（`app/`、`tests/`、`playground/` 及其 uv 项目）完整保留在标签 [`learning-phase-archive`](https://github.com/Galilieo/jobpilot/tree/learning-phase-archive)，可在 GitHub 上直接浏览，或在本地查看：

```bash
git checkout learning-phase-archive
```

完成清单、验收记录和源码阅读顺序见[早期机制实验说明](notes/history/早期机制实验说明.md)，学习笔记见 [notes/README.md](notes/README.md)；笔记中的源码链接都指向这个标签。

## License

见 [LICENSE](LICENSE)。
