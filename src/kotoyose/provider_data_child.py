"""Owner approvals for provider declarations offered by the linked host.

The original document bytes stay intact. A change of those bytes, or of the
host certificate fingerprint, makes the approval stale until the owner approves
again. Execution and revocation share one lock.
"""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
from uuid import uuid4

from .print_cli_profiles import use_data_profiles
from .provider_data import CAPABILITIES, UNRESTRICTED, Declaration, identifier, parse_declaration, supports_capability
from .provider_data_runtime import DeclarationRejected, installed_path, stop_process
from .provider_data_storage import _windows, atomic_write, locked, private_directory, private_read, restrict_owner_directory

_FINGERPRINT = re.compile(r"[0-9a-f]{64}")
_home_var: ContextVar[Path | None] = ContextVar("provider_data_home", default=None)
RIGHTS = (
    "The approved CLI runs with your user rights. "
    "Network and file access are not sandboxed on this child."
)
UNRESTRICTED_RIGHTS = (
    "Grok runs with your rights on this PC. Tool limits and the work folder are not enforced."
)
GROK_RIGHTS = "Install and login use your user rights. Read turns can read files accessible to your user."


@dataclass(frozen=True)
class Approval:
    row: Declaration
    host_fingerprint: str
    capability: str
    platform: str
    approved_at: str
    generation: str = ""

    def active(self, fingerprint: str, platform: str) -> bool:
        target = self.row.platform(platform)
        return (self.host_fingerprint == fingerprint and self.platform == platform
                and target is not None and target.supports(self.capability))


def execution_home() -> Path | None:
    return _home_var.get()


def host_fingerprint(home: Path) -> str:
    try:
        path = home / "host.json"
        payload = json.loads(private_read(path, 64 * 1024) if _windows() else path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("child host pin is unavailable") from exc
    fingerprint = payload.get("fingerprint") if isinstance(payload, dict) else None
    if not isinstance(fingerprint, str) or _FINGERPRINT.fullmatch(fingerprint) is None:
        raise ValueError("child host pin is unavailable")
    return fingerprint


def _root(home: Path) -> Path:
    return home.absolute() / "provider-declarations"


def _approved_dir(home: Path) -> Path:
    return _root(home) / "approved"


def _offer_path(home: Path) -> Path:
    return _root(home) / "offered.json"


def _revoked_path(home: Path) -> Path:
    return _root(home) / "revoked.json"


def _read_json(path: Path, limit: int):
    try:
        return json.loads(private_read(path, limit))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None


def store_offers(home: Path, fingerprint: str, offers: list) -> None:
    """Remember the host snapshot. Receiving it does not approve or launch anything."""
    if _FINGERPRINT.fullmatch(fingerprint) is None:
        raise ValueError("invalid host fingerprint")
    from .provider_data import parse_offers

    rows = parse_offers(offers)
    restrict_owner_directory(home)
    with locked(_root(home)):
        atomic_write(_offer_path(home), json.dumps({
            "host_fingerprint": fingerprint,
            "offers": [row.offer() for row in rows],
        }).encode(), child=True)


def offered(home: Path) -> tuple[str, tuple[Declaration, ...]]:
    payload = _read_json(_offer_path(home), 128 * 1024)
    if not isinstance(payload, dict):
        return "", ()
    fingerprint = payload.get("host_fingerprint")
    try:
        from .provider_data import parse_offers

        rows = parse_offers(payload.get("offers"))
    except ValueError:
        return "", ()
    if not isinstance(fingerprint, str):
        return "", ()
    return fingerprint, rows


def _approval_path(home: Path, provider: str) -> Path:
    return _approved_dir(home) / f"{provider}.json"


def _load_file(home: Path, provider: str) -> Approval | None:
    payload = _read_json(_approval_path(home, provider), 24 * 1024)
    if not isinstance(payload, dict):
        return None
    document = payload.get("document")
    fingerprint = payload.get("host_fingerprint")
    capability = payload.get("capability")
    platform = payload.get("platform")
    approved_at = payload.get("approved_at")
    generation = payload.get("generation", "")
    if not all(isinstance(item, str) for item in (document, fingerprint, capability, platform, approved_at, generation)):
        return None
    try:
        row = parse_declaration(document.encode("utf-8"))
    except (UnicodeError, ValueError):
        return None
    if payload.get("sha256") != row.sha256 or capability not in CAPABILITIES or provider != row.id:
        return None
    return Approval(row, fingerprint, capability, platform, approved_at, generation)


def approve(
    home: Path, provider: str, *, sha256: str, capability: str = "read", confirm_unrestricted: bool = False,
) -> Approval:
    """Approve one offered document for this host fingerprint and OS."""
    from .cli_install_spec import platform_key
    restrict_owner_directory(home)
    with locked(_root(home)):
        fingerprint, rows = offered(home)
        if fingerprint != host_fingerprint(home):
            raise ValueError("offered declarations are not from the current host")
        row = next((item for item in rows if item.id == provider and item.sha256 == sha256), None)
        platform = platform_key()
        target = row.platform(platform) if row is not None else None
        if row is None or target is None:
            raise ValueError("no matching offered declaration")
        if not target.supports(capability):
            raise ValueError("capability is not offered for this platform")
        if capability == UNRESTRICTED and confirm_unrestricted is not True:
            raise ValueError("unrestricted approval requires explicit confirmation")
        previous = _load_file(home, row.id)
        stop = previous is not None and (
            previous.row.sha256 != row.sha256 or not supports_capability(capability, previous.capability)
        )
        record = {
            "host_fingerprint": fingerprint, "sha256": row.sha256, "capability": capability,
            "platform": platform, "document": row.document,
            "approved_at": datetime.now(timezone.utc).isoformat(),
            "generation": uuid4().hex if previous is None or stop else previous.generation,
        }
        directory = _approved_dir(home)
        private_directory(directory)
        atomic_write(directory / f"{row.id}.json", json.dumps(record).encode(), child=True)
        if stop:
            from .provider_data_runtime import stop_declaration

            stop_declaration(row.id)
        loaded = _load_file(home, row.id)
    if loaded is None or not loaded.active(fingerprint, platform):
        raise ValueError("approval was not stored")
    return loaded


def revoke(home: Path, provider: str) -> None:
    """Drop an approval. The same bytes stay refused until the owner approves again."""
    path = _approval_path(home, provider)
    with locked(_root(home)):
        current = _load_file(home, provider)
        if current is None:
            raise ValueError("provider declaration is not approved")
        revoked = _read_json(_revoked_path(home), 64 * 1024)
        if not isinstance(revoked, list):
            revoked = []
        revoked.append({
            "id": current.row.id, "sha256": current.row.sha256,
            "host_fingerprint": current.host_fingerprint,
        })
        atomic_write(_revoked_path(home), json.dumps(revoked[-32:]).encode(), child=True)
        from .provider_data_runtime import stop_declaration

        stop_declaration(current.row.id)
        path.unlink()


def approvals(home: Path) -> tuple[Approval, ...]:
    directory = _approved_dir(home)
    if not directory.is_dir() or directory.is_symlink():
        return ()
    found = []
    for path in sorted(directory.glob("*.json")):
        loaded = _load_file(home, path.stem)
        if loaded is not None:
            found.append(loaded)
    return tuple(found)


def approved_for(
    home: Path, row: Declaration, capability: str = "read", *,
    generation: str | None = None, wait_for_lock: bool = True,
) -> Approval:
    """Raise before a process starts when this hash or host is not the approved one."""
    from .cli_install_spec import platform_key

    with locked(_root(home), blocking=wait_for_lock):
        loaded = _load_file(home, row.id)
        try:
            fingerprint = host_fingerprint(home)
        except ValueError as exc:
            raise DeclarationRejected("unapproved provider declaration") from exc
        if loaded is None or loaded.row.sha256 != row.sha256 or not loaded.active(fingerprint, platform_key()):
            raise DeclarationRejected("unapproved provider declaration")
        if not supports_capability(loaded.capability, capability):
            raise DeclarationRejected(f"child owner has not approved {capability} for '{row.id}'")
        if generation is not None and loaded.generation != generation:
            raise DeclarationRejected(f"child approval changed for '{row.id}'")
        return loaded


async def watch_approval(home: Path, approval: Approval, pid: int) -> None:
    """Stop an ACP process when the desktop's separate approval helper invalidates its grant."""
    while True:
        await asyncio.sleep(0.5)
        for attempt in range(3):
            try:
                await asyncio.to_thread(
                    approved_for, home, approval.row, approval.capability,
                    generation=approval.generation, wait_for_lock=False,
                )
            except OSError:
                if attempt < 2:
                    await asyncio.sleep(0.2)
                    continue
            except ValueError:
                pass
            else:
                break
            await asyncio.to_thread(stop_process, approval.row.id, pid)
            return


def active_profiles(home: Path):
    from .cli_install_spec import platform_key

    try:
        fingerprint = host_fingerprint(home)
    except ValueError:
        return ()
    platform = platform_key()
    return tuple(item.row.profile() for item in approvals(home) if item.active(fingerprint, platform))


@contextmanager
def use_approved(home: Path):
    """Expose the active approvals to the stage A/B profile lookup for this task."""
    token = _home_var.set(home.absolute())
    try:
        with use_data_profiles(active_profiles(home)):
            yield
    finally:
        _home_var.reset(token)


def preview(home: Path) -> dict:
    """Fields the Providers page shows before an owner approves."""
    from .cli_install_spec import platform_key

    fingerprint, rows = offered(home)
    current = ""
    try:
        current = host_fingerprint(home)
    except ValueError:
        pass
    platform = platform_key()
    approved = {item.row.id: item for item in approvals(home)}
    revoked = _read_json(_revoked_path(home), 64 * 1024)
    revoked_hashes = {
        item.get("sha256") for item in revoked if isinstance(item, dict)
    } if isinstance(revoked, list) else set()
    offers = []
    for row in rows:
        current_approval = approved.get(row.id)
        if current_approval is not None and current_approval.row.sha256 == row.sha256 and current_approval.active(current, platform):
            state = "approved"
        elif row.sha256 in revoked_hashes:
            state = "revoked"
        elif current_approval is not None:
            state = "stale"
        else:
            state = "pending"
        target = row.platform(platform)
        offers.append({
            "id": row.id, "profile_id": row.profile_id, "display_name": row.name,
            "publisher": row.publisher, "sha256": row.sha256, "driver": row.driver,
            "contract_version": row.contract_version, "status": state,
            "capability": target.capability if target is not None else None,
            "approved_capability": current_approval.capability if state == "approved" else None,
            "can_install": state == "approved" and target is not None and target.artifact.format in ("raw", "tar.gz"),
            "installed": state == "approved" and installed_path(row, home) is not None,
            "local_login": target is not None and (platform.startswith("linux-") or target.supports(UNRESTRICTED)),
            "platform": platform, "host_fingerprint": fingerprint,
            "binary": target.binary if target is not None else None,
            "version": row.install.version,
            "artifacts": [
                {
                    "url": url, "sha256": target.artifact.digest, "version": row.install.version,
                    "platform": target.artifact.platform,
                }
                for url in target.artifact.url_templates
            ] if target is not None else [],
            "commands": row.commands(platform),
            "network": {
                "download_hosts": list(row.download_hosts),
                "login_hosts": list(row.login_hosts),
                "runtime_hosts": list(row.runtime_hosts),
            },
            "output_guard_env": list(row.output_guard_env),
            "rights": (UNRESTRICTED_RIGHTS if target is not None and target.supports(UNRESTRICTED)
                       else GROK_RIGHTS if row.isolation is not None else RIGHTS),
        })
    return {"host_fingerprint": fingerprint, "current_fingerprint": current, "offers": offers}


def advertised_providers(home: Path) -> list[dict]:
    from .cli_install_spec import platform_key

    try:
        fingerprint = host_fingerprint(home)
    except ValueError:
        return []
    platform = platform_key()
    return [
        {
            "driver": item.row.driver, "contract_version": item.row.contract_version,
            "provider": item.row.id, "profile": item.row.profile_id,
            "sha256": item.row.sha256, "capability": item.capability,
        }
        for item in approvals(home)
        if item.active(fingerprint, platform) and installed_path(item.row, home) is not None
    ]


def validate_request(payload: dict) -> dict:
    """Run ``parse_declaration`` for one child-app request. This is the only validator."""
    from .cli_install_spec import platform_key
    from .provider_data import parse_offers

    if not isinstance(payload, dict):
        raise ValueError("invalid declaration validate request")
    action = payload.get("action")
    if action == "offers":
        rows = parse_offers(payload.get("offers"))
        return {"offers": [row.offer() for row in rows]}
    if action == "approval":
        document = payload.get("document")
        if not isinstance(document, str):
            raise ValueError("invalid declaration")
        row = parse_declaration(document.encode("utf-8"))
        if payload.get("id") != row.id or payload.get("sha256") != row.sha256:
            raise ValueError("offer id does not match identity.id")
        if row.platform(platform_key()) is None:
            raise ValueError("no artifact for this platform")
        return {
            "id": row.id, "sha256": row.sha256, "document": row.document, "platform": platform_key(),
        }
    home = payload.get("home")
    if not isinstance(home, str) or not Path(home).is_absolute():
        raise ValueError("invalid declaration home")
    if action == "store_offers":
        child_home = Path(home)
        store_offers(child_home, host_fingerprint(child_home), payload.get("offers"))
        return {}
    if action == "preview":
        return preview(Path(home))
    if action == "advertise":
        return {"providers": advertised_providers(Path(home))}
    if action in {"approve", "revoke"}:
        child_home = Path(home)
        provider = identifier(payload.get("id"))
        if action == "approve":
            approve(
                child_home, provider, sha256=payload.get("sha256"), capability=payload.get("capability"),
                confirm_unrestricted=payload.get("confirm_unrestricted") is True,
            )
        else:
            revoke(child_home, provider)
        return preview(child_home)
    if action == "install":
        from .provider_data_download import install_download

        child_home = Path(home)
        provider = identifier(payload.get("id"))
        approval = _load_file(child_home, provider)
        if approval is None or approval.row.sha256 != payload.get("sha256"):
            raise DeclarationRejected("unapproved or changed provider declaration")
        install_download(approval.row, child_home=child_home)
        return {"installed": True}
    raise ValueError("unknown declaration validate action")


def main(argv: list[str] | None = None) -> int:
    """``python -I -m kotoyose.provider_data_child validate`` reads one JSON request on stdin."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args != ["validate"]:
        print("usage: python -I -m kotoyose.provider_data_child validate", file=sys.stderr)
        return 2
    try:
        payload = json.loads(sys.stdin.buffer.read())
        result = validate_request(payload)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc) or "invalid declaration", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(json.dumps(result).encode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
