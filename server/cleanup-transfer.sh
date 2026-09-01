#!/usr/bin/env bash
set -euo pipefail

deploy_id=${1:-}
if [[ ! "$deploy_id" =~ ^[0-9a-f]{32}$ ]]; then
  echo "invalid deployment id" >&2
  exit 2
fi

headscale_domain=${HEADSCALE_DOMAIN:?set HEADSCALE_DOMAIN}

conf="/www/server/panel/vhost/nginx/$headscale_domain.conf"
if [[ -f /tmp/headscale-before-rk-deploy.conf ]]; then
  install -m 0644 /tmp/headscale-before-rk-deploy.conf "$conf"
fi
rm -f "/run/$deploy_id.tar.gz" /tmp/headscale-with-rk-deploy.conf
/www/server/nginx/sbin/nginx -t
/www/server/nginx/sbin/nginx -s reload -c /www/server/nginx/conf/nginx.conf
