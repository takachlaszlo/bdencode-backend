#!/usr/bin/env bash
set -Eeuo pipefail

umask 027

if [[ "$(id -u)" -eq 0 ]]; then
    echo "Run the WSL installer as the configured Linux user, not root." >&2
    exit 2
fi
if ! grep -qi microsoft /proc/sys/kernel/osrelease; then
    echo "This installer is intended for WSL2." >&2
    exit 2
fi
if [[ -z "${BDENCODE_SOURCE_ROOT:-}" || ! -d "$BDENCODE_SOURCE_ROOT" ]]; then
    echo "BDENCODE_SOURCE_ROOT must name an existing Windows-mounted directory." >&2
    exit 2
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
listen_port="${BDENCODE_WINDOWS_PORT:-8787}"
if [[ ! "$listen_port" =~ ^[0-9]+$ ]] || \
    ((listen_port < 1024 || listen_port > 65535 || listen_port == 8796)); then
    echo "BDENCODE_WINDOWS_PORT must be an unprivileged port other than 8796." >&2
    exit 2
fi

if ! systemctl show-environment >/dev/null 2>&1; then
    echo "systemd is not active in this WSL distribution." >&2
    echo "Set [boot] systemd=true in /etc/wsl.conf, terminate WSL, then retry." >&2
    exit 2
fi

sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    nginx curl ca-certificates

BDENCODE_SOURCE_ROOT="$BDENCODE_SOURCE_ROOT" \
BDENCODE_CPU_PERCENT="${BDENCODE_CPU_PERCENT:-80}" \
    bash "$repo_root/install/install.sh"

frontend_root=/var/www/bdencode
nginx_target=/etc/nginx/conf.d/bdencode-wsl.conf
nginx_temporary="$(mktemp)"
sed \
    -e "s|@LISTEN_PORT@|$listen_port|g" \
    -e 's|@BACKEND_PORT@|8796|g' \
    -e "s|@FRONTEND_ROOT@|$frontend_root/current|g" \
    "$repo_root/deploy/nginx/bdencode-standalone.conf.in" >"$nginx_temporary"
if grep -Eq '@(LISTEN_PORT|BACKEND_PORT|FRONTEND_ROOT)@' "$nginx_temporary"; then
    echo "Standalone nginx template was not fully rendered." >&2
    rm -f -- "$nginx_temporary"
    exit 1
fi
sudo install -o root -g root -m 0644 "$nginx_temporary" "$nginx_target"
rm -f -- "$nginx_temporary"
sudo nginx -t
sudo systemctl enable nginx.service
sudo systemctl restart nginx.service

health_url="http://127.0.0.1:${listen_port}/encoder/api/v1/health"
healthy=0
for _attempt in {1..30}; do
    if curl -fsS "$health_url" >/dev/null; then
        healthy=1
        break
    fi
    sleep 1
done
if [[ "$healthy" -ne 1 ]]; then
    echo "The Windows-local web endpoint did not become healthy: $health_url" >&2
    exit 1
fi

printf 'BDEncode Windows/WSL installation is healthy.\n'
printf 'Web: http://localhost:%s/encoder/\n' "$listen_port"
printf 'Completed files: %s/completed\n' "${BDENCODE_DATA_ROOT:-$HOME/encode}"
