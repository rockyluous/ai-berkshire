#!/usr/bin/env python3
"""Report Audit Tool for AI Berkshire.

数据抽检工具：从研究报告中抽取15%的财务数据点，与可靠信源比对，
通过则准出，不通过则打回并说明原因。

Zero external dependencies — uses only Python stdlib.
Requires Python >= 3.7.

工作流程（三步）：
  Step 1 — 提取数据点，随机抽样15%：
    python3 tools/report_audit.py extract --report reports/xxx.md

  Step 2 — Claude 对抽检清单中的每个数据点，从可靠信源（macrotrends/
            stockanalysis/aastocks/eastmoney）取数，填入 fetched_value

  Step 3 — 输入核验结果，输出准出/打回判决：
    python3 tools/report_audit.py verdict --results '[...]'

  一步完成（仅提取+打印抽检清单，不做网络验证）：
    python3 tools/report_audit.py extract --report reports/xxx.md --dry-run
"""

import argparse
import json
import math
import os
import re
import sys
from decimal import Decimal, Context, ROUND_HALF_EVEN
from datetime import date as date_mod
from random import Random

_CTX = Context(prec=28, rounding=ROUND_HALF_EVEN)

# ---------------------------------------------------------------------------
# 数据点提取：从 Markdown 报告中识别财务数字
# ---------------------------------------------------------------------------

# 匹配模式：数字 + 单位，前面有上下文标签
# 例：收入：1,239亿元、PE 18.8x、毛利率 56%、市值 ~$5,670亿
#
# 注意：所有数字捕获组必须带上可选符号位 _SIGN，否则 "-1.72%" 会被抓成 "1.72"，
# 导致核验时报告值与信源值符号相反、偏差 200%，产生假打回。
# 符号位涵盖 ASCII 正负号、Unicode 减号(U+2212)、en-dash(U+2013)、全角正负号。
_SIGN = r'[+\-−–－＋]?'

_PATTERNS = [
    # 百分比
    (r'(' + _SIGN + r'[\d,，\.]+)\s*%',                        '%',    'percent'),
    # 亿元/亿美元/亿港元
    (r'(' + _SIGN + r'[\d,，\.]+)\s*亿(元|美元|港元|RMB|USD|HKD)?', '亿',    'hundred_million'),
    # 倍数 PE/PB/PS
    (r'(' + _SIGN + r'[\d,，\.]+)\s*[xX倍]',                   'x',    'multiple'),
    # 万亿
    (r'(' + _SIGN + r'[\d,，\.]+)\s*万亿',                      '万亿', 'trillion'),
    # 美元绝对值（B/T）
    (r'\$\s*(' + _SIGN + r'[\d,，\.]+)\s*([BMT亿])',             '$',    'usd_abs'),
    # 纯整数（如市值、收入、用户数等，出现在表格 | 里）
    (r'\|\s*[~约]?\$?(' + _SIGN + r'[\d,，\.]+)\s*\|',          '',     'table_num'),
]

_LABEL_RE = re.compile(
    r'(?P<label>[^\|\n：:]{2,25})[：:\s]+[~约]?\$?(?P<num>' + _SIGN + r'[\d,，\.]+)'
    r'\s*(?P<unit>亿[元美港]?元?|万亿|[xX倍]|%|[BMT])?'
)

# 标签命中即视为非数据点（评分、定性判断、操作建议里的价格/PE 阈值等）
_NON_DATA_LABEL_RE = re.compile(
    r'评分|评级|判断|阈值|建议|操作|情景|信号|催化|激进型|稳健型|保守型|Checklist|通过\?'
    r'|区间|中位|隐含|目标价|涨跌幅|vs 现价|负面项|正面项|更新日期'   # 工具派生值 / 摘要句，不是外部可核验数据
)

_TABLE_ROW_RE = re.compile(
    r'\|\s*(?P<label>[^|]{1,40})\s*\|\s*[~约]?\$?(?P<num>' + _SIGN + r'[\d,，\.]+)'
    r'\s*(?P<unit>亿[元美港]?元?|万亿|[xX倍]|%|[BMT])?\s*\|'
)


def _clean_num(s: str) -> float:
    """把带逗号、中文逗号、各类正负号的数字字符串转为 float。

    支持 ASCII '-'/'+'、Unicode 减号 '−'(U+2212)、en-dash '–'(U+2013)、
    全角 '－'(U+FF0D)/'＋'(U+FF0B)——报告中这些符号都可能被用作正负号。
    """
    s = s.replace(',', '').replace('，', '').strip()
    # 归一化各类符号为 ASCII
    for ch in ('−', '–', '－'):
        s = s.replace(ch, '-')
    s = s.replace('＋', '+')
    try:
        return float(s)
    except ValueError:
        return None


def _is_valid_label(label: str) -> bool:
    """判断标签是否是有意义的财务字段名，过滤噪声。"""
    label = label.strip()
    # 太短
    if len(label) < 2:
        return False
    # 纯数字或纯年份
    if re.fullmatch(r'[\d\s年季度Q]+', label):
        return False
    # 以符号/markdown标记开头
    if re.match(r'^[+\-\*#\|~\$>_`]', label):
        return False
    # 含有 markdown 粗体/代码标记
    if '**' in label or '`' in label or '__' in label:
        return False
    # 标签含有纯增速符号（如 +56%、-13% 单独作标签）
    if re.fullmatch(r'[+\-]?\d+(\.\d+)?%', label):
        return False
    # 常见无意义标签
    _SKIP = {'来源', 'sources', 'source', '说明', '注意', '备注', '数据来源',
             'n/a', '—', '-', '/', '合计', 'total', '单位', '趋势'}
    if label.lower() in _SKIP:
        return False
    return True


# 两列表格行：| 标签 | 数值 unit |（专为财务报告的 KV 表设计）
_KV_TABLE_RE = re.compile(
    r'^\|\s*(?P<label>[^|*\n]{2,40}?)\s*\|\s*[~约]?\$?(?P<num>' + _SIGN + r'[\d,，\.]+)\s*'
    r'(?P<unit>亿[元美港]?元?|万亿|[xX倍]|%|[BMT亿])?\s*[\|（\(]'
)

# 带标签的 KV 行：标签：数值 单位
_KV_LABEL_RE = re.compile(
    r'(?P<label>[\u4e00-\u9fa5A-Za-z][^\|\n：:*]{1,30})[：:]\s*[~约]?\$?'
    r'(?P<num>' + _SIGN + r'[\d,，\.]+)\s*(?P<unit>亿[元美港]?元?|万亿|[xX倍]|%|[BMT])?'
)


def _parse_md_tables(lines: list) -> list:
    """解析 Markdown 中所有表格，返回 (row_label, col_header, value, unit, lineno, raw, heading) 列表。

    heading 是该表格上方最近的标题文本，供调用方跳过"复核记录 / 版本对照 / 成本"等流程元数据表。
    """
    results = []
    heading = ''
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        hm = re.match(r'^#{1,6}\s+(.*)$', line)
        if hm:
            heading = hm.group(1).strip()
            i += 1
            continue
        # 检测表头行（含 | 且不是分隔行）
        if '|' in line and not re.match(r'^\|[\-\s\|:]+\|$', line):
            headers_raw = [h.strip().strip('*_').strip() for h in line.split('|')]
            headers_raw = [h for h in headers_raw if h]
            # 下一行应是分隔行
            if i + 1 < len(lines) and re.match(r'^\|[\-\s\|:]+\|$', lines[i+1].strip()):
                i += 2  # 跳过分隔行
                # 读数据行
                while i < len(lines):
                    dline = lines[i].strip()
                    if not dline or not dline.startswith('|'):
                        break
                    cells = [c.strip().strip('*_~').strip() for c in dline.split('|')]
                    cells = [c for c in cells if c != '']
                    if len(cells) < 2:
                        i += 1
                        continue
                    row_label = cells[0]
                    for col_idx, cell in enumerate(cells[1:], start=1):
                        col_header = headers_raw[col_idx] if col_idx < len(headers_raw) else f'列{col_idx}'
                        # 提取 cell 中的数字+单位
                        m = re.search(
                            r'[~约]?\$?(' + _SIGN + r'[\d,，\.]+)\s*'
                            r'(亿[元美港]?元?|万亿|[xX倍]|%|[BMT])?',
                            cell
                        )
                        if m:
                            val = _clean_num(m.group(1))
                            unit = (m.group(2) or '').strip()
                            if val is not None and val != 0 and abs(val) < 1e15:
                                results.append((row_label, col_header, val, unit, i + 1, dline, heading))
                    i += 1
                continue
        i += 1
    return results


_COUNT_WORD_RE = re.compile(r'([\d,，]+(?:\.\d+)?)\s*(?:位|人|家|个|次|份|篇|颗|辆|城|款|条|年|季|月|日|天|周|小时)')


def _looks_like_year_or_count(val, unit: str, raw: str) -> bool:
    """无单位的 1900–2100 整数当年份；"54 位分析师""3,067 辆"这类计数不是财务数据点。"""
    if unit == '' and float(val).is_integer() and 1900 <= val <= 2100:
        return True
    # "FY26 / Q4 / H1 / FY2027" 这类期间前缀里的数字不是数据点
    if float(val).is_integer() and re.search(r'(?:FY|Q|H)\s*' + str(int(val)) + r'(?!\d)', raw):
        return True
    for m in _COUNT_WORD_RE.finditer(raw):
        n = _clean_num(m.group(1))
        if n is not None and abs(n - val) < 1e-9:
            return True
    return False


# 流程元数据表（复核记录 / 版本对照 / 成本统计 / 抽查与双算表 / 更新记录）里的数字与公司无关，
# 抽进样本只会核到"壁钟 7.4 分钟"这类东西，白占 15% 的抽检名额。
_META_HEADING_RE = re.compile(r'复核记录|对照|成本|更新记录|抽查|双算|证据|进度|token|壁钟|信源|来源分级|数据来源|方法论|交叉质询', re.I)
_META_ROW_RE = re.compile(r'token|壁钟|分钟|行数|工具调用|子\s*Agent|\bKB\b|版本|v\d\s*[（(→]', re.I)


def extract_data_points(md_text: str) -> list:
    """从 Markdown 报告中提取所有可识别的财务数据点。

    覆盖三类结构：
      1. 多列 Markdown 表格（最主要的来源）：(行标签 + 列标题) → 数值
      2. 带冒号的 KV 行：标签：数值 单位
      3. 加粗数字行：**数值** 单位

    返回 list of dict：
      {id, label, reported_value, unit, raw_text, line_number}
    """
    points = []
    seen = set()

    def _add(label, val, unit, lineno, raw):
        label = re.sub(r'[\*_`]+', '', label).strip()
        if not _is_valid_label(label):
            return
        # 评分 / 判断 / 建议阈值不是可外部核验的数据点，抽进样本只会制造假"通过"
        if '★' in raw or _NON_DATA_LABEL_RE.search(label):
            return
        if val is None or val == 0 or abs(val) > 1e15:
            return
        # 过滤纯年份/季度
        if re.fullmatch(r'(20\d{2}|Q[1-4]|\d{4}\s*Q[1-4])', label.strip()):
            return
        key = f"{label}|{round(val,4)}|{unit}"
        if key in seen:
            return
        seen.add(key)
        points.append({
            'id': len(points) + 1,
            'label': label,
            'reported_value': val,
            'unit': unit,
            'raw_text': raw[:120],
            'line_number': lineno,
        })

    lines = md_text.split('\n')
    in_code = False

    # --- 1. 多列表格 ---
    for row_label, col_header, val, unit, lineno, raw, heading in _parse_md_tables(lines):
        # 流程元数据表不是公司数据
        if _META_HEADING_RE.search(heading) or _META_ROW_RE.search(row_label):
            continue
        # 跳过无意义行标签
        if not _is_valid_label(row_label):
            continue
        # 跳过无意义列标题（YoY增速列单独标注，不作为待核验数据）
        if col_header.upper() in ('YOY', 'YOY增速', '增速', '同比', '变化', '趋势', '说明', '备注'):
            continue
        # "来源 / 说明""口径""角色""期末"这类列装的是注释，不是待核验数据
        if any(k in col_header for k in ('来源', '说明', '备注', '口径', '角色', '期末', '日期', '更新', '同业', '对比', '可比', '对照', '算式', '工具', '依据')):
            continue
        if _looks_like_year_or_count(val, unit, raw):
            continue
        # label = "行标签 · 列标题"（若列标题是行标签的补充）
        if col_header and col_header != row_label:
            label = f"{row_label} · {col_header}"
        else:
            label = row_label
        _add(label, val, unit, lineno, raw)

    # --- 2. KV 冒号行 ---
    for lineno, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith('```'):
            in_code = not in_code
            continue
        if in_code or stripped.startswith('> ') or re.match(r'^#{1,6}\s', stripped):
            continue
        if '|' in stripped:
            continue  # 表格已在上面处理

        for m in _KV_LABEL_RE.finditer(stripped):
            label = m.group('label')
            val = _clean_num(m.group('num'))
            unit = (m.group('unit') or '').strip()
            if val is not None and _looks_like_year_or_count(val, unit, stripped):
                continue
            _add(label, val, unit, lineno, stripped)

    return points


# 一手引用标记：行内出现这些字样，说明该数据点声称有一手出处，抽到它才能核"引对了没有"
_PRIMARY_CITE_RE = re.compile(
    r'10-K|10-Q|8-K|20-F|6-K|DEF\s*14A|424B|S-1|F-1|Exhibit|Ex\.?\s*99|Note\s*\d|附注|年报|季报|中报|半年报|'
    r'招股|公告|法院|判决|裁定|SEC|港交所|披露易|巨潮|FinMind|XBRL', re.I)
_DERIVED_LABEL_RE = re.compile(r'(?<![A-Za-z])(?:PE|PB|PS|EV)(?![A-Za-z])|EV/|利润率|收益率|CAGR|IRR|增速|周转|稀释|隐含|折现|内在价值|安全边际')

_STRATUM_NAME = {'derived': '派生', 'primary': '一手引用', 'random': '随机'}


def _stratum(p: dict) -> str:
    """派生指标（算出来的数）与带一手引用的数据点各成一层——纯随机几乎抽不到它们，
    而实测出错的恰恰是这两类（稀释率错 20 倍、引错文件）。"""
    if _DERIVED_LABEL_RE.search(p['label']) or _DERIVED_RE.search(p.get('raw_text', '')):
        return 'derived'
    if _PRIMARY_CITE_RE.search(p.get('raw_text', '')):
        return 'primary'
    return 'random'


def sample_points(points: list, ratio: float = 0.15, seed: int = None,
                  stratify: bool = True, min_derived: int = 2, min_primary: int = 2) -> list:
    """随机抽取 ratio 比例的数据点，最少 3 个，最多 30 个。

    stratify=True（默认）时分层：随机样本之外强制补足 min_derived 个派生指标点与
    min_primary 个带一手引用的点。纯随机抽到的多是营收、净利这类最不容易错的表格数字。
    """
    for p in points:
        p['stratum'] = _stratum(p)
    n = max(3, min(30, math.ceil(len(points) * ratio)))
    n = min(n, len(points))
    rng = Random(seed)
    sampled = rng.sample(points, n)
    if stratify:
        chosen = {p['id'] for p in sampled}
        for stratum, need in (('derived', min_derived), ('primary', min_primary)):
            have = sum(1 for p in sampled if p['stratum'] == stratum)
            pool = [p for p in points if p['stratum'] == stratum and p['id'] not in chosen]
            rng.shuffle(pool)
            for p in pool[:max(0, need - have)]:
                sampled.append(p)
                chosen.add(p['id'])
    # 按行号排序，方便人工比对
    return sorted(sampled, key=lambda p: p['line_number'])


# ---------------------------------------------------------------------------
# 准出/打回判决
# ---------------------------------------------------------------------------

_TOLERANCE = 0.01   # 1% 容差


def _pct_diff(reported: float, fetched: float) -> float:
    """相对偏差 (absolute)。"""
    if reported == 0:
        return 0.0 if fetched == 0 else float('inf')
    return abs(reported - fetched) / abs(reported)


def render_verdict(results: list, report_name: str = "") -> dict:
    """
    根据核验结果输出准出/打回判决。

    results: list of dict，每项包含：
      - id, label, reported_value, unit, fetched_value, fetched_source
      - (可选) fetched_value2, fetched_source2   ← 第二来源

    返回：
      {
        'verdict': 'PASS' | 'FAIL',
        'pass_count': int,
        'fail_count': int,
        'total': int,
        'fail_items': [...],
        'summary': str,
      }
    """
    BOLD = '\033[1m'
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RESET = '\033[0m'

    print('=' * 70)
    print(f'{BOLD}报告数据抽检 — 准出/打回判决{RESET}')
    if report_name:
        print(f'报告：{report_name}')
    print('=' * 70)
    print()

    fail_items = []
    warn_items = []

    for item in results:
        label = item.get('label', '?')
        reported = float(item.get('reported_value', 0))
        unit = item.get('unit', '')
        fetched = item.get('fetched_value')
        source = item.get('fetched_source', '?')
        fetched2 = item.get('fetched_value2')
        source2 = item.get('fetched_source2', '')

        # --- 主来源比对 ---
        if fetched is None:
            # 没有提供核验值 → 跳过（不计入通过/失败）
            print(f'  ⬜ [{item["id"]:>2}] {label[:35]:35s} {reported:>12.2f} {unit}  →  [未提供核验值，跳过]')
            continue

        fetched = float(fetched)
        diff1 = _pct_diff(reported, fetched)

        # --- 第二来源比对（如有）---
        diff2 = None
        if fetched2 is not None:
            fetched2 = float(fetched2)
            diff2 = _pct_diff(reported, fetched2)

        # 判断
        pass1 = diff1 <= _TOLERANCE
        pass2 = (diff2 is None) or (diff2 <= _TOLERANCE)

        if pass1 and pass2:
            status = f'{GREEN}✅ 通过{RESET}'
            detail = f'{source}: {fetched:.2f} (偏差 {diff1*100:.2f}%)'
            if diff2 is not None:
                detail += f'  |  {source2}: {fetched2:.2f} (偏差 {diff2*100:.2f}%)'
        elif not pass1 and not pass2:
            status = f'{RED}❌ 不通过{RESET}'
            detail = f'{source}: {fetched:.2f} (偏差 {diff1*100:.2f}%)'
            if diff2 is not None:
                detail += f'  |  {source2}: {fetched2:.2f} (偏差 {diff2*100:.2f}%)'
            fail_items.append({
                'id': item['id'],
                'label': label,
                'reported': reported,
                'unit': unit,
                'fetched': fetched,
                'source': source,
                'fetched2': fetched2,
                'source2': source2,
                'diff1_pct': round(diff1 * 100, 2),
                'diff2_pct': round(diff2 * 100, 2) if diff2 is not None else None,
                'raw_text': item.get('raw_text', ''),
                'line_number': item.get('line_number', 0),
            })
        else:
            # 一个来源通过，一个不通过 → 警告，不计入失败
            status = f'{YELLOW}⚠️  警告{RESET}'
            detail = f'{source}: {fetched:.2f} (偏差 {diff1*100:.2f}%)'
            if diff2 is not None:
                detail += f'  |  {source2}: {fetched2:.2f} (偏差 {diff2*100:.2f}%)'
            warn_items.append({
                'id': item['id'], 'label': label,
                'reported': reported, 'unit': unit,
                'diff1_pct': round(diff1 * 100, 2),
                'diff2_pct': round(diff2 * 100, 2) if diff2 is not None else None,
            })

        print(f'  {status} [{item["id"]:>2}] {label[:35]:35s}  报告: {reported:>12.2f} {unit}')
        print(f'              {" " * 38}{detail}')

    print()
    print('-' * 70)

    total = len([r for r in results if r.get('fetched_value') is not None])
    fail_count = len(fail_items)
    warn_count = len(warn_items)
    pass_count = total - fail_count - warn_count

    print(f'  抽检总数: {total}  |  通过: {GREEN}{pass_count}{RESET}  |  警告: {YELLOW}{warn_count}{RESET}  |  不通过: {RED}{fail_count}{RESET}')
    print()

    if fail_count == 0:
        print(f'{BOLD}{GREEN}【准出】所有抽检数据通过，报告可发布。{RESET}')
        verdict = 'PASS'
    else:
        print(f'{BOLD}{RED}【打回】{fail_count} 个数据点核验不通过，报告需修正后重审。{RESET}')
        print()
        print(f'{BOLD}打回原因：{RESET}')
        for fi in fail_items:
            print(f'  ❌ 第 {fi["line_number"]} 行 | {fi["label"]}')
            print(f'     报告值：{fi["reported"]} {fi["unit"]}')
            print(f'     {fi["source"]}：{fi["fetched"]}  （偏差 {fi["diff1_pct"]}%）')
            if fi.get('fetched2') is not None:
                print(f'     {fi["source2"]}：{fi["fetched2"]}  （偏差 {fi["diff2_pct"]}%）')
            print(f'     原文：{fi["raw_text"][:80]}')
            print()
        verdict = 'FAIL'

    if warn_count > 0:
        print(f'{YELLOW}注意：{warn_count} 个数据点两来源结果不一致（超过1%），可能是口径差异（GAAP/Non-GAAP或汇率），请人工复核。{RESET}')
        for wi in warn_items:
            print(f'  ⚠️  {wi["label"]}  报告:{wi["reported"]} {wi["unit"]}  偏差: {wi["diff1_pct"]}% / {wi["diff2_pct"]}%')

    print('=' * 70)

    return {
        'verdict': verdict,
        'pass_count': pass_count,
        'warn_count': warn_count,
        'fail_count': fail_count,
        'total': total,
        'fail_items': fail_items,
        'warn_items': warn_items,
    }


# ---------------------------------------------------------------------------
# 一致性检查：多份底稿之间的关键事实是否打架
# ---------------------------------------------------------------------------

# 单位 → 亿 的换算系数（货币类）
_UNIT_TO_YI = {'亿': 1.0, '万亿': 10000.0, 'B': 10.0, 'T': 10000.0, 'M': 0.01, '': 1.0}

_NUM = r'([\d,，]+(?:\.\d+)?)'
_RANGE_SEP = r'\s*[-–~—至]\s*'

# 关键字段：名称 → (正则, 是否区间, 容差)
# 正则需捕获 1 个数（或区间 2 个数）+ 可选单位组；默认容差 1%
_KEY_FIELDS = [
    ('资本开支指引',
     r'(?:资本开支|Capex|capex|CapEx|资本支出)[^\n|]{0,40}?指引[^\n|]{0,40}?\$?\s*' + _NUM + _RANGE_SEP + r'\$?\s*' + _NUM + r'\s*(万亿|亿|B|T)',
     True, 0.01),
    # 现价：排除"目标/隐含/情景"股价，排除后接货币单位（那是市值）和"左右/分歧"（那是价差描述）
    ('股价',
     r'(?<!目标)(?<!隐含)(?<!情景)(?<!转)(?<!触发)(?<!上限)(?<!下限)股价(?![^\d\n]{0,25}(?:目标|分歧|价差|、|区间|最低|最高|日线|年内|买点|买入|悲观|乐观|中性|压力|折价|情景|跌至|一度|曾|涨至|升至|高点|低点))[^\d\n]{0,25}\$\s*' + _NUM + r'()(?![\d\.])(?![,，]\d)(?!\s*[万亿BT])(?!\s*左右)',
     False, 0.02),
    ('总股本',
     r'总股本(?![^\d\n]{0,20}、)[^\d\n]{0,20}' + _NUM + r'\s*(亿|B)',
     False, 0.01),
    # 市值：排除"蒸发/缩水/增减"等变动量描述
    ('市值',
     r'(?<!回购)(?<!回购总)(?<!回购的)市值(?![^\d\n]{0,20}(?:蒸发|缩水|损失|增|减|变|、))[^\d\n]{0,20}\$?\s*' + _NUM + r'\s*(万亿|亿|T|B)(?!\s*股)',
     False, 0.02),
    # 净现金：排除"剔除/扣除净现金后"的算式行
    ('净现金',
     r'(?<!剔除)(?<!扣除)(?<!减去)(?<!金融)(?<!真实)(?<!真实的)净现金(?![^\d\n]{0,20}(?:[、，,]|金融|拆))[^\d\n]{0,20}\$?\s*' + _NUM + r'\s*(万亿|亿|T|B)',
     False, 0.01),
]

# 同一行既提旧指引又提新指引（上调/下调/原为…）时，只取该行最后一个区间
_REVISION_HINT_RE = re.compile(r'上调|下调|上修|下修|调高|调低|原为|原指引|旧指引|更新前|此前')
# 复核记录 / 勘误类的元描述行，本身就在罗列错误值，整行不计
_META_LINE_RE = re.compile(r'复核|订正|勘误|各不相同|误写|误引|原稿')
_GENERIC_RANGE_RE = re.compile(r'\$?\s*' + _NUM + _RANGE_SEP + r'\$?\s*' + _NUM + r'\s*(万亿|亿|B|T)')


_OLD_BEFORE_RE = re.compile(r'(?:原|旧|由|从|年初|此前|更新前)[^\d$]{0,6}$')
_OLD_AFTER_RE = re.compile(r'^\s*(?:两次|再次|多次|再度)?\s*(?:上调|下调|上修|下修|调高|调低|调整|修正)')


def _looks_old(line: str, m) -> bool:
    """修订语境里，判断某个区间是否是被替换掉的旧值。"""
    before = line[max(0, m.start() - 10):m.start()]
    after = line[m.end():m.end() + 8]
    return bool(_OLD_BEFORE_RE.search(before) or _OLD_AFTER_RE.search(after))


_CELL_NUM_RE = re.compile(r'\$?\s*[\d,，]+(?:\.\d+)?\s*(?:万亿|亿|[BMT]|%|x)?')


def _is_peer_table_row(line: str) -> bool:
    """表格行且行标签之后有 ≥3 个数值单元格 → 视为多公司/多期横评，不做单值一致性比对。"""
    st = line.strip()
    if not st.startswith('|'):
        return False
    cells = [c.strip() for c in st.strip('|').split('|')]
    if len(cells) < 4:
        return False
    numeric = sum(1 for c in cells[1:] if c and _CELL_NUM_RE.fullmatch(c.strip('*_~ ')))
    return numeric >= 3


def _to_yi(num_str: str, unit: str) -> float:
    v = _clean_num(num_str)
    if v is None:
        return None
    return v * _UNIT_TO_YI.get(unit or '', 1.0)


# 行级排除：这些行里的"股价 / 净现金"不是对现值的陈述
#   股价：加减仓阈值、历史低点、情景价位——是条件不是快照
#   净现金：并列多口径、解释口径差异的行——口径问题交给 crosscheck 的口径标注处理，不在这里判冲突
_FIELD_LINE_EXCLUDE = {
    '股价': re.compile(r'加仓|减仓|买入|卖出|信号|低点|高点|历史|若|如果|跌|涨|回落|低于|高于|≤|≥|<|>|触发|情景|区间|锚|目标|临界'),
    '净现金': re.compile(r'口径|狭义|广义|租赁|旧值|漏|扣除|剔除|再扣|含融资|须拆|其中'),
}


def collect_key_facts(files: list, fields=None) -> dict:
    """返回 {字段名: [ {file, line, raw, value, display} ... ]}。"""
    fields = fields or _KEY_FIELDS
    out = {name: [] for name, *_ in fields}
    for path in files:
        try:
            with open(path, 'r', encoding='utf-8') as f:
                lines = f.read().split('\n')
        except OSError:
            continue
        in_code = False
        for lineno, line in enumerate(lines, start=1):
            if line.strip().startswith('```'):
                in_code = not in_code
                continue
            if in_code or _META_LINE_RE.search(line):
                continue          # 工具原始输出块 / 勘误行不计
            if _is_peer_table_row(line):
                continue          # 横评表：一行里是多家公司的数，不是本公司的
            for name, pat, is_range, _tol in fields:
                excl = _FIELD_LINE_EXCLUDE.get(name)
                if excl is not None and excl.search(line):
                    continue
                matches = list(re.finditer(pat, line))
                if name == '市值':
                    def _inside_paren(m):
                        gap = line[m.start():m.start(1)]
                        return ('(' in gap or '（' in gap) and not (')' in gap or '）' in gap)
                    matches = [m for m in matches if not _inside_paren(m)]
                    # "回购 X 亿股按现价计市值 …" 不是公司市值：看匹配点前 14 字
                    matches = [m for m in matches
                               if not re.search(r'回购|按现价|持仓|浮盈', line[max(0, m.start() - 14):m.start()])]
                if is_range and matches and _REVISION_HINT_RE.search(line):
                    # 该行在描述修订：字段命中即可，从行内所有区间里挑出"现行值"——
                    # 排除被 原/旧/由/从/年初 修饰、或紧跟"上调/下调"的旧值
                    generic = list(_GENERIC_RANGE_RE.finditer(line))
                    current = [m for m in generic if not _looks_old(line, m)]
                    if current:
                        matches = current[-1:]
                    elif generic:
                        matches = generic[-1:]
                for m in matches:
                    g = m.groups()
                    if is_range:
                        lo, hi, unit = _to_yi(g[0], g[2]), _to_yi(g[1], g[2]), g[2] or ''
                        if lo is None or hi is None:
                            continue
                        value = (lo + hi) / 2
                        display = f'{g[0]}–{g[1]}{unit}'
                    else:
                        unit = g[1] if len(g) > 1 and g[1] else ''
                        value = _to_yi(g[0], unit) if unit != '%' else _clean_num(g[0])
                        if value is None:
                            continue
                        display = f'{g[0]}{unit}'
                    out[name].append({
                        'file': os.path.basename(path), 'line': lineno,
                        'raw': line.strip()[:100], 'value': value, 'display': display,
                    })
    return out


def render_consistency(facts: dict, fields=None) -> dict:
    """打印各字段跨文件对照，返回 {'conflicts': [...], 'checked': n}。"""
    fields = fields or _KEY_FIELDS
    tol_by_name = {name: tol for name, _p, _r, tol in fields}
    BOLD, RED, GREEN, YELLOW, RESET = '\033[1m', '\033[91m', '\033[92m', '\033[93m', '\033[0m'

    print('=' * 70)
    print(f'{BOLD}底稿一致性检查 — 关键事实跨文件对照{RESET}')
    print('=' * 70)
    conflicts = []
    checked = 0
    for name, hits in facts.items():
        if not hits:
            continue
        checked += 1
        vals = [h['value'] for h in hits]
        lo, hi = min(vals), max(vals)
        spread = (hi - lo) / abs(lo) if lo else (0.0 if hi == 0 else float('inf'))
        tol = tol_by_name.get(name, 0.01)
        files_involved = sorted({h['file'] for h in hits})
        if spread <= tol:
            print(f'  {GREEN}✅{RESET} {name:<8s} 一致（{len(hits)} 处，{len(files_involved)} 个文件）：{hits[0]["display"]}')
            continue
        print(f'  {RED}❌{RESET} {name:<8s} 冲突（跨度 {spread*100:.1f}% > 容差 {tol*100:.0f}%）')
        for h in sorted(hits, key=lambda x: (x['file'], x['line'])):
            print(f'       {h["file"]}:{h["line"]:<4d} {h["display"]:<22s} | {h["raw"][:70]}')
        conflicts.append({'field': name, 'spread_pct': round(spread * 100, 2), 'hits': hits})
    print('-' * 70)
    if not conflicts:
        print(f'{BOLD}{GREEN}【一致】{checked} 个关键字段跨文件无冲突。{RESET}')
    else:
        print(f'{BOLD}{RED}【冲突】{len(conflicts)} 个关键字段在不同文件里数值不一致，汇总前先统一底稿。{RESET}')
    print('=' * 70)
    return {'conflicts': conflicts, 'checked': checked}


# ---------------------------------------------------------------------------
# 双算复核：关键派生指标必须由两个角色各算一次
#
# 背景：跨文件一致性（consistency）只能发现"两份报告数字不同"，
# 发现不了"一份报告自己算错了"。实测中曾出现优先股稀释率算错 20 倍
# （漏除存托股 1/20），consistency 与 lint 均无法察觉，只有换一个角色
# 独立重算才暴露。因此关键派生指标（核心EPS、PE、稀释率、CAGR、IRR…）
# 必须在至少两份报告的「双算复核表」中各出现一次，由本命令比对。
#
# 报告中的表格格式（标题含"双算"二字即可被识别）：
#   ## 双算复核表
#   | 指标 | 本报告值 | 算式 / 工具 |
#   |---|---|---|
#   | 核心TTM EPS | 10.11 | financial_rigor calc '138.28/12.23' |
# ---------------------------------------------------------------------------

_DUAL_HEADING_RE = re.compile(r'^#{1,6}\s*.*双算.*$')
_DUAL_SKIP_LABEL = {'指标', '项目', '名称', '口径'}

# 指标字典：双算表里的指标名必须落到这些基名上，工具才能跨角色分组。
# 各角色随手起名（"核心TTM EPS" / "TTM 核心 EPS" / "核心每股收益"）是双算永远配不上对的首要原因；
# 字典外的指标只作信息展示，不参与打回判定。用 `crosscheck --list-metrics` 打印本表。
_DUAL_CANON = {
    '核心EPS':   ('核心TTMEPS', 'TTM核心EPS', '核心每股收益', '调整后EPS', 'NonGAAPEPS', 'Non-GAAPEPS'),
    '核心PE':    ('TTM核心PE', '核心市盈率', '调整后PE', 'NonGAAPPE', 'Non-GAAPPE'),
    '市值':      ('总市值', '市值验算', '当前市值'),
    'FCF利润率': ('FCFmargin', '自由现金流利润率', 'FCF率', 'FCF/收入', 'FCF/营收'),
    '隐含增速':  ('隐含增长率', '反向折现隐含增速', '现价隐含增速', '隐含年增速', '隐含CAGR'),
    '稀释率':    ('稀释比例', '年稀释率', '股权稀释率'),
    '净现金':    ('狭义净现金', '净现金头寸'),
    '回购均价':  ('平均回购价', '回购平均价格'),
    'TAC率':     ('TAC/总收入', 'TAC占比', 'TAC/收入'),
    '份额变化':  ('市场份额变化', '份额增减', '份额变动'),
    '收入增速':  ('营收增速', '收入同比', '营收同比'),
    '分部利润率': ('分部经营利润率', '分部营业利润率'),
    '单位经济':  ('UE', '单位经济指标', '单均利润'),
}
# 口径敏感指标：值不同但有人没标口径时，先判"口径未标"而不是"算错"
_QUALIFIER_SENSITIVE = {'净现金', '市值', '核心EPS', '核心PE', '隐含增速', 'TAC率', '稀释率', '收入增速', '分部利润率'}


def _canon_metric(base: str):
    """把归一后的基名映射到字典基名；返回 (基名, 是否在字典内)。"""
    low = base.replace(' ', '').lower()
    for canon, aliases in _DUAL_CANON.items():
        if low == canon.lower() or low in {a.lower() for a in aliases}:
            return canon, True
    for canon in _DUAL_CANON:           # "TTM核心PE" / "核心PE①" 这类带前缀的写法
        if low.endswith(canon.lower()):
            return canon, True
    return base, False


def list_dual_metrics():
    print('双算复核表可用的指标名（基名；口径写在括号内，如「净现金(狭义)」）：')
    for canon, aliases in _DUAL_CANON.items():
        sens = '  ※口径敏感，必须带括号口径' if canon in _QUALIFIER_SENSITIVE else ''
        print(f'  {canon:<10s} 别名：{"、".join(aliases)}{sens}')
    print('字典外的指标可以写，但只作信息展示，不参与"打回"判定。')


def _norm_metric(s: str) -> str:
    """指标名归一：去 markdown 记号与空白，全角括号转半角，便于跨文件匹配。"""
    s = re.sub(r'[\*_`~\s]+', '', s)
    s = s.replace('（', '(').replace('）', ')')
    # 去掉尾部的编号/并列标记："核心PE①/②" → "核心PE"
    return re.sub(r'[①②③④⑤⑥⑦⑧⑨/／、，,]+$', '', s)


def _split_metric(label: str):
    """把「核心EPS(口径A)」拆成 (基名 '核心EPS', 口径 '口径A')——
    不同角色给同一指标加的括号说明常不一样，分组只看基名。"""
    m = re.match(r'^(.*?)\s*\((.*?)\)\s*$', label)
    if m:
        return m.group(1), m.group(2)
    return label, ''


def _tables_under(path, heading_re):
    """扫描文件中所有"标题命中 heading_re"之后紧邻的 Markdown 表格，逐行 yield (行号, 单元格列表)。"""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            lines = f.read().split('\n')
    except OSError:
        return
    i = 0
    while i < len(lines):
        if not heading_re.match(lines[i].strip()):
            i += 1
            continue
        j = i + 1
        while j < len(lines) and not lines[j].strip().startswith('|'):
            if lines[j].strip().startswith('#'):
                break
            j += 1
        while j < len(lines) and lines[j].strip().startswith('|'):
            cells = [c.strip() for c in lines[j].strip().strip('|').split('|')]
            j += 1
            if len(cells) >= 2 and not re.fullmatch(r'[\s\-:]+', cells[1]):
                yield j, cells
        i = j


def collect_dual_calc(files: list) -> dict:
    """抽取各文件「双算复核表」，返回 {指标: [{file, line, value, unit, formula}]}。"""
    out = {}
    for path in files:
        for j, cells in _tables_under(path, _DUAL_HEADING_RE):
                label = _norm_metric(cells[0])
                if not label or label in _DUAL_SKIP_LABEL:
                    continue
                label, qualifier = _split_metric(label)
                label, in_dict = _canon_metric(label)
                nums = re.findall(r'(' + _SIGN + r'[\d,，]+(?:\.\d+)?)\s*(万亿|亿|[BMT]|%|[xX]|倍)?', cells[1])
                vals = [(_clean_num(n), (u or '').lower().replace('倍', 'x')) for n, u in nums]
                vals = [(v, u) for v, u in vals if v is not None]
                if not vals:
                    continue
                out.setdefault(label, []).append({
                    'file': os.path.basename(path), 'line': j,
                    'value': vals[0][0], 'unit': vals[0][1],
                    'alt_values': [v for v, _ in vals[1:]],      # 同格并列的其他口径值
                    'qualifier': qualifier, 'in_dict': in_dict,
                    'formula': cells[2] if len(cells) > 2 else '',
                })
    return out


def render_crosscheck(dual: dict, required=None, tol: float = 0.01, verbose: bool = False) -> dict:
    BOLD, RED, GREEN, YELLOW, RESET = '\033[1m', '\033[91m', '\033[92m', '\033[93m', '\033[0m'
    print('=' * 70)
    print(f'{BOLD}双算复核 — 关键派生指标是否被两个角色各算一次{RESET}')
    print('=' * 70)

    if not dual:
        print(f'  {YELLOW}未找到任何「双算复核表」{RESET}——各视角报告里应有标题含"双算"二字的表格：')
        print('       | 指标 | 本报告值 | 算式 / 工具 |')
        print('  关键派生指标只被算一次时，算错了没有任何检查能发现（实测曾错 20 倍仍全检查通过）。')
        print('-' * 70)
        if required:
            print(f'{BOLD}{RED}【打回】要求双算的 {len(required)} 个指标一个都没有：{"、".join(required)}{RESET}')
        else:
            print(f'{BOLD}{YELLOW}【未执行】没有可比对的双算数据。{RESET}')
        print('=' * 70)
        return {'agreed': [], 'mismatch': [], 'single': [], 'missing': list(required or [])}

    mismatch, single, agreed, qual_diff, unqualified = [], [], [], [], []
    for metric, hits in sorted(dual.items()):
        files = {h['file'] for h in hits}
        vals = [h['value'] for h in hits]
        units = {h['unit'] for h in hits}
        if len(files) < 2:
            single.append((metric, hits))
            continue
        lo, hi = min(vals), max(vals)
        spread = abs(hi - lo) / abs(lo) if lo else (0.0 if hi == 0 else float('inf'))
        # 单元格并列多值（如"28.8x / 29.6x"两种剔法）：任一并列值与他人主值在容差内即视为一致
        if spread > tol:
            cands = [[h['value']] + h.get('alt_values', []) for h in hits]
            import itertools
            for combo in itertools.product(*cands):
                c_lo, c_hi = min(combo), max(combo)
                c_sp = abs(c_hi - c_lo) / abs(c_lo) if c_lo else float('inf')
                if c_sp <= tol:
                    spread = c_sp
                    break
        quals = {h.get('qualifier', '') for h in hits}
        if len(units) > 1 or spread > tol:
            if len(quals) > 1 and spread > tol:
                # 值不同、但各方标注的口径也不同 → 是口径分歧不是算错，提示统一口径
                print(f'  {YELLOW}≠{RESET} {metric:<22s} 口径不同（{" / ".join(q or "未标口径" for q in sorted(quals))}），不判不一致——汇总时并列并注明')
                for h in sorted(hits, key=lambda x: (x['file'], x['line'])):
                    print(f'       {h["file"]}:{h["line"]:<4d} {h["value"]}{h["unit"]:<4s} [{h.get("qualifier") or "—"}] | {h["formula"][:44]}')
                qual_diff.append({'metric': metric, 'hits': hits})
                continue
            if metric in _QUALIFIER_SENSITIVE and '' in quals and spread > tol:
                # 口径敏感指标有人没标口径（"净现金 572 亿" vs "净现金 1,414 亿"）：先补口径再判，不当算错
                print(f'  {YELLOW}?{RESET} {metric:<22s} 口径未标——该指标口径敏感，值不同但有角色未在括号内写口径，补齐后再判')
                for h in sorted(hits, key=lambda x: (x['file'], x['line'])):
                    print(f'       {h["file"]}:{h["line"]:<4d} {h["value"]}{h["unit"]:<4s} [{h.get("qualifier") or "未标口径"}] | {h["formula"][:44]}')
                unqualified.append({'metric': metric, 'hits': hits})
                continue
            reason = '单位不一致' if len(units) > 1 else f'偏差 {spread * 100:.1f}% > {tol * 100:.0f}%'
            print(f'  {RED}✗{RESET} {metric:<22s} {reason}')
            for h in sorted(hits, key=lambda x: (x['file'], x['line'])):
                print(f'       {h["file"]}:{h["line"]:<4d} {h["value"]}{h["unit"]:<4s} | {h["formula"][:56]}')
            mismatch.append({'metric': metric, 'spread_pct': round(spread * 100, 2), 'hits': hits})
        else:
            print(f'  {GREEN}✓{RESET} {metric:<22s} {hits[0]["value"]}{hits[0]["unit"]}  （{len(files)} 个角色独立算出，一致）')
            agreed.append(metric)

    missing = []
    if required:
        have = {m for m, h in dual.items() if len({x['file'] for x in h}) >= 2}
        for req in required:
            key, _ = _canon_metric(_norm_metric(req))
            if key not in have and not any(key in m or m in key for m in have):
                missing.append(req)

    single_dict = [(m, h) for m, h in single if h[0].get('in_dict')]
    single_other = [(m, h) for m, h in single if not h[0].get('in_dict')]
    if single_dict:
        print()
        print(f'  {YELLOW}字典内指标只有一个角色算过（未构成双算，应补算）：{RESET}')
        for metric, hits in single_dict:
            print(f'       {metric}  =  {hits[0]["value"]}{hits[0]["unit"]}   （仅 {hits[0]["file"]}）')
    if single_other:
        print()
        if verbose:
            print(f'  字典外指标（{len(single_other)} 个，仅信息，不参与判定）：')
            for metric, hits in single_other:
                print(f'       {metric}  =  {hits[0]["value"]}{hits[0]["unit"]}   （仅 {hits[0]["file"]}）')
        else:
            print(f'  另有 {len(single_other)} 个字典外指标仅单算（正常；加 --verbose 查看）')

    print('-' * 70)
    print(f'  双算一致: {GREEN}{len(agreed)}{RESET}  |  不一致: {RED}{len(mismatch)}{RESET}  '
          f'|  口径不同: {YELLOW}{len(qual_diff)}{RESET}  |  口径未标: {YELLOW}{len(unqualified)}{RESET}  '
          f'|  字典内单算: {YELLOW}{len(single_dict)}{RESET}  |  必算项缺失: {RED}{len(missing)}{RESET}')
    if missing:
        print(f'  {RED}以下必算指标没有被两个角色各算一次：{RESET}{"、".join(missing)}')
    if mismatch or missing:
        print(f'{BOLD}{RED}【打回】派生指标的双算未通过——先确认谁算错了，再汇总。{RESET}')
    else:
        print(f'{BOLD}{GREEN}【通过】双算指标全部一致。{RESET}')
    print('=' * 70)
    return {'agreed': agreed, 'mismatch': mismatch, 'qualifier_diff': [q['metric'] for q in qual_diff],
            'unqualified': [u['metric'] for u in unqualified],
            'single': [m for m, _ in single], 'missing': missing}


# ---------------------------------------------------------------------------
# 证据台账：把各视角的「底稿抽查表」汇总成一份可审计的账
#
# 单一事实源（共享底稿）的代价是：底稿错了，四份报告会同时继承。
# 解药是每个 Agent 抽查自己领域内的底稿条目、回查一手原文。本命令把这些
# 抽查结果收拢成台账，回答三个问题：
#   1. 底稿哪些条目被独立核过（可从 ⚠️ 升为 ✅，且留下了文件名/日期/核验人）
#   2. 哪些被证伪（必须回写底稿，否则下一轮继续错）
#   3. 哪些查了但核不到（保持 ⚠️，不得当作确定事实引用）
# ---------------------------------------------------------------------------

_EVIDENCE_HEADING_RE = re.compile(r'^#{1,6}\s*.*抽查.*$')
_VERDICT_RE = re.compile(r'(证实|证伪|核不到|无法核实|未能核实|不成立|成立|不属实|属实|有误|核实为真|为真|不符|矛盾|吻合|确认|相符|两值都对|都对|核实|数值正确|正确|一致|冲突|已更新|需更新|需订正|已订正)')
_EVID_SKIP_LABEL = {'底稿条目', '条目', '项目', '数据项'}
# 否定结论必须先于肯定词匹配："一手无法证实"含"证实"二字，不先拦会被记成证实
_VERDICT_NEG_RE = re.compile(r'无法证实|未能证实|不能证实|无法核实|未能核实|核不到|未见一手|未找到|查不到|无一手|无法取得|取不到')
# 一手来源列必须指向具体文件/URL："在 10-Q 里见过"不算，"10-Q 2025-12-31 Note 1（URL）"才算
_SPECIFIC_SRC_RE = re.compile(
    r'https?://|www\.|\.pdf|\.htm|10-K|10-Q|8-K|20-F|6-K|DEF\s*14A|424B|S-1|F-1|Exhibit|Ex\.?\s*99|Note\s*\d|'
    r'附注|年报|季报|中报|半年报|招股|公告|判决|裁定|法院|意见书|新闻稿|电话会|转录|transcript|投资者关系|'
    r'港交所|披露易|巨潮|FinMind|XBRL|财报|10-K/A|S-4|Form\s*\d', re.I)


def collect_evidence(files: list) -> list:
    """抽取各文件「底稿抽查表」。列序按 skill 模板：条目 | 原值 | 一手来源 | 核验日期 | 结论。"""
    rows = []
    for path in files:
        for line, cells in _tables_under(path, _EVIDENCE_HEADING_RE):
            item = re.sub(r'[\*_`~]+', '', cells[0]).strip()
            if not item or item in _EVID_SKIP_LABEL:
                continue
            joined = ' '.join(cells)
            m = _VERDICT_RE.search(joined)
            verdict = m.group(1) if m else '未标注'
            if _VERDICT_NEG_RE.search(' '.join(cells[3:]) if len(cells) > 3 else joined):
                verdict = '核不到'
            verdict = {'无法核实': '核不到', '未能核实': '核不到',
                       '成立': '证实', '属实': '证实', '核实为真': '证实', '为真': '证实',
                       '吻合': '证实', '确认': '证实', '相符': '证实', '两值都对': '证实', '都对': '证实', '核实': '证实',
                       '数值正确': '证实', '正确': '证实', '一致': '证实',
                       '冲突': '证伪', '已更新': '证伪', '需更新': '证伪', '需订正': '证伪', '已订正': '证伪',
                       '不成立': '证伪', '不属实': '证伪', '有误': '证伪', '不符': '证伪', '矛盾': '证伪'}.get(verdict, verdict)
            # 核验日期优先取「核验日期」列（模板第 4 列）；来源列里往往含文件自身的日期，
            # 直接扫全行会把"SEC FWP 2026-06-02"误当成核验日期。
            _DATE = r'20\d{2}[-/]\d{1,2}[-/]\d{1,2}'
            date = ''
            dm = re.search(_DATE, cells[3]) if len(cells) > 3 else None
            if dm is None:
                tail = ' '.join(cells[3:]) if len(cells) > 3 else ''
                dm = re.search(_DATE, tail) or (re.search(_DATE, joined) if len(cells) <= 3 else None)
            if dm:
                date = dm.group(0).replace('/', '-')
            # 既无结论也无核验日期的行不是抽查行（多半是标题含"抽查"的段落下顺带贴的数据表），不入账
            if verdict == '未标注' and not date:
                continue
            rows.append({
                'file': os.path.basename(path), 'line': line, 'item': item,
                'old_value': cells[1] if len(cells) > 1 else '',
                'source': cells[2] if len(cells) > 2 else '',
                'date': date, 'verdict': verdict,
                'raw': cells,
            })
    return rows


def render_evidence(rows: list, out_path: str = None, company: str = '') -> dict:
    BOLD, RED, GREEN, YELLOW, RESET = '\033[1m', '\033[91m', '\033[92m', '\033[93m', '\033[0m'
    print('=' * 70)
    print(f'{BOLD}证据台账 — 底稿抽查汇总{RESET}')
    print('=' * 70)
    if not rows:
        print(f'  {YELLOW}未找到任何「底稿抽查表」{RESET}——每份视角报告应有标题含"抽查"二字的表格：')
        print('       | 底稿条目 | 原值 | 一手来源（文件名/URL） | 核验日期 | 结论 |')
        print('  没有抽查，底稿的错会被四份报告同时继承且永远发现不了。')
        print('=' * 70)
        return {'rows': [], 'confirmed': 0, 'refuted': 0, 'unverifiable': 0, 'incomplete': []}

    # "证实"必须同时有具体一手来源与核验日期，否则降为"证实(凭据不全)"，按规则不得升 ✅
    for r in rows:
        if r['verdict'] == '证实':
            src_ok = bool(r['source'].strip()) and _SPECIFIC_SRC_RE.search(r['source']) is not None
            if not src_ok or not r['date']:
                r['verdict'] = '证实(凭据不全)'
                r['why'] = '来源不具体或为空' if not src_ok else '缺核验日期'
    buckets = {'证实': [], '证实(凭据不全)': [], '证伪': [], '核不到': [], '未标注': []}
    for r in rows:
        buckets[r['verdict']].append(r)
    incomplete = buckets['证实(凭据不全)']

    for v, mark, color in (('证伪', '✗', RED), ('核不到', '○', YELLOW),
                           ('证实', '✓', GREEN), ('证实(凭据不全)', '△', YELLOW), ('未标注', '?', YELLOW)):
        if not buckets[v]:
            continue
        print(f'  {color}{mark} {v}（{len(buckets[v])} 条）{RESET}')
        for r in buckets[v]:
            why = f'  ← {r["why"]}' if r.get('why') else ''
            print(f'       {r["file"]}:{r["line"]:<4d} {r["item"][:26]:<26s} {r["source"][:34]}{why}')
    print('-' * 70)
    print(f'  抽查 {len(rows)} 条  |  证实 {GREEN}{len(buckets["证实"])}{RESET}  '
          f'|  凭据不全 {YELLOW}{len(incomplete)}{RESET}  '
          f'|  证伪 {RED}{len(buckets["证伪"])}{RESET}  |  核不到 {YELLOW}{len(buckets["核不到"])}{RESET}')
    if buckets['证伪']:
        print(f'  {RED}证伪项必须回写底稿并订正引用它的报告，否则下一轮继续错。{RESET}')
    if incomplete:
        print(f'  {YELLOW}{len(incomplete)} 条标"证实"但来源不具体或缺核验日期 → 保持 ⚠️，不得升 ✅；'
              f'"在 10-Q 里见过"不是来源，要写文件名/URL + 附注号。{RESET}')

    if out_path:
        lines = [f'# {company or ""} 证据台账'.strip(), '',
                 f'> 由 `tools/report_audit.py evidence` 汇总自各视角报告的「底稿抽查表」，'
                 f'生成于 {date_mod.today().isoformat()}。',
                 '> 用途：底稿条目 ⚠️→✅ 的升级凭据（须同时有一手来源与核验日期）；证伪项须回写底稿。', '',
                 '| 结论 | 底稿条目 | 原值 | 一手来源 | 核验日期 | 出处 |', '|---|---|---|---|---|---|']
        for v in ('证伪', '核不到', '证实', '证实(凭据不全)', '未标注'):
            for r in buckets[v]:
                tag = f'{v}·{r["why"]}' if r.get('why') else v
                lines.append(f'| {tag} | {r["item"]} | {r["old_value"]} | {r["source"]} | '
                             f'{r["date"] or "—"} | {r["file"]}:{r["line"]} |')
        lines += ['', f'**合计**：抽查 {len(rows)} 条 — 证实 {len(buckets["证实"])}、'
                      f'证实但凭据不全 {len(incomplete)}、'
                      f'证伪 {len(buckets["证伪"])}、核不到 {len(buckets["核不到"])}、'
                      f'未标注 {len(buckets["未标注"])}。']
        if incomplete:
            lines.append(f'**{len(incomplete)} 条标"证实"但凭据不全**（来源不具体 / 缺核验日期），按规则保持 ⚠️，'
                         f'不得升 ✅。')
        with open(out_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')
        print(f'  台账已写入 {out_path}')
    print('=' * 70)
    return {'rows': rows, 'confirmed': len(buckets['证实']), 'refuted': len(buckets['证伪']),
            'unverifiable': len(buckets['核不到']), 'incomplete': incomplete}


# ---------------------------------------------------------------------------
# Lint：格式与纪律检查（对应 CLAUDE.md 的报告规范）
# ---------------------------------------------------------------------------

_LINT_RULES = [
    # (代码, 级别, 说明, 检查函数(line) -> bool)
    ('HALF-STAR', 'FAIL', '评分出现半星（CLAUDE.md：★1-5 不含半星）',
     lambda l: re.search(r'\d\.5\s*/\s*5|½|★+\s*\.5', l) is not None),
    ('STAR-COUNT', 'WARN', '星级写法异常（含☆时须凑满5个；★不得超过5个）',
     lambda l: any(len(t) > 5 or ('☆' in t and len(t) != 5) for t in re.findall(r'[★☆]{2,}', l))),
    ('SUBJECTIVE', 'FAIL', '主观表述（我认为/我觉得/显然）',
     lambda l: (not l.lstrip().startswith('>')) and re.search(r'我认为|我觉得|显然', l) is not None),
    ('GUIDANCE-NO-DATE', 'WARN', '指引类数字未标注日期/来源事件（可写"同上 / 见底稿 §N"指向底稿里的日期）',
     lambda l: (_GUIDE_NUM_RE.search(l) is not None and _GUIDE_REF_RE.search(l) is None)),
    ('UNIT-SLIP', 'WARN', '同一行两个"亿"数值呈 10× / 100× 关系，疑似单位错位',
     lambda l: _unit_slip(l)),
]


# "指引"后 40 字内出现带单位的数字才算指引数字；"分部、指引、一致预期"这类罗列名词不算
_GUIDE_NUM_RE = re.compile(r'指引[^\n|]{0,40}?' + _SIGN + r'\d[\d,，\.]*\s*(?:%|亿|万亿|[BMT](?![A-Za-z])|[–\-~～]\s*\d)')
_GUIDE_REF_RE = re.compile(r'20\d\d|截至|财报|电话会|上调|下调|同上|底稿|§|发布|指引日|新闻稿|8-K|（估计）|\(估计\)|估计值')

_DERIVED_RE = re.compile(
    r'稀释(?:率|约|了|比例)|稀释\s*[\d.]|核心\s*EPS|核心\s*PE|CAGR|IRR|隐含增速|'
    r'隐含.{0,6}增长|反向折现|FCF\s*(?:利润率|收益率)|EV/EBIT|内在价值|安全边际倍数')
_TOOL_RE = re.compile(r'financial_rigor|terminal_value|usstock_data|twstock_data|ashare_data')
# 论文/摘要类文件本身不做计算，只承接研究报告的结论——引用了来源报告即视为有证据链
_CITES_SOURCE_RE = re.compile(r'最终报告|数据底稿|/investment-team|/investment-research|0[1-4]-.{0,20}视角')


def _file_level_lint(text: str) -> list:
    """整文件级规则：派生指标必须留下工具验算痕迹。

    consistency 查跨文件冲突、逐行 lint 查格式，二者都发现不了"自己算错了"。
    本规则要求凡出现多类派生指标的报告，全文至少有一处工具调用/验算记录。
    """
    items = []
    kinds = len({m.group(0) for m in _DERIVED_RE.finditer(text)})
    if kinds >= 2 and not _TOOL_RE.search(text) and not _CITES_SOURCE_RE.search(text):
        items.append({
            'code': 'DERIVED-NO-CALC', 'level': 'WARN', 'line': 0,
            'desc': f'出现 {kinds} 类派生指标（稀释率/核心EPS/CAGR/IRR 等），但全文无工具验算痕迹——'
                    f'派生指标须用 tools/ 下的工具计算并保留算式，禁止心算',
            'raw': '',
        })
    return items


def _unit_slip(line: str) -> bool:
    nums = [_clean_num(x) for x in re.findall(r'\$\s*([\d,，]+(?:\.\d+)?)\s*亿', line)]
    nums = [n for n in nums if n]
    for i in range(len(nums)):
        for j in range(i + 1, len(nums)):
            a, b = sorted((abs(nums[i]), abs(nums[j])))
            if a and any(abs(b / a - k) / k < 0.005 for k in (10, 100)):
                return True
    return False


def lint_files(files: list) -> dict:
    BOLD, RED, YELLOW, GREEN, RESET = '\033[1m', '\033[91m', '\033[93m', '\033[92m', '\033[0m'
    print('=' * 70)
    print(f'{BOLD}报告 lint — 格式与纪律{RESET}')
    print('=' * 70)
    fails, warns = [], []
    for path in files:
        try:
            with open(path, 'r', encoding='utf-8') as f:
                lines = f.read().split('\n')
        except OSError:
            print(f'  ⬜ 无法读取：{path}')
            continue
        in_code = False
        for lineno, line in enumerate(lines, start=1):
            if line.strip().startswith('```'):
                in_code = not in_code
                continue
            if in_code:
                continue
            for code, level, desc, check in _LINT_RULES:
                try:
                    hit = check(line)
                except Exception:
                    hit = False
                if hit:
                    item = {'file': os.path.basename(path), 'line': lineno, 'code': code,
                            'desc': desc, 'raw': line.strip()[:90]}
                    (fails if level == 'FAIL' else warns).append(item)
        for fl in _file_level_lint('\n'.join(lines)):
            fl['file'] = os.path.basename(path)
            (fails if fl['level'] == 'FAIL' else warns).append(fl)
    for it in fails:
        print(f'  {RED}❌ {it["code"]:<16s}{RESET} {it["file"]}:{it["line"]}  {it["desc"]}')
        print(f'       {it["raw"]}')
    for it in warns:
        print(f'  {YELLOW}⚠️  {it["code"]:<16s}{RESET} {it["file"]}:{it["line"]}  {it["desc"]}')
        print(f'       {it["raw"]}')
    print('-' * 70)
    print(f'  文件: {len(files)}  |  不通过: {RED}{len(fails)}{RESET}  |  警告: {YELLOW}{len(warns)}{RESET}')
    if fails:
        print(f'{BOLD}{RED}【打回】修正上述 FAIL 项后再汇总。{RESET}')
    else:
        print(f'{BOLD}{GREEN}【通过】无 FAIL 项。{RESET}' + ('  警告项请人工过目。' if warns else ''))
    print('=' * 70)
    return {'fails': fails, 'warns': warns}


# ---------------------------------------------------------------------------
# 上轮复核记录 → 本轮抽查清单
#
# skill 规定"team-lead 复核记录的每一项自动进入下一轮底稿抽查清单"，但此前没有任何
# 机制读它。本命令把最终报告的「复核记录」表抽出来，按"上轮有没有填依据"排优先级，
# 输出可直接贴进 00-数据底稿.md 的表格。复核者自己也会错（实测把正确的 847.5 亿
# "订正"成 800 亿），所以上轮每一处订正都要在本轮被不同的人独立核一次。
# ---------------------------------------------------------------------------

_REVIEW_HEADING_RE = re.compile(r'^#{1,6}\s*.*复核记录.*$')
_EMPTY_CELL = {'', '—', '-', '无', '无需', 'n/a', 'N/A'}


def collect_review_items(path: str) -> list:
    rows = []
    for line, cells in _tables_under(path, _REVIEW_HEADING_RE):
        head = re.sub(r'[\*_`~]+', '', cells[0]).strip()
        if head in ('#', '序号', '编号', '事项') or re.fullmatch(r'[\s\-:]+', head):
            continue
        # 有编号列时事项在第 2 列，否则在第 1 列
        off = 1 if re.fullmatch(r'\d+', head) else 0
        get = lambda k: (cells[k + off].strip() if len(cells) > k + off else '')
        item, handling, basis, date = get(0), get(1), get(2), get(3)
        if not item:
            continue
        has_basis = basis not in _EMPTY_CELL and not re.fullmatch(r'[\s\-—]*', basis)
        if not re.search(r'20\d\d[-/]\d{1,2}', date):
            date = ''
        rows.append({'line': line, 'item': item, 'handling': handling, 'basis': basis,
                     'date': date, 'has_basis': has_basis})
    return rows


def render_review_items(rows: list, report: str, out_path: str = None) -> dict:
    lines = ['## 上轮 team-lead 复核记录 → 本轮底稿抽查清单', '',
             f'> 来源：`{report}` 的「复核记录」表。规则：上轮每一处订正都要在本轮被**不同角色**独立核一次；'
             f'上轮没填依据或没填核验日期的项优先级为高。',
             '', '| # | 事项 | 上轮处理 | 上轮依据 | 上轮核验日期 | 本轮优先级 | 本轮抽查人 | 本轮结论 |',
             '|---|---|---|---|---|---|---|---|']
    high = 0
    for i, r in enumerate(rows, start=1):
        if not r['has_basis'] or not r['date']:
            prio = '**高**（上轮' + ('未填依据' if not r['has_basis'] else '未填核验日期') + '）'
            high += 1
        else:
            prio = '普通'
        lines.append(f'| {i} | {r["item"]} | {r["handling"]} | {r["basis"] or "—"} | {r["date"] or "—"} | {prio} |  |  |')
    if not rows:
        lines.append('| — | （未在报告中找到「复核记录」表） |  |  |  |  |  |  |')
    lines += ['', f'共 {len(rows)} 项，其中 {high} 项上轮凭据不全须优先核。']
    text = '\n'.join(lines)
    print(text)
    if out_path:
        with open(out_path, 'a', encoding='utf-8') as f:
            f.write('\n' + text + '\n')
        print(f'\n已追加写入 {out_path}', file=sys.stderr)
    return {'items': rows, 'high_priority': high}


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------

def _force_utf8_stdio():
    """把 stdout/stderr 强制切到 UTF-8。

    Windows 控制台默认 GBK，报告里的 €、→、★ 等字符会让 print(json.dumps(...))
    抛 UnicodeEncodeError 直接崩溃。errors='replace' 保证极端字符也不中断流程。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass  # 非 TextIOWrapper（如被重定向到管道对象）时忽略


def main():
    _force_utf8_stdio()
    parser = argparse.ArgumentParser(
        description='Report Audit Tool — 研究报告数据抽检工具',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
工作流程：

  Step 1 — 提取数据点并随机抽样 15%，输出抽检清单：
    python3 tools/report_audit.py extract --report reports/腾讯/腾讯-research-20260408.md

  Step 2 — Claude 对清单中每个数据点，从可靠信源取数，
            填入 fetched_value / fetched_source / fetched_value2 / fetched_source2

  Step 3 — 输入核验结果，输出准出/打回判决：
    python3 tools/report_audit.py verdict --results '[
      {"id":1,"label":"营业收入","reported_value":7518,"unit":"亿","fetched_value":7518,"fetched_source":"macrotrends","fetched_value2":7500,"fetched_source2":"stockanalysis"},
      ...
    ]'

  一步预览（只打印抽检清单，不核验）：
    python3 tools/report_audit.py extract --report reports/xxx.md --dry-run

  指定抽样比例（默认0.15）：
    python3 tools/report_audit.py extract --report reports/xxx.md --ratio 0.20

  固定随机种子（复现同一批样本）：
    python3 tools/report_audit.py extract --report reports/xxx.md --seed 42

  多份底稿一致性检查（team-lead 汇总前必跑；指引/股价/股本/市值/净现金…跨文件对照）：
    python3 tools/report_audit.py consistency --dir reports/腾讯

  双算复核（关键派生指标必须两个角色各算一次；只查数字一致查不出"自己算错"）：
    python3 tools/report_audit.py crosscheck --dir reports/腾讯 \
      --require '核心EPS,核心PE,稀释率,FCF利润率,隐含增速'

  证据台账（汇总各视角的底稿抽查，作为 ⚠️→✅ 的升级凭据）：
    python3 tools/report_audit.py evidence --dir reports/腾讯 \
      --company 腾讯 --out reports/腾讯/00-证据台账.md

  格式与纪律 lint（半星 / 主观表述 / 无日期指引 / 单位错位 / 派生指标无算式）：
    python3 tools/report_audit.py lint reports/腾讯/0*.md

  上轮复核记录 → 本轮抽查清单（新一轮研究做底稿时先跑，贴进 00-数据底稿.md）：
    python3 tools/report_audit.py review-items --report reports/腾讯/最终报告.md

  双算表允许的指标名（各角色必须用字典名，否则配不上对）：
    python3 tools/report_audit.py crosscheck --list-metrics
        """)

    sub = parser.add_subparsers(dest='command')

    # extract
    ext = sub.add_parser('extract', help='从报告提取数据点并随机抽样')
    ext.add_argument('--report', required=True, help='报告文件路径（Markdown）')
    ext.add_argument('--ratio', type=float, default=0.15, help='抽样比例，默认 0.15')
    ext.add_argument('--seed', type=int, default=None, help='随机种子（可选，用于复现）')
    ext.add_argument('--dry-run', action='store_true', help='只打印，不输出 JSON')
    ext.add_argument('--no-stratify', action='store_true', help='关闭分层（默认强制补 2 个派生指标 + 2 条一手引用）')

    # verdict
    vrd = sub.add_parser('verdict', help='根据核验结果输出准出/打回判决')
    vrd.add_argument('--results', required=True, help='JSON 数组，含 fetched_value 等字段')
    vrd.add_argument('--report', default='', help='报告名称（可选，用于显示）')
    vrd.add_argument('--output-json', action='store_true', help='将判决结果以 JSON 输出到 stdout')

    # consistency
    con = sub.add_parser('consistency', help='多份底稿之间的关键事实一致性检查')
    con.add_argument('--dir', help='目录：检查其中所有 0*.md / 最终报告.md')
    con.add_argument('files', nargs='*', help='或直接列出文件')
    con.add_argument('--output-json', action='store_true')

    # crosscheck
    cc = sub.add_parser('crosscheck', help='双算复核：关键派生指标是否被两个角色各算一次')
    cc.add_argument('--dir', help='目录：检查其中所有 0*.md / 最终报告.md')
    cc.add_argument('files', nargs='*', help='或直接列出文件')
    cc.add_argument('--require', default='', help='必算指标，逗号分隔（缺一即打回）')
    cc.add_argument('--tolerance', type=float, default=0.01, help='容差，默认 1%%')
    cc.add_argument('--verbose', action='store_true', help='列出字典外的单算指标')
    cc.add_argument('--list-metrics', action='store_true', help='打印双算指标字典后退出')
    cc.add_argument('--output-json', action='store_true')

    # evidence
    ev = sub.add_parser('evidence', help='证据台账：汇总各视角的底稿抽查结果')
    ev.add_argument('--dir', help='目录：扫描其中所有 0*.md / 最终报告.md')
    ev.add_argument('files', nargs='*', help='或直接列出文件')
    ev.add_argument('--out', help='台账写入路径，如 reports/{公司}/00-证据台账.md')
    ev.add_argument('--company', default='', help='公司名（写进台账标题）')
    ev.add_argument('--output-json', action='store_true')

    # lint
    lnt = sub.add_parser('lint', help='格式与纪律 lint（半星/主观表述/无日期指引/单位错位/派生指标无算式）')
    lnt.add_argument('files', nargs='+', help='报告文件（可多个）')
    lnt.add_argument('--output-json', action='store_true')

    # review-items
    rv = sub.add_parser('review-items', help='把上轮最终报告的「复核记录」转成本轮底稿抽查清单')
    rv.add_argument('--report', required=True, help='上轮最终报告路径')
    rv.add_argument('--out', help='追加写入的文件（通常是本轮 00-数据底稿.md）')
    rv.add_argument('--output-json', action='store_true')

    args = parser.parse_args()

    if args.command == 'crosscheck' and args.list_metrics:
        list_dual_metrics()
        sys.exit(0)

    if args.command == 'consistency':
        files = list(args.files or [])
        if args.dir:
            import glob
            files += sorted(glob.glob(os.path.join(args.dir, '0*.md')))
            final = os.path.join(args.dir, '最终报告.md')
            if os.path.exists(final):
                files.append(final)
        files = [f for f in files if os.path.exists(f)]
        if not files:
            print('❌ 没有可检查的文件', file=sys.stderr)
            sys.exit(1)
        facts = collect_key_facts(files)
        outcome = render_consistency(facts)
        if args.output_json:
            print(json.dumps(outcome, ensure_ascii=False, indent=2))
        sys.exit(1 if outcome['conflicts'] else 0)

    elif args.command == 'crosscheck':
        files = list(args.files or [])
        if args.dir:
            import glob
            files += sorted(glob.glob(os.path.join(args.dir, '0*.md')))
            final = os.path.join(args.dir, '最终报告.md')
            if os.path.exists(final):
                files.append(final)
        files = [f for f in files if os.path.exists(f)]
        if not files:
            print('❌ 没有可检查的文件', file=sys.stderr)
            sys.exit(1)
        req = [x.strip() for x in args.require.split(',') if x.strip()]
        outcome = render_crosscheck(collect_dual_calc(files), req, args.tolerance, verbose=args.verbose)
        if args.output_json:
            print(json.dumps(outcome, ensure_ascii=False, indent=2))
        sys.exit(1 if (outcome['mismatch'] or outcome['missing']) else 0)

    elif args.command == 'evidence':
        files = list(args.files or [])
        if args.dir:
            import glob
            files += sorted(glob.glob(os.path.join(args.dir, '0*.md')))
            final = os.path.join(args.dir, '最终报告.md')
            if os.path.exists(final):
                files.append(final)
        files = [f for f in files if os.path.exists(f) and not f.endswith('证据台账.md')]
        if not files:
            print('❌ 没有可检查的文件', file=sys.stderr)
            sys.exit(1)
        outcome = render_evidence(collect_evidence(files), args.out, args.company)
        if args.output_json:
            print(json.dumps({k: v for k, v in outcome.items() if k != 'rows'},
                             ensure_ascii=False, indent=2, default=str))
        sys.exit(1 if outcome['refuted'] else 0)

    elif args.command == 'lint':
        outcome = lint_files(args.files)
        if args.output_json:
            print(json.dumps(outcome, ensure_ascii=False, indent=2))
        sys.exit(1 if outcome['fails'] else 0)

    elif args.command == 'review-items':
        if not os.path.exists(args.report):
            print(f'❌ 文件不存在: {args.report}', file=sys.stderr)
            sys.exit(1)
        outcome = render_review_items(collect_review_items(args.report), args.report, args.out)
        if args.output_json:
            print(json.dumps(outcome, ensure_ascii=False, indent=2))
        sys.exit(0)

    elif args.command == 'extract':
        if not os.path.exists(args.report):
            print(f'❌ 文件不存在: {args.report}', file=sys.stderr)
            sys.exit(1)

        with open(args.report, 'r', encoding='utf-8') as f:
            text = f.read()

        all_points = extract_data_points(text)
        sampled = sample_points(all_points, ratio=args.ratio, seed=args.seed, stratify=not args.no_stratify)

        print('=' * 70)
        print(f'报告数据抽检清单')
        print(f'文件：{args.report}')
        print(f'总提取数据点：{len(all_points)}  |  抽样比例：{args.ratio:.0%}  |  抽检数量：{len(sampled)}')
        if args.seed is not None:
            print(f'随机种子：{args.seed}（可用于复现同一批样本）')
        print('=' * 70)
        print()
        strata = {k: sum(1 for p in sampled if p.get('stratum') == k) for k in _STRATUM_NAME}
        print('分层：' + '  '.join(f'{_STRATUM_NAME[k]} {v}' for k, v in strata.items())
              + ('' if args.no_stratify else '  （派生指标与一手引用各至少 2 个，随机之外强制补足）'))
        print()
        print(f'{"ID":>3}  {"行号":>5}  {"层":<6}  {"数据标签":<35}  {"报告值":>12}  {"单位"}')
        print(f'{"─"*3}  {"─"*5}  {"─"*6}  {"─"*35}  {"─"*12}  {"─"*6}')
        for p in sampled:
            print(f'{p["id"]:>3}  {p["line_number"]:>5}  {_STRATUM_NAME.get(p.get("stratum"), ""):<6}  '
                  f'{p["label"][:35]:<35}  {p["reported_value"]:>12.2f}  {p["unit"]}')
        print()
        print('↑ 请对上述每个数据点，从以下信源取数，填入 fetched_value：')
        print('  美股：macrotrends.net（主）+ stockanalysis.com（副）')
        print('  港股：aastocks.com（主）+ macrotrends ADR（副）')
        print('  A股： eastmoney.com（主）+ cninfo.com.cn（副）')
        print()

        if not args.dry_run:
            # 输出可填写的 JSON 模板
            template = []
            for p in sampled:
                template.append({
                    'id': p['id'],
                    'label': p['label'],
                    'reported_value': p['reported_value'],
                    'unit': p['unit'],
                    'line_number': p['line_number'],
                    'raw_text': p['raw_text'],
                    'stratum': p.get('stratum', 'random'),
                    'fetched_value': None,       # ← 填入主来源核验值
                    'fetched_source': '',        # ← 填入主来源名称
                    'fetched_value2': None,      # ← 填入副来源核验值（可选）
                    'fetched_source2': '',       # ← 填入副来源名称（可选）
                })
            print('抽检清单 JSON（填入 fetched_value 后，传给 verdict 命令）：')
            print()
            print(json.dumps(template, ensure_ascii=False, indent=2))

    elif args.command == 'verdict':
        try:
            results = json.loads(args.results)
        except json.JSONDecodeError as e:
            print(f'❌ JSON 解析失败: {e}', file=sys.stderr)
            sys.exit(1)

        report_name = args.report or ''
        outcome = render_verdict(results, report_name=report_name)

        if args.output_json:
            print(json.dumps(outcome, ensure_ascii=False, indent=2))

        # 非零退出码表示打回，方便 CI/脚本判断
        sys.exit(0 if outcome['verdict'] == 'PASS' else 1)

    else:
        parser.print_help()


if __name__ == '__main__':
    main()
