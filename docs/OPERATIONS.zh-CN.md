# RK3576 私有云盘使用与维护说明

本文记录当前已经部署完成的私有云盘环境，供日常使用、设备接入、Key 管理、磁盘维护和故障排查使用。

最后核对时间：2026-09-02。

## 1. 当前状态

- 公网入口：`https://cloud.example.com/`
- Headscale 控制端：`https://headscale.example.com/`
- RK 的 Tailscale 地址：`<RK_TAILSCALE_IP>`（用 `tailscale ip -4` 查询）
- Windows 网络驱动器：`Z:`
- Samba 共享：`RKCloud`
- Samba 用户：`cloud`
- SSD：`/dev/nvme0n1p1`，挂载到 `/srv/cloud/SSD`，约 116 GB 可用
- U 盘：`/dev/sda1`，挂载到 `/srv/cloud/USB`，约 58 GB 可用
- Web 和 `Z:` 操作的是同一份文件，不存在额外同步副本

截至本文最后核对时，SSD 和 U 盘的用户可见内容均为空。网页中只显示 `SSD` 和 `USB` 两个存储入口。

文件系统内部还保留以下隐藏内容：

- `lost+found`：ext4 文件系统的恢复目录，不应删除。
- `.rk-cloud-cache`：FileBrowser Quantum 的缓存目录，不应删除。
- `.resized`：存储初始化标记，不应删除。

这些内部内容已从网页文件列表中排除。

## 2. 整体结构

```text
Windows Z:
  Windows -> Tailscale/Headscale -> RK Tailscale Serve -> Samba -> /srv/cloud

公网网页：
  浏览器 -> HTTPS cloud.example.com -> 公网服务器 Nginx
         -> FRP 反向隧道 -> RK Nginx -> 单 Key 鉴权
         -> FileBrowser Quantum -> /srv/cloud/{SSD,USB}
```

公网服务器只承担 Headscale、HTTPS 和 FRP 转发，不保存云盘文件。实际数据始终位于 RK 上连接的 SSD 或 U 盘中。

## 3. 从网页访问

1. 打开部署时设置的云盘域名，例如 `https://cloud.example.com/`。
2. 输入有效的访问 Key。
3. 登录后，左侧可看到 `SSD` 和 `USB` 两个独立来源及各自的容量占用。
4. 可在权限允许的情况下上传、下载、新建目录、重命名和删除。

陌生设备不需要安装 Tailscale，只要能访问 HTTPS 域名并持有有效 Key 即可登录。

不要把明文 Key 写入本文、公开仓库、聊天群或浏览器脚本。建议保存到密码管理器中。

当前网页登录会话有效期为 12 小时。关闭浏览器不会立即撤销会话；需要立即让所有浏览器退出时，使用后文的 `cloudkey sessions-reset`。

### 删除文件的含义

Web 和 `Z:` 看到的是同一份文件，因此：

- 在网页删除文件，`Z:` 中也会立即消失。
- 在 `Z:` 中删除文件，网页中也会立即消失。
- 当前没有配置回收站、版本历史或自动备份。

重要文件在删除前应先复制到另一台设备或独立备份介质。

## 4. 从 Windows 的 Z 盘访问

Windows 已通过 Headscale 管理的 Tailscale 网络连接到 RK。正常情况下，资源管理器中的 `Z:` 包含：

```text
Z:\SSD
Z:\USB
```

查看当前映射：

```powershell
net use
Get-PSDrive Z
```

验证两个存储入口：

```powershell
Get-Item Z:\SSD, Z:\USB
```

如果需要重新映射，先确认 Tailscale 已连接，然后执行：

```powershell
net use Z: /delete
net use Z: \\<RK_TAILSCALE_IP>\RKCloud /user:cloud * /persistent:yes
```

命令中的 `*` 会让 Windows 交互式询问 Samba 密码，避免把密码留在命令历史中。

检查 Tailscale 连通性：

```powershell
tailscale status
tailscale ping <RK_TAILSCALE_IP>
Test-NetConnection <RK_TAILSCALE_IP> -Port 445
```

SMB 服务本身只监听 RK 的回环地址 `127.0.0.1:445`，由 Tailscale Serve 将 Tailnet 内的 `<RK_TAILSCALE_IP>:445` 转发到 Samba。因此校园网和公网不能直接访问 445 端口。

### Z 盘容量显示说明

`Z:` 是一个共享根目录，下面包含两个独立文件系统。Windows 可能把共享根目录容量显示为 RK 系统盘容量，而不是 SSD 与 U 盘容量之和。这不表示数据写入了 eMMC。

以 RK 上的以下命令为准：

```sh
df -hT /srv/cloud/SSD /srv/cloud/USB
```

## 5. 存储目录与挂载

统一共享根目录为：

```text
/srv/cloud
├── SSD   -> 128 GB NVMe SSD
└── USB   -> 64 GB U 盘
```

这两个设备没有组成 RAID、LVM 或联合文件系统；它们只是分别挂载在共享根目录的两个子目录中。这样任一设备故障时不会同时破坏另一个设备的数据。

`/etc/fstab` 中当前使用 UUID 自动挂载：

```fstab
UUID=<SSD_UUID> /srv/cloud/SSD ext4 defaults,noatime,nofail,x-systemd.device-timeout=10s 0 2
UUID=<USB_UUID> /srv/cloud/USB ext4 defaults,noatime,nofail,x-systemd.device-timeout=10s 0 2
```

检查设备、文件系统和挂载点：

```sh
lsblk -o NAME,MODEL,SIZE,FSTYPE,MOUNTPOINTS,TRAN
findmnt /srv/cloud/SSD
findmnt /srv/cloud/USB
df -hT /srv/cloud/SSD /srv/cloud/USB
```

### 安全拔出 U 盘

先停止对 U 盘的读写，再执行：

```sh
sync
umount /srv/cloud/USB
```

确认 `findmnt /srv/cloud/USB` 没有输出后再拔出。重新插入后可执行：

```sh
mount /srv/cloud/USB
```

不要在仍有上传、下载或 SMB 文件操作时直接拔出 U 盘。

### 权限

网页和 Samba 都以 Linux 用户 `cloud` 访问文件。正常的用户目录应属于 `cloud:cloud`，目录权限通常为 `0770`，文件权限通常为 `0660`。

如果通过 root 命令手动复制了文件，网页无法写入，可只修复对应文件或目录：

```sh
chown -R cloud:cloud /srv/cloud/SSD/需要修复的目录
chmod -R u+rwX,g+rwX,o-rwx /srv/cloud/SSD/需要修复的目录
```

应替换为明确的目标路径，不要直接对整个系统执行递归 `chown`，也不要使用 `chmod 777`。

## 6. Key 管理

所有 Key 都在 RK 上管理：

```sh
cloudkey list
```

Key 格式类似：

```text
rk1.<标识符>.<随机密钥>
```

RK 只保存使用 scrypt 派生的单向散列，不保存明文 Key。因此明文 Key只能在创建或轮换时看到一次，之后无法反查。

### 创建临时 Key

```sh
cloudkey add guest-phone --expires 1d
cloudkey add guest-week --expires 7d
```

有效期支持：

- `30m`：30 分钟
- `12h`：12 小时
- `7d`：7 天
- `4w`：4 周
- `never`：永不过期

创建后终端会显示一次完整 Key，应立即安全保存。

### 轮换 owner Key

```sh
cloudkey rotate owner
```

该命令会生成新的 owner Key，并立即让旧 Key 和旧 Key 创建的浏览器会话失效。

### 撤销或重新启用 Key

```sh
cloudkey revoke guest-phone
cloudkey enable guest-phone
```

撤销会立即阻止该 Key 并使其已有会话失效。

### 退出所有浏览器

```sh
cloudkey sessions-reset
```

该命令更换会话签名密钥，使所有当前 Cookie 立即失效，但不会更换访问 Key。

### 登录保护

- 登录失败限制：同一来源 10 分钟内最多尝试 5 次。
- 超过限制返回 HTTP `429`，等待限制时间结束后再试。
- Cookie 使用 `Secure`、`HttpOnly` 和 `SameSite=Lax`。
- Key 通过 HTTPS 传输。

## 7. RK 上的服务

当前关键服务：

| 服务 | 作用 | 当前设置 |
| --- | --- | --- |
| `cloud-auth` | 单 Key 登录、Cookie 会话 | 开机启动 |
| `filebrowser-quantum` | 网页文件管理 | 开机启动 |
| `nginx` | RK 本地鉴权与反向代理 | 开机启动 |
| `frpc-cloud` | 主动连接阿里云 FRP | 开机启动 |
| `smbd` | Windows SMB 共享 | 开机启动 |
| `tailscaled` | Tailnet 点对点网络 | 开机启动 |
| `rk-wifi-watchdog.timer` | 检查校园网和 FRP 连通性 | 每 30 秒检查 |

一次检查所有服务：

```sh
systemctl is-active \
  cloud-auth filebrowser-quantum nginx frpc-cloud \
  smbd tailscaled rk-wifi-watchdog.timer
```

检查是否设置为开机启动：

```sh
systemctl is-enabled \
  cloud-auth filebrowser-quantum nginx frpc-cloud \
  smbd tailscaled rk-wifi-watchdog.timer
```

查看近期日志：

```sh
journalctl -u cloud-auth --since "-30 min" --no-pager
journalctl -u filebrowser-quantum --since "-30 min" --no-pager
journalctl -u frpc-cloud --since "-30 min" --no-pager
journalctl -u rk-wifi-watchdog --since "-30 min" --no-pager
tail -100 /var/log/nginx/error.log
```

仅在对应服务确实异常时重启：

```sh
systemctl restart cloud-auth
systemctl restart filebrowser-quantum
systemctl restart frpc-cloud
systemctl restart smbd
```

修改 RK Nginx 配置后应先检查再重载：

```sh
nginx -t
systemctl reload nginx
```

### RK 本地端口

- `127.0.0.1:18080`：受保护的云盘整合入口
- `127.0.0.1:18081`：单 Key 鉴权服务
- `127.0.0.1:18082`：FileBrowser Quantum
- `127.0.0.1:445`：Samba，由 Tailscale Serve 转发到 Tailnet

这些服务不应直接暴露到公网。

## 8. 阿里云服务器维护

从当前 Windows 电脑连接服务器：

```powershell
ssh -o IdentitiesOnly=yes -i "<path-to-private-key>" root@203.0.113.10
```

服务器关键服务：

- `headscale`：Tailscale 的自建控制端。
- `frps`：接收 RK 主动建立的反向隧道。
- 宝塔 Nginx：处理域名、HTTPS 和转发。
- `certbot-renew.timer`：自动续期 Let's Encrypt 证书。

检查状态：

```sh
systemctl is-active headscale frps certbot-renew.timer
systemctl is-enabled headscale frps certbot-renew.timer
```

检查 FRP 端口：

```sh
ss -lntp | grep -E '(:10925|127.0.0.1:18082)'
```

- `10925`：FRP 客户端控制连接端口。
- `127.0.0.1:18082`：云盘隧道出口，仅允许服务器本机 Nginx 访问。

检查 Nginx：

```sh
/www/server/nginx/sbin/nginx -t
tail -100 /www/wwwlogs/cloud.example.com.error.log
```

检查证书：

```sh
certbot certificates
systemctl list-timers certbot-renew.timer
```

服务器部署文件位于：

```text
/root/rk-cloud-deploy
```

云盘虚拟主机配置位于：

```text
/www/server/panel/vhost/nginx/cloud.example.com.conf
```

## 9. 常见故障

### 网页显示“云盘正在恢复连接”

含义：服务器暂时无法通过 FRP 连接 RK。页面会每 5 秒自动重试。

先在 RK 检查：

```sh
ip -br address show wlan0
ip route
ping -c 3 203.0.113.10
systemctl status frpc-cloud --no-pager
journalctl -u frpc-cloud --since "-15 min" --no-pager
```

再在服务器检查：

```sh
systemctl status frps --no-pager
ss -lntp | grep -E '(:10925|127.0.0.1:18082)'
curl -I http://127.0.0.1:18082/
```

### 登录返回 403

含义：登录请求的来源校验失败。当前版本兼容某些浏览器发送的 `Origin: null`，但只在代理 Host 校验一致时允许。

检查日志：

```sh
journalctl -u cloud-auth --since "-10 min" --no-pager | grep "rejected cross-origin"
```

不要反复刷新 POST 产生的错误页面，应重新打开云盘首页。

### 显示“Key 无效或已过期”

检查 Key 状态：

```sh
cloudkey list
```

明文 Key 无法反查。如果已经丢失，应轮换：

```sh
cloudkey rotate owner
```

### 登录返回 429

同一来源短时间输入了过多错误 Key。停止尝试并等待约 10 分钟，或者在确认不是攻击后重启 `cloud-auth` 清空内存中的失败计数：

```sh
systemctl restart cloud-auth
```

### Z 盘断开

依次检查：

```powershell
tailscale status
tailscale ping 100.64.0.1
Test-NetConnection 100.64.0.1 -Port 445
net use
```

Windows 可能把长期未访问的网络驱动器显示为“已断开”；双击 `Z:` 后能够恢复通常属于正常的延迟重连。

RK 上检查：

```sh
tailscale status
tailscale serve status
systemctl status smbd tailscaled --no-pager
```

### SSD 或 USB 不显示

```sh
lsblk -f
findmnt /srv/cloud/SSD
findmnt /srv/cloud/USB
mount -a
df -hT /srv/cloud/SSD /srv/cloud/USB
```

执行写操作前必须先确认目标设备确实已经挂载，避免把文件误写入 eMMC 上的空挂载目录。

### 网页能浏览但不能写入

检查目录归属和权限：

```sh
namei -l /srv/cloud/SSD/目标路径
ls -ld /srv/cloud/SSD/目标路径
```

只修复明确的目标目录，参考“存储目录与挂载”章节中的权限命令。

## 10. 配置文件位置

RK：

```text
/usr/local/lib/rk-cloud/cloud_auth.py
/etc/cloud-auth/keys.json
/etc/cloud-auth/session.key
/etc/filebrowser-quantum/config.yaml
/var/lib/filebrowser-quantum/database.db
/etc/nginx/conf.d/rk-cloud.conf
/etc/frp-cloud/frpc.toml
/etc/samba/smb.conf
/etc/systemd/system/cloud-auth.service
/etc/systemd/system/filebrowser-quantum.service
/etc/systemd/system/frpc-cloud.service
/usr/local/sbin/rk-wifi-watchdog
```

阿里云服务器：

```text
/etc/frp/frps.toml
/etc/systemd/system/frps.service
/www/server/panel/vhost/nginx/cloud.example.com.conf
/etc/letsencrypt/live/cloud.example.com/
/root/rk-cloud-deploy/
```

Windows 源码：

```text
D:\root\cloud
```

仓库主要内容：

```text
gateway/       单 Key 鉴权服务
filebrowser/   FileBrowser Quantum 配置
nginx/         RK Nginx 配置
frp/           FRP 客户端模板
systemd/       RK 服务单元
server/        阿里云 Nginx、FRPS 和证书脚本
tests/         鉴权、权限和公网读写测试
```

配置文件中可能包含或引用认证信息。复制、提交或发送前应检查并清除 Key、FRP token、Samba 密码和 SSH 私钥。

## 11. 当前软件版本

最后核对时：

- RK：Debian 12，Linux 6.1.75，ARM64
- FileBrowser Quantum：`v1.5.5-stable`
- FRP 客户端与服务器：`0.67.0`
- RK Nginx：`1.22.1`
- Samba：`4.17.12-Debian`
- Headscale：`0.29.3`
- 阿里云 Nginx：`1.26.1`

升级前应备份配置与数据库，并阅读新版本的兼容性说明。不要直接覆盖正在运行的二进制后跳过服务重启和验收。

## 12. 建议的日常检查

每月或出现异常时执行：

```sh
df -hT /srv/cloud/SSD /srv/cloud/USB
systemctl --failed
systemctl is-active cloud-auth filebrowser-quantum nginx frpc-cloud smbd tailscaled
cloudkey list
journalctl -p warning --since "-7 days" --no-pager
```

同时确认：

- 重要文件存在另一份独立备份。
- 临时访客 Key 已过期或撤销。
- HTTPS 证书续期任务正常。
- SSD 和 U 盘没有 I/O 错误。
- 不在脚本或文档中保存明文 Key 和密码。
