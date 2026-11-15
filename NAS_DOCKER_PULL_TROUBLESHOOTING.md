# Synology NAS Docker `pull` / BuildKit 构建与代理排查报告

> 适用环境：DSM 7.2 的 **Container Manager**。本文记录本机于 2026-11-15 遇到的 Docker 镜像拉取、BuildKit 构建与构建下载代理故障、根因、修复步骤与回滚方法。
>
> 文中的命令均不包含 NAS 密码、Cookie、代理凭据或任何其他敏感数据。

## 一、现象

在 Container Manager 中构建镜像时，Docker BuildKit 无法拉取基础镜像：

```text
failed to solve: ubuntu:24.04: failed to do request:
Head "https://<mirror>/v2/library/ubuntu/manifests/24.04?ns=docker.io":
http: server gave HTTP response to HTTPS client
```

移除错误镜像源后，直接访问 Docker Hub 仍可能失败：

```text
Error response from daemon: Get "https://registry-1.docker.io/v2/":
net/http: request canceled while waiting for connection
(Client.Timeout exceeded while awaiting headers)
```

这两类问题都会导致：

- `docker pull ubuntu:24.04` 失败；
- `docker compose build` / `docker compose up --build` 失败；
- Dockerfile 顶部使用 `# syntax=docker/dockerfile:...` 时，BuildKit 还会在拉取基础镜像之前失败，因为它也需要下载 Dockerfile 前端镜像。

## 二、原因

### 1. HTTP 镜像源被 BuildKit 按 HTTPS 使用

Container Manager 的 Docker 守护进程配置文件包含类似配置：

```json
{
  "registry-mirrors": ["http://example-mirror:5000"],
  "insecure-registries": ["example-mirror:5000"]
}
```

该镜像站只提供 HTTP，而 Docker / BuildKit 对镜像拉取走 HTTPS 请求。即使设置了 `insecure-registries`，在某些 Docker / BuildKit 版本组合中，镜像加速器请求仍可能被按 HTTPS 发出，最终得到「HTTP response to HTTPS client」。

### 2. NAS 到 Docker Hub 直连超时

删除 HTTP 镜像源后，Docker 会回退访问 `https://registry-1.docker.io`。如果 NAS 所在网络到 Docker Hub 的连接不稳定、受限或速度过慢，就会出现等待响应头超时。

因此可用的长期方案不是恢复 HTTP 镜像站，而是配置一个**已从该 NAS 实测可用的 HTTPS Docker Registry 镜像**。

## 三、本次环境中的配置位置

DSM Container Manager 的 Docker 守护进程配置位于：

```text
/var/packages/ContainerManager/etc/dockerd.json
```

实际文件通常是指向以下目录的符号链接：

```text
/volume2/@appconf/ContainerManager/dockerd.json
```

注意：该文件在本次 NAS 上带有 immutable 属性：

```bash
sudo lsattr /var/packages/ContainerManager/etc/dockerd.json
# ----i------------- .../dockerd.json
```

所以即使使用 `sudo cp` 或 `sudo tee`，也会出现 `Permission denied`。修改前必须暂时解除 immutable 属性。

> **重要更正：不要在修改完成后重新加回 immutable 属性。** Container Manager 启动时会由 DSM 的 updater 写入此文件；如果执行 `chattr +i`，服务会报 `Failed to save ... dockerd.json, Operation not permitted`，并无法完成启动。配置应保留为 `root:root`、权限 `600`，但不带 immutable 属性。

## 四、安全修复步骤

> **影响提示：** 重启 Container Manager 会让当前容器短暂中断。先确认现有容器的重启策略，并选择维护窗口执行。

### 1. 检查当前运行的容器

```bash
sudo /var/packages/ContainerManager/target/usr/bin/docker ps
```

### 2. 备份并检查当前配置

```bash
CONFIG=/var/packages/ContainerManager/etc/dockerd.json
sudo cp "$CONFIG" "${CONFIG}.bak-$(date +%Y%m%d-%H%M%S)"
sudo cat "$CONFIG"
sudo lsattr "$CONFIG"
```

保留备份文件，便于随时回滚。

### 3. 先测试候选 HTTPS 镜像

不要未经测试直接写入全局 Docker 配置。下面命令返回 `200` 或 `401` 都可能是成功：Registry v2 在未认证访问时经常正常返回 `401`。

```bash
curl -sS -o /dev/null -w '%{http_code} %{time_total}\n' \
  --max-time 15 https://docker.1ms.run/v2/
```

本次 NAS 上该地址返回了正常的 Registry v2 响应，响应速度也符合预期。镜像服务是否可用会随网络和服务运营状态变化；如果未来失效，应重新选择并测试其他可信的 **HTTPS** 镜像源。

### 4. 写入 HTTPS 镜像配置

本次保留 NAS 原有的数据目录、日志驱动、seccomp 和存储驱动，仅调整镜像源：

```json
{
  "data-root": "/var/packages/ContainerManager/var/docker",
  "registry-mirrors": ["https://docker.1ms.run"],
  "log-driver": "db",
  "seccomp-profile": "unconfined",
  "storage-driver": "btrfs"
}
```

推荐先将上述 JSON 保存到 NAS 的临时文件，例如 `/tmp/dockerd.json`。然后原子地替换配置：

```bash
CONFIG=/var/packages/ContainerManager/etc/dockerd.json

sudo chattr -i "$CONFIG"
sudo cp /tmp/dockerd.json "$CONFIG"
sudo chown root:root "$CONFIG"
sudo chmod 600 "$CONFIG"
# 不要执行 chattr +i：Container Manager 启动时需要写入该文件。

sudo cat "$CONFIG"
sudo lsattr "$CONFIG"
```

**不要**再把只有 HTTP 的地址写入 `registry-mirrors`；也不应为正常 HTTPS 镜像添加 `insecure-registries`。

### 5. 重启 Container Manager

```bash
sudo /usr/syno/bin/synopkg stop ContainerManager
sudo /usr/syno/bin/synopkg start ContainerManager
```

等待十几秒后确认引擎及镜像源：

```bash
DOCKER=/var/packages/ContainerManager/target/usr/bin/docker
sudo "$DOCKER" version
sudo "$DOCKER" info --format '{{json .RegistryConfig.Mirrors}}'
sudo "$DOCKER" ps --format '{{.Names}} {{.Status}}'
```

预期 `RegistryConfig.Mirrors` 输出：

```json
["https://docker.1ms.run/"]
```

## 五、最终验证

先验证普通镜像拉取：

```bash
DOCKER=/var/packages/ContainerManager/target/usr/bin/docker
sudo "$DOCKER" pull ubuntu:24.04
sudo "$DOCKER" image inspect ubuntu:24.04 >/dev/null && echo 'ubuntu pull OK'
```

再验证 BuildKit（比普通 `pull` 更贴近实际构建）：

```bash
mkdir -p /tmp/docker-buildkit-test
cat >/tmp/docker-buildkit-test/Dockerfile <<'EOF'
FROM ubuntu:24.04
RUN echo buildkit-ok
EOF

sudo "$DOCKER" build /tmp/docker-buildkit-test -t local/buildkit-test:latest
```

这两步均通过，才说明 Docker 镜像拉取和 BuildKit 构建链路都恢复正常。

## 六、本次验证结果

配置生效后，Docker 已报告正在使用以下 HTTPS 镜像源：

```json
["https://docker.1ms.run/"]
```

并且已经在该 NAS 上成功完成：

```text
$ docker pull ubuntu:24.04
Status: Downloaded newer image for ubuntu:24.04
```

拉取层的校验和验证也已成功完成。这确认了原先的 HTTP/HTTPS 协议不匹配和 Docker Hub 直连超时问题，均已由 HTTPS 镜像源配置解决。

完整项目镜像仍应在每次更新 Dockerfile 或依赖后，用第五节的 BuildKit 命令或 `docker compose build` 做一次验证。

## 七、容器 Build 阶段使用 NAS 本地代理

### 为什么 Registry 镜像源不能解决所有构建下载

`registry-mirrors` 只会加速 Docker 镜像层，例如 `ubuntu:24.04`。Dockerfile 中后续执行的下载仍由构建容器直接访问网络，包括：

- `apt-get` 下载 Ubuntu 软件包；
- `pip install` 下载 PyPI 依赖；
- `python -m camoufox fetch` 下载浏览器二进制；
- 任何从 GitHub 或其他公网地址下载的构建依赖。

本次 NAS 的实测结果显示：经本地代理访问 PyPI 和 GitHub 明显快于直连；而直连 PyPI 曾在传输中超时。因此应将代理限制在**镜像构建阶段**，不要让应用运行时继承它。

### `127.0.0.1` 在容器内不是 NAS

若 NAS 上的代理监听在：

```text
127.0.0.1:5890
```

则 Docker 构建容器中的 `127.0.0.1` 指向构建容器自身，并不指向 NAS 主机。当前 NAS Docker bridge 网关是：

```text
172.17.0.1
```

因此构建容器应使用：

```text
http://172.17.0.1:5890
```

先确认 NAS 本机代理可用：

```bash
curl -sS -o /dev/null -w 'HTTP %{http_code}, total %{time_total}s\n' \
  --max-time 20 -x http://127.0.0.1:5890 https://pypi.org/simple/
```

再确认 Docker bridge 内可以到达该代理：

```bash
DOCKER=/var/packages/ContainerManager/target/usr/bin/docker
sudo "$DOCKER" run --rm ubuntu:24.04 \
  bash -c 'exec 3<>/dev/tcp/172.17.0.1/5890' \
  && echo 'build-container-proxy-reachable'
```

### Compose 中传入构建专用代理

推荐使用 Compose 的 `build.args`，而不是在 Dockerfile 中写死代理。当前项目的 `compose.yaml` 使用如下模式：

```yaml
services:
  glados-checkin:
    build:
      args:
        HTTP_PROXY: ${GLADOS_BUILD_PROXY:-http://172.17.0.1:5890}
        HTTPS_PROXY: ${GLADOS_BUILD_PROXY:-http://172.17.0.1:5890}
        NO_PROXY: localhost,127.0.0.1,::1,host.docker.internal
```

Dockerfile 对需要下载的 `RUN` 层临时导出这些变量：

```dockerfile
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY

RUN export http_proxy="$HTTP_PROXY" https_proxy="$HTTPS_PROXY" no_proxy="$NO_PROXY"; \
    apt-get update && apt-get install -y --no-install-recommends ...
```

这会使 APT、pip 和 Camoufox 下载走代理，但不会把 `HTTP_PROXY` / `HTTPS_PROXY` 作为最终镜像的 `ENV` 保存。因此，运行中的 GLaDOS 浏览器不会意外走构建代理，仍由每个账号的专属/公共代理配置控制。

### 修改构建代理

在项目目录创建或修改 `.env`：

```bash
cd /volume2/docker/glados_chein
cat >.env <<'EOF'
GLADOS_BUILD_PROXY=http://172.17.0.1:5890
EOF
```

如果代理服务不在 Docker bridge 网关，或端口发生变化，只需要更新该变量后重新构建：

```bash
DOCKER=/var/packages/ContainerManager/target/usr/bin/docker
sudo "$DOCKER" compose -p glados_chein build --progress=plain
```

`.env` 可能包含代理认证信息，不应提交 Git，也不应把内容粘贴到公开日志。

### 实时查看构建并保留日志

```bash
cd /volume2/docker/glados_chein
DOCKER=/var/packages/ContainerManager/target/usr/bin/docker

sudo "$DOCKER" compose -p glados_chein build --progress=plain 2>&1 \
  | tee "build-$(date +%Y%m%d-%H%M%S).log"
```

另开一个 SSH 窗口查看：

```bash
tail -f /volume2/docker/glados_chein/build-*.log
```

构建完成后再启动：

```bash
sudo "$DOCKER" compose -p glados_chein up -d
sudo "$DOCKER" compose -p glados_chein logs -f --tail=100
```

## 八、回滚方法

如果新 HTTPS 镜像出现故障，可以恢复备份：

```bash
CONFIG=/var/packages/ContainerManager/etc/dockerd.json
BACKUP=/var/packages/ContainerManager/etc/dockerd.json.bak-<时间戳>

sudo chattr -i "$CONFIG"
sudo cp "$BACKUP" "$CONFIG"
sudo chown root:root "$CONFIG"
sudo chmod 600 "$CONFIG"
# 不要执行 chattr +i：DSM 的 Container Manager 需要写回此配置文件。

sudo /usr/syno/bin/synopkg stop ContainerManager
sudo /usr/syno/bin/synopkg start ContainerManager
```

如果原备份中包含 HTTP 镜像源，回滚只适合用于临时恢复原设置，并不能解决 BuildKit 的 HTTPS/HTTP 协议不匹配；建议优先替换为另一个经过实测的 HTTPS Registry v2 镜像。

## 九、日后快速检查清单

当 NAS 再出现 `docker pull` 或 `docker compose build` 失败时，按顺序检查：

1. `sudo docker info --format '{{json .RegistryConfig.Mirrors}}'`：确认镜像源是否为 HTTPS；
2. `curl -I --max-time 15 <镜像源>/v2/`：确认 NAS 能连通镜像服务；
3. `sudo docker pull ubuntu:24.04`：确认普通拉取；
4. `sudo docker build ...`：确认 BuildKit；
5. 检查 `/var/packages/ContainerManager/etc/dockerd.json` 是否被 DSM 更新或 Container Manager 设置修改；
6. 修改配置文件前检查 `lsattr`，必要时执行 `chattr -i`；完成后保持 `root:root` 和 `600` 权限，但**不要**执行 `chattr +i`；
7. 对 APT、PyPI、GitHub 或浏览器下载慢的问题，测试 `127.0.0.1:代理端口` 后，将构建代理配置为 Docker bridge 网关地址（当前为 `172.17.0.1:5890`）；
8. 每次修改都先创建带时间戳的备份，并在重启 Container Manager 后确认业务容器已恢复。
