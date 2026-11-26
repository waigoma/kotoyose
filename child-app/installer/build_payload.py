"""Generate locked child runtime requirements from the component catalog."""

from pathlib import Path
import re
import subprocess
import sys

try:
    from packaging.markers import Marker
except ImportError:
    from pip._vendor.packaging.markers import Marker

from kotoyose.child_catalog import component_rows
from kotoyose.print_cli_profiles import list_print_cli_profiles


def _export(pruned: set[str]) -> dict[str, str]:
    args = ["uv", "export", "--quiet", "--offline", "--no-cache", "--no-config", "--frozen",
            "--no-dev", "--no-emit-project", "--no-header", "--no-annotate"]
    for package in sorted(pruned):
        args.extend(("--prune", package))
    rows = {}
    for entry in re.split(r"(?m)^(?=\w)", subprocess.check_output(args, text=True)):
        if not entry.strip():
            continue
        name, _, _ = entry.partition("==")
        if not re.fullmatch(r"[a-z0-9-]+", name) or "--hash=sha256:" not in entry or name in rows:
            raise SystemExit(f"Unexpected uv export entry: {entry[:120]}")
        rows[name] = entry
    return rows


def write_requirements(parts: Path, providers: list[dict], *, linux: bool = False) -> None:
    """Write hashed base and provider requirements from uv.lock."""
    # Windows pip evaluates the platform markers and selects a compatible wheel.
    vendor_packages = {package for provider in providers for package in provider["packages"]}
    host_only = {package for profile in list_print_cli_profiles() for package in profile.host_sdk_packages}
    excluded = (host_only if linux else {"uvloop"} | host_only) - vendor_packages
    base = _export(excluded | vendor_packages)
    (parts / "requirements.txt").write_text("".join(base.values()), encoding="utf-8")
    for provider in providers:
        packages = set(provider["packages"])
        provider_runtime = _export(excluded | (vendor_packages - packages))
        # Keep provider-only dependencies with the provider. Shared pins are already in the base.
        provider_only = provider_runtime.keys() - base.keys()
        if not packages <= provider_only:
            raise SystemExit(f'Missing locked packages for {provider["id"]}')
        pins = "".join(entry for name, entry in provider_runtime.items() if name in provider_only)
        (parts / f'requirements-{provider["id"]}.txt').write_text(pins, encoding="utf-8")


def windows_runtime_requirements(parts: Path) -> str:
    """Evaluate the locked markers for the bundled Windows Python before cross-download."""
    env = {
        "implementation_name": "cpython", "implementation_version": "3.13.18",
        "os_name": "nt", "platform_machine": "AMD64", "platform_python_implementation": "CPython",
        "platform_release": "11", "platform_system": "Windows", "platform_version": "10.0",
        "python_full_version": "3.13.18", "python_version": "3.13", "sys_platform": "win32",
    }
    rows = []
    continuation = " " + chr(92)
    for entry in re.split(r"(?m)^(?=\w)", (parts / "requirements.txt").read_text(encoding="utf-8")):
        requirement, _, hashes = entry.partition(chr(10))
        pin, separator, marker = requirement.removesuffix(continuation).partition(";")
        if not separator or Marker(marker).evaluate(env):
            rows.append(pin + continuation + chr(10) + hashes)
    return "".join(rows)


def repair_markers(providers: list[dict]) -> str:
    """Render the NSIS conditions for provider-owned Python repair markers."""
    blocks = []
    for provider in providers:
        marker = provider["id"] + ".needs-reinstall"
        blocks.append(
            '    ${If} ${FileExists} "$INSTDIR\\$VersionDir\\python\\' + marker + '"\n'
            '        StrCpy $0 1\n'
            '    ${EndIf}\n'
        )
    return "".join(blocks)


def main(stage: Path, *, linux: bool = False) -> None:
    providers = [row for row in component_rows() if row["packages"]]
    write_requirements(stage / "bin" / "parts", providers, linux=linux)
    if linux:
        return
    (stage / "windows-runtime.txt").write_text(windows_runtime_requirements(stage / "bin" / "parts"), encoding="utf-8")
    (stage / "provider-repairs.nsh").write_text(repair_markers(providers), encoding="utf-8")


if __name__ == "__main__":
    main(Path(sys.argv[1]), linux=sys.argv[2:] == ["--linux"])
