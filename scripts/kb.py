#!/usr/bin/env python3
"""One entry point for the whole pipeline. Each command is a thin wrapper around the
other scripts in this folder, with the workspace layout filled in for you.

  kb.py start  <导出目录> <工作区>    parse the exports, strip process noise, build the conversation catalog
  kb.py topics <工作区>              check topics.json and write 话题目录.md
  kb.py digest <工作区> [--topic 名] [--budget N]
                                    for each chosen topic, pick the passages worth reading and pull
                                    out commands, AI instructions and errors word for word
  kb.py show   <工作区> <话题> u12 [u30-u33]   the full text of units, when an excerpt is not enough
  kb.py find   <工作区> <关键词> [--topic 名]   where a word occurs
  kb.py verify <工作区> <知识库>      check the finished vault, scan for secrets, save an inventory
  kb.py hub    <知识库>              build 00_跨话题汇总/: terms, commands and AI instructions from all topics
  kb.py gaps   <工作区> <知识库>      what was left out, and frequent terms and commands the notes lack
  kb.py more   <工作区> [--tier high|all] [--topic 名] [--keyword 词]
                                    a second helping from what was left out, as 补充N.md per topic

`digest` reads <工作区>/focus.json when present: weights per kind of content and extra
keywords, so the selection follows what this person wants to keep.

Run `start` again with a newer export to pick up new or changed conversations, then
`digest` again. Reading everything instead of a digest is still possible for one topic
that must be exhaustive: batch, status, next, check, export (see references/tools.md).
"""
from __future__ import annotations
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def run(script, *args):
    return subprocess.run([sys.executable, str(HERE / script), *map(str, args)]).returncode


def snapshots(work):
    root = work / 'snapshots'
    found = sorted((p for p in root.glob('s*') if re.fullmatch(r's\d+', p.name)), key=lambda p: int(p.name[1:])) \
        if root.is_dir() else []
    return found


def cmd_start(a):
    work = a.work.resolve()
    source = a.source.resolve()
    if work == source or source in work.parents:
        sys.exit('工作区不能放在导出目录里面。')
    work.mkdir(parents=True, exist_ok=True)
    state_path = work / 'kb.json'
    state = json.loads(state_path.read_text()) if state_path.is_file() else {}
    scope_id = a.scope_id or state.get('scope_id') or source.name
    previous = snapshots(work)
    target = work / 'snapshots' / f's{len(previous) + 1}'
    args = [source, target, '--scope-id', scope_id]
    if previous:
        args += ['--baseline', previous[-1] / 'source_manifest.json']
    if a.include_tool_blocks:
        args.append('--include-tool-blocks')
    code = run('prepare_sources.py', *args)
    if not (target / 'source_manifest.json').is_file():
        return code or 2
    state_path.write_text(json.dumps({'scope_id': scope_id, 'source': str(source)}, ensure_ascii=False, indent=1) + '\n')
    return run('catalog.py', 'build', target, work) or code


def cmd_topics(a):
    return run('catalog.py', 'topics', a.work)


def cmd_batch(a):
    work = a.work.resolve()
    found = snapshots(work)
    if not found:
        sys.exit('还没有快照，先运行 kb.py start。')
    if not (work / 'topics.json').is_file():
        sys.exit('还没有 topics.json，先归好话题并运行 kb.py topics。')
    if run('catalog.py', 'topics', work):
        sys.exit('topics.json 有问题，先按上面的提示改好。')
    used = {int(m.group(1)) for p in (work / 'batches').glob('*.json') if (m := re.match(r's(\d+)-b', p.name))} \
        if (work / 'batches').is_dir() else set()
    label = f's{max(used) + 1 if used else 1}'
    args = [found[-1], work, '--label', label, '--scope', work / 'topics.json', '--skip-batched',
            '--batch-chars', a.batch_chars]
    return run('make_batches.py', *args)


def cmd_digest(a):
    work = a.work.resolve()
    found = snapshots(work)
    if not found:
        sys.exit('还没有快照，先运行 kb.py start。')
    if not (work / 'topics.json').is_file():
        sys.exit('还没有 topics.json，先归好话题并运行 kb.py topics。')
    if run('catalog.py', 'topics', work):
        sys.exit('topics.json 有问题，先按上面的提示改好。')
    args = ['build', found[-1], work]
    if a.budget:
        args += ['--budget', a.budget]
    for topic in a.topic or []:
        args += ['--topic', topic]
    return run('digest.py', *args)


def cmd_show(a):
    return run('digest.py', 'show', a.work, a.topic, *a.refs, '--max-chars', a.max_chars)


def cmd_find(a):
    args = ['find', a.work, a.keyword, '-n', a.n]
    if a.topic:
        args += ['--topic', a.topic]
    return run('digest.py', *args)


def cmd_hub(a):
    return run('hub.py', a.vault)


def cmd_gaps(a):
    args = ['gaps', a.work, a.vault, '-n', a.n]
    for topic in a.topic or []:
        args += ['--topic', topic]
    return run('digest.py', *args)


def cmd_more(a):
    args = ['more', a.work, '--tier', a.tier]
    for topic in a.topic or []:
        args += ['--topic', topic]
    for word in a.keyword or []:
        args += ['--keyword', word]
    if a.budget:
        args += ['--budget', a.budget]
    return run('digest.py', *args)


def cmd_cards(a):
    args = [a.command, a.work]
    if a.command == 'next':
        args += ['-n', a.n]
    if a.command == 'check':
        for batch in a.batch or []:
            args += ['--batch', batch]
    return run('cards.py', *args)


def cmd_verify(a):
    work, vault = a.work.resolve(), a.vault.resolve()
    code = run('check_vault.py', work, vault)
    code = run('scan_sensitive_data.py', vault, '--profile', a.profile,
               '--output', work / 'privacy_result_reader.json') or code
    inventory = work / 'inventory_last.json'
    if code == 0:
        inventory.unlink(missing_ok=True)
        run('vault_inventory.py', vault, inventory)
    return code


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='command', required=True)
    s = sub.add_parser('start')
    s.add_argument('source', type=Path)
    s.add_argument('work', type=Path)
    s.add_argument('--scope-id', help='a fixed name for this set of exports; remembered after the first run')
    s.add_argument('--include-tool-blocks', action='store_true', help='keep tool traffic and model reasoning too')
    s.set_defaults(handler=cmd_start)
    t = sub.add_parser('topics')
    t.add_argument('work', type=Path)
    t.set_defaults(handler=cmd_topics)
    b = sub.add_parser('batch')
    b.add_argument('work', type=Path)
    b.add_argument('--batch-chars', type=int, default=30000)
    b.set_defaults(handler=cmd_batch)
    d = sub.add_parser('digest')
    d.add_argument('work', type=Path)
    d.add_argument('--topic', action='append', help='only this topic; repeatable')
    d.add_argument('--budget', type=int, help='characters of excerpts per topic (default: 10%% of it, 6000-60000)')
    d.set_defaults(handler=cmd_digest)
    w = sub.add_parser('show')
    w.add_argument('work', type=Path)
    w.add_argument('topic')
    w.add_argument('refs', nargs='+')
    w.add_argument('--max-chars', type=int, default=6000)
    w.set_defaults(handler=cmd_show)
    f = sub.add_parser('find')
    f.add_argument('work', type=Path)
    f.add_argument('keyword')
    f.add_argument('--topic')
    f.add_argument('-n', type=int, default=20)
    f.set_defaults(handler=cmd_find)
    h = sub.add_parser('hub')
    h.add_argument('vault', type=Path)
    h.set_defaults(handler=cmd_hub)
    g = sub.add_parser('gaps')
    g.add_argument('work', type=Path)
    g.add_argument('vault', type=Path)
    g.add_argument('--topic', action='append')
    g.add_argument('-n', type=int, default=10)
    g.set_defaults(handler=cmd_gaps)
    m = sub.add_parser('more')
    m.add_argument('work', type=Path)
    m.add_argument('--tier', choices=('high', 'all'), default='high')
    m.add_argument('--topic', action='append')
    m.add_argument('--keyword', action='append')
    m.add_argument('--budget', type=int)
    m.set_defaults(handler=cmd_more)
    for name in ('status', 'next', 'check', 'export', 'owners'):
        c = sub.add_parser(name)
        c.add_argument('work', type=Path)
        if name == 'next':
            c.add_argument('-n', type=int, default=5)
        if name == 'check':
            c.add_argument('--batch', action='append')
        c.set_defaults(handler=cmd_cards)
    v = sub.add_parser('verify')
    v.add_argument('work', type=Path)
    v.add_argument('vault', type=Path)
    v.add_argument('--profile', default='reader-local-private',
                   choices=('reader-local-private', 'reader-shareable'))
    v.set_defaults(handler=cmd_verify)
    a = p.parse_args()
    return a.handler(a) or 0


if __name__ == '__main__':
    raise SystemExit(main())
