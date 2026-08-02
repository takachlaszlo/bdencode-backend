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
apt_transaction=/usr/local/libexec/bdencode-apt-transaction
runtime_transaction=/usr/local/libexec/bdencode-update-runtime
install_transaction=/usr/local/libexec/bdencode-install-transaction
release_id="$(date -u +%Y%m%dT%H%M%SZ)-$$"

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

if [[ ! -x "$apt_transaction" || ! -x "$runtime_transaction" || \
    ! -x "$install_transaction" ]]; then
    echo "Missing update transaction helper" >&2
    exit 1
fi

pause_apt_timers() {
    local unit service
    for unit in apt-daily.timer apt-daily-upgrade.timer; do
        if systemctl is-active --quiet "$unit"; then
            systemctl stop "$unit"
        fi
    done
    # Never kill dpkg midway. A running oneshot is ActiveState=activating, which
    # `systemctl is-active` does not treat as active on systemd 252.
    for service in apt-daily.service apt-daily-upgrade.service; do
        for _attempt in {1..120}; do
            service_state="$(systemctl show --property=ActiveState --value "$service")"
            case "$service_state" in
                inactive|failed) break ;;
            esac
            sleep 1
        done
        service_state="$(systemctl show --property=ActiveState --value "$service")"
        case "$service_state" in
            inactive|failed) ;;
            *)
            echo "Timed out waiting for $service; media update deferred" >&2
            return 1
            ;;
        esac
    done
}

unit_should_run() {
    local active_state
    active_state="$(systemctl show --property=ActiveState --value "$1")"
    case "$active_state" in
        active|reloading|activating|deactivating) return 0 ;;
        *) return 1 ;;
    esac
}

# The updater can be the first process to acquire deployment.lock after an
# installer SIGKILL. Finish that fixed-target recovery before reading any app
# pointer or opening a package transaction.
install_status="$("$install_transaction" status)"
if [[ "$install_status" == *'"active": true'* ]]; then
    "$install_transaction" recover >>"$report_file" 2>&1
    systemctl daemon-reload
    if command -v nginx >/dev/null; then
        nginx -t
        if systemctl is-active --quiet nginx.service; then
            systemctl reload nginx.service
        fi
    fi
fi

# Quiesce native APT only when recovery is actually pending. A stale runtime
# journal already contains the timer/service pre-state. In the defensive
# APT-only case, synthesize that durable runtime journal before stopping a
# timer, so every interruption remains recoverable.
runtime_status="$("$runtime_transaction" status)"
apt_status="$("$apt_transaction" status)"
if [[ "$runtime_status" == *'"active": true'* ]]; then
    pause_apt_timers
    "$runtime_transaction" recover --restore-runtime >>"$report_file" 2>&1
elif [[ "$apt_status" == *'"active": true'* ]]; then
    emergency_id="$(python3 -c \
        'import json,sys; print(json.loads(sys.stdin.read())["transaction_id"])' \
        <<<"$apt_status")"
    emergency_api=0
    emergency_worker=0
    emergency_apt_daily=0
    emergency_apt_upgrade=0
    unit_should_run bdencode-api.service && emergency_api=1
    unit_should_run bdencode-worker.service && emergency_worker=1
    unit_should_run apt-daily.timer && emergency_apt_daily=1
    unit_should_run apt-daily-upgrade.timer && emergency_apt_upgrade=1
    "$runtime_transaction" begin \
        --release-id "$emergency_id" \
        --data-root "$data_root" \
        --task-user "$task_user" \
        --previous-tools "$(readlink -f "$current_tools")" \
        --api-active "$emergency_api" \
        --worker-active "$emergency_worker" \
        --apt-daily-active "$emergency_apt_daily" \
        --apt-upgrade-active "$emergency_apt_upgrade" \
        >>"$report_file" 2>&1
    "$runtime_transaction" apt-prepared >>"$report_file" 2>&1
    pause_apt_timers
    "$runtime_transaction" recover --restore-runtime >>"$report_file" 2>&1
fi

api_was_active=0
worker_was_active=0
apt_daily_was_active=0
apt_upgrade_was_active=0
unit_should_run bdencode-api.service && api_was_active=1
unit_should_run bdencode-worker.service && worker_was_active=1
unit_should_run apt-daily.timer && apt_daily_was_active=1
unit_should_run apt-daily-upgrade.timer && apt_upgrade_was_active=1
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

wait_for_api() {
    local api_healthy=0
    for _attempt in {1..20}; do
        if curl --fail --silent --show-error --max-time 2 \
            http://127.0.0.1:8796/api/v1/health >/dev/null; then
            api_healthy=1
            break
        fi
        sleep 1
    done
    [[ "$api_healthy" -eq 1 ]]
}

start_validation_services() {
    if [[ "$api_was_active" -eq 1 ]]; then
        systemctl start bdencode-api.service
        systemctl is-active --quiet bdencode-api.service
        wait_for_api
    fi
    if [[ "$worker_was_active" -eq 1 ]]; then
        # Type=notify makes this block until the worker has initialized its
        # database and acquired its singleton lock.
        systemctl start bdencode-worker.service
        systemctl is-active --quiet bdencode-worker.service
    fi
}

stop_validation_services() {
    if [[ "$worker_was_active" -eq 1 ]]; then
        systemctl stop bdencode-worker.service
    fi
    if [[ "$api_was_active" -eq 1 ]]; then
        systemctl stop bdencode-api.service
    fi
}

finish() {
    local status=$?
    local recovery_failed=0
    trap - EXIT
    set +e

    if [[ "$status" -ne 0 || "$succeeded" -ne 1 ]]; then
        if ! "$runtime_transaction" recover --restore-runtime >>"$report_file" 2>&1; then
            recovery_failed=1
        elif [[ -n "$tool_release" && -d "$tool_release" && \
            "$(readlink -f "$current_tools" 2>/dev/null)" != "$(readlink -f "$tool_release")" ]]; then
            safe_remove_tool_release "$tool_release" || recovery_failed=1
        fi
        status=1
    fi
    if [[ "$recovery_failed" -ne 0 ]]; then
        status=1
    fi
    if [[ "$status" -ne 0 ]]; then
        printf '%s update failed (recovery_failed=%s)\n' \
            "$(date -u +%FT%TZ)" "$recovery_failed" >>"$report_file"
    fi
    exit "$status"
}
trap finish EXIT

"$runtime_transaction" begin \
    --release-id "$release_id" \
    --data-root "$data_root" \
    --task-user "$task_user" \
    --previous-tools "$previous_tools" \
    --api-active "$api_was_active" \
    --worker-active "$worker_was_active" \
    --apt-daily-active "$apt_daily_was_active" \
    --apt-upgrade-active "$apt_upgrade_was_active" \
    >>"$report_file" 2>&1

# Close the public administrative claim path before checking the queue.  The
# worker shares deployment.lock around every claim, so it cannot claim a new
# job while this update owns the lock.
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
    "$runtime_transaction" recover --restore-runtime >>"$report_file" 2>&1
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

pause_apt_timers
"$apt_transaction" recover >>"$report_file" 2>&1
"$apt_transaction" prepare --transaction-id "$release_id" >>"$report_file" 2>&1
"$runtime_transaction" apt-prepared >>"$report_file" 2>&1
apt_changed=0
apt_status="$("$apt_transaction" status)"
if [[ "$apt_status" == *'"active": true'* ]]; then
    apt_changed=1
fi
"$apt_transaction" apply >>"$report_file" 2>&1
"$apt_transaction" validate >>"$report_file" 2>&1

tool_release="$app_root/tools/releases/$release_id"
"$runtime_transaction" staging --candidate-tools "$tool_release" \
    >>"$report_file" 2>&1
candidate_config="$tool_release/config"
export UV_PYTHON_INSTALL_DIR="$tool_release/.python"
export UV_CACHE_DIR="$data_root/cache/uv"
runuser -u "$task_user" -- env \
    UV_PYTHON_INSTALL_DIR="$UV_PYTHON_INSTALL_DIR" UV_CACHE_DIR="$UV_CACHE_DIR" \
    "$current_backend/venv/bin/uv" python install --no-bin --upgrade 3.12
runuser -u "$task_user" -- env \
    UV_PYTHON_INSTALL_DIR="$UV_PYTHON_INSTALL_DIR" UV_CACHE_DIR="$UV_CACHE_DIR" \
    "$current_backend/venv/bin/uv" venv --allow-existing --managed-python \
    --no-python-downloads --python 3.12 "$tool_release"
runuser -u "$task_user" -- env \
    UV_PYTHON_INSTALL_DIR="$UV_PYTHON_INSTALL_DIR" UV_CACHE_DIR="$UV_CACHE_DIR" \
    "$current_backend/venv/bin/uv" pip install \
    --python "$tool_release/bin/python" --upgrade \
    'VapourSynth>=77,<78' 'vapoursynth-bestsource>=20,<21' \
    'vapoursynth-bwdif>=5.1,<6' 'vapoursynth-vivtc>=2,<3'
install -d -m 0750 -o "$task_user" -g "$(id -gn "$task_user")" "$candidate_config"
runuser -u "$task_user" -- env XDG_CONFIG_HOME="$candidate_config" \
    "$tool_release/bin/vapoursynth" config

# Build the native scanner into the versioned candidate, never /usr/local.
native_build="$tool_release/.native-build"
install -d -m 0750 "$native_build"
test -f "$current_backend/native/Makefile"
test -f "$current_backend/native/libbluray_scan.c"
cp "$current_backend/native/Makefile" "$current_backend/native/libbluray_scan.c" "$native_build/"
make -C "$native_build" \
    REPRO_CFLAGS="-g0 -ffile-prefix-map=$native_build=." clean all
make -C "$native_build" install PREFIX="$tool_release"
rm -rf -- "$native_build"

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
runuser -u "$task_user" -- "$tool_release/bin/bdencode-libbluray-scan" --help \
    >>"$report_file" 2>&1

# Exercise the upgraded codecs and shared libraries, not only --version output.
runuser -u "$task_user" -- env PATH="$candidate_path" ffmpeg -v error -nostdin \
    -f lavfi -i 'testsrc2=size=128x72:rate=24' -frames:v 8 -threads 2 \
    -c:v libx264 -f null - >>"$report_file" 2>&1
runuser -u "$task_user" -- env PATH="$candidate_path" ffmpeg -v error -nostdin \
    -f lavfi -i 'testsrc2=size=128x72:rate=24' -frames:v 8 -threads 2 \
    -pix_fmt yuv420p10le -c:v libx265 \
    -x265-params 'pools=2:frame-threads=1:log-level=error' -f null - \
    >>"$report_file" 2>&1
runuser -u "$task_user" -- env \
    BDENCODE_CONFIG=/etc/bdencode/config.toml \
    PATH="$candidate_path" \
    XDG_CACHE_HOME="$data_root/cache" \
    XDG_CONFIG_HOME="$candidate_config" \
    "$current_backend/venv/bin/bdencode" doctor --json >>"$report_file"

change_check="$tool_release/.bdencode-change-check"
install -d -m 0700 "$change_check"
current_freeze="$change_check/current-freeze.txt"
candidate_freeze="$change_check/candidate-freeze.txt"
current_python_target="$change_check/current-python-target.txt"
candidate_python_target="$change_check/candidate-python-target.txt"
current_python_version="$change_check/current-python-version.txt"
candidate_python_version="$change_check/candidate-python-version.txt"

# These commands deliberately run outside process substitutions. With `set -e`
# any inspection failure aborts the transaction instead of looking like two
# equal empty outputs and discarding a real candidate.
runuser -u "$task_user" -- "$current_backend/venv/bin/uv" pip freeze \
    --python "$current_tools/bin/python" >"$current_freeze"
runuser -u "$task_user" -- "$current_backend/venv/bin/uv" pip freeze \
    --python "$tool_release/bin/python" >"$candidate_freeze"
current_tools_resolved="$(readlink -f "$current_tools")"
candidate_tools_resolved="$(readlink -f "$tool_release")"
current_python_resolved="$(readlink -f "$current_tools/bin/python")"
candidate_python_resolved="$(readlink -f "$tool_release/bin/python")"
case "$candidate_python_resolved" in
    "$candidate_tools_resolved"/.python/*) ;;
    *)
        echo "Candidate Python escaped its versioned tool release" >&2
        exit 1
        ;;
esac
# Relative canonical targets are stable across release IDs. A legacy venv that
# still follows the old shared minor alias intentionally compares different and
# is migrated to the release-local layout even when its patch version is equal.
realpath --relative-to="$current_tools_resolved" "$current_python_resolved" \
    >"$current_python_target"
realpath --relative-to="$candidate_tools_resolved" "$candidate_python_resolved" \
    >"$candidate_python_target"
runuser -u "$task_user" -- "$current_tools/bin/python" -VV \
    >"$current_python_version"
runuser -u "$task_user" -- "$tool_release/bin/python" -VV \
    >"$candidate_python_version"

tool_changed=1
candidate_unchanged=0
if [[ "$apt_changed" -eq 0 ]] && \
    cmp -s "$current_freeze" "$candidate_freeze" && \
    cmp -s "$current_python_target" "$candidate_python_target" && \
    cmp -s "$current_python_version" "$candidate_python_version" && \
    cmp -s "$current_tools/bin/python" "$tool_release/bin/python" && \
    [[ -x "$current_tools/bin/bdencode-libbluray-scan" ]] && \
    cmp -s "$current_tools/bin/bdencode-libbluray-scan" \
        "$tool_release/bin/bdencode-libbluray-scan"; then
    candidate_unchanged=1
fi
rm -f -- "$current_freeze" "$candidate_freeze" \
    "$current_python_target" "$candidate_python_target" \
    "$current_python_version" "$candidate_python_version"
rmdir -- "$change_check"

if [[ "$candidate_unchanged" -eq 1 ]]; then
    printf '%s no VapourSynth/plugin/native-scanner changes\n' \
        "$(date -u +%FT%TZ)" >>"$report_file"
    safe_remove_tool_release "$tool_release"
    tool_release=""
    "$runtime_transaction" candidate-discarded >>"$report_file" 2>&1
    tool_changed=0
fi

if [[ "$tool_changed" -eq 1 ]]; then
    # Only a fully tested candidate becomes current.
    "$runtime_transaction" activating --candidate-tools "$tool_release" \
        >>"$report_file" 2>&1
    ln -sfn "$tool_release" "$app_root/tools/.current-new"
    mv -Tf "$app_root/tools/.current-new" "$current_tools"
    activated=1
    printf '%s update activated: %s\n' "$(date -u +%FT%TZ)" "$release_id" >>"$report_file"
fi

# APT remains rollbackable until the real services have started successfully.
start_validation_services
stop_validation_services
"$runtime_transaction" healthy >>"$report_file" 2>&1
"$apt_transaction" commit >>"$report_file" 2>&1
"$runtime_transaction" commit >>"$report_file" 2>&1
# Restore timers and clear the committed journal even when an administrator
# runs this script directly; ExecStopPost remains a second recovery boundary.
"$runtime_transaction" recover --restore-runtime >>"$report_file" 2>&1
succeeded=1

# Retention is intentionally post-commit and best-effort.  It must never turn a
# healthy, committed runtime into a rollback attempt.
set +e
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
                printf '%s retention skipped unexpected VMAF target: %s\n' \
                    "$(date -u +%FT%TZ)" "$vmaf_target" >>"$report_file"
                protected_releases["$release"]=1
                ;;
        esac
    fi
done
for release in "${releases[@]}"; do
    resolved_release="$(readlink -f "$release")"
    if [[ -n "${protected_releases[$resolved_release]+x}" ]]; then
        continue
    fi
    safe_remove_tool_release "$resolved_release" || \
        printf '%s retention could not remove %s\n' "$(date -u +%FT%TZ)" "$resolved_release" \
            >>"$report_file"
done
set -e
