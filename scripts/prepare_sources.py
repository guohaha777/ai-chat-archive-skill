#!/usr/bin/env python3
"""Read-only source preparation. Never interpret or rank semantic value.

Supports chat exports with `chat_messages`, `messages` or `mapping`, message JSONL,
Markdown, text, plain HTML and activity-log HTML (one prompt/response per card).
"""
from __future__ import annotations
import argparse
import hashlib
import json
import re
from pathlib import Path
from text_support import (clean_html, render_chat_messages, parse_activity_html,
                          group_activity_sessions)


def digest(value):
    if isinstance(value, str):
        value = value.encode('utf-8')
    return hashlib.sha256(value).hexdigest()


# Mapping-style exports mix the visible dialogue with tool traffic and model reasoning.
HIDDEN_CONTENT_TYPES = {'thoughts', 'reasoning_recap', 'user_editable_context', 'model_editable_context',
                        'execution_output', 'tether_browsing_display', 'tether_quote', 'system_error'}


def chunks(text, limit):
    """Never split a fenced block or a paragraph; oversize blocks stay visible."""
    blocks, current, fence = [], [], None
    for line in text.splitlines(keepends=True):
        match = re.match(r'^\s{0,3}(`{3,}|~{3,})', line)
        current.append(line)
        if match:
            token = match.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
        if not line.strip() and fence is None:
            blocks.append(''.join(current))
            current = []
    if current:
        blocks.append(''.join(current))
    result, pending = [], ''
    for block in blocks:
        if pending and len(pending) + len(block) > limit:
            result.append(pending)
            pending = ''
        pending += block
    if pending:
        result.append(pending)
    return result or ['']


def conversations(data):
    if isinstance(data, dict):
        if any(k in data for k in ('chat_messages', 'messages', 'mapping')):
            return [data]
        raise ValueError('unsupported_json_schema')
    if isinstance(data, list) and data:
        if all(isinstance(x, dict) and any(k in x for k in ('chat_messages', 'messages', 'mapping')) for x in data):
            return data
        if all(isinstance(x, dict) and ('role' in x or 'sender' in x) for x in data):
            groups = {}
            for x in data:
                key = str(x.get('conversation_id') or x.get('session_id') or 'single')
                groups.setdefault(key, []).append(x)
            return [{'id': k if k != 'single' else None, 'messages': v} for k, v in groups.items()]
    raise ValueError('unsupported_json_schema')


def messages(conversation):
    mapping = conversation.get('mapping')
    if isinstance(mapping, dict):
        active, node = set(), conversation.get('current_node')
        while node is not None:
            if node in active or node not in mapping:
                raise ValueError('invalid_active_branch')
            active.add(node)
            node = mapping[node].get('parent')
        ordered, visiting, done = [], set(), set()

        def visit(key):
            if key in done:
                return
            if key in visiting or key not in mapping:
                raise ValueError('invalid_parent_graph')
            visiting.add(key)
            parent = mapping[key].get('parent')
            if parent is not None:
                visit(parent)
            visiting.remove(key)
            done.add(key)
            raw = mapping[key].get('message')
            if raw:
                msg = dict(raw)
                content = raw.get('content', {})
                if isinstance(content, dict) and isinstance(content.get('parts'), list):
                    msg['content'] = [({'type': 'text', 'text': p} if isinstance(p, str) else p) for p in content['parts']]
                msg['role'] = (raw.get('author') or {}).get('role', 'unknown')
                msg['timestamp'] = raw.get('create_time')
                msg['id'] = raw.get('id') or key
                msg['_parent'] = parent
                msg['_active'] = key in active if active else None
                meta = raw.get('metadata') or {}
                kind = content.get('content_type') if isinstance(content, dict) else None
                if (msg['role'] in {'tool', 'system'} or kind in HIDDEN_CONTENT_TYPES
                        or raw.get('recipient') not in (None, 'all')
                        or meta.get('is_visually_hidden_from_conversation')):
                    msg['_process'] = True
                ordered.append(msg)
        for key in mapping:
            visit(key)
        return ordered
    rows = conversation.get('chat_messages', conversation.get('messages'))
    if not isinstance(rows, list) or not all(isinstance(x, dict) for x in rows):
        raise ValueError('invalid_message_list')
    return rows


def activity_conversations(text, gap_minutes):
    """Turn an activity log into inferred sessions; identity is the earliest record."""
    items = []
    for number, session in enumerate(group_activity_sessions(parse_activity_html(text), gap_minutes)):
        rows = []
        for n, record in enumerate(session):
            stamp = record['timestamp']
            key = f"{stamp or n}:{digest(record['prompt'])[:10]}"
            prompt = record['prompt']
            if record['attachments']:
                prompt += '\n' + '\n'.join(f'[attachment] {name}' for name in record['attachments'])
            if prompt.strip():
                rows.append({'role': 'user', 'text': prompt, 'timestamp': stamp, 'id': 'q:' + key,
                             '_files': record['attachments']})
            if record['response'].strip():
                rows.append({'role': 'assistant', 'text': record['response'], 'timestamp': stamp, 'id': 'a:' + key})
        if rows:
            first = session[0]
            items.append({'id': 'activity:' + f"{first['timestamp'] or 'n%d' % number}:{digest(first['prompt'])[:10]}",
                          'title': first['prompt'].strip().splitlines()[0][:60] if first['prompt'].strip() else '',
                          'messages': rows, '_activity': True, '_dated': first['timestamp'] is not None})
    return items


def prepare(root, out, scope, baseline=None, limit=12000, gap_minutes=45, conversation_only=True):
    root, out = Path(root).resolve(), Path(out).resolve()
    if out == root or root in out.parents or out in root.parents:
        raise ValueError('source_and_output_must_be_separate')
    if out.exists() and any(out.iterdir()):
        raise ValueError('output_must_be_new_or_empty')
    old = json.loads(Path(baseline).read_text()) if baseline else {'sources': []}
    if baseline and old.get('scope_id') != scope:
        raise ValueError('baseline_scope_mismatch')
    prior = {x['source_id']: x for x in old['sources']}
    sources, units, attachments = [], [], []
    seen = set()
    for path in sorted(root.rglob('*')):
        rel = path.relative_to(root).as_posix()
        if any(p.startswith('.') for p in Path(rel).parts) or not path.is_file():
            continue
        if path.is_symlink() or root not in path.resolve().parents:
            attachments.append({'locator': rel, 'status': 'symlink_not_read'})
            continue
        if path.suffix.lower() not in {'.json', '.jsonl', '.md', '.txt', '.html', '.htm'}:
            attachments.append({'locator': rel, 'status': 'registered_not_processed'})
            continue
        try:
            raw = path.read_bytes()
            if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
                text = raw.decode('utf-16')
            else:
                text = raw.decode('utf-8-sig')
            ext = path.suffix.lower()
            if ext == '.json':
                items = conversations(json.loads(text))
            elif ext == '.jsonl':
                items = conversations([json.loads(x) for x in text.splitlines() if x.strip()])
            elif ext in {'.html', '.htm'} and (activity := activity_conversations(text, gap_minutes)):
                items = activity
            else:
                items = [{'messages': [{'role': 'unknown', 'text': clean_html(text) if ext in {'.html', '.htm'} else text}]}]
            for ordinal, item in enumerate(items):
                native = item.get('uuid') or item.get('id') or item.get('conversation_id')
                identity = ('native:' + str(native)) if native else f'file:{rel}:{ordinal}'
                sid = 'src_' + digest(scope + ':' + identity)[:24]
                rows = messages(item)
                source_hash = digest(json.dumps(item, ensure_ascii=False, sort_keys=True))
                if sid in seen:
                    existing = next(x for x in sources if x['source_id'] == sid)
                    if existing['content_hash'] == source_hash:
                        existing.setdefault('duplicate_locators', []).append(rel)
                        continue
                    raise ValueError('duplicate_source_id_with_different_content_use_separate_snapshots')
                # Prepare locally so failure cannot leave a half-ready source.
                local_units, local_attachments = [], []
                warnings = []
                if item.get('_activity'):
                    warnings.append('activity_sessions_inferred_by_time_gap' if item.get('_dated')
                                    else 'activity_order_and_sessions_unknown')
                elif ext in {'.html', '.htm'}:
                    warnings.append('html_conversation_boundaries_not_reconstructed')
                if isinstance(item.get('mapping'), dict) and not item.get('current_node'):
                    warnings.append('active_branch_unknown_all_nodes_preserved')
                left_out = {}
                for index, message in enumerate(rows):
                    if conversation_only and message.get('_process'):
                        size = len(json.dumps(message.get('content'), ensure_ascii=False, default=str))
                        left_out['tool_or_hidden_message'] = left_out.get('tool_or_hidden_message', 0) + size
                        continue
                    rendered, attached, stats = render_chat_messages([message], conversation_only)
                    for category, size in stats['omitted_chars'].items():
                        left_out[category] = left_out.get(category, 0) + size
                    # A message that was nothing but tool traffic leaves only its header line.
                    bare = conversation_only and '\n' not in rendered.strip()
                    rendered = rendered.replace('[message 0 |', f'[message {index} |', 1)
                    # Stable role and native identity live in structured fields as well.
                    mid = str(message.get('uuid') or message.get('id') or index)
                    for part, body in enumerate([] if bare else chunks(rendered, limit)):
                        uid = 'unit_' + digest(f'{sid}:{mid}:{part}:{digest(body)}')[:24]
                        local_units.append({'unit_id': uid, 'source_id': sid, 'message_id': mid,
                            'message_index': index, 'part': part,
                            'role': message.get('role') or message.get('sender') or 'unknown',
                            'timestamp': message.get('created_at') or message.get('timestamp'),
                            'parent_id': message.get('_parent'), 'active_branch': message.get('_active'),
                            'text': body, 'content_hash': digest(body), 'oversize': len(body) > limit})
                    if stats['unrecovered_placeholder_message_count']:
                        warnings.append('unsupported_text_placeholder')
                    for a in attached:
                        local_attachments.append({'source_id': sid, 'message_id': mid,
                            'status': 'processed_embedded_text' if a['embedded_text'] else 'registered_not_processed',
                            'metadata': a['metadata']})
                    def register_media(value):
                        if isinstance(value, list):
                            for child in value:
                                register_media(child)
                        elif isinstance(value, dict):
                            kind = str(value.get('type') or value.get('content_type') or '')
                            if kind in {'image', 'image_url', 'input_image', 'image_asset_pointer',
                                        'document', 'input_file', 'audio', 'input_audio', 'video'}:
                                local_attachments.append({'source_id': sid, 'message_id': mid,
                                    'status': 'registered_not_processed', 'media_type': kind})
                            else:
                                for child in value.values():
                                    if isinstance(child, (dict, list)):
                                        register_media(child)
                    register_media(message.get('content'))
                    for name in message.get('_files', []):
                        local_attachments.append({'source_id': sid, 'message_id': mid,
                            'status': 'registered_not_processed', 'file_name': name})
                seen.add(sid)
                previous = prior.get(sid)
                stamps = [str(u['timestamp']) for u in local_units if u['timestamp']]
                sources.append({'source_id': sid, 'locator': rel, 'content_hash': source_hash,
                    'title': str(item.get('name') or item.get('title') or '')[:80],
                    'left_out_chars': left_out,
                    'started_at': min(stamps) if stamps else None,
                    'status': 'ready' if local_units else 'empty', 'warnings': warnings,
                    'change': 'unchanged' if previous and previous.get('content_hash') == source_hash else 'changed' if previous else 'added',
                    'unit_count': len(local_units)})
                units.extend(local_units)
                attachments.extend(local_attachments)
        except (ValueError, UnicodeError, TypeError, KeyError, RecursionError) as exc:
            # No raw exception text: JSON errors or paths can contain private text.
            sources.append({'source_id': 'failed_' + digest(scope + ':' + rel)[:24],
                'locator': rel, 'error_type': type(exc).__name__,
                'status': 'not_a_conversation' if str(exc) == 'unsupported_json_schema' else 'parse_failed',
                'unit_count': 0, 'change': 'unresolved'})
    missing = sorted(set(prior) - seen)
    manifest = {'schema_version': 1, 'scope_id': scope, 'sources': sources,
                'missing_source_ids': missing, 'attachments': attachments,
                'boundary': 'missing_sources_do_not_authorize_knowledge_deletion'}
    out.mkdir(parents=True, exist_ok=True)
    (out / 'source_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    (out / 'corpus_units.jsonl').write_text(''.join(json.dumps(u, ensure_ascii=False) + '\n' for u in units))
    return manifest, units


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('source_root', type=Path)
    p.add_argument('output_dir', type=Path)
    p.add_argument('--scope-id', required=True)
    p.add_argument('--baseline', type=Path)
    p.add_argument('--chunk-chars', type=int, default=12000)
    p.add_argument('--include-tool-blocks', action='store_true',
                   help='also keep tool calls, tool results and model reasoning (default: dialogue only)')
    p.add_argument('--activity-gap-minutes', type=int, default=45,
                   help='activity-log records closer than this are treated as one session')
    a = p.parse_args()
    if not a.source_root.is_dir() or a.chunk_chars < 100:
        p.error('source_root must exist and chunk-chars must be >= 100')
    try:
        manifest, units = prepare(a.source_root, a.output_dir, a.scope_id, a.baseline, a.chunk_chars,
                                  a.activity_gap_minutes, not a.include_tool_blocks)
    except (ValueError, OSError):
        p.exit(2, 'Preparation refused; check separate paths, empty output and baseline scope.\n')
    failures = sum(s['status'] == 'parse_failed' for s in manifest['sources'])
    other = sum(s['status'] == 'not_a_conversation' for s in manifest['sources'])
    print(json.dumps({'sources': len(manifest['sources']), 'units': len(units), 'failures': failures,
                      'not_a_conversation': other, 'chars': sum(len(u['text']) for u in units),
                      'left_out_chars': {k: sum(s.get('left_out_chars', {}).get(k, 0) for s in manifest['sources'])
                                         for k in sorted({k for s in manifest['sources'] for k in s.get('left_out_chars', {})})},
                      'attachments': len(manifest['attachments'])}))
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
