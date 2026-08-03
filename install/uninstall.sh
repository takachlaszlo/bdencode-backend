#!/usr/bin/env bash
set -Eeuo pipefail

umask 027

usage() {
    cat <<'EOF'
Usage:
  bash install/uninstall.sh [options]

Remove the BDEncode application and its host integration. Blu-ray sources are
never modified. Queue/job/output data and image-host credentials are preserved
unless their dedicated purge options are supplied.

Options:
  --data-root PATH          Installed work/data root
  --source-root PATH        Blu-ray source root (repeatable)
  --purge-data              Also remove the complete data root
  --confirm-data-root PATH  Required exact confirmation for --purge-data
  --purge-credentials       Remove the three fixed image-host credentials
  --purge-credential        Legacy option: remove only the ImgBB credential
  -h, --help                Show this help

If /etc/bdencode/config.toml is absent, --data-root and at least one
--source-root are mandatory. APT packages and this Git checkout are retained;
the installer cannot safely infer package provenance on older installations.
EOF
}

data_root_argument=""
confirm_data_root=""
purge_data=0
purge_credentials=0
purge_imgbb_credential=0
declare -a source_root_arguments=()

while (($#)); do
    case "$1" in
        --data-root)
            [[ $# -ge 2 ]] || { echo "--data-root requires PATH" >&2; exit 2; }
            data_root_argument="$2"
            shift 2
            ;;
        --source-root)
            [[ $# -ge 2 ]] || { echo "--source-root requires PATH" >&2; exit 2; }
            source_root_arguments+=("$2")
            shift 2
            ;;
        --purge-data)
            purge_data=1
            shift
            ;;
        --confirm-data-root)
            [[ $# -ge 2 ]] || {
                echo "--confirm-data-root requires PATH" >&2
                exit 2
            }
            confirm_data_root="$2"
            shift 2
            ;;
        --purge-credentials)
            purge_credentials=1
            shift
            ;;
        --purge-credential)
            purge_imgbb_credential=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [[ "$(id -u)" -eq 0 ]]; then
    echo "Run this uninstaller as the target account, not as root." >&2
    echo "It uses sudo only for BDEncode system integration." >&2
    exit 2
fi

task_user="$(id -un)"
task_uid="$(id -u)"
task_home="$(getent passwd "$task_user" | cut -d: -f6)"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
config_path=/etc/bdencode/config.toml
frontend_root=/var/www/bdencode
system_state_root=/var/lib/bdencode
database_path=""
config_data_root=""
declare -a config_source_roots=()

canonicalize() {
    realpath -m -- "$1"
}

assert_absolute_without_symlinks() {
    local raw="$1" label="$2" logical physical
    if [[ "$raw" != /* ]]; then
        echo "$label must be an absolute path: $raw" >&2
        return 1
    fi
    logical="$(realpath -ms -- "$raw")"
    physical="$(canonicalize "$raw")"
    if [[ "$logical" != "$physical" ]]; then
        echo "$label contains a symlinked path component: $raw" >&2
        return 1
    fi
}

path_is_within() {
    local candidate="$1" parent="$2"
    if [[ "$parent" == / ]]; then
        return 0
    fi
    [[ "$candidate" == "$parent" || "$candidate" == "$parent/"* ]]
}

paths_overlap() {
    path_is_within "$1" "$2" || path_is_within "$2" "$1"
}

refuse_broad_path() {
    local candidate="$1"
    case "$candidate" in
        /|/bin|/boot|/dev|/etc|/home|/lib|/lib64|/media|/mnt|/opt|/proc|/root|/run|/sbin|/srv|/storage|/sys|/tmp|/usr|/var)
            echo "Refusing unsafe broad path: $candidate" >&2
            return 1
            ;;
    esac
}

assert_no_mounts() {
    local target="$1" mount_target resolved_mount
    command -v findmnt >/dev/null || {
        echo "findmnt is required before recursive removal" >&2
        return 1
    }
    while IFS= read -r mount_target; do
        [[ -n "$mount_target" ]] || continue
        resolved_mount="$(canonicalize "$mount_target")"
        if path_is_within "$resolved_mount" "$target"; then
            echo "Refusing to cross or remove mountpoint: $resolved_mount" >&2
            return 1
        fi
    done < <(findmnt -rn -o TARGET)
}

validate_system_tree_exact() {
    local target="$1" expected="$2" resolved
    if ! sudo test -e "$target" && ! sudo test -L "$target"; then
        return 0
    fi
    if sudo test -L "$target"; then
        echo "Refusing recursive removal of symlink: $target" >&2
        return 1
    fi
    resolved="$(sudo realpath -m -- "$target")"
    if [[ "$resolved" != "$expected" ]]; then
        echo "Refusing unexpected system path: $target -> $resolved" >&2
        return 1
    fi
    refuse_broad_path "$resolved"
    assert_no_mounts "$resolved"
}

remove_system_tree_exact() {
    local target="$1" expected="$2"
    validate_system_tree_exact "$target" "$expected"
    if ! sudo test -e "$target" && ! sudo test -L "$target"; then
        return 0
    fi
    sudo rm -rf --one-file-system -- "$expected"
}

validate_owned_tree_exact() {
    local target="$1" expected="$2" owner resolved foreign_owner
    if [[ ! -e "$target" && ! -L "$target" ]]; then
        return 0
    fi
    if [[ -L "$target" ]]; then
        echo "Refusing recursive removal of symlink: $target" >&2
        return 1
    fi
    resolved="$(canonicalize "$target")"
    if [[ "$resolved" != "$expected" ]]; then
        echo "Refusing unexpected data path: $target -> $resolved" >&2
        return 1
    fi
    refuse_broad_path "$resolved"
    owner="$(stat -c %u -- "$resolved")"
    if [[ "$owner" != "$task_uid" ]]; then
        echo "Refusing data not owned by $task_user: $resolved" >&2
        return 1
    fi
    if ! foreign_owner="$(
        find -P "$resolved" -xdev ! -uid "$task_uid" -print -quit
    )"; then
        echo "Could not validate ownership under: $resolved" >&2
        return 1
    fi
    if [[ -n "$foreign_owner" ]]; then
        echo "Refusing tree containing foreign-owned data: $foreign_owner" >&2
        return 1
    fi
    assert_no_mounts "$resolved"
}

remove_owned_tree_exact() {
    local target="$1" expected="$2"
    validate_owned_tree_exact "$target" "$expected"
    if [[ ! -e "$target" && ! -L "$target" ]]; then
        return 0
    fi
    rm -rf --one-file-system -- "$expected"
}

declare -a config_values=()
if sudo test -f "$config_path"; then
    config_dump=""
    if ! config_dump="$(
        sudo python3 - "$config_path" <<'PY'
import sys
import tomllib

with open(sys.argv[1], "rb") as handle:
    document = tomllib.load(handle)
settings = document.get("bdencode", document)
print(settings.get("data_root", ""))
print(settings.get("database_path") or "")
for source in settings.get("source_roots", []):
    print(source)
PY
    )"; then
        echo "Could not read the installed configuration: $config_path" >&2
        exit 2
    fi
    mapfile -t config_values <<<"$config_dump"
    config_data_root="${config_values[0]:-}"
    database_path="${config_values[1]:-}"
    if ((${#config_values[@]} > 2)); then
        config_source_roots=("${config_values[@]:2}")
    fi
fi

if [[ -z "$config_data_root" ]]; then
    if [[ -z "$data_root_argument" || ${#source_root_arguments[@]} -eq 0 ]]; then
        echo "No installed config was found at $config_path." >&2
        echo "Supply both --data-root and --source-root explicitly." >&2
        exit 2
    fi
fi

if [[ -n "$config_data_root" ]]; then
    assert_absolute_without_symlinks "$config_data_root" "configured data root"
fi
if [[ -n "$data_root_argument" ]]; then
    data_root_raw="$data_root_argument"
    assert_absolute_without_symlinks "$data_root_raw" "--data-root"
    data_root="$(canonicalize "$data_root_raw")"
    if [[ -n "$config_data_root" && \
        "$data_root" != "$(canonicalize "$config_data_root")" ]]; then
        echo "--data-root does not match the installed configuration" >&2
        exit 2
    fi
else
    data_root_raw="$config_data_root"
    data_root="$(canonicalize "$data_root_raw")"
fi

declare -a source_roots=()
for source in "${config_source_roots[@]}" "${source_root_arguments[@]}"; do
    [[ -n "$source" ]] || continue
    if [[ "$source" != /* ]]; then
        echo "Source roots must be absolute: $source" >&2
        exit 2
    fi
    source_roots+=("$(canonicalize "$source")")
done
if ((${#source_roots[@]} == 0)); then
    echo "At least one source root is required for the safety check" >&2
    exit 2
fi

task_home="$(canonicalize "$task_home")"
repo_root="$(canonicalize "$repo_root")"
refuse_broad_path "$data_root"
if [[ "$data_root" == "$task_home" ]]; then
    echo "Refusing to use the complete home directory as data root" >&2
    exit 2
fi
for source in "${source_roots[@]}"; do
    if paths_overlap "$data_root" "$source"; then
        echo "Data/source roots overlap; refusing removal:" >&2
        echo "  data:   $data_root" >&2
        echo "  source: $source" >&2
        exit 2
    fi
done

if [[ -e "$data_root" ]]; then
    data_owner="$(stat -c %u -- "$data_root")"
    if [[ "$data_owner" != "$task_uid" ]]; then
        echo "Data root is not owned by $task_user: $data_root" >&2
        exit 2
    fi
fi

if [[ "$purge_data" -eq 1 ]]; then
    if [[ -z "$confirm_data_root" ]]; then
        echo "--purge-data requires --confirm-data-root matching: $data_root" >&2
        exit 2
    fi
    assert_absolute_without_symlinks "$confirm_data_root" "--confirm-data-root"
    if [[ "$(canonicalize "$confirm_data_root")" != "$data_root" ]]; then
        echo "--purge-data requires --confirm-data-root matching: $data_root" >&2
        exit 2
    fi
    if path_is_within "$repo_root" "$data_root"; then
        echo "Refusing to purge the data root containing this checkout" >&2
        exit 2
    fi
fi

installed_user=""
for unit_file in \
    /etc/systemd/system/bdencode-api.service \
    /etc/systemd/system/bdencode-worker.service; do
    if sudo test -f "$unit_file"; then
        unit_user="$(sudo sed -n 's/^User=//p' "$unit_file" | head -n 1)"
        if [[ -n "$unit_user" ]]; then
            installed_user="$unit_user"
            break
        fi
    fi
done
if [[ -n "$installed_user" && "$installed_user" != "$task_user" ]]; then
    echo "This installation belongs to $installed_user, not $task_user" >&2
    exit 2
fi

active_markers=(
    /var/lib/bdencode/install-transactions/active
    /var/lib/bdencode/install-transactions/services-pending
    /var/lib/bdencode/update-runtime/active.json
    /var/lib/bdencode/apt-transactions/active
)
recovery_needed=0
for marker in "${active_markers[@]}"; do
    if sudo test -e "$marker" || sudo test -L "$marker"; then
        recovery_needed=1
        break
    fi
done
if [[ "$recovery_needed" -eq 1 ]]; then
    echo "Finishing the interrupted BDEncode recovery before uninstall..." >&2
    if sudo test -f /etc/systemd/system/bdencode-install-recovery.service; then
        sudo systemctl restart bdencode-install-recovery.service
    elif sudo test -x /usr/local/libexec/bdencode-update-recover && \
        ! sudo test -L /usr/local/libexec/bdencode-update-recover; then
        sudo env \
            BDENCODE_USER="$task_user" \
            BDENCODE_DATA_ROOT="$data_root" \
            /usr/local/libexec/bdencode-update-recover --finalize
    else
        echo "Recovery is active, but no trusted recovery helper is available." >&2
        exit 3
    fi
fi
for marker in "${active_markers[@]}"; do
    if sudo test -e "$marker" || sudo test -L "$marker"; then
        echo "BDEncode recovery did not clear this marker: $marker" >&2
        exit 3
    fi
done

state_root="$data_root/state"
assert_absolute_without_symlinks "$state_root" "state root"
if [[ -e "$state_root" && ! -d "$state_root" ]]; then
    echo "State root is not a directory: $state_root" >&2
    exit 2
fi

if [[ -z "$database_path" ]]; then
    database_path_raw="$state_root/encoder.sqlite3"
elif [[ "$database_path" == /* ]]; then
    database_path_raw="$database_path"
else
    database_path_raw="$data_root/$database_path"
fi
assert_absolute_without_symlinks "$database_path_raw" "queue database"
database_path="$(canonicalize "$database_path_raw")"
if ! path_is_within "$database_path" "$state_root"; then
    echo "Queue database must be inside the validated state root: $database_path" >&2
    exit 2
fi
for database_file in "$database_path" "$database_path-wal" "$database_path-shm"; do
    assert_absolute_without_symlinks "$database_file" "queue database file"
done

app_root="$data_root/app"
validate_system_tree_exact "$frontend_root" /var/www/bdencode
validate_system_tree_exact "$system_state_root" /var/lib/bdencode
if [[ "$purge_data" -eq 1 ]]; then
    validate_owned_tree_exact "$data_root" "$data_root"
else
    validate_owned_tree_exact "$app_root" "$data_root/app"
fi

credential_directory="$task_home/.config/bdencode"
credential_names=(imgbb-api-key catbox-userhash freeimage-api-key)
declare -a credential_paths=()
for credential_name in "${credential_names[@]}"; do
    credential_paths+=("$credential_directory/${credential_name}.cred")
done
if [[ "$purge_credentials" -eq 1 || "$purge_imgbb_credential" -eq 1 ]]; then
    assert_absolute_without_symlinks "$credential_directory" "credential directory"
    if [[ -e "$credential_directory" && ! -d "$credential_directory" ]]; then
        echo "Credential path is not a directory: $credential_directory" >&2
        exit 2
    fi
    for credential_path in "${credential_paths[@]}"; do
        if sudo test -e "$credential_path" && \
            sudo test -d "$credential_path" && ! sudo test -L "$credential_path"; then
            echo "Credential target is unexpectedly a directory: $credential_path" >&2
            exit 2
        fi
    done
fi

nginx_target=/etc/nginx/apps/bdencode.conf
nginx_binary=/usr/sbin/nginx
if (sudo test -e "$nginx_target" || sudo test -L "$nginx_target") && \
    [[ ! -x "$nginx_binary" ]]; then
    echo "Cannot validate nginx after removing $nginx_target" >&2
    echo "Expected executable: $nginx_binary" >&2
    exit 2
fi

if [[ -d "$data_root/state" ]]; then
    deployment_lock="$data_root/state/deployment.lock"
    if [[ -L "$deployment_lock" ]]; then
        echo "Refusing a symlink deployment lock: $deployment_lock" >&2
        exit 2
    fi
    # Append-open never truncates an existing lock target. The lstat checks
    # reject a symlink both before and immediately after opening it.
    exec 9>>"$deployment_lock"
    if [[ -L "$deployment_lock" ]]; then
        exec 9>&-
        echo "Deployment lock changed into a symlink" >&2
        exit 2
    fi
    if ! flock -n -x 9; then
        echo "BDEncode is busy; the deployment lock could not be acquired" >&2
        exit 3
    fi
fi

api_was_active=0
restore_api=0
nginx_backup=""

finish() {
    local status=$?
    trap - EXIT
    set +e
    if [[ -n "$nginx_backup" ]]; then
        sudo mv -Tf "$nginx_backup" "$nginx_target"
        if [[ -x "$nginx_binary" ]]; then
            sudo "$nginx_binary" -t >/dev/null && \
                sudo systemctl reload nginx.service >/dev/null 2>&1
        fi
    fi
    if [[ "$restore_api" -eq 1 && "$api_was_active" -eq 1 ]]; then
        sudo systemctl start bdencode-api.service
    fi
    exit "$status"
}
trap finish EXIT

if sudo systemctl is-active --quiet bdencode-api.service; then
    api_was_active=1
    restore_api=1
    sudo systemctl stop bdencode-api.service
fi

queue_cli=""
if [[ -x "$data_root/app/current/venv/bin/bdencode" ]]; then
    queue_cli="$data_root/app/current/venv/bin/bdencode"
else
    shopt -s nullglob
    queue_candidates=("$data_root"/app/releases/*/venv/bin/bdencode)
    shopt -u nullglob
    for candidate in "${queue_candidates[@]}"; do
        [[ -x "$candidate" ]] && queue_cli="$candidate"
    done
fi

if [[ -e "$database_path" || -e "$database_path-wal" || \
    -e "$database_path-shm" ]]; then
    if [[ -z "$queue_cli" ]]; then
        echo "A queue database exists, but no CLI can verify that it is idle:" >&2
        echo "  $database_path" >&2
        exit 3
    fi
    source_path_value="$(IFS=:; printf '%s' "${source_roots[*]}")"
    queue_status=0
    queue_output="$(
        env \
            -u BDENCODE_CONFIG \
            -u BDENCODE_CONFIG_PATH \
            -u BDENCODE_DATABASE_PATH \
            -u BDENCODE_DB_PATH \
            BDENCODE_DATA_ROOT="$data_root" \
            BDENCODE_SOURCE_ROOTS="$source_path_value" \
            "$queue_cli" --config /dev/null --database "$database_path" \
            queue-idle 2>&1
    )" || queue_status=$?
    if [[ -n "$queue_output" ]]; then
        printf '%s\n' "$queue_output" >&2
    fi
    if [[ "$queue_status" -ne 0 ]]; then
        echo "The queue is active or could not be verified; uninstall stopped." >&2
        exit 3
    fi
fi

# No API can enqueue new work, and the exclusive deployment lock prevents a
# worker claim. From this point the application is intentionally being removed.
restore_api=0
managed_units=(
    bdencode-update.timer
    bdencode-update.service
    bdencode-worker.service
    bdencode-api.service
    bdencode-install-recovery.path
    bdencode-install-recovery.service
    bdencode-update-recovery.service
)
for unit in "${managed_units[@]}"; do
    sudo systemctl stop "$unit" >/dev/null 2>&1 || true
done
for unit in "${managed_units[@]}"; do
    unit_state="$(sudo systemctl is-active "$unit" 2>/dev/null || true)"
    case "$unit_state" in
        inactive|failed|unknown|"") ;;
        *)
            echo "Unit did not stop: $unit ($unit_state)" >&2
            exit 1
            ;;
    esac
    sudo systemctl disable "$unit" >/dev/null 2>&1 || true
done

for marker in "${active_markers[@]}"; do
    if sudo test -e "$marker" || sudo test -L "$marker"; then
        echo "A transaction became active during uninstall: $marker" >&2
        exit 3
    fi
done

if sudo test -e "$nginx_target" || sudo test -L "$nginx_target"; then
    nginx_backup_candidate="$(
        sudo mktemp /etc/nginx/apps/.bdencode.conf.uninstall.XXXXXX
    )"
    sudo rm -f -- "$nginx_backup_candidate"
    sudo cp -aT -- "$nginx_target" "$nginx_backup_candidate"
    nginx_backup="$nginx_backup_candidate"
    sudo rm -f -- "$nginx_target"
    sudo "$nginx_binary" -t
    if sudo systemctl is-active --quiet nginx.service; then
        sudo systemctl reload nginx.service
    fi
    sudo rm -f -- "$nginx_backup"
    nginx_backup=""
fi

system_files=(
    /etc/bdencode/config.toml
    /etc/bdencode/media-apt.sources.list
    /etc/apt/preferences.d/bdencode-media
    /etc/systemd/system/bdencode-api.service
    /etc/systemd/system/bdencode-worker.service
    /etc/systemd/system/bdencode-update.service
    /etc/systemd/system/bdencode-update.timer
    /etc/systemd/system/bdencode-update-recovery.service
    /etc/systemd/system/bdencode-install-recovery.service
    /etc/systemd/system/bdencode-install-recovery.path
    /etc/systemd/system/bdencode-update.service.d/bdencode-recovery.conf
    /etc/systemd/system/bdencode-api.service.d/bdencode-recovery.conf
    /etc/systemd/system/bdencode-worker.service.d/bdencode-recovery.conf
    /etc/systemd/system/bdencode-worker.service.d/credential.conf
    /etc/systemd/system/apt-daily.service.d/bdencode-recovery.conf
    /etc/systemd/system/apt-daily-upgrade.service.d/bdencode-recovery.conf
    /etc/systemd/system/multi-user.target.wants/bdencode-api.service
    /etc/systemd/system/multi-user.target.wants/bdencode-worker.service
    /etc/systemd/system/multi-user.target.wants/bdencode-update-recovery.service
    /etc/systemd/system/multi-user.target.wants/bdencode-install-recovery.path
    /etc/systemd/system/timers.target.wants/bdencode-update.timer
    /usr/local/libexec/bdencode-daily-update
    /usr/local/libexec/bdencode-install-transaction
    /usr/local/libexec/bdencode-apt-transaction
    /usr/local/libexec/bdencode-apt-guard
    /usr/local/libexec/bdencode-update-runtime
    /usr/local/libexec/bdencode-update-recover
    /usr/local/libexec/bdencode-recovery-check
)
sudo rm -f -- "${system_files[@]}"
sudo systemctl daemon-reload
for unit in "${managed_units[@]}"; do
    sudo systemctl reset-failed "$unit" >/dev/null 2>&1 || true
done

remove_system_tree_exact "$frontend_root" /var/www/bdencode

if [[ -e "$app_root" || -L "$app_root" ]]; then
    if [[ "$(dirname "$(canonicalize "$app_root")")" != "$data_root" ]]; then
        echo "Refusing an app path outside the validated data root" >&2
        exit 2
    fi
    remove_owned_tree_exact "$app_root" "$data_root/app"
fi

remove_system_tree_exact "$system_state_root" /var/lib/bdencode

system_directories=(
    /etc/systemd/system/bdencode-update.service.d
    /etc/systemd/system/bdencode-api.service.d
    /etc/systemd/system/bdencode-worker.service.d
    /etc/systemd/system/apt-daily.service.d
    /etc/systemd/system/apt-daily-upgrade.service.d
    /etc/bdencode
)
for directory in "${system_directories[@]}"; do
    sudo rmdir --ignore-fail-on-non-empty -- "$directory" 2>/dev/null || true
done

if [[ "$purge_credentials" -eq 1 || "$purge_imgbb_credential" -eq 1 ]]; then
    credential_purge_mode=imgbb
    if [[ "$purge_credentials" -eq 1 ]]; then
        credential_purge_mode=all
    fi
    # Traverse every directory component with O_NOFOLLOW and unlink only the
    # fixed basenames relative to the anchored directory descriptor.  This
    # prevents a writable parent from being swapped to a symlink between the
    # preflight and the privileged deletion.
    sudo python3 - "$credential_directory" "$task_uid" "$credential_purge_mode" <<'PY'
import os
import stat
import sys

directory, raw_uid, mode = sys.argv[1:]
expected_uid = int(raw_uid)
names = (
    ("imgbb-api-key.cred", "catbox-userhash.cred", "freeimage-api-key.cred")
    if mode == "all"
    else ("imgbb-api-key.cred",)
)
if mode not in {"all", "imgbb"} or not directory.startswith("/"):
    raise SystemExit("invalid credential purge request")

descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
try:
    for component in (item for item in directory.split("/") if item):
        try:
            next_descriptor = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
        except FileNotFoundError:
            raise SystemExit(0)
        os.close(descriptor)
        descriptor = next_descriptor
    details = os.fstat(descriptor)
    if details.st_uid != expected_uid or stat.S_IMODE(details.st_mode) != 0o700:
        raise SystemExit("credential directory ownership or mode changed")
    for name in names:
        try:
            target = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if stat.S_ISDIR(target.st_mode):
            raise SystemExit(f"credential target became a directory: {name}")
        os.unlink(name, dir_fd=descriptor)
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY
fi
if [[ ! -L "$credential_directory" ]]; then
    rmdir --ignore-fail-on-non-empty -- "$credential_directory" 2>/dev/null || true
fi

if [[ "$purge_data" -eq 1 ]]; then
    remove_owned_tree_exact "$data_root" "$data_root"
    echo "Removed BDEncode data root: $data_root"
else
    echo "Preserved BDEncode queue/job/output data: $data_root"
fi

echo "BDEncode application and host integration removed."
echo "Blu-ray source roots were not modified."
for credential_path in "${credential_paths[@]}"; do
    if [[ -e "$credential_path" || -L "$credential_path" ]]; then
        echo "Preserved image-host credential: $credential_path"
    fi
done
echo "APT packages and this Git checkout were intentionally retained."
