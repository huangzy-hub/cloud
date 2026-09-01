#!/usr/bin/env bash
set -euo pipefail

deploy_id=${1:-}
if [[ ! "$deploy_id" =~ ^[0-9a-f]{32}$ ]]; then
  echo "invalid deployment id" >&2
  exit 2
fi

cloud_domain=${CLOUD_DOMAIN:?set CLOUD_DOMAIN}
headscale_domain=${HEADSCALE_DOMAIN:?set HEADSCALE_DOMAIN}
server_public_ip=${SERVER_PUBLIC_IP:?set SERVER_PUBLIC_IP}

stamp=$(date -u +%Y%m%dT%H%M%SZ)
backup_dir="/root/frp-backup-before-rkcloud-$stamp"

install -d -m 0700 "$backup_dir"
cp -a "/www/server/panel/vhost/nginx/$headscale_domain.conf" "$backup_dir/headscale.conf"

if systemctl is-active --quiet frps.service && [[ -f /etc/frp/frps.toml ]]; then
  cp -a /etc/frp/frps.toml "$backup_dir/frps.toml"
else
  old_pid=$(pgrep -xo frps)
  old_cwd=$(readlink -f "/proc/$old_pid/cwd")
  cp -a "$old_cwd/frps.toml" "$backup_dir/frps.toml"
  getent group frp >/dev/null || groupadd --system frp
  id frp >/dev/null 2>&1 || useradd --system --gid frp --home-dir /nonexistent --shell /usr/sbin/nologin frp
  install -m 0755 "$old_cwd/frps" /usr/local/bin/frps
  install -d -m 0750 -o root -g frp /etc/frp
  install -m 0640 -o root -g frp "$old_cwd/frps.toml" /etc/frp/frps.toml
  if ! grep -q '^proxyBindAddr[[:space:]]*=' /etc/frp/frps.toml; then
    sed -i '1i proxyBindAddr = "127.0.0.1"' /etc/frp/frps.toml
  fi
  install -m 0644 /tmp/frps.service /etc/systemd/system/frps.service
  kill "$old_pid"
  for _ in 1 2 3 4 5; do
    kill -0 "$old_pid" 2>/dev/null || break
    sleep 1
  done
  systemctl daemon-reload
  systemctl enable --now frps.service
fi

install -d -m 0755 "/www/wwwroot/$cloud_domain"
install -m 0644 /tmp/cloud-unavailable.html "/www/wwwroot/$cloud_domain/cloud-unavailable.html"
install -m 0644 /tmp/cloud.http.conf "/www/server/panel/vhost/nginx/$cloud_domain.conf"
/www/server/nginx/sbin/nginx -t
/www/server/nginx/sbin/nginx -s reload -c /www/server/nginx/conf/nginx.conf

grep -q '^auth.token[[:space:]]*=' /etc/frp/frps.toml
awk -v server_addr="$server_public_ip" '
  /^auth.token[[:space:]]*=/ { token=$0 }
  END {
    print "serverAddr = \"" server_addr "\""
    print "serverPort = 10925"
    print "loginFailExit = false"
    print "auth.method = \"token\""
    print token
    print ""
    print "[[proxies]]"
    print "name = \"rk-cloud-web\""
    print "type = \"tcp\""
    print "localIP = \"127.0.0.1\""
    print "localPort = 18080"
    print "remotePort = 18082"
  }
' /etc/frp/frps.toml > /tmp/frpc.toml

work=$(mktemp -d)
tar -xzf /tmp/rk-cloud-public-bundle.tar.gz -C "$work"
install -m 0640 /tmp/frpc.toml "$work/frp/frpc.toml"
asset="/run/$deploy_id.tar.gz"
tar -czf "$asset" -C "$work" .
rm -rf "$work" /tmp/frpc.toml
chown root:www "$asset"
chmod 0640 "$asset"

conf="/www/server/panel/vhost/nginx/$headscale_domain.conf"
cp -a "$conf" /tmp/headscale-before-rk-deploy.conf
awk -v url="/$deploy_id.tar.gz" -v asset="$asset" -v domain="$headscale_domain" '
  $0 ~ "server_name[[:space:]]+" domain { blocks++ }
  blocks == 2 && !done && /^[[:space:]]*location \/ \{/ {
    print "    location = " url " {"
    print "        alias " asset ";"
    print "        default_type application/gzip;"
    print "    }"
    print ""
    done=1
  }
  { print }
' "$conf" > /tmp/headscale-with-rk-deploy.conf
install -m 0644 /tmp/headscale-with-rk-deploy.conf "$conf"
/www/server/nginx/sbin/nginx -t
/www/server/nginx/sbin/nginx -s reload -c /www/server/nginx/conf/nginx.conf

systemctl is-active frps
pgrep -x nginx >/dev/null
ss -lntp | grep -E ':(10925|18082)\b' || true
echo "DEPLOY_URL=https://$headscale_domain/$deploy_id.tar.gz"
