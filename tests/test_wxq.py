"""Tests for wxq.py against a synthetic archive that follows the public format contract.

Run: python -m unittest discover -s tests
Requires ripgrep (rg) on PATH.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(__file__), '..', 'wechat-archive', 'scripts', 'wxq.py')

ZHANG_09 = '''# 张三 · 2026-09

会话：zhangsan_id
我：my_id

<a id="msg-a1"></a>
### 2026-09-01 10:05:00 · 张三 · 文本

发送者：zhangsan_id

> 周五交方案，报价 3000\\_元 \\[笑\\]
> 链接 https://x.cn/?a=1&amp;b=2

<a id="msg-a2"></a>
### 2026-09-01 10:06 · 我自己 · 文件/链接/卡片

发送者：my_id · 引用：msg-a1 · 提到：zhangsan_id，lisi_id

> 好的，结案报告见附件

- [文件](../文件/2026-09-01_100600_%E7%BB%93%E6%A1%88%E6%8A%A5%E5%91%8A.pdf)

<a id="msg-a3"></a>
### 2026-09-30 23:59:00 · 张三 · 语音

发送者：zhangsan_id

> [语音]

- [语音](../语音/2026-09-30_235900.mp3)
  转写：明天开会讨论投放预算
'''

ZHANG_10 = '''# 张三 · 2026-10

会话：zhangsan_id
我：my_id

<a id="msg-b1"></a>
### 2026-10-02 09:00:00 · 张三 · 文本

发送者：zhangsan_id

> 方案改到下周一交
'''

GROUP_10 = '''# 项目群 · 2026-10

会话：123@chatroom
我：my_id
群人数：5

<a id="msg-c1"></a>
### 2026-10-02 09:30:00 · 李四 · 文本

发送者：lisi_id · 提到：notify@all

> 投放预算定为 5 万 · 已确认

<a id="msg-c2"></a>
### 2026-10-03 11:00:00 · 我自己 · 文本

发送者：my_id

> 收到，按 5 万执行

<a id="msg-c3"></a>
### 2026-10-03 11:01:00 ·  · 系统消息

发送者：

> "王五"加入了群聊
'''

ATTACHMENT_MD = '''# 别人发来的笔记 · 2026-10

<a id="msg-zz"></a>
### 2026-10-01 00:00:00 · 假的 · 文本

发送者：fake

> 报价 3000_元
'''


def build(root):
    files = {
        '私聊/张三/记录/2026-09.md': ZHANG_09,
        '私聊/张三/记录/2026-10.md': ZHANG_10,
        '工作/公司/项目群/记录/2026-10.md': GROUP_10,
        '私聊/张三/文件/笔记.md': ATTACHMENT_MD,
    }
    for rel, text in files.items():
        path = os.path.join(root, *rel.split('/'))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(text)


class WxqTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which('rg'):
            raise unittest.SkipTest('rg not on PATH')
        cls.root = tempfile.mkdtemp(prefix='wxq-test-')
        build(cls.root)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def run_wxq(self, *args, env=None, archive=True):
        cmd = [sys.executable, '-I', SCRIPT] + (['--archive', self.root] if archive else []) + list(args)
        proc = subprocess.run(cmd, capture_output=True, env=env)
        return (proc.returncode, proc.stdout.decode('utf-8').replace('\r\n', '\n'),
                proc.stderr.decode('utf-8').replace('\r\n', '\n'))

    def anchors(self, out):
        return re.findall(r'#(msg-[A-Za-z0-9]+)\s*$', out, re.M)

    def test_escaped_text_matches_plain_terms(self):
        _, out, _ = self.run_wxq('find', '3000_元 [笑]')
        self.assertEqual(self.anchors(out), ['msg-a1'])
        _, out, _ = self.run_wxq('find', 'a=1&b=2')
        self.assertEqual(self.anchors(out), ['msg-a1'])

    def test_attachment_markdown_is_not_a_volume(self):
        _, out, _ = self.run_wxq('find', '报价', '3000')
        self.assertNotIn('msg-zz', out)

    def test_attachment_name_and_extra_lines_are_searchable(self):
        _, out, _ = self.run_wxq('find', '结案报告')
        self.assertEqual(self.anchors(out), ['msg-a2'])
        self.assertIn('结案报告.pdf', out)
        _, out, _ = self.run_wxq('find', '投放预算')
        self.assertEqual(sorted(self.anchors(out)), ['msg-a3', 'msg-c1'])

    def test_and_any_and_filters(self):
        _, out, _ = self.run_wxq('find', '方案', '下周一')
        self.assertEqual(self.anchors(out), ['msg-b1'])
        _, out, _ = self.run_wxq('find', '方案', '5 万', '--any', '--oldest')
        self.assertEqual(self.anchors(out), ['msg-a1', 'msg-b1', 'msg-c1', 'msg-c2'])
        _, out, _ = self.run_wxq('find', '方案', '--from', '2026-10')
        self.assertEqual(self.anchors(out), ['msg-b1'])
        _, out, _ = self.run_wxq('find', '5 万', '--chat', '工作/公司')
        self.assertEqual(sorted(self.anchors(out)), ['msg-c1', 'msg-c2'])

    def test_sender_filters(self):
        _, out, _ = self.run_wxq('find', '--sender', 'me', '--oldest')
        self.assertEqual(self.anchors(out), ['msg-a2', 'msg-c2'])
        _, out, _ = self.run_wxq('find', '--sender', '李四')
        self.assertEqual(self.anchors(out), ['msg-c1'])
        _, out, _ = self.run_wxq('count', '--chat', '张三', '--by', 'sender')
        self.assertIn('<zhangsan_id> 张三', out)

    def test_metadata_parsing(self):
        _, out, _ = self.run_wxq('find', '结案报告')
        self.assertIn('quotes msg-a1', out)
        self.assertIn('@zhangsan_id,lisi_id', out)
        self.assertIn('[me]', out)
        _, out, _ = self.run_wxq('find', '已确认')
        self.assertIn('5 万 · 已确认', out)
        self.assertIn('@notify@all', out)

    def test_context_crosses_months(self):
        _, out, _ = self.run_wxq('context', 'msg-b1', '-B', '1', '-A', '0')
        self.assertEqual(self.anchors(out), ['msg-a3', 'msg-b1'])
        self.assertIn('(end of this chat in the archive)', out)
        code, _, err = self.run_wxq('context', 'msg-nope')
        self.assertEqual(code, 2)

    def test_chats_and_who(self):
        _, out, _ = self.run_wxq('chats')
        self.assertIn('私聊/张三 | zhangsan_id | 4 msgs | 2026-09 – 2026-10 | 2 volumes', out)
        self.assertIn('工作/公司/项目群 | 123@chatroom | 3 msgs', out)
        self.assertIn('members 5', out)
        self.assertNotIn('别人发来的笔记', out)
        _, out, _ = self.run_wxq('who', '李')
        self.assertIn('<lisi_id>', out)

    def test_root_resolution_and_errors(self):
        env = dict(os.environ, WECHAT_ARCHIVE=self.root)
        code, out, _ = self.run_wxq('count', '方案', env=env, archive=False)
        self.assertEqual(code, 0)
        self.assertIn('2 messages', out)
        code, _, err = self.run_wxq('find', 'x', '--to', '2026-9')
        self.assertEqual(code, 2)
        self.assertIn('bad date', err)

    def test_config_json(self):
        skill = tempfile.mkdtemp(prefix='wxq-skill-')
        try:
            os.makedirs(os.path.join(skill, 'scripts'))
            shutil.copy(SCRIPT, os.path.join(skill, 'scripts', 'wxq.py'))
            with open(os.path.join(skill, 'config.json'), 'w', encoding='utf-8') as fh:
                json.dump({'archive': self.root}, fh)
            env = {k: v for k, v in os.environ.items() if k != 'WECHAT_ARCHIVE'}
            proc = subprocess.run([sys.executable, '-I', os.path.join(skill, 'scripts', 'wxq.py'),
                                   'count', '方案'], capture_output=True, env=env)
            self.assertIn('2 messages', proc.stdout.decode('utf-8'))
        finally:
            shutil.rmtree(skill, ignore_errors=True)

    def test_count_by_chatday(self):
        _, out, _ = self.run_wxq('count', '方案', '5 万', '--any', '--by', 'chatday')
        self.assertIn('4 messages on 4 (day, chat) pairs', out)
        self.assertIn('1  2026-10-02 私聊/张三', out)
        self.assertIn('1  2026-10-03 工作/公司/项目群', out)

    def test_count_by_type(self):
        _, out, _ = self.run_wxq('count', '--chat', '张三', '--by', 'type')
        self.assertIn('2  文本', out)
        self.assertIn('1  语音', out)

    def test_digest_pages_and_days(self):
        _, out, _ = self.run_wxq('digest', '--chat', '张三', '--size', '2')
        self.assertIn('4 messages in 1 chats; page 1/2', out)
        self.assertIn('## 2026-09-01', out)
        self.assertIn('10:06 我自己 [me] [文件/链接/卡片] (re msg-a1): 好的，结案报告见附件 | 2026-09-01_100600_结案报告.pdf #msg-a2', out)
        _, out, _ = self.run_wxq('digest', '--chat', '张三', '--size', '2', '--page', '2')
        self.assertIn('[语音]', out)
        self.assertIn('#msg-b1', out)
        code, _, _ = self.run_wxq('digest', '--chat', '张三', '--page', '9')
        self.assertEqual(code, 2)

    def test_files(self):
        _, out, _ = self.run_wxq('files', '--name', '结案')
        self.assertIn('1 files; by extension: .pdf:1', out)
        self.assertIn('(missing)', out)
        self.assertIn('#msg-a2', out)
        _, out, _ = self.run_wxq('files', '--chat', '张三')
        self.assertIn('2 files', out)

    def test_activity(self):
        _, out, _ = self.run_wxq('activity')
        self.assertIn('<my_id> 2 messages in 2 chats, 2026-09-01 – 2026-10-03', out)
        self.assertIn('names used: 我自己 (2)', out)
        self.assertRegex(out, r'1 \|\s+4 \| 2026-09-01 – 2026-09-01 \| 私聊/张三 \| zhangsan_id')
        _, out, _ = self.run_wxq('activity', '--sender', 'zhangsan_id', '--from', '2026-10')
        self.assertIn('1 messages in 1 chats', out)
        self.assertIn('private chat with this sender', out)

    def test_extract_office_files(self):
        import zipfile
        d = tempfile.mkdtemp(prefix='wxq-office-')
        try:
            w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
            with zipfile.ZipFile(os.path.join(d, 'r.docx'), 'w') as z:
                z.writestr('word/document.xml', f'<w:document {w}><w:body><w:p><w:r><w:t>结案</w:t></w:r>'
                           '<w:r><w:t>报告</w:t></w:r></w:p><w:p><w:r><w:t>曝光 120 万</w:t></w:r></w:p></w:body></w:document>')
            a = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
            with zipfile.ZipFile(os.path.join(d, 's.pptx'), 'w') as z:
                for n in (2, 10):
                    z.writestr(f'ppt/slides/slide{n}.xml', f'<p:sld {a} xmlns:p="p"><a:p><a:r><a:t>第{n}页</a:t></a:r></a:p></p:sld>')
            s = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
            r = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
            with zipfile.ZipFile(os.path.join(d, 't.xlsx'), 'w') as z:
                z.writestr('xl/workbook.xml', f'<workbook {s} {r}><sheets><sheet name="数据" r:id="rId1"/></sheets></workbook>')
                z.writestr('xl/_rels/workbook.xml.rels', '<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
                z.writestr('xl/sharedStrings.xml', f'<sst {s}><si><t>阅读量</t></si></sst>')
                z.writestr('xl/worksheets/sheet1.xml', f'<worksheet {s}><sheetData><row><c t="s"><v>0</v></c><c><v>3500</v></c></row></sheetData></worksheet>')
            _, out, _ = self.run_wxq('extract', os.path.join(d, 'r.docx'))
            self.assertIn('结案报告\n曝光 120 万', out)
            _, out, _ = self.run_wxq('extract', os.path.join(d, 's.pptx'))
            self.assertLess(out.index('slide 2 '), out.index('slide 10 '))
            _, out, _ = self.run_wxq('extract', os.path.join(d, 't.xlsx'))
            self.assertIn('--- sheet 数据 ---\n阅读量\t3500', out)
            code, _, err = self.run_wxq('extract', os.path.join(d, 'nope.doc'))
            self.assertEqual(code, 2)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_review_regressions(self):
        # a term found only in the attachment's folder path must not match
        _, out, _ = self.run_wxq('find', '结案报告', '私聊')
        self.assertIn('0 messages', out)
        # a volume with a header but no messages must not break chats
        empty = os.path.join(self.root, '私聊', '空会话', '记录', '2026-08.md')
        os.makedirs(os.path.dirname(empty), exist_ok=True)
        with open(empty, 'w', encoding='utf-8') as fh:
            fh.write('# 空会话 · 2026-08\n\n会话：empty_id\n我：my_id\n')
        try:
            code, out, _ = self.run_wxq('chats')
            self.assertEqual(code, 0)
            self.assertIn('私聊/空会话 | empty_id | 0 msgs', out)
        finally:
            shutil.rmtree(os.path.dirname(os.path.dirname(empty)))
        _, out, _ = self.run_wxq('find', '方案', '--any', '5 万', '--oldest')
        self.assertEqual(self.anchors(out), ['msg-a1', 'msg-b1', 'msg-c1', 'msg-c2'])
        code, _, err = self.run_wxq('find', '方案', '--bogus')
        self.assertEqual(code, 2)
        code, _, err = self.run_wxq('digest', '--chat', '张三', '--size', '0')
        self.assertEqual(code, 2)
        self.assertIn('--size must be at least 1', err)

    def test_md_file_in_chat_folder_does_not_double_count(self):
        extra = os.path.join(self.root, '私聊', '张三', '说明.md')
        with open(extra, 'w', encoding='utf-8') as fh:
            fh.write('# 说明\n')
        try:
            _, out, _ = self.run_wxq('activity', '--chat', '私聊')
            self.assertIn('<my_id> 1 messages in 1 chats', out)
            _, out, _ = self.run_wxq('count', '--chat', '私聊', '--sender', 'me')
            self.assertIn('1 messages', out)
        finally:
            os.remove(extra)

    def test_link_relative_to_chat_folder(self):
        chat = os.path.join(self.root, '私聊', '链接测试')
        os.makedirs(os.path.join(chat, '记录'))
        os.makedirs(os.path.join(chat, '图片'))
        open(os.path.join(chat, '图片', 'a.jpg'), 'wb').close()
        with open(os.path.join(chat, '记录', '2026-07.md'), 'w', encoding='utf-8') as fh:
            fh.write('# 链接测试 · 2026-07\n\n会话：link_id\n我：my_id\n\n<a id="msg-l1"></a>\n'
                     '### 2026-07-01 08:00:00 · 某人 · 图片\n\n发送者：link_id\n\n> [图片]\n\n- [图片](图片/a.jpg)\n')
        try:
            _, out, _ = self.run_wxq('files', '--chat', '链接测试')
            self.assertIn(os.path.join(chat, '图片', 'a.jpg'), out)
            self.assertNotIn('(missing)', out)
        finally:
            shutil.rmtree(chat)

    def test_extract_rejects_bad_files(self):
        import zipfile
        d = tempfile.mkdtemp(prefix='wxq-bad-')
        try:
            w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
            dtd = '<!-- ' + 'x' * 5000 + ' --><!DOCTYPE d [<!ENTITY e "boom">]>'
            with zipfile.ZipFile(os.path.join(d, 'dtd.docx'), 'w') as z:
                z.writestr('word/document.xml', f'{dtd}<w:document {w}><w:body><w:p><w:r><w:t>&e;</w:t></w:r></w:p></w:body></w:document>')
            with zipfile.ZipFile(os.path.join(d, 'broken.docx'), 'w') as z:
                z.writestr('word/document.xml', '<w:document><unclosed>')
            with zipfile.ZipFile(os.path.join(d, 'noworkbook.xlsx'), 'w') as z:
                z.writestr('xl/sharedStrings.xml', '<sst/>')
            for name, msg in (('dtd.docx', 'DTD'), ('broken.docx', 'cannot extract'), ('noworkbook.xlsx', 'cannot extract')):
                code, out, err = self.run_wxq('extract', os.path.join(d, name))
                self.assertEqual(code, 2, name)
                self.assertIn(msg, err)
                self.assertNotIn('Traceback', err)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_link(self):
        code, out, _ = self.run_wxq('link', 'msg-c1', '证据 #msg-a1', 'msg-nope')
        self.assertEqual(code, 0)
        lines = out.strip().split('\n')
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith('msg-c1  [2026-10-02 09:30 · 李四 · 项目群](file:///'))
        self.assertTrue(lines[0].endswith('.md#msg-c1)  ｜ 投放预算定为 5 万 · 已确认'))
        self.assertIn('｜ 周五交方案，报价 3000_元 [笑]', lines[1])  # body unescaped for checking quotes
        self.assertIn('%E9%A1%B9%E7%9B%AE%E7%BE%A4', lines[0])  # 项目群, percent-encoded in the URI
        self.assertTrue(lines[1].startswith('msg-a1  [2026-09-01 10:05 · 张三 · 与张三的私聊](file:///'))
        self.assertEqual(lines[2], 'msg-nope  (not found)')
        code, _, err = self.run_wxq('link', 'nothing')
        self.assertEqual(code, 2)
        self.assertIn('not an anchor', err)

    def test_read_only(self):
        before = {os.path.join(d, f): os.path.getmtime(os.path.join(d, f))
                  for d, _, fs in os.walk(self.root) for f in fs}
        self.run_wxq('find', '方案')
        self.run_wxq('chats')
        after = {os.path.join(d, f): os.path.getmtime(os.path.join(d, f))
                 for d, _, fs in os.walk(self.root) for f in fs}
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
