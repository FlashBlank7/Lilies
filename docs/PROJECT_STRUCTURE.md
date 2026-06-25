# 项目目录结构

本仓库现在按“平台实现”和“上游参考源码”分层，避免把正在开发的产品代码与被重构的外部项目混在一起。

## 顶层结构

```text
.
├── platform/
│   ├── backend/
│   │   └── src/agent_platform/   Python 后端、Agent Runtime、Workflow Runtime
│   └── frontend/                 Next.js Studio 前端
├── references/
│   └── claude-code/
│       └── src/                  原 Claude Code TypeScript 源码，只作为迁移参考
├── docs/                         项目文档、业务逻辑、迁移矩阵
├── scripts/                      本平台的启动、验收、升级脚本
├── tests/                        Python 单元与集成测试
├── examples/                     本平台测试/验收使用的样例项目
├── data/                         本地 SQLite / JSONL 运行数据
└── workspaces/                   沙盒工作区
```

## 目录边界

### `platform/backend`

这是当前产品的 Python 后端实现。包名仍然是 `agent_platform`，因此运行命令保持不变：

```bash
.venv/bin/uvicorn agent_platform.api:app --host 127.0.0.1 --port 8001
```

源码实际位置是：

```text
platform/backend/src/agent_platform/
```

### `platform/frontend`

这是原创 Studio 前端，使用 Next.js、React、TypeScript、Tailwind 和 React Flow。

```bash
cd platform/frontend
npm install
npm run dev -- --hostname 127.0.0.1 --port 3000
```

### `references`

这里放外部项目或上游源码快照。它们是重构、迁移和设计参考，不是当前平台运行时的一部分。

当前已有：

```text
references/claude-code/src/
```

后续如果加入 Dify 或其他项目，应放在：

```text
references/dify/
references/<project-name>/
```

不要再把外部项目源码放回根目录 `src/`。根目录目前不再保留 `src/`，避免误以为那是本平台源码。

### `scripts`

脚本属于当前平台，不属于参考项目。标准开发启动入口仍然是：

```bash
./scripts/dev_platform.sh
```

### `tests`

测试仍放在根目录，便于 `pytest` 直接发现。测试导入的是安装后的 `agent_platform` 包，而不是直接依赖旧的 `src/` 路径。

## 新增上游项目的规则

以后再下载或克隆新项目用于重构时，遵守这个规则：

1. 外部项目统一进入 `references/<project-name>/`。
2. 平台产物统一进入 `platform/backend` 或 `platform/frontend`。
3. 不在根目录新增含糊的 `src/`、`web/`、`server/` 来放外部源码。
4. 如果某个外部项目需要被长期分析，给它新增一份 `references/<project-name>/README.md`，说明来源、用途和许可证注意点。
5. 如果某个外部项目被迁移成平台能力，只把迁移后的实现放进 `platform/`，原始项目仍保留在 `references/`。

这套边界的目标很朴素：看到 `platform/` 就知道是我们正在交付的软件；看到 `references/` 就知道是可读、可比对、但不直接运行的参考资产。
