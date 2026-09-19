# MediaWarp Panel

[![Docker Hub](https://img.shields.io/badge/Docker%20Hub-xiaoyu96%2Fmediawarp--panel-2496ED?logo=docker&logoColor=white)](https://hub.docker.com/r/xiaoyu96/mediawarp-panel)
[![Docker Pulls](https://img.shields.io/docker/pulls/xiaoyu96/mediawarp-panel)](https://hub.docker.com/r/xiaoyu96/mediawarp-panel)
[![Image Size](https://img.shields.io/docker/image-size/xiaoyu96/mediawarp-panel/latest)](https://hub.docker.com/r/xiaoyu96/mediawarp-panel/tags)
[![Platform](https://img.shields.io/badge/platform-amd64%20%7C%20arm64-2496ED)](https://hub.docker.com/r/xiaoyu96/mediawarp-panel/tags)
[![GHCR](https://img.shields.io/badge/GHCR-ghcr.io%2Fyq487900%2Fmediawarp--panel-181717?logo=github)](https://github.com/yq487900/mediawarp-panel/pkgs/container/mediawarp-panel)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## ⚠️写在前面
> ⚠️ **本项目不是 MediaWarp 本身。** MediaWarp 的版权归其作者 **[AkimioJR](https://github.com/AkimioJR)**，采用 **AGPL-3.0 修改版许可**（禁止商用；使用其代码须开源并注明出处）。本仓库只包含**外挂面板**的代码，
> MediaWarp 二进制在**构建镜像时从官方 Release 下载**，不随仓库分发。详见 [NOTICE](NOTICE)。
---
> ⚠️ **本项目是个人用AI修改出来的，仅个人使用！！！**  
> ⚠️ **使用本项目产生的任何问题不负任何责任，有问题我也不会解决**(我只会靠AI)

---

## 简介
给 [MediaWarp](https://github.com/AkimioJR/MediaWarp) 套一个**网页设置面板**的 Docker 镜像。

不用再手改 `config.yaml`、也不用「改完还要手动重启容器才生效」——在网页上改完点一下，
面板会在 0.2~0.5 秒内自动重载 MediaWarp 并**确认生效**，结果直接显示在页面上。

---

## 功能

**面板本身**
- 网页改配置 → 「保存并立即生效」，自动重载并校验生效（不用进容器、不用敲命令）
- **配置项按卡片分组**：上游媒体服务器 / 网页美化开关 / 客户端过滤 / Strm 重定向
- **API 密钥不再明文显示**：后端渲染时就不下发真值（不是视觉遮挡，F12 也看不到），要改用「更改」按钮重填
- 运行状态徽标、一键「打开媒体服务器」、日志在线查看（自动轮转）、「原始 YAML」高级编辑
- **配置自动备份**：每次保存留一份 `config.yaml.bak.<时间戳>`，只保留最近 5 份，随时回档
- 面板密码（PBKDF2-SHA256 12 万轮哈希，不存明文）；**改密码后强制重新登录**；
  **会话落盘持久化**——重启/重建容器不必重新登录

**容器已内置的加固**
- 时区跟随宿主机（`TZ` + 挂 `/etc/localtime`）
- 内存上限默认 512 MiB（0.2.x 的内存缓存需要；实测 256 MiB 会在浏览/播放时被系统 OOM 杀掉）、swap 与上限一致
- `HEALTHCHECK`（`docker ps` 能看到 `(healthy)`）
- MediaWarp 日志超过 5 MB 自动轮转（`.1/.2/.3`）
- **MediaWarp 进程守护（看门狗）**：MediaWarp 万一崩溃/被系统 OOM 杀掉，面板会在 ~30 秒内自动把它拉起来，
  并把「已自动重启」写进「日志」（以前挂了只会显示「已停止」，得手动保存配置或重启容器才能恢复）
- **多架构镜像**：`linux/amd64` + `linux/arm64`（构建时按架构拉对应的官方二进制）

---

## 快速开始

### 方式一：用现成镜像（推荐，复制粘贴即可）

**镜像地址（点这里直达 Docker Hub）**：<https://hub.docker.com/r/xiaoyu96/mediawarp-panel>

| | |
|---|---|
| 镜像（Docker Hub） | `xiaoyu96/mediawarp-panel:latest` |
| 镜像（GHCR，备用） | `ghcr.io/yq487900/mediawarp-panel:latest` |
| 可用 tag | `latest`（最新版）/ `1.0.1`（当前版本号）——两者是同一个镜像 |
| 架构 | `linux/amd64`、`linux/arm64`（x86 服务器 / 群晖、树莓派等 arm 设备都能跑） |
| 端口 | 容器内 `9000`＝MediaWarp 本体、`9009`＝设置面板（下面示例映射成 `9002` / `9003`） |
| 手动拉取 | `docker pull xiaoyu96/mediawarp-panel:latest` |

**1️⃣ 建目录，把下面这段存成 `docker-compose.yml`**

```bash
mkdir -p mediawarp && cd mediawarp
```

```yaml
services:
  mediawarp:
    image: xiaoyu96/mediawarp-panel:latest   # 想用 GHCR 就换成 ghcr.io/yq487900/mediawarp-panel:latest
    container_name: mediawarp
    restart: unless-stopped
    ports:
      - "9002:9000"        # MediaWarp 本体（宿主端口被占就改左边）
      - "9003:9009"        # 设置面板
    environment:
      - TZ=Asia/Shanghai
      # - MW_PUBLIC_PORT=9002     # 「打开媒体服务器」按钮的对外端口（留空=自动探测）
      # - SITE_NAME=mediawarp     # 站点名：显示在页面标题/页头（不留则通用标题）
      # - CONTAINER_NAME=mediawarp # 排障提示里 docker logs/exec 用的容器名
    volumes:
      - ./data:/app                           # 配置 / 日志 / 面板密码 / 会话都在这里，别删
      - /etc/localtime:/etc/localtime:ro       # 时区跟随宿主机
    mem_limit: 512m          # 0.2.x 带图片/字幕内存缓存，512m 稳妥（256m 实测会被 OOM 杀掉）
    memswap_limit: 512m
```

**2️⃣ 启动**

```bash
docker compose up -d
docker logs mediawarp 2>&1 | grep 初始密码      # 拿首次登录密码
```

**3️⃣ 打开面板（首次登录）**

浏览器打开 **`http://<主机IP>:9003`** → 用上面的初始密码登录（会要求你重设密码）→
在「上游媒体服务器」里填 Emby/Jellyfin 地址与 API 密钥即可。

> ℹ️ **还没填上游之前，`:9002`（媒体服务器入口）会返回 502 —— 这是正常的**：
> MediaWarp 此时没有可代理的上游，只能报错。填完上游地址 + API 密钥（点保存即生效）后就能正常打开了。
> 实测：全新部署时容器 10 秒内 healthy、面板立即可用、日志会打印初始密码，自动生成的就是 0.2.x 格式配置。

**更新到最新版**：

```bash
docker compose pull && docker compose up -d      # ./data 不动，配置和登录状态都保留
```

> 不用 compose 的话，等价的单条命令：
>
> ```bash
> docker run -d --name mediawarp --restart unless-stopped \
>   -p 9002:9000 -p 9003:9009 \
>   -e TZ=Asia/Shanghai \
>   -v "$PWD/data:/app" -v /etc/localtime:/etc/localtime:ro \
>   --memory 512m --memory-swap 512m \
>   xiaoyu96/mediawarp-panel:latest
> ```

### 方式二：本地构建（想自己改代码 / 换 MediaWarp 版本）

```bash
git clone https://github.com/yq487900/mediawarp-panel.git   # 换成你 fork 的地址即可
cd mediawarp-panel
docker compose -f docker-compose.build.yml up -d --build

# 换 MediaWarp 版本：
docker build --network=host --build-arg MEDIAWARP_VERSION=0.2.4 -t mediawarp-panel:local .
```

### 方式三：离线构建（用你自己手上的 MediaWarp 二进制）

仓库里还有 `Dockerfile.local`：**完全不联网下载** MediaWarp，运行时直接使用数据目录里的
`/app/MediaWarp`。适合内网 / fake-ip DNS 环境，或想固定用某个官方没发过 release 的版本。

```bash
git clone https://github.com/yq487900/mediawarp-panel.git
cd mediawarp-panel
mkdir -p data && cp /你的路径/MediaWarp data/     # 放入你现成的二进制
docker build -f Dockerfile.local -t mediawarp-panel:local .
docker run -d --name mediawarp -p 9002:9000 -p 9003:9009 \
  -v "$PWD/data":/app -e TZ=Asia/Shanghai mediawarp-panel:local
```

> ⚠️ **本地构建必须能直连 GitHub**（要下载 MediaWarp 发行包）。
> 如果你的网络用了 **fake-ip DNS**（旁路由 / Clash 类代理很常见：域名被解析成 `198.x.x.x` 之类的假地址），
> Docker 默认的 bridge 网络会**解析到假 IP 而下载失败**——用 `--network=host` 构建即可
> （`docker-compose.build.yml` 里已经写好了 `network: host`）。
> 用 GitHub Actions 构建镜像则完全没有这个问题。

---

## 端口与数据

| 项 | 容器内 | 示例宿主映射 | 用途 |
|---|---|---|---|
| MediaWarp 本体 | `9000` | `9002` | 媒体服务器网页端（剧照墙等美化在这里生效）|
| 设置面板 | `9009` | `9003` | 改设置、看日志的地方 |

数据全部落在挂载目录（默认 `./data`）：

```
data/
├── config/config.yaml          # MediaWarp 配置（面板负责写）
├── config/config.yaml.bak.*    # 自动备份，保留最近 5 份
├── logs/mediawarp.out          # 运行日志（>5MB 自动轮转）
├── logs/<日期>/access.log      # 访问日志
├── ui_state.json               # 面板密码（哈希+salt）
└── sessions.json               # 面板会话（重启不掉登录）
```

---

## MediaWarp 版本与配置格式（重要）

镜像在**构建时**从官方 Release 下载 MediaWarp（默认 `0.2.4`，可用 `--build-arg MEDIAWARP_VERSION=x.y.z` 换别的版本）。

面板**自动识别并保持配置格式**，所以新旧版本都能用：

| 格式 | 对应版本 | 字段风格 |
|---|---|---|
| `old` | 0.1.x | `Port:` / `MediaServer:` / `Web:` / `ClientFilter:` / `HTTPStrm:` |
| `new` | 0.2.x | `port:` / `server:` / `web:` / `client:` / `http_strm:` / `cache:`（多了缓存等配置）|

- **全新安装**：面板自动生成 **new 格式**（配官方 0.2.x），开箱可用。
- **从旧版（0.1.x）迁移**：把旧的 `config.yaml` 放进 `./data/config/` 再启动即可 ——
  面板检测到旧格式后会**继续按旧格式读写**，不会把你的配置改乱。
- 想升级到 0.2.x：换 `MEDIAWARP_VERSION` 重新构建，并在面板「原始 YAML」里把字段名对照上表改一下即可。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `TZ` | `Asia/Shanghai` | 容器时区 |
| `MW_BIN` | `/opt/mediawarp/MediaWarp` | MediaWarp 二进制路径（镜像内已内置） |
| `UI_PORT` | `9009` | 面板监听端口 |
| `MW_PORT` | `9000` | MediaWarp 监听端口（默认与 `config.yaml` 的 `Port` 一致） |
| `MW_PUBLIC_PORT` | 空 | 「打开媒体服务器」按钮用的对外端口；留空则自动探测 |
| `SITE_NAME` | 空 | 站点名，显示在浏览器标题 / 页头 / 登录页（如 `SITE_NAME=emby-av`）；留空则显示通用标题 |
| `CONTAINER_NAME` | `mediawarp` | 页面排障提示里 `docker logs` / `docker exec` 示例用的容器名 |
| `MW_CFG_FMT` | `new` | 首次生成配置用哪种格式：`new`=0.2.x / `old`=0.1.x（已有配置时以文件实际格式为准）|

---

## 忘记面板密码

```bash
docker exec mediawarp python3 /opt/ui/reset_pw.py
```

会立即打印一个新的初始密码（原状态备份为 `ui_state.json.prereset.<时间>`，可回退；登录后仍会要求重设）。

---

## 镜像发布（GHCR + Docker Hub）

仓库自带 GitHub Actions（`.github/workflows/docker-publish.yml`）：推到 `main`、或打 `v*` tag 时，
自动构建 `linux/amd64` + `linux/arm64` 并**同时推送两处**：

| 位置 | 镜像名 | 说明 |
|---|---|---|
| **Docker Hub** | `xiaoyu96/mediawarp-panel` | <https://hub.docker.com/r/xiaoyu96/mediawarp-panel> |
| GHCR | `ghcr.io/yq487900/mediawarp-panel` | 公开仓库的包可匿名拉取 |

**tag 规则（只有两个）**：

| tag | 什么时候出现 | 指向 |
|---|---|---|
| `latest` | 每次推 `main` 或打 `v*` tag | 最新一次发布 |
| `1.0.0` 这样的版本号 | 只在打 `v*` tag 时 | 与 `latest` **同一个镜像**（同一 digest） |

发新版：

```bash
git tag v1.1.0 && git push origin v1.1.0     # 构建出 1.1.0 + latest（同一个镜像）
```

> 纯文档改动（`**.md` / `LICENSE` / `NOTICE`）不触发构建，省 CI 时间、也不会产生无意义的新镜像。

**要发到自己的 Docker Hub（fork 后）**：Settings → Secrets and variables → Actions 里加两个密钥
`DOCKERHUB_USERNAME`、`DOCKERHUB_TOKEN`（Token 权限要 **Read & Write**）；没配就只推 GHCR，不会报红。

**GHCR 拉取权限**：仓库是 Public 时，GHCR 生成的包通常**会自动继承为 Public**，
无需额外操作（本仓库的包实测无登录即可匿名拉取：`docker pull ghcr.io/yq487900/mediawarp-panel:latest`）。

若你的包仍是 Private，到 GitHub → 仓库右侧 **Packages** → 打开该包 → **Package settings**
→ 把可见性改成 **Public**，这样任何人都能直接 `docker compose up -d` 拉取。

> 想保持私有也行：拉取机器先 `echo <PAT> | docker login ghcr.io -u <用户名> --password-stdin`。

想手动改 MediaWarp 版本重跑：仓库页面 → **Actions** → *Build and push image* → **Run workflow** → 填版本号。

---

## 在 FnOS / 飞牛OS 上的注意事项

- 宿主 `9000` 常被 MoviePilot 等占用，所以对外端口默认用 `9002`（改你自己 compose 里左侧的端口即可）
- 宿主的 `:5666` 桌面里可以用「Docker」应用直接粘贴上面的 compose 创建项目
- 时区同步：挂 `/etc/localtime`（compose 里已有）

---

## 常见问题（FAQ）

| 现象 | 原因与处理 |
|---|---|
| 网页一直转圈，浏览器标签显示「每日推荐」，页面只显示出服务器名 | **重复注入了美化脚本**。MediaWarp 0.2.x 在 `web.crx` / `web.actor_plus` / `web.fanart_show` 打开时**自己就会注入**这些脚本（`/MediaWarp/static/emby-web-mod/…`、`/MediaWarp/static/emby-crx/…`）。如果你又在「原始 YAML」的 `web.head` 里写了 `/MediaWarp/custom/emby-front-end-mod/*.js`（官方 `config.yaml.example` 里的那段），同一脚本会被加载两遍 → 前端死循环。**把 `web.head` 留空、并删掉 `custom/emby-front-end-mod/` 即可**。判断口诀：服务端访问日志全是 200，但页面转圈 = 前端 JS 的问题；`curl http://<IP>:9002/web/index.html \| grep '<script src='` 数一下有没有重复。 |
| `docker ps` 显示 `(healthy)`，但 AV 网页打不开、面板显示「已停止」 | MediaWarp 进程崩了或**被系统 OOM 杀掉**（`docker inspect <容器> --format '{{.State.OOMKilled}}'` 会是 true；`dmesg -T \| grep -i "killed process"` 有记录）。0.2.x 带图片/字幕内存缓存，请把 `mem_limit` 设为 **512m 或更高**（256m 实测会被杀）。**v1.0.1 起面板自带看门狗**，异常约 30 秒后会自动拉起，并在「日志」里留一条记录。 |
| 改了「原始 YAML」/ 表单后怎么生效 | 点「保存并立即生效」：面板会重启 MediaWarp 进程（约 0.2 秒）并确认端口已监听，失败会自动重试一次。 |
| 刚部署完，`:9002` 打不开 / 返回 502 | **正常**：还没配上游。打开 `:9003` 面板 → 填「上游媒体服务器」地址 + API 密钥 → 保存即生效。 |
| 上游换成别的 Emby/Jellyfin | 在「上游媒体服务器」卡片里改地址 + API 密钥 → 保存即生效。 |
| 忘记面板密码 | 宿主机上 `docker exec <容器名> python3 /opt/ui/reset_pw.py`，会打印一个新的初始密码。 |

---

## 许可与致谢

- **本面板代码**（`app.py` / `reset_pw.py` / `Dockerfile` / compose 等）：MIT，见 [LICENSE](LICENSE)
- **[MediaWarp](https://github.com/AkimioJR/MediaWarp)**：版权归 **AkimioJR**，
  [AGPL-3.0 修改版](https://github.com/AkimioJR/MediaWarp/blob/main/LICENSE)
  —— 禁止商用；使用其代码须开源并注明出处。本项目的全部灵感与上游能力都来自它，特此致谢。
  详细声明见 [NOTICE](NOTICE)。
