"""Synthetic fixtures only; tests check behavior, not generated wording."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from prepare_sources import prepare, chunks, messages
from vault_inventory import inventory, compare
from scan_sensitive_data import is_placeholder


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.source = self.base / 'source'
        self.source.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def save(self, data, name='chat.json'):
        (self.source / name).write_text(json.dumps(data, ensure_ascii=False))

    def test_incremental_and_idempotent_import(self):
        data = [{'uuid': 'synthetic-conversation', 'chat_messages': [{'uuid': 'm1', 'sender': 'human', 'text': '主体周围保留空白。'}]}]
        self.save(data)
        original = (self.source / 'chat.json').read_bytes()
        m1, u1 = prepare(self.source, self.base / 'one', 'synthetic')
        m2, u2 = prepare(self.source, self.base / 'two', 'synthetic', self.base / 'one/source_manifest.json')
        self.assertEqual(m2['sources'][0]['change'], 'unchanged')
        self.assertEqual(u1, u2)
        self.assertEqual(original, (self.source / 'chat.json').read_bytes())
        data[0]['chat_messages'].append({'uuid': 'm2', 'sender': 'human', 'text': '手机正文需要更紧凑。'})
        self.save(data)
        m3, u3 = prepare(self.source, self.base / 'three', 'synthetic', self.base / 'one/source_manifest.json')
        self.assertEqual(m3['sources'][0]['change'], 'changed')
        self.assertEqual(u1[0]['unit_id'], u3[0]['unit_id'])

    def test_source_is_read_only_and_snapshot_not_overwritten(self):
        self.save({'messages': [{'role': 'user', 'content': 'Keep this exact input.'}]})
        before = (self.source / 'chat.json').read_bytes()
        prepare(self.source, self.base / 'out', 'test')
        self.assertEqual(before, (self.source / 'chat.json').read_bytes())
        with self.assertRaises(ValueError):
            prepare(self.source, self.base / 'out', 'test')
        with self.assertRaises(ValueError):
            prepare(self.source, self.source / 'out', 'test')

    def test_unknown_json_is_visible_failure(self):
        self.save({'unrecognized': ['value']})
        m, u = prepare(self.source, self.base / 'out', 'test')
        self.assertEqual(m['sources'][0]['status'], 'not_a_conversation')
        self.assertEqual(u, [])

    def test_mixed_container_not_silently_dropped(self):
        self.save([{'messages': [{'role': 'user', 'content': 'supported'}]}, {'other': 'unhandled'}])
        m, _ = prepare(self.source, self.base / 'out', 'test')
        self.assertEqual(m['sources'][0]['status'], 'not_a_conversation')

    def test_mapping_branches_preserved(self):
        data = {'current_node': 'b', 'mapping': {
            'r': {'parent': None, 'message': None},
            'a': {'parent': 'r', 'message': {'author': {'role': 'user'}, 'content': {'parts': ['question']}}},
            'c': {'parent': 'a', 'message': {'author': {'role': 'assistant'}, 'content': {'parts': ['alternative']}}},
            'b': {'parent': 'a', 'message': {'author': {'role': 'assistant'}, 'content': {'parts': ['selected']}}}}}
        rows = messages(data)
        self.assertEqual([r['_active'] for r in rows], [True, False, True])
        self.save(data)
        _, units = prepare(self.source, self.base / 'out', 'test')
        self.assertTrue(any('alternative' in u['text'] for u in units))

    def test_cycle_rejected(self):
        with self.assertRaises(ValueError):
            messages({'mapping': {'a': {'parent': 'b'}, 'b': {'parent': 'a'}}})

    def test_code_not_split(self):
        block = '```text\n' + 'a\n' * 200 + '```\n'
        parts = chunks('intro\n\n' + block + '\nafter', 100)
        self.assertTrue(any(block in p for p in parts))
        self.assertEqual(''.join(parts), 'intro\n\n' + block + '\nafter')

    def test_jsonl_roles_and_attachments(self):
        rows = [{'role': 'user', 'text': 'question', 'attachments': [{'file_name': 'image.png', 'extracted_content': 'not authorized OCR'}]}, {'role': 'assistant', 'content': 'answer'}]
        (self.source / 'chat.jsonl').write_text('\n'.join(json.dumps(r) for r in rows))
        m, units = prepare(self.source, self.base / 'out', 'test')
        self.assertEqual([u['role'] for u in units], ['user', 'assistant'])
        self.assertEqual(m['attachments'][0]['status'], 'registered_not_processed')
        self.assertFalse(any('not authorized OCR' in u['text'] for u in units))

    def test_missing_source_is_not_delete_action(self):
        self.save({'messages': [{'role': 'user', 'text': 'history'}]})
        prepare(self.source, self.base / 'one', 'test')
        (self.source / 'chat.json').unlink()
        m, _ = prepare(self.source, self.base / 'two', 'test', self.base / 'one/source_manifest.json')
        self.assertEqual(len(m['missing_source_ids']), 1)
        self.assertNotIn('delete', m)

    def test_inline_media_registered(self):
        self.save({'messages': [{'role': 'user', 'content': [{'type': 'image', 'source': {'type': 'base64', 'data': 'synthetic'}}]}]})
        m, _ = prepare(self.source, self.base / 'out', 'test')
        self.assertEqual(m['attachments'][0]['media_type'], 'image')
        self.assertEqual(m['attachments'][0]['status'], 'registered_not_processed')



class InventoryAndPrivacyTests(unittest.TestCase):
    def test_manual_changes_detected_without_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'note.md'
            path.write_text('initial')
            before = inventory(root)
            path.write_text('manual edit')
            after = inventory(root)
            self.assertEqual(compare(before, after)['changed'], ['note.md'])
            self.assertEqual(path.read_text(), 'manual edit')

    def test_unrelated_placeholder_does_not_hide_secret(self):
        self.assertFalse(is_placeholder('example.com plus sensitive value', 'some-real-credential'))


if __name__ == '__main__':
    unittest.main()
