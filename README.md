# RK Cloud

在 RK3576、SSD/U 盘和一台公网 Linux 服务器上搭建的自托管私有云盘方案。

它同时提供两条访问路径：

- 已加入 Headscale Tailnet 的电脑通过 Tailscale + Samba 挂载为 Windows 网络驱动器。
- 未安装客户端的设备通过 HTTPS 域名和一次性管理的访问 Key 使用网页文件管理器。

两种方式直接操作 RK 上的同一份文件，不做二次同步，也不把文件保存到公网服务器。

## 架构

```text
Windows 网络驱动器
  Windows -> Tailscale/Headscale -> Tailscale Serve -> Samba
          -> Z: /srv/cloud/SSD, Y: /srv/cloud/USB

公网 Web
  Browser -> HTTPS/Nginx -> FRP -> RK Nginx -> cloud-auth
          -> FileBrowser Quantum -> /srv/cloud/{SSD,USB}
```

网页中将 SSD 和 U 盘配置成两个独立 source，因此可以分别显示两个文件系统的容量占用。

## 功能

- SSD、U 盘独立挂载，并分别映射为 Windows `Z:`、`Y:`，容量显示与物理盘一致。
- Windows SMB 端口只在 RK 回环地址监听，再由 Tailscale Serve 暴露到 Tailnet。
- 公网只开放 HTTPS；FRP 的 Web 出口在服务器上仅绑定 `127.0.0.1`。
- 自定义单 Key 登录，支持创建、过期、撤销、轮换和全局会话失效。
- Key 只保存 scrypt 散列，明文只在创建或轮换时显示一次。
- FileBrowser Quantum 使用反向代理认证，无公开后台管理入口。
- 自带仿 Windows 文件资源管理器网页：首页、地址栏、磁盘容量、详细列表与常用文件操作。
- systemd 自动启动和重连；可选 NetworkManager 网络看门狗。
- 包含单元测试、HTTP 集成测试和公网 SSD/USB CRUD 测试。

## 文档

- [完整部署指南](docs/DEPLOYMENT.zh-CN.md)
- [日常使用与维护](docs/OPERATIONS.zh-CN.md)
- [安全说明](SECURITY.md)

部署指南从磁盘初始化、Headscale、Samba、Tailscale Serve、FileBrowser、Key 网关、FRP、Nginx、HTTPS一直写到重启和公网验收。

## 目录

```text
config/        RK 部署变量示例
filebrowser/   FileBrowser Quantum 配置
frontend/      仿 Windows 文件资源管理器网页
frp/           FRP 客户端配置模板
gateway/       单 Key 认证服务
headscale/     Headscale 示例配置与 systemd 服务
nginx/         RK 本地 Nginx 配置
network/       可选的 Wi-Fi 与隧道恢复看门狗
samba/         Samba 共享配置片段
server/        公网服务器 FRPS、Nginx 和证书配置
systemd/       RK 服务单元
tests/         单元、集成和公网 CRUD 测试
docs/          部署及运维文档
```

## 版本基线

本项目实际验证过以下组合：

- Debian 12 / ARM64 / Linux 6.1
- FileBrowser Quantum `v1.5.5-stable`
- FRP `0.67.0`
- Headscale `0.29.3`
- Samba `4.17.x`
- Nginx `1.22+`

下载第三方二进制时，应从各项目的官方发布页获取，并校验官方提供的 SHA256；仓库不分发这些二进制。

## 快速准备

克隆仓库后，先将示例值替换为自己的部署信息：

```text
cloud.example.com       -> 云盘域名
headscale.example.com   -> Headscale 域名
tail.example.com        -> Tailnet MagicDNS 域
203.0.113.10            -> 公网服务器 IP
<SSD_UUID>              -> SSD 文件系统 UUID
<USB_UUID>              -> U 盘文件系统 UUID
```

FRP token、Samba 密码、访问 Key 和 SSH 私钥不得提交到 Git。完整步骤见部署指南。

## Key 管理

在 RK 的 root shell 中执行：

```sh
cloudkey list
cloudkey add guest --expires 7d
cloudkey rotate owner
cloudkey revoke guest
cloudkey enable guest
cloudkey sessions-reset
```

`add` 和 `rotate` 只输出一次明文 Key。`revoke`、`rotate` 会立即让相关浏览器会话失效；`sessions-reset` 会退出所有浏览器，但不更换访问 Key。

## 测试

在 Linux 或 WSL 中运行鉴权测试：

```sh
python3 -m unittest discover -s tests -v
bash tests/integration.sh
```

部署完成后，从 Windows 执行公网读写测试：

```powershell
.\tests\public_crud.ps1 `
  -AccessKey '<temporary-test-key>' `
  -BaseUrl 'https://cloud.example.com' `
  -HostName 'cloud.example.com'
```

测试会在 SSD 和 USB 分别创建、读取、重命名并删除一个随机文件，并在 `finally` 中清理测试路径。建议使用短期临时 Key，不要把 owner Key 写进命令脚本或 CI 日志。

## 安全边界

- 仓库中的域名、IP、UUID 和 token 都是示例值。
- 不要提交 `/etc/cloud-auth`、FileBrowser 数据库、真实 FRP 配置、证书私钥或 Samba 密码。
- 不要把 Samba 445 端口直接暴露到校园网或公网。
- 网页删除和 SMB 删除都会直接删除同一份真实文件；本项目默认不提供回收站和备份。
- 在公开部署前应按自己的用户、设备和标签收紧 Headscale ACL。

## 许可证

本仓库未包含 FileBrowser Quantum、FRP、Headscale、Tailscale、Samba 或 Nginx 的二进制和源码。使用这些项目时请分别遵守其许可证。
