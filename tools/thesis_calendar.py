#!/usr/bin/env python3
"""Thesis Calendar — 扫描所有投资论文的「检验点日历」，列出到期 / 逾期 / 从未追踪的检验点。

为什么需要它：/investment-team 第八步半要求把结论落成 {公司}-thesis.md，并写明检验点日历
（财报日、判决日、整改期限…）。但日期躺在 Markdown 里不会自己跳出来——实测 29 个有最终报告的
公司只有 5 份论文，且追踪记录全部为空，thesis-tracker 一次没跑过。本工具把"该回看谁了"变成一条命令，
可以手动跑，也可以挂到每周定时任务（cron / Claude 定时任务）里。

用法：
    python3 tools/thesis_calendar.py                 # 默认：45 天内到期 + 已逾期 + 从未追踪
    python3 tools/thesis_calendar.py --days 90       # 拉长窗口
    python3 tools/thesis_calendar.py --all           # 列出全部检验点（含远期）
    python3 tools/thesis_calendar.py --json          # 机器可读，供定时任务判断"有没有事要做"
    python3 tools/thesis_calendar.py --company 腾讯   # 只看一家

退出码：有逾期未检的检验点 → 1；否则 0（方便定时任务/CI 用退出码触发提醒）。

零外部依赖（仅 stdlib）。
"""

import argparse
import glob
import json
import os
import re
import sys
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS = os.path.join(ROOT, 'reports')

_SECTION_RE = re.compile(r'^#{1,6}\s*(.*)$')
_FULL_DATE_RE = re.compile(r'(20\d\d)[-/年](\d{1,2})(?:[-/月](\d{1,2}))?')
# "2027-01 / 04 / 07（各季财报）" 这种一年多月的写法：年月后面跟若干 "/ MM"
_MULTI_MONTH_RE = re.compile(r'(20\d\d)-(\d{1,2})((?:\s*/\s*\d{1,2})+)')
_ONGOING_RE = re.compile(r'持续|常态|每季|每次财报|随时|长期')
_TRACK_DATE_RE = re.compile(r'^\|\s*(20\d\d-\d{1,2}-\d{1,2})\s*\|')


def _parse_dates(text: str) -> list:
    """从检验点第一列抽出所有日期，返回 [(date, 精度)]；精度 'day' 或 'month'。"""
    found = []
    consumed = []
    for m in _MULTI_MONTH_RE.finditer(text):
        y = int(m.group(1))
        months = [int(m.group(2))] + [int(x) for x in re.findall(r'\d{1,2}', m.group(3))]
        for mo in months:
            if 1 <= mo <= 12:
                found.append((date(y, mo, 1), 'month'))
        consumed.append((m.start(), m.end()))
    for m in _FULL_DATE_RE.finditer(text):
        if any(s <= m.start() < e for s, e in consumed):
            continue
        y, mo = int(m.group(1)), int(m.group(2))
        if not 1 <= mo <= 12:
            continue
        if m.group(3):
            d = int(m.group(3))
            try:
                found.append((date(y, mo, d), 'day'))
            except ValueError:
                found.append((date(y, mo, 1), 'month'))
        else:
            found.append((date(y, mo, 1), 'month'))
    return found


def parse_thesis(path: str) -> dict:
    """返回 {company, path, status, calendar: [...], last_tracked: date|None, has_calendar: bool}。"""
    try:
        with open(path, encoding='utf-8') as f:
            lines = f.read().split('\n')
    except OSError:
        return None
    company = os.path.basename(path).replace('-thesis.md', '')
    section = ''
    calendar, tracks = [], []
    status = ''
    for i, line in enumerate(lines, start=1):
        hm = _SECTION_RE.match(line.strip())
        if hm:
            section = hm.group(1)
            continue
        if i <= 8 and '状态' in line and not status:
            sm = re.search(r'状态[：:]\s*\**\s*([^*｜|（(]+)', line)
            status = sm.group(1).strip() if sm else ''
        if '检验点' in section and line.strip().startswith('|'):
            cells = [c.strip() for c in line.strip().strip('|').split('|')]
            if len(cells) < 2 or re.fullmatch(r'[\s\-:]*', cells[0]) or cells[0] in ('日期 / 触发事件', '日期', '触发事件', '日期/触发事件'):
                continue
            when = cells[0]
            what = cells[1] if len(cells) > 1 else ''
            hyp = cells[2] if len(cells) > 2 else ''
            dates = _parse_dates(when)
            if not dates:
                kind = 'ongoing' if _ONGOING_RE.search(when) else 'event'
                calendar.append({'line': i, 'when': when, 'what': what, 'hyp': hyp,
                                 'date': None, 'precision': None, 'kind': kind})
            for d, prec in dates:
                calendar.append({'line': i, 'when': when, 'what': what, 'hyp': hyp,
                                 'date': d, 'precision': prec, 'kind': 'dated'})
        if '追踪记录' in section:
            tm = _TRACK_DATE_RE.match(line.strip())
            if tm:
                try:
                    tracks.append(date.fromisoformat(tm.group(1)))
                except ValueError:
                    pass
    return {'company': company, 'path': os.path.relpath(path, ROOT), 'status': status,
            'calendar': calendar, 'last_tracked': max(tracks) if tracks else None,
            'has_calendar': any(c['kind'] == 'dated' for c in calendar)}


def classify(entry: dict, today: date, last_tracked, window_days: int) -> str:
    """dated 检验点相对今天与上次追踪的状态。"""
    d = entry['date']
    if d is None:
        return entry['kind']
    # 月精度：视为当月最后一天到期，避免月初就报逾期
    due = d
    if entry['precision'] == 'month':
        nxt = date(d.year + (d.month == 12), (d.month % 12) + 1, 1)
        due = nxt - timedelta(days=1)
    if due < today:
        if last_tracked and last_tracked >= due:
            return 'checked'
        return 'overdue'
    if due <= today + timedelta(days=window_days):
        return 'upcoming'
    return 'future'


def main():
    ap = argparse.ArgumentParser(description='投资论文检验点日历：谁该回看了')
    ap.add_argument('--days', type=int, default=45, help='"即将到期"窗口，默认 45 天')
    ap.add_argument('--all', action='store_true', help='列出全部检验点（含远期与已检）')
    ap.add_argument('--company', help='只看某家公司（匹配文件名前缀）')
    ap.add_argument('--json', action='store_true', help='机器可读输出')
    ap.add_argument('--today', help='指定"今天"（YYYY-MM-DD），用于测试')
    args = ap.parse_args()

    today = date.fromisoformat(args.today) if args.today else date.today()
    files = sorted(glob.glob(os.path.join(REPORTS, '**', '*-thesis.md'), recursive=True))
    if args.company:
        files = [f for f in files if os.path.basename(f).startswith(args.company)]
    theses = [t for t in (parse_thesis(f) for f in files) if t]

    out = []
    for t in theses:
        rows = []
        for e in t['calendar']:
            st = classify(e, today, t['last_tracked'], args.days)
            rows.append({**e, 'date': e['date'].isoformat() if e['date'] else None, 'status': st})
        out.append({'company': t['company'], 'path': t['path'], 'status': t['status'],
                    'last_tracked': t['last_tracked'].isoformat() if t['last_tracked'] else None,
                    'has_calendar': t['has_calendar'], 'checkpoints': rows})

    overdue_total = sum(1 for t in out for r in t['checkpoints'] if r['status'] == 'overdue')
    if args.json:
        print(json.dumps({'today': today.isoformat(), 'window_days': args.days,
                          'overdue': overdue_total, 'theses': out}, ensure_ascii=False, indent=2))
        sys.exit(1 if overdue_total else 0)

    BOLD, RED, GREEN, YELLOW, RESET = '\033[1m', '\033[91m', '\033[92m', '\033[93m', '\033[0m'
    print('=' * 70)
    print(f'{BOLD}投资论文检验点日历{RESET}   今天 {today}   窗口 {args.days} 天   论文 {len(out)} 份')
    print('=' * 70)
    if not out:
        print('  没有找到任何 *-thesis.md。/investment-team 第八步半要求每次研究都建论文（不买入也建观察论文）。')
        sys.exit(0)

    order = {'overdue': 0, 'upcoming': 1, 'event': 2, 'ongoing': 3, 'future': 4, 'checked': 5}
    label = {'overdue': (RED, '逾期未检'), 'upcoming': (YELLOW, '即将到期'), 'event': (YELLOW, '事件触发'),
             'ongoing': ('', '持续观察'), 'future': ('', '远期'), 'checked': (GREEN, '已检')}
    for t in out:
        tracked = t['last_tracked'] or '从未'
        never = '' if t['last_tracked'] else f'  {RED}← 从未跑过 /thesis-tracker{RESET}'
        print(f'\n{BOLD}{t["company"]}{RESET}  状态：{t["status"] or "未标"}  上次追踪：{tracked}{never}')
        if not t['has_calendar']:
            print(f'  {YELLOW}论文没有带日期的检验点——补「检验点日历」表，否则没有东西会提醒你回看{RESET}')
        rows = sorted(t['checkpoints'], key=lambda r: (order.get(r['status'], 9), r['date'] or '9999'))
        for r in rows:
            if not args.all and r['status'] in ('future', 'checked', 'ongoing'):
                continue
            color, name = label[r['status']]
            when = r['date'] or r['when'][:24]
            print(f'  {color}{name:<5s}{RESET} {when:<12s} {r["what"][:60]}'
                  + (f'  （假设 {r["hyp"]}）' if r['hyp'] else ''))
    print('-' * 70)
    if overdue_total:
        print(f'{BOLD}{RED}{overdue_total} 个检验点已过期且未追踪 → 对应公司跑 /thesis-tracker {{公司名}} 做模式 B 检查{RESET}')
    else:
        print(f'{BOLD}{GREEN}没有逾期检验点。{RESET}')
    print('=' * 70)
    sys.exit(1 if overdue_total else 0)


if __name__ == '__main__':
    main()
