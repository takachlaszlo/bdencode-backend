#!/usr/bin/env bash
set -Eeuo pipefail

umask 027

if [[ "$(id -u)" -eq 0 ]]; then
    echo "Run this installer as the target account (for example accofil), not as root." >&2
    echo "It will use sudo only for packages and system configuration." >&2
    exit 2
fi

task_user="$(id -un)"
task_group="$(id -gn)"
task_home="$(getent passwd "$task_user" | cut -d: -f6)"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
data_root="${BDENCODE_DATA_ROOT:-$task_home/encode}"
source_root="${BDENCODE_SOURCE_ROOT:-$task_home/storage}"
app_root="$data_root/app"
release_id="$(date -u +%Y%m%dT%H%M%SZ)-$$"
release_root="$app_root/releases/$release_id"
tool_release="$app_root/tools/releases/$release_id"
tool_config="$tool_release/config"
logical_cpus="$(getconf _NPROCESSORS_ONLN)"
cpu_percent="${BDENCODE_CPU_PERCENT:-80}"

if [[ ! "$cpu_percent" =~ ^[0-9]+$ ]] || ((cpu_percent < 1 || cpu_percent > 100)); then
    echo "BDENCODE_CPU_PERCENT must be an integer between 1 and 100" >&2
    exit 2
fi
cpu_quota="$((logical_cpus * cpu_percent))%"

if [[ -z "$task_home" || "$task_home" == "/" || "$data_root" == "/" ]]; then
    echo "Refusing an unsafe home/data path" >&2
    exit 2
fi
if [[ ! -d "$source_root" ]]; then
    echo "Source root does not exist: $source_root" >&2
    exit 2
fi

install -d -m 0750 \
    "$data_root" "$data_root/state" "$data_root/state/backups" \
    "$data_root/state/overrides" "$data_root/jobs" "$data_root/completed" \
    "$data_root/cache" "$data_root/cache/build" "$data_root/updates" "$app_root/releases" \
    "$app_root/tools/releases"
install -d -m 0700 "$task_home/.config/bdencode"

exec 9>"$data_root/state/deployment.lock"
flock -x 9

api_was_active=0
worker_was_active=0
timer_was_active=0
api_was_enabled=0
worker_was_enabled=0
timer_was_enabled=0
sudo systemctl is-active --quiet bdencode-api.service && api_was_active=1
sudo systemctl is-active --quiet bdencode-worker.service && worker_was_active=1
sudo systemctl is-active --quiet bdencode-update.timer && timer_was_active=1
sudo systemctl is-enabled --quiet bdencode-api.service && api_was_enabled=1
sudo systemctl is-enabled --quiet bdencode-worker.service && worker_was_enabled=1
sudo systemctl is-enabled --quiet bdencode-update.timer && timer_was_enabled=1
previous_app="$(readlink -f "$app_root/current" 2>/dev/null || true)"
previous_tools="$(readlink -f "$app_root/tools/current" 2>/dev/null || true)"
activated=0
succeeded=0

finish() {
    local status=$?
    if [[ "$succeeded" -ne 1 && "$activated" -eq 1 ]]; then
        if [[ -n "$previous_app" ]]; then
            ln -sfn "$previous_app" "$app_root/.current-rollback"
            mv -Tf "$app_root/.current-rollback" "$app_root/current"
        else
            rm -f "$app_root/current"
        fi
        if [[ -n "$previous_tools" ]]; then
            ln -sfn "$previous_tools" "$app_root/tools/.current-rollback"
            mv -Tf "$app_root/tools/.current-rollback" "$app_root/tools/current"
        else
            rm -f "$app_root/tools/current"
        fi
    fi
    if [[ "$succeeded" -ne 1 ]]; then
        sudo systemctl daemon-reload || true
        restore_unit_state bdencode-api.service "$api_was_active" "$api_was_enabled"
        restore_unit_state bdencode-worker.service "$worker_was_active" "$worker_was_enabled"
        restore_unit_state bdencode-update.timer "$timer_was_active" "$timer_was_enabled"
    fi
    exit "$status"
}

restore_unit_state() {
    local unit="$1" was_active="$2" was_enabled="$3"
    if [[ "$was_active" -eq 1 ]]; then
        sudo systemctl start "$unit" || true
    else
        sudo systemctl stop "$unit" || true
    fi
    if [[ "$was_enabled" -eq 1 ]]; then
        sudo systemctl enable "$unit" || true
    else
        sudo systemctl disable "$unit" || true
    fi
}
trap finish EXIT

# Close the public administrative claim path before checking an existing queue.
# The worker shares deployment.lock around each claim, so it cannot start a new
# job while this installer owns the exclusive lock.
if [[ "$api_was_active" -eq 1 ]]; then
    sudo systemctl stop bdencode-api.service
fi
if [[ -x "$app_root/current/venv/bin/bdencode" ]]; then
    set +e
    env \
        BDENCODE_CONFIG=/etc/bdencode/config.toml \
        PATH="$app_root/tools/current/bin:$app_root/current/venv/bin:/usr/local/bin:/usr/bin:/bin" \
        XDG_CACHE_HOME="$data_root/cache" \
        XDG_CONFIG_HOME="$app_root/tools/current/config" \
        "$app_root/current/venv/bin/bdencode" queue-idle
    idle_status=$?
    set -e
    if [[ "$idle_status" -eq 3 ]]; then
        echo "The encode queue is active; installation was deferred without changing it." >&2
        exit 3
    fi
    if [[ "$idle_status" -ne 0 ]]; then
        echo "Unable to verify the existing queue state (exit=$idle_status)." >&2
        exit "$idle_status"
    fi
fi
if [[ "$worker_was_active" -eq 1 ]]; then
    sudo systemctl stop bdencode-worker.service
fi

sudo apt-get update
sudo apt-get install -y --no-install-recommends \
    build-essential ca-certificates curl ffmpeg git libbluray-bin libbluray-dev \
    mediainfo meson mkvtoolnix nasm ninja-build pkg-config python3-pip \
    python3-venv sqlite3 util-linux x264 x265 xxd

make -C "$repo_root/native" clean all
sudo make -C "$repo_root/native" install PREFIX=/usr/local

python3 -m venv "$release_root/venv"
"$release_root/venv/bin/python" -m pip install --disable-pip-version-check --upgrade pip wheel
"$release_root/venv/bin/python" -m pip install --disable-pip-version-check "$repo_root[test,language]"
"$release_root/venv/bin/python" -m pytest -q "$repo_root/tests"

# Current VapourSynth and BestSource require Python >=3.12. uv installs a
# dedicated managed interpreter without replacing Debian's system Python.
"$release_root/venv/bin/python" -m pip install --disable-pip-version-check "uv>=0.8,<1"
export UV_PYTHON_INSTALL_DIR="$app_root/tools/python"
export UV_CACHE_DIR="$data_root/cache/uv"
"$release_root/venv/bin/uv" python install 3.12
"$release_root/venv/bin/uv" venv --python 3.12 "$tool_release"
"$release_root/venv/bin/uv" pip install --python "$tool_release/bin/python" \
    --requirement "$repo_root/tools/requirements-vapoursynth.lock"
install -d -m 0750 "$tool_config"
XDG_CONFIG_HOME="$tool_config" "$tool_release/bin/vapoursynth" config
XDG_CONFIG_HOME="$tool_config" "$tool_release/bin/vspipe" --version
XDG_CONFIG_HOME="$tool_config" "$tool_release/bin/python" -c \
    'from vapoursynth import core; assert all(hasattr(core,n) for n in ("bs","bwdif","vivtc","resize"))'

# Official standalone VMAF avoids replacing Debian FFmpeg. The exact upstream
# commit is pinned; the installed CLI is statically linked and reads aligned
# Y4M named pipes without materializing raw video.
vmaf_commit="6375a4be62fd2673bba2c356b26867d8af01a685"
vmaf_source="$data_root/cache/build/vmaf-$release_id"
git clone --filter=blob:none --no-checkout https://github.com/Netflix/vmaf.git "$vmaf_source"
git -C "$vmaf_source" checkout --detach "$vmaf_commit"
test "$(git -C "$vmaf_source" rev-parse HEAD)" = "$vmaf_commit"
meson setup --buildtype release --default-library=static --prefix "$tool_release/vmaf" \
    "$vmaf_source/libvmaf/build" "$vmaf_source/libvmaf"
ninja -C "$vmaf_source/libvmaf/build"
ninja -C "$vmaf_source/libvmaf/build" test
ninja -C "$vmaf_source/libvmaf/build" install
ln -s "$tool_release/vmaf/bin/vmaf" "$tool_release/bin/vmaf"
"$tool_release/bin/vmaf" --version

ln -sfn "$release_root" "$app_root/.current-new"
mv -Tf "$app_root/.current-new" "$app_root/current"
ln -sfn "$tool_release" "$app_root/tools/.current-new"
mv -Tf "$app_root/tools/.current-new" "$app_root/tools/current"
activated=1

sudo install -d -m 0755 /etc/bdencode /usr/local/libexec
if [[ ! -e /etc/bdencode/config.toml ]]; then
    sudo install -m 0640 -o root -g "$task_group" \
        "$repo_root/config/config.example.toml" /etc/bdencode/config.toml
    sudo sed -i \
        -e "s|/home/accofil/encode|$data_root|g" \
        -e "s|/home/accofil/storage|$source_root|g" \
        -e "s|cpu_limit_percent = 80|cpu_limit_percent = $cpu_percent|g" \
        /etc/bdencode/config.toml
else
    configured_values="$(python3 -c 'import sys,tomllib; d=tomllib.load(open(sys.argv[1],"rb"))["bdencode"]; print(d["data_root"]); print(d["source_roots"][0]); print(d["cpu_limit_percent"]); print(d["bind_host"]); print(d["bind_port"]); print(d["api_root_path"])' /etc/bdencode/config.toml)"
    expected_values="$data_root"$'\n'"$source_root"$'\n'"$cpu_percent"$'\n'"127.0.0.1"$'\n'"8796"$'\n'"/encoder"
    if [[ "$configured_values" != "$expected_values" ]]; then
        echo "Existing /etc/bdencode/config.toml conflicts with the rendered data/source/CPU/nginx settings." >&2
        exit 2
    fi
fi
sudo chown "root:$task_group" /etc/bdencode/config.toml
sudo chmod 0640 /etc/bdencode/config.toml

render_unit() {
    local source="$1"
    local destination="$2"
    sudo sed \
        -e "s|@USER@|$task_user|g" \
        -e "s|@GROUP@|$task_group|g" \
        -e "s|@DATA_ROOT@|$data_root|g" \
        -e "s|@SOURCE_ROOT@|$source_root|g" \
        -e "s|@APP_ROOT@|$app_root|g" \
        -e "s|@CPU_QUOTA@|$cpu_quota|g" \
        "$source" | sudo tee "$destination" >/dev/null
    sudo chmod 0644 "$destination"
}

render_unit "$repo_root/deploy/systemd/bdencode-api.service.in" /etc/systemd/system/bdencode-api.service
render_unit "$repo_root/deploy/systemd/bdencode-worker.service.in" /etc/systemd/system/bdencode-worker.service
render_unit "$repo_root/deploy/systemd/bdencode-update.service.in" /etc/systemd/system/bdencode-update.service
sudo install -m 0644 "$repo_root/deploy/systemd/bdencode-update.timer" /etc/systemd/system/bdencode-update.timer
sudo install -m 0755 "$repo_root/install/daily-update.sh" /usr/local/libexec/bdencode-daily-update

credential="$task_home/.config/bdencode/imgbb-api-key.cred"
sudo install -d -m 0755 /etc/systemd/system/bdencode-worker.service.d
if [[ -s "$credential" ]]; then
    sudo systemd-creds decrypt --name=imgbb-api-key "$credential" /dev/null
    printf '[Service]\nLoadCredentialEncrypted=imgbb-api-key:%s\n' "$credential" \
        | sudo tee /etc/systemd/system/bdencode-worker.service.d/credential.conf >/dev/null
    sudo chmod 0644 /etc/systemd/system/bdencode-worker.service.d/credential.conf
else
    sudo rm -f /etc/systemd/system/bdencode-worker.service.d/credential.conf
fi

if [[ -d /etc/nginx/apps && -f /etc/htpasswd ]]; then
    nginx_target=/etc/nginx/apps/bdencode.conf
    nginx_new=/etc/nginx/apps/.bdencode.conf.new
    nginx_backup=/etc/nginx/apps/.bdencode.conf.rollback
    sudo sed -e 's|@PORT@|8796|g' "$repo_root/deploy/nginx/bdencode.conf.in" \
        | sudo tee "$nginx_new" >/dev/null
    if sudo test -e "$nginx_target"; then
        sudo cp -a "$nginx_target" "$nginx_backup"
    else
        sudo rm -f "$nginx_backup"
    fi
    sudo mv -f "$nginx_new" "$nginx_target"
    if ! sudo nginx -t; then
        if sudo test -e "$nginx_backup"; then
            sudo mv -f "$nginx_backup" "$nginx_target"
        else
            sudo rm -f "$nginx_target"
        fi
        exit 1
    fi
    sudo rm -f "$nginx_backup"
    sudo systemctl reload nginx
else
    echo "Warning: Swizzin nginx apps directory or /etc/htpasswd is missing; /encoder was not installed." >&2
fi

sudo systemctl daemon-reload
runtime_path="$app_root/tools/current/bin:$app_root/current/venv/bin:/usr/local/bin:/usr/bin:/bin"
env \
    BDENCODE_CONFIG=/etc/bdencode/config.toml \
    PATH="$runtime_path" \
    XDG_CACHE_HOME="$data_root/cache" \
    XDG_CONFIG_HOME="$app_root/tools/current/config" \
    "$app_root/current/venv/bin/bdencode" init-db
env \
    BDENCODE_CONFIG=/etc/bdencode/config.toml \
    PATH="$runtime_path" \
    XDG_CACHE_HOME="$data_root/cache" \
    XDG_CONFIG_HOME="$app_root/tools/current/config" \
    "$app_root/current/venv/bin/bdencode" doctor --json
sudo systemctl enable --now bdencode-api.service bdencode-worker.service bdencode-update.timer
sudo systemctl is-active --quiet bdencode-api.service
sudo systemctl is-active --quiet bdencode-worker.service
sudo systemctl is-active --quiet bdencode-update.timer
api_ready=0
for _attempt in {1..20}; do
    if curl --fail --silent --show-error \
        http://127.0.0.1:8796/api/v1/health >/dev/null; then
        api_ready=1
        break
    fi
    sleep 0.5
done
if [[ "$api_ready" -ne 1 ]]; then
    echo "BDEncode API did not become healthy after service start" >&2
    exit 1
fi
succeeded=1

echo "BDEncode installed at $app_root/current"
echo "Worker CPUQuota=$cpu_quota (${cpu_percent}% of ${logical_cpus} logical CPUs)"
