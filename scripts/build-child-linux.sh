#!/usr/bin/env bash
set -euo pipefail

if [[ $# != 0 || $(uname -sm) != 'Linux x86_64' ]]; then
    echo "Usage: $0 (on Linux x64)" >&2
    exit 2
fi

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root"
version=$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')
if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$ ]]; then
    echo "Unsupported package version: $version" >&2
    exit 1
fi
export GIT_OPTIONAL_LOCKS=0 GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
build_id=$(git rev-parse HEAD)
dirty=$(git -c core.fsmonitor=false -c core.excludesFile=/dev/null status --porcelain)
if [[ -n "$dirty" ]]; then build_id+=-dirty-000000000000; fi
export KOTOYOSE_CHILD_BUILD_ID="$build_id"
target=x86_64-unknown-linux-gnu
cargo=${CARGO:-$HOME/.cargo/bin/cargo}
if [[ ! -x "$cargo" ]]; then
    cargo=${CARGO:-cargo}
fi

mkdir -p dist
stage=$(mktemp -d "$root/dist/.child-linux.XXXXXX")
trap 'rm -rf -- "$stage"' EXIT
folder=kotoyose-child-$version
payload=$stage/$folder
mkdir -p "$payload/bin/parts/runtime" "$payload/licenses"

python_release=20261110
python_archive=cpython-3.13.18%2B20261110-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz
python_sha256=f2d2199eb25880e07863b391cf7124e7ffa1338b0d65600dfcec117c4223a7fb
archive=${CHILD_PYTHON_ARCHIVE:-$stage/python.tar.gz}
if [[ -z ${CHILD_PYTHON_ARCHIVE:-} ]]; then
    curl --fail --silent --show-error --location --retry 3 \
        "https://github.com/astral-sh/python-build-standalone/releases/download/$python_release/$python_archive" \
        --output "$archive"
fi
printf '%s  %s\n' "$python_sha256" "$archive" | sha256sum --check
tar -xzf "$archive" -C "$payload" --exclude='*/__pycache__'
python=$payload/python/bin/python3
test -x "$python"
# Keep one copy of the interpreter and the shared library, without archive links.
rm -- "$payload/python/bin/python" "$python" "$payload/python/lib/libpython3.13.so"
mv -- "$payload/python/bin/python3.13" "$python"
# The runtime only needs the shared library, standard library and Python tools.
find "$payload/python" -name 'libpython*.a' -type f -delete

cargo_target="$root/child-app/target"
build=("$cargo" build --locked --release --target "$target" --target-dir "$cargo_target" --manifest-path child-app/Cargo.toml --bin kotoyose-child-app)
"${build[@]}"
cp "$cargo_target/$target/release/kotoyose-child-app" "$payload/bin/"
cp LICENSE "$payload/licenses/Kotoyose.txt"
"$cargo" metadata --locked --format-version 1 --filter-platform "$target" --manifest-path child-app/Cargo.toml \
    > "$payload/cargo-metadata.json"
python3 child-app/installer/rust_licenses.py "$payload"
rm -- "$payload/cargo-metadata.json"
install -m 755 child-app/installer/install-linux.sh "$payload/install.sh"

uv build --no-config --no-python-downloads --default-index https://pypi.org/simple \
    --wheel --no-create-gitignore --out-dir "$payload/bin/parts"
readarray -t wheels < <(find "$payload/bin/parts" -maxdepth 1 -name 'kotoyose-*.whl' -type f)
[[ ${#wheels[@]} == 1 ]]
PYTHONPATH=src python3 -B child-app/installer/build_payload.py "$payload" --linux

download_source=(--index-url https://pypi.org/simple)
if [[ -n ${CHILD_RUNTIME_WHEELS:-} ]]; then
    download_source=(--no-index --find-links "$CHILD_RUNTIME_WHEELS")
fi
PIP_CONFIG_FILE=/dev/null "$python" -I -m pip --isolated --disable-pip-version-check --no-cache-dir download \
    "${download_source[@]}" --require-hashes --no-deps --only-binary=:all: \
    --dest "$payload/bin/parts/runtime" -r "$payload/bin/parts/requirements.txt"

if [[ -n "$dirty" ]]; then
    build_id=$(python3 child-app/installer/payload_fingerprint.py "$payload" "$build_id" .)
fi
name=$folder-linux-x64.tar.gz
# Resolve the pinned Python distribution's internal aliases before packaging.
tar --format=ustar --dereference --hard-dereference --exclude='*/__pycache__' \
    -czf "$stage/$name" -C "$stage" "$folder"
(cd "$stage" && sha256sum "$name" > "$name.sha256")
python3 child-app/installer/package_metadata.py "$stage" "$name" "$version" "$build_id"
mv -- "$stage/$name" "$stage/$name.sha256" "$stage/$name.json" dist/
ls -lh "dist/$name" "dist/$name.sha256" "dist/$name.json"
