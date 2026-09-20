# v0.6 独立安装与生产前端

适用于已有 Python 3.12+、Node.js 22+ 和 Docker 的电脑。账号、项目、工作流、数据及模型产物分别保存在配置的数据目录和工作目录；升级保留这些目录。先按 [README](../README.md) 安装 Python/前端依赖，创建本机 `.env` 并构建沙盒与建模镜像。

## 启动可试用实例

在仓库根目录运行后端，保持这个终端打开：

```sh
.venv/bin/uvicorn agent_platform.api:app --host 127.0.0.1 --port 8001
```

需要管理账号和旧项目时，在另一个终端初始化管理员，密码由命令交互读取：

```sh
.venv/bin/python -m agent_platform.auth 管理员用户名
```

普通用户也可直接从 `/register` 注册并创建自己的项目，不需要管理员服务令牌。账号不会自动得到模型连接。需要服务端集成时再设置自己的 `API_TOKEN`；默认 `change-me` 不提供访问权限。

从仓库根目录构建并启动前端：

```sh
npm --prefix platform/frontend run build
HOSTNAME=127.0.0.1 PORT=3000 AGENT_PLATFORM_URL=http://127.0.0.1:8001 npm --prefix platform/frontend start
```

打开 `http://127.0.0.1:3000/register` 或 `/login`。`npm start` 现在运行构建出的独立服务，端口和监听地址使用 `PORT`、`HOSTNAME` 环境变量；开发热更新继续使用 `scripts/dev_platform.sh`。

构建时自动将 `public` 和 `.next/static` 放入 `.next/standalone`，其中包含必需的服务依赖。可以把整个 `.next/standalone` 目录复制到同一系统和架构的运行位置，再设置上述环境变量并执行 `node server.js`，不需要源代码和外层 `node_modules`。跨系统或架构应在目标环境构建，避免混用原生依赖。处理方式依据 [Next.js 独立输出说明](https://nextjs.org/docs/app/api-reference/config/next-config-js/output)。

## 模型与网络

本地 Docker 训练、手动建图和已有模型预测不需要付费大模型连接。远程项目对话或 AI 生成需要在项目设置填写已授权连接，并由部署者在确认使用范围后启用 `MODEL_EGRESS_ENABLED=true`、重启后端；默认仍为 false。连接不可用时不自动换供应商。

局域网试用时将前端 `HOSTNAME` 改为 `0.0.0.0`，访问这台电脑的实际局域网 IP 和 3000 端口。后端可以继续只监听回环地址，浏览器请求由前端代理转发。不同设备必须能互访；公共网络或无线客户端隔离导致的连接超时不能靠登录系统解决。反向代理及会话配置见 [登录说明](V06_AUTH.md)，没有以本机访问代替跨设备验收。

## 前端容器

`platform/frontend/Dockerfile` 现在只携带已构建的独立运行包，不在运行层再次安装 npm 依赖。它提供前端，后端和计算环境仍需分别配置。例如后端在可达地址 `http://backend-host:8001` 时：

```sh
docker build -t lilies-studio:local platform/frontend
docker run --rm -p 127.0.0.1:3000:3000 -e AGENT_PLATFORM_URL=http://backend-host:8001 lilies-studio:local
```

`backend-host` 必须替换为容器可访问的实际后端地址。Docker Desktop 上可使用 `host.docker.internal`；普通 Linux Docker 不默认提供该地址。现有 `compose.yaml` 的全栈计算部署仍未作为本次验收路径，不把前端镜像成功称为一键完整部署。

## 2026-09-20 实际验证

- 从已提交源码导出隔离副本，使用新的 Python 3.14 虚拟环境安装平台包，在仓库外启动；`pip check` 通过。
- 前端用 Node.js 24.15.0、npm 11.12.1 执行干净 `npm ci`；141 项测试、类型检查、生产构建通过。独立运行包复制到源码外运行，页面交互及静态资源正常。
- 沙盒和建模镜像以 `--no-cache` 重新安装并构建，ARM64/Linux/Python 3.12；使用当前主机 UID/GID。未复用开发项目、数据库、凭据、模型产物或旧计算镜像。
- 在空数据库中通过页面注册、新建项目、粘贴导入 UCI Wine Quality 训练和预测数据、创建未绑定预测流程、提前运行得到准确缺项提示、独立训练、绑定同一流程、预测及下载。训练 1260 行，预测 339 行；来源和指标限制见 [真实数据记录](V06_REAL_ML.md)。训练任务没有工作流编号。
- 下载 CSV 与运行产物字节一致，SHA256 为 `476b7eeb9dccc03e5815886dd075be689dce6514a12de7e058d04024b24a1178`。另通过项目代码接口使用新沙盒计算并保存结果；该项是接口验证，不称为页面操作。
- 更新后的 Docker 前端在 Node.js 22/Linux 构建、运行，浏览器读取项目和预测结果、下载同一 CSV 均通过；未登录接口返回 401。
- 首次干净 npm 安装报告 7 项漏洞。升级到 Next.js 16.3.5、Vitest 4.1.11、PostCSS 8.5.28 及兼容间接依赖后，锁文件和实际干净安装的 `npm audit` 均报告 0 项。修复依据包括 [Next.js 公告](https://github.com/vercel/next.js/security/advisories/GHSA-2xp9-vwfh-vxw4) 和 [Vitest 公告](https://github.com/vitest-dev/vitest/security/advisories/GHSA-82fw-gwwq-j7x9)。审计结果不等于不存在其他缺陷。

以上仍使用同一台 Mac 的 Docker Desktop 和浏览器，属于依赖、数据与运行包隔离验证。另一台物理设备、原生文件选择器及真实 AI 连接恢复后的验收仍需完成；全程没有开启远程模型出口。
