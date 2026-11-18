"""The whole flow through the single entry point, on invented conversations.

start -> topics -> batch -> cards -> export -> verify, plus the rules that keep the run
cheap and the folders clean: only chosen topics are read, and a card's owner must be one
of the chosen topic names.
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
KB = ROOT / 'scripts' / 'kb.py'


def conversation(number, name, ask, answer):
    return {'uuid': f'conv{number}', 'name': name, 'chat_messages': [
        {'uuid': f'm{number}a', 'sender': 'human', 'created_at': f'2025-0{number}-01T10:00:00Z', 'text': ask},
        {'uuid': f'm{number}b', 'sender': 'assistant', 'created_at': f'2025-0{number}-01T10:01:00Z', 'text': answer},
    ]}


CHATS = [
    conversation(1, '提醒丢失', '服务重启之后提醒不发了', '先看日志：\n\n```bash\njournalctl -u yali -n 100 --no-pager\n```\n\n提醒只存在内存里，启动时要读回。'),
    conversation(2, '光圈', '光圈和快门怎么配合', '先开大光圈，再放慢快门。' * 20),
    conversation(3, '同步冲突', '两边都改了同一条记录以谁为准', '比较修改时间，晚的覆盖早的。' * 20),
]
TOPICS = {'topics': [
    {'name': '芽历', 'category': '01_项目', 'about': '种植记录应用', 'sessions': ['c001', 'c003'], 'keep': True},
    {'name': '摄影入门', 'category': '02_专题知识', 'about': '曝光问答', 'sessions': ['c002'], 'keep': False},
]}


class KbFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.source, self.work, self.vault = base / 'exports', base / '.work', base / 'vault'
        self.source.mkdir()
        (self.source / 'conversations.json').write_text(json.dumps(CHATS, ensure_ascii=False))

    def tearDown(self):
        self.temp.cleanup()

    def kb(self, *args):
        return subprocess.run([sys.executable, str(KB), *map(str, args)], capture_output=True, text=True)

    def start_and_choose(self):
        self.assertEqual(self.kb('start', self.source, self.work).returncode, 0)
        (self.work / 'topics.json').write_text(json.dumps(TOPICS, ensure_ascii=False))
        self.assertEqual(self.kb('topics', self.work).returncode, 0)
        self.assertEqual(self.kb('batch', self.work).returncode, 0)

    def cards(self, owner='芽历'):
        batch = json.loads((self.work / 'batches' / 's1-b0001.json').read_text())
        refs = [u['ref'] for u in batch['units']]
        return {'batch_id': 's1-b0001', 'cards': [
            {'id': '1', 'owner': owner, 'kind': 'issue', 'title': '重启后提醒不发', 'units': refs[:2],
             'content': '提醒只存在内存里，启动时读回。', 'verbatim': ['journalctl -u yali -n 100 --no-pager']},
            {'id': '2', 'owner': '芽历', 'kind': 'decision', 'title': '冲突时后改的覆盖先改的', 'units': refs[2:],
             'content': '比较修改时间，晚的覆盖早的。'},
        ], 'skipped': []}

    def write_cards(self, data):
        (self.work / 'cards').mkdir(exist_ok=True)
        (self.work / 'cards' / 's1-b0001.json').write_text(json.dumps(data, ensure_ascii=False))

    def test_start_builds_a_catalog_of_questions(self):
        self.assertEqual(self.kb('start', self.source, self.work).returncode, 0)
        catalog = (self.work / 'catalog.md').read_text()
        self.assertIn('## c001｜提醒丢失', catalog)
        self.assertIn('- 光圈和快门怎么配合', catalog)

    def test_only_chosen_topics_are_read_and_owners_are_fixed(self):
        self.start_and_choose()
        files = sorted(p.name for p in (self.work / 'batches').glob('*.md'))
        self.assertEqual(files, ['s1-b0001.md'])
        text = (self.work / 'batches' / files[0]).read_text()
        self.assertIn('服务重启之后提醒不发了', text)
        self.assertIn('两边都改了同一条记录', text)
        self.assertNotIn('光圈和快门', text)
        self.assertIn('只能填这些名字之一：芽历、其他', text)
        table = (self.work / '话题目录.md').read_text()
        self.assertIn('| 1 | 芽历 | 01_项目 | 种植记录应用 | 2 |', table)

    def test_invented_owner_names_are_rejected(self):
        self.start_and_choose()
        self.write_cards(self.cards(owner='芽历项目'))
        result = self.kb('check', self.work)
        self.assertEqual(result.returncode, 1)
        self.assertIn('OWNER_NOT_A_TOPIC', result.stdout)
        self.write_cards(self.cards(owner='其他'))
        self.assertEqual(self.kb('check', self.work).returncode, 0)

    def test_full_run_passes_verification(self):
        self.start_and_choose()
        self.write_cards(self.cards())
        self.assertEqual(self.kb('check', self.work).returncode, 0)
        self.assertEqual(self.kb('export', self.work).returncode, 0)
        self.assertTrue((self.work / 'owners' / '芽历' / 'issue.md').is_file())
        pages = {
            '00_知识库入口.md': '# 知识库入口\n\n> 从这里进入。\n\n- [[01_项目/芽历/00_项目总览]]\n',
            '01_项目/芽历/00_项目总览.md': '# 芽历 项目总览\n\n> 种植记录应用。\n\n- [[01_问题与决策]]\n',
            '01_项目/芽历/01_问题与决策.md': '# 芽历 问题与决策\n\n> 故障和取舍。\n\n## 重启后提醒不发\n\n提醒只存在内存里。\n\n'
                '```bash\njournalctl -u yali -n 100 --no-pager\n```\n\n## 同步冲突\n\n晚的覆盖早的。\n',
        }
        for rel, text in pages.items():
            path = self.vault / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        (self.work / 'placement.jsonl').write_text(json.dumps(
            {'page': '01_项目/芽历/01_问题与决策.md', 'cards': ['s1-b0001#1', 's1-b0001#2']}, ensure_ascii=False) + '\n')
        result = self.kb('verify', self.work, self.vault)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.work / 'inventory_last.json').is_file())
        report = json.loads((self.work / 'vault_check.json').read_text())
        self.assertEqual(report['owners']['芽历']['cards'], 2)

    def test_export_refuses_until_reading_is_done(self):
        self.start_and_choose()
        self.assertNotEqual(self.kb('export', self.work).returncode, 0)

    def test_choosing_more_topics_later_adds_only_the_new_ones(self):
        self.start_and_choose()
        chosen = json.loads(json.dumps(TOPICS))
        chosen['topics'][1]['keep'] = True
        (self.work / 'topics.json').write_text(json.dumps(chosen, ensure_ascii=False))
        self.assertEqual(self.kb('batch', self.work).returncode, 0)
        files = sorted(p.name for p in (self.work / 'batches').glob('*.md'))
        self.assertEqual(files, ['s1-b0001.md', 's2-b0001.md'])
        later = (self.work / 'batches' / 's2-b0001.md').read_text()
        self.assertIn('光圈和快门', later)
        self.assertNotIn('服务重启之后提醒不发了', later)

    def test_new_export_keeps_old_conversation_numbers(self):
        self.assertEqual(self.kb('start', self.source, self.work).returncode, 0)
        newer = [conversation(0, '更早的一段', '新导出里才出现的更早对话', '回答。')] + CHATS
        (self.source / 'conversations.json').write_text(json.dumps(newer, ensure_ascii=False))
        self.assertEqual(self.kb('start', self.source, self.work).returncode, 0)
        catalog = (self.work / 'catalog.md').read_text()
        self.assertIn('## c001｜提醒丢失', catalog)
        self.assertIn('## c004｜更早的一段', catalog)
        self.assertIn('｜新增', catalog)


if __name__ == '__main__':
    unittest.main()
