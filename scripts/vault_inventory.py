#!/usr/bin/env python3
"""Read-only inventory and change detection. Never edit the Vault."""
import argparse
import hashlib
import json
import re
from pathlib import Path


def knowledge_id(text):
    match = re.match(r'\A---\s*\n(.*?)\n---(?:\s*\n|$)', text, re.S)
    if not match:
        return None
    value = re.search(r'^knowledge_id:\s*(.*?)\s*$', match.group(1), re.M)
    return value.group(1).strip('"\'') if value else None


def inventory(root):
    root = Path(root).resolve()
    result = {}
    for path in sorted(root.rglob('*')):
        rel = path.relative_to(root).as_posix()
        if any(p.startswith('.') for p in Path(rel).parts) or not path.is_file():
            continue
        if path.is_symlink() or root not in path.resolve().parents:
            raise ValueError('symlink_outside_inventory')
        data = path.read_bytes()
        result[rel] = {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
        if path.suffix == '.md':
            result[rel]['knowledge_id'] = knowledge_id(data.decode('utf-8-sig'))
    return result


def compare(before, after):
    return {'added': sorted(set(after) - set(before)), 'missing': sorted(set(before) - set(after)),
            'changed': sorted(k for k in before.keys() & after.keys() if before[k]['sha256'] != after[k]['sha256'])}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('vault', type=Path)
    p.add_argument('output', type=Path)
    p.add_argument('--baseline', type=Path)
    a = p.parse_args()
    root, output = a.vault.resolve(), a.output.resolve()
    if not root.is_dir() or root == output or root in output.parents or output.exists():
        p.error('use an existing Vault and a new output file outside it')
    current = inventory(root)
    result = {'files': current}
    if a.baseline:
        result['changes'] = compare(json.loads(a.baseline.read_text())['files'], current)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'files': len(current), 'changes': {k: len(v) for k, v in result.get('changes', {}).items()}}))


if __name__ == '__main__':
    main()
