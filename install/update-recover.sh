#!/usr/bin/env bash
set -Eeuo pipefail

mode="${1:---finalize}"
case "$mode" in
    --gate|--finalize|--preflight) ;;
    *)
        echo "Usage: $0 [--gate|--finalize|--preflight]" >&2
        exit 2
        ;;
esac

task_user="${BDENCODE_USER:-accofil}"
task_home="$(getent passwd "$task_user" | cut -d: -f6)"
if [[ -z "$task_home" ]]; then
    echo "Unknown BDEncode account: $task_user" >&2
    exit 1
fi
data_root="${BDENCODE_DATA_ROOT:-$task_home/encode}"
app_root="$data_root/app"
deployment_lock="$data_root/state/deployment.lock"
install_transaction=/usr/local/libexec/bdencode-install-transaction
runtime_transaction=/usr/local/libexec/bdencode-update-runtime
apt_transaction=/usr/local/libexec/bdencode-apt-transaction

install -d -m 0750 "$data_root/state"
touch "$deployment_lock"
exec 9>"$deployment_lock"

if [[ "$mode" == "--gate" ]]; then
    # API/worker starts initiated by the updater itself arrive here while the
    # updater owns the lock. They are validation starts, not a recovery cue.
    if ! flock -n 9; then
        exit 0
    fi
else
    # The install watchdog deliberately waits here while install.sh is alive.
    # SIGKILL closes its descriptor, handing this process the recovery lock.
    flock -x 9
fi

unit_should_run() {
    local active_state
    active_state="$(systemctl show --property=ActiveState --value "$1")"
    case "$active_state" in
        active|reloading|activating|deactivating) return 0 ;;
        *) return 1 ;;
    esac
}

pause_apt_timers() {
    local unit service_state
    for unit in apt-daily.timer apt-daily-upgrade.timer; do
        if unit_should_run "$unit"; then
            systemctl stop "$unit"
        fi
    done
    # APT/dpkg must finish naturally; killing it would make package recovery
    # less safe than waiting. ActiveState=activating is common for oneshots.
    for unit in apt-daily.service apt-daily-upgrade.service; do
        for _attempt in {1..120}; do
            service_state="$(systemctl show --property=ActiveState --value "$unit")"
            case "$service_state" in
                inactive|failed) break ;;
                *) sleep 1 ;;
            esac
        done
        service_state="$(systemctl show --property=ActiveState --value "$unit")"
        case "$service_state" in
            inactive|failed) ;;
            *)
                echo "Timed out waiting for $unit; recovery remains pending" >&2
                return 1
                ;;
        esac
    done
}

install_recovered=0
install_marker=/var/lib/bdencode/install-transactions/active
install_services_marker=/var/lib/bdencode/install-transactions/services-pending
if [[ ( -e "$install_marker" || -L "$install_marker" || \
        -e "$install_services_marker" || -L "$install_services_marker" ) && \
    ! -x "$install_transaction" ]]; then
    echo "Install recovery journal exists but its stable helper is unavailable" >&2
    exit 1
fi
if [[ -x "$install_transaction" ]]; then
    install_status="$("$install_transaction" status)"
    if [[ "$install_status" == *'"active": true'* ]]; then
        "$install_transaction" recover
        install_recovered=1
        systemctl daemon-reload
        if command -v nginx >/dev/null; then
            nginx -t
            if systemctl is-active --quiet nginx.service; then
                systemctl reload nginx.service
            fi
        fi
    fi
fi

if [[ "$mode" == "--preflight" ]]; then
    # A loaded ExecStart may belong to the interrupted installer candidate.
    # Otherwise daily-update performs runtime/APT recovery after it has
    # durably captured the complete pre-state.
    [[ "$install_recovered" -eq 0 ]] || exit 75
    exit 0
fi
if [[ ! -x "$runtime_transaction" || ! -x "$apt_transaction" ]]; then
    if [[ "$install_recovered" -eq 1 ]]; then
        exit 0
    fi
    echo "No complete BDEncode update recovery helper set is installed" >&2
    exit 1
fi

runtime_status="$("$runtime_transaction" status)"
apt_status="$("$apt_transaction" status)"
if [[ "$runtime_status" != *'"active": true'* && \
    "$apt_status" == *'"active": true'* ]]; then
    # Defensive compatibility path for a crash between publishing the APT and
    # runtime journals. Persist every state before stopping a single unit.
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
    previous_tools="$(readlink -f "$app_root/tools/current" 2>/dev/null || true)"
    if [[ -z "$previous_tools" ]]; then
        echo "Cannot synthesize update recovery without a current tool release" >&2
        exit 1
    fi
    "$runtime_transaction" begin \
        --release-id "$emergency_id" \
        --data-root "$data_root" \
        --task-user "$task_user" \
        --previous-tools "$previous_tools" \
        --api-active "$emergency_api" \
        --worker-active "$emergency_worker" \
        --apt-daily-active "$emergency_apt_daily" \
        --apt-upgrade-active "$emergency_apt_upgrade"
    "$runtime_transaction" apt-prepared
    runtime_status="$("$runtime_transaction" status)"
fi

if [[ "$runtime_status" == *'"active": true'* ]]; then
    pause_apt_timers
    if [[ "$mode" == "--finalize" ]]; then
        "$runtime_transaction" recover --restore-runtime
    else
        "$runtime_transaction" recover --restore-timers
    fi
elif [[ "$apt_status" == *'"active": true'* ]]; then
    # This is unreachable after successful synthesis; retain an explicit
    # fail-closed assertion instead of touching dpkg without a pre-state.
    echo "APT recovery is active without a runtime recovery journal" >&2
    exit 1
fi
