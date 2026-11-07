"""Read-only search over a WeChat Markdown chat archive (Python 3.11+ stdlib + ripgrep).

Subcommands: find, count, context, digest, files, who, chats, activity, extract, link.  Run `wxq.py <subcommand> -h` for options.
The archive root comes from --archive, else $WECHAT_ARCHIVE, else "archive" in config.json
next to this skill's SKILL.md.  Nothing is written anywhere.
"""
import argparse
import datetime
import html
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, unquote

ROOT = ''
ANCHOR = re.compile(r'<a id="(msg-[A-Za-z0-9]+)"></a>$')
H1 = re.compile(r'^# (.+) · (\d{4}-\d{2})(?: · [^·]+)?$')
HEADING = re.compile(r'^### (\d{4}-\d\d-\d\d \d\d:\d\d(?::\d\d)?) · (.*) · ([^·]+)$')
LINK = re.compile(r'^\s*[-*] \[([^\]]*)\]\((<[^>]*>|[^)\s]*)\)(.*)$')
UNESCAPE = re.compile(r'\\([!-/:-@\[-`{-~])')
RG_META = set('\\.+*?()|[]{}^$#&-~')
ENTITY = {'&': 'amp', '<': 'lt', '>': 'gt', '"': 'quot'}
LINE_PREFIX = re.compile(rb'(\d+)[:-]')
ANY_MONTH = '\uffff'


def die(msg):
    print('wxq: ' + msg, file=sys.stderr)
    sys.exit(2)


def resolve_root(arg):
    if arg:
        return os.path.normpath(arg)
    if os.environ.get('WECHAT_ARCHIVE'):
        return os.path.normpath(os.environ['WECHAT_ARCHIVE'])
    cfg = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config.json')
    if os.path.isfile(cfg):
        with open(cfg, encoding='utf-8-sig') as fh:
            value = json.load(fh).get('archive')
        if value:
            return os.path.normpath(os.path.expandvars(os.path.expanduser(value)))
    die('archive root unknown: pass --archive, set WECHAT_ARCHIVE, or put {"archive": "<path>"} '
        'in config.json beside SKILL.md.')


def rg_bin():
    exe = shutil.which('rg')
    if not exe:
        die('ripgrep (rg) not found on PATH; it is required.')
    return exe


def rg_escape(s):
    return ''.join('\\' + c if c in RG_META else c for c in s)


def tolerant(term):
    """Regex matching term as written in Markdown under any escaping style:
    optional backslash before ASCII punctuation, HTML entities for & < > "."""
    out = []
    for c in term:
        if c in ENTITY:
            out.append(f'(?:\\\\?{rg_escape(c)}|&{ENTITY[c]};)')
        elif c.isascii() and not c.isalnum() and not c.isspace():
            out.append('\\\\?' + rg_escape(c))
        else:
            out.append(rg_escape(c))
    return ''.join(out)


def unescape(s):
    return html.unescape(UNESCAPE.sub(r'\1', s))


def rg_stream(args_, roots=None, files_only=False):
    """Stream rg over *.md under the roots. Yields (path, line_no, text) or, with files_only, path."""
    cmd = [rg_bin(), '--no-config', '--hidden', '--no-ignore', '--no-messages', '--null',
           '--color', 'never', '--glob', '*.md']
    cmd += ['-l'] if files_only else ['-n', '--no-heading']
    cmd += [*args_, '--', *(roots or [ROOT])]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    finished = False
    try:
        if files_only:
            for raw in proc.stdout.read().split(b'\0'):
                if raw.strip():
                    yield raw.strip().decode('utf-8')
        else:
            for raw in proc.stdout:
                path, nul, rest = raw.rstrip(b'\n').partition(b'\0')
                m = LINE_PREFIX.match(rest) if nul else None
                if m:  # match lines "N:", context lines "N-"; "--" separators carry no path
                    yield (path.decode('utf-8'), int(m.group(1)),
                           rest[m.end():].decode('utf-8', 'replace').rstrip('\r'))
        finished = True
    finally:
        if not finished:
            proc.kill()
        code = proc.wait()
    if finished and code == 2:
        print('wxq: rg could not read some files; results may be incomplete.', file=sys.stderr)
    elif finished and code not in (0, 1):
        die(f'rg failed (exit {code}).')


def rg_counts(pattern, roots=None):
    """{path: number of lines matching pattern} over *.md under the roots."""
    cmd = [rg_bin(), '--no-config', '--hidden', '--no-ignore', '--no-messages', '--null', '--color', 'never',
           '--glob', '*.md', '-c', '-e', pattern, '--', *(roots or [ROOT])]
    out = {}
    for raw in subprocess.run(cmd, capture_output=True).stdout.split(b'\n'):
        path, nul, n = raw.partition(b'\0')
        if nul and n.strip().isdigit():
            out[path.decode('utf-8')] = int(n)
    return out


def md_files():
    proc = subprocess.run([rg_bin(), '--no-config', '--hidden', '--no-ignore', '--files', '--null',
                           '--glob', '*.md', ROOT], capture_output=True)
    return [p.decode('utf-8') for p in proc.stdout.split(b'\0') if p.strip()]


def read_lines(path):
    with open(path, encoding='utf-8-sig', newline='') as fh:
        return [l.rstrip('\r') for l in fh.read().split('\n')]


def header(lines):
    """Volume header facts, or None when the file is not a chat volume."""
    info = {}
    for l in lines[:12]:
        h = H1.match(l)
        if h and 'name' not in info:
            info['name'], info['month'] = unescape(h.group(1)), h.group(2)
        for key in ('会话：', '我：', '群人数：'):
            if l.startswith(key):
                info[key[:-1]] = l[len(key):].strip()
    return info if 'month' in info and '会话' in info else None


def first_lines(path, n=12):
    out = []
    with open(path, encoding='utf-8-sig', errors='replace') as fh:
        for _ in range(n):
            line = fh.readline()
            if not line:
                break
            out.append(line.rstrip('\r\n'))
    return out


def chat_of(path):
    """Archive-relative chat folder: the parent of the folder holding the volume."""
    return os.path.relpath(os.path.dirname(os.path.dirname(path)), ROOT).replace(os.sep, '/')


def chat_key(pattern):
    p = pattern.replace('\\', '/').rstrip('/').lower()
    root = ROOT.replace('\\', '/').lower() + '/'
    return p[len(root):] if p.startswith(root) else p


def volumes(roots=None, first=False):
    """Chat volumes under roots as (path, header info): files whose header has an H1 with a month
    and a 会话： line. Header lines are taken from rg, so files are not opened."""
    lines = defaultdict(list)
    out = []

    def flush(path):
        info = header(lines.pop(path))
        if info:
            out.append((path, info))

    current = None
    for path, n, text in rg_stream(['-m', '4', '-e', '^# ', '-e', '^会话：', '-e', '^我：', '-e', '^群人数：'], roots):
        if n > 40:
            continue
        if current is not None and path != current:
            flush(current)
            if first and out:
                return out
        current = path
        lines[path].append(text)
    if current is not None:
        flush(current)
    return out


class Msg:
    __slots__ = ('path', 'line', 'anchor', 'time', 'name', 'type', 'sender', 'quote', 'mentions',
                 'body', 'extra', 'files', 'chat', 'me')

    def text(self):
        return '\n'.join([*self.body, *self.extra, *(os.path.basename(f) for _, f, _ in self.files)])


def is_end(l):
    return bool(ANCHOR.match(l)) or l.startswith('# ') or l.startswith('## ') or l.strip() == '---'


def parse_block(lines, i, path, me):
    """lines[i] is an anchor line. Returns (Msg, index where the next block may start)."""
    m = Msg()
    m.path, m.line, m.anchor = path, i + 1, ANCHOR.match(lines[i]).group(1)
    m.chat, m.me = chat_of(path), me
    m.time = m.name = m.type = m.sender = m.quote = ''
    m.mentions, m.body, m.extra, m.files = [], [], [], []
    j = i + 1
    while j < len(lines) and not is_end(lines[j]):
        l = lines[j]
        if l.startswith('### ') and not m.time:
            h = HEADING.match(l)
            if h:
                m.time, m.name, m.type = h.group(1), unescape(h.group(2)), h.group(3)
        elif l.startswith('发送者：') and not m.sender:
            for k, field in enumerate(l.split(' · ')):
                key, _, val = field.partition('：')
                if k == 0:
                    m.sender = val.strip()
                elif key == '引用':
                    m.quote = val.strip()
                elif key == '提到':
                    m.mentions = [x.strip() for x in re.split('[,，]', val) if x.strip()]
        elif l.startswith('> ') or l == '>':
            m.body.append(unescape(l[2:]))
        elif l.strip():
            k = LINK.match(l)
            if k:
                target = unquote(k.group(2).strip('<>'))
                full = os.path.normpath(os.path.join(os.path.dirname(path), target))
                if not os.path.exists(full):  # some exporters write links relative to the chat folder
                    alt = os.path.normpath(os.path.join(os.path.dirname(os.path.dirname(path)), target))
                    full = alt if os.path.exists(alt) else full
                m.files.append((k.group(1), full, k.group(3).strip()))
            else:
                m.extra.append(unescape(l.strip()))
        j += 1
    if m.body and m.sender and m.body[0] == m.sender + ':':
        m.body.pop(0)  # some exporters keep a "<sender id>:" transport line in group bodies
    return m, j


def blocks_at(path, line_nos):
    lines = read_lines(path)
    info = header(lines)
    if not info:
        return []
    starts = []
    for n in sorted(set(line_nos)):
        k = n - 1
        while k >= 0 and not ANCHOR.match(lines[k]):
            if lines[k].startswith('# ') or lines[k].startswith('## ') or lines[k].strip() == '---':
                k = -1
                break
            k -= 1
        if k >= 0 and (not starts or starts[-1] != k):
            starts.append(k)
    return [parse_block(lines, k, path, info.get('我', ''))[0] for k in dict.fromkeys(starts)]


def all_blocks(path):
    lines = read_lines(path)
    info = header(lines)
    if not info:
        return []
    out, i = [], 0
    while i < len(lines):
        if ANCHOR.match(lines[i]):
            msg, i = parse_block(lines, i, path, info.get('我', ''))
            out.append(msg)
        else:
            i += 1
    return out


def date_bounds(a):
    for d in (a.start, a.end):
        if d and not re.fullmatch(r'\d{4}(-\d{2}(-\d{2})?)?', d):
            die(f'bad date {d!r}: use YYYY, YYYY-MM or YYYY-MM-DD with zero padding.')
    return a.start or '', (a.end + ANY_MONTH) if a.end else ANY_MONTH


def sender_ok(m, want):
    if not want:
        return True
    if want == 'me':
        return m.sender == m.me
    w = want.lower()
    return m.sender.lower() == w or m.name.lower() == w


def chat_roots(chat):
    dirs = sorted({os.path.dirname(p) for p in md_files() if chat in chat_of(p).lower()})
    if not dirs:
        die('no chat folder path contains ' + chat)
    roots = []  # drop folders inside another root, or rg scans them twice
    for d in dirs:
        if not roots or not d.startswith(roots[-1] + os.sep):
            roots.append(d)
    return roots if len(roots) <= 300 else [os.path.commonpath(roots)]


def select(a):
    terms = [t for t in a.terms if t]
    lo, hi = date_bounds(a)
    chat = chat_key(a.chat) if a.chat else None
    roots = chat_roots(chat) if chat else None
    if terms:
        pool = terms if a.any else [max(terms, key=len)]
        pats = {tolerant(t) for t in pool} | {rg_escape(quote(t)) for t in pool if quote(t) != t}
        rg_args = ['-i'] + [x for p in sorted(pats) for x in ('-e', p)]
    elif chat:
        rg_args = ['-e', r'^<a id="msg-']
    elif a.sender:
        sid = self_id() if a.sender == 'me' else a.sender
        rg_args = ['-i', '-e', '^发送者：' + rg_escape(sid) + '(?: · |$)',
                   '-e', r'^### \S+ \S+ · ' + tolerant(a.sender) + r' · [^·]+$']
    else:
        die('give search terms, --chat, or --sender.')
    rg_args += ['-e', r'^# .+ · \d{4}-\d{2}']   # H1 of each file, to skip out-of-range months unread
    hits, month = defaultdict(list), {}
    for path, n, text in rg_stream(rg_args, roots):
        if path not in month:
            h = H1.match(text) if text.startswith('# ') else None
            month[path] = h.group(2) if h else ''
            if h:
                continue
        if chat and chat not in chat_of(path).lower():
            continue
        mo = month[path]
        if mo and not (lo[:7] <= mo <= hi):
            continue
        hits[path].append(n)
    needles = [t.lower() for t in terms]

    def work(item):
        res = []
        for m in blocks_at(*item):
            if not m.time or not (lo <= m.time <= hi) or not sender_ok(m, a.sender):
                continue
            if a.type and m.type != a.type:
                continue
            if needles:
                text = m.text().lower()
                found = [t in text for t in needles]
                if not (any(found) if a.any else all(found)):
                    continue
            res.append(m)
        return res

    out = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for res in ex.map(work, hits.items()):
            out.extend(res)
    return out


def self_id():
    for path, info in volumes(first=True):
        if info.get('我'):
            return info['我']
    die('no volume header has a 我： line.')


def clip(s, width):
    return s if width <= 0 or len(s) <= width else s[:width] + f'…(+{len(s) - width} chars)'


def show(m, width, mark=''):
    who = m.name or '(system)'
    if m.sender:
        who += f' <{m.sender}>' + (' [me]' if m.sender == m.me else '')
    extra = (f' | quotes {m.quote}' if m.quote else '') + (f' | @{",".join(m.mentions)}' if m.mentions else '')
    print(f'{mark}[{m.time}] {m.chat} | {who} | {m.type}{extra}')
    body = '\n'.join(m.body).strip()
    if body:
        print('  ' + clip(body, width).replace('\n', '\n  '))
    for line in m.extra:
        print('  + ' + clip(line, width))
    for label, f, note in m.files:
        print(f'  file: {f}' + (f' {note}' if note else ''))
    print(f'  @ {m.path}:{m.line}#{m.anchor}')


def cmd_find(a):
    msgs = select(a)
    msgs.sort(key=lambda m: (m.time, m.path, m.line), reverse=not a.oldest)
    years = Counter(m.time[:4] for m in msgs)
    chats = Counter(m.chat for m in msgs)
    print(f'{len(msgs)} messages in {len(chats)} chats; by year: '
          + ' '.join(f'{y}:{n}' for y, n in sorted(years.items())))
    if len(chats) > 1:
        print('top chats: ' + '; '.join(f'{c} ({n})' for c, n in chats.most_common(8)))
    shown = msgs[a.offset:a.offset + a.limit]
    print(f'showing {a.offset + 1}-{a.offset + len(shown)}, {"oldest" if a.oldest else "newest"} first\n')
    for m in shown:
        show(m, a.width)
        print()


def cmd_count(a):
    msgs = select(a)
    key = {'month': lambda m: m.time[:7], 'year': lambda m: m.time[:4], 'day': lambda m: m.time[:10],
           'chat': lambda m: m.chat, 'type': lambda m: m.type,
           'chatday': lambda m: f'{m.time[:10]} {m.chat}'}.get(a.by)
    if a.by == 'sender':  # one row per sender ID, labelled with its most frequent display name
        names = defaultdict(Counter)
        for m in msgs:
            names[m.sender][m.name] += 1
        key = lambda m: f'<{m.sender}> {names[m.sender].most_common(1)[0][0] or "(system)"}'
    c = Counter(key(m) for m in msgs)
    print(f'{len(msgs)} messages' + (f' on {len(c)} (day, chat) pairs' if a.by == 'chatday' else ''))
    rows = sorted(c.items()) if a.by in ('month', 'year', 'day', 'chatday') else c.most_common()
    for k, n in rows:
        print(f'{n:>8}  {k}')


def locate(ref):
    """Accept msg-anchor, path#anchor, or path:line; return (volume path, anchor)."""
    m = re.search(r'(msg-[A-Za-z0-9]+)$', ref.split('#')[-1]) if '#' in ref or ref.startswith('msg-') else None
    if not m and re.search(r':\d+$', ref):
        path, line = ref.rsplit(':', 1)
        path = os.path.normpath(path if os.path.isabs(path) else os.path.join(ROOT, path))
        if not os.path.isfile(path):
            die('no such file: ' + path)
        lines = read_lines(path)
        k = min(int(line), len(lines)) - 1
        while k >= 0 and not ANCHOR.match(lines[k]):
            k -= 1
        if k < 0:
            die('no message at that line.')
        return path, ANCHOR.match(lines[k]).group(1)
    if not m:
        die('reference must be msg-<id>, path#msg-<id>, or path:line.')
    if '#' in ref:
        path = unquote(ref.split('#')[0].strip('<>'))
        path = os.path.normpath(path if os.path.isabs(path) else os.path.join(ROOT, path))
        if os.path.isfile(path) and path.startswith(ROOT + os.sep):
            return path, m.group(1)
    found = {p for p, _, _ in rg_stream(['-F', '-e', f'<a id="{m.group(1)}"></a>'])}
    if not found:
        die('anchor not found in any volume.')
    return sorted(found)[0], m.group(1)


def md_label(s):
    return s.replace('\\', '\\\\').replace('[', '\\[').replace(']', '\\]')


def cmd_link(a):
    """One Markdown link per anchor: time, sender name and chat, pointing at volume#anchor."""
    ids = []
    for ref in a.refs:
        m = re.search(r'(msg-[A-Za-z0-9]+)', ref)
        if not m:
            die(f'not an anchor: {ref!r}')
        ids.append(m.group(1))
    ids = list(dict.fromkeys(ids))
    want, found = set(ids), {}
    args_ = ['-F']
    for i in ids:
        args_ += ['-e', f'<a id="{i}"></a>']
    for path, n, text in rg_stream(args_):
        m = ANCHOR.match(text)
        if m and m.group(1) in want:
            found.setdefault(m.group(1), []).append((path, n))
    cache = {}
    for i in ids:
        if i not in found:
            print(f'{i}  (not found)')
            continue
        path, n = sorted(found[i])[0]
        if path not in cache:
            lines = read_lines(path)
            cache[path] = (lines, header(lines))
        lines, info = cache[path]
        if not info:
            print(f'{i}  (not in a chat volume)')
            continue
        msg = parse_block(lines, n - 1, path, info.get('我', ''))[0]
        group = '群人数' in info or info.get('会话', '').endswith('@chatroom')
        where = info['name'] if group else f'与{info["name"]}的私聊'
        label = f'{msg.time[:16]} · {msg.name or "系统消息"} · {where}'
        uri = pathlib.Path(os.path.abspath(path)).as_uri() + '#' + i
        print(f'{i}  [{md_label(label)}]({uri})  ｜ {flat(msg, 60)}')


def cmd_context(a):
    path, anchor = locate(a.ref)
    info = header(first_lines(path))
    if not info:
        die('not a chat volume: ' + path)
    folder = os.path.dirname(path)
    vols = []
    for f in os.listdir(folder):
        p = os.path.join(folder, f)
        if f.endswith('.md') and os.path.isfile(p):
            h = header(first_lines(p))
            if h:
                vols.append((h['month'], p))
    months = sorted({mo for mo, _ in vols})
    i = months.index(info['month'])
    near = set(months[max(0, i - 1):i + 2])
    msgs = [m for mo, p in vols if mo in near for m in all_blocks(p)]
    msgs.sort(key=lambda m: (m.time, m.path, m.line))
    k = next((n for n, m in enumerate(msgs) if m.anchor == anchor), None)
    if k is None:
        die(f'{anchor} is not in {path}; pass the bare anchor to search all volumes.')
    lo, hi = max(0, k - a.before), min(len(msgs), k + a.after + 1)
    if lo == 0 and months[0] in near:
        print('(start of this chat in the archive)\n')
    for n in range(lo, hi):
        show(msgs[n], a.width, '>> ' if n == k else '')
        print()
    if hi == len(msgs) and months[-1] in near:
        print('(end of this chat in the archive)')


def cmd_who(a):
    pat = r'^### \S+ \S+ · .*' + tolerant(a.name) + r'.* · [^·]+$'
    people, pending = {}, {}
    for path, n, text in rg_stream(['-i', '-A', '3', '-e', pat]):
        h = HEADING.match(text)
        if h and a.name.lower() in unescape(h.group(2)).lower():
            pending[path] = (unescape(h.group(2)), h.group(1))
        elif text.startswith('发送者：') and path in pending:
            name, t = pending.pop(path)
            sid = text.split(' · ')[0][4:].strip()
            p = people.setdefault(sid, {'names': Counter(), 'chats': Counter(), 'first': t, 'last': t})
            p['names'][name] += 1
            p['chats'][chat_of(path)] += 1
            p['first'], p['last'] = min(p['first'], t), max(p['last'], t)
    if not people:
        print('no sender display name contains that text.')
    for sid, p in sorted(people.items(), key=lambda kv: -sum(kv[1]['chats'].values())):
        total = sum(p['chats'].values())
        print(f'<{sid}>  {total} messages sent under a matching name  {p["first"][:10]} – {p["last"][:10]}')
        print('  names: ' + '; '.join(f'{k} ({v})' for k, v in p['names'].most_common(6)))
        print('  chats: ' + '; '.join(f'{k} ({v})' for k, v in p['chats'].most_common(a.top)))


def cmd_chats(a):
    pat = chat_key(a.pattern) if a.pattern else ''
    by_chat = defaultdict(list)
    for path, info in volumes():
        if pat in chat_of(path).lower():
            by_chat[chat_of(path)].append((info['month'], path, info))
    dirs = sorted({os.path.dirname(p) for v in by_chat.values() for _, p, _ in v})
    counts = rg_counts(r'^<a id="msg-', dirs if len(dirs) <= 300 else None) if dirs else {}
    for chat in sorted(by_chat):
        vols = sorted(by_chat[chat])
        info = vols[-1][2]
        total = sum(counts.get(p, 0) for _, p, _ in vols)
        print(f'{chat} | {info["会话"]} | {total:,} msgs | {vols[0][0]} – {vols[-1][0]} | {len(vols)} volumes'
              + (f' | members {info["群人数"]}' if info.get('群人数') else ''))
    print(f'{len(by_chat)} chats')


def cmd_activity(a):
    """Per chat: how many messages one sender wrote, against the chat's total, with first/last dates."""
    lo, hi = date_bounds(a)
    sid = self_id() if a.sender in (None, 'me') else a.sender
    chat = chat_key(a.chat) if a.chat else None
    roots = chat_roots(chat) if chat else None
    vols = {p: info for p, info in volumes(roots) if not chat or chat in chat_of(p).lower()}
    pat = '^发送者：' + rg_escape(sid) + '(?: · |$)'
    per = defaultdict(lambda: {'n': 0, 'first': '', 'last': ''})
    names, heading = Counter(), {}
    for path, _, text in rg_stream(['-B', '3', '-e', pat], roots):
        if text.startswith('### '):
            heading[path] = text
            continue
        if path not in vols or not text.startswith('发送者：'):
            continue
        h = HEADING.match(heading.pop(path, ''))
        if not h or not (lo <= h.group(1) <= hi):
            continue
        p = per[chat_of(path)]
        p['n'] += 1
        p['first'] = min(p['first'] or h.group(1), h.group(1))
        p['last'] = max(p['last'], h.group(1))
        names[unescape(h.group(2))] += 1
    in_range = [p for p, info in vols.items() if lo[:7] <= info['month'] <= hi]
    dirs = sorted({os.path.dirname(p) for p in in_range if chat_of(p) in per})
    counts = rg_counts(r'^<a id="msg-', dirs if len(dirs) <= 300 else roots) if dirs else {}
    total, info_of = Counter(), {}
    for p in in_range:
        c = chat_of(p)
        if c in per:
            total[c] += counts.get(p, 0)
            if info_of.get(c, ('',))[0] <= vols[p]['month']:
                info_of[c] = (vols[p]['month'], vols[p])
    rows = sorted(((c, v) for c, v in per.items() if v['n'] >= a.min), key=lambda kv: -kv[1]['n'])
    n_all = sum(v['n'] for v in per.values())
    span = (min(v['first'] for v in per.values())[:10], max(v['last'] for v in per.values())[:10]) if per else ('', '')
    print(f'<{sid}> {n_all:,} messages in {len(per)} chats, {span[0]} – {span[1]}; '
          f'{len(rows)} chats with >= {a.min}; showing {min(a.limit, len(rows))}')
    if names:
        print('names used: ' + '; '.join(f'{k} ({v})' for k, v in names.most_common(6)))
    print('sender msgs | chat total (in-range months) | first – last | chat | 会话 | members')
    for c, v in rows[:a.limit]:
        info = info_of.get(c, ('', {}))[1]
        private = ' | private chat with this sender' if info.get('会话') == sid else ''
        print(f'{v["n"]:>7,} | {total[c]:>9,} | {v["first"][:10]} – {v["last"][:10]} | {c} | '
              f'{info.get("会话", "?")}' + (f' | {info["群人数"]}' if info.get('群人数') else '') + private)


def flat(m, width):
    parts = [' / '.join(x for x in m.body if x.strip()), *m.extra]
    parts += [os.path.basename(f) for _, f, _ in m.files]
    return clip(' | '.join(x for x in parts if x).replace('\n', ' '), width)


def cmd_digest(a):
    """One line per message, oldest first, grouped by day, paged."""
    msgs = sorted(select(a), key=lambda m: (m.time, m.path, m.line))
    chats = {m.chat for m in msgs}
    pages = max(1, -(-len(msgs) // a.size))
    if not 1 <= a.page <= pages:
        die(f'page {a.page} out of range 1–{pages}.')
    shown = msgs[(a.page - 1) * a.size:a.page * a.size]
    print(f'{len(msgs):,} messages in {len(chats)} chats; page {a.page}/{pages}, {a.size} per page')
    day = path = None
    for m in shown:
        if m.path != path:
            path = m.path
            print(f'\n@ {path}')
        if m.time[:10] != day:
            day = m.time[:10]
            print(f'## {day}')
        who = (m.name or '(system)') + (' [me]' if m.sender and m.sender == m.me else '')
        tag = '' if m.type == '文本' else f' [{m.type}]'
        where = f'{m.chat} | ' if len(chats) > 1 else ''
        reply = f' (re {m.quote})' if m.quote else ''
        print(f'{m.time[11:16]} {where}{who}{tag}{reply}: {flat(m, a.width)} #{m.anchor}')


def cmd_files(a):
    """Attachment links in matching messages."""
    name = (a.name or '').lower()
    if name and not (a.terms or a.chat or a.sender):
        a.terms = [a.name]
    rows = [(m, label, f, note) for m in select(a) for label, f, note in m.files
            if name in os.path.basename(f).lower()]
    rows.sort(key=lambda r: (r[0].time, r[0].path, r[0].line), reverse=not a.oldest)
    exts = Counter(os.path.splitext(f)[1].lower() or '(none)' for _, _, f, _ in rows)
    print(f'{len(rows)} files; by extension: ' + ' '.join(f'{k}:{v}' for k, v in exts.most_common(12)))
    for m, label, f, note in rows[a.offset:a.offset + a.limit]:
        who = (m.name or '(system)') + (f' <{m.sender}>' if m.sender else '') + (' [me]' if m.sender == m.me else '')
        state = '' if os.path.isfile(f) else ' (missing)'
        print(f'[{m.time}] {m.chat} | {who} | {label}{state}\n  file: {f}' + (f' {note}' if note else ''))
        print(f'  @ {m.path}:{m.line}#{m.anchor}')


MAX_MEMBER = 64 * 1024 * 1024


def xml_of(z, name):
    import xml.etree.ElementTree as ET
    info = z.getinfo(name)
    if info.file_size > MAX_MEMBER:
        die(f'{name} inside the file is too large to extract.')
    data = z.read(info)
    if data[:2] in (b'\xff\xfe', b'\xfe\xff') or b'\0' in data[:200]:
        die(f'{name} is not UTF-8 XML; refusing to parse it.')
    if b'<!DOCTYPE' in data or b'<!ENTITY' in data:
        die(f'{name} declares a DTD; refusing to parse it.')
    return ET.fromstring(data)


def local(tag):
    return tag.rsplit('}', 1)[-1]


def paragraphs(root, para, text):
    out = []
    for p in root.iter():
        if local(p.tag) == para:
            out.append(''.join(t.text or '' for t in p.iter() if local(t.tag) == text))
    return out


def extract_text(path):
    import zipfile
    ext = os.path.splitext(path)[1].lower()
    if ext in ('.txt', '.csv', '.tsv', '.md', '.json', '.log', '.html', '.htm', '.xml'):
        if os.path.getsize(path) > MAX_MEMBER:
            die('file too large to extract.')
        with open(path, 'rb') as fh:
            raw = fh.read()
        for enc in ('utf-8-sig', 'gb18030'):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                pass
        return raw.decode('utf-8', 'replace')
    if ext not in ('.docx', '.pptx', '.xlsx', '.docm', '.pptm', '.xlsm'):
        die(f'cannot extract {ext or "this file"}: supported are docx, pptx, xlsx and plain-text formats; '
            'open PDFs and images directly.')
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        die('not a valid Office Open XML file (old binary .doc/.ppt/.xls are not supported).')
    with z:
        names = z.namelist()
        if ext.startswith('.doc'):
            parts = [n for n in names if re.fullmatch(r'word/(document|footnotes|endnotes)\d*\.xml', n)]
            return '\n'.join(t for n in parts for t in paragraphs(xml_of(z, n), 'p', 't'))
        if ext.startswith('.ppt'):
            num = lambda n: int(re.search(r'(\d+)\.xml$', n).group(1))
            out = []
            for n in sorted((n for n in names if re.fullmatch(r'ppt/slides/slide\d+\.xml', n)), key=num):
                out.append(f'--- slide {num(n)} ---')
                out += [t for t in paragraphs(xml_of(z, n), 'p', 't') if t.strip()]
                note = f'ppt/notesSlides/notesSlide{num(n)}.xml'
                if note in names:
                    texts = [t for t in paragraphs(xml_of(z, note), 'p', 't') if t.strip() and not t.strip().isdigit()]
                    if texts:
                        out += ['[notes]'] + texts
            return '\n'.join(out)
        shared = []
        if 'xl/sharedStrings.xml' in names:
            for si in xml_of(z, 'xl/sharedStrings.xml'):
                shared.append(''.join(t.text or '' for t in si.iter() if local(t.tag) == 't'))
        rel = {}
        if 'xl/_rels/workbook.xml.rels' in names:
            for r in xml_of(z, 'xl/_rels/workbook.xml.rels'):
                rel[r.get('Id')] = 'xl/' + r.get('Target', '').lstrip('/').removeprefix('xl/')
        out = []
        for s in (e for e in xml_of(z, 'xl/workbook.xml').iter() if local(e.tag) == 'sheet'):
            rid = next((v for k, v in s.attrib.items() if local(k) == 'id'), None)
            target = rel.get(rid)
            if target not in names:
                continue
            out.append(f'--- sheet {s.get("name")} ---')
            for row in (e for e in xml_of(z, target).iter() if local(e.tag) == 'row'):
                cells = []
                for c in (e for e in row if local(e.tag) == 'c'):
                    v = next((e.text for e in c if local(e.tag) == 'v'), None)
                    if c.get('t') == 's' and v is not None and v.isdigit() and int(v) < len(shared):
                        v = shared[int(v)]
                    elif c.get('t') == 'inlineStr':
                        v = ''.join(t.text or '' for t in c.iter() if local(t.tag) == 't')
                    cells.append(v or '')
                while cells and not cells[-1]:
                    cells.pop()
                if cells:
                    out.append('\t'.join(cells))
        return '\n'.join(out)


def cmd_extract(a):
    path = os.path.normpath(a.path if os.path.isabs(a.path) else os.path.join(ROOT, a.path))
    if not os.path.isfile(path):
        die('no such file: ' + path)
    import zipfile
    import zlib
    import xml.etree.ElementTree as ET
    try:
        text = extract_text(path)
    except (ET.ParseError, zipfile.BadZipFile, zlib.error, KeyError, OSError, ValueError) as exc:
        die(f'cannot extract this file ({type(exc).__name__}): it may be damaged or not a real Office file.')
    pages = max(1, -(-len(text) // a.size))
    if not 1 <= a.page <= pages:
        die(f'page {a.page} out of range 1–{pages}.')
    print(f'{path}\n{len(text):,} chars; page {a.page}/{pages}, {a.size:,} chars per page\n')
    print(text[(a.page - 1) * a.size:a.page * a.size])


def main():
    global ROOT
    p = argparse.ArgumentParser(description='Read-only WeChat archive search.')
    p.add_argument('--archive', help='archive root (default: $WECHAT_ARCHIVE or config.json)')
    sub = p.add_subparsers(dest='cmd', required=True)

    def filters(sp):
        sp.add_argument('terms', nargs='*', help='substrings; all must occur in one message (case-insensitive)')
        sp.add_argument('--any', action='store_true', help='match messages containing any term')
        sp.add_argument('--chat', help='substring of the chat folder path (name, category, or full path)')
        sp.add_argument('--sender', help='sender ID, exact display name, or "me"')
        sp.add_argument('--type', help='message type exactly as in headings')
        sp.add_argument('--from', dest='start', help='YYYY, YYYY-MM or YYYY-MM-DD (inclusive)')
        sp.add_argument('--to', dest='end', help='YYYY, YYYY-MM or YYYY-MM-DD (inclusive)')

    f = sub.add_parser('find', help='list matching messages')
    filters(f)
    f.add_argument('--oldest', action='store_true', help='oldest first (default newest first)')
    f.add_argument('--limit', type=int, default=30)
    f.add_argument('--offset', type=int, default=0)
    f.add_argument('--width', type=int, default=400, help='max body chars per message, 0 = full')
    c = sub.add_parser('count', help='count matching messages')
    filters(c)
    c.add_argument('--by', choices=['day', 'month', 'year', 'chat', 'sender', 'type', 'chatday'],
                   default='month')
    d = sub.add_parser('digest', help='one line per matching message, oldest first, by day, paged')
    filters(d)
    d.add_argument('--page', type=int, default=1)
    d.add_argument('--size', type=int, default=200, help='messages per page')
    d.add_argument('--width', type=int, default=160, help='max chars per line, 0 = full')
    fl = sub.add_parser('files', help='attachment links in matching messages')
    filters(fl)
    fl.add_argument('--name', help='substring of the attached file name')
    fl.add_argument('--oldest', action='store_true')
    fl.add_argument('--limit', type=int, default=50)
    fl.add_argument('--offset', type=int, default=0)
    v = sub.add_parser('activity', help="one sender's messages per chat, against each chat's total")
    v.add_argument('--sender', default='me', help='sender ID or "me" (default)')
    v.add_argument('--chat', help='substring of the chat folder path')
    v.add_argument('--from', dest='start')
    v.add_argument('--to', dest='end')
    v.add_argument('--min', type=int, default=1, help='hide chats with fewer messages by the sender')
    v.add_argument('--limit', type=int, default=60)
    e = sub.add_parser('extract', help='plain text of a docx/pptx/xlsx or text attachment, paged')
    e.add_argument('path', help='absolute path, or relative to the archive root')
    e.add_argument('--page', type=int, default=1)
    e.add_argument('--size', type=int, default=20000, help='chars per page')
    x = sub.add_parser('context', help='messages around one message (crosses volume boundaries)')
    x.add_argument('ref', help='msg-<id>, path#msg-<id>, or path:line')
    x.add_argument('-B', '--before', type=int, default=10)
    x.add_argument('-A', '--after', type=int, default=10)
    x.add_argument('--width', type=int, default=0)
    w = sub.add_parser('who', help='sender IDs whose display name contains NAME')
    w.add_argument('name')
    w.add_argument('--top', type=int, default=8)
    k = sub.add_parser('link', help='Markdown links (time · sender · chat) for anchors, for reports; each line ends with the start of the message')
    k.add_argument('refs', nargs='+', help='msg-<id> or anything containing one')
    h = sub.add_parser('chats', help='list chats (optionally filtered by folder path fragment)')
    h.add_argument('pattern', nargs='?')
    a, extra = p.parse_known_args()
    if extra:  # search terms given after an option, e.g. `find a --any b`
        if hasattr(a, 'terms') and not any(x.startswith('-') for x in extra):
            a.terms += extra
        else:
            p.error('unrecognized arguments: ' + ' '.join(extra))
    if (sys.stdout.encoding or '').lower() != 'utf-8':
        sys.stdout.reconfigure(encoding='utf-8')
    for opt in ('size', 'page', 'limit'):
        if getattr(a, opt, 1) < 1:
            die(f'--{opt} must be at least 1.')
    ROOT = resolve_root(a.archive)
    if not os.path.isdir(ROOT):
        die(f'archive not found: {ROOT}')
    {'find': cmd_find, 'count': cmd_count, 'context': cmd_context, 'who': cmd_who, 'chats': cmd_chats,
     'digest': cmd_digest, 'files': cmd_files, 'activity': cmd_activity, 'extract': cmd_extract,
     'link': cmd_link}[a.cmd](a)


if __name__ == '__main__':
    main()
