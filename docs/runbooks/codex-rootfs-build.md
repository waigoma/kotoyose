# Build a persistent Codex coding rootfs

`python scripts/build-codex-rootfs` creates a **new** reviewed artifact. It does
not reconstruct an old digest, change profiles or old Attempt snapshots, restart
Kotoyose, alter mounts/cgroups, or contact any provider. A missing old `/tmp` artifact
is not recovered by pointing an old execution at the new tree. New artifact
activation and a fresh execution require a separate operator decision.

## Inputs and trust boundary

Run on the intended Linux host with trusted distro Python, shell, Git and
libraries. Inspect the source versions/provenance first. The tool never runs any
input executable, `ldd`, package manager, downloader, or shell command. It runs
only the explicitly SHA-256-pinned OS `readelf`, through an open file descriptor.
The existing pinned Codex CLI is inspected for dependencies but **not copied**
into the tree; the existing launcher mounts it separately.

Prepare a small JSON file (replace all placeholder hashes and paths):

```json
{
  "schema_version": 1,
  "python": {"path": "/usr/bin/python3.12", "sha256": "<64 lowercase hex>"},
  "stdlib": "/usr/lib/python3.12",
  "binaries": {
    "sh": {"path": "/usr/bin/dash", "sha256": "<64 lowercase hex>"},
    "bash": {"path": "/usr/bin/bash", "sha256": "<64 lowercase hex>"},
    "git": {"path": "/usr/bin/git", "sha256": "<64 lowercase hex>"},
    "env": {"path": "/usr/bin/env", "sha256": "<64 lowercase hex>"},
    "rg": {"path": "<canonical existing rg ELF>", "sha256": "<64 lowercase hex>"}
  },
  "codex": {
    "path": "<canonical existing codex_cli_bin/bin/codex>",
    "sha256": "880819aab735adcf16f36cd628940b9f1e652a36c01d10a5d32f83cc84902971"
  },
  "bwrap": {
    "path": "<canonical existing codex_cli_bin/codex-resources/bwrap>",
    "sha256": "15fd5f2673d2e1c2f80e34e5108462fd014f5e8bbc7b0731f1ab23d1ff1ac140"
  },
  "ca": {"path": "/etc/ssl/certs/ca-certificates.crt", "sha256": "<64 lowercase hex>"},
  "readelf": {"path": "/usr/bin/x86_64-linux-gnu-readelf", "sha256": "<64 lowercase hex>"},
  "library_dirs": ["/usr/lib/x86_64-linux-gnu"]
}
```

These CLI/helper hashes identify the files in the locked Linux glibc x86_64
`openai-codex-cli-bin==0.170.0` wheel. Inspect the artifacts on the target host
and verify the hashes before building. For another platform, review the matching
wheel and record the hashes of its CLI and helper before using them.
`sh`, `bash`, `git` are required; `env`, `rg` are the only optional binaries.
All named file paths and source directory roots must be absolute and canonical.
The Python executable and `python3.X` stdlib must be the matching distro version.
User-installed `rg`, CLI and helper are allowed only as explicitly named,
hash-pinned ELF inputs. A pinned helper's source mode 0775 is normalized to 0555;
it is never executed. The inspector itself must not be group/world-writable.

Only the named stdlib root is recursively visited. Excluded subtrees are
`site-packages`, `dist-packages`, `__pycache__`, `test`, `tests`, `idlelib`,
`tkinter`, `ensurepip`, `venv`, hidden names and `config-*`. At every depth,
`sitecustomize.py`, `usercustomize.py` and `*.pth` entries are excluded by name
before source-path resolution or content reads, including externally linked hooks.
Hidden files and `.pyc`, `.pyo`, `.a` files are skipped. Distro `_sysconfigdata__*.py` aliases
directly under the selected stdlib root may resolve only to a regular module
with that same naming pattern directly in that same root; copied output is a
regular file. All other stdlib symlinks are rejected. In particular the build-only
`config-3.12-x86_64-linux-gnu/libpython3.12.so` link is never followed because the
whole `config-*` subtree is excluded. Writable/untrusted sources, missing basic
stdlib files, over 20,000 traversal entries, over 9,000
selected files, copied files over 256 MiB, and total selected bytes over 1 GiB fail.
The separately mounted Codex CLI has its own finite 512 MiB cap. Its SHA-256
is checked in at most 1 MiB chunks, including both ELF-inspection rechecks;
its body is never loaded into the copy inventory. Its size still counts toward
the existing 1 GiB total source-byte limit.
No user HOME, `.venv`, credential/config directory or broad host library tree is
recursively copied. The three explicitly named user-installed executables are
individual reads, not authorization to scan their parent directories.

ELF `NEEDED` dependencies, including Python extensions, are followed only by
exact filename in the supplied finite system library directories. Dependencies
must resolve uniquely; symlink targets must stay in those directories. ELF
interpreters must resolve there too. Only required libraries are copied,
dereferenced into regular files at their loader paths. Supply the distro's
default loader directory, not a private or arbitrary search path. RPATH/RUNPATH
inputs fail closed and need a separately reviewed recipe. There is no fallback
to host mounts, host `ld.so.cache`, network fetching, or arbitrary execution.
All final inventory is checked by the existing, unchanged rootfs validator.

## Build and inspect

The existing parent must be persistent, canonical, owned by root or the operator,
and not group/world-writable. Its creation is a separate operator step. The CLI
rejects `/tmp`, `/var/tmp` and `/run` parents. For example:

```sh
python scripts/build-codex-rootfs \
  --inputs /path/to/reviewed-rootfs-inputs.json \
  --parent "$HOME/.local/share/kotoyose/rootfs" \
  --version codex-0.170.0-python3.12-20261119-v1
```

Output is one versioned envelope containing `rootfs/` and `manifest.json`.
The manifest is **outside** the rootfs and contains the inputs, each source and
destination, SHA-256, normalized modes, dependency edges, external CLI pin,
directory modes, rootfs digest and final path. Executables/libraries and
directories are 0555; stdlib/data/public CA are 0444. No credential contents are
read or written. Files/directories are synced, the authoritative
`rootfs_digest(coding=True)` and `validate_rootfs(coding=True)` run, then Linux
`renameat2(RENAME_NOREPLACE)` publishes the envelope atomically. An existing
destination, even an empty directory or broken symlink, is never overwritten.
There is no unsafe fallback if that syscall/filesystem operation is unavailable.
An unpublished failed staging envelope is cleaned up. A post-publication sync
error may leave the complete new envelope present: inspect it; never overwrite
or assume an error means publication did not occur.

Review the complete manifest and independently recheck its digest before
activation. A successful build proves inventory/hash/mode validation, not CLI
compatibility, import completeness, model eligibility or production readiness.
Separately authorized host tests should check Python bridge imports, the pinned
CLI/helper, and credential-free Git init/add/commit/branch inside the existing
isolation boundary. Git fetch/push, credentials, templates outside the allowed
inventory and new toolchains are not installed by this recipe.

Only after that review should an operator create a new profile pin and perform
the separately authorized service restart/smoke. Keep the old profile/history
unchanged. The new canonical rootfs path/digest cannot silently resume old
Attempts pinned to a different artifact.

## Activate the 0.170.0 release

1. Drain active attempts before changing the runtime. Sync the updated Kotoyose
   release from its lockfile on the target host, including the extras used
   there. Stage the reviewed 0.170.0 CLI at a new canonical path for the
   native driver and verify its SHA-256.
2. Rebuild the coding rootfs with the matching CLI and helper from the
   reviewed inputs above. Use a new envelope and verify its manifest,
   including the external CLI SHA-256 and the copied helper. For an
   enabled standard cell, separately validate its dependency inventory
   against the new CLI and rebuild that cell's artifact if its required
   files change.
3. Set `drivers.codex.app-server.cli_binary` to the staged CLI and
   `drivers.codex.app-server.cells.coding.{rootfs,rootfs_digest}` to the new
   envelope's canonical `rootfs/` path and manifest digest. If a new
   standard artifact was staged, set the corresponding fields on the
   `standard` cell. Roll out the updated Kotoyose bridge and these driver pins
   together, then perform the service restart and smoke test. Start a
   fresh attempt for each enabled surface to check startup, egress and
   cancellation before reopening traffic.
