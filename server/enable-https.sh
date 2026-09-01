#!/usr/bin/env bash
set -euo pipefail

domain=${CLOUD_DOMAIN:?set CLOUD_DOMAIN, for example cloud.example.com}
expected=${SERVER_PUBLIC_IP:?set SERVER_PUBLIC_IP}
deploy_dir=${DEPLOY_DIR:-/root/rk-cloud-deploy}
webroot=${WEBROOT:-/www/wwwroot/$domain}
vhost=${VHOST_PATH:-/www/server/panel/vhost/nginx/$domain.conf}
resolved=$(getent ahostsv4 "$domain" | awk '{print $1}' | sort -u)
if ! grep -Fxq "$expected" <<<"$resolved"; then
  echo "$domain is not publicly resolving to $expected yet" >&2
  exit 2
fi

certbot certonly --webroot \
  --webroot-path "$webroot" \
  --domain "$domain" \
  --non-interactive --agree-tos --register-unsafely-without-email \
  --keep-until-expiring

install -m 0644 "$deploy_dir/cloud-unavailable.html" \
  "$webroot/cloud-unavailable.html"
install -m 0644 "$deploy_dir/cloud.https.conf" "$vhost"
/www/server/nginx/sbin/nginx -t
/www/server/nginx/sbin/nginx -s reload -c /www/server/nginx/conf/nginx.conf

for _ in 1 2 3 4 5; do
  if curl --fail --silent --show-error --resolve "$domain:443:127.0.0.1" \
    --head "https://$domain/login" | sed -n '1,8p'; then
    exit 0
  fi
  sleep 2
done
exit 1
