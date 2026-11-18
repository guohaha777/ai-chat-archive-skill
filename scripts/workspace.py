#!/usr/bin/env python3
"""Shared readers for the private workspace: batches, extraction cards, owner map.

Nothing here judges whether knowledge is valuable. It only checks that every unit was
accounted for and that text claimed to be verbatim really occurs in the source.
"""
from __future__ import annotations
import json
import re
from pathlib import Path

VERBATIM_KINDS = {'command', 'prompt'}
KIND_ORDER = ['overview', 'concept', 'method', 'decision', 'issue', 'command', 'prompt',
              'term', 'reflection', 'material', 'other']
PLACEHOLDER = re.compile(r'<[^<>\n]{1,40}>')
OTHER = '其他'  # owner for content that belongs to none of the chosen topics


def squash(text):
    """Collapse whitespace so line wrapping and indentation do not break comparisons."""
    return re.sub(r'\s+', ' ', text).strip()


def occurs(needle, haystack):
    """True when `needle` occurs in `haystack`, both already squashed.

    A placeholder such as <TOKEN> in the needle matches any short run of text, so a
    redacted secret does not make an otherwise exact command fail the check.
    """
    if not needle:
        return False
    if needle in haystack:
        return True
    pieces = [p.strip() for p in PLACEHOLDER.split(needle)]
    pieces = [p for p in pieces if p]
    if len(pieces) < 1 or not PLACEHOLDER.search(needle):
        return False
    if sum(len(p) for p in pieces) < 4:
        return False
    pattern = r'.{0,200}?'.join(re.escape(p) for p in pieces)
    return re.search(pattern, haystack, re.S) is not None


def expand_refs(values, known):
    """Expand ["u3", "u5-u8"] into an ordered list; unknown references are returned separately."""
    found, unknown = [], []
    if isinstance(values, str):
        values = [values]
    for value in values or []:
        match = re.fullmatch(r'u(\d+)(?:\s*-\s*u?(\d+))?', str(value).strip())
        if not match:
            unknown.append(str(value))
            continue
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if end < start:
            unknown.append(str(value))
            continue
        for number in range(start, end + 1):
            ref = f'u{number}'
            (found if ref in known else unknown).append(ref)
    return found, unknown


def load_batches(work):
    batches = {}
    for path in sorted((Path(work) / 'batches').glob('*.json')):
        data = json.loads(path.read_text())
        batches[data['batch_id']] = data
    return batches


def check_batch(batch, path):
    """Validate one card file against its batch. Returns (cards, errors, warnings)."""
    errors, warnings, cards = [], [], []
    bid = batch['batch_id']

    def err(code, **extra):
        errors.append({'code': code, 'batch': bid, **extra})

    if not path.is_file():
        err('MISSING_CARD_FILE')
        return cards, errors, warnings
    try:
        data = json.loads(path.read_text())
    except (ValueError, UnicodeError):
        err('INVALID_JSON')
        return cards, errors, warnings
    if not isinstance(data, dict) or data.get('batch_id') != bid:
        err('BATCH_ID_MISMATCH')
        return cards, errors, warnings
    units = {u['ref']: u for u in batch['units']}
    text = {ref: squash(u['text']) for ref, u in units.items()}
    whole = ' '.join(text[ref] for ref in units)
    accounted, seen_ids = set(), set()
    for row in data.get('cards') or []:
        cid = str(row.get('id', '')).strip()
        if not cid or cid in seen_ids:
            err('BAD_CARD_ID', card=cid)
            continue
        seen_ids.add(cid)
        missing = [k for k in ('owner', 'kind', 'title', 'content') if not str(row.get(k) or '').strip()]
        if missing:
            err('MISSING_FIELD', card=cid, fields=missing)
        allowed = batch.get('owners')
        if allowed and str(row.get('owner') or '').strip() not in set(allowed) | {OTHER}:
            err('OWNER_NOT_A_TOPIC', card=cid)
        refs, unknown = expand_refs(row.get('units'), units)
        if unknown or not refs:
            err('BAD_UNIT_REF', card=cid)
        accounted.update(refs)
        verbatim = row.get('verbatim') or []
        if isinstance(verbatim, str):
            verbatim = [verbatim]
        if row.get('kind') in VERBATIM_KINDS and not any(str(v).strip() for v in verbatim):
            err('VERBATIM_REQUIRED', card=cid)
        for index, value in enumerate(verbatim):
            if not occurs(squash(str(value)), whole):
                err('VERBATIM_NOT_IN_SOURCE', card=cid, index=index)
        cards.append({**row, 'id': f'{bid}#{cid}', 'batch': bid, 'verbatim': [str(v) for v in verbatim],
                      'unit_refs': refs,
                      'sources': sorted({units[r]['source_id'] for r in refs})})
    skipped_chars = 0
    for row in data.get('skipped') or []:
        refs, unknown = expand_refs(row.get('units'), units)
        if unknown or not refs:
            err('BAD_UNIT_REF', where='skipped')
        if not str(row.get('reason') or '').strip():
            err('SKIP_WITHOUT_REASON')
        skipped_chars += sum(units[r]['chars'] for r in refs if r not in accounted)
        accounted.update(refs)
    left = [ref for ref in units if ref not in accounted]
    if left:
        err('UNITS_NOT_ACCOUNTED', count=len(left), first=left[0])
    if not data.get('review_note'):
        if batch['chars'] > 5000 and skipped_chars > 0.7 * batch['chars']:
            warnings.append({'code': 'MOSTLY_SKIPPED', 'batch': bid})
        if batch['chars'] > 15000 and len(cards) < 3:
            warnings.append({'code': 'FEW_CARDS', 'batch': bid})
    return cards, errors, warnings


def load_cards(work, only=None):
    """Return (cards, errors, warnings, state) where state maps batch id to done/invalid/pending."""
    work = Path(work)
    cards, errors, warnings, state = [], [], [], {}
    for bid, batch in load_batches(work).items():
        if only and bid not in only:
            continue
        path = work / 'cards' / f'{bid}.json'
        got, errs, warns = check_batch(batch, path)
        if not path.is_file():
            state[bid] = 'pending'
        elif errs:
            state[bid] = 'invalid'
        else:
            state[bid] = 'done'
            cards.extend(got)
        errors.extend(errs)
        warnings.extend(warns)
    return cards, errors, warnings, state


def load_topics(work):
    path = Path(work) / 'topics.json'
    return json.loads(path.read_text()).get('topics', []) if path.is_file() else []


def load_owner_map(work):
    """Return (label -> owner entry, list of entries).

    Ownership comes from topics.json: every chosen topic is an owner whose folder is
    <category>/<name>. Topics not chosen, and the catch-all '其他', are not written.
    An owner_map.json, when present, overrides this (older workspaces).
    """
    path = Path(work) / 'owner_map.json'
    if path.is_file():
        entries = json.loads(path.read_text()).get('owners', [])
    else:
        entries = []
        for topic in load_topics(work):
            keep = bool(topic.get('keep', True))
            entries.append({'name': topic['name'], 'keep': keep,
                            'folder': f"{topic.get('category', '')}/{topic['name']}".strip('/'),
                            'reason': '' if keep else '本次未选这个话题'})
        if entries:
            entries.append({'name': OTHER, 'keep': False, 'reason': '不属于任何所选话题'})
    by_label = {}
    for entry in entries:
        for label in [entry.get('name')] + list(entry.get('labels') or []):
            if label:
                by_label[str(label).strip()] = entry
    return by_label, entries


def corpus_text(work):
    """All prepared source text, squashed, for 'does this line occur anywhere' checks."""
    return ' '.join(squash(u['text']) for b in load_batches(work).values() for u in b['units'])
