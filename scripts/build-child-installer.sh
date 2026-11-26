#!/usr/bin/env bash
set -euo pipefail

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root"
stage_only=false
if [[ ${1:-} == --stage-only && $# == 1 ]]; then
    stage_only=true
elif [[ $# != 0 ]]; then
    echo "Usage: $0 [--stage-only]" >&2
    exit 2
fi

version=$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')
if [[ ! "$version" =~ ^([0-9]+\.[0-9]+\.[0-9]+)(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$ ]]; then
    echo "Unsupported installer version: $version" >&2
    exit 1
fi
file_version=${BASH_REMATCH[1]}.0
export GIT_OPTIONAL_LOCKS=0 GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
build_id=$(git rev-parse HEAD)
dirty=$(git -c core.fsmonitor=false -c core.excludesFile=/dev/null status --porcelain)
if [[ -n "$dirty" ]]; then build_id+=-dirty-000000000000; fi
export KOTOYOSE_CHILD_BUILD_ID="$build_id"
target=x86_64-pc-windows-gnu
cargo=${CARGO:-$HOME/.cargo/bin/cargo}
if [[ ! -x "$cargo" ]]; then
    cargo=${CARGO:-cargo}
fi

mkdir -p dist
stage=$(mktemp -d "$root/dist/.child-installer.XXXXXX")
keep_stage=false
trap 'if ! $keep_stage; then rm -rf -- "$stage"; fi' EXIT

python_release=20261110
python_archive=cpython-3.13.18%2B20261110-x86_64-pc-windows-msvc-install_only.tar.gz
python_sha256=7e93813bd362c6413f30788b248590e2f169df2d17dcac81aafbcc7a6eeb542c
archive=${CHILD_PYTHON_ARCHIVE:-$stage/python.tar.gz}
if [[ -z ${CHILD_PYTHON_ARCHIVE:-} ]]; then
    curl --fail --silent --show-error --location --retry 3 \
        "https://github.com/astral-sh/python-build-standalone/releases/download/$python_release/$python_archive" \
        --output "$archive"
fi
printf '%s  %s\n' "$python_sha256" "$archive" | sha256sum --check
tar -xzf "$archive" -C "$stage" --exclude='*.pdb' --exclude='*/__pycache__'
test -f "$stage/python/python.exe"

cargo_target="$root/child-app/target"
build=("$cargo" build --locked --release --target "$target" --target-dir "$cargo_target" --manifest-path child-app/Cargo.toml)
"${build[@]}"
mkdir -p "$stage/bin/parts" "$stage/licenses"
cp "$cargo_target/$target/release/"{kotoyose-child-app,kotoyose-child-background}.exe "$stage/bin/"
x86_64-w64-mingw32-gcc -Os -s -Wall -Wextra -Werror -municode \
    child-app/installer/stop_child.c -o "$stage/stop-child.exe" -ladvapi32
cp LICENSE "$stage/licenses/Kotoyose.txt"
"$cargo" metadata --locked --format-version 1 --filter-platform "$target" --manifest-path child-app/Cargo.toml \
    > "$stage/cargo-metadata.json"
python3 child-app/installer/rust_licenses.py "$stage"

uv build --no-config --no-python-downloads --default-index https://pypi.org/simple \
    --wheel --no-create-gitignore --out-dir "$stage/bin/parts"
readarray -t wheels < <(find "$stage/bin/parts" -maxdepth 1 -name 'kotoyose-*.whl' -type f)
[[ ${#wheels[@]} == 1 ]]

PYTHONPATH=src python3 -B child-app/installer/build_payload.py "$stage"

download_source=(--index-url https://pypi.org/simple)
if [[ -n ${CHILD_RUNTIME_WHEELS:-} ]]; then
    download_source=(--no-index --find-links "$CHILD_RUNTIME_WHEELS")
fi
PIP_CONFIG_FILE=/dev/null python3 -m pip --isolated --disable-pip-version-check --no-cache-dir download \
    "${download_source[@]}" --require-hashes --no-deps --only-binary=:all: \
    --platform win_amd64 --python-version 3.13.18 --implementation cp --abi cp313 \
    --dest "$stage/bin/parts/runtime" -r "$stage/windows-runtime.txt"

cp child-app/installer/setup.nsi "$stage/setup.nsi"
if [[ -n "$dirty" ]]; then
    build_id=$(python3 child-app/installer/payload_fingerprint.py "$stage" "$build_id" \
        bin python licenses stop-child.exe provider-repairs.nsh setup.nsi)
fi
if $stage_only; then
    keep_stage=true
    printf 'Installer payload: %s\n' "$stage"
    du -sh "$stage/bin" "$stage/python" "$stage/licenses"
    exit 0
fi
if ! command -v makensis >/dev/null; then
    echo 'makensis is required (Ubuntu package: nsis).' >&2
    exit 1
fi

name=KotoyoseChild-Setup-$version.exe
makensis -V2 -INPUTCHARSET UTF8 "-DPAYLOAD=$stage" "-DOUTPUT=$stage/$name" \
    "-DPRODUCT_VERSION=$version" "-DFILE_VERSION=$file_version" "-DBUILD_ID=$build_id" "$stage/setup.nsi"
(cd "$stage" && sha256sum "$name" > "$name.sha256")
python3 child-app/installer/package_metadata.py "$stage" "$name" "$version" "$build_id"
mv -- "$stage/$name" "$stage/$name.sha256" "$stage/$name.json" dist/
ls -lh "dist/$name" "dist/$name.sha256" "dist/$name.json"
