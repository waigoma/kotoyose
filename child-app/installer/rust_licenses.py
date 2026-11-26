"""Collect license notices for linked Rust dependencies."""

import json
from pathlib import Path
import shutil
import sys

stage = Path(sys.argv[1])
metadata = json.loads((stage / 'cargo-metadata.json').read_bytes())
resolved = {node['id']: node['deps'] for node in metadata['resolve']['nodes']}
pending = [metadata['resolve']['root']]
used = set(pending)
while pending:
    for dependency in resolved[pending.pop()]:
        package = dependency['pkg']
        if package not in used and any(kind['kind'] is None for kind in dependency['dep_kinds']):
            used.add(package)
            pending.append(package)
lines = []
for package in metadata['packages']:
    if package['id'] not in used or package['source'] is None:
        continue
    name = f"{package['name']}-{package['version']}"
    lines.append(f"{name}  {package['license'] or package['license_file']}")
    source = Path(package['manifest_path']).parent
    for path in source.iterdir():
        if path.is_file() and path.name.upper().startswith(('LICENSE', 'LICENCE', 'COPYING', 'NOTICE', 'AUTHORS', 'COPYRIGHT')):
            shutil.copyfile(path, stage / 'licenses' / f'{name}-{path.name}')
    if package['license_file']:
        license = source / package['license_file']
        shutil.copyfile(license, stage / 'licenses' / f'{name}-{license.name}')
(stage / 'licenses' / 'Rust-dependencies.txt').write_text('\n'.join(sorted(lines)) + '\n', encoding='utf-8')
