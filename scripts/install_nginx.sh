#!/usr/bin/env bash
# Install rendered snippets, validate the full configuration, then reload.
set -euo pipefail
if [[ "${EUID}" -ne 0 || "$#" -ne 1 ]]; then
    echo "Usage: sudo bash scripts/install_nginx.sh generated/nginx" >&2
    exit 1
fi
config_dir="$(cd -- "$1" && pwd)"
command -v nginx >/dev/null
command -v systemctl >/dev/null
files=(emoticon-lookup-origin.conf)
[[ ! -f "${config_dir}/${files[0]}" ]] && { echo "Missing origin config." >&2; exit 1; }
if [[ -f "${config_dir}/emoticon-lookup-front.conf" ]]; then
    files+=(emoticon-lookup-front.conf)
fi
install -d -m 0755 /var/lib/emoticon-lookup /var/lib/emoticon-lookup/www /var/lib/emoticon-lookup/acme
install -d -m 0755 /etc/nginx/conf.d
backup_dir="$(mktemp -d /var/lib/emoticon-lookup/nginx-backup.XXXXXX)"
chmod 0700 "${backup_dir}"
changed=()
rollback() {
    for name in "${changed[@]}"; do
        if [[ -f "${backup_dir}/${name}" ]]; then
            cp -p -- "${backup_dir}/${name}" "/etc/nginx/conf.d/${name}"
        else
            rm -f -- "/etc/nginx/conf.d/${name}"
        fi
    done
    echo "Restored prior snippets. Backup: ${backup_dir}" >&2
}
trap 'rollback' ERR
for name in "${files[@]}"; do
    target="/etc/nginx/conf.d/${name}"
    if [[ -L "${target}" ]]; then
        echo "Refusing to replace a symlink: ${target}" >&2
        rollback
        exit 1
    fi
    if [[ -e "${target}" ]]; then
        cp -p -- "${target}" "${backup_dir}/${name}"
    fi
    changed+=("${name}")
    install -m 0644 -- "${config_dir}/${name}" "${target}"
done
nginx -t
if systemctl is-active --quiet nginx; then
    systemctl reload nginx
else
    systemctl enable --now nginx
fi
trap - ERR
echo "NGINX configuration installed. Prior snippets: ${backup_dir}"
