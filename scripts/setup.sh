#!/usr/bin/env bash
# Install generator dependencies into this checkout's .venv.
set -euo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${PYTHON:-python3}"
if [[ "${EUID}" -eq 0 ]]; then
    echo "Run setup.sh as your normal user; sudo is only needed for publishing and NGINX installation." >&2
    exit 1
fi
"${python_bin}" -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ is required"'
if [[ -e "${repo_dir}/.venv" && ! -x "${repo_dir}/.venv/bin/python" ]]; then
    echo "Existing .venv is not a Linux/macOS environment. Use a separate server checkout." >&2
    exit 1
fi
"${python_bin}" -m venv "${repo_dir}/.venv"
"${repo_dir}/.venv/bin/python" -m pip install -r "${repo_dir}/requirements.txt"
"${repo_dir}/.venv/bin/python" -m pip check
echo "Ready: ${repo_dir}/.venv/bin/python"
