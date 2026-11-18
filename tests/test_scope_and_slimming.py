"""Checks for what is left out before reading, and for choosing scope by topic.

All fixtures are invented.
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
from text_support import render_content_blocks, tool_block_role
from make_batches import drop_repeats, trim_long_code, plan, CODE_LIMIT

BLOCKS = [
    {'type': 'thinking', 'thinking': '先想想用户要什么，这段不该被读到'},
    {'type': 'text', 'text': '提醒只存在内存里，所以重启后丢了。'},
    {'type': 'tool_use', 'name': 'view', 'input': {'path': '/srv/app/reminders.py', 'description': '查看文件'}},
    {'type': 'tool_result', 'name': 'view', 'content': [{'type': 'text', 'text': 'def load():\n    pass\n' * 50}]},
    {'type': 'tool_use', 'name': 'create_file', 'input': {'path': '/out/交接说明.md', 'file_text': '# 交接说明\n\n启动时要读回提醒。'}},
    {'type': 'tool_use', 'name': 'create_file', 'input': {'path': '/out/page.html', 'file_text': '<html><body>很长的页面</body></html>'}},
    {'type': 'tool_use', 'name': 'ask_user_input_v0', 'input': {'questions': [{'question': '提醒用邮件还是通知？', 'options': ['邮件', '通知']}]}},
    {'type': 'tool_result', 'name': 'ask_user_input_v0', 'content': [{'type': 'text', 'text': '用户选择：邮件'}]},
]


class DialogueOnlyTests(unittest.TestCase):
    def render(self, conversation_only):
        left_out = {}
        fragments, _ = render_content_blocks(BLOCKS, conversation_only, left_out)
        return '\n'.join(text for _, text in fragments), left_out

    def test_process_is_left_out_and_counted(self):
        text, left_out = self.render(True)
        self.assertIn('提醒只存在内存里', text)
        self.assertNotIn('这段不该被读到', text)
        self.assertNotIn('def load()', text)
        self.assertNotIn('很长的页面', text)
        self.assertEqual(set(left_out), {'thinking', 'tool_use', 'tool_result'})

    def test_answers_hidden_in_tool_calls_are_kept(self):
        text, _ = self.render(True)
        self.assertIn('启动时要读回提醒', text)       # a document written for the person
        self.assertIn('提醒用邮件还是通知', text)     # a question put to the person
        self.assertIn('用户选择：邮件', text)         # and their answer

    def test_everything_can_still_be_kept(self):
        text, left_out = self.render(False)
        self.assertIn('def load()', text)
        self.assertEqual(left_out, {})

    def test_tool_roles(self):
        self.assertEqual(tool_block_role({'name': 'bash_tool', 'input': {'command': 'ls'}})[0], 'process')
        self.assertEqual(tool_block_role({'name': 'artifacts', 'input': {'type': 'text/markdown', 'title': '方案', 'content': '正文'}})[0], 'document')
        self.assertEqual(tool_block_role({'name': 'artifacts', 'input': {'type': 'application/vnd.react', 'title': 'App', 'content': 'export default'}})[0], 'process')
        self.assertEqual(tool_block_role({'name': 'Task', 'input': {'prompt': '子任务'}})[0], 'process')

    def test_prepare_defaults_to_dialogue_and_reports_what_it_left_out(self):
        chat = [{'uuid': 'c1', 'name': '排查', 'chat_messages': [
            {'uuid': 'm1', 'sender': 'human', 'text': '重启后提醒不发了'},
            {'uuid': 'm2', 'sender': 'assistant', 'text': '', 'content': BLOCKS[:4]},
            {'uuid': 'm3', 'sender': 'assistant', 'text': '', 'content': BLOCKS[2:4]},
        ]}]
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / 'src').mkdir()
            (base / 'src' / 'conversations.json').write_text(json.dumps(chat, ensure_ascii=False))
            manifest, units = prepare(base / 'src', base / 'slim', 'synthetic')
            self.assertEqual(len(units), 2)  # the message that was only tool traffic leaves no unit
            self.assertTrue(manifest['sources'][0]['left_out_chars']['tool_result'] > 0)
            _, full = prepare(base / 'src', base / 'full', 'synthetic', conversation_only=False)
            self.assertEqual(len(full), 3)
            self.assertTrue(any('def load()' in u['text'] for u in full))

    def test_mapping_export_drops_tool_and_hidden_nodes(self):
        def node(key, parent, role, text, **extra):
            message = {'id': key, 'author': {'role': role}, 'content': {'content_type': extra.pop('kind', 'text'), 'parts': [text]}, **extra}
            return {'id': key, 'parent': parent, 'message': message}
        chat = [{'id': 'conv', 'title': '合成', 'current_node': 'e', 'mapping': {
            'a': node('a', None, 'system', '隐藏的系统设定'),
            'b': node('b', 'a', 'user', '怎么备份数据库'),
            'c': node('c', 'b', 'assistant', 'search("sqlite backup")', recipient='browser'),
            'd': node('d', 'c', 'tool', '搜索结果一大段'),
            'e': node('e', 'd', 'assistant', '用 .backup 命令。'),
        }}]
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / 'src').mkdir()
            (base / 'src' / 'conversations.json').write_text(json.dumps(chat, ensure_ascii=False))
            manifest, units = prepare(base / 'src', base / 'out', 'synthetic')
            joined = '\n'.join(u['text'] for u in units)
            self.assertIn('怎么备份数据库', joined)
            self.assertIn('用 .backup 命令', joined)
            for hidden in ('隐藏的系统设定', 'sqlite backup', '搜索结果一大段'):
                self.assertNotIn(hidden, joined)
            self.assertTrue(manifest['sources'][0]['left_out_chars']['tool_or_hidden_message'] > 0)


class SlimmingTests(unittest.TestCase):
    def test_repeat_is_replaced_and_first_copy_kept(self):
        doc = '需求文档第一节。' * 60
        seen = set()
        first, cut1 = drop_repeats({'text': '[message 0 | human]\n看这份文档\n\n' + doc}, seen)
        second, cut2 = drop_repeats({'text': '[message 4 | human]\n还是这份，改一下第二节\n\n' + doc}, seen)
        self.assertEqual(cut1, 0)
        self.assertIn(doc, first['text'])
        self.assertEqual(cut2, len(doc))
        self.assertIn('改一下第二节', second['text'])
        self.assertIn('已省略', second['text'])
        self.assertNotIn(doc, second['text'])

    def test_unit_that_is_only_a_repeat_disappears(self):
        doc = '同一份粘贴内容。' * 60
        seen = set()
        drop_repeats({'text': '[message 0 | human]\n' + doc}, seen)
        unit, cut = drop_repeats({'text': '[message 2 | human]\n' + doc}, seen)
        self.assertIsNone(unit)
        self.assertEqual(cut, len(doc))

    def test_short_blocks_are_never_deduplicated(self):
        seen = set()
        for _ in range(2):
            unit, cut = drop_repeats({'text': '[message 0 | human]\n好的，就用第二个方案。'}, seen)
            self.assertEqual(cut, 0)

    def test_long_code_keeps_head_and_tail(self):
        lines = [f'<div id="row{n}">内容</div>' for n in range(600)]
        text = '[message 0 | user]\n帮我看看这个页面哪里错了\n\n```html\n' + '\n'.join(lines) + '\n```\n\n谢谢'
        unit, cut = trim_long_code({'text': text})
        self.assertGreater(cut, 5000)
        self.assertLess(len(unit['text']), CODE_LIMIT)
        for kept in ('帮我看看这个页面哪里错了', 'row0"', 'row599"', '已省略', '谢谢'):
            self.assertIn(kept, unit['text'])
        self.assertNotIn('row300"', unit['text'])

    def test_short_code_is_untouched(self):
        text = '[message 0 | assistant]\n先看日志：\n\n```bash\njournalctl -u yali -n 100 --no-pager\n```\n' + '解释。' * 2000
        unit, cut = trim_long_code({'text': text})
        self.assertEqual(cut, 0)
        self.assertEqual(unit['text'], text)

    def test_repeats_only_compared_within_what_is_batched(self):
        doc = '只在选中的范围里比较。' * 50
        manifest = {'sources': [{'source_id': s, 'status': 'ready', 'started_at': day} for s, day in (('a', '2025-01-01'), ('b', '2025-01-02'))]}
        units = [{'unit_id': s, 'source_id': s, 'role': 'human', 'text': '[message 0 | human]\n' + doc} for s in ('a', 'b')]
        only_b = plan(manifest, units, 30000, keep=lambda s: s['source_id'] == 'b', seen=set())
        self.assertIn(doc, only_b[0][0][1][0]['text'])   # 'a' is out of scope, so 'b' keeps the full copy
        both = plan(manifest, units, 30000, seen=set())
        self.assertEqual([s['source_id'] for s, *_ in both[0]], ['a'])


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        (base / 'src').mkdir()
        chats = [{'uuid': f'conv{n}', 'name': name, 'chat_messages': [
            {'uuid': f'm{n}a', 'sender': 'human', 'created_at': f'2025-0{n}-01T10:00:00Z', 'text': ask},
            {'uuid': f'm{n}b', 'sender': 'assistant', 'created_at': f'2025-0{n}-01T10:01:00Z', 'text': f'第{n}个回答。' * 150},
        ]} for n, (name, ask) in enumerate([('提醒丢失', '重启后提醒不发了'), ('光圈', '光圈和快门怎么配合'), ('同步冲突', '两边都改了以谁为准')], 1)]
        (base / 'src' / 'conversations.json').write_text(json.dumps(chats, ensure_ascii=False))
        self.snap, self.work = base / 'snap', base / 'work'
        prepare(base / 'src', self.snap, 'synthetic')

    def tearDown(self):
        self.temp.cleanup()

    def run_script(self, name, *args):
        return subprocess.run([sys.executable, str(SCRIPTS / name), *map(str, args)], capture_output=True, text=True)

    def scope(self, topics):
        for topic in topics:
            topic.setdefault('category', '01_项目')
        (self.work / 'topics.json').write_text(json.dumps({'topics': topics}, ensure_ascii=False))

    def test_catalog_shows_questions_not_answers(self):
        self.assertEqual(self.run_script('catalog.py', 'build', self.snap, self.work).returncode, 0)
        catalog = (self.work / 'catalog.md').read_text()
        self.assertIn('## c001｜提醒丢失', catalog)
        self.assertIn('- 重启后提醒不发了', catalog)
        self.assertNotIn('个回答。第', catalog)

    def test_every_conversation_needs_exactly_one_topic(self):
        self.run_script('catalog.py', 'build', self.snap, self.work)
        self.scope([{'name': '芽历', 'about': '种植应用', 'sessions': ['c001']}])
        result = self.run_script('catalog.py', 'topics', self.work)
        self.assertEqual(result.returncode, 1)
        self.assertIn('SESSIONS_WITHOUT_TOPIC', result.stdout)
        self.scope([{'name': '芽历', 'about': '种植应用', 'sessions': ['c001-c003']},
                    {'name': '摄影', 'about': '曝光问答', 'sessions': ['c002']}])
        self.assertIn('SESSION_IN_TWO_TOPICS', self.run_script('catalog.py', 'topics', self.work).stdout)

    def test_only_chosen_topics_are_batched_and_the_rest_can_follow(self):
        self.run_script('catalog.py', 'build', self.snap, self.work)
        self.scope([{'name': '芽历', 'about': '种植应用', 'sessions': ['c001', 'c003'], 'keep': True},
                    {'name': '摄影', 'about': '曝光问答', 'sessions': ['c002'], 'keep': False}])
        self.assertEqual(self.run_script('catalog.py', 'topics', self.work).returncode, 0)
        self.assertIn('| 2 | 摄影 | 01_项目 | 曝光问答 | 1 |', (self.work / '话题目录.md').read_text())
        self.run_script('make_batches.py', self.snap, self.work, '--scope', self.work / 'topics.json')
        text = (self.work / 'batches' / 's1-b0001.md').read_text()
        self.assertIn('重启后提醒不发了', text)
        self.assertIn('两边都改了以谁为准', text)
        self.assertNotIn('光圈和快门', text)
        later = self.run_script('make_batches.py', self.snap, self.work, '--label', 's2', '--skip-batched')
        self.assertEqual(json.loads(later.stdout)['batches'], 1)
        self.assertIn('光圈和快门', (self.work / 'batches' / 's2-b0001.md').read_text())

    def test_replace_pending_keeps_what_was_already_read(self):
        self.run_script('make_batches.py', self.snap, self.work, '--batch-chars', '1000')
        ids = sorted(p.stem for p in (self.work / 'batches').glob('*.json'))
        self.assertEqual(len(ids), 3)
        (self.work / 'cards').mkdir()
        (self.work / 'cards' / f'{ids[0]}.json').write_text('{}')
        result = json.loads(self.run_script('make_batches.py', self.snap, self.work, '--label', 's2', '--replace-pending').stdout)
        self.assertEqual(result['pending_batches_replaced'], 2)
        left = sorted(p.stem for p in (self.work / 'batches').glob('*.json'))
        self.assertEqual(left, [ids[0], 's2-b0001'])
        self.assertNotIn('重启后提醒不发了', (self.work / 'batches' / 's2-b0001.md').read_text())


if __name__ == '__main__':
    unittest.main()
