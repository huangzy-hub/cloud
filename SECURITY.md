# 安全说明

## 必须保密的内容

下列内容不得提交到 Git、粘贴到公开 Issue，或写入可公开访问的日志：

- Web 访问 Key 及 `/etc/cloud-auth/` 中的状态文件；
- FRP 的 `auth.token`；
- Samba 密码；
- Headscale 预认证 Key、数据库和私钥；
- SSH 私钥、TLS 私钥、无线网络和校园网凭据；
- FileBrowser 数据库及浏览器 Cookie。

仓库只保存 `.example`、`.template` 和占位符。部署时应在目标机器上生成随机值，并把真实配置权限限制为 `root` 或相应服务组可读。

## 网络暴露边界

- Samba 仅监听 RK 的 `127.0.0.1:445`，由 Tailscale Serve 暴露给 Tailnet；不要把 445 端口直接开放到局域网或公网。
- FileBrowser、认证网关和 RK Nginx 均只监听回环地址。
- 公网服务器的 FRP 远端端口只绑定 `127.0.0.1`，公网请求必须经过 HTTPS Nginx。
- FileBrowser 的代理认证头只能由受信任的 RK Nginx 注入，不能让客户端绕过代理直连 FileBrowser。
- Headscale 初次联调可以临时使用宽松策略，稳定后必须按用户、设备或标签缩小访问范围。

## 凭据生成与轮换

使用密码学随机源生成 FRP token，例如：

```sh
openssl rand -hex 32
```

Web Key 由 `cloudkey add` 或 `cloudkey rotate` 生成。明文只显示一次；服务端保存 scrypt 散列。怀疑泄露时立即轮换，并执行 `cloudkey sessions-reset` 使所有浏览器会话失效。

## 数据安全

网页和 SMB 操作的是同一份真实文件。删除默认不可恢复，本项目不提供回收站、版本历史或异地备份。重要数据至少应保留一份与 RK、SSD 和 U 盘物理分离的备份，并定期做恢复演练。
