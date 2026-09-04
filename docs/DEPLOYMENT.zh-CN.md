# RK3576 私有云盘完整部署指南

本文从空白 Debian 主机开始，复现一套同时支持 Windows 网络驱动器和公网 Web 的私有云盘。示例环境是 RK3576（Debian 12 ARM64）、一块 NVMe SSD、两块 U 盘、一台 Debian 公网服务器和一台 Windows 电脑。

最终有两条访问链路：

```text
私有文件协议：Windows -> Tailscale/Headscale -> RK -> Samba -> SSD/USB/USB2
公网网页：浏览器 -> HTTPS -> 公网 Nginx -> FRP -> RK Nginx
                    -> 单 Key 鉴权 -> FileBrowser -> SSD/USB/USB2
```

公网服务器只负责 Headscale 控制、TLS 终止和流量转发，不保存云盘文件。SSD 与两块 U 盘是三个独立文件系统；Samba 将它们分别共享为 Windows `Z:`、`Y:`、`X:`，FileBrowser 也显示为三个 source，因此两端都能报告各自容量。

## 1. 部署前准备

准备以下资源：

- RK3576 或其他 ARM64 Linux 主机，建议 4 GB 以上内存；
- 一块 SSD 和两块 U 盘；
- 一台有公网 IPv4 的 Debian 12/Ubuntu 服务器；
- 两个解析到公网服务器的域名；
- Windows 10/11 客户端；
- root 或 sudo 权限。

本文统一使用这些示例值：

| 项目 | 示例值 |
| --- | --- |
| 云盘域名 | `cloud.example.com` |
| Headscale 域名 | `headscale.example.com` |
| MagicDNS 域 | `tail.example.com` |
| 公网服务器 IP | `203.0.113.10` |
| Headscale 用户 | `cloud` |
| RK Tailscale 名称 | `rk3576-cloud` |
| RK Tailscale IP | `<RK_TAILSCALE_IP>` |
| SSD UUID | `<SSD_UUID>` |
| U 盘 UUID | `<USB_UUID>` |
| 第二块 U 盘 UUID | `<USB2_UUID>` |

先克隆仓库：

```sh
git clone https://github.com/huangzy-hub/cloud.git
cd cloud
```

不要直接复制示例值上线。真实的 FRP token、Samba 密码、Web Key、预认证 Key 和 SSH 私钥都只能保存在目标机器上。

## 2. 检查系统与设备

在 RK 上执行：

```sh
cat /etc/os-release
uname -a
uname -m
free -h
lsblk -o NAME,MODEL,SIZE,TYPE,FSTYPE,MOUNTPOINTS,TRAN
ip -br address
ip route
```

确认系统是 `aarch64`，并通过 `MODEL`、`SIZE` 和 `TRAN` 分清 eMMC、NVMe 和 U 盘。后续示例假设 SSD 是 `/dev/nvme0n1`、U 盘是 `/dev/sda`；你的名称可能不同。

如果 RK 尚未联网，可先用 NetworkManager 连接普通 WPA2 热点：

```sh
nmcli device wifi list
nmcli device wifi connect '<SSID>' password '<WIFI_PASSWORD>' ifname wlan0
ip -br address show wlan0
ping -c 3 1.1.1.1
```

校园网若使用 802.1X，必须根据学校给出的 EAP 类型、匿名身份、CA 证书和二阶段认证配置，不能只凭账号密码猜测。建议先用普通热点完成软件部署，再单独迁移网络配置。

## 3. 初始化并挂载 SSD 与 U 盘

> **危险：本节的 `wipefs`、`parted` 和 `mkfs.ext4` 会清空选定磁盘。** 先用 `lsblk`、`blkid`、`findmnt` 三次确认设备名。不要对系统所在的 `mmcblk0` 操作。如果要保留旧数据，跳过格式化并先做备份。

停止可能正在使用目录的服务：

```sh
systemctl stop filebrowser-quantum smbd 2>/dev/null || true
findmnt /dev/nvme0n1 /dev/sda || true
```

确认目标后，为两块盘各建一个 GPT 分区和 ext4 文件系统：

```sh
wipefs -a /dev/nvme0n1
parted -s /dev/nvme0n1 mklabel gpt mkpart primary ext4 0% 100%
mkfs.ext4 -L RK_SSD /dev/nvme0n1p1

wipefs -a /dev/sda
parted -s /dev/sda mklabel gpt mkpart primary ext4 0% 100%
mkfs.ext4 -L RK_USB /dev/sda1
```

读取 UUID，并记录输出：

```sh
blkid /dev/nvme0n1p1 /dev/sda1
```

创建专用用户和挂载目录：

```sh
getent group cloud >/dev/null || groupadd --system cloud
id cloud >/dev/null 2>&1 || useradd --system --gid cloud \
  --home-dir /nonexistent --shell /usr/sbin/nologin cloud
install -d -m 0770 -o cloud -g cloud /srv/cloud/SSD /srv/cloud/USB
```

在 `/etc/fstab` 末尾添加，替换两个 UUID：

```fstab
UUID=<SSD_UUID> /srv/cloud/SSD ext4 defaults,noatime,nofail,x-systemd.device-timeout=10s 0 2
UUID=<USB_UUID> /srv/cloud/USB ext4 defaults,noatime,nofail,x-systemd.device-timeout=10s 0 2
```

如需加入第二块保持 exFAT 格式的 U 盘，可单独创建安全挂载点。先用 `id cloud` 查出实际 UID/GID，再替换下例中的数值：

```sh
install -d -m 0000 -o root -g root /srv/cloud/USB2
echo 'UUID=<USB2_UUID> /srv/cloud/USB2 exfat defaults,noatime,nosuid,nodev,nofail,x-systemd.automount,x-systemd.device-timeout=10s,uid=<CLOUD_UID>,gid=<CLOUD_GID>,fmask=0007,dmask=0007 0 0' >> /etc/fstab
systemctl daemon-reload
mount /srv/cloud/USB2
findmnt /srv/cloud/USB2
```

未挂载时将目录权限保持为 `0000`，可以防止 FileBrowser 或 Samba 意外把 USB2 文件写进 eMMC。exFAT 不保存 Linux 属主和权限，所以通过挂载参数将所有文件映射给 `cloud` 用户。

挂载并设置根目录权限：

```sh
systemctl daemon-reload
mount -a
chown cloud:cloud /srv/cloud/SSD /srv/cloud/USB
chmod 0770 /srv/cloud/SSD /srv/cloud/USB
findmnt /srv/cloud/SSD
findmnt /srv/cloud/USB
df -hT /srv/cloud/SSD /srv/cloud/USB /srv/cloud/USB2
```

Linux 看到块设备不等于可以直接放文件。挂载把文件系统的根目录接入系统目录树；不挂载时向 `/srv/cloud/SSD` 写入的数据会落到 eMMC 上的同名普通目录，所以每次开机都要检查 `findmnt`。

## 4. 在公网服务器部署 Headscale

先将 `headscale.example.com` 的 A 记录指向公网服务器。Headscale 0.29.3 要求 Tailscale 客户端至少为 1.80.0。

Debian 12 推荐安装官方 DEB：

```sh
HEADSCALE_VERSION=0.29.3
wget -O /tmp/headscale.deb \
  "https://github.com/juanfont/headscale/releases/download/v${HEADSCALE_VERSION}/headscale_${HEADSCALE_VERSION}_linux_amd64.deb"
echo '14eccf8d41fd93927ad3fe1fd9e49dbe14d5668a244c4205d940967c442aea0d  /tmp/headscale.deb' | sha256sum -c -
apt update
apt install -y /tmp/headscale.deb nginx certbot
```

ARM64 公网服务器应把文件名改为 `linux_arm64.deb`，并使用该发布页公布的对应校验值。

复制示例配置并替换域名和公网 IP：

```sh
install -d -m 0755 /etc/headscale
install -m 0644 headscale/config.yaml.example /etc/headscale/config.yaml
install -m 0644 headscale/policy.hujson.example /etc/headscale/policy.hujson
sed -i \
  -e 's/headscale\.example\.com/你的Headscale域名/g' \
  -e 's/tail\.example\.com/你的MagicDNS域/g' \
  -e 's/203\.0\.113\.10/你的公网IP/g' \
  /etc/headscale/config.yaml
systemctl enable --now headscale
systemctl status --no-pager headscale
curl -fsS http://127.0.0.1:8080/health
```

示例策略只允许 `cloud@` 用户拥有的设备互通。Headscale 的用户名 `cloud` 在策略中写作 `cloud@`。

### 配置 Headscale HTTPS 反向代理

先申请证书。80 端口需要能从公网访问：

```sh
certbot certonly --standalone -d headscale.example.com \
  --non-interactive --agree-tos --register-unsafely-without-email
```

复制 `headscale/nginx.conf.example` 到 Nginx 站点配置，替换域名后启用：

```sh
install -m 0644 headscale/nginx.conf.example \
  /etc/nginx/sites-available/headscale.conf
sed -i 's/headscale\.example\.com/你的Headscale域名/g' \
  /etc/nginx/sites-available/headscale.conf
ln -s /etc/nginx/sites-available/headscale.conf \
  /etc/nginx/sites-enabled/headscale.conf
nginx -t
systemctl reload nginx
curl -fsS https://headscale.example.com/health
```

Nginx 必须透传自定义的 `tailscale-control-protocol` WebSocket Upgrade；不要把 Headscale 域名套在不支持 WebSocket POST 的 CDN 代理后面。自建 DERP 的 STUN 还需要公网 UDP 3478。

创建用户：

```sh
headscale users create cloud
headscale users list
```

## 5. 让 RK 和 Windows 加入自建 Tailnet

### RK

安装 Tailscale：

```sh
curl -fsSL https://tailscale.com/install.sh | sh
systemctl enable --now tailscaled
```

在 Headscale 服务器创建一个一小时、单次使用的预认证 Key：

```sh
headscale preauthkeys create --user cloud --expiration 1h --reusable=false
```

立即在 RK 使用它，之后不要再保存明文：

```sh
tailscale up \
  --login-server=https://headscale.example.com \
  --authkey='<HEADSCALE_PREAUTH_KEY>' \
  --hostname=rk3576-cloud
tailscale status
tailscale ip -4
```

记下 `tailscale ip -4` 输出，后文称为 `<RK_TAILSCALE_IP>`。

### Windows

安装官方 Tailscale Windows 客户端。在 Headscale 服务器再创建一个单次 Key，然后以管理员身份打开 PowerShell：

```powershell
& "$env:ProgramFiles\Tailscale\tailscale.exe" up `
  --login-server=https://headscale.example.com `
  --authkey='<ANOTHER_PREAUTH_KEY>' `
  --hostname='windows-client'

& "$env:ProgramFiles\Tailscale\tailscale.exe" status
ping <RK_TAILSCALE_IP>
```

预认证 Key 只负责第一次注册。注册后设备使用自己的节点密钥，所以重启或换网络时不需要再次输入账号；只有节点过期、注销、状态丢失或被管理员删除时才需重新注册。

Tailscale 会优先尝试 NAT 穿透形成点对点 WireGuard 链路；校园网限制较严时会退回 DERP 中继，只要控制端、DERP 和 HTTPS 可达仍可连接。`tailscale status` 或 `tailscale ping <目标>` 可查看是 `direct` 还是 `relay`。

## 6. 部署 Samba 并挂载 Z、Y 盘

在 RK 安装 Samba：

```sh
apt update
apt install -y samba
cp -a /etc/samba/smb.conf /etc/samba/smb.conf.before-rk-cloud
printf '\n' >> /etc/samba/smb.conf
cat samba/smb.conf.fragment >> /etc/samba/smb.conf
smbpasswd -a cloud
testparm -s
install -d -m 0755 /etc/systemd/system/smbd.service.d
install -m 0644 samba/smbd-rk-cloud.conf \
  /etc/systemd/system/smbd.service.d/rk-cloud.conf
systemctl daemon-reload
systemctl enable --now smbd
ss -lntp | grep ':445'
```

示例配置让 Samba 只监听 `127.0.0.1:445`。再让 Tailscale 在 Tailnet 地址的 445 端口转发到本地 Samba：

```sh
tailscale serve --bg --tcp=445 tcp://127.0.0.1:445
tailscale serve status
```

在 Windows 管理员 PowerShell 中分别挂载两个网络驱动器：

```powershell
net use Z: /delete /y
net use Y: /delete /y
net use X: /delete /y
net use Z: "\\<RK_TAILSCALE_IP>\RKSSD" /user:cloud * /persistent:yes
net use Y: "\\<RK_TAILSCALE_IP>\RKUSB" /user:cloud * /persistent:yes
net use X: "\\<RK_TAILSCALE_IP>\RKUSB2" /user:cloud * /persistent:yes
```

第一次命令中的星号会让 Windows 安全地交互输入 Samba 密码；后续命令通常会复用同一服务器凭据。不要把密码写入 `.bat`、PowerShell 历史或 Git。`Z:` 是 SSD，`Y:`、`X:` 是两块 U 盘，Windows 显示的容量会分别对应三个真实文件系统。

## 7. 在 RK 部署 FileBrowser Quantum

本项目验证的是 `v1.5.5-stable` ARM64：

```sh
wget -O /tmp/filebrowser-quantum \
  https://github.com/gtsteffaniak/filebrowser/releases/download/v1.5.5-stable/linux-arm64-filebrowser
echo 'bd787c5b827235909af75b0b42594f48f48b838391022fbf6b535db33db7a7a9  /tmp/filebrowser-quantum' | sha256sum -c -
install -m 0755 /tmp/filebrowser-quantum /usr/local/bin/filebrowser-quantum
```

安装配置和服务：

```sh
install -d -m 0755 /etc/filebrowser-quantum
install -d -m 0750 -o cloud -g cloud \
  /var/lib/filebrowser-quantum /var/cache/filebrowser-quantum
install -m 0644 filebrowser/config.yaml \
  /etc/filebrowser-quantum/config.yaml
sed -i 's/cloud\.example\.com/你的云盘域名/g' \
  /etc/filebrowser-quantum/config.yaml
install -m 0644 systemd/filebrowser-quantum.service \
  /etc/systemd/system/filebrowser-quantum.service
systemctl daemon-reload
systemctl enable --now filebrowser-quantum
systemctl status --no-pager filebrowser-quantum
curl -I http://127.0.0.1:18082/
```

这里不能把 `/srv/cloud` 配成一个 source：它只是 eMMC 上的父目录，内部跨越多个挂载点，容量会被识别成父文件系统容量。把 `/srv/cloud/SSD`、`/srv/cloud/USB` 和 `/srv/cloud/USB2` 配成独立 source 后，FileBrowser 才能分别读取正确的文件系统统计。`frontend.disableUsedPercentage: false` 会显示占用条。

FileBrowser 使用 `X-Forwarded-User` 代理认证。它必须只监听 `127.0.0.1`，否则攻击者可自行伪造这个头绕过登录。

### 安装资源管理器风格网页

仓库的 `frontend/` 是一个不依赖 Node.js 构建环境的静态前端。它通过 FileBrowser Quantum 的同源 API 读取真实目录、文件夹大小和磁盘容量，并提供目录浏览、搜索、上传、下载、新建文件夹、重命名和删除。文件夹下载由新界面直接请求 ZIP 压缩包，不依赖旧页面。旧 FileBrowser 页面不再公开，访问 `/files/` 会跳回 `/explorer/`；FileBrowser 后端仍作为内部文件 API 使用。

```sh
install -d -m 0755 /usr/local/share/rk-cloud/explorer
install -m 0644 frontend/index.html frontend/styles.css frontend/app.js \
  /usr/local/share/rk-cloud/explorer/
```

静态页面本身不保存 Key，也不能绕过鉴权。Nginx 对 `/explorer/` 执行与 FileBrowser 相同的 `auth_request`，浏览器登录后才会收到页面文件；API 请求继续由 Nginx 注入可信的 `X-Forwarded-User`。

也可以将这三个静态文件安装到公网服务器的站点目录：

```sh
install -d -m 0755 /www/wwwroot/cloud.example.com/explorer
install -m 0644 frontend/index.html frontend/styles.css frontend/app.js \
  /www/wwwroot/cloud.example.com/explorer/
```

仓库的 `server/cloud.https.conf.example` 已包含对应的静态路由。此布局下，静态外壳由公网服务器直接返回，但目录、容量及全部文件操作仍通过 FRP 到 RK；未登录的 API 请求仍会被 RK 重定向到 Key 登录页，因此静态服务器接触不到文件内容。

## 8. 部署单 Key 鉴权与 RK Nginx

安装 Python 服务和管理命令：

```sh
install -d -m 0755 /usr/local/lib/rk-cloud /etc/rk-cloud
install -m 0755 gateway/cloud_auth.py \
  /usr/local/lib/rk-cloud/cloud_auth.py
install -m 0750 gateway/cloudkey /usr/local/sbin/cloudkey
install -d -m 0700 -o cloud -g cloud /etc/cloud-auth
install -m 0644 config/cloud.env.example /etc/rk-cloud/cloud.env
sed -i 's/cloud\.example\.com/你的云盘域名/g' /etc/rk-cloud/cloud.env
install -m 0644 systemd/cloud-auth.service \
  /etc/systemd/system/cloud-auth.service
```

创建 owner Key。明文只显示一次，请立即存入密码管理器：

```sh
cloudkey init
cloudkey add owner --expires never
```

安装 RK Nginx：

```sh
apt install -y nginx
install -m 0644 nginx/rk-cloud.conf /etc/nginx/conf.d/rk-cloud.conf
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl daemon-reload
systemctl enable --now cloud-auth filebrowser-quantum nginx
curl -fsS http://127.0.0.1:18081/healthz
curl -I http://127.0.0.1:18080/login
```

部署完成后，访问域名根路径会跳转到 `/explorer/`。验证静态页面可访问、旧界面路由已停用：

```sh
curl -I http://127.0.0.1:18080/explorer/
curl -I http://127.0.0.1:18080/files/SSD/
```

两条命令都应返回重定向；第一条指向 `/login`（尚未登录时），第二条固定指向 `/explorer/`。

认证过程是：RK Nginx 先用 `auth_request` 询问 `cloud-auth`；成功后只取回内部用户名，再由 Nginx 写入 `X-Forwarded-User` 交给 FileBrowser。浏览器不能直接提供这个用户名。

Key 常用管理命令：

```sh
cloudkey list
cloudkey add guest --expires 7d
cloudkey rotate owner
cloudkey revoke guest
cloudkey enable guest
cloudkey sessions-reset
```

`rotate` 和 `revoke` 会让对应浏览器会话立即失效；`sessions-reset` 会退出所有浏览器，但不更换 Key。

## 9. 部署 FRP 公网隧道

### 公网服务器：FRPS

下载和校验 AMD64 FRP：

```sh
FRP_VERSION=0.67.0
wget -O /tmp/frp.tar.gz \
  "https://github.com/fatedier/frp/releases/download/v${FRP_VERSION}/frp_${FRP_VERSION}_linux_amd64.tar.gz"
echo 'f8629ca7ca56b8e7e7a9903779b8d5c47c56ad1b75b99b2d7138477acc4c7105  /tmp/frp.tar.gz' | sha256sum -c -
tar -xzf /tmp/frp.tar.gz -C /tmp
install -m 0755 "/tmp/frp_${FRP_VERSION}_linux_amd64/frps" /usr/local/bin/frps
```

创建服务账号和配置。token 必须在服务器现场生成：

```sh
getent group frp >/dev/null || groupadd --system frp
id frp >/dev/null 2>&1 || useradd --system --gid frp \
  --home-dir /nonexistent --shell /usr/sbin/nologin frp
install -d -m 0750 -o root -g frp /etc/frp
install -m 0640 -o root -g frp server/frps.toml.example /etc/frp/frps.toml
FRP_TOKEN=$(openssl rand -hex 32)
sed -i "s/REPLACE_WITH_A_LONG_RANDOM_TOKEN/$FRP_TOKEN/" /etc/frp/frps.toml
unset FRP_TOKEN
install -m 0644 server/frps.service /etc/systemd/system/frps.service
systemctl daemon-reload
systemctl enable --now frps
ss -lntp | grep ':10925'
```

安全关键点是 `proxyBindAddr = "127.0.0.1"`：RK 请求的远端端口 18082 只能被同机 Nginx 访问，不直接暴露公网。防火墙只需为 RK 放行 TCP 10925；不要放行 18082。

### RK：FRPC

下载 ARM64 包并安装 `frpc`：

```sh
FRP_VERSION=0.67.0
wget -O /tmp/frp.tar.gz \
  "https://github.com/fatedier/frp/releases/download/v${FRP_VERSION}/frp_${FRP_VERSION}_linux_arm64.tar.gz"
echo '0e9683226acdcbbb2ac8d073f35ba8be2a8b1e7584684d2073f39d337ebd6de7  /tmp/frp.tar.gz' | sha256sum -c -
tar -xzf /tmp/frp.tar.gz -C /tmp
install -m 0755 "/tmp/frp_${FRP_VERSION}_linux_arm64/frpc" \
  /usr/local/bin/frpc-cloud
getent group frp >/dev/null || groupadd --system frp
id frp >/dev/null 2>&1 || useradd --system --gid frp \
  --home-dir /nonexistent --shell /usr/sbin/nologin frp
install -d -m 0750 -o root -g frp /etc/frp-cloud
```

将 `frp/frpc.toml.template` 复制为 `/etc/frp-cloud/frpc.toml`，替换公网 IP 和与 FRPS 完全相同的 token：

```sh
install -m 0640 -o root -g frp frp/frpc.toml.template \
  /etc/frp-cloud/frpc.toml
sed -i \
  -e 's/203\.0\.113\.10/你的公网IP/' \
  -e 's/__FRP_TOKEN__/你的FRP_TOKEN/' \
  /etc/frp-cloud/frpc.toml
install -m 0644 systemd/frpc-cloud.service \
  /etc/systemd/system/frpc-cloud.service
systemctl daemon-reload
systemctl enable --now frpc-cloud
journalctl -u frpc-cloud -n 50 --no-pager
```

回到公网服务器检查 `127.0.0.1:18082`，成功时应该到达 RK 登录页：

```sh
ss -lntp | grep ':18082'
curl -I -H 'Host: cloud.example.com' http://127.0.0.1:18082/login
```

## 10. 配置公网 HTTPS 云盘域名

将 `cloud.example.com` 的 A 记录指向公网服务器。先复制 HTTP 配置完成 ACME 验证，再申请证书：

```sh
install -d -m 0755 /var/www/cloud.example.com
install -m 0644 server/cloud.http.conf.example \
  /etc/nginx/sites-available/cloud.conf
```

仓库示例使用宝塔的 `/www/wwwroot` 和 `/www/wwwlogs` 路径。普通 Debian Nginx 请把配置中的网站根目录改为 `/var/www/cloud.example.com`，日志改到 `/var/log/nginx/`，然后：

```sh
ln -s /etc/nginx/sites-available/cloud.conf /etc/nginx/sites-enabled/cloud.conf
nginx -t
systemctl reload nginx
certbot certonly --webroot -w /var/www/cloud.example.com \
  -d cloud.example.com --non-interactive --agree-tos \
  --register-unsafely-without-email
```

再部署 `server/cloud.https.conf.example`，同样调整域名、网站根目录和日志路径：

```sh
install -m 0644 server/cloud.https.conf.example \
  /etc/nginx/sites-available/cloud.conf
install -m 0644 server/cloud-unavailable.html \
  /var/www/cloud.example.com/cloud-unavailable.html
nginx -t
systemctl reload nginx
curl -I https://cloud.example.com/login
```

正式请求只开放 443。Nginx 将 Host、客户端 IP、协议和 WebSocket 头传入 FRP；RK 鉴权服务据此校验来源。不要让另一个站点随意代理到 `127.0.0.1:18082`。

## 11. 可选：Wi-Fi 自动恢复

若校园网或热点偶尔断连，可安装轻量看门狗：

```sh
install -d -m 0755 /etc/rk-cloud
install -m 0600 network/rk-wifi-watchdog.env.example \
  /etc/rk-cloud/wifi-watchdog.env
install -m 0755 network/rk-wifi-watchdog.sh \
  /usr/local/sbin/rk-wifi-watchdog
install -m 0644 network/rk-wifi-watchdog.service \
  /etc/systemd/system/rk-wifi-watchdog.service
install -m 0644 network/rk-wifi-watchdog.timer \
  /etc/systemd/system/rk-wifi-watchdog.timer
```

编辑环境文件中的 NetworkManager 连接名、Headscale 域名和服务器 IP，再启用：

```sh
systemctl daemon-reload
systemctl enable --now rk-wifi-watchdog.timer
systemctl list-timers rk-wifi-watchdog.timer
```

无线密码仍由 NetworkManager 安全保存，不应写入看门狗脚本或本仓库。

## 12. 验收

### RK 本地检查

```sh
systemctl --no-pager --full status \
  tailscaled smbd filebrowser-quantum cloud-auth nginx frpc-cloud
findmnt /srv/cloud/SSD /srv/cloud/USB
df -hT /srv/cloud/SSD /srv/cloud/USB
ss -lntp | grep -E ':(445|18080|18081|18082)\b'
tailscale serve status
```

预期：445、18080、18081、18082 都只监听回环；445 另由 Tailscale IP 提供；三个存储目录的 `SOURCE` 分别对应真实 SSD 和两块 U 盘。

### 公网服务器检查

```sh
systemctl --no-pager --full status headscale frps nginx
ss -lntp | grep -E ':(80|443|10925|18082)\b'
curl -fsS https://headscale.example.com/health
curl -I https://cloud.example.com/login
```

### 浏览器检查

1. 打开云盘 HTTPS 域名；
2. 输入 `cloudkey add` 创建的 Key；
3. 左侧应看到 `SSD`、`USB` 和 `USB2` 三个来源及各自容量；
4. 分别在三个来源中新建、上传、重命名、下载和删除测试文件；
5. 在 Windows `Z:` 盘确认能看到同一文件变化。

也可在 Windows 使用仓库内的自动化脚本：

```powershell
.\tests\public_crud.ps1 `
  -AccessKey '<TEMPORARY_TEST_KEY>' `
  -BaseUrl 'https://cloud.example.com' `
  -HostName 'cloud.example.com'
```

建议先用 `cloudkey add smoke-test --expires 1h` 创建临时 Key，测试后立即 `cloudkey revoke smoke-test`。

### 重启检查

```sh
reboot
```

重新上线后重复 `findmnt`、`df`、`systemctl`、`tailscale serve status` 和公网 CRUD 测试。仓库中的 FileBrowser 服务和 Samba drop-in 都有 `mountpoint` 前置检查；U 盘或 SSD 缺失时服务会拒绝启动，避免把文件误写进未挂载的 eMMC 目录。

## 13. 常见故障

### 公网页面 502 或“正在恢复连接”

公网 Nginx 已工作，但 FRP 到 RK 的后端不可达。依次检查：

```sh
# 公网服务器
systemctl status frps nginx
ss -lntp | grep -E ':(10925|18082)\b'

# RK
ip route
systemctl status frpc-cloud nginx cloud-auth filebrowser-quantum
journalctl -u frpc-cloud -n 100 --no-pager
curl -I http://127.0.0.1:18080/login
```

### `/login` 返回 403

常见原因是请求的 Host/Origin 与配置域名不一致，或反向代理没有正确传递 Host。检查 `/etc/rk-cloud/cloud.env`、FileBrowser `externalUrl`、公网/RK 两层 Nginx 的 `Host` 与 `X-Forwarded-Host`。某些浏览器会发送 `Origin: null`；本仓库的认证网关只在 Host 和转发 Host 归一化后恰好指向同一站点时接受它。

### Key 无法登录

```sh
cloudkey list
journalctl -u cloud-auth -n 100 --no-pager
```

Key 可能已过期、被撤销或已轮换。不要尝试读取旧明文；用 `cloudkey rotate <name>` 生成替代 Key。

### Z 或 Y 盘断开

```powershell
& "$env:ProgramFiles\Tailscale\tailscale.exe" status
ping <RK_TAILSCALE_IP>
net use
```

RK 上检查：

```sh
tailscale status
tailscale serve status
systemctl status smbd tailscaled
ss -lntp | grep ':445'
```

必要时删除旧映射后重新执行 `net use`。同一服务器的旧 Windows 凭据也可在“凭据管理器”中清理。

### 网页容量不显示或不正确

确认配置中是 `/srv/cloud/SSD`、`/srv/cloud/USB`、`/srv/cloud/USB2` 三个独立 source，且：

```yaml
frontend:
  disableUsedPercentage: false
```

修改后重启 FileBrowser，并退出网页重新登录或强制刷新：

```sh
systemctl restart filebrowser-quantum
```

如果把父目录 `/srv/cloud` 当成单一 source，它无法代表三个挂载点的容量总和。

## 14. 备份与升级

至少备份以下内容，但备份文件本身必须加密并限制权限：

- `/etc/cloud-auth/`；
- `/var/lib/filebrowser-quantum/database.db`；
- `/etc/filebrowser-quantum/config.yaml`；
- `/etc/frp/` 和 `/etc/frp-cloud/`；
- `/etc/headscale/`、`/var/lib/headscale/`；
- `/etc/samba/smb.conf`；
- Nginx 站点配置和证书续期配置；
- SSD、U 盘上的实际数据。

升级第三方程序前，先阅读 release notes，保存当前二进制和数据库，再在临时 Key 下完成登录、上传、下载、删除和重启测试。Headscale、Tailscale 客户端和 FileBrowser 的配置格式会随大版本变化，不要无审查地自动升级。

## 15. 官方参考

- [Headscale 官方安装](https://headscale.net/stable/setup/install/official/)
- [Headscale 设备注册](https://headscale.net/stable/ref/registration/)
- [Headscale 策略](https://headscale.net/stable/ref/acls/)
- [Headscale 反向代理](https://headscale.net/stable/ref/integration/reverse-proxy/)
- [Tailscale Linux 安装](https://tailscale.com/docs/install/linux)
- [Tailscale Serve CLI](https://tailscale.com/docs/reference/tailscale-cli/serve)
- [FileBrowser Quantum sources](https://filebrowserquantum.com/en/docs/configuration/sources/)
- [FileBrowser Quantum 代理认证](https://filebrowserquantum.com/en/docs/configuration/authentication/proxy/)
- [FileBrowser Quantum 界面容量选项](https://filebrowserquantum.com/en/docs/configuration/frontend/interface/)
- [FRP 官方仓库与配置示例](https://github.com/fatedier/frp)
