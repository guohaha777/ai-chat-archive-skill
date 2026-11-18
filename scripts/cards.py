#!/usr/bin/env python3
"""Track reading progress and gate the move from reading to writing.

Subcommands:
  status  <work>            how many batches are done, invalid or still unread
  next    <work> [-n N]     the next unread or invalid batches, with file paths
  check   <work> [--batch]  validate card files; exit 1 until every batch passes
  owners  <work>            summarize raw owner labels (and the owner map, if present)
  export  <work>            write per-owner card files for the writing stage

Progress is derived from the card files themselves, so there is no separate ledger
that can drift from reality after an interruption.
"""
from __future__ import annotations
import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from workspace import KIND_ORDER, load_batches, load_cards, load_owner_map

KIND_NAMES = {'overview': '概况与目标', 'concept': '概念与领域知识', 'method': '方法与流程',
              'decision': '决策与取舍', 'issue': '问题与排查', 'command': '命令',
              'prompt': '给 AI 的指令', 'term': '术语', 'reflection': '复盘与原则',
              'material': '素材与成品', 'other': '其他'}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def cmd_status(a):
    batches = load_batches(a.work)
    cards, errors, warnings, state = load_cards(a.work)
    counts = Counter(state.values())
    chars = {bid: b['chars'] for bid, b in batches.items()}
    done_chars = sum(chars[b] for b, s in state.items() if s == 'done')
    print(json.dumps({'batches': len(batches), 'done': counts['done'], 'invalid': counts['invalid'],
                      'pending': counts['pending'], 'chars_total': sum(chars.values()),
                      'chars_done': done_chars, 'cards': len(cards), 'warnings': len(warnings),
                      'ready_to_write': bool(batches) and counts['done'] == len(batches)},
                     ensure_ascii=False))


def cmd_next(a):
    batches = load_batches(a.work)
    _, _, _, state = load_cards(a.work)
    todo = [bid for bid in batches if state[bid] != 'done'][:a.n]
    for bid in todo:
        print(json.dumps({'batch': bid, 'state': state[bid], 'chars': batches[bid]['chars'],
                          'read': str(Path(a.work) / 'batches' / f'{bid}.md'),
                          'write': str(Path(a.work) / 'cards' / f'{bid}.json')}, ensure_ascii=False))
    if not todo:
        print(json.dumps({'batch': None, 'message': 'all batches have valid card files'}))


def cmd_check(a):
    only = set(a.batch) if a.batch else None
    batches = load_batches(a.work)
    if only and only - set(batches):
        raise SystemExit('unknown batch id')
    cards, errors, warnings, state = load_cards(a.work, only)
    result = {'status': 'FAIL' if errors or not state else 'PASS', 'scope': sorted(only) if only else 'all',
              'batches': len(state), 'cards': len(cards), 'errors': errors, 'warnings': warnings}
    if not only:
        write_json(Path(a.work) / 'cards_check.json', result)
    brief = Counter(e['code'] for e in errors)
    print(json.dumps({'status': result['status'], 'batches': len(state), 'cards': len(cards),
                      'errors': dict(brief), 'warnings': dict(Counter(w['code'] for w in warnings)),
                      'first_errors': errors[:8]}, ensure_ascii=False))
    return 1 if result['status'] == 'FAIL' else 0


def resolve(cards, by_label):
    """Group cards by canonical owner; return (groups, unmapped label counts)."""
    groups, unmapped = defaultdict(list), Counter()
    for card in cards:
        label = str(card.get('owner') or '').strip()
        entry = by_label.get(label)
        if entry is None:
            unmapped[label] += 1
        else:
            groups[entry['name']].append(card)
    return groups, unmapped


def cmd_owners(a):
    cards, errors, _, state = load_cards(a.work)
    by_label, entries = load_owner_map(a.work)
    raw = defaultdict(lambda: {'cards': 0, 'kinds': Counter(), 'sources': set(), 'when': []})
    for card in cards:
        row = raw[str(card.get('owner') or '').strip()]
        row['cards'] += 1
        row['kinds'][card.get('kind')] += 1
        row['sources'].update(card['sources'])
        if card.get('when'):
            row['when'].append(str(card['when']))
    lines = ['# 归属标签汇总', '',
             f'已读批次 {sum(s == "done" for s in state.values())}/{len(state)}，抽取卡 {len(cards)} 张。', '',
             '| 标签 | 归入 | 卡片 | 会话 | 时间范围 | 类型分布 |', '| --- | --- | --- | --- | --- | --- |']
    for label, row in sorted(raw.items(), key=lambda kv: -kv[1]['cards']):
        entry = by_label.get(label)
        target = '（未映射）' if entry is None else entry['name'] if entry.get('keep', True) else f"不收录：{entry['name']}"
        span = f"{min(row['when'])} – {max(row['when'])}" if row['when'] else ''
        kinds = '、'.join(f'{KIND_NAMES.get(k, k)} {n}' for k, n in row['kinds'].most_common())
        lines.append(f"| {label or '（空）'} | {target} | {row['cards']} | {len(row['sources'])} | {span} | {kinds} |")
    out = Path(a.work) / 'owners_summary.md'
    out.write_text('\n'.join(lines) + '\n')
    _, unmapped = resolve(cards, by_label)
    print(json.dumps({'labels': len(raw), 'cards': len(cards), 'mapped_owners': len(entries),
                      'unmapped_labels': len(unmapped), 'reading_complete': not errors and bool(state),
                      'summary': str(out)}, ensure_ascii=False))


def safe_name(name):
    return re.sub(r'[\\/:*?"<>|\s]+', '_', name).strip('_') or 'owner'


def cmd_export(a):
    cards, errors, _, state = load_cards(a.work)
    if errors or not state:
        raise SystemExit('reading is not complete: run `cards.py check` and fix every batch first')
    by_label, entries = load_owner_map(a.work)
    groups, unmapped = resolve(cards, by_label)
    if unmapped:
        raise SystemExit('these owner labels are not topics in topics.json: ' + '、'.join(sorted(unmapped)[:20]))
    for entry in entries:
        if not entry.get('keep', True) and not str(entry.get('reason') or '').strip():
            raise SystemExit(f"owner '{entry['name']}' is not kept but has no reason")
    batches = load_batches(a.work)
    titles = {}
    for batch in batches.values():
        for source in batch['sources']:
            titles[source['source_id']] = (source.get('title') or '（无标题）', str(source.get('started_at') or '')[:10])
    root = Path(a.work) / 'owners'
    index = ['# 归属与卡片数', '', '| 归属 | 目录 | 卡片 | 类型分布 |', '| --- | --- | --- | --- |']
    written = 0
    for entry in entries:
        if not entry.get('keep', True):
            continue
        rows = groups.get(entry['name'], [])
        kinds = Counter(c.get('kind') for c in rows)
        index.append(f"| {entry['name']} | {entry.get('folder', '')} | {len(rows)} | "
                     + '、'.join(f'{KIND_NAMES.get(k, k)} {n}' for k, n in kinds.most_common()) + ' |')
        folder = root / safe_name(entry['name'])
        folder.mkdir(parents=True, exist_ok=True)
        for old in folder.glob('*.md'):
            old.unlink()
        order = [k for k in KIND_ORDER if k in kinds] + sorted(k for k in kinds if k not in KIND_ORDER)
        for kind in order:
            lines = [f"# {entry['name']}｜{KIND_NAMES.get(kind, kind)}", '',
                     '写作素材，不是成品。方括号里的卡片编号只用于 placement.jsonl，不要写进读者笔记。', '']
            for card in sorted((c for c in rows if c.get('kind') == kind), key=lambda c: (str(c.get('when') or ''), c['id'])):
                meta = [str(card[k]) for k in ('when', 'state') if card.get(k)]
                origin = '；'.join(f'{titles.get(s, ("", ""))[0]}（{titles.get(s, ("", ""))[1]}）' for s in card['sources'][:3])
                lines += [f"## [{card['id']}] {card.get('title')}", '',
                          ' · '.join(meta + [f'出自：{origin}']), '', str(card.get('content')).strip(), '']
                for value in card['verbatim']:
                    lines += ['原文：', '', '~~~text', value.rstrip(), '~~~', '']
            (folder / f'{kind}.md').write_text('\n'.join(lines) + '\n')
            written += 1
    root.mkdir(parents=True, exist_ok=True)
    (root / '_index.md').write_text('\n'.join(index) + '\n')
    print(json.dumps({'owners': sum(e.get('keep', True) for e in entries), 'files': written,
                      'index': str(root / '_index.md')}, ensure_ascii=False))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='command', required=True)
    for name, handler in [('status', cmd_status), ('next', cmd_next), ('check', cmd_check),
                          ('owners', cmd_owners), ('export', cmd_export)]:
        s = sub.add_parser(name)
        s.add_argument('work', type=Path)
        s.set_defaults(handler=handler)
        if name == 'next':
            s.add_argument('-n', type=int, default=5)
        if name == 'check':
            s.add_argument('--batch', action='append', help='check only this batch; repeatable')
    a = p.parse_args()
    if not (a.work / 'batches').is_dir():
        p.error('work directory has no batches/; run make_batches.py first')
    return a.handler(a) or 0


if __name__ == '__main__':
    raise SystemExit(main())
