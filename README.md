# xbow-benchmark-platform

自托管的多用户 CTF 解题平台，托管 [xbow validation-benchmarks](https://github.com/xbow-engineering/validation-benchmarks) 的 104 个 Web 安全靶机。

提供完整的题目管理、动态 flag 注入、自动判题、排行榜、解题数据导出等功能，支持人类用户通过 Web 界面操作，也支持 AI agent 通过 API 或 MCP 协议自动化做题。

## 功能

- **104 个 Web 安全靶机**：涵盖 IDOR、SQL 注入、XSS、SSRF、文件上传、认证绕过等漏洞类型，难度分 L1/L2/L3。
- **动态 flag**：每次启动一道题注入一个随机 flag，提交时与该次启动的 flag 比对，通过后自动关闭并清理靶机容器。
- **多用户 / 多解题表**：注册（需注册码）后可创建多张解题表，每张表独立的 104 题进度和 api-key。
- **Web 界面**：解题看板（状态/用时/筛选/统计）、排行榜、解题数据导出（JSON/CSV）、管理后台。
- **API 接入**：通过 api-key 完成做题闭环（列题 → 启动 → 提交 → 关闭），供 AI agent 或自动化工具调用。
- **MCP 协议**：内置 MCP Server，支持 Claude / opencode 等 MCP 客户端直接做题。
- **安全设计**：API 不暴露题目考点（tags/title/description），关闭题目不重置计时（防作弊），生产环境关闭 Swagger。

## 快速开始

### 一键部署

```bash
git clone https://github.com/Blue-And-White/xbow-benchmark-platform.git
cd xbow-benchmark-platform
bash scripts/setup_all.sh
```

脚本会自动完成：下载 xbow 仓库 → 生成 flag manifest → 构建 104 个靶机镜像 → 启动平台。

可通过环境变量自定义配置：

```bash
XBOW_PORT=6888                        # 平台端口
XBOW_ADMIN_PASSWORD=YOUR_PASSWORD     # 管理员密码
XBOW_N_BUILD=104                      # 构建题数
bash scripts/setup_all.sh
```

### 手动部署（Docker）

```bash
git clone https://github.com/Blue-And-White/xbow-benchmark-platform.git
cd xbow-benchmark-platform

# 1. 下载 xbow 仓库到 validation-benchmarks/
curl -fL -o vb.tar.gz \
  https://github.com/xbow-engineering/validation-benchmarks/archive/refs/heads/main.tar.gz
tar xzf vb.tar.gz && mv validation-benchmarks-main validation-benchmarks && rm vb.tar.gz

# 2. 生成 flag manifest
python3 scripts/gen_manifest.py

# 3. 配置环境
cp .env.example .env   # 编辑 REPO_DIR / RUNS_DIR / 密码等

# 4. 启动
docker compose --env-file .env up -d --build
```

访问 `http://localhost:8000`，默认管理员账号 `admin`（密码见 `.env` 中的 `ADMIN_PASSWORD`）。

## 配置说明

| 环境变量 | 说明 | 默认值 |
|---|---|---|
| `XBOW_PORT` | 平台监听端口 | 8000 |
| `XBOW_ADMIN_USER` | 管理员用户名 | admin |
| `XBOW_ADMIN_PASSWORD` | 管理员密码 | 启动时随机生成 |
| `XBOW_REGISTRATION_CODE` | 用户注册码 | 需自行设置 |
| `XBOW_PUBLIC_BASE_URL` | 对外访问地址（生成靶机反代 URL） | http://localhost:8000 |
| `XBOW_CHALLENGE_HOST` | 平台访问靶机容器的地址（裸跑 127.0.0.1，容器内 host.docker.internal） | 127.0.0.1 |
| `XBOW_MAX_CONCURRENT` | 每张解题表最大并发题数 | 3 |
| `XBOW_REPO_DIR` | xbow 仓库路径 | ./validation-benchmarks |

## API

每张解题表有独立的 api-key，用它完成做题闭环：

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| POST | `/api/auth/register` | 注册码 | 注册账号 |
| POST | `/api/auth/login` | 账密 | 登录 |
| GET/POST | `/api/sheets` | 登录 | 列出/创建解题表（返回 api-key） |
| DELETE | `/api/sheets/{id}` | 登录 | 删除解题表 |
| GET | `/api/challenges` | X-API-Key | 列出 104 题 + 状态（不暴露考点） |
| POST | `/api/challenges/{benchmark}/start` | X-API-Key | 启动靶机，返回反代 URL |
| POST | `/api/challenges/{benchmark}/submit` | X-API-Key | 提交 flag，正确则自动关闭容器 |
| POST | `/api/challenges/{benchmark}/stop` | X-API-Key | 手动关闭靶机 |
| GET | `/api/leaderboard` | 公开 | 排行榜 |

题目地址 = 反代 URL（`http://<平台地址>/c/<attempt_id>/`），公网可直接访问。

登录后在「API 文档」页面可查看完整的调用示例。

## MCP 接入

平台内置 MCP Server，AI agent 可通过 MCP 协议做题：

```json
{
  "mcpServers": {
    "xbow-ctf": {
      "command": "python",
      "args": ["mcp/server.py"],
      "env": {
        "XBOW_PLATFORM_URL": "http://YOUR_SERVER_IP:6888",
        "XBOW_API_KEY": "xben_YOUR_API_KEY"
      }
    }
  }
}
```

提供 4 个 tools：`list_challenges`、`start_challenge`、`submit_flag`、`stop_challenge`。详见 [mcp/README.md](mcp/README.md)。

## 系统要求

- Docker 20+ 及 docker compose v2
- Python 3.10+
- 磁盘空间 ~30GB（104 个靶机镜像 + 仓库）
- 内存 4GB+（单题靶机约 200-500MB）

## 技术栈

- 后端：FastAPI + SQLAlchemy 2.0 (async) + SQLite
- 前端：Jinja2 + HTMX + Tailwind CSS
- 容器：Docker + docker compose
- MCP：stdio JSON-RPC

## 许可证

本项目采用 Apache License 2.0。

靶机内容来自 [xbow validation-benchmarks](https://github.com/xbow-engineering/validation-benchmarks)（Apache License 2.0, Copyright 2024- XBOW USA Inc.），本平台不修改靶机代码，仅在构建时注入镜像源加速补丁和动态 flag。靶机包含故意留存的漏洞，仅供安全研究和学习使用。
