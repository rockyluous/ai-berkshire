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
    """解析 Markdown 中所有表格，返回 (row_label, col_header, value, unit, lineno, raw) 列表。"""
    results = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
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
                                results.append((row_label, col_header, val, unit, i + 1, dline))
                    i += 1
                continue
        i += 1
    return results


_COUNT_WORD_RE = re.compile(r'([\d,，]+(?:\.\d+)?)\s*(?:位|人|家|个|次|份|篇|颗|辆|城|款|条|年|季|月|日|天|周|小时)')


def _looks_like_year_or_count(val, unit: str, raw: str) -> bool:
    """无单位的 1900–2100 整数当年份；"54 位分析师""3,067 辆"这类计数不是财务数据点。"""
    if unit == '' and float(val).is_integer() and 1900 <= val <= 2100:
        return True
    for m in _COUNT_WORD_RE.finditer(raw):
        n = _clean_num(m.group(1))
        if n is not None and abs(n - val) < 1e-9:
            return True
    return False


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
    for row_label, col_header, val, unit, lineno, raw in _parse_md_tables(lines):
        # 跳过无意义行标签
        if not _is_valid_label(row_label):
            continue
        # 跳过无意义列标题（YoY增速列单独标注，不作为待核验数据）
        if col_header.upper() in ('YOY', 'YOY增速', '增速', '同比', '变化', '趋势', '说明', '备注'):
            continue
        # "来源 / 说明""口径""角色""期末"这类列装的是注释，不是待核验数据
        if any(k in col_header for k in ('来源', '说明', '备注', '口径', '角色', '期末', '日期', '更新', '同业', '对比', '可比', '对照')):
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


def sample_points(points: list, ratio: float = 0.15, seed: int = None) -> list:
    """随机抽取 ratio 比例的数据点，最少 3 个，最多 30 个。"""
    n = max(3, min(30, math.ceil(len(points) * ratio)))
    n = min(n, len(points))
    rng = Random(seed)
    sampled = rng.sample(points, n)
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
     r'(?<!目标)(?<!隐含)(?<!情景)(?<!转)(?<!触发)(?<!上限)(?<!下限)股价(?![^\d\n]{0,25}(?:目标|分歧|价差|、))[^\d\n]{0,25}\$\s*' + _NUM + r'()(?![\d\.])(?![,，]\d)(?!\s*[万亿BT])(?!\s*左右)',
     False, 0.02),
    ('总股本',
     r'总股本(?![^\d\n]{0,20}、)[^\d\n]{0,20}' + _NUM + r'\s*(亿|B)',
     False, 0.01),
    # 市值：排除"蒸发/缩水/增减"等变动量描述
    ('市值',
     r'(?<!回购)(?<!回购总)(?<!回购的)市值(?![^\d\n]{0,20}(?:蒸发|缩水|损失|增|减|变|、))[^\d\n]{0,20}\$?\s*' + _NUM + r'\s*(万亿|亿|T|B)',
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
                matches = list(re.finditer(pat, line))
                if name == '市值':
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
    ('GUIDANCE-NO-DATE', 'WARN', '指引类数字未标注日期/来源事件',
     lambda l: ('指引' in l and re.search(r'\d', l) is not None
                and re.search(r'20\d\d|截至|财报|电话会|上调|下调', l) is None)),
    ('UNIT-SLIP', 'WARN', '同一行两个"亿"数值呈 10× / 100× 关系，疑似单位错位',
     lambda l: _unit_slip(l)),
]


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

  格式与纪律 lint（半星评分 / 主观表述 / 无日期指引 / 疑似单位错位）：
    python3 tools/report_audit.py lint reports/腾讯/0*.md
        """)

    sub = parser.add_subparsers(dest='command')

    # extract
    ext = sub.add_parser('extract', help='从报告提取数据点并随机抽样')
    ext.add_argument('--report', required=True, help='报告文件路径（Markdown）')
    ext.add_argument('--ratio', type=float, default=0.15, help='抽样比例，默认 0.15')
    ext.add_argument('--seed', type=int, default=None, help='随机种子（可选，用于复现）')
    ext.add_argument('--dry-run', action='store_true', help='只打印，不输出 JSON')

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

    # lint
    lnt = sub.add_parser('lint', help='格式与纪律 lint（半星/主观表述/无日期指引/单位错位）')
    lnt.add_argument('files', nargs='+', help='报告文件（可多个）')
    lnt.add_argument('--output-json', action='store_true')

    args = parser.parse_args()

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

    elif args.command == 'lint':
        outcome = lint_files(args.files)
        if args.output_json:
            print(json.dumps(outcome, ensure_ascii=False, indent=2))
        sys.exit(1 if outcome['fails'] else 0)

    elif args.command == 'extract':
        if not os.path.exists(args.report):
            print(f'❌ 文件不存在: {args.report}', file=sys.stderr)
            sys.exit(1)

        with open(args.report, 'r', encoding='utf-8') as f:
            text = f.read()

        all_points = extract_data_points(text)
        sampled = sample_points(all_points, ratio=args.ratio, seed=args.seed)

        print('=' * 70)
        print(f'报告数据抽检清单')
        print(f'文件：{args.report}')
        print(f'总提取数据点：{len(all_points)}  |  抽样比例：{args.ratio:.0%}  |  抽检数量：{len(sampled)}')
        if args.seed is not None:
            print(f'随机种子：{args.seed}（可用于复现同一批样本）')
        print('=' * 70)
        print()
        print(f'{"ID":>3}  {"行号":>5}  {"数据标签":<35}  {"报告值":>12}  {"单位"}')
        print(f'{"─"*3}  {"─"*5}  {"─"*35}  {"─"*12}  {"─"*6}')
        for p in sampled:
            print(f'{p["id"]:>3}  {p["line_number"]:>5}  {p["label"][:35]:<35}  {p["reported_value"]:>12.2f}  {p["unit"]}')
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
