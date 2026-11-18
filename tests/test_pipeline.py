"""End-to-end checks on synthetic data: prepare -> batch -> cards -> export -> vault check.

Every fixture here is invented. The tests check that the gates catch what they claim to
catch; they do not grade wording.
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'scripts'
sys.path.insert(0, str(SCRIPTS))
from prepare_sources import prepare
from text_support import parse_activity_html, parse_activity_timestamp, group_activity_sessions
from make_batches import plan, render, pieces
from workspace import occurs, squash, expand_refs, check_batch, load_cards
import check_vault

ACTIVITY = '''<html><body><div class="mdl-grid">
<div class="outer-cell mdl-cell"><div class="mdl-grid"><div class="header-cell"><p class="mdl-typography--title">Assistant Apps<br></p></div>
<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1">Prompted 光圈和快门怎么配合<br>Attached 1 file.<br>-  <a href="leaf.png">leaf.png</a><br>3 Mar 2025, 21:05:10 CST<br><p>先开大<strong>光圈</strong>。</p><p>再放慢快门。</p></div>
<div class="content-cell mdl-cell mdl-cell--12-col mdl-typography--caption"><b>Products:</b><br>Assistant</div></div></div>
<div class="outer-cell mdl-cell"><div class="mdl-grid"><div class="header-cell"><p class="mdl-typography--title">Assistant Apps<br></p></div>
<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1">Prompted 什么是景深<br>3 Mar 2025, 20:50:00 CST<br><p>画面里看起来清晰的前后范围。</p></div>
<div class="content-cell mdl-cell mdl-cell--12-col mdl-typography--caption"><b>Products:</b><br>Assistant</div></div></div>
<div class="outer-cell mdl-cell"><div class="mdl-grid"><div class="header-cell"><p class="mdl-typography--title">Assistant Apps<br></p></div>
<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1">Prompted 番茄多久浇一次水<br>1 Mar 2025, 09:00:00 CST<br><p>看土壤干湿。</p></div>
<div class="content-cell mdl-cell mdl-cell--12-col mdl-typography--caption"><b>Products:</b><br>Assistant</div></div></div>
</div></body></html>'''

CHAT = [{'uuid': 'conv-reminder', 'name': '提醒丢失排查', 'chat_messages': [
    {'uuid': 'm1', 'sender': 'human', 'created_at': '2025-03-10T10:00:00Z',
     'text': '服务重启之后提醒不发了，帮我看看。'},
    {'uuid': 'm2', 'sender': 'assistant', 'created_at': '2025-03-10T10:01:00Z',
     'text': '先看日志：\n\n```bash\njournalctl -u yali -n 100 --no-pager\n```\n\n如果任务在跑但列表为空，说明提醒只存在内存里。'},
    {'uuid': 'm3', 'sender': 'human', 'created_at': '2025-03-10T10:20:00Z',
     'text': '确实是空的。启动时读回数据库之后好了，重启也能收到。谢谢'},
]}]


class ActivityLogTests(unittest.TestCase):
    def test_timestamp_formats(self):
        self.assertEqual(parse_activity_timestamp('1 Jul 2026, 14:39:29 KST'), '2026-07-01T14:39:29')
        self.assertEqual(parse_activity_timestamp('Jul 1, 2026, 2:39:29 PM PDT'), '2026-07-01T14:39:29')
        self.assertEqual(parse_activity_timestamp('2026年7月1日 下午2:39:29'), '2026-07-01T14:39:29')
        self.assertEqual(parse_activity_timestamp('2026-07-01' + ' 14:39:29'), '2026-07-01T14:39:29')
        self.assertIsNone(parse_activity_timestamp('什么是景深'))

    def test_records_split_prompt_response_and_attachment(self):
        records = parse_activity_html(ACTIVITY)
        self.assertEqual(len(records), 3)
        first = records[0]
        self.assertEqual(first['prompt'], '光圈和快门怎么配合')
        self.assertEqual(first['attachments'], ['leaf.png'])
        self.assertIn('先开大光圈', first['response'])
        self.assertEqual(parse_activity_html('<html><body><p>plain page</p></body></html>'), [])

    def test_sessions_grouped_by_gap_and_ordered(self):
        sessions = group_activity_sessions(parse_activity_html(ACTIVITY), 45)
        self.assertEqual([len(s) for s in sessions], [1, 2])
        self.assertEqual(sessions[1][0]['prompt'], '什么是景深')

    def test_prepare_turns_activity_log_into_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / 'src').mkdir()
            (base / 'src' / 'My Activity.html').write_text(ACTIVITY)
            manifest, units = prepare(base / 'src', base / 'out', 'synthetic')
            ready = [s for s in manifest['sources'] if s['status'] == 'ready']
            self.assertEqual(len(ready), 2)
            self.assertTrue(all('activity_sessions_inferred_by_time_gap' in s['warnings'] for s in ready))
            self.assertEqual({u['role'] for u in units}, {'user', 'assistant'})
            self.assertTrue(any(a.get('file_name') == 'leaf.png' for a in manifest['attachments']))
            again, _ = prepare(base / 'src', base / 'two', 'synthetic', base / 'out' / 'source_manifest.json')
            self.assertTrue(all(s['change'] == 'unchanged' for s in again['sources'] if s['status'] == 'ready'))


class BatchTests(unittest.TestCase):
    def test_oversize_unit_is_cut_and_nothing_is_lost(self):
        unit = {'unit_id': 'x', 'source_id': 's', 'text': ''.join(f'line {n}\n' for n in range(900))}
        parts = pieces(unit, 1000)
        self.assertTrue(all(len(p['text']) <= 1000 for p in parts))
        self.assertEqual(''.join(p['text'] for p in parts), unit['text'])

    def test_long_conversation_spans_consecutive_batches(self):
        manifest = {'sources': [{'source_id': 's', 'status': 'ready', 'title': 't', 'started_at': '2025-01-01'}]}
        units = [{'unit_id': f'u{n}', 'source_id': 's', 'role': 'user', 'text': 'x' * 700} for n in range(5)]
        batches = plan(manifest, units, 1500)
        self.assertEqual(len(batches), 3)
        self.assertEqual([b[0][2:] for b in batches], [(1, 3), (2, 3), (3, 3)])
        text, refs = render('s1-b0001', batches[0])
        self.assertIn('第 1/3 段', text)
        self.assertEqual([r['ref'] for r in refs], ['u1', 'u2'])


class MatchingTests(unittest.TestCase):
    def test_whitespace_ignored_but_characters_matter(self):
        source = squash('先看日志：\n  journalctl -u yali -n 100   --no-pager')
        self.assertTrue(occurs(squash('journalctl -u yali -n 100 --no-pager'), source))
        self.assertFalse(occurs(squash('journalctl -u yali -n 200 --no-pager'), source))

    def test_placeholder_matches_redacted_secret(self):
        source = squash('curl -H "Authorization: Bearer abc123secret" https://api.example.com/v1/items')
        self.assertTrue(occurs(squash('curl -H "Authorization: Bearer <TOKEN>" https://api.example.com/v1/items'), source))
        self.assertFalse(occurs(squash('wget <URL>'), source))

    def test_ranges(self):
        known = {f'u{n}' for n in range(1, 6)}
        self.assertEqual(expand_refs(['u1', 'u3-u5'], known), (['u1', 'u3', 'u4', 'u5'], []))
        self.assertEqual(expand_refs(['u4-u9'], known)[1], ['u6', 'u7', 'u8', 'u9'])
        self.assertEqual(expand_refs(['7'], known)[1], ['7'])


class PipelineTests(unittest.TestCase):
    """One synthetic conversation carried through every stage."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        (base / 'src').mkdir()
        (base / 'src' / 'conversations.json').write_text(json.dumps(CHAT, ensure_ascii=False))
        self.work, self.vault = base / 'work', base / 'vault'
        prepare(base / 'src', self.work / 'snapshots' / 's1', 'synthetic')
        self.run_script('make_batches.py', self.work / 'snapshots' / 's1', self.work)
        (self.work / 'cards').mkdir()
        self.cards = {'batch_id': 's1-b0001', 'cards': [
            {'id': '1', 'owner': '芽历', 'kind': 'issue', 'title': '重启后提醒不再触发',
             'content': '提醒只存在内存里，启动时没有从数据库读回。读回后重启也能收到。',
             'units': ['u1-u3'], 'state': '已验证', 'when': '2025-03'},
            {'id': '2', 'owner': '种植应用', 'kind': 'command', 'title': '查看服务日志',
             'content': '看最近 100 行日志。', 'verbatim': ['journalctl -u yali -n 100 --no-pager'], 'units': ['u2']},
        ], 'skipped': []}
        self.owner_map = {'owners': [{'name': '芽历', 'folder': '01_项目/芽历', 'labels': ['种植应用'], 'keep': True}]}
        self.pages = {
            '00_知识库入口.md': '# 知识库入口\n\n> 从这里进入。\n\n- [[01_项目/芽历/00_项目总览]]\n',
            '01_项目/芽历/00_项目总览.md': '# 芽历 项目总览\n\n> 种植记录应用。\n\n- [[01_问题与命令]]\n',
            '01_项目/芽历/01_问题与命令.md': '# 芽历 问题与命令\n\n> 故障和命令。\n\n## 重启后提醒不再触发\n\n'
                '提醒只存在内存里。\n\n```bash\njournalctl -u yali -n 100 --no-pager\n```\n',
        }
        self.placement = [{'page': '01_项目/芽历/01_问题与命令.md', 'cards': ['s1-b0001#1', 's1-b0001#2']}]

    def tearDown(self):
        self.temp.cleanup()

    def run_script(self, name, *args):
        return subprocess.run([sys.executable, str(SCRIPTS / name), *map(str, args)], capture_output=True, text=True)

    def write(self):
        (self.work / 'cards' / 's1-b0001.json').write_text(json.dumps(self.cards, ensure_ascii=False))
        (self.work / 'owner_map.json').write_text(json.dumps(self.owner_map, ensure_ascii=False))
        (self.work / 'placement.jsonl').write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in self.placement))
        for rel, text in self.pages.items():
            path = self.vault / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def card_codes(self):
        self.write()
        return {e['code'] for e in load_cards(self.work)[1]}

    def vault_result(self):
        self.write()
        thresholds = {'quick_locate_chars': 3000, 'assets': 5, 'terms': 8, 'issues': 5,
                      'full_owner_cards': 30, 'chars_per_card': 1}
        return check_vault.check(self.work, self.vault, thresholds)

    def vault_codes(self):
        return {e['code'] for e in self.vault_result()['errors']}

    # --- reading gate ---
    def test_unread_batch_blocks_writing(self):
        self.assertEqual(self.run_script('cards.py', 'check', self.work).returncode, 1)
        status = json.loads(self.run_script('cards.py', 'status', self.work).stdout)
        self.assertFalse(status['ready_to_write'])
        self.assertNotEqual(self.run_script('cards.py', 'export', self.work).returncode, 0)

    def test_valid_cards_pass_and_export(self):
        self.write()
        self.assertEqual(self.run_script('cards.py', 'check', self.work).returncode, 0)
        self.assertEqual(self.run_script('cards.py', 'export', self.work).returncode, 0)
        exported = (self.work / 'owners' / '芽历' / 'command.md').read_text()
        self.assertIn('[s1-b0001#2]', exported)
        self.assertIn('journalctl -u yali -n 100 --no-pager', exported)

    def test_every_unit_must_be_accounted_for(self):
        self.cards['cards'][0]['units'] = ['u1']
        self.assertIn('UNITS_NOT_ACCOUNTED', self.card_codes())

    def test_skipping_needs_a_reason(self):
        self.cards['cards'][0]['units'] = ['u1']
        self.cards['skipped'] = [{'units': ['u3'], 'reason': ''}]
        self.assertIn('SKIP_WITHOUT_REASON', self.card_codes())

    def test_invented_verbatim_is_caught(self):
        self.cards['cards'][1]['verbatim'] = ['journalctl -u yali --since today']
        self.assertIn('VERBATIM_NOT_IN_SOURCE', self.card_codes())

    def test_command_card_needs_source_text(self):
        self.cards['cards'][1]['verbatim'] = []
        self.assertIn('VERBATIM_REQUIRED', self.card_codes())

    def test_unmapped_label_blocks_export(self):
        self.owner_map['owners'][0]['labels'] = []
        self.write()
        self.assertNotEqual(self.run_script('cards.py', 'export', self.work).returncode, 0)

    # --- vault gate ---
    def test_complete_vault_passes(self):
        result = self.vault_result()
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['owners']['芽历']['cards'], 2)
        self.assertEqual(self.run_script('check_vault.py', self.work, self.vault).returncode, 0)

    def test_card_left_out_is_caught(self):
        self.placement[0]['cards'] = ['s1-b0001#2']
        self.assertIn('CARDS_NOT_PLACED', self.vault_codes())

    def test_dropping_needs_a_reason(self):
        self.placement[0]['cards'] = ['s1-b0001#2']
        self.placement.append({'dropped': ['s1-b0001#1'], 'reason': ''})
        self.assertIn('PLACEMENT_REASON_MISSING', self.vault_codes())

    def test_command_summarised_away_is_caught(self):
        page = '01_项目/芽历/01_问题与命令.md'
        self.pages[page] = self.pages[page].replace('```bash\njournalctl -u yali -n 100 --no-pager\n```', '用 journalctl 看日志。')
        self.assertIn('VERBATIM_NOT_IN_PAGE', self.vault_codes())

    def test_invented_code_block_needs_a_template_label(self):
        page = '01_项目/芽历/01_问题与命令.md'
        self.pages[page] += '\n## 清理缓存\n\n```bash\nrm -rf /var/cache/yali/thumbnails\n```\n'
        self.assertIn('UNMARKED_NONSOURCE_BLOCK', self.vault_codes())
        self.pages[page] = self.pages[page].replace('## 清理缓存', '## 可复用的指令骨架')
        self.assertNotIn('UNMARKED_NONSOURCE_BLOCK', self.vault_codes())

    def test_structure_problems_are_caught(self):
        self.pages['01_项目/芽历/03_孤立页.md'] = '# 芽历 孤立页\n\n> 没有人链到这里。\n'
        self.pages['01_项目/芽历/00_项目总览.md'] += '\n见 [[不存在的页面]] 和编号 s1-b0001#1。\n'
        codes = self.vault_codes()
        for code in ('UNREACHABLE_FROM_ENTRY', 'NUMBERING_NOT_CONTIGUOUS_FROM_00', 'BROKEN_LINK', 'BOOKKEEPING_IN_READER_TEXT'):
            self.assertIn(code, codes)

    def test_readability_warnings(self):
        self.pages['01_项目/芽历/00_项目总览.md'] = '# 芽历 项目总览\n\n## 直接开始\n\n' + '很长的一段。' * 80 + '\n\n- [[01_问题与命令]]\n'
        codes = {w['code'] for w in self.vault_result()['warnings']}
        self.assertIn('NO_LEAD_LINE', codes)
        self.assertIn('LONG_PARAGRAPH', codes)


class SampleVaultTests(unittest.TestCase):
    """The bundled sample must itself follow the layout rules it illustrates."""

    def setUp(self):
        root = ROOT / 'assets' / 'sample_vault'
        self.files = {p.relative_to(root).as_posix(): p.read_text() for p in root.rglob('*.md')}

    def test_links_resolve(self):
        errors, graph = check_vault.check_links(self.files)
        self.assertEqual(errors, [])
        self.assertTrue(graph['00_知识库入口.md'])

    def test_pages_follow_the_skeleton(self):
        for path, text in self.files.items():
            m = check_vault.page_metrics(text)
            self.assertEqual(m['h1'], 1, path)
            self.assertTrue(m['lead_ok'], path)
            self.assertEqual(m['long_paragraphs'], 0, path)
            self.assertEqual(m['deep_headings'], 0, path)
            self.assertFalse(check_vault.LEAK.search(text), path)


if __name__ == '__main__':
    unittest.main()
