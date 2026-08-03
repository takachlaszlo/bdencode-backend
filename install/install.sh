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
install_transaction_source="$repo_root/install/install_transaction.py"
installer_apt_lock=/run/lock/bdencode-installer-apt.lock
data_root="${BDENCODE_DATA_ROOT:-$task_home/encode}"
source_root="${BDENCODE_SOURCE_ROOT:-$task_home/storage}"
app_root="$data_root/app"
frontend_dist="$repo_root/frontend/dist"
frontend_root=/var/www/bdencode
release_id="$(date -u +%Y%m%dT%H%M%SZ)-$$"
release_root="$app_root/releases/$release_id"
tool_release="$app_root/tools/releases/$release_id"
frontend_release="$frontend_root/releases/$release_id"
tool_config="$tool_release/config"
logical_cpus="$(getconf _NPROCESSORS_ONLN)"
cpu_percent="${BDENCODE_CPU_PERCENT:-80}"

if [[ ! "$cpu_percent" =~ ^[0-9]+$ ]] || ((cpu_percent < 1 || cpu_percent > 100)); then
    echo "BDENCODE_CPU_PERCENT must be an integer between 1 and 100" >&2
    exit 2
fi
cpu_quota="$((logical_cpus * cpu_percent))%"

unit_should_run() {
    local active_state
    active_state="$(sudo systemctl show --property=ActiveState --value "$1")"
    case "$active_state" in
        active|reloading|activating|deactivating) return 0 ;;
        *) return 1 ;;
    esac
}

pause_apt_timers() {
    local unit service_state
    for unit in apt-daily.timer apt-daily-upgrade.timer; do
        if unit_should_run "$unit"; then
            sudo systemctl stop "$unit"
        fi
    done
    for unit in apt-daily.service apt-daily-upgrade.service; do
        for _attempt in {1..120}; do
            service_state="$(sudo systemctl show --property=ActiveState --value "$unit")"
            case "$service_state" in
                inactive|failed) break ;;
                *) sleep 1 ;;
            esac
        done
        service_state="$(sudo systemctl show --property=ActiveState --value "$unit")"
        case "$service_state" in
            inactive|failed) ;;
            *)
                echo "Timed out waiting for $unit; installation deferred" >&2
                return 1
                ;;
        esac
    done
}

recover_stale_update() {
    local runtime_status apt_status emergency_id previous_tools
    local emergency_api=0 emergency_worker=0
    local emergency_apt_daily=0 emergency_apt_upgrade=0
    runtime_status="$(sudo /usr/local/libexec/bdencode-update-runtime status)"
    apt_status="$(sudo /usr/local/libexec/bdencode-apt-transaction status)"
    if [[ "$runtime_status" != *'"active": true'* && \
        "$apt_status" == *'"active": true'* ]]; then
        emergency_id="$(python3 -c \
            'import json,sys; print(json.loads(sys.stdin.read())["transaction_id"])' \
            <<<"$apt_status")"
        unit_should_run bdencode-api.service && emergency_api=1
        unit_should_run bdencode-worker.service && emergency_worker=1
        unit_should_run apt-daily.timer && emergency_apt_daily=1
        unit_should_run apt-daily-upgrade.timer && emergency_apt_upgrade=1
        previous_tools="$(readlink -f "$app_root/tools/current" 2>/dev/null || true)"
        if [[ -z "$previous_tools" ]]; then
            echo "Cannot recover APT without a current tool release" >&2
            return 1
        fi
        sudo /usr/local/libexec/bdencode-update-runtime begin \
            --release-id "$emergency_id" \
            --data-root "$data_root" \
            --task-user "$task_user" \
            --previous-tools "$previous_tools" \
            --api-active "$emergency_api" \
            --worker-active "$emergency_worker" \
            --apt-daily-active "$emergency_apt_daily" \
            --apt-upgrade-active "$emergency_apt_upgrade"
        sudo /usr/local/libexec/bdencode-update-runtime apt-prepared
        runtime_status="$(sudo /usr/local/libexec/bdencode-update-runtime status)"
    fi
    if [[ "$runtime_status" == *'"active": true'* ]]; then
        pause_apt_timers
        sudo /usr/local/libexec/bdencode-update-runtime recover --restore-runtime
    elif [[ "$apt_status" == *'"active": true'* ]]; then
        echo "APT recovery is active without a runtime recovery journal" >&2
        return 1
    fi
}

if [[ -z "$task_home" || "$task_home" == "/" || "$data_root" == "/" ]]; then
    echo "Refusing an unsafe home/data path" >&2
    exit 2
fi
if [[ ! -d "$source_root" ]]; then
    echo "Source root does not exist: $source_root" >&2
    exit 2
fi
if [[ ! -d "$frontend_dist" || ! -s "$frontend_dist/index.html" ]]; then
    echo "Missing prebuilt frontend release: $frontend_dist/index.html" >&2
    echo "Build and commit frontend/dist before running the server installer." >&2
    exit 2
fi
frontend_invalid=""
if ! frontend_invalid="$(
    find "$frontend_dist" -mindepth 1 ! -type d ! -type f -print -quit
)"; then
    echo "Could not validate frontend dist" >&2
    exit 2
fi
if [[ -n "$frontend_invalid" ]]; then
    echo "Frontend dist may contain only regular files and directories" >&2
    exit 2
fi
if ! grep -Fq '/encoder/' "$frontend_dist/index.html"; then
    echo "Frontend dist was not built for the required /encoder/ base path" >&2
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

# An interrupted earlier installer may have left new pointers and only part of
# its host configuration. Restore its fixed-target snapshot before consulting
# the daily-update journal or recording this run's service state.
stale_install_status="$(sudo python3 "$install_transaction_source" status)"
if [[ "$stale_install_status" == *'"active": true'* ]]; then
    sudo python3 "$install_transaction_source" recover
    sudo systemctl daemon-reload
    if command -v nginx >/dev/null; then
        sudo nginx -t
        if sudo systemctl is-active --quiet nginx.service; then
            sudo systemctl reload nginx.service
        fi
    fi
fi

api_was_active=0
worker_was_active=0
timer_was_active=0
apt_daily_was_active=0
apt_upgrade_was_active=0
api_was_enabled=0
worker_was_enabled=0
timer_was_enabled=0
apt_daily_was_enabled=0
apt_upgrade_was_enabled=0
succeeded=0
install_txn_started=0
api_stopped_for_check=0

finish() {
    local status=$?
    local recovery_failed=0
    trap - EXIT
    set +e
    if [[ "$succeeded" -ne 1 ]]; then
        if [[ "$install_txn_started" -eq 1 ]]; then
            if ! sudo python3 "$install_transaction_source" recover; then
                recovery_failed=1
            fi
        elif [[ "$api_stopped_for_check" -eq 1 && "$api_was_active" -eq 1 ]]; then
            sudo systemctl start bdencode-api.service || recovery_failed=1
        fi
        if [[ "$recovery_failed" -eq 0 ]]; then
            sudo systemctl daemon-reload || recovery_failed=1
            if command -v nginx >/dev/null; then
                sudo nginx -t || recovery_failed=1
                if [[ "$recovery_failed" -eq 0 ]] && \
                    sudo systemctl is-active --quiet nginx.service; then
                    sudo systemctl reload nginx.service || recovery_failed=1
                fi
            fi
        fi
        if [[ "$recovery_failed" -ne 0 ]]; then
            status=1
            echo "Installer rollback requires manual recovery; services remain stopped." >&2
        fi
    fi
    exit "$status"
}

atomic_root_install() {
    local source="$1" destination="$2" mode="$3"
    local temporary="${destination}.new-${release_id}"
    sudo install -m "$mode" "$source" "$temporary"
    sudo sync -f "$temporary"
    sudo mv -Tf "$temporary" "$destination"
    sudo sync -f "$(dirname "$destination")"
}

render_unit_atomic() {
    local source="$1" destination="$2"
    local temporary="${destination}.new-${release_id}"
    sudo sed \
        -e "s|@USER@|$task_user|g" \
        -e "s|@GROUP@|$task_group|g" \
        -e "s|@DATA_ROOT@|$data_root|g" \
        -e "s|@SOURCE_ROOT@|$source_root|g" \
        -e "s|@APP_ROOT@|$app_root|g" \
        -e "s|@CPU_QUOTA@|$cpu_quota|g" \
        "$source" | sudo tee "$temporary" >/dev/null
    sudo chmod 0644 "$temporary"
    sudo sync -f "$temporary"
    sudo mv -Tf "$temporary" "$destination"
    sudo sync -f "$(dirname "$destination")"
}

wait_for_api() {
    for _attempt in {1..20}; do
        if curl --fail --silent --show-error --max-time 2 \
            http://127.0.0.1:8796/api/v1/health >/dev/null; then
            return 0
        fi
        sleep 0.5
    done
    return 1
}

queue_is_install_safe() {
    local queue_command queue_help queue_output queue_status legacy_cli
    local -a queue_args
    queue_command="$app_root/current/venv/bin/bdencode"
    legacy_cli=1

    # The first deployment of --allow-review necessarily invokes a previous
    # release which does not know the option yet. Detect support without opening
    # the database, then keep the compatibility path fail-closed and restricted
    # to the old CLI's exact AWAITING_SELECTION busy result.
    if ! queue_help="$("$queue_command" queue-idle --help 2>&1)"; then
        echo "Unable to inspect the installed queue-idle command" >&2
        return 1
    fi
    queue_args=(queue-idle)
    if [[ "$queue_help" == *"--allow-review"* ]]; then
        queue_args+=(--allow-review)
        legacy_cli=0
    fi

    queue_status=0
    queue_output="$(
        env \
            BDENCODE_CONFIG=/etc/bdencode/config.toml \
            PATH="$app_root/tools/current/bin:$app_root/current/venv/bin:/usr/local/bin:/usr/bin:/bin" \
            XDG_CACHE_HOME="$data_root/cache" \
            XDG_CONFIG_HOME="$app_root/tools/current/config" \
            "$queue_command" "${queue_args[@]}" 2>&1
    )" || queue_status=$?
    if [[ -n "$queue_output" ]]; then
        printf '%s\n' "$queue_output" >&2
    fi
    if [[ "$queue_status" -eq 0 ]]; then
        return 0
    fi
    if [[ "$legacy_cli" -eq 1 && "$queue_status" -eq 3 && \
        "$queue_output" =~ ^busy:\ [^[:space:]]+\ AWAITING_SELECTION$ ]]; then
        echo "Legacy queue gate accepted the persisted AWAITING_SELECTION pause" >&2
        return 0
    fi
    return "$queue_status"
}
trap finish EXIT

# Publish a stable, atomically replaced recovery bootstrap before any pointer
# or host-file mutation. These files deliberately remain outside rollback so a
# second outage can always resume the same journal format.
sudo install -d -m 0755 /etc/systemd/system /usr/local/libexec /var/lib/bdencode \
    /etc/systemd/system/bdencode-update.service.d \
    /etc/systemd/system/bdencode-api.service.d \
    /etc/systemd/system/bdencode-worker.service.d
atomic_root_install "$repo_root/install/install_transaction.py" \
    /usr/local/libexec/bdencode-install-transaction 0755
atomic_root_install "$repo_root/install/apt_transaction.py" \
    /usr/local/libexec/bdencode-apt-transaction 0755
atomic_root_install "$repo_root/install/apt_transaction.py" \
    /usr/local/libexec/bdencode-apt-guard 0755
atomic_root_install "$repo_root/install/update_runtime.py" \
    /usr/local/libexec/bdencode-update-runtime 0755
atomic_root_install "$repo_root/install/update-recover.sh" \
    /usr/local/libexec/bdencode-update-recover 0755
atomic_root_install "$repo_root/install/recovery-check.sh" \
    /usr/local/libexec/bdencode-recovery-check 0755
render_unit_atomic "$repo_root/deploy/systemd/bdencode-update-recovery.service.in" \
    /etc/systemd/system/bdencode-update-recovery.service
atomic_root_install "$repo_root/deploy/systemd/bdencode-update-preflight.conf" \
    /etc/systemd/system/bdencode-update.service.d/bdencode-recovery.conf 0644
render_unit_atomic "$repo_root/deploy/systemd/bdencode-runtime-recovery.conf.in" \
    /etc/systemd/system/bdencode-api.service.d/bdencode-recovery.conf
render_unit_atomic "$repo_root/deploy/systemd/bdencode-runtime-recovery.conf.in" \
    /etc/systemd/system/bdencode-worker.service.d/bdencode-recovery.conf
render_unit_atomic "$repo_root/deploy/systemd/bdencode-install-recovery.service.in" \
    /etc/systemd/system/bdencode-install-recovery.service
atomic_root_install "$repo_root/deploy/systemd/bdencode-install-recovery.path" \
    /etc/systemd/system/bdencode-install-recovery.path 0644
sudo systemctl daemon-reload
sudo systemctl enable bdencode-update-recovery.service \
    bdencode-install-recovery.path
sudo systemctl restart bdencode-install-recovery.path
sudo systemctl is-active --quiet bdencode-install-recovery.path

# Finish an interrupted daily transaction before recording the service state
# this installation must preserve. APT oneshots are always allowed to finish.
recover_stale_update

unit_should_run bdencode-api.service && api_was_active=1
unit_should_run bdencode-worker.service && worker_was_active=1
unit_should_run bdencode-update.timer && timer_was_active=1
unit_should_run apt-daily.timer && apt_daily_was_active=1
unit_should_run apt-daily-upgrade.timer && apt_upgrade_was_active=1
sudo systemctl is-enabled --quiet bdencode-api.service && api_was_enabled=1
sudo systemctl is-enabled --quiet bdencode-worker.service && worker_was_enabled=1
sudo systemctl is-enabled --quiet bdencode-update.timer && timer_was_enabled=1
sudo systemctl is-enabled --quiet apt-daily.timer && apt_daily_was_enabled=1
sudo systemctl is-enabled --quiet apt-daily-upgrade.timer && apt_upgrade_was_enabled=1

sudo /usr/local/libexec/bdencode-install-transaction begin \
    --transaction-id "$release_id" --app-root "$app_root" \
    --api-active "$api_was_active" --api-enabled "$api_was_enabled" \
    --worker-active "$worker_was_active" --worker-enabled "$worker_was_enabled" \
    --timer-active "$timer_was_active" --timer-enabled "$timer_was_enabled" \
    --apt-daily-active "$apt_daily_was_active" \
    --apt-daily-enabled "$apt_daily_was_enabled" \
    --apt-upgrade-active "$apt_upgrade_was_active" \
    --apt-upgrade-enabled "$apt_upgrade_was_enabled"
install_txn_started=1
sudo systemctl stop bdencode-update.timer || true
# A timer race may have queued the update preflight on our deployment lock.
# Queue its cancellation without waiting for ExecStopPost, which deliberately
# needs the same lock and would otherwise deadlock the installer.
sudo systemctl --no-block stop bdencode-update.service || true

# The state-restoration decision is now durable. Close the administrative API
# before checking the queue; the worker cannot claim while fd 9 is exclusive.
if [[ "$api_was_active" -eq 1 ]]; then
    sudo systemctl stop bdencode-api.service
    api_stopped_for_check=1
fi
if [[ -x "$app_root/current/venv/bin/bdencode" ]]; then
    set +e
    queue_is_install_safe
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
worker_state="$(sudo systemctl show --property=ActiveState --value bdencode-worker.service)"
case "$worker_state" in
    inactive|failed) ;;
    *)
        echo "Worker did not stop before installation mutation: $worker_state" >&2
        exit 1
        ;;
esac
pause_apt_timers
sudo /usr/local/libexec/bdencode-install-transaction prepare

# Publish the media pin and APT recovery gates before invoking apt-get. They
# are mutable installer targets, so a later failure restores their snapshot.
sudo install -d -m 0755 /etc/bdencode /etc/apt/preferences.d \
    /etc/systemd/system/apt-daily.service.d \
    /etc/systemd/system/apt-daily-upgrade.service.d
sudo install -d -m 0711 /var/lib/bdencode/apt-transactions
atomic_root_install "$repo_root/install/media-apt.sources.list" \
    /etc/bdencode/media-apt.sources.list 0644
atomic_root_install "$repo_root/install/bdencode-media.pref" \
    /etc/apt/preferences.d/bdencode-media 0644
for apt_unit in apt-daily.service apt-daily-upgrade.service; do
    atomic_root_install "$repo_root/deploy/systemd/bdencode-apt-recovery.conf" \
        "/etc/systemd/system/$apt_unit.d/bdencode-recovery.conf" 0644
done
sudo systemctl daemon-reload

sudo flock -x "$installer_apt_lock" apt-get update
sudo flock -x "$installer_apt_lock" apt-get \
    -o Dir::Etc::preferences=/dev/null \
    -o Dir::Etc::preferencesparts=/dev/null \
    install -y --no-install-recommends --no-upgrade \
    build-essential ca-certificates curl ffmpeg git libbluray-bin libbluray-dev \
    dpkg-repack mediainfo meson mkvtoolnix nasm ninja-build pkg-config python3-pip \
    python3-venv sqlite3 util-linux x264 x265 xxd

python3 -m venv "$release_root/venv"
"$release_root/venv/bin/python" -m pip install --disable-pip-version-check --upgrade pip wheel
"$release_root/venv/bin/python" -m pip install --disable-pip-version-check "$repo_root[test,language]"
# The installer itself accepts BDENCODE_DATA_ROOT and related runtime
# overrides.  The test suite creates isolated temporary configurations, so
# allowing those values to leak into pytest would make CLI tests open the live
# deployment database instead of their temporary database.
env \
    -u BDENCODE_CONFIG \
    -u BDENCODE_CONFIG_PATH \
    -u BDENCODE_DATA_ROOT \
    -u BDENCODE_DATABASE_PATH \
    -u BDENCODE_DB_PATH \
    -u BDENCODE_SOURCE_ROOT \
    -u BDENCODE_SOURCE_ROOTS \
    -u BDENCODE_CPU_PERCENT \
    -u BDENCODE_CPU_LIMIT_PERCENT \
    -u BDENCODE_BIND_HOST \
    -u BDENCODE_BIND_PORT \
    -u BDENCODE_API_ROOT_PATH \
    -u BDENCODE_WORKER_POLL_SECONDS \
    -u BDENCODE_COMPARISON_PAIR_COUNT \
    -u BDENCODE_COMPARISON_FRAMES_PER_TYPE \
    -u BDENCODE_LOG_LEVEL \
    "$release_root/venv/bin/python" -m pytest -q "$repo_root/tests"

# Current VapourSynth and BestSource require Python >=3.12. uv installs a
# dedicated managed interpreter without replacing Debian's system Python.
"$release_root/venv/bin/python" -m pip install --disable-pip-version-check "uv>=0.8,<1"
export UV_PYTHON_INSTALL_DIR="$tool_release/.python"
export UV_CACHE_DIR="$data_root/cache/uv"
"$release_root/venv/bin/uv" python install --no-bin --upgrade 3.12
"$release_root/venv/bin/uv" venv --allow-existing --managed-python \
    --no-python-downloads --python 3.12 "$tool_release"
"$release_root/venv/bin/uv" pip install --python "$tool_release/bin/python" \
    --requirement "$repo_root/tools/requirements-vapoursynth.lock"
install -d -m 0750 "$tool_config"
XDG_CONFIG_HOME="$tool_config" "$tool_release/bin/vapoursynth" config
XDG_CONFIG_HOME="$tool_config" "$tool_release/bin/vspipe" --version
XDG_CONFIG_HOME="$tool_config" "$tool_release/bin/python" -c \
    'from vapoursynth import core; assert all(hasattr(core,n) for n in ("bs","bwdif","vivtc","resize"))'

# Keep the native scanner inside the immutable tool release.  It still uses
# Debian's libbluray, whose complete package transaction is now rollbackable.
make -C "$repo_root/native" \
    REPRO_CFLAGS="-g0 -ffile-prefix-map=$repo_root/native=." clean all
make -C "$repo_root/native" install PREFIX="$tool_release"
install -d -m 0750 "$release_root/native"
install -m 0644 "$repo_root/native/Makefile" "$repo_root/native/libbluray_scan.c" \
    "$release_root/native/"
test -f "$release_root/native/Makefile"
test -f "$release_root/native/libbluray_scan.c"

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

# Publish immutable, root-owned static assets outside the private encode tree.
# The current web pointer is part of the durable installer snapshot, so a crash
# after activation restores the frontend together with backend/tool pointers.
sudo install -d -m 0755 "$frontend_root" "$frontend_root/releases"
if sudo test -e "$frontend_release" || sudo test -L "$frontend_release"; then
    echo "Frontend release already exists: $frontend_release" >&2
    exit 1
fi
sudo install -d -m 0755 "$frontend_release" "$frontend_release/encoder"
sudo cp -a "$frontend_dist/." "$frontend_release/encoder/"
frontend_invalid=""
if ! frontend_invalid="$(
    sudo find "$frontend_release" -mindepth 1 ! -type d ! -type f -print -quit
)"; then
    echo "Could not validate published frontend" >&2
    exit 1
fi
if [[ -n "$frontend_invalid" ]]; then
    echo "Published frontend contains an unsafe filesystem object" >&2
    exit 1
fi
sudo chown -R root:root "$frontend_release"
sudo find "$frontend_release" -type d -exec chmod 0755 {} +
sudo find "$frontend_release" -type f -exec chmod 0644 {} +
sudo test ! -L "$frontend_release/encoder/index.html"
sudo test -s "$frontend_release/encoder/index.html"
sudo diff -qr "$frontend_dist" "$frontend_release/encoder"
sudo sync -f "$frontend_release"

ln -sfn "$release_root" "$app_root/.current-new"
mv -Tf "$app_root/.current-new" "$app_root/current"
ln -sfn "$tool_release" "$app_root/tools/.current-new"
mv -Tf "$app_root/tools/.current-new" "$app_root/tools/current"
frontend_new="$frontend_root/.current-new-$release_id"
sudo ln -s "$frontend_release" "$frontend_new"
sudo mv -Tf "$frontend_new" "$frontend_root/current"
sudo sync -f "$frontend_root"

sudo install -d -m 0755 /etc/bdencode /usr/local/libexec /var/lib/bdencode
sudo install -d -m 0711 /var/lib/bdencode/apt-transactions
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

render_unit_atomic "$repo_root/deploy/systemd/bdencode-api.service.in" \
    /etc/systemd/system/bdencode-api.service
render_unit_atomic "$repo_root/deploy/systemd/bdencode-worker.service.in" \
    /etc/systemd/system/bdencode-worker.service
render_unit_atomic "$repo_root/deploy/systemd/bdencode-update.service.in" \
    /etc/systemd/system/bdencode-update.service
atomic_root_install "$repo_root/deploy/systemd/bdencode-update.timer" \
    /etc/systemd/system/bdencode-update.timer 0644
atomic_root_install "$repo_root/install/daily-update.sh" \
    /usr/local/libexec/bdencode-daily-update 0755
sudo systemd-analyze verify \
    /etc/systemd/system/bdencode-api.service \
    /etc/systemd/system/bdencode-worker.service \
    /etc/systemd/system/bdencode-update.service \
    /etc/systemd/system/bdencode-update-recovery.service \
    /etc/systemd/system/bdencode-install-recovery.service \
    /etc/systemd/system/bdencode-install-recovery.path \
    /etc/systemd/system/bdencode-update.timer \
    apt-daily.service apt-daily-upgrade.service

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
    sudo sed \
        -e 's|@PORT@|8796|g' \
        -e "s|@FRONTEND_ROOT@|$frontend_root/current|g" \
        "$repo_root/deploy/nginx/bdencode.conf.in" \
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
sudo systemctl enable bdencode-update-recovery.service \
    bdencode-api.service bdencode-worker.service bdencode-update.timer
sudo systemctl start bdencode-update-recovery.service
sudo systemctl start bdencode-api.service bdencode-worker.service
sudo systemctl is-active --quiet bdencode-api.service
sudo systemctl is-active --quiet bdencode-worker.service
if ! wait_for_api; then
    echo "BDEncode API did not become healthy after service start" >&2
    exit 1
fi

# Validate the new runtime while the durable marker and exclusive deployment
# lock prevent claims, then stop it again before recording HEALTHY. Recovery of
# HEALTHY finalizes the tested candidate. The new worker also checks every
# maintenance marker before a claim, including while its start job is queued.
sudo systemctl stop bdencode-worker.service bdencode-api.service
sudo /usr/local/libexec/bdencode-install-transaction healthy
sudo /usr/local/libexec/bdencode-install-transaction recover
install_txn_started=0
sudo systemctl start bdencode-update-recovery.service \
    bdencode-api.service bdencode-worker.service bdencode-update.timer
sudo systemctl is-active --quiet bdencode-update-recovery.service
sudo systemctl is-active --quiet bdencode-api.service
sudo systemctl is-active --quiet bdencode-worker.service
sudo systemctl is-active --quiet bdencode-update.timer
if ! wait_for_api; then
    echo "Committed BDEncode API did not remain healthy" >&2
    exit 1
fi
succeeded=1

echo "BDEncode installed at $app_root/current"
echo "Worker CPUQuota=$cpu_quota (${cpu_percent}% of ${logical_cpus} logical CPUs)"
