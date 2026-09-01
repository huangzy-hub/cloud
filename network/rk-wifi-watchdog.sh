#!/usr/bin/env bash
set -euo pipefail

env_file=/etc/rk-cloud/wifi-watchdog.env
[[ -r "$env_file" ]] || exit 0
# shellcheck disable=SC1090
source "$env_file"

connection=${WIFI_CONNECTION_NAME:?set WIFI_CONNECTION_NAME}
headscale_host=${HEADSCALE_HOST:?set HEADSCALE_HOST}
server_ip=${SERVER_PUBLIC_IP:?set SERVER_PUBLIC_IP}

if ! ip -4 route get "$server_ip" >/dev/null 2>&1; then
  nmcli connection up "$connection" >/dev/null 2>&1 || true
fi

if ! getent ahostsv4 "$headscale_host" >/dev/null 2>&1; then
  nmcli networking off >/dev/null 2>&1 || true
  sleep 2
  nmcli networking on >/dev/null 2>&1 || true
  nmcli connection up "$connection" >/dev/null 2>&1 || true
fi

systemctl is-active --quiet tailscaled || systemctl restart tailscaled
systemctl is-active --quiet frpc-cloud || systemctl restart frpc-cloud
