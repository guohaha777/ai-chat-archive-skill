#!/usr/bin/env python3
"""Mechanical checks on the finished reader vault. Not a grade of usefulness.

Checks, in order:
  placement    every kept card went into a page or was dropped with a reason
  verbatim     commands and prompts kept as cards still appear word for word in a page
  code blocks  a fenced block either occurs in the source or is introduced as a template
  links        wiki links, Markdown links, anchors, canvas file nodes
  structure    numbering, entry page, reachability, one H1, no bookkeeping in reader text
  readability  lead line, quick-locate table, long paragraphs, deep headings
  size         pages and topics that outgrew their budget, so bloat is visible
  density      (card mode only) per-owner counts

Two modes. A workspace with digest/ is checked in digest mode: there are no cards and no
placement file, only the source check, links, structure, readability and size. A workspace
with cards is checked the old way.

Errors block delivery. Warnings must be read and either fixed or explained.
"""
from __future__ import annotations
import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import unquote
from digest import safe_name
from hub import scan as hub_scan
from workspace import VERBATIM_KINDS, corpus_text, load_cards, load_owner_map, load_topics, occurs, squash

TEMPLATE_WORDS = ('模板', '骨架', '示意', '示例', '改写', '改进', '整理', '重构', '建议写法',
                  '可复用', '写法', '格式', '结构', 'template', 'example')
HUB = '00_跨话题汇总/'   # written by hub.py from the topic pages; checked for links and structure only
SKIP_FENCES = {'mermaid', 'dataview', 'dataviewjs', 'query', 'tasks'}
UNIT_REF = re.compile(r'[\[【]u\d+(?:[\]｜])')
LEAK = re.compile(r'\b[a-z0-9]{1,8}-b\d{4}#\d+\b|\b(?:unit|src)_[0-9a-f]{24}\b')


def fences(text):
    """Yield (info, start_line, [lines]) for each fenced block and the text outside them."""
    blocks, outside, fence, info, start, body = [], [], None, '', 0, []
    for number, line in enumerate(text.splitlines(), 1):
        match = re.match(r'^\s{0,3}(`{3,}|~{3,})\s*([^\s`]*)', line)
        if match and fence is None:
            fence, info, start, body = match.group(1), match.group(2).lower(), number, []
            outside.append('')
            continue
        if match and fence and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence) and not match.group(2):
            blocks.append((info, start, body))
            fence = None
            outside.append('')
            continue
        if fence is None:
            outside.append(line)
        else:
            body.append(line)
            outside.append('')
    return blocks, outside


def strip_frontmatter(text):
    match = re.match(r'\A---\s*\n.*?\n---\s*(?:\n|$)', text, re.S)
    return text[match.end():] if match else text


def anchors(outside):
    heads = [m.group(1) for line in outside if (m := re.match(r'^#{1,6}\s+(.+?)\s*#*\s*$', line))]
    values = set(heads)
    for head in heads:
        values.add(re.sub(r'[^\w\-\s]', '', head.lower()).replace(' ', '-'))
    values.update('^' + m.group(1) for line in outside if (m := re.search(r'\s\^([\w-]+)\s*$', line)))
    return values


def link_targets(source, text):
    if source.endswith('.canvas'):
        try:
            return [(n['file'], 'canvas') for n in json.loads(text).get('nodes', []) if n.get('type') == 'file']
        except (ValueError, TypeError, KeyError):
            return None
    _, outside = fences(text)
    body = '\n'.join(re.sub(r'`+[^`]*`+', '', line) for line in outside)
    found = [(m.group(1).split('|', 1)[0].replace('\\', ''), 'wiki') for m in re.finditer(r'\[\[([^\]\n]+)\]\]', body)]
    found += [((m.group(1) or m.group(2)), 'markdown')
              for m in re.finditer(r'!?\[[^\]\n]*\]\((?:<([^>]+)>|([^\s)]+))(?:\s+"[^"]*")?\)', body)]
    return found


def resolve_link(source, raw, kind, files):
    """Return the set of vault paths a link could mean."""
    target = raw.partition('#')[0]
    if not target:
        return {source}
    options = set()
    variants = [target] if Path(target).suffix else [target, target + '.md']
    for variant in variants:
        if kind in {'wiki', 'canvas'} and variant in files:
            options.add(variant)
        parts, valid = [], not variant.startswith('/')
        for part in (Path(source).parent / variant).parts:
            if part == '..':
                if parts:
                    parts.pop()
                else:
                    valid = False
            elif part != '.':
                parts.append(part)
        if valid and '/'.join(parts) in files:
            options.add('/'.join(parts))
    if kind == 'wiki' and '/' not in target and not options:
        options.update(p for p in files if Path(p).name in variants)
    return options


def check_links(files):
    errors, graph = [], defaultdict(set)
    for source, text in files.items():
        targets = link_targets(source, text) if source.endswith(('.md', '.canvas')) else []
        if targets is None:
            errors.append({'code': 'INVALID_CANVAS', 'path': source})
            continue
        for raw, kind in targets:
            if re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:', raw) or raw.startswith('//'):
                continue
            raw = unquote(raw)
            options = resolve_link(source, raw, kind, files)
            anchor = raw.partition('#')[2]
            if not options:
                errors.append({'code': 'BROKEN_LINK', 'path': source, 'target': raw})
            elif len(options) > 1:
                errors.append({'code': 'AMBIGUOUS_LINK', 'path': source, 'target': raw})
            else:
                chosen = next(iter(options))
                graph[source].add(chosen)
                if anchor and chosen.endswith('.md') and anchor not in anchors(fences(files[chosen])[1]):
                    errors.append({'code': 'BROKEN_ANCHOR', 'path': source, 'target': raw})
    return errors, graph


def page_metrics(text):
    body = strip_frontmatter(text)
    blocks, outside = fences(body)
    lines = [line.rstrip() for line in outside]
    h1 = [n for n, line in enumerate(lines) if re.match(r'^#\s+\S', line)]
    h2 = [n for n, line in enumerate(lines) if re.match(r'^##\s+\S', line)]
    table_rows = [n for n, line in enumerate(lines) if line.lstrip().startswith('|')]
    paragraphs, current = [], ''
    for line in lines:
        stripped = line.strip()
        plain = stripped and not re.match(r'^(#|\||>|[-*+]\s|\d+[.)]\s|!\[|---|\*\*\*)', stripped)
        if plain:
            current += stripped
        else:
            if current:
                paragraphs.append(current)
            current = ''
    if current:
        paragraphs.append(current)
    lead = ''
    if h1:
        for line in lines[h1[0] + 1:]:
            if line.strip():
                lead = line.strip()
                break
    return {'chars': len(body), 'h1': len(h1), 'h2': len(h2),
            'deep_headings': sum(bool(re.match(r'^#{4,}\s', line)) for line in lines),
            'table_rows': len(table_rows), 'mermaid': sum(info == 'mermaid' for info, _, _ in blocks),
            'code_blocks': sum(info not in SKIP_FENCES for info, _, _ in blocks),
            'lists': sum(bool(re.match(r'^\s*([-*+]|\d+[.)])\s', line)) for line in lines),
            'long_paragraphs': sum(len(p) > 300 for p in paragraphs),
            'lead_ok': bool(lead) and not lead.startswith(('#', '|', '```', '~~~', '- ', '* ')),
            'table_before_second_h2': any(n < (h2[1] if len(h2) > 1 else 10 ** 9) for n in table_rows)}


def code_block_findings(path, text, corpus):
    """Blocks that neither occur in the source nor are introduced as a template."""
    findings = []
    body = strip_frontmatter(text)
    blocks, outside = fences(body)
    for info, start, lines in blocks:
        if info in SKIP_FENCES:
            continue
        checkable = [squash(l) for l in lines if len(squash(l)) >= 4 and re.search(r'[\w一-鿿]', l)]
        if not checkable:
            continue
        hits = sum(occurs(line, corpus) for line in checkable)
        if hits >= 0.8 * len(checkable):
            continue
        before = [l for l in outside[:start - 1] if l.strip()]
        heading = next((l for l in reversed(before) if l.lstrip().startswith('#')), '')
        context = ' '.join(before[-3:] + [heading]).lower()
        if not any(word in context for word in TEMPLATE_WORDS):
            findings.append({'code': 'UNMARKED_NONSOURCE_BLOCK', 'path': path, 'line': start})
    return findings


def card_checks(work, pages, errors):
    """Card mode: every kept card is placed, and exact assets survive into pages."""
    cards, card_errors, _, state = load_cards(work)
    if card_errors or not state:
        errors.append({'code': 'READING_NOT_COMPLETE', 'batches_not_done': sum(s != 'done' for s in state.values())})
    by_label, entries = load_owner_map(work)
    owner_of, kept = {}, {}
    for card in cards:
        entry = by_label.get(str(card.get('owner') or '').strip())
        if entry is None:
            errors.append({'code': 'CARD_OWNER_UNMAPPED', 'card': card['id']})
        elif entry.get('keep', True):
            owner_of[card['id']] = entry['name']
            kept[card['id']] = card

    # placement
    placed, dropped, omitted = defaultdict(set), set(), set()
    placement = work / 'placement.jsonl'
    if not placement.is_file():
        errors.append({'code': 'PLACEMENT_MISSING'})
    else:
        for number, line in enumerate(placement.read_text().splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                errors.append({'code': 'PLACEMENT_INVALID_LINE', 'line': number})
                continue
            if 'page' in row:
                if row['page'] not in pages:
                    errors.append({'code': 'PLACEMENT_PAGE_MISSING', 'page': row['page']})
                for cid in row.get('cards', []):
                    placed[cid].add(row['page'])
            elif 'dropped' in row or 'verbatim_omitted' in row:
                if not str(row.get('reason') or '').strip():
                    errors.append({'code': 'PLACEMENT_REASON_MISSING', 'line': number})
                dropped.update(row.get('dropped', []))
                omitted.update(row.get('verbatim_omitted', []))
        unknown = (set(placed) | dropped | omitted) - {c['id'] for c in cards}
        if unknown:
            errors.append({'code': 'PLACEMENT_UNKNOWN_CARD', 'count': len(unknown), 'first': sorted(unknown)[0]})
        missing = [cid for cid in kept if cid not in placed and cid not in dropped]
        if missing:
            errors.append({'code': 'CARDS_NOT_PLACED', 'count': len(missing), 'first': missing[:10]})

    # verbatim assets survive into reader pages
    squashed = {p: squash(t) for p, t in pages.items()}
    lost = []
    for cid, card in kept.items():
        if card.get('kind') not in VERBATIM_KINDS or cid in dropped or cid in omitted or cid not in placed:
            continue
        for value in card['verbatim']:
            needle = squash(value)
            if not any(occurs(needle, squashed[p]) for p in placed[cid] if p in squashed) \
                    and not any(occurs(needle, t) for t in squashed.values()):
                lost.append(cid)
                break
    if lost:
        errors.append({'code': 'VERBATIM_NOT_IN_PAGE', 'count': len(lost), 'first': lost[:10]})

    return corpus_text(work), placed, kept, owner_of, entries


# A kind of content "has a home" when some page of the topic is named for it or has a
# section headed for it.
HOMES = {'method': r'流程|方法|复盘|协作|工作流|步骤|顺序|原则|写法|规则|清单|模板|策略|怎么|如何',
         'issue': r'排查|问题|故障|报错|症状', 'decision': r'决策|取舍|选型|选择|为什么'}
KIND_NAMES = {'code': '命令', 'term': '术语', 'prompt': '给 AI 的指令', 'method': '方法与流程',
              'issue': '问题与排查', 'decision': '决策'}


def homeless_material(folder, mine, pages, found):
    """Kinds of content the digest clearly holds for this topic but the pages do not carry.

    This is what keeps a page floor honest: a large topic is only called thin when
    specific material has nowhere to live, never because a page count was not reached.
    """
    selection, assets = folder / 'selection.json', folder / 'assets.json'
    if not selection.is_file() or not assets.is_file():
        return None, 0
    data = json.loads(selection.read_text())
    stock = json.loads(assets.read_text())
    picked = Counter(e['kind'] for e in data['exchanges'] if e['picked'] and e['kind'])
    names = ' '.join(Path(p).stem for p in mine)
    heads = ' '.join(m.group(1) for p in mine for m in re.finditer(r'^#{2,3}\s+(.+)$', pages[p], re.M))
    missing = []
    repeated = sum(1 for c in stock.get('commands', []) if c['count'] >= 2)
    if repeated >= 8 and found['commands'] < 5:
        missing.append({'kind': 'code', 'in_source': repeated, 'in_pages': found['commands']})
    explained = sum(1 for t in stock.get('terms', []) if t.get('defined') or t.get('asked'))
    if explained >= 15 and found['terms'] < 8:
        missing.append({'kind': 'term', 'in_source': explained, 'in_pages': found['terms']})
    if picked['prompt'] >= 5 and found['prompts'] < 2:
        missing.append({'kind': 'prompt', 'in_source': picked['prompt'], 'in_pages': found['prompts']})
    for kind, pattern in HOMES.items():
        floor = {'decision': 6, 'issue': 8, 'method': 12}[kind]
        if picked[kind] >= floor and not re.search(pattern, names + ' ' + heads):
            missing.append({'kind': kind, 'in_source': picked[kind], 'in_pages': 0})
    for row in missing:
        row['name'] = KIND_NAMES[row['kind']]
    return missing, data.get('source_chars', 0)


def unwritten_terms(folder, mine, pages):
    """Terms the chat explained or the person asked about, across several conversations, that no page
    of the topic mentions. These are the words a glossary exists for."""
    path = folder / 'assets.json'
    if not path.is_file():
        return []
    lowered = '\n'.join(pages[p] for p in mine).lower()
    out = []
    for row in json.loads(path.read_text()).get('terms', []):
        strong = (row.get('asked') and row.get('defined') and row.get('sessions', 0) >= 3) or \
                 (row.get('lexicon') and row.get('mentions', 0) >= 20 and row.get('defined'))
        names = [n.strip().lower() for n in re.split(r'[（()）]', row['term']) if len(n.strip()) >= 2]
        if strong and names and not any(n in lowered for n in names):
            out.append(row['term'])
    return out


def overlap(a, b):
    """Share of the shorter page's substantial lines that also appear in the other page."""
    def lines(text):
        out = set()
        for line in strip_frontmatter(text).splitlines():
            line = squash(line)
            if len(line) >= 16 and not line.startswith(('#', '| ---', '> ', '---')) and '[[#' not in line:
                out.add(line)
        return out
    one, two = lines(a), lines(b)
    small = min(len(one), len(two))
    return len(one & two) / small if small >= 8 else 0.0


def check(work, vault, thresholds):
    errors, warnings = [], []
    files = {}
    for f in sorted(vault.rglob('*')):
        rel = f.relative_to(vault).as_posix()
        if f.is_file() and not any(part.startswith('.') for part in Path(rel).parts):
            files[rel] = f.read_text(encoding='utf-8-sig') if f.suffix in {'.md', '.canvas'} else ''
    pages = {p: t for p, t in files.items() if p.endswith('.md')}

    digest = (work / 'digest').is_dir()
    placed, kept, owner_of, entries = defaultdict(set), {}, {}, []
    if digest:
        corpus = ' '.join(squash(u['text']) for path in sorted((work / 'digest').glob('*/units.json'))
                          for u in json.loads(path.read_text())['units'])
    else:
        corpus, placed, kept, owner_of, entries = card_checks(work, pages, errors)
    for path, text in pages.items():
        if path.startswith(HUB):
            continue
        errors.extend(code_block_findings(path, text, corpus))
        if LEAK.search(strip_frontmatter(text)) or (digest and UNIT_REF.search(strip_frontmatter(text))):
            errors.append({'code': 'BOOKKEEPING_IN_READER_TEXT', 'path': path})

    link_errors, graph = check_links(files)
    errors.extend(link_errors)

    # structure
    entry = sorted(p for p in pages if '/' not in p and re.match(r'^00_', p))
    if len(entry) != 1:
        errors.append({'code': 'ENTRY_PAGE', 'found': len(entry)})
    else:
        reach, frontier = {entry[0]}, {entry[0]}
        for _ in range(2):
            frontier = {t for s in frontier for t in graph.get(s, ()) if t not in reach}
            reach |= frontier
        for path in sorted(set(pages) - reach):
            errors.append({'code': 'UNREACHABLE_FROM_ENTRY', 'path': path})
    folders = defaultdict(list)
    for path in pages:
        if '/' in path:
            folders[str(Path(path).parent)].append(Path(path).name)
    for folder, names in folders.items():
        numbers = sorted(int(m.group(1)) for n in names if (m := re.match(r'^(\d{2})_', n)))
        if len(numbers) != len(names):
            errors.append({'code': 'PAGE_WITHOUT_NUMBER', 'folder': folder})
        elif numbers != list(range(len(numbers))):
            errors.append({'code': 'NUMBERING_NOT_CONTIGUOUS_FROM_00', 'folder': folder})

    # readability
    metrics = {}
    for path, text in pages.items():
        m = metrics[path] = page_metrics(text)
        if m['h1'] != 1:
            errors.append({'code': 'NEEDS_EXACTLY_ONE_H1', 'path': path})
        if not m['lead_ok']:
            warnings.append({'code': 'NO_LEAD_LINE', 'path': path})
        if m['chars'] > thresholds['quick_locate_chars'] and m['h2'] >= 5 and not m['table_before_second_h2']:
            warnings.append({'code': 'NO_QUICK_LOCATE_TABLE', 'path': path})
        if m['long_paragraphs']:
            warnings.append({'code': 'LONG_PARAGRAPH', 'path': path, 'count': m['long_paragraphs']})
        if m['deep_headings']:
            warnings.append({'code': 'HEADING_DEEPER_THAN_H3', 'path': path})
        if m['chars'] > 1500 and not (m['table_rows'] or m['code_blocks'] or m['mermaid'] or m['lists']):
            warnings.append({'code': 'PLAIN_PROSE_PAGE', 'path': path})

    # size: a note that takes longer to read than the chat it came from has failed
    for path, m in metrics.items():
        # Look-up pages (glossary, commands, instructions) are scanned, not read through: allow half as much again.
        lookup = bool(re.search(r'术语|指令|命令|速查', Path(path).stem))
        if m['chars'] > thresholds.get('page_chars', 6000) * (1.5 if lookup else 1) and not path.startswith(HUB):
            warnings.append({'code': 'PAGE_TOO_LONG', 'path': path, 'chars': m['chars']})
    topics_report = {}
    hub_rows = dict(zip(('terms', 'commands', 'prompts'), hub_scan(vault))) if digest else {}
    for topic in load_topics(work):
        if not topic.get('keep', True):
            continue
        folder = f"{topic.get('category', '')}/{topic['name']}".strip('/') + '/'
        mine = [p for p in metrics if p.startswith(folder)]
        chars = sum(metrics[p]['chars'] for p in mine)
        topics_report[topic['name']] = {'pages': len(mine), 'chars': chars,
                                        'code_blocks': sum(metrics[p]['code_blocks'] for p in mine)}
        if not mine:
            if digest:
                warnings.append({'code': 'TOPIC_NOT_WRITTEN_YET', 'topic': topic['name']})
            continue
        if digest:
            # Thin for what it holds: only when named material has no home.
            found = {k: sum(1 for r in rows if r['topic'] == topic['name']) for k, rows in hub_rows.items()}
            missing, source_chars = homeless_material(work / 'digest' / safe_name(topic['name']), mine, pages, found)
            expected = 8 if source_chars >= 500000 else 4 if source_chars >= 100000 else 1
            topics_report[topic['name']].update({'source_chars': source_chars, 'usual_pages_at_least': expected})
            terms_missing = unwritten_terms(work / 'digest' / safe_name(topic['name']), mine, pages)
            if terms_missing:
                warnings.append({'code': 'TERMS_NOT_IN_PAGES', 'topic': topic['name'], 'count': len(terms_missing),
                                 'terms': terms_missing[:15]})
            if missing and (len(mine) < expected or any(m['kind'] in {'code', 'prompt', 'term'} for m in missing)):
                warnings.append({'code': 'MATERIAL_WITHOUT_A_PAGE', 'topic': topic['name'], 'pages': len(mine),
                                 'usual_pages_at_least': expected, 'missing': missing})
            # Padding: a page with almost nothing on it, or two pages saying the same thing.
            if len(mine) > 1:
                for p in mine:
                    if metrics[p]['chars'] < thresholds.get('thin_page_chars', 500):
                        warnings.append({'code': 'PAGE_TOO_THIN', 'path': p, 'chars': metrics[p]['chars']})
                for i, p in enumerate(mine):
                    for q in mine[i + 1:]:
                        share = overlap(pages[p], pages[q])
                        if share >= 0.3:
                            warnings.append({'code': 'PAGES_REPEAT_EACH_OTHER', 'paths': [p, q], 'share': round(share, 2)})
        if len(mine) > thresholds.get('topic_pages', 12):
            warnings.append({'code': 'TOO_MANY_PAGES', 'topic': topic['name'], 'pages': len(mine)})
        if chars > thresholds.get('topic_chars', 40000):
            warnings.append({'code': 'TOPIC_TOO_LONG', 'topic': topic['name'], 'chars': chars})

    owners = {}
    # density per owner (card mode)
    if not digest:
        for item in entries:
            if not item.get('keep', True):
                continue
            name = item['name']
            mine = [c for cid, c in kept.items() if owner_of[cid] == name]
            own_pages = sorted({p for c in mine for p in placed.get(c['id'], ()) if p in metrics})
            kinds = Counter(c.get('kind') for c in mine)
            total = {k: sum(metrics[p][k] for p in own_pages) for k in ('chars', 'table_rows', 'mermaid', 'code_blocks')}
            owners[name] = {'cards': len(mine), 'kinds': dict(kinds), 'pages': len(own_pages), **total}
            if not mine:
                continue
            if not own_pages:
                errors.append({'code': 'OWNER_HAS_NO_PAGES', 'owner': name})
                continue
            if kinds['command'] + kinds['prompt'] >= thresholds['assets'] and total['code_blocks'] == 0:
                errors.append({'code': 'EXACT_ASSETS_BUT_NO_CODE_BLOCKS', 'owner': name})
            if kinds['term'] >= thresholds['terms'] and not any('术语' in p or 'glossary' in p.lower() for p in own_pages):
                warnings.append({'code': 'MANY_TERMS_BUT_NO_GLOSSARY_PAGE', 'owner': name})
            if kinds['issue'] >= thresholds['issues'] and total['mermaid'] == 0:
                warnings.append({'code': 'MANY_ISSUES_BUT_NO_DIAGRAM', 'owner': name})
            if len(mine) >= thresholds['full_owner_cards'] and len(own_pages) < 4:
                warnings.append({'code': 'LARGE_OWNER_FEW_PAGES', 'owner': name})
            if total['chars'] < thresholds['chars_per_card'] * len(mine):
                warnings.append({'code': 'THIN_OWNER', 'owner': name})
    return {'status': 'FAIL' if errors else 'PASS', 'mode': 'digest' if digest else 'cards',
            'errors': errors, 'warnings': warnings, 'topics': topics_report,
            'owners': owners, 'pages': len(pages),
            'totals': {k: sum(m[k] for m in metrics.values()) for k in ('chars', 'table_rows', 'mermaid', 'code_blocks')},
            'boundary': 'PASS means these mechanical checks found nothing; it does not prove the notes are useful or true'}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('work', type=Path)
    p.add_argument('vault', type=Path)
    p.add_argument('--output', type=Path, help='defaults to <work>/vault_check.json')
    p.add_argument('--full-owner-cards', type=int, default=30)
    p.add_argument('--chars-per-card', type=int, default=120)
    p.add_argument('--page-chars', type=int, default=6000, help='warn when a page is longer than this')
    p.add_argument('--topic-pages', type=int, default=12, help='warn when a topic has more pages than this')
    p.add_argument('--topic-chars', type=int, default=40000, help='warn when a topic is longer than this in total')
    a = p.parse_args()
    vault, work = a.vault.resolve(), a.work.resolve()
    output = (a.output or work / 'vault_check.json').resolve()
    if not vault.is_dir() or vault == output or vault in output.parents:
        p.error('vault must exist and the output must be outside it')
    thresholds = {'quick_locate_chars': 3000, 'assets': 5, 'terms': 8, 'issues': 5,
                  'full_owner_cards': a.full_owner_cards, 'chars_per_card': a.chars_per_card,
                  'page_chars': a.page_chars, 'topic_pages': a.topic_pages, 'topic_chars': a.topic_chars}
    result = check(work, vault, thresholds)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'status': result['status'], 'pages': result['pages'],
                      'errors': dict(Counter(e['code'] for e in result['errors'])),
                      'warnings': dict(Counter(w['code'] for w in result['warnings'])),
                      'report': str(output)}, ensure_ascii=False))
    return 1 if result['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
