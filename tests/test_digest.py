"""The fast flow on invented conversations: start -> topics -> digest -> write -> verify.

What matters: the model is handed a small digest instead of everything; exact commands
come out word for word; the passages that carry a cause or a decision are the ones
picked; any unit can still be opened in full; and oversized notes are flagged.
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
KB = ROOT / 'scripts' / 'kb.py'
sys.path.insert(0, str(ROOT / 'scripts'))
import digest  # noqa: E402

FILLER = '这一段只是在铺陈背景，没有什么特别需要记下来的内容，继续往下说。'


def conversation(number, name, turns):
    messages = []
    for index, (ask, answer) in enumerate(turns):
        messages += [
            {'uuid': f'm{number}-{index}a', 'sender': 'human', 'created_at': f'2025-0{number}-01T10:{index:02d}:00Z', 'text': ask},
            {'uuid': f'm{number}-{index}b', 'sender': 'assistant', 'created_at': f'2025-0{number}-01T10:{index:02d}:30Z', 'text': answer},
        ]
    return {'uuid': f'conv{number}', 'name': name, 'chat_messages': messages}


CHATS = [
    conversation(1, '提醒丢失', [
        ('服务重启之后提醒不发了', '先看日志：\n\n```bash\njournalctl -u yali -n 100 --no-pager\n```\n\n看完告诉我。'),
        ('随便聊聊天气', (FILLER + '\n\n') * 30),
        ('再说说别的', (FILLER + '\n\n') * 30),
        ('还是不行，日志里任务在跑', '根因是提醒只存在内存里，启动时没有读回。\n\n解决方法：启动时从数据库读回未完成的提醒。\n\n'
                                   + (FILLER + '\n\n') * 20 + '经验：重启后丢状态，先查它存在哪。'),
    ]),
    conversation(2, '光圈', [('光圈和快门怎么配合', '先开大光圈，再放慢快门。' * 20)]),
]
TOPICS = {'topics': [
    {'name': '芽历', 'category': '01_项目', 'about': '种植记录应用', 'sessions': ['c001'], 'keep': True},
    {'name': '摄影入门', 'category': '02_专题知识', 'about': '曝光问答', 'sessions': ['c002'], 'keep': False},
]}


class DigestFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.source, self.work, self.vault = base / 'exports', base / '.work', base / 'vault'
        self.source.mkdir()
        (self.source / 'conversations.json').write_text(json.dumps(CHATS, ensure_ascii=False))
        self.assertEqual(self.kb('start', self.source, self.work).returncode, 0)
        (self.work / 'topics.json').write_text(json.dumps(TOPICS, ensure_ascii=False))

    def tearDown(self):
        self.temp.cleanup()

    def kb(self, *args):
        return subprocess.run([sys.executable, str(KB), *map(str, args)], capture_output=True, text=True)

    def digest(self, *args):
        result = self.kb('digest', self.work, *args)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return self.work / 'digest' / '芽历'

    def write_vault(self, pages):
        for rel, text in pages.items():
            path = self.vault / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def test_only_chosen_topics_get_a_digest(self):
        folder = self.digest()
        self.assertTrue((folder / '精选.md').is_file())
        self.assertFalse((self.work / 'digest' / '摄影入门').exists())
        self.assertIn('芽历', (self.work / 'digest' / '_index.md').read_text())

    def test_signal_is_picked_and_filler_is_left_out(self):
        text = (self.digest('--budget', '1500') / '精选.md').read_text()
        self.assertIn('根因是提醒只存在内存里', text)
        self.assertIn('经验：重启后丢状态', text)
        self.assertNotIn('随便聊聊天气\n', text.split('〔略')[0])
        self.assertLess(len(text), 3500)
        self.assertIn('〔略', text)           # the skipped turns are still traceable

    def test_commands_are_extracted_word_for_word(self):
        assets = (self.digest() / '原文资产.md').read_text()
        self.assertIn('journalctl -u yali -n 100 --no-pager', assets)
        self.assertIn('### journalctl', assets)

    def test_terms_and_workflows_are_listed_as_candidates(self):
        chats = CHATS + [conversation(3, '风格词', [
            ('这种柔和阴影的风格叫什么', '这叫拟物化（Skeuomorphism），是指用柔和阴影做出凸起质感的界面风格。\n\n'
                                     '上手流程：\n\n1. 先定底色\n2. 再加双向阴影\n3. 最后调圆角'),
        ])]
        (self.source / 'conversations.json').write_text(json.dumps(chats, ensure_ascii=False))
        self.assertEqual(self.kb('start', self.source, self.work).returncode, 0)
        topics = json.loads(json.dumps(TOPICS))
        topics['topics'][0]['sessions'] = ['c001', 'c003']
        (self.work / 'topics.json').write_text(json.dumps(topics, ensure_ascii=False))
        assets = (self.digest() / '原文资产.md').read_text()
        self.assertIn('| 拟物化（Skeuomorphism） |', assets)
        self.assertIn('先定底色', assets.split('## 流程候选')[1].split('## 命令')[0])

    def test_a_term_needs_two_pieces_of_evidence_and_code_names_are_kept_apart(self):
        chats = CHATS + [conversation(3, '杂项', [
            ('帮我看看这个页面', '**判断**：页面没有问题。\n\n**长处**：加载快。\n\n字段 `user_profile` 要先建，`user_profile` 不能为空，'
                               '`user_profile` 关联 `auth_users`。'),
            ('什么是幂等', '幂等是指同一个操作执行多次，结果和执行一次相同。'),
        ])]
        (self.source / 'conversations.json').write_text(json.dumps(chats, ensure_ascii=False))
        self.assertEqual(self.kb('start', self.source, self.work).returncode, 0)
        topics = json.loads(json.dumps(TOPICS))
        topics['topics'][0]['sessions'] = ['c001', 'c003']
        (self.work / 'topics.json').write_text(json.dumps(topics, ensure_ascii=False))
        folder = self.digest()
        terms = (folder / '原文资产.md').read_text().split('## 流程候选')[0]
        self.assertIn('| 幂等 |', terms)             # asked about and explained
        self.assertNotIn('| 判断 |', terms)          # a bolded label, nothing else
        self.assertNotIn('| 长处 |', terms)
        self.assertNotIn('user_profile', terms)     # a field name is not a term
        names = json.loads((folder / 'assets.json').read_text())['names']
        self.assertIn('user_profile', [row['term'] for row in names])

    def test_topic_keywords_in_focus_file_pull_matching_exchanges_in(self):
        def weather_score():
            rows = json.loads((self.digest() / 'selection.json').read_text())['exchanges']
            return next(r['score'] for r in rows if r['ask'] == '随便聊聊天气')
        before = weather_score()
        (self.work / 'focus.json').write_text(json.dumps({'topics': {'芽历': ['天气']}}, ensure_ascii=False))
        self.assertGreater(weather_score(), before)
        (self.work / 'focus.json').write_text(json.dumps({'topics': {'别的话题': ['天气']}}, ensure_ascii=False))
        self.assertEqual(weather_score(), before)

    def test_every_conversation_with_signal_is_represented(self):
        chats = [conversation(n, f'会话{n}', [('怎么部署', f'第 {n} 种做法的解决方法是改成容器。' + FILLER * 8)]) for n in range(1, 7)]
        (self.source / 'conversations.json').write_text(json.dumps(chats, ensure_ascii=False))
        self.assertEqual(self.kb('start', self.source, self.work).returncode, 0)
        topics = {'topics': [{'name': '芽历', 'category': '01_项目', 'about': '种植记录应用', 'sessions': ['c001-c006'], 'keep': True}]}
        (self.work / 'topics.json').write_text(json.dumps(topics, ensure_ascii=False))
        text = (self.digest('--budget', '6000') / '精选.md').read_text()
        self.assertNotIn('选入 0/', text)

    def hover_topic(self):
        """hover is used all over a project and explained once, in a bold-then-"是" sentence."""
        turns = [(f'第 {i} 次改卡片', '加一个 hover 效果，卡片悬停时抬起。') for i in range(9)]
        turns.append(('什么是 hover', '**hover** 是鼠标悬停在元素上方的状态，不点击只是移过去。\n\n另见：术语速查：hover、feat、input token 等。'))
        chats = [conversation(1, '界面', turns[:5]), conversation(2, '交互', turns[5:])]
        (self.source / 'conversations.json').write_text(json.dumps(chats, ensure_ascii=False))
        self.assertEqual(self.kb('start', self.source, self.work).returncode, 0)
        topics = {'topics': [{'name': '芽历', 'category': '01_项目', 'about': '种植记录应用', 'sessions': ['c001', 'c002'], 'keep': True}]}
        (self.work / 'topics.json').write_text(json.dumps(topics, ensure_ascii=False))
        return self.digest()

    def test_a_term_used_often_gets_its_plainest_definition(self):
        folder = self.hover_topic()
        row = next(t for t in json.loads((folder / 'assets.json').read_text())['terms'] if t['term'] == 'hover')
        self.assertTrue(row['defined'])
        terms = (folder / '原文资产.md').read_text().split('## 流程候选')[0]
        self.assertIn('hover 是鼠标悬停在元素上方的状态', terms)

    def test_ui_vocabulary_is_listed_even_if_nobody_asked(self):
        turns = [(f'第 {i} 次', '弹窗里的按钮要对齐，点击弹窗外关闭。') for i in range(5)]
        chats = [conversation(1, '一', turns[:3]), conversation(2, '二', turns[3:] + [('再看', '弹窗 弹窗 弹窗')])]
        (self.source / 'conversations.json').write_text(json.dumps(chats, ensure_ascii=False))
        self.assertEqual(self.kb('start', self.source, self.work).returncode, 0)
        topics = {'topics': [{'name': '芽历', 'category': '01_项目', 'about': '种植记录应用', 'sessions': ['c001', 'c002'], 'keep': True}]}
        (self.work / 'topics.json').write_text(json.dumps(topics, ensure_ascii=False))
        folder = self.digest()
        self.assertIn('| 弹窗 |', (folder / '原文资产.md').read_text())
        self.assertTrue(next(t for t in json.loads((folder / 'assets.json').read_text())['terms'] if t['term'] == '弹窗')['lexicon'])

    def test_extra_vocabulary_comes_from_focus_file(self):
        turns = [(f'第 {i} 次', '这一版要换成折纸工艺的纹样。') for i in range(5)]
        chats = [conversation(1, '一', turns[:3]), conversation(2, '二', turns[3:])]
        (self.source / 'conversations.json').write_text(json.dumps(chats, ensure_ascii=False))
        self.assertEqual(self.kb('start', self.source, self.work).returncode, 0)
        topics = {'topics': [{'name': '芽历', 'category': '01_项目', 'about': '种植记录应用', 'sessions': ['c001', 'c002'], 'keep': True}]}
        (self.work / 'topics.json').write_text(json.dumps(topics, ensure_ascii=False))
        (self.work / 'focus.json').write_text(json.dumps({'lexicon': ['折纸']}, ensure_ascii=False))
        self.assertIn('| 折纸 |', (self.digest() / '原文资产.md').read_text())

    def test_verify_names_a_well_explained_term_the_pages_leave_out(self):
        self.hover_topic()
        # three conversations are needed for "across several conversations"; add one
        folder = self.work / 'digest' / '芽历'
        assets = json.loads((folder / 'assets.json').read_text())
        for row in assets['terms']:
            if row['term'] == 'hover':
                row.update(asked=True, defined=True, sessions=3)
        (folder / 'assets.json').write_text(json.dumps(assets, ensure_ascii=False))
        found = self.warnings({'01_项目/芽历/00_项目总览.md': '# 芽历 项目总览\n\n> 种植记录应用。\n\n卡片有动效。\n'})
        warn = next(w for w in found if w['code'] == 'TERMS_NOT_IN_PAGES')
        self.assertIn('hover', warn['terms'])
        found = self.warnings({'01_项目/芽历/00_项目总览.md': '# 芽历 项目总览\n\n> 种植记录应用。\n\n卡片有 hover 动效。\n'})
        self.assertNotIn('TERMS_NOT_IN_PAGES', {w['code'] for w in found})

    def test_gaps_reports_what_was_left_out_and_more_adds_it(self):
        self.digest('--budget', '300')
        self.write_vault({
            '00_知识库入口.md': '# 知识库入口\n\n> 从这里进入。\n\n- [[01_项目/芽历/00_项目总览]]\n',
            '01_项目/芽历/00_项目总览.md': '# 芽历 项目总览\n\n> 种植记录应用。\n\n提醒只存在内存里。\n',
        })
        result = self.kb('gaps', self.work, self.vault)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        summary = json.loads(result.stdout.strip().splitlines()[-1])
        row = summary['per_topic'][0]
        self.assertEqual(row['picked'] + row['high'] + row['mid'] + row['none'], 4)
        self.assertGreaterEqual(row['high'] + row['mid'], 1)
        self.assertIn('为什么会被省略', (self.work / '省略报告.md').read_text())
        before = row['picked']
        more = self.kb('more', self.work, '--tier', 'all')
        self.assertEqual(more.returncode, 0, more.stdout + more.stderr)
        extra = self.work / 'digest' / '芽历' / '补充1.md'
        self.assertTrue(extra.is_file())
        self.assertIn('并进已有页面', extra.read_text())
        after = json.loads(self.kb('gaps', self.work, self.vault).stdout.strip().splitlines()[-1])['per_topic'][0]
        self.assertGreater(after['picked'], before)

    def test_more_by_keyword_finds_a_left_out_exchange(self):
        self.digest('--budget', '700')
        more = json.loads(self.kb('more', self.work, '--keyword', '天气').stdout.strip().splitlines()[-1])
        self.assertEqual(more['topics'], 1)
        self.assertIn('随便聊聊天气', (self.work / 'digest' / '芽历' / '补充1.md').read_text())

    def test_show_and_find_open_the_full_text(self):
        self.digest('--budget', '1500')
        found = self.kb('find', self.work, '读回未完成')
        self.assertIn('芽历 u8', found.stdout)
        shown = self.kb('show', self.work, '芽历', 'u8')
        self.assertIn('解决方法：启动时从数据库读回未完成的提醒。', shown.stdout)
        self.assertEqual(shown.stdout.count(FILLER), 20)

    def test_topic_table_shows_reading_cost(self):
        self.assertEqual(self.kb('topics', self.work).returncode, 0)
        table = (self.work / '话题目录.md').read_text()
        self.assertIn('预计阅读', table)
        self.assertIn('| 1 | 芽历 | 01_项目 | 种植记录应用 | 1 |', table)

    def test_verify_passes_without_cards_or_placement(self):
        self.digest()
        self.write_vault({
            '00_知识库入口.md': '# 知识库入口\n\n> 从这里进入。\n\n- [[01_项目/芽历/00_项目总览]]\n',
            '01_项目/芽历/00_项目总览.md': '# 芽历 项目总览\n\n> 种植记录应用。\n\n## 重启后提醒不发\n\n提醒只存在内存里。\n\n'
                                       '```bash\njournalctl -u yali -n 100 --no-pager\n```\n',
        })
        result = self.kb('verify', self.work, self.vault)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads((self.work / 'vault_check.json').read_text())
        self.assertEqual(report['mode'], 'digest')
        self.assertEqual(report['topics']['芽历']['pages'], 1)

    def big_topic(self):
        """A topic whose chat is full of repeated commands, and nothing else of note."""
        command = '```bash\n{}\n```'
        names = ['git status', 'git pull origin main', 'npm run build', 'npm run dev', 'docker ps',
                 'docker compose up -d', 'git push origin main', 'npm install', 'docker logs app']
        turns = [(f'第 {i} 次怎么执行', f'运行：\n\n{command.format(name)}') for i in range(2) for name in names]
        chats = [conversation(1, '部署', turns)]
        (self.source / 'conversations.json').write_text(json.dumps(chats, ensure_ascii=False))
        self.assertEqual(self.kb('start', self.source, self.work).returncode, 0)
        topics = {'topics': [{'name': '芽历', 'category': '01_项目', 'about': '种植记录应用', 'sessions': ['c001'], 'keep': True}]}
        (self.work / 'topics.json').write_text(json.dumps(topics, ensure_ascii=False))
        self.digest()

    def warnings(self, pages):
        self.write_vault({'00_知识库入口.md': '# 知识库入口\n\n> 从这里进入。\n\n'
                          + ''.join(f'- [[{p[:-3]}]]\n' for p in pages), **pages})
        self.kb('verify', self.work, self.vault)
        return json.loads((self.work / 'vault_check.json').read_text())['warnings']

    def test_material_without_a_page_is_named_not_a_page_count(self):
        self.big_topic()
        found = self.warnings({'01_项目/芽历/00_项目总览.md': '# 芽历 项目总览\n\n> 种植记录应用。\n\n部署用容器。\n'})
        missing = next(w for w in found if w['code'] == 'MATERIAL_WITHOUT_A_PAGE')
        self.assertEqual([m['kind'] for m in missing['missing']], ['code'])
        self.assertGreaterEqual(missing['missing'][0]['in_source'], 8)

    def test_carrying_the_material_clears_the_warning_without_adding_pages(self):
        self.big_topic()
        blocks = ''.join(f'{why}：\n\n```bash\n{cmd}\n```\n\n' for why, cmd in [
            ('看状态', 'git status'), ('拉代码', 'git pull origin main'), ('构建', 'npm run build'),
            ('本地跑', 'npm run dev'), ('看容器', 'docker ps')])
        found = self.warnings({'01_项目/芽历/00_项目总览.md': '# 芽历 项目总览\n\n> 种植记录应用。\n\n## 常用命令\n\n' + blocks})
        self.assertNotIn('MATERIAL_WITHOUT_A_PAGE', {w['code'] for w in found})

    def test_a_small_topic_is_never_told_to_grow(self):
        self.digest()
        found = self.warnings({'01_项目/芽历/00_项目总览.md': '# 芽历 项目总览\n\n> 种植记录应用。\n\n提醒只存在内存里。\n'})
        self.assertNotIn('MATERIAL_WITHOUT_A_PAGE', {w['code'] for w in found})

    def test_padding_is_flagged(self):
        self.digest()
        body = ''.join(f'- 第 {i} 条：提醒只存在内存里，启动时要从数据库读回未完成的提醒。\n' for i in range(12))
        found = self.warnings({
            '01_项目/芽历/00_项目总览.md': '# 芽历 项目总览\n\n> 种植记录应用。\n\n' + body,
            '01_项目/芽历/01_过程复盘.md': '# 芽历 过程复盘\n\n> 复盘。\n\n' + body,
            '01_项目/芽历/02_决策记录.md': '# 芽历 决策记录\n\n> 决策。\n\n暂无。\n',
        })
        codes = {w['code'] for w in found}
        self.assertIn('PAGES_REPEAT_EACH_OTHER', codes)
        self.assertIn('PAGE_TOO_THIN', codes)

    def test_verify_flags_invented_commands_refs_and_bloat(self):
        self.digest()
        self.write_vault({
            '00_知识库入口.md': '# 知识库入口\n\n> 从这里进入。\n\n- [[01_项目/芽历/00_项目总览]]\n',
            '01_项目/芽历/00_项目总览.md': '# 芽历 项目总览\n\n> 种植记录应用。\n\n见【u8｜答】。\n\n'
                                       '```bash\nsystemctl restart something-never-said\n```\n\n' + ('- 一条很长的流水账。\n' * 700),
        })
        self.assertEqual(self.kb('verify', self.work, self.vault).returncode, 1)
        report = json.loads((self.work / 'vault_check.json').read_text())
        codes = {e['code'] for e in report['errors']}
        self.assertIn('UNMARKED_NONSOURCE_BLOCK', codes)
        self.assertIn('BOOKKEEPING_IN_READER_TEXT', codes)
        self.assertIn('PAGE_TOO_LONG', {w['code'] for w in report['warnings']})


class SkeletonTests(unittest.TestCase):
    def test_short_answers_are_kept_whole(self):
        self.assertEqual(digest.skeleton('就用第二个方案。', 400), '就用第二个方案。')

    def test_long_answers_keep_cause_and_commands(self):
        text = '\n\n'.join([FILLER * 3] * 6 + ['根因是端口没放行。', '```bash\nufw allow 443\n```'] + [FILLER * 3] * 6)
        out = digest.skeleton(text, 400)
        self.assertIn('根因是端口没放行。', out)
        self.assertIn('ufw allow 443', out)
        self.assertLess(len(out), 700)
        self.assertIn('〔…〕', out)

    def test_pushback_raises_the_score(self):
        answer = [{'text': '改成从数据库读。', 'role': 'assistant'}]
        calm, _ = digest.score_exchange([{'text': '嗯', 'role': 'human'}], answer, 3, 9)
        pushed, _ = digest.score_exchange([{'text': '还是不行，没有生效', 'role': 'human'}], answer, 3, 9)
        self.assertGreater(pushed, calm)

    def test_each_kind_of_reusable_content_is_recognised(self):
        def kind(ask, answer):
            _, points = digest.score_exchange([{'text': ask, 'role': 'human'}], [{'text': answer, 'role': 'assistant'}], 3, 9)
            return digest.dominant(points, digest.DEFAULT_FOCUS)
        self.assertEqual(kind('这种柔和阴影的风格叫什么', '这叫拟物化（Skeuomorphism），是指用柔和阴影做出凸起质感的界面风格。'
                              '相近的还有扁平化（Flat Design）。'), 'term')
        self.assertEqual(kind('本地怎么启动', '在终端运行：\n\n```bash\nnpm install\nnpm run dev\n```'), 'code')
        self.assertEqual(kind('这件事该怎么推进', '建议的流程：\n\n1. 先定目标\n2. 再拆任务\n3. 每周验收\n4. 复盘调整'), 'method')
        self.assertEqual(kind('选哪个好', '最终方案是自建，放弃托管。代价是要自己运维，相比之下更适合长期。'), 'decision')

    def test_focus_file_changes_what_wins(self):
        ask = [{'text': '折纸工艺里这一步怎么处理', 'role': 'human'}]
        answer = [{'text': '折痕压好之后再翻面。', 'role': 'assistant'}]
        plain, _ = digest.score_exchange(ask, answer, 3, 9)
        focus = {'weights': dict(digest.DEFAULT_FOCUS['weights']), 'keywords': {'any': __import__('re').compile('折纸|折痕')}}
        focused, _ = digest.score_exchange(ask, answer, 3, 9, focus)
        self.assertGreater(focused, plain)

    def test_term_candidates_drop_filler_and_menu_labels(self):
        self.assertEqual(digest.clean_term('状态驱动的纹理混合（Texture Blending）'), '纹理混合（Texture Blending）')
        self.assertEqual(digest.clean_term('时空纹理'), '时空纹理')
        for junk in ('这个', '文件（File）', '下一步', '第一步', '注意', '第一步（NotebookLM）'):
            self.assertIsNone(digest.clean_term(junk), junk)


if __name__ == '__main__':
    unittest.main()
