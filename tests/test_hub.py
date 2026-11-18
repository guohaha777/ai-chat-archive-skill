"""The cross-topic hub: terms, commands and AI instructions from every topic in one place,
each row linking back to where it lives."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
KB = ROOT / 'scripts' / 'kb.py'
sys.path.insert(0, str(ROOT / 'scripts'))
import check_vault  # noqa: E402

PAGES = {
    '00_知识库入口.md': '# 知识库入口\n\n> 从这里进入。\n\n## 01 项目\n\n- [[01_项目/芽历/00_项目总览]]\n- [[01_项目/芽历/01_术语与命令]]\n'
                     '- [[02_专题知识/摄影入门/00_曝光速查]]\n\n## 使用边界\n\n- 会过时。\n',
    '01_项目/芽历/00_项目总览.md': '# 芽历 项目总览\n\n> 种植记录应用。\n\n## 阶段\n\n| 阶段 | 做了什么 |\n| --- | --- |\n| 原型 | 画界面 |\n',
    '01_项目/芽历/01_术语与命令.md': '# 芽历 术语与命令\n\n> 速查。\n\n## 术语\n\n| 术语 | 含义 |\n| --- | --- |\n'
                              '| 幂等 | 同一操作做多次结果不变 |\n| 景深（Depth of Field） | 界面里借用的虚化层次 |\n\n'
                              '## 上传代码\n\n推送到远程：\n\n```bash\ngit add .\ngit commit -m "update"\ngit push\n```\n\n'
                              '## 给 Cursor 的指令\n\n只读排查用：\n\n```text\n请不要修改任何代码，只排查登录状态为什么不同步，并报告涉及的文件和行号。\n```\n',
    '02_专题知识/摄影入门/00_曝光速查.md': '# 摄影入门 曝光速查\n\n> 速查。\n\n## 术语\n\n| 术语 | 含义 | 用在哪 |\n| --- | --- | --- |\n'
                                 '| 景深 | 清晰范围的前后深度 | 人像 |\n| 光圈 | 镜头进光孔的大小 | 曝光 |\n',
}


class HubTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.vault = Path(self.temp.name) / 'vault'
        for rel, text in PAGES.items():
            path = self.vault / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def tearDown(self):
        self.temp.cleanup()

    def hub(self):
        result = subprocess.run([sys.executable, str(KB), 'hub', str(self.vault)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def read(self, name):
        return (self.vault / '00_跨话题汇总' / name).read_text()

    def test_collects_from_every_topic_and_links_back(self):
        summary = self.hub()
        self.assertEqual((summary['terms'], summary['commands'], summary['prompts']), (4, 1, 1))
        terms = self.read('00_术语总览.md')
        self.assertIn('| 幂等 | 同一操作做多次结果不变 | [[01_项目/芽历/01_术语与命令#术语\\|术语]] |', terms)
        self.assertIn('| 光圈 | 镜头进光孔的大小 |', terms)
        self.assertNotIn('画界面', terms)                    # an ordinary table is not a glossary
        commands = self.read('01_命令总览.md')
        self.assertIn('## Git', commands)
        self.assertIn('git commit -m "update"', commands)
        self.assertIn('[[01_项目/芽历/01_术语与命令#上传代码\\|芽历 · 上传代码]]', commands)
        prompts = self.read('02_AI指令总览.md')
        self.assertIn('只读排查用', prompts)
        self.assertIn('[[01_项目/芽历/01_术语与命令#给 Cursor 的指令', prompts)

    def test_a_term_used_in_two_topics_is_shown_side_by_side(self):
        self.hub()
        shared = self.read('00_术语总览.md').split('## 多个话题都有的术语')[1].split('\n## ')[0]
        self.assertIn('景深', shared)
        self.assertIn('界面里借用的虚化层次', shared)
        self.assertIn('清晰范围的前后深度', shared)
        self.assertNotIn('光圈', shared)

    def test_entry_page_gets_one_section_and_originals_are_untouched(self):
        before = (self.vault / '01_项目/芽历/01_术语与命令.md').read_text()
        self.hub()
        self.hub()                                          # rebuilding does not pile up
        entry = (self.vault / '00_知识库入口.md').read_text()
        self.assertEqual(entry.count('## 跨话题汇总'), 1)
        self.assertLess(entry.index('## 跨话题汇总'), entry.index('## 01 项目'))
        self.assertIn('[[00_跨话题汇总/00_术语总览\\|术语总览]]', entry)
        self.assertIn('## 使用边界', entry)
        self.assertEqual((self.vault / '01_项目/芽历/01_术语与命令.md').read_text(), before)

    def test_hub_pages_pass_links_and_structure_checks(self):
        self.hub()
        files = {p.relative_to(self.vault).as_posix(): p.read_text() for p in self.vault.rglob('*.md')}
        errors, _ = check_vault.check_links(files)
        self.assertEqual(errors, [])

    def test_vault_without_assets_gets_no_hub(self):
        for rel in ('01_项目/芽历/01_术语与命令.md', '02_专题知识/摄影入门/00_曝光速查.md'):
            (self.vault / rel).unlink()
        summary = self.hub()
        self.assertEqual(summary['terms'], 0)
        self.assertFalse((self.vault / '00_跨话题汇总').exists())


if __name__ == '__main__':
    unittest.main()
