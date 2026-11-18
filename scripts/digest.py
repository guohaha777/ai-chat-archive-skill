#!/usr/bin/env python3
"""Locate what is worth reading, so the model reads a small digest instead of everything.

  build <snapshot> <work> [--budget N] [--topic NAME]   write digest/<话题>/ for chosen topics
  show  <work> <话题> <u12> [u30-u33 ...]               print whole units when an excerpt is not enough
  find  <work> <关键词> [--topic NAME]                  where a word occurs, with a little context
  gaps  <work> <vault>                                  what was left out and what the notes lack
  more  <work> [--tier high|all] [--topic] [--keyword]  a second helping from what was left out

For every chosen topic `build` writes:

  精选.md        the exchanges that carry the most reusable content, shortened, in time
                 order, with a one-line trace of every conversation so nothing is invisible
  原文资产.md     term candidates, workflow candidates, commands, instructions written for
                 an AI and error messages, pulled out word for word and de-duplicated
  units.json     every unit in full, for `show`, `find` and the final source check
  selection.json every exchange with its score, kind and whether it was picked
  assets.json    the extracted assets as data, for `gaps`

Scoring is simple and visible. Each exchange is scored per kind of reusable content
(CATEGORIES below); weights and extra keywords come from <work>/focus.json when present.
It ranks; it does not judge truth. Any unit can be opened in full with `show`.
"""
from __future__ import annotations
import argparse
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from make_batches import HEADER, blocks, drop_repeats, expand_sessions, load, trim_long_code

ASKER_ROLES = {'user', 'human'}
FENCE = re.compile(r'^\s{0,3}(`{3,}|~{3,})\s*([^\s`]*)')
CJK = '一-鿿'

# ---------------------------------------------------------------------------------------
# What counts as reusable. One entry per kind: a display name, a default weight, words in
# the person's question that ask for it, and words in the answer that deliver it.
# Structure (a command block, a numbered procedure, a glossary-shaped list) is detected
# separately in `structure()` because it does not depend on vocabulary or subject.
# ---------------------------------------------------------------------------------------
CATEGORIES = {
    'term': ('术语与概念', 3,
             r'什么是|是什么意思|什么意思|啥意思|怎么理解|是啥|叫什么|怎么称呼|怎么叫|专业(?:术语|名词|说法|叫法|词汇?|表达)|术语|名词解释|'
             r'有什么(?:区别|不同)|的区别|用什么词|怎么描述|怎么形容|关键词|这种(?:风格|效果|手法|现象|感觉).{0,8}叫|学名|概念',
             r'是指|指的是|全称|缩写|简称|又称|也叫|俗称|称为|称作|叫做|被称|术语|定义[是为：:]|可以理解为|通俗地?说|简单来说|'
             r'相当于|区别(?:在于|是)|的意思是|英文(?:是|叫|名|为)|源[自于]|这个词|一词|专业(?:说法|叫法|术语|表达)'),
    'code': ('可复用的代码与命令', 3,
             r'命令|怎么(?:运行|启动|安装|部署|上传|推送|提交|回滚|配置|打包|发布|备份|恢复|连接)|如何(?:运行|启动|安装|部署|配置)|'
             r'脚本|配置文件|环境变量|代码(?:怎么|如何)|本地(?:跑|运行|启动)',
             r'运行|执行|终端|命令行|安装|启动|部署|推送|提交|回滚|环境变量|配置文件|复制(?:下面|以下|这[段条行])|粘贴|一键'),
    'method': ('方法与流程', 3,
               r'流程|步骤|怎么做|如何做|该怎么|方法|工作流|计划|规划|路线|顺序|先做什么|从哪(?:里|儿)?开始|框架|模板|清单|规范|'
               r'怎么(?:推进|安排|拆|组织|管理|协作|分工)|SOP|最佳实践',
               r'第[一二三四五六七八九十\d]步|步骤|流程|工作流|阶段|里程碑|清单|checklist|模板|框架|方法论|SOP|规范|约定|节奏|分工|'
               r'先.{1,30}再.{1,30}|优先级|路线图|闭环|验收标准?|最佳实践|标准做法|套路|迭代'),
    'prompt': ('给 AI 的指令与协作方式', 2,
               r'提示词|prompt|指令|怎么(?:跟|和|对|让|给)\s*(?:ai|cursor|claude|gpt|gemini|codex|模型)|'
               r'发给\s*(?:ai|cursor|claude|codex)|帮我写.{0,10}(?:指令|提示)',
               r'提示词|prompt|角色设定|上下文|约束|发给|复制给|粘贴给|交给\s*(?:cursor|claude|codex|ai)|只读|不要修改|验收|'
               r'(?:ask|plan|agent)\s*模式'),
    'decision': ('决策与取舍', 2,
                 r'选哪个|哪个(?:好|更|合适)|还是.{1,20}[？?]|要不要|值不值得|该不该|怎么选|对比|推荐哪|有必要|是否(?:应该|需要|值得)',
                 r'决定|最终(?:方案|选择|采用|确定)|选择了|就用|不再|放弃|取舍|权衡|折中|结论[是：:]|推荐(?:方案|使用|用)|'
                 r'建议(?:采用|使用|选|用)|代价|缺点|优缺点|风险|副作用|不可逆|方案[一二三ABC]|相比之下|更适合'),
    'issue': ('问题与排查', 2,
              r'报错|失败|不行|没反应|打不开|不显示|不生效|为什么.{0,20}(?:不|没|无法)|怎么回事|出问题|出错|异常|卡住|崩|bug',
              r'根因|根本原因|原因是|问题出在|问题在于|之所以|真正的问题|解决方[法案]|修复|改成|改为|换成|处理办法|正确做法|'
              r'验证|复测|已经(?:正常|成功|好了)|生效|报错|错误信息|排查|定位|日志|'
              r'\b(?:error|exception|failed|denied|refused|timeout|not found|undefined|cannot|unable)\b|\b[45]\d\d\b'),
    'lesson': ('经验与复盘', 2,
               r'复盘|总结一下|回顾|学到|经验|教训|下次|以后(?:怎么|该)',
               r'经验|教训|踩坑|以后|下次|记住|务必|千万|关键[是在点]|核心[是在]|本质|总结|小结|复盘|综上|要点|启示|反思|'
               r'一句话|总的来说|最重要的'),
    'reflect': ('个人判断与反思', 1,
                r'我(?:发现|意识到|觉得|感觉|担心|害怕|焦虑|纠结|总是|一直|其实|适合|应该).{0,40}|内耗|迷茫|我的(?:问题|优势|短板|目标|方向)',
                r'模式|倾向|假设|证据|反例|盲区|动机|你的(?:优势|问题|模式|短板)|诚实|定位|方向感'),
}
CAT_RE = {key: (re.compile(ask, re.I), re.compile(answer, re.I)) for key, (_, _, ask, answer) in CATEGORIES.items()}
# What the person says when the previous answer missed: the most reliable sign that the
# next answer contains a correction worth keeping.
PUSHBACK = re.compile(r'不对|不是这样|还是不行|还是报错|没有生效|没用|为什么还|依然|仍然|又出现|别[再给]|我的意思是|你理解错|'
                      r'你搞错|不是我要的|重新|跑偏|其实我')
COMMAND_LANGS = {'bash', 'sh', 'shell', 'zsh', 'console', 'powershell', 'ps1', 'cmd', 'bat', 'sql', 'psql',
                 'dockerfile', 'nginx', 'terminal'}
CONFIG_LANGS = {'json', 'yaml', 'yml', 'toml', 'ini', 'env', 'dotenv', 'conf', 'xml', 'css', 'scss', 'js', 'jsx',
                'ts', 'tsx', 'javascript', 'typescript', 'python', 'py', 'html', 'swift', 'glsl', 'wxml', 'wxss'}
COMMAND_HEAD = re.compile(
    r'^\s*(?:\$\s+|#\s*)?(?:sudo\s+)?(git|npm|npx|pnpm|yarn|node|docker|docker-compose|ssh|scp|curl|wget|pip3?|python3?|'
    r'cd|ls|cat|cp|mv|rm|mkdir|chmod|chown|find|grep|tail|head|systemctl|journalctl|nginx|certbot|psql|pg_dump|'
    r'pg_restore|supabase|vercel|brew|apt|apt-get|yum|ufw|kill|ps|netstat|lsof|export|source|echo|tar|unzip|'
    r'claude|codex|ollama|ffmpeg|adb|open|nano|vim|vi|touch|make|go|cargo|uv|conda)\b')
SQL_HEAD = re.compile(r'^\s*(select|insert|update|delete|create|alter|drop|grant|revoke|with)\b', re.I)
PROMPT_CUES = re.compile(r'请(?:你|帮|先|只|不要|务必|严格)|不要(?:修改|改动|动|删除)|只(?:修改|改|读|排查|输出|做)|你是一[个名位]|你的任务|'
                         r'角色[：:]|要求[：:]|约束[：:]|输出格式|背景[：:]|目标[：:]|@[\w./\-]+|step by step|you are|do not|must', re.I)
PROMPT_LEAD = re.compile(r'指令|提示词|prompt|发给|复制给|粘贴给|告诉\s*(?:cursor|claude|codex|ai)|交给', re.I)
ERROR_LINE = re.compile(
    r'(?:\b(?:error|exception|fatal|failed|denied|refused|traceback|unable to|cannot|can\'t|not found|'
    r'no such|undefined|invalid|unauthorized|forbidden|timeout|timed out|exited with)\b|'
    r'\bE[A-Z]{3,}\b|\b(?:4\d\d|5\d\d)\s+(?:[A-Z][a-z]+|\()|报错[：:])', re.I)
IDENT = re.compile(r'`([^`\n]{2,40})`')
# Term shapes that do not depend on the subject: 中文（English）, **词**：解释, 「词」.
BILINGUAL = re.compile(rf'([{CJK}]{{2,12}})\s*[（(]\s*([A-Za-z][A-Za-z0-9 .\-/]{{2,40}})\s*[)）]')
BOLD_DEF = re.compile(r'^\s*(?:[-*+]|\d+[.、)])?\s*\*\*([^*\n]{2,28})\*\*\s*(?:[（(][^)）\n]{1,40}[)）])?\s*[：:—–-]\s*(\S.{4,})$')
ASKED_TERM = re.compile(rf'(?:什么是|啥是)\s*[“"「『]?([A-Za-z{CJK}][^，。？?\n”"」』]{{1,24}})|'
                        rf'[“"「『]?([A-Za-z{CJK}][^，。？?\n”"」』\s]{{1,24}})[”"」』]?\s*(?:是什么意思|什么意思|是啥|是什么)[？?]?')
NOT_A_TERM = re.compile(r'^(?:第.{1,3}步|步骤|方法|方案|注意|总结|小结|原因|解决|结论|优点|缺点|优势|劣势|问题|建议|提示|说明|示例|例如|'
                        r'目标|背景|现象|结果|操作|验证|核心|关键|重点|作用|用途|特点|场景|效果|含义|位置|时间|地点|名称|类型|状态|'
                        r'step|note|tip|why|how|what|对|错|是|否|好处|坏处|前提|风险|代价|经验|为什么|怎么做|做法|思路|'
                        r'[一二三四五六七八九十\d]+)[\s\d一二三四五六七八九十、.：:]*$', re.I)
# Interface labels and filler that take the shape of a term without being one.
GENERIC = re.compile(r'^(?:文件|编辑|删除|常规|项目|输入|输出|网页|状态|设置|色彩|颜色|画风|风格|脚本|首选项|导出|导入|新建|保存|打开|关闭|'
                     r'确定|取消|选项|工具|窗口|帮助|视图|图层|菜单|按钮|下一步|上一步|是谁|触发|适合|推荐|用户|功能|价格|费用|时间线|'
                     r'序列|效果|媒体|格式|预设|名称|位置|大小|类型|模式|属性|参数|添加|创建|选择|点击|运行|安装|登录|注册|搜索)$')
PRONOUN = re.compile(r'^(?:这|那|它|他|她|你|我|哪|什么|为什么|怎么|上面|下面|以上|以下|刚才|现在|所以|那么|请问|然后|还有|另外|其中|里面)')
DEFINES = re.compile(r'是指|指的是|是一种|是一个|代表|原本是|概念|术语|也就是|即[：:，]|意思是|全称|源[自于]|又称|也叫|俗称')
NUMBERED = re.compile(rf'^\s*(?:\d{{1,2}}[.、)]|第[一二三四五六七八九十\d]+步|步骤\s*\d+|[①②③④⑤⑥⑦⑧⑨])\s*\S')


def squash(text):
    return re.sub(r'\s+', ' ', text).strip()


def body_of(unit):
    # 'Prompted ' is a label some exports put in front of every question.
    return re.sub(r'^Prompted\s+', '', HEADER.sub('', unit['text'], count=1))


def cut(text, limit):
    text = text.strip()
    if len(text) <= limit:
        return text
    head = text[:limit]
    stop = max(head.rfind(c) for c in '。！？\n.；;')
    if stop > limit * 0.6:
        head = head[:stop + 1]
    return head.rstrip() + f' 〔…余 {len(text) - len(head)} 字〕'


def load_focus(work):
    """Weights and extra keywords for this person, from <work>/focus.json when present.

    {"weights": {"term": 4, "issue": 1}, "keywords": {"term": ["折纸", "构图"], "any": ["芽历"]}}
     "topics": {"芽历": ["浇水提醒", "同步", "阳台"]}}
    A weight of 0 switches a kind off. Unknown kinds are ignored. "topics" holds words
    that matter inside one topic only: its own vocabulary, which no general list can know.
    """
    weights = {key: value[1] for key, value in CATEGORIES.items()}
    keywords = {}
    path = Path(work) / 'focus.json'
    if path.is_file():
        data = json.loads(path.read_text())
        for key, value in (data.get('weights') or {}).items():
            if key in weights and isinstance(value, (int, float)) and value >= 0:
                weights[key] = value
        for key, words in (data.get('keywords') or {}).items():
            words = [str(w).strip() for w in words or [] if str(w).strip()]
            if words and (key in weights or key == 'any'):
                keywords[key] = re.compile('|'.join(re.escape(w) for w in sorted(words, key=len, reverse=True)), re.I)
        topics = {str(name): [str(w).strip() for w in words or [] if str(w).strip()]
                  for name, words in (data.get('topics') or {}).items()}
        lexicon = [str(w).strip() for w in data.get('lexicon') or [] if str(w).strip()]
        return {'weights': weights, 'keywords': keywords, 'topics': topics, 'lexicon': lexicon}
    return {'weights': weights, 'keywords': keywords, 'topics': {}, 'lexicon': []}


def focus_for(focus, topic):
    """Add a topic's own keywords (focus.json "topics") to the general ones."""
    words = focus.get('topics', {}).get(topic) or []
    if not words:
        return focus
    general = focus['keywords'].get('any')
    pattern = '|'.join(re.escape(w) for w in sorted(words, key=len, reverse=True))
    if general:
        pattern += '|' + general.pattern
    return {**focus, 'keywords': {**focus['keywords'], 'any': re.compile(pattern, re.I)}}


DEFAULT_FOCUS = {'weights': {key: value[1] for key, value in CATEGORIES.items()}, 'keywords': {}, 'topics': {}, 'lexicon': []}


def distinct(pattern, text, cap=4):
    """How many different cue words occur; repetition of one word does not add up."""
    return min(len({m.group(0).lower()[:6] for m in pattern.finditer(text)}), cap)


def is_fenced(block):
    return bool(FENCE.match(block.lstrip('\n')))


def fence_parts(block):
    """Return (lang, inner text) of a fenced block."""
    lines = block.strip('\n').splitlines()
    match = FENCE.match(lines[0]) if lines else None
    if not match:
        return '', block
    inner = lines[1:]
    if inner and FENCE.match(inner[-1]) and not FENCE.match(inner[-1]).group(2):
        inner = inner[:-1]
    return match.group(2).lower(), '\n'.join(inner)


def looks_like_commands(lang, inner):
    lines = [line for line in inner.splitlines() if line.strip()]
    if not lines or len(lines) > 40:
        return False
    if lang in COMMAND_LANGS:
        return True
    hits = sum(bool(COMMAND_HEAD.match(line) or SQL_HEAD.match(line)) for line in lines)
    return hits >= max(1, 0.5 * len(lines))


def looks_like_prompt(lang, inner, before=''):
    if not (60 <= len(inner) <= 3000) or not re.search(rf'[{CJK}]', inner):
        return 0
    if lang not in {'', 'text', 'markdown', 'md', 'plaintext', 'txt'}:
        return 0
    strength = len(PROMPT_CUES.findall(inner)) + 3 * bool(PROMPT_LEAD.search(before))
    return strength if strength >= 2 else 0


def numbered_run(block):
    """Length of the longest run of numbered steps in a block."""
    best = run = 0
    for line in block.splitlines():
        if NUMBERED.match(line):
            run += 1
            best = max(best, run)
        elif line.strip() and not line.startswith((' ', '\t')):
            run = 0
    return best


def structure(text):
    """Subject-independent signs of reusable content in one answer. Returns points per kind."""
    points = Counter()
    parts = [b for b in blocks(text) if b.strip()]
    steps = bold_defs = 0
    for index, block in enumerate(parts):
        stripped = block.strip()
        if is_fenced(stripped):
            lang, inner = fence_parts(stripped)
            before = parts[index - 1].strip()[-160:] if index else ''
            if looks_like_commands(lang, inner):
                points['code'] = max(points['code'], 6)
            elif looks_like_prompt(lang, inner, before):
                points['prompt'] = max(points['prompt'], 6)
            elif lang in CONFIG_LANGS and len(inner.splitlines()) <= 40:
                points['code'] = max(points['code'], 3)
            continue
        steps = max(steps, numbered_run(stripped))
        bold_defs += sum(bool(BOLD_DEF.match(line)) and not NOT_A_TERM.match(BOLD_DEF.match(line).group(1).strip())
                         for line in stripped.splitlines())
        if stripped.startswith('|') and re.search(r'术语|名词|概念|含义|释义|定义|term', stripped.splitlines()[0], re.I):
            points['term'] += 5
    plain = '\n'.join(b for b in parts if not is_fenced(b.strip()))
    pairs = len(set(BILINGUAL.findall(plain)))
    if pairs >= 2:
        points['term'] += min(pairs, 6)
    if bold_defs >= 3:
        points['term'] += min(bold_defs, 6)
    if steps >= 3:
        points['method'] += min(steps + 1, 7)
    if '```mermaid' in text or 'flowchart' in text:
        points['method'] += 2
    return points


def score_exchange(asks, answers, position, total, focus=None):
    """Return (score, points per kind). Points are before weighting; score is after."""
    focus = focus or DEFAULT_FOCUS
    ask_text = '\n'.join(body_of(u) for u in asks)
    answer_text = '\n'.join(body_of(u) for u in answers)
    ask_head = ask_text[:600]
    plain_answer = '\n'.join(b for b in blocks(answer_text) if not is_fenced(b.strip()))
    points = structure(answer_text)
    for key, (ask_re, answer_re) in CAT_RE.items():
        points[key] += 2 * distinct(answer_re, plain_answer)
        if ask_re.search(ask_head):
            points[key] += 4
        extra = focus['keywords'].get(key)
        if extra:
            points[key] += 3 * distinct(extra, ask_text + '\n' + answer_text)
    if 200 <= len(ask_text) <= 3000 and len(PROMPT_CUES.findall(ask_text)) >= 3:
        points['prompt'] += 5
    if PUSHBACK.search(ask_text[:400]):
        points['issue'] += 3
        points['lesson'] += 3
    score = sum(focus['weights'].get(key, 0) * value for key, value in points.items())
    if 'any' in focus['keywords']:
        score += 6 * distinct(focus['keywords']['any'], ask_text + '\n' + answer_text)
    if position == 0:
        score += 14         # how the conversation started: the goal
    if position == total - 1 and total > 1:
        score += 8          # where it ended: the outcome
    if len(answer_text) < 80 and len(ask_text) < 40:
        score -= 12         # "继续" / "好的"
    return score, {key: value for key, value in points.items() if value}


def dominant(points, focus):
    weighted = {key: focus['weights'].get(key, 0) * value for key, value in points.items()}
    best = max(weighted, key=weighted.get, default=None)
    return best if best and weighted[best] > 0 else None


def block_value(block):
    """Rough worth of one block inside an answer, used only to shorten it."""
    return sum(2 for _, answer_re in CAT_RE.values() if answer_re.search(block))


def skeleton(text, limit):
    """Shorten one answer to its load-bearing blocks, kept in their original order.

    Headings are nearly free and show the shape of the answer; blocks that define a term,
    list steps, state a cause, a decision or a lesson, or hold a short command are kept
    first; long code is cut to its top.
    """
    parts = [b for b in blocks(text) if b.strip()]
    if sum(len(b) for b in parts) <= limit:
        return text.strip()
    ranked = []
    for index, block in enumerate(parts):
        stripped = block.strip()
        score = block_value(stripped)
        size = len(stripped)
        if is_fenced(stripped):
            lang, inner = fence_parts(stripped)
            score += 6 if looks_like_commands(lang, inner) or looks_like_prompt(lang, inner) else 0
            if size > 500:
                kept = inner.splitlines()[:8]
                stripped = f'```{lang}\n' + '\n'.join(kept) + f'\n〔…共 {len(inner)} 字，已省略其余〕\n```'
                size = len(stripped)
        elif stripped.startswith('#'):
            score += 3
        elif stripped.startswith('|'):
            rows = stripped.splitlines()
            if len(rows) > 9:
                stripped = '\n'.join(rows[:9]) + f'\n〔…表格共 {len(rows)} 行〕'
                size = len(stripped)
            score += 3
        else:
            if numbered_run(stripped) >= 3:
                score += 4
            if BOLD_DEF.search(stripped) or BILINGUAL.search(stripped):
                score += 3
            if re.match(r'^\*\*[^*]+\*\*', stripped):
                score += 1
        if index == 0:
            score += 3
        if index == len(parts) - 1:
            score += 2
        if size > 600:
            stripped = cut(stripped, 600)
            size = len(stripped)
        ranked.append((score / math.sqrt(max(size, 40)), index, stripped, size))
    chosen, used = {}, 0
    for _, index, stripped, size in sorted(ranked, key=lambda r: (-r[0], r[1])):
        if used + size > limit and chosen:
            continue
        chosen[index] = stripped
        used += size
        if used >= limit:
            break
    out, last = [], -1
    for index in sorted(chosen):
        if index != last + 1:
            out.append('〔…〕')
        out.append(chosen[index])
        last = index
    if last != len(parts) - 1:
        out.append('〔…〕')
    return '\n\n'.join(out)


def exchanges(units):
    """Group a conversation into (question units, answer units) pairs."""
    pairs, asks, answers = [], [], []
    for unit in units:
        if unit.get('role') in ASKER_ROLES:
            if answers:
                pairs.append((asks, answers))
                asks, answers = [], []
            asks.append(unit)
        else:
            answers.append(unit)
    if asks or answers:
        pairs.append((asks, answers))
    return pairs


def render_exchange(asks, answers, refs, rich):
    ask_limit, answer_limit = (360, 800) if rich else (200, 400)
    lines = []
    for unit in asks:
        lines += [f"【{refs[unit['unit_id']]}｜问】", cut(body_of(unit), ask_limit), '']
    for unit in answers:
        lines += [f"【{refs[unit['unit_id']]}｜答】", skeleton(body_of(unit), answer_limit), '']
    return '\n'.join(lines)


def choose(candidates, budget, focus):
    """Pick exchanges within the budget, giving every wanted kind a share before the rest.

    Without the shares, whichever kind is wordiest in the corpus (usually debugging)
    crowds out the others. 60% of the budget is divided by weight among the kinds; the
    remaining 40% goes to whatever scores best overall.
    """
    density = lambda c: -c['score'] / math.sqrt(max(len(c['text']), 200))
    ranked = sorted(candidates, key=density)
    used = 0
    # First, the best exchange of each conversation, strongest conversations first, within a
    # quarter of the budget: a conversation whose wording matches no cue is still represented.
    best = {}
    for cand in candidates:
        if cand['score'] > 0 and (cand['cid'] not in best or cand['score'] > best[cand['cid']]['score']):
            best[cand['cid']] = cand
    for cand in sorted(best.values(), key=lambda c: -c['score']):
        if used + len(cand['text']) > 0.25 * budget and used:
            continue
        cand['picked'] = 1
        used += len(cand['text'])
    total_weight = sum(w for w in focus['weights'].values() if w > 0) or 1
    for key, weight in sorted(focus['weights'].items(), key=lambda kv: -kv[1]):
        if weight <= 0:
            continue
        quota, spent = 0.6 * budget * weight / total_weight, 0
        for cand in ranked:
            if cand['picked'] or cand['kind'] != key or cand['score'] <= 0:
                continue
            size = len(cand['text'])
            if (spent + size > quota and spent) or (used + size > budget and used):
                continue
            cand['picked'] = 1
            spent += size
            used += size
            if spent >= quota:
                break
    for cand in ranked:
        if cand['picked']:
            continue
        if cand['score'] <= 0 and used:
            break
        if used + len(cand['text']) > budget and used:
            continue
        cand['picked'] = 1
        used += len(cand['text'])
        if used >= budget:
            break
    return used


def is_code_name(term):
    """A file, field, table or component name rather than a word of the trade."""
    if not re.fullmatch(r'[\x21-\x7e]+', term):
        return False
    return bool(re.search(r'[_./\\=<>(){}\[\]@#$:;]', term) or re.search(r'[a-z][A-Z]', term)
                or re.fullmatch(r'[a-z0-9\-]+', term))


def clean_term(term):
    term = term.strip(' \t*_`"“”「」『』:：,，。.、')
    if not (2 <= len(term) <= 28) or NOT_A_TERM.match(term) or not re.search(rf'[A-Za-z{CJK}]', term):
        return None
    if re.search(r'[。！？!?，,]', term) or len(re.findall(rf'[{CJK}]', term)) > 14:
        return None
    # "状态驱动的纹理混合（Texture Blending）" -> "纹理混合（Texture Blending）": keep what follows the last particle.
    head, sep, rest = term.partition('（')
    tail = re.split(r'[的是和与在或及了将把又叫]', head)[-1].strip()
    if tail != head:
        if len(tail) < 2:
            return None
        term = tail + sep + rest
    base = tail
    if GENERIC.match(base) or NOT_A_TERM.match(base) or PRONOUN.match(term) or re.search(r'[是又的了吗呢]$', base):
        return None
    return term


# Words of the trade that matter even when nobody asked what they mean. Matching is on the
# word itself, so a term that is used a lot but never explained (hover, 弹窗) still reaches
# the candidate list. Extend it per person with "lexicon" in focus.json.
UI_LEXICON = ('hover', 'tooltip', 'modal', 'toast', 'dropdown', 'popover', 'drawer', 'skeleton', 'spinner',
              'placeholder', 'carousel', 'lightbox', 'sticky', 'viewport', 'breakpoint', 'debounce', 'throttle',
              'lazy load', 'infinite scroll', 'responsive', 'bottom sheet', 'z-index', 'glassmorphism',
              '悬停', '弹窗', '轮播', '拖拽', '吸顶', '懒加载', '骨架屏', '下拉菜单', '抽屉', '气泡', '瀑布流',
              '毛玻璃', '响应式', '无限滚动', '点击态', '选中态', '浮层', '蒙层', '锚点', '滚动条', '占位符')
# "X 是……" right after a bolded or leading term: a definition even without 是指/称为.
IS_DEF = r'^\s*(?:[-*+]\s*)?\**TERM\**\s*(?:\([^)]{0,40}\)|（[^）]{0,40}）)?\s*(?:是|指|即|就是|表示|代表|意思是|用来)\s*(\S.{3,})'


IS_LEADING = re.compile(r'^\s*(?:[-*+]\s*)?\**[A-Za-z\u4e00-\u9fff][^*，。：:]{0,20}\**\s*(?:\([^)]{0,40}\)|（[^）]{0,40}）)?\s*(?:是|指|即|就是|表示|代表|意思是|用来)')


def lexicon_in(text_lower, word):
    if re.fullmatch(r'[a-z0-9 \-]+', word):
        return len(re.findall(rf'(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])', text_lower))
    return text_lower.count(word)


def best_definition(sentences, base, loose=True):
    """The sentence that most plainly says what `base` is, or ''.

    `loose` also accepts a sentence that merely contains the word and a defining phrase;
    that is right for a term the person asked about, and too generous for a common word.
    """
    pattern = re.compile(IS_DEF.replace('TERM', re.escape(base)), re.I)
    fallback = ''
    for sentence in sentences:
        if base not in sentence.lower() or len(sentence) > 200:
            continue
        match = pattern.match(sentence)
        if match and sentence.count('、') < 2:
            return squash(sentence.replace('*', ''))[:110]
        if loose and not fallback and DEFINES.search(sentence) and sentence.count('、') < 2:
            fallback = squash(sentence.replace('*', ''))[:110]
    return fallback


def collect_assets(units, refs, lexicon=()):
    """Pull exact, reusable text out of the units."""
    commands, prompts, errors, idents = {}, {}, Counter(), Counter()
    error_ref, ident_ref, terms, flows = {}, {}, {}, {}

    def note_term(term, ref, definition='', asked=False, shape=''):
        term = clean_term(term)
        if not term:
            return
        row = terms.setdefault(term.lower(), {'term': term, 'count': 0, 'ref': ref, 'definition': '',
                                              'asked': False, 'defined': False, 'shapes': set()})
        row['count'] += 1
        if shape:
            row['shapes'].add(shape)
        row['asked'] = row['asked'] or asked
        better = bool(DEFINES.search(definition or ''))
        if definition and (not row['definition'] or (better and not row['defined'])):
            row['definition'], row['ref'] = squash(definition)[:110], ref
        row['defined'] = row['defined'] or better

    for unit in units:
        ref = refs[unit['unit_id']]
        text = body_of(unit)
        parts = [b for b in blocks(text) if b.strip()]
        asker = unit.get('role') in ASKER_ROLES
        if asker:
            for match in ASKED_TERM.finditer(text[:400]):
                note_term(match.group(1) or match.group(2), ref, asked=True)
        heading = ''
        for index, block in enumerate(parts):
            stripped = block.strip()
            if is_fenced(stripped):
                lang, inner = fence_parts(stripped)
                inner = inner.strip('\n')
                key = squash(inner)
                if not key:
                    continue
                before = parts[index - 1].strip()[-160:] if index else ''
                if not asker and looks_like_commands(lang, inner) and len(inner) <= 1500:
                    row = commands.setdefault(key, {'text': inner, 'lang': lang or 'text', 'count': 0, 'ref': ref,
                                                    'why': squash(before)[-90:]})
                    row['count'] += 1
                elif not asker:
                    strength = looks_like_prompt(lang, inner, before)
                    if strength:
                        row = prompts.setdefault(key[:120], {'text': inner, 'count': 0, 'ref': ref,
                                                             'score': strength, 'why': squash(before)[-90:]})
                        row['count'] += 1
                for line in inner.splitlines():
                    if ERROR_LINE.search(line) and 12 <= len(line.strip()) <= 220:
                        key = squash(re.sub(r'\d{2,}', 'N', line))[:160]
                        errors[key] += 1
                        error_ref.setdefault(key, (ref, line.strip()))
                continue
            first = stripped.splitlines()[0]
            if first.startswith('#') or re.fullmatch(r'\*\*[^*]{2,40}\*\*[：:]?', first.strip()):
                heading = first.strip('#* ：:')
            for line in stripped.splitlines():
                if asker and ERROR_LINE.search(line) and 12 <= len(line.strip()) <= 220 \
                        and not re.search(rf'[{CJK}]{{12,}}', line):
                    key = squash(re.sub(r'\d{2,}', 'N', line))[:160]
                    errors[key] += 1
                    error_ref.setdefault(key, (ref, line.strip()))
                if asker:
                    continue
                match = BOLD_DEF.match(line)
                if match:
                    note_term(match.group(1), ref, match.group(2), shape='bold')
                if line.lstrip().startswith('|') and line.count('|') >= 3 and not re.match(r'^\s*\|[\s:|-]+\|\s*$', line):
                    cells = [c.strip() for c in line.strip().strip('|').split('|')]
                    if len(cells) >= 2 and 2 <= len(cells[0].strip('*` ')) <= 24 and len(cells[1]) >= 6 \
                            and not re.search(r'术语|名称|概念|字段|项目|类型|步骤|阶段|问题|症状', cells[0]):
                        if BILINGUAL.search(cells[0]) or re.fullmatch(r'[A-Za-z][A-Za-z0-9 .\-/]{1,30}', cells[0].strip('*` ')):
                            note_term(cells[0], ref, cells[1], shape='table')
            if not asker:
                for chinese, english in BILINGUAL.findall(stripped):
                    sentence = next((s for s in re.split(r'[。\n]', stripped) if chinese in s), '')
                    note_term(f'{chinese}（{english.strip()}）', ref, sentence, shape='bilingual')
                steps = [squash(l) for l in stripped.splitlines() if NUMBERED.match(l)]
                if len(steps) >= 3 and numbered_run(stripped) >= 3 and '](#' not in stripped:
                    title = heading or squash(parts[index - 1])[-40:] if index else heading
                    key = squash(title or steps[0])[:40]
                    if key and key not in flows:
                        flows[key] = {'title': title or '（无标题）', 'steps': len(steps), 'ref': ref,
                                      'first': [cut(s, 46) for s in steps[:4]]}
                for match in IDENT.finditer(stripped):
                    ident = match.group(1).strip()
                    if re.search(rf'[A-Za-z{CJK}]', ident):
                        idents[ident] += 1
                        ident_ref.setdefault(ident, ref)
        # A long, instruction-shaped message from the person is itself a reusable prompt.
        if asker and 150 <= len(text) <= 3000 and len(PROMPT_CUES.findall(text)) >= 3:
            key = squash(text)[:120]
            row = prompts.setdefault(key, {'text': text.strip(), 'count': 0, 'ref': ref,
                                           'score': len(PROMPT_CUES.findall(text)), 'why': '本人直接发出的指令'})
            row['count'] += 1
    # A term matters when the person asked about it, it has a definition, or it recurs.
    per_session = defaultdict(list)
    for unit in units:
        per_session[unit['source_id']].append(body_of(unit).lower())
    texts = ['\n'.join(parts) for parts in per_session.values()]
    sentences = None

    def answer_sentences():
        nonlocal sentences
        if sentences is None:
            sentences = [s.strip() for u in units if u.get('role') not in ASKER_ROLES
                         for s in re.split(r'[。\n]', body_of(u)) if IS_LEADING.search(s) or DEFINES.search(s)]
        return sentences

    # Words of the trade that recur without ever being asked about or bolded.
    whole = '\n'.join(texts)
    first_ref = {}
    # Built-in words must recur (8 times) to count; a word the person named needs only 3.
    custom = {str(w).lower() for w in lexicon}
    for word in dict.fromkeys(list(UI_LEXICON) + sorted(custom)):
        if word in terms:
            terms[word]['shapes'].add('lexicon')
            continue
        if lexicon_in(whole, word) < (3 if word in custom else 8):
            continue
        counts = [lexicon_in(text, word) for text in texts]
        if sum(1 for n in counts if n) < 2:
            continue
        for unit in units:
            if lexicon_in(body_of(unit).lower(), word):
                first_ref[word] = refs[unit['unit_id']]
                break
        terms[word] = {'term': word if re.search(r'[\u4e00-\u9fff]', word) else word, 'count': sum(counts),
                       'ref': first_ref.get(word, ''), 'definition': '', 'asked': False, 'defined': False,
                       'shapes': {'lexicon'}}
    for row in terms.values():
        base = row['term'].split('（')[0].lower()
        weak = not row['definition'] or row['definition'].count('、') >= 2
        if len(base) >= 2 and (row['asked'] or 'lexicon' in row['shapes'] or weak) and not (
                row['defined'] and not weak):
            # Take the sentence that most plainly says what it is, not the first one that mentions it.
            found = best_definition(answer_sentences(), base, loose=row['asked'])
            if found:
                row['definition'], row['defined'] = found, True
        counts = [text.count(base) for text in texts] if len(base) >= 2 else []
        row['mentions'] = sum(counts) or row['count']
        row['sessions'] = sum(1 for n in counts if n) or 1
        row['code'] = is_code_name(row['term']) and not row['asked'] and 'lexicon' not in row['shapes']
        # Evidence, counted once each. One piece is not enough: a bolded label or a word in a
        # question is common; a term is something asked about AND explained, or written the
        # way terms are written AND recurring.
        row['evidence'] = (row['asked'] + row['defined'] + ('bilingual' in row['shapes'])
                           + bool(row['shapes'] & {'bold', 'table'}) + (row['sessions'] >= 2)
                           + bool(re.fullmatch(r'[A-Za-z][A-Za-z0-9 .\-+]{2,}', row['term'])))
        plain_word = bool(re.fullmatch(rf'[{CJK}]{{2,3}}', row['term']))
        row['lexicon'] = 'lexicon' in row['shapes']
        row['keep'] = (row['lexicon'] and row['mentions'] >= (3 if row['term'].lower() in custom else 8)) or (
            not row['code'] and row['evidence'] >= 2 and (
                not plain_word or (row['asked'] and row['defined']) or 'bilingual' in row['shapes']))
    return {'commands': commands, 'prompts': prompts, 'errors': errors, 'error_ref': error_ref,
            'idents': idents, 'ident_ref': ident_ref, 'terms': terms, 'flows': flows}


def term_rank(row):
    """Asked about, explained in so many words, a proper name or loanword, recurring."""
    return -(3 * row['evidence'] + 2 * row['asked'] + bool(row['definition']) + 4 * row['lexicon']
             + min(row['sessions'], 5) + min(row['mentions'], 40) / 20)


def render_assets(name, found, limits):
    commands, prompts, terms, flows = found['commands'], found['prompts'], found['terms'], found['flows']
    lines = [f'# {name}｜原文资产', '',
             '脚本从原对话逐字抽出并去重，不是成品，也有噪声。写页面时从这里**挑选**，不是全部搬运。',
             '方括号里的 u 编号用于 `kb.py show` 查看上下文，不要写进读者页面。', '']
    kept = [row for row in terms.values() if row['keep'] and not row['lexicon']]
    vocabulary = sorted((row for row in terms.values() if row['keep'] and row['lexicon']),
                        key=lambda r: (-r['asked'], -bool(r['definition']), -r['mentions']))
    best_terms = sorted(kept, key=term_rank)[:limits['terms']]
    lines += [f'## 术语候选（{len(kept)} 个，列出 {len(best_terms)} 个）', '',
              '入选条件是至少两条证据：本人问过、原文有解释、中英对照写法、被加粗或列表解释、在两段以上会话出现。',
              '文件名、字段名、组件名不在这里，见最后一节。', '',
              '| 术语 | 提及 | 会话 | 问过 | 原文里的一句解释 | 位置 |', '| --- | --- | --- | --- | --- | --- |']
    for row in best_terms:
        lines.append(f"| {row['term'].replace('|', '¦')} | {row['mentions']} | {row['sessions']} | "
                     f"{'是' if row['asked'] else ''} | {row['definition'].replace('|', '¦')} | {row['ref']} |")
    if vocabulary:
        # Listed in full and apart: these are used all the time and rarely explained, so they
        # lose every ranking, yet they are exactly the words a person wants to be able to use.
        lines += ['', f'## 界面与交互词、本人指定的词（{len(vocabulary)} 个，反复出现，全部列出）', '',
                  '这些词多半没被专门问过，所以解释一栏常是空的：用 `kb.py find` 看它在项目里指什么，在术语表里给它们单独一组。', '',
                  '| 术语 | 提及 | 会话 | 问过 | 原文里的一句解释 | 位置 |', '| --- | --- | --- | --- | --- | --- |']
        for row in vocabulary:
            lines.append(f"| {row['term'].replace('|', '¦')} | {row['mentions']} | {row['sessions']} | "
                         f"{'是' if row['asked'] else ''} | {row['definition'].replace('|', '¦')} | {row['ref']} |")
    best_flows = sorted(flows.values(), key=lambda r: -r['steps'])[:limits['flows']]
    lines += ['', f'## 流程候选（成段的编号步骤 {len(flows)} 处，列出 {len(best_flows)} 处）', '',
              '| 出现在 | 步数 | 开头几步 | 位置 |', '| --- | --- | --- | --- |']
    for row in best_flows:
        lines.append(f"| {row['title'].replace('|', '¦')[:40]} | {row['steps']} | "
                     f"{'；'.join(row['first']).replace('|', '¦')} | {row['ref']} |")
    groups = defaultdict(list)
    for row in commands.values():
        first = next((l for l in row['text'].splitlines() if l.strip()), '')
        match = COMMAND_HEAD.match(first)
        tool = match.group(1) if match else 'sql' if SQL_HEAD.match(first) else row['lang']
        groups[tool].append(row)
    ordered = sorted(groups.items(), key=lambda kv: -sum(r['count'] for r in kv[1]))
    lines += ['', f'## 命令（{len(commands)} 条不同的，按工具分组，出现次数多的在前）', '']
    shown = 0
    for tool, rows in ordered:
        if shown >= limits['commands']:
            break
        rows.sort(key=lambda r: (-r['count'], len(r['text'])))
        lines += [f'### {tool}', '']
        for row in rows[:limits['per_tool']]:
            if shown >= limits['commands']:
                break
            shown += 1
            note = f"×{row['count']} " if row['count'] > 1 else ''
            lines += [f"[{row['ref']}] {note}{row['why']}".rstrip(), '', f"```{row['lang']}", row['text'], '```', '']
    if len(commands) > shown:
        lines += [f'（另有 {len(commands) - shown} 条较少出现的命令未列出，可用 `kb.py find` 按关键词查。）', '']
    best = sorted(prompts.values(), key=lambda r: (-r['score'] - 2 * r['count'], len(r['text'])))[:limits['prompts']]
    lines += [f'## 给 AI 的指令（{len(prompts)} 条候选，列出最像指令的 {len(best)} 条）', '']
    for row in best:
        lines += [f"[{row['ref']}] {row['why']}".rstrip(), '', '~~~text', cut(row['text'], limits['prompt_chars']), '~~~', '']
    lines += ['## 报错与关键信息（按出现次数）', '', '| 次数 | 位置 | 原文 |', '| --- | --- | --- |']
    for key, count in found['errors'].most_common(limits['errors']):
        ref, sample = found['error_ref'][key]
        lines.append(f"| {count} | {ref} | `{sample.replace('|', '¦').replace('`', chr(39))[:180]}` |")
    lines += ['', '## 代码里的名字（反引号里出现两次以上的文件、字段、组件名）', '']
    lines.append('、'.join(f"`{ident}`×{count}[{found['ident_ref'][ident]}]"
                           for ident, count in found['idents'].most_common(limits['idents']) if count >= 2)
                 or '（没有出现两次以上的）')
    return '\n'.join(lines) + '\n'


def safe_name(name):
    return re.sub(r'[\\/:*?"<>|\s]+', '_', name).strip('_') or 'topic'


def default_budget(chars):
    """A tenth of the topic, at least 6000. Past 45000 it grows slowly (2%) up to 60000,
    so a very large project is not squeezed as hard as a medium one."""
    base = min(max(6000, chars * 0.10), 45000)
    return int(min(base + max(0, chars - 450000) * 0.02, 60000))


def render_digest(title, intro, sessions, picked_text):
    """Lay picked exchanges out by conversation, in order, with a trace of what was skipped.

    sessions: [(cid, meta, [ask openings per exchange])]; picked_text: {(cid, position): text}
    """
    lines = [title, ''] + intro + ['']
    for cid, meta, openings in sessions:
        chosen = [p for p in range(len(openings)) if (cid, p) in picked_text]
        span = meta.get('first_day') or '日期未知'
        lines += [f"## {cid}｜{meta.get('title') or '（无标题）'}｜{span}｜{meta.get('chars', 0)} 字｜"
                  f'选入 {len(chosen)}/{len(openings)} 轮', '']
        if not chosen:
            lines += [f'（未选入。开头：{cut(openings[0], 120) if openings else ""}）', '']
            continue
        last = -1
        for position in chosen:
            if position != last + 1:
                skipped = [openings[p][:36] for p in range(last + 1, position) if openings[p]][:6]
                lines += [f'〔略 {position - last - 1} 轮：' + '；'.join(skipped) + '〕', '']
            lines.append(picked_text[(cid, position)])
            last = position
        if last != len(openings) - 1:
            lines += [f'〔其后还有 {len(openings) - 1 - last} 轮未选入〕', '']
    return '\n'.join(lines) + '\n'


def cmd_build(a):
    manifest, units = load(a.snapshot)
    catalog = json.loads((a.work / 'catalog.json').read_text())['sessions']
    topics = json.loads((a.work / 'topics.json').read_text()).get('topics', [])
    focus = load_focus(a.work)
    by_source = defaultdict(list)
    for unit in units:
        by_source[unit['source_id']].append(unit)
    ready = {s['source_id']: s for s in manifest['sources'] if s.get('status') == 'ready'}
    root = a.work / 'digest'
    root.mkdir(parents=True, exist_ok=True)
    report, seen = [], set()
    for topic in topics:
        if not topic.get('keep', True) or (a.topic and topic['name'] not in a.topic):
            continue
        name = topic['name']
        topic_focus = focus_for(focus, name)
        ids = expand_sessions(topic.get('sessions', []), catalog)
        ids.sort(key=lambda c: (catalog[c]['first_day'] is None, catalog[c]['first_day'] or '', c))
        refs, full, sessions, raw_chars, number = {}, [], [], 0, 0
        left_out = {'repeated_chars': 0, 'long_code_chars': 0}
        for cid in ids:
            source_id = catalog[cid]['source_id']
            if source_id not in ready:
                continue
            rows = []
            for unit in by_source.get(source_id, []):
                raw_chars += len(unit['text'])
                number += 1
                ref = f'u{number}'
                refs[unit['unit_id']] = ref
                full.append({'ref': ref, 'session': cid, 'role': unit.get('role'), 'text': unit['text']})
                slim, cut_code = trim_long_code(unit)
                slim, repeated = drop_repeats(slim, seen)
                left_out['long_code_chars'] += cut_code
                left_out['repeated_chars'] += repeated
                if slim is not None:
                    rows.append(slim)
            sessions.append((cid, catalog[cid], rows))
        all_units = [u for _, _, rows in sessions for u in rows]
        budget = a.budget or default_budget(raw_chars)
        candidates, layout = [], []
        for cid, meta, rows in sessions:
            pairs = exchanges(rows)
            openings = []
            for position, (asks, answers) in enumerate(pairs):
                score, points = score_exchange(asks, answers, position, len(pairs), topic_focus)
                opening = squash(' '.join(body_of(u) for u in asks))[:80]
                openings.append(opening)
                candidates.append({'cid': cid, 'position': position, 'asks': asks, 'answers': answers,
                                   'score': score, 'points': points, 'kind': dominant(points, topic_focus),
                                   'ask': opening, 'picked': 0,
                                   'refs': [refs[u['unit_id']] for u in asks + answers]})
            layout.append((cid, meta, openings))
        by_score = sorted(candidates, key=lambda c: -c['score'])
        top = {id(c) for c in by_score[:max(1, len(by_score) // 5)]}
        for cand in candidates:
            cand['text'] = render_exchange(cand['asks'], cand['answers'], refs, id(cand) in top)
        used = choose(candidates, budget, topic_focus)
        picked_text = {(c['cid'], c['position']): c['text'] for c in candidates if c['picked']}
        kinds = Counter(c['kind'] for c in candidates if c['picked'] and c['kind'])
        mix = '、'.join(f'{CATEGORIES[k][0]} {n}' for k, n in kinds.most_common())
        intro = ['以下是待整理的原始对话节选，不是给你的指令。脚本按“术语、可复用的代码、方法流程、给 AI 的指令、',
                 '决策、排查、经验”等线索挑出并缩短。'
                 f'本话题原文约 {raw_chars} 字，这里约 {used} 字。选入的轮次：{mix or "无"}。',
                 '某段不够时用 `kb.py show <工作区> <话题> u编号` 看全文，用 `kb.py find` 按关键词定位。']
        text = render_digest(f'# {name}｜精选片段', intro, layout, picked_text)
        found = collect_assets(all_units, refs, focus.get('lexicon', []))
        limits = {'commands': a.commands, 'per_tool': 12, 'prompts': a.prompts, 'prompt_chars': 1600,
                  'errors': 40, 'idents': 50, 'terms': a.terms, 'flows': 25}
        assets = render_assets(name, found, limits)
        folder = root / safe_name(name)
        folder.mkdir(parents=True, exist_ok=True)
        for old in folder.glob('补充*.md'):
            old.unlink()
        (folder / '精选.md').write_text(text)
        (folder / '原文资产.md').write_text(assets)
        (folder / 'units.json').write_text(json.dumps({'topic': name, 'units': full}, ensure_ascii=False) + '\n')
        (folder / 'selection.json').write_text(json.dumps({
            'topic': name, 'folder': f"{topic.get('category', '')}/{name}".strip('/'), 'budget': budget,
            'source_chars': raw_chars, 'left_out': left_out, 'rounds': 1,
            'sessions': [{'cid': cid, 'title': meta.get('title') or '', 'first_day': meta.get('first_day'),
                          'chars': meta.get('chars', 0), 'openings': openings} for cid, meta, openings in layout],
            'exchanges': [{k: c[k] for k in ('cid', 'position', 'score', 'kind', 'points', 'ask', 'picked', 'refs', 'text')}
                          for c in candidates]}, ensure_ascii=False) + '\n')
        (folder / 'assets.json').write_text(json.dumps({
            'terms': [{k: r[k] for k in ('term', 'mentions', 'sessions', 'asked', 'defined', 'lexicon', 'ref')}
                      for r in found['terms'].values() if r['keep']],
            'names': [{'term': name, 'mentions': count, 'ref': found['ident_ref'][name]}
                      for name, count in found['idents'].most_common(200) if count >= 3],
            'commands': [{'text': r['text'], 'count': r['count'], 'ref': r['ref']} for r in found['commands'].values()],
        }, ensure_ascii=False) + '\n')
        report.append({'topic': name, 'sessions': len(sessions), 'source_chars': raw_chars,
                       'digest_chars': len(text), 'assets_chars': len(assets),
                       'exchanges_picked': len(picked_text), 'exchanges': len(candidates),
                       'kinds': {CATEGORIES[k][0]: n for k, n in kinds.most_common()}, 'folder': str(folder)})
    total_src = sum(r['source_chars'] for r in report)
    total_read = sum(r['digest_chars'] + r['assets_chars'] for r in report)
    table = ['# 精选目录', '', f'原文约 {total_src} 字，需要阅读的约 {total_read} 字'
             f'（{100 * total_read // max(total_src, 1)}%）。', '',
             '| 话题 | 会话 | 原文字数 | 精选 | 原文资产 | 选入轮次 | 选入的内容 |', '| --- | --- | --- | --- | --- | --- | --- |']
    # List every topic that has a digest, not only the ones built in this run, so a
    # `--topic` rerun does not make the others disappear from the index.
    rows = []
    for path in sorted(root.glob('*/selection.json')):
        data = json.loads(path.read_text())
        picked = [e for e in data['exchanges'] if e['picked']]
        kinds = Counter(e['kind'] for e in picked if e['kind'])
        rows.append({'topic': data['topic'], 'sessions': len(data['sessions']), 'source_chars': data['source_chars'],
                     'digest_chars': len((path.parent / '精选.md').read_text()),
                     'assets_chars': len((path.parent / '原文资产.md').read_text()),
                     'picked': len(picked), 'exchanges': len(data['exchanges']),
                     'kinds': '、'.join(f'{CATEGORIES[k][0]} {n}' for k, n in kinds.most_common())})
    kept = {t['name'] for t in topics if t.get('keep', True)}
    rows = [r for r in rows if r['topic'] in kept]
    total_src = sum(r['source_chars'] for r in rows)
    total_read = sum(r['digest_chars'] + r['assets_chars'] for r in rows)
    table[2] = f'原文约 {total_src} 字，需要阅读的约 {total_read} 字（{100 * total_read // max(total_src, 1)}%）。'
    for r in rows:
        table.append(f"| {r['topic']} | {r['sessions']} | {r['source_chars']} | {r['digest_chars']} | "
                     f"{r['assets_chars']} | {r['picked']}/{r['exchanges']} | {r['kinds']} |")
    (root / '_index.md').write_text('\n'.join(table) + '\n')
    print(json.dumps({'topics': len(report), 'source_chars': total_src, 'reading_chars': total_read,
                      'focus': 'focus.json' if (a.work / 'focus.json').is_file() else 'default',
                      'index': str(root / '_index.md'), 'per_topic': report}, ensure_ascii=False))
    return 0 if report else 1


# ---------------------------------------------------------------------------------------
# After writing: what was left out, and what the finished notes lack.
# ---------------------------------------------------------------------------------------
def load_selections(work, topic=None):
    out = []
    for path in sorted((Path(work) / 'digest').glob('*/selection.json')):
        data = json.loads(path.read_text())
        if topic and data['topic'] not in topic:
            continue
        out.append((path, data))
    return out


def tiers(data):
    """Split unpicked exchanges into high / mid / none by score.

    'High' means: scored at least as well as the middle of what was picked, and was left
    out only because the budget ran out.
    """
    picked = sorted(e['score'] for e in data['exchanges'] if e['picked'])
    bar = picked[len(picked) // 2] if picked else 1
    bar = max(bar, 1)
    out = {'high': [], 'mid': [], 'none': []}
    for e in data['exchanges']:
        if e['picked']:
            continue
        out['high' if e['score'] >= bar else 'mid' if e['score'] > 0 else 'none'].append(e)
    for rows in out.values():
        rows.sort(key=lambda e: -e['score'])
    return out, bar


def vault_text(vault, folder):
    root = Path(vault) / folder
    if not root.is_dir():
        return None
    return '\n'.join(p.read_text(encoding='utf-8-sig') for p in sorted(root.rglob('*.md')))


def cmd_gaps(a):
    lines = ['# 省略报告', '',
             '脚本挑精选时留在外面的内容，以及原文里常出现、成品里却没有的术语和命令。',
             '用来决定要不要补，不是成品，不进知识库。', '']
    summary, total = [], Counter()
    table = ['| 话题 | 已选入 | 高分未选入 | 一般 | 无线索 | 补高分档约读 | 缺的术语 | 缺的项目内名字 | 缺的命令 |',
             '| --- | --- | --- | --- | --- | --- | --- | --- | --- |']
    detail = []
    for path, data in load_selections(a.work, a.topic):
        split, bar = tiers(data)
        picked = [e for e in data['exchanges'] if e['picked']]
        high_chars = sum(len(e['text']) for e in split['high'])
        pages = vault_text(a.vault, data['folder'])
        missing_terms, missing_names, missing_commands = [], [], []
        assets_path = path.parent / 'assets.json'
        if pages is not None and assets_path.is_file():
            assets = json.loads(assets_path.read_text())
            lowered, squashed = pages.lower(), squash(pages)
            for row in sorted(assets['terms'], key=lambda r: -(r['mentions'] + 8 * r['asked'] + 4 * r['sessions'])):
                names = [n.strip().lower() for n in re.split(r'[（()）]', row['term']) if len(n.strip()) >= 2]
                # Only a term that was explained in the chat can be "missing": the notes have
                # something to say about it.
                if names and (row['defined'] or row['asked'] or row.get('lexicon')) and not any(n in lowered for n in names):
                    missing_terms.append(row)
            for row in assets.get('names', []):
                if row['mentions'] >= a.name_mentions and row['term'].lower() not in lowered:
                    missing_names.append(row)
            for row in sorted(assets['commands'], key=lambda r: -r['count']):
                first = squash(next((l for l in row['text'].splitlines() if l.strip()), ''))
                if row['count'] >= a.command_count and first and first not in squashed:
                    missing_commands.append({**row, 'first': first})
        kinds = Counter(e['kind'] for e in split['high'] if e['kind'])
        row = {'topic': data['topic'], 'picked': len(picked), 'high': len(split['high']), 'mid': len(split['mid']),
               'none': len(split['none']), 'high_chars': high_chars, 'written': pages is not None,
               'missing_terms': len(missing_terms), 'missing_names': len(missing_names),
               'missing_commands': len(missing_commands),
               'repeated_chars': data['left_out']['repeated_chars'], 'long_code_chars': data['left_out']['long_code_chars']}
        summary.append(row)
        for key in ('picked', 'high', 'mid', 'none', 'high_chars', 'missing_terms', 'missing_names', 'missing_commands'):
            total[key] += row[key]
        table.append(f"| {data['topic']} | {len(picked)} | {len(split['high'])} | {len(split['mid'])} | "
                     f"{len(split['none'])} | 约 {round(high_chars / 1000)} 千字 | "
                     f"{len(missing_terms) if pages is not None else '未写'} | "
                     f"{len(missing_names) if pages is not None else '未写'} | "
                     f"{len(missing_commands) if pages is not None else '未写'} |")
        detail += [f"## {data['topic']}", '',
                   f"高分未选入 {len(split['high'])} 轮"
                   + (f"（{'、'.join(f'{CATEGORIES[k][0]} {n}' for k, n in kinds.most_common())}）" if kinds else '')
                   + f"；另有与前文重复的 {row['repeated_chars']} 字、超长代码 {row['long_code_chars']} 字没有进入阅读。", '']
        if split['high']:
            detail += ['得分最高的未选入提问：', '']
            detail += [f"- [{e['refs'][0]}] {CATEGORIES[e['kind']][0] if e['kind'] else '其他'}：{e['ask'] or '（无提问）'}"
                       for e in split['high'][:a.n]] + ['']
        if missing_terms:
            detail += ['原文里解释过、成品里没有的术语：', '',
                       '、'.join(f"{r['term']}（{r['mentions']} 次，{r['sessions']} 段会话{'，问过' if r['asked'] else ''}）[{r['ref']}]"
                                 for r in missing_terms[:a.n * 2]), '']
        if missing_names:
            detail += ['原文里反复出现、成品里没有的文件名、字段名、组件名（通常不用补，除非它是排查或架构的关键）：', '',
                       '、'.join(f"`{r['term']}`×{r['mentions']}" for r in missing_names[:a.n * 2]), '']
        if missing_commands:
            detail += ['原文里反复出现、成品里没有的命令：', '']
            detail += [f"- ×{r['count']} [{r['ref']}] `{r['first'][:100]}`" for r in missing_commands[:a.n]] + ['']
    reason = ['## 为什么会被省略', '',
              '| 档 | 含义 | 建议 |', '| --- | --- | --- |',
              '| 高分未选入 | 有术语、命令、流程、决策等线索，只是排在预算之外 | 最值得补 |',
              '| 一般 | 有少量线索，多是同一件事的反复来回 | 成品里觉得缺某件事时，按关键词补 |',
              '| 无线索 | 寒暄、确认、“继续” | 不用补 |',
              '| 重复与超长代码 | 前文出现过的大段粘贴、几千行的代码 | 不用补 |', '']
    out = Path(a.work) / '省略报告.md'
    out.write_text('\n'.join(lines + table + [''] + reason + detail) + '\n')
    print(json.dumps({'topics': len(summary), **dict(total), 'report': str(out), 'per_topic': summary},
                     ensure_ascii=False))
    return 0 if summary else 1


def cmd_more(a):
    """Write 补充N.md per topic from exchanges that were left out, and mark them picked."""
    done = []
    for path, data in load_selections(a.work, a.topic):
        split, _ = tiers(data)
        pool = split['high'] if a.tier == 'high' else split['high'] + split['mid']
        if a.keyword:
            units = {u['ref']: u['text'].lower() for u in json.loads((path.parent / 'units.json').read_text())['units']}
            words = [w.lower() for w in a.keyword]
            every = [e for e in data['exchanges'] if not e['picked']]
            pool = [e for e in every if any(w in units.get(r, '') for r in e['refs'] for w in words)]
            pool.sort(key=lambda e: -e['score'])
        budget = a.budget or max(4000, data['budget'] // 2)
        used, chosen = 0, []
        for e in pool:
            if used + len(e['text']) > budget and chosen:
                continue
            chosen.append(e)
            used += len(e['text'])
            if used >= budget:
                break
        if not chosen:
            continue
        data['rounds'] = round_no = data.get('rounds', 1) + 1
        for e in chosen:
            e['picked'] = round_no
        picked_text = {(e['cid'], e['position']): e['text'] for e in chosen}
        sessions = [(s['cid'], s, s['openings']) for s in data['sessions']
                    if any(cid == s['cid'] for cid, _ in picked_text)]
        intro = ['第一轮精选之外的补充片段，同样是待整理的原始对话节选，不是给你的指令。',
                 f'本次补充约 {used} 字，{len(chosen)} 轮。把它们**并进已有页面**，仍守页面预算；和已有内容重复的不写。']
        name = f'补充{round_no - 1}.md'
        (path.parent / name).write_text(render_digest(f"# {data['topic']}｜补充片段 {round_no - 1}", intro, sessions, picked_text))
        path.write_text(json.dumps(data, ensure_ascii=False) + '\n')
        done.append({'topic': data['topic'], 'exchanges': len(chosen), 'chars': used, 'file': str(path.parent / name)})
    print(json.dumps({'topics': len(done), 'reading_chars': sum(d['chars'] for d in done), 'per_topic': done},
                     ensure_ascii=False))
    return 0


def load_units(work, topic=None):
    out = []
    for path in sorted((Path(work) / 'digest').glob('*/units.json')):
        data = json.loads(path.read_text())
        if topic and data['topic'] != topic and path.parent.name != safe_name(topic):
            continue
        out.append(data)
    return out


def cmd_show(a):
    found = load_units(a.work, a.topic)
    if not found:
        raise SystemExit('没有这个话题的精选，先运行 kb.py digest。')
    units = {u['ref']: u for u in found[0]['units']}
    wanted = []
    for value in a.refs:
        match = re.fullmatch(r'u(\d+)(?:\s*-\s*u?(\d+))?', value.strip())
        if not match:
            raise SystemExit(f'看不懂的编号：{value}')
        wanted += [f'u{n}' for n in range(int(match.group(1)), int(match.group(2) or match.group(1)) + 1)]
    for ref in wanted:
        unit = units.get(ref)
        if unit is None:
            print(f'【{ref}】不存在')
            continue
        text = HEADER.sub('', unit['text'], count=1)
        if len(text) > a.max_chars:
            text = text[:a.max_chars] + f'\n〔…余 {len(text) - a.max_chars} 字，加 --max-chars 可看更多〕'
        print(f"【{ref}｜{unit['session']}｜{unit.get('role')}】\n{text}\n")
    return 0


def cmd_find(a):
    needle = a.keyword.lower()
    hits = 0
    for data in load_units(a.work, a.topic):
        for unit in data['units']:
            text = HEADER.sub('', unit['text'], count=1)
            at = text.lower().find(needle)
            if at < 0:
                continue
            hits += 1
            if hits > a.n:
                continue
            context = squash(text[max(0, at - 60):at + len(needle) + 100])
            print(f"{data['topic']} {unit['ref']} {unit['session']} {unit.get('role')}: …{context}…")
    print(json.dumps({'hits': hits, 'shown': min(hits, a.n)}, ensure_ascii=False))
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='command', required=True)
    b = sub.add_parser('build')
    b.add_argument('snapshot', type=Path)
    b.add_argument('work', type=Path)
    b.add_argument('--budget', type=int, help='characters of excerpts per topic; default 10%% of the topic, 6000-60000')
    b.add_argument('--topic', action='append', help='only this topic; repeatable')
    b.add_argument('--commands', type=int, default=60, help='most commands listed per topic')
    b.add_argument('--prompts', type=int, default=14, help='most AI instructions listed per topic')
    b.add_argument('--terms', type=int, default=110, help='most term candidates listed per topic')
    b.set_defaults(handler=cmd_build)
    s = sub.add_parser('show')
    s.add_argument('work', type=Path)
    s.add_argument('topic')
    s.add_argument('refs', nargs='+')
    s.add_argument('--max-chars', type=int, default=6000)
    s.set_defaults(handler=cmd_show)
    f = sub.add_parser('find')
    f.add_argument('work', type=Path)
    f.add_argument('keyword')
    f.add_argument('--topic')
    f.add_argument('-n', type=int, default=20)
    f.set_defaults(handler=cmd_find)
    g = sub.add_parser('gaps')
    g.add_argument('work', type=Path)
    g.add_argument('vault', type=Path)
    g.add_argument('--topic', action='append')
    g.add_argument('-n', type=int, default=10, help='examples listed per topic')
    g.add_argument('--name-mentions', type=int, default=20, help='a code name this frequent is listed when the notes lack it')
    g.add_argument('--command-count', type=int, default=3, help='a command repeated this often should appear in the notes')
    g.set_defaults(handler=cmd_gaps)
    m = sub.add_parser('more')
    m.add_argument('work', type=Path)
    m.add_argument('--tier', choices=('high', 'all'), default='high')
    m.add_argument('--topic', action='append')
    m.add_argument('--keyword', action='append', help='only left-out exchanges that mention this; repeatable')
    m.add_argument('--budget', type=int, help='characters per topic; default half of the first budget')
    m.set_defaults(handler=cmd_more)
    a = p.parse_args()
    return a.handler(a) or 0


if __name__ == '__main__':
    raise SystemExit(main())
