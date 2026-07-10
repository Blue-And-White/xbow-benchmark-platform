# xbow-benchmark-platform

一个自托管的多用户 CTF 解题平台，托管 [xbow validation-benchmarks](https://github.com/xbow-engineering/validation-benchmarks) 的 104 个 web 安全靶机。

- **动态 flag**：每次启动一道题注入一个随机 flag（不重新构建镜像），提交时和该次启动记录的 flag 比对，一致才算通过；通过后平台自动关闭并清理靶机容器，无需手动停。
- **多用户 / 多解题表**：注册（需注册码）→ 登录 → 创建若干"解题情况表"，每张表一个独立 api-key + 独立的 104 题进度。
- **完整 UI**（HTMX + Tailwind）：登录/解题表列表（带每表汇总进度）/104 看板（表格 + 筛选 + 统计面板 + 轮询）/排行榜/管理/API 文档。
- **API-first**：拿一个 api-key 即可完成做题闭环（列题/启动/提交/停止），供人或 agent 调用。

## 架构

- 后端：FastAPI（async）+ SQLAlchemy 2.0（async）+ SQLite(WAL)。
- 容器编排：平台 shell out 调 `docker compose`，每次启动用独立 compose project（`xben_<attempt_id>`）+ 临时工作目录，天然支持同题多用户并发。
- 平台自身容器化（`Dockerfile` + `docker-compose.yml`），挂 docker.sock 编排靶机。

## 快速开始（本地 docker）

```bash
git clone https://github.com/Blue-And-White/xbow-benchmark-platform.git
cd xbow-benchmark-platform

# 1) 把 xbow 仓库放到 validation-benchmarks/（或用 .env 指向已有路径）
curl -fL -o vb.tar.gz \
  https://github.com/xbow-engineering/validation-benchmarks/archive/refs/heads/main.tar.gz
tar xzf vb.tar.gz && mv validation-benchmarks-main validation-benchmarks && rm vb.tar.gz

# 2) 生成 flag manifest（扫 104 个 Dockerfile，定每题 flag 落点）
python3 scripts/gen_manifest.py        # -> flag_manifest.json (104/104)

# 3) 配 .env（REPO_DIR / RUNS_DIR 必须是宿主绝对路径）
cp .env.example .env   # 按你的实际路径改 REPO_DIR / RUNS_DIR / ADMIN_PASSWORD
#    容器化运行时 CHALLENGE_HOST=host.docker.internal（平台在容器里时反代用）

# 4) 起平台
docker compose --env-file .env up -d --build
# -> http://localhost:8000  （默认 admin/<.env 里的 ADMIN_PASSWORD>，注册码 ROOTLAB@123）
```

> ⚠️ 题目镜像需先预构建（`scripts/prebuild.py`）。未构建的题点"启动"会提示 `image not built — run prebuild`。

## 配置（.env）

| 变量 | 说明 |
|---|---|
| `REPO_DIR` | xbow 仓库的**宿主绝对路径**（平台与 docker daemon 都要能访问） |
| `RUNS_DIR` | 每次 attempt 的工作目录**宿主绝对路径**（flag 文件/compose 临时文件，daemon 要能读） |
| `ADMIN_USER` / `ADMIN_PASSWORD` | 启动时 seed 的管理员账号 |
| `REGISTRATION_CODE` | 注册码（默认 `ROOTLAB@123`） |
| `PUBLIC_BASE_URL` | 对外基础 URL（生成靶机反代地址，如 `http://your-host:8000`） |
| `CHALLENGE_HOST` | 平台进程访问靶机容器用：本机裸跑 `127.0.0.1`；平台在容器里 `host.docker.internal` |
| `ALLOW_DIRECT_PORT` | 是否额外返回直连 `http://127.0.0.1:<port>` |

管理员可在「管理」页改：注册码 / 每用户最大并发题数（默认 3）/ 对外 URL / 直连开关。

## API（api-key 做题闭环）

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| POST | `/api/auth/register` | 注册码 | 注册账号 |
| POST | `/api/auth/login` | 账密 | 登录（会话，网页用） |
| GET/POST | `/api/sheets` | 登录 | 列/建解题表（返回 api_key） |
| DELETE | `/api/sheets/{id}` | 登录 | 删表（停跑着的容器 + 级联删记录） |
| GET | `/api/challenges` | **X-API-Key** | 104 题 + 本表状态 |
| POST | `/api/challenges/{benchmark}/start` | **X-API-Key** | 起靶机（随机 flag，返回 url） |
| POST | `/api/challenges/{benchmark}/submit` | **X-API-Key** | 提交 flag；正确则自动关停清理容器 |
| POST | `/api/challenges/{benchmark}/stop` | **X-API-Key** | 手动放弃（清 flag + 计时） |
| GET | `/api/leaderboard` | 公开 | 排行榜（按 用户/看板 排名） |

网页端登录后导航有「API 文档」页（做题流程 + curl 示例）和 Swagger `/docs`。

## flag 与判题机制

- xbow 每个靶机的 flag 是**确定性**的：`FLAG{sha256(大写(benchmark名))}`，构建时（`common.mk`）通过 `--build-arg FLAG=` 注入。
- 平台启动一道题时，按该题 flag 落点类型注入一个**随机** flag 覆盖（不重建镜像）：
  - **file**（写到 /flag 等）：bind-mount 一个随机 flag 文件覆盖。
  - **env**（`ENV FLAG`）：override 环境变量。
  - **embedded**（sed 进源文件）：起容器后 `exec sed` 把确定性 flag 换成随机 flag。
  - **fixed**（flag 烤进 mysql init.sql，运行时改不动）：用 build 时的确定性 flag，平台照样记录 + 比对。
- 提交时比对本次启动记录的 flag；一致 → solved + 记录用时 + 自动 `docker compose down -v` 清容器。

`scripts/gen_manifest.py` 扫 104 个 Dockerfile 生成 `flag_manifest.json`（类型/路径/服务名/原始 flag），平台据此注入。当前：file=35 / embedded=51 / env=16 / fixed=2，104/104 支持。

## 预构建题目镜像

```bash
# 在能访问 docker 的机器上（远程建议放后台）
python3 scripts/prebuild.py --repo ./validation-benchmarks --n 60 --log prebuild.log
```

脚本对每个 benchmark：找到声明 `ARG FLAG` 的 Dockerfile → 把宿主 CA bundle 拷进构建上下文 → 打补丁（CA + 国内 apt/apk/pip 源，规避国内构建卡死）→ `make build` → 还原 Dockerfile。逐题记录 ok/fail/skip，失败继续。国内/高核机器构建慢（vfs），放后台即可。

## 已知坑（已固化进代码）

1. GitHub 国内限速 → 用 gh-proxy 拉仓库；get.docker.com/download.docker.com 被墙 → docker 走阿里云镜像源装。
2. 基础 slim 镜像无 ca-certificates + 国内 apt 源 HTTPS 重定向 → 构建期 COPY 宿主 CA bundle + 换腾讯 apt 源、阿里云 pip 源。
3. 384 核机器上 `mysql:5.7.15` 等老镜像里老 Go 二进制 `procresize(384)` panic → 所有服务加 `GOMAXPROCS=8`。
4. `docker compose up` 按项目名重找镜像会重建出空 FLAG 镜像 → 给每个服务 pin 固定 image 名（`<benchmark>-<service>`）。
5. 部分靶机依赖过时（如 phantomjs/python2.7）构建不了 → 预构建脚本会记 fail，不影响其它题。
