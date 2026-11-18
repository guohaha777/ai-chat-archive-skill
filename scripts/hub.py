#!/usr/bin/env python3
"""Build the cross-topic hub: one place to look up terms, commands and AI instructions
that live in many topics.

  hub.py <vault>

Reads the finished pages and writes <vault>/00_跨话题汇总/ with three pages, then adds a
section linking them to the entry page. Nothing is moved: every row links back to the
page and section it came from, which stays the place to edit. Run it again after any
change to the pages; the hub is rebuilt from scratch each time.

What is collected:
  术语    rows of tables whose first column is headed 术语 / 概念 / 名词 / 词汇 / 风格
  命令    fenced blocks that are commands (bash, sql, or lines starting with git, npm, ...)
  AI 指令  plain-text fenced blocks that read like an instruction, or sit under a heading
          that mentions 指令 / 提示词 / prompt
"""
from __future__ import annotations
import argparse
import json
import re
from collections import defaultdict
from pathlib import Path
from digest import COMMAND_HEAD, SQL_HEAD, looks_like_commands, looks_like_prompt

HUB = '00_跨话题汇总'
PAGES = {'terms': '00_术语总览.md', 'commands': '01_命令总览.md', 'prompts': '02_AI指令总览.md'}
SECTION = '## 跨话题汇总'
TERM_HEAD = re.compile(r'^(?:术语|概念|名词|词汇|风格|叫法|专有名词|关键词)(?:[（(].*)?$')
PROMPT_HEAD = re.compile(r'指令|提示词|prompt', re.I)
FENCE = re.compile(r'^\s{0,3}(`{3,}|~{3,})\s*([^\s`]*)')
TOOL_NAMES = {'sql': 'SQL', 'git': 'Git', 'docker': 'Docker', 'docker-compose': 'Docker', 'npm': 'npm 与 Node',
              'npx': 'npm 与 Node', 'node': 'npm 与 Node', 'pnpm': 'npm 与 Node', 'yarn': 'npm 与 Node',
              'ssh': 'SSH 与远程', 'scp': 'SSH 与远程', 'curl': '网络请求', 'wget': '网络请求',
              'psql': '数据库', 'pg_dump': '数据库', 'pg_restore': '数据库', 'supabase': '数据库',
              'nginx': 'Nginx 与服务', 'systemctl': 'Nginx 与服务', 'journalctl': 'Nginx 与服务', 'certbot': 'Nginx 与服务',
              'brew': '安装与环境', 'pip': '安装与环境', 'pip3': '安装与环境', 'python': '安装与环境',
              'python3': '安装与环境', 'apt': '安装与环境', 'apt-get': '安装与环境', 'export': '安装与环境',
              'source': '安装与环境', 'echo': '安装与环境', 'conda': '安装与环境', 'uv': '安装与环境',
              'claude': 'AI 工具', 'codex': 'AI 工具', 'ollama': 'AI 工具', 'ffmpeg': '音视频'}


def strip_frontmatter(text):
    match = re.match(r'\A---\s*\n.*?\n---\s*(?:\n|$)', text, re.S)
    return text[match.end():] if match else text


def cells_of(line):
    body = line.strip()
    body = body[1:] if body.startswith('|') else body
    body = body[:-1] if body.endswith('|') and not body.endswith('\\|') else body
    return [c.strip().replace('\\|', '|') for c in re.split(r'(?<!\\)\|', body)]


def plain(text):
    """Table-cell text without emphasis marks, for comparing and sorting."""
    return re.sub(r'[*_`]', '', text).strip()


def topic_of(rel):
    parts = Path(rel).parts
    return parts[1] if len(parts) >= 3 else parts[0] if len(parts) == 2 else Path(rel).stem


def scan(vault):
    """Collect term rows, command blocks and instruction blocks from every topic page."""
    terms, commands, prompts = [], [], []
    for path in sorted(vault.rglob('*.md')):
        rel = path.relative_to(vault).as_posix()
        parts = Path(rel).parts
        if len(parts) < 2 or parts[0] == HUB or any(p.startswith('.') for p in parts):
            continue
        topic = topic_of(rel)
        lines = strip_frontmatter(path.read_text(encoding='utf-8-sig')).splitlines()
        heading, last_prose, fence, lang, body = '', '', None, '', []
        table_head = None
        for index, line in enumerate(lines):
            match = FENCE.match(line)
            if fence is None and match:
                fence, lang, body = match.group(1), match.group(2).lower(), []
                table_head = None
                continue
            if fence is not None:
                if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence) and not match.group(2):
                    inner = '\n'.join(body).strip('\n')
                    where = {'topic': topic, 'page': rel, 'heading': heading, 'note': last_prose}
                    if inner and looks_like_commands(lang, inner):
                        commands.append({**where, 'text': inner, 'lang': lang or 'text'})
                    elif inner and lang in {'', 'text', 'markdown', 'md', 'txt'} and len(inner) >= 20 and (
                            PROMPT_HEAD.search(heading) or PROMPT_HEAD.search(last_prose) or looks_like_prompt(lang, inner)):
                        prompts.append({**where, 'text': inner})
                    fence = None
                else:
                    body.append(line)
                continue
            head = re.match(r'^(#{2,3})\s+(.+?)\s*#*\s*$', line)
            if head:
                heading, table_head, last_prose = head.group(2), None, ''
                continue
            stripped = line.strip()
            if stripped.startswith('|'):
                row = cells_of(stripped)
                if re.fullmatch(r'[\s:|-]+', stripped):
                    continue
                following = lines[index + 1].strip() if index + 1 < len(lines) else ''
                if re.fullmatch(r'\|?[\s:|-]+\|?', following) and '-' in following:
                    table_head = row if TERM_HEAD.match(plain(row[0])) and len(row) >= 2 else None
                    continue
                if table_head and len(row) >= 2 and plain(row[0]):
                    terms.append({'topic': topic, 'page': rel, 'heading': heading, 'term': row[0],
                                  'meaning': row[1], 'extra': '；'.join(c for c in row[2:] if c)})
                continue
            table_head = None
            if stripped and not stripped.startswith(('>', '---')):
                last_prose = re.sub(r'[：:]\s*$', '', plain(stripped))[:80]
    return terms, commands, prompts


def link(item, label=None):
    target = item['page'][:-3]
    anchor = f"#{item['heading']}" if item['heading'] else ''
    text = label or (f"{item['topic']} · {item['heading']}" if item['heading'] else item['topic'])
    return f'[[{target}{anchor}\\|{text}]]'


def cell(text):
    return text.replace('|', '\\|').replace('\n', ' ')


def term_key(term):
    """`RLS（Row Level Security）` and `RLS` are the same entry."""
    return re.split(r'[（(/]', plain(term))[0].strip().lower()


def page(title, tags, lead, body):
    return '\n'.join(['---', f'tags: [跨话题汇总, {tags}]', '---', '', f'# {title}', '', f'> {lead}', ''] + body) + '\n'


def build_terms(terms):
    by_topic, by_key = defaultdict(list), defaultdict(list)
    for row in terms:
        by_topic[row['topic']].append(row)
        by_key[term_key(row['term'])].append(row)
    shared = {k: rows for k, rows in by_key.items() if len({r['topic'] for r in rows}) >= 2}
    body = ['## 快速定位', '', '| 想查什么 | 去这里 |', '| --- | --- |']
    if shared:
        body.append(f'| 在几个话题里都出现的词（{len(shared)} 个） | [[#多个话题都有的术语]] |')
    for topic in by_topic:
        body.append(f'| {topic} 的术语（{len(by_topic[topic])} 个） | [[#{topic}]] |')
    body.append('')
    if shared:
        body += ['## 多个话题都有的术语', '', '同一个词在不同话题里的说法放在一起看，含义有出入时以各自的语境为准。', '',
                 '| 术语 | 各话题里的含义 |', '| --- | --- |']
        for key in sorted(shared, key=lambda k: (-len({r['topic'] for r in shared[k]}), k)):
            rows = shared[key]
            seen, parts = set(), []
            for row in rows:
                if row['topic'] in seen:
                    continue
                seen.add(row['topic'])
                parts.append(f"{link(row, row['topic'])}：{cell(row['meaning'])}")
            body.append(f"| {cell(rows[0]['term'])} | {'<br>'.join(parts)} |")
        body.append('')
    for topic, rows in by_topic.items():
        body += [f'## {topic}', '', '| 术语 | 含义 | 出处 |', '| --- | --- | --- |']
        for row in sorted(rows, key=lambda r: (r['page'], r['heading'])):
            body.append(f"| {cell(row['term'])} | {cell(row['meaning'])} | {link(row, row['heading'] or '原页')} |")
        body.append('')
    body += ['---', '', '本页由脚本从各话题的术语表汇总生成，不要直接改这里：改原页面，再重新生成。']
    lead = f'全库 {len(terms)} 条术语的总入口，来自 {len(by_topic)} 个话题。先按词找，再点出处回到原页面看上下文。'
    return page('术语总览', '术语表', lead, body)


def tool_of(text):
    rows = [l for l in text.splitlines() if l.strip() and not l.strip().startswith('#')]
    # `cd project` before the real command says nothing about what the block is for.
    first = next((l for l in rows if not re.match(r'^\s*(?:\$\s+)?cd\b', l)), rows[0] if rows else '')
    match = COMMAND_HEAD.match(first)
    if match:
        return TOOL_NAMES.get(match.group(1), '文件与系统')
    return 'SQL' if SQL_HEAD.match(first) else '其他'


def build_commands(commands):
    merged = {}
    for row in commands:
        key = re.sub(r'\s+', ' ', row['text']).strip()
        entry = merged.setdefault(key, {**row, 'sources': []})
        if not any(s['page'] == row['page'] and s['heading'] == row['heading'] for s in entry['sources']):
            entry['sources'].append(row)
    groups = defaultdict(list)
    for entry in merged.values():
        groups[tool_of(entry['text'])].append(entry)
    order = sorted(groups, key=lambda g: (g == '其他', -len(groups[g]), g))
    body = ['## 快速定位', '', '| 哪类命令 | 条数 | 来自 | 去这里 |', '| --- | --- | --- | --- |']
    for group in order:
        topics = list(dict.fromkeys(s['topic'] for e in groups[group] for s in e['sources']))
        body.append(f"| {group} | {len(groups[group])} | {'、'.join(topics)} | [[#{group}]] |")
    body.append('')
    for group in order:
        body += [f'## {group}', '']
        for entry in sorted(groups[group], key=lambda e: (e['topic'], e['page'])):
            sources = '；'.join(link(s) for s in entry['sources'])
            note = entry['note'] or entry['heading'] or '（原页没有说明）'
            body += [f'**{note}**　出处：{sources}', '', f"```{entry['lang']}", entry['text'], '```', '']
    body += ['---', '', '本页由脚本从各话题页面里的命令代码块汇总生成，原样复制。执行位置、前提和风险看出处页面；',
             '不要直接改这里：改原页面，再重新生成。']
    lead = (f'全库 {len(merged)} 条命令的总入口，按工具分组，跨项目通用的（Git 上传、启动本地项目、Docker）在这里一次找到。'
            '带 `sudo`、`rm`、`reset --hard`、`push -f` 的命令有破坏性，先看出处的说明。')
    return page('命令总览', '指令与命令', lead, body)


def build_prompts(prompts):
    by_topic = defaultdict(list)
    for row in prompts:
        by_topic[row['topic']].append(row)
    body = ['## 快速定位', '', '| 话题 | 条数 | 去这里 |', '| --- | --- | --- |']
    for topic, rows in by_topic.items():
        body.append(f'| {topic} | {len(rows)} | [[#{topic}]] |')
    body.append('')
    for topic, rows in by_topic.items():
        body += [f'## {topic}', '', '| 用来做什么 | 开头 | 原文在 |', '| --- | --- | --- |']
        for row in rows:
            opening = re.sub(r'\s+', ' ', row['text'])[:70]
            what = row['note'] or row['heading'] or '（原页没有说明）'
            body.append(f"| {cell(what)} | {cell(opening)}… | {link(row, row['heading'] or '原页')} |")
        body.append('')
    body += ['---', '', '本页只列出每条指令的用途和开头，完整原文在出处页面。由脚本汇总生成，不要直接改这里。']
    lead = f'全库 {len(prompts)} 条给 AI 的指令和提示词的总入口，来自 {len(by_topic)} 个话题。想起“上次是怎么跟 AI 说的”，从这里找。'
    return page('AI 指令总览', '指令与命令', lead, body)


def update_entry(vault, counts):
    entries = sorted(p for p in vault.glob('00_*.md'))
    if len(entries) != 1:
        return None
    text = entries[0].read_text(encoding='utf-8-sig')
    block = [SECTION, '', '术语、命令、给 AI 的指令分散在各个话题里，这里把它们汇到一处，每条都能点回原页面。', '',
             '| 想找什么 | 条数 | 入口 |', '| --- | --- | --- |']
    names = {'terms': '一个词是什么意思、该用哪个词', 'commands': '那条命令怎么写', 'prompts': '上次怎么跟 AI 说的'}
    for key, name in names.items():
        if counts[key]:
            block.append(f"| {name} | {counts[key]} | [[{HUB}/{PAGES[key][:-3]}\\|{PAGES[key][3:-3]}]] |")
    block = '\n'.join(block) + '\n'
    pattern = re.compile(rf'^{re.escape(SECTION)}\n.*?(?=^## |\Z)', re.S | re.M)
    if pattern.search(text):
        text = pattern.sub(lambda _: block + '\n', text, count=1)
    else:
        first = re.search(r'^## ', text, re.M)
        at = first.start() if first else len(text)
        text = text[:at] + block + '\n' + text[at:]
    entries[0].write_text(text)
    return entries[0].name


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('vault', type=Path)
    a = p.parse_args()
    vault = a.vault.resolve()
    if not vault.is_dir():
        p.error('vault does not exist')
    terms, commands, prompts = scan(vault)
    folder = vault / HUB
    folder.mkdir(exist_ok=True)
    built = {'terms': (terms, build_terms), 'commands': (commands, build_commands), 'prompts': (prompts, build_prompts)}
    counts, number = {}, 0
    for old in folder.glob('*.md'):
        old.unlink()
    for key, (rows, builder) in built.items():
        counts[key] = len(rows)
        if not rows:
            continue
        # Keep numbering contiguous when a kind is absent.
        name = f"{number:02d}_{PAGES[key][3:]}"
        PAGES[key] = name
        (folder / name).write_text(builder(rows))
        number += 1
    if not number:
        folder.rmdir()
    entry = update_entry(vault, counts) if number else None
    print(json.dumps({'terms': counts['terms'], 'commands': counts['commands'], 'prompts': counts['prompts'],
                      'topics': len({r['topic'] for r in terms + commands + prompts}),
                      'folder': str(folder), 'entry_updated': entry}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
