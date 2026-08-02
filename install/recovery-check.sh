#!/usr/bin/env bash
set -Eeuo pipefail

mode="${1:---runtime}"
case "$mode" in
    --runtime|--apt) ;;
    *) echo "Usage: $0 [--runtime|--apt]" >&2; exit 2 ;;
esac

if [[ "$mode" == "--runtime" ]]; then
    task_user="${BDENCODE_USER:-accofil}"
    task_home="$(getent passwd "$task_user" | cut -d: -f6)"
    if [[ -z "$task_home" ]]; then
        echo "Unknown BDEncode runtime account: $task_user" >&2
        exit 1
    fi
    data_root="${BDENCODE_DATA_ROOT:-$task_home/encode}"
    deployment_lock="$data_root/state/deployment.lock"
    # Runtime starts performed by the installer/updater are validation steps
    # under the exclusive deployment lock. All other starts must fail closed
    # while any durable mutation journal is still published.
    exec 9>"$deployment_lock"
    # A shared probe still succeeds beside the worker's short shared claim
    # lock; only the updater/installer's exclusive lock authorizes bypass.
    if ! flock -s -n 9; then
        exit 0
    fi
fi

markers=(
    /var/lib/bdencode/install-transactions/active
    /var/lib/bdencode/update-runtime/active.json
    /var/lib/bdencode/apt-transactions/active
)
for marker in "${markers[@]}"; do
    if [[ -e "$marker" || -L "$marker" ]]; then
        echo "BDEncode recovery is incomplete: $marker" >&2
        exit 1
    fi
done
