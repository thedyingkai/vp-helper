#!/bin/bash
set -euo pipefail

vp_source="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
vp_submit_path="${1:-}"
if [[ $EUID -ne 0 ]]; then
    exec sudo -- bash "$vp_source/install.sh" "$vp_submit_path"
fi
source /etc/os-release
if [[ "${ID:-}" != ubuntu ]]; then
    printf '%s\n' 'This installer supports Ubuntu only.' >&2
    exit 1
fi
if [[ -n "$vp_submit_path" && ! -x "$vp_submit_path" ]]; then
    printf '%s\n' 'The existing submit path must be an executable file.' >&2
    exit 1
fi
if [[ -e /usr/local/bin/vp && ! -L /usr/local/bin/vp ]]; then
    printf '%s\n' '/usr/local/bin/vp already exists and is not a managed symlink.' >&2
    exit 1
fi
if [[ -L /usr/local/bin/vp && "$(readlink /usr/local/bin/vp)" != /opt/vp-helper/current/venv/bin/vp ]]; then
    printf '%s\n' '/usr/local/bin/vp points to another program.' >&2
    exit 1
fi
if [[ -n "$vp_submit_path" && -e /usr/local/bin/submit && "$(readlink -f /usr/local/bin/submit)" != "$(readlink -f "$vp_submit_path")" ]]; then
    printf '%s\n' 'A different global submit already exists. It was left in place.' >&2
    exit 1
fi
vp_python=python3
if ! python3 -c 'import sys; assert sys.version_info[:2] == (3,12)' 2>/dev/null; then
    if [[ $(uname -m) != x86_64 || ! -f "$vp_source/runtimes/manifest.json" ]]; then
        printf '%s\n' 'Python 3.12+ is required. Bundle the verified standalone runtime for older Ubuntu.' >&2
        exit 1
    fi
    readarray -t vp_runtime < <(python3 - "$vp_source/runtimes/manifest.json" <<'PY'
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text())
print(data['filename'])
print(data['sha256'])
PY
)
    vp_archive="$vp_source/runtimes/${vp_runtime[0]}"
    printf '%s  %s\n' "${vp_runtime[1]}" "$vp_archive" | sha256sum -c -
    vp_runtime_root="/opt/vp-helper/runtime/${vp_runtime[1]:0:12}"
    if [[ ! -x "$vp_runtime_root/python/bin/python3" ]]; then
        install -d -m 755 "$vp_runtime_root"
        tar -xzf "$vp_archive" -C "$vp_runtime_root" --no-same-owner
    fi
    vp_python="$vp_runtime_root/python/bin/python3"
fi
vp_apt_packages=()
if ! command -v tmux >/dev/null; then vp_apt_packages+=(tmux); fi
if [[ $vp_python == python3 ]] && ! python3 -c 'import ensurepip' 2>/dev/null; then
    vp_apt_packages+=(python3-venv)
fi
if (( ${#vp_apt_packages[@]} )); then
    apt-get update -qq
    apt-get install -y --no-install-recommends "${vp_apt_packages[@]}"
fi
vp_release="/opt/vp-helper/releases/$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 755 "$vp_release/app" /usr/local/bin
cp -a "$vp_source/vp_helper" "$vp_source/pyproject.toml" "$vp_release/app/"
"$vp_python" -m venv "$vp_release/venv"
if [[ -d "$vp_source/wheels" ]]; then
    "$vp_release/venv/bin/python" -m pip install --disable-pip-version-check --no-index --find-links "$vp_source/wheels" "$vp_release/app"
else
    "$vp_release/venv/bin/python" -m pip install --disable-pip-version-check "$vp_release/app"
fi
if [[ -d "$vp_source/third_party" ]]; then
    cp -a "$vp_source/third_party" "$vp_release/"
    chmod 755 "$vp_release/third_party/qoj/submit" "$vp_release/third_party/cf_submit/submit"
    vp_pip_args=(--disable-pip-version-check)
    if [[ -d "$vp_source/wheels" ]]; then
        vp_pip_args+=(--no-index --find-links "$vp_source/wheels")
    fi
    "$vp_release/venv/bin/python" -m pip install "${vp_pip_args[@]}" selectolax lxml robobrowser 'Werkzeug==0.16.1' 'prettytable<4'
fi
ln -sfn "$vp_release" /opt/vp-helper/current
ln -sfn /opt/vp-helper/current/venv/bin/vp /usr/local/bin/vp
if [[ -n "$vp_submit_path" && ! -e /usr/local/bin/submit ]]; then
    ln -s -- "$vp_submit_path" /usr/local/bin/submit
elif [[ -z "$vp_submit_path" && -d "$vp_release/third_party" ]]; then
    vp_owner="${SUDO_USER:-root}"
    vp_owner_home="$(getent passwd "$vp_owner" | cut -d: -f6)"
    vp_owner_group="$(id -gn "$vp_owner")"
    vp_selected="$vp_owner_home/.local/state/vp/submit"
    if [[ -e /usr/local/bin/submit || -L /usr/local/bin/submit ]]; then
        if [[ ! -L /usr/local/bin/submit || $(readlink /usr/local/bin/submit) != "$vp_selected" ]]; then
            printf '%s\n' 'Preserved the previously installed global submit.'
        fi
    else
        install -d -m 700 -o "$vp_owner" -g "$vp_owner_group" "$vp_owner_home/.local/state/vp"
        if [[ ! -e "$vp_selected" && ! -L "$vp_selected" ]]; then
            ln -s /opt/vp-helper/current/third_party/qoj/submit "$vp_selected"
            chown -h "$vp_owner:$vp_owner_group" "$vp_selected"
        fi
        ln -s "$vp_selected" /usr/local/bin/submit
    fi
fi
/usr/local/bin/vp --help
printf '%s\n' 'Installed /usr/local/bin/vp. submit runs the selected existing platform script.'
