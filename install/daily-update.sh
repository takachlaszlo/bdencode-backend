#!/usr/bin/env bash
set -Eeuo pipefail

umask 027
task_user="${BDENCODE_USER:-accofil}"
task_home="$(getent passwd "$task_user" | cut -d: -f6)"
data_root="${BDENCODE_DATA_ROOT:-$task_home/encode}"
app_root="$data_root/app"
current_backend="$app_root/current"
current_tools="$app_root/tools/current"
deployment_lock="$data_root/state/deployment.lock"
report_file="$data_root/updates/daily-update.log"

install -d -m 0750 -o "$task_user" -g "$(id -gn "$task_user")" \
    "$data_root/state" "$data_root/updates" "$app_root/tools/releases"
touch "$report_file"
chown "$task_user:$(id -gn "$task_user")" "$report_file"
chmod 0640 "$report_file"
touch "$deployment_lock"
chown "$task_user:$(id -gn "$task_user")" "$deployment_lock"
chmod 0640 "$deployment_lock"

exec 9>"$deployment_lock"
flock -x 9

api_was_active=0
worker_was_active=0
systemctl is-active --quiet bdencode-api.service && api_was_active=1
systemctl is-active --quiet bdencode-worker.service && worker_was_active=1
previous_tools="$(readlink -f "$current_tools" 2>/dev/null || true)"
tool_release=""
activated=0
succeeded=0

safe_remove_tool_release() {
    local target
    target="$(readlink -m "$1")"
    case "$target" in
        "$app_root"/tools/releases/*) rm -rf -- "$target" ;;
        *) echo "Refusing to remove unsafe tool release path: $target" >&2; return 1 ;;
    esac
}

finish() {
    local status=$?
    local restart_failed=0
    if [[ "$succeeded" -ne 1 && "$activated" -eq 1 && -n "$previous_tools" ]]; then
        ln -sfn "$previous_tools" "$app_root/tools/.current-rollback"
        mv -Tf "$app_root/tools/.current-rollback" "$current_tools"
    fi
    if [[ "$succeeded" -ne 1 && -n "$tool_release" && -d "$tool_release" ]]; then
        safe_remove_tool_release "$tool_release" || true
    fi
    if [[ "$worker_was_active" -eq 1 ]]; then
        if ! systemctl start bdencode-worker.service; then
            restart_failed=1
        elif ! systemctl is-active --quiet bdencode-worker.service; then
            restart_failed=1
        fi
    fi
    if [[ "$api_was_active" -eq 1 ]]; then
        if ! systemctl start bdencode-api.service; then
            restart_failed=1
        else
            api_healthy=0
            for _attempt in {1..20}; do
                if curl --fail --silent --show-error --max-time 2 \
                    http://127.0.0.1:8796/api/v1/health >/dev/null; then
                    api_healthy=1
                    break
                fi
                sleep 1
            done
            if [[ "$api_healthy" -ne 1 ]]; then
                restart_failed=1
            fi
        fi
    fi
    if [[ "$status" -eq 0 && "$restart_failed" -ne 0 ]]; then
        status=1
    fi
    if [[ "$status" -ne 0 ]]; then
        printf '%s update failed (exit=%s)\n' "$(date -u +%FT%TZ)" "$status" >>"$report_file"
    fi
    exit "$status"
}
trap finish EXIT

# Stop the API first so its administrative claim endpoint cannot race the idle
# check. The worker observes deployment.lock around every database claim.
if [[ "$api_was_active" -eq 1 ]]; then
    systemctl stop bdencode-api.service
fi

set +e
runuser -u "$task_user" -- env \
    BDENCODE_CONFIG=/etc/bdencode/config.toml \
    PATH="$current_tools/bin:$current_backend/venv/bin:/usr/local/bin:/usr/bin:/bin" \
    XDG_CACHE_HOME="$data_root/cache" \
    XDG_CONFIG_HOME="$current_tools/config" \
    "$current_backend/venv/bin/bdencode" queue-idle
idle_status=$?
set -e
if [[ "$idle_status" -eq 3 ]]; then
    printf '%s queue busy; update deferred\n' "$(date -u +%FT%TZ)" >>"$report_file"
    succeeded=1
    exit 0
fi
if [[ "$idle_status" -ne 0 ]]; then
    echo "queue-idle failed operationally (exit=$idle_status)" >&2
    exit "$idle_status"
fi

if [[ "$worker_was_active" -eq 1 ]]; then
    systemctl stop bdencode-worker.service
fi

apt-get update
apt-get install -y --only-upgrade \
    ffmpeg libbluray-bin mediainfo mkvtoolnix x264 x265

release_id="$(date -u +%Y%m%dT%H%M%SZ)-$$"
tool_release="$app_root/tools/releases/$release_id"
candidate_config="$tool_release/config"
export UV_PYTHON_INSTALL_DIR="$app_root/tools/python"
export UV_CACHE_DIR="$data_root/cache/uv"
runuser -u "$task_user" -- env \
    UV_PYTHON_INSTALL_DIR="$UV_PYTHON_INSTALL_DIR" UV_CACHE_DIR="$UV_CACHE_DIR" \
    "$current_backend/venv/bin/uv" python install 3.12
runuser -u "$task_user" -- env \
    UV_PYTHON_INSTALL_DIR="$UV_PYTHON_INSTALL_DIR" UV_CACHE_DIR="$UV_CACHE_DIR" \
    "$current_backend/venv/bin/uv" venv --python 3.12 "$tool_release"
runuser -u "$task_user" -- env \
    UV_PYTHON_INSTALL_DIR="$UV_PYTHON_INSTALL_DIR" UV_CACHE_DIR="$UV_CACHE_DIR" \
    "$current_backend/venv/bin/uv" pip install \
    --python "$tool_release/bin/python" --upgrade \
    'VapourSynth>=77,<78' 'vapoursynth-bestsource>=20,<21' \
    'vapoursynth-bwdif>=5.1,<6' 'vapoursynth-vivtc>=2,<3'
install -d -m 0750 -o "$task_user" -g "$(id -gn "$task_user")" "$candidate_config"
runuser -u "$task_user" -- env XDG_CONFIG_HOME="$candidate_config" \
    "$tool_release/bin/vapoursynth" config

if [[ -x "$current_tools/vmaf/bin/vmaf" ]]; then
    ln -s "$(readlink -f "$current_tools/vmaf")" "$tool_release/vmaf"
    ln -s "$tool_release/vmaf/bin/vmaf" "$tool_release/bin/vmaf"
fi

candidate_path="$tool_release/bin:$current_backend/venv/bin:/usr/local/bin:/usr/bin:/bin"
runuser -u "$task_user" -- env XDG_CONFIG_HOME="$candidate_config" \
    "$tool_release/bin/vspipe" --version >>"$report_file" 2>&1
runuser -u "$task_user" -- env XDG_CONFIG_HOME="$candidate_config" \
    "$tool_release/bin/python" -c \
    'from vapoursynth import core; assert all(hasattr(core,n) for n in ("bs","bwdif","vivtc","resize"))'
runuser -u "$task_user" -- "$tool_release/bin/vmaf" --version >>"$report_file" 2>&1
runuser -u "$task_user" -- env \
    BDENCODE_CONFIG=/etc/bdencode/config.toml \
    PATH="$candidate_path" \
    XDG_CACHE_HOME="$data_root/cache" \
    XDG_CONFIG_HOME="$candidate_config" \
    "$current_backend/venv/bin/bdencode" doctor --json >>"$report_file"

if diff -q \
    <(runuser -u "$task_user" -- "$current_tools/bin/python" -m pip freeze) \
    <(runuser -u "$task_user" -- "$tool_release/bin/python" -m pip freeze) \
    >/dev/null; then
    printf '%s no VapourSynth/plugin version changes\n' "$(date -u +%FT%TZ)" >>"$report_file"
    safe_remove_tool_release "$tool_release"
    tool_release=""
    succeeded=1
    exit 0
fi

# Only a fully tested candidate becomes current.
ln -sfn "$tool_release" "$app_root/tools/.current-new"
mv -Tf "$app_root/tools/.current-new" "$current_tools"
activated=1
printf '%s update activated: %s\n' "$(date -u +%FT%TZ)" "$release_id" >>"$report_file"

# Retain current plus the two newest previous tool releases. Every VMAF owner
# used by those rollback candidates is additionally protected because plugin
# releases link to an immutable VMAF build owned by another release directory.
current_release="$(readlink -f "$current_tools")"
mapfile -t releases < <(
    find "$app_root/tools/releases" -mindepth 1 -maxdepth 1 -type d \
        -printf '%T@ %p\n' | sort -nr | cut -d' ' -f2-
)
declare -A protected_releases=()
protected_releases["$current_release"]=1
previous_kept=0
for release in "${releases[@]}"; do
    resolved_release="$(readlink -f "$release")"
    if [[ "$resolved_release" == "$current_release" ]]; then
        continue
    fi
    if ((previous_kept < 2)); then
        protected_releases["$resolved_release"]=1
        previous_kept=$((previous_kept + 1))
    fi
done
for release in "${!protected_releases[@]}"; do
    if [[ -e "$release/vmaf" ]]; then
        vmaf_target="$(readlink -f "$release/vmaf")"
        case "$vmaf_target" in
            "$app_root"/tools/releases/*/vmaf)
                protected_releases["$(dirname "$vmaf_target")"]=1
                ;;
            *)
                echo "Refusing unexpected VMAF target during retention: $vmaf_target" >&2
                exit 1
                ;;
        esac
    fi
done
for release in "${releases[@]}"; do
    resolved_release="$(readlink -f "$release")"
    if [[ -n "${protected_releases[$resolved_release]+x}" ]]; then
        continue
    fi
    safe_remove_tool_release "$resolved_release"
done
succeeded=1
