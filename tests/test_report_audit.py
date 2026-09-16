#!/usr/bin/env python3
"""report_audit.py 回归测试.

覆盖两个已修复的缺陷：

  BUG-1  数字提取丢失负号
         7 处正则用 `([\\d,，\\.]+)` 捕获数字，无符号位。报告中的 "-1.72%"
         被提取为 1.72，核验时与信源的 -1.72 相比得出 200% 偏差，
         产出**假打回**。这是最危险的一类 bug：它不报错，只是悄悄
         把结论弄反。

  BUG-2  Windows 控制台 GBK 编码崩溃
         main() 中 print(json.dumps(...)) 遇到 €/→/★ 等字符抛
         UnicodeEncodeError 直接退出。

另覆盖 consistency / crosscheck / evidence / lint 子命令与抽样器对评分行的排除。

Zero external dependencies — 仅用 unittest，与 report_audit.py 本身保持一致。
运行：  python tests/test_report_audit.py
"""

import io
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'tools'))

import report_audit as R  # noqa: E402

_TOOLS = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'tools')


class TestCleanNumSign(unittest.TestCase):
    """BUG-1 的最小单元：_clean_num 必须识别各类正负号。"""

    def test_ascii_minus(self):
        self.assertEqual(R._clean_num('-1.72'), -1.72)

    def test_plain_positive(self):
        self.assertEqual(R._clean_num('1.72'), 1.72)

    def test_ascii_plus_is_stripped(self):
        self.assertEqual(R._clean_num('+3.5'), 3.5)

    def test_unicode_minus_u2212(self):
        """报告里常出现 U+2212 真减号，而非 ASCII 连字符。"""
        self.assertEqual(R._clean_num('−4.2'), -4.2)

    def test_en_dash_u2013(self):
        """Markdown 编辑器会把 - 自动转成 en-dash。"""
        self.assertEqual(R._clean_num('–5.1'), -5.1)

    def test_fullwidth_minus_uff0d(self):
        """中文输入法下的全角减号。"""
        self.assertEqual(R._clean_num('－6.3'), -6.3)

    def test_negative_with_thousands_separator(self):
        self.assertEqual(R._clean_num('-1,234.5'), -1234.5)

    def test_negative_with_fullwidth_comma(self):
        self.assertEqual(R._clean_num('-1，234.5'), -1234.5)

    def test_garbage_returns_none(self):
        self.assertIsNone(R._clean_num('n/a'))


class TestNegativeExtractionFromTable(unittest.TestCase):
    """BUG-1 的端到端复现：报告表格中的负数必须被完整提取。

    表格结构复刻自触发假打回的行情表：跌幅列写作 "-1.72%"，
    修复前会被提取成 1.72，与信源比对得出 200% 偏差。
    数据为虚构，仅用于固定格式。
    """

    MD = (
        "| 公司 | 代码 | 股价 | 当日 | PB |\n"
        "|---|---|---|---|---|\n"
        "| 示例公司甲 | 000001 | 27.40 | -1.72% | 1.03 |\n"
        "| 示例公司乙 | 000002 | 17.56 | -1.90% | 2.18 |\n"
        "| 示例公司丙 | 000003 | 12.78 | +1.11% | 1.79 |\n"
        "| 毛利率 | — | — | -3.10% | — |\n"
    )

    def setUp(self):
        self.values = {p['reported_value'] for p in R.extract_data_points(self.MD)}

    def test_extracts_negative_percentages(self):
        for expected in (-1.72, -1.9, -3.1):
            with self.subTest(value=expected):
                self.assertIn(
                    expected, self.values,
                    f"{expected} 未被提取 —— 负号很可能又被正则吃掉了")

    def test_does_not_flip_sign(self):
        """回归核心：绝不能把 -1.72 提成 +1.72。"""
        self.assertNotIn(1.72, self.values, "负号丢失：-1.72 被提取成了 1.72")
        self.assertNotIn(1.9, self.values, "负号丢失：-1.90 被提取成了 1.90")

    def test_plus_sign_normalised_to_positive(self):
        self.assertIn(1.11, self.values)

    def test_negative_count(self):
        self.assertEqual(
            len([v for v in self.values if v < 0]), 3,
            "应恰好提取到 3 个负值（-1.72 / -1.90 / -3.10）")


class TestAbsoluteValueGuard(unittest.TestCase):
    """`val > 1e15` 的上限判断对负数永远为假，须用 abs()。"""

    def test_huge_negative_is_filtered(self):
        md = (
            "| 项目 | 数值 |\n"
            "|---|---|\n"
            "| 异常值 | -9999999999999999 |\n"
            "| 正常值 | -12.5 |\n"
        )
        values = {p['reported_value'] for p in R.extract_data_points(md)}
        self.assertIn(-12.5, values)
        self.assertTrue(
            all(abs(v) < 1e15 for v in values),
            "超过 1e15 的负值未被过滤（abs() 守卫失效）")


class TestGbkStdoutSurvival(unittest.TestCase):
    """BUG-2：Windows GBK 控制台下含 € 的报告必须能正常 extract 而不崩溃。"""

    MD = (
        "# 测试报告\n\n"
        "| 公司 | FY25营收 | 毛利率 |\n"
        "|---|---|---|\n"
        "| Example Corp | €11,297M | 26.1% |\n"
        "| 示例公司甲 | ¥23.6亿 | -3.10% |\n\n"
        "评级：★★☆☆☆ → 观察\n"
    )

    def test_extract_under_gbk_console(self):
        import tempfile
        with tempfile.NamedTemporaryFile('w', suffix='.md', delete=False,
                                         encoding='utf-8') as fh:
            fh.write(self.MD)
            path = fh.name
        try:
            env = dict(os.environ)
            # 模拟 Windows GBK 控制台；显式清掉 UTF-8 逃生阀
            env['PYTHONIOENCODING'] = 'gbk'
            env.pop('PYTHONUTF8', None)
            proc = subprocess.run(
                [sys.executable, os.path.join(_TOOLS, 'report_audit.py'),
                 'extract', '--report', path, '--seed', '1'],
                capture_output=True, env=env)
            self.assertEqual(
                proc.returncode, 0,
                "GBK 控制台下 extract 崩溃了：\n"
                + proc.stderr.decode('utf-8', 'replace')[-800:])
            self.assertNotIn(b'UnicodeEncodeError', proc.stderr)
        finally:
            os.unlink(path)

    def test_force_utf8_stdio_is_idempotent(self):
        """重复调用不应抛异常（stdout 可能已被重定向为非 TextIOWrapper）。"""
        R._force_utf8_stdio()
        R._force_utf8_stdio()

    def test_force_utf8_stdio_tolerates_non_reconfigurable_stream(self):
        orig = sys.stdout
        try:
            sys.stdout = io.BytesIO()  # 没有 reconfigure 方法
            R._force_utf8_stdio()      # 不应抛异常
        finally:
            sys.stdout = orig



class TestSamplerSkipsNonDataPoints(unittest.TestCase):
    """评分 / 建议阈值不是可外部核验的数据点，不应进入抽检样本。"""

    MD = (
        "| 维度 | 判断 |\n|---|---|\n"
        "| 生意质量 | 优秀（★4/5） |\n"
        "| 估值 | 偏贵（★2/5） |\n\n"
        "| 类型 | 建议 | 参考价格区间 |\n|---|---|---|\n"
        "| 保守型 | 不参与 | 核心 PE ≤22x（约 $249） |\n\n"
        "| 指标 | FY2025 |\n|---|---|\n"
        "| 营收 | $4,028亿 |\n"
    )

    def test_scores_and_thresholds_excluded(self):
        labels = [p['label'] for p in R.extract_data_points(self.MD)]
        self.assertFalse(any('生意质量' in l or '估值' in l or '保守型' in l for l in labels),
                         f"评分/阈值行被抽进了样本：{labels}")

    def test_real_data_point_kept(self):
        values = {p['reported_value'] for p in R.extract_data_points(self.MD)}
        self.assertIn(4028.0, values)


class TestSamplerSkipsYearsAndCounts(unittest.TestCase):
    MD = (
        "| 指标 | 值 | 来源 / 说明 |\n|---|---|---|\n"
        "| 监管现金流出 | $52亿 | 2026-07 终局，欧盟 |\n"
        "| FY2027 EPS | 14.88 | 卖方一致预期（54 位分析师） |\n"
        "| 召回 | 3,067 辆 | NHTSA |\n"
    )

    def test_notes_column_years_and_counts_excluded(self):
        pts = R.extract_data_points(self.MD)
        values = {p['reported_value'] for p in pts}
        self.assertIn(52.0, values)
        self.assertIn(14.88, values)
        self.assertNotIn(2026.0, values)
        self.assertNotIn(54.0, values)
        self.assertNotIn(3067.0, values)


class TestConsistency(unittest.TestCase):
    """多份底稿之间关键事实（指引 / 股价）打架必须被抓出来；修订语境要取现行值。"""

    def _write(self, tmpdir, name, text):
        path = os.path.join(tmpdir, name)
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(text)
        return path

    def test_conflicting_guidance_detected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            files = [
                self._write(d, '01-a.md', '# A\n2026年资本开支指引$1,800-1,900亿，股价 $340\n'),
                self._write(d, '02-b.md', '# B\n2026年资本开支指引$1,950-2,050亿；股价 $349.39\n'),
            ]
            facts = R.collect_key_facts(files)
            self.assertEqual(len(facts['资本开支指引']), 2)
            import contextlib
            with contextlib.redirect_stdout(io.StringIO()):
                out = R.render_consistency(facts)
            fields = {c['field'] for c in out['conflicts']}
            self.assertIn('资本开支指引', fields)
            self.assertIn('股价', fields, "2.8% 的股价快照差异应超过 2% 容差被标出")

    def test_revision_line_takes_current_value(self):
        """"已从$1,800-1,900亿上调至$1,950-2,050亿" 应取新值；"…$1,950-2,050亿，原指引$1,800-1,900亿" 亦然。"""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            files = [
                self._write(d, '01-a.md', '# A\nCapex指引已从年初的$1,800-1,900亿两次上调至$1,950-2,050亿\n'),
                self._write(d, '02-b.md', '# B\n资本开支指引$1,950-2,050亿（2026-07-22上调，原指引$1,800-1,900亿）\n'),
                self._write(d, '03-c.md', '# C\n2026年Capex指引$1,950-2,050亿，2026-07-22由$1,800-1,900亿上调\n'),
            ]
            facts = R.collect_key_facts(files)
            mids = {round(h['value']) for h in facts['资本开支指引']}
            self.assertEqual(mids, {2000}, f"修订语境应只保留现行值 1,950–2,050，实际：{facts['资本开支指引']}")

    def test_conversion_and_buyback_prices_not_treated_as_quote(self):
        """"转股价 $444.05" 不是现价；"回购市值 838.6 亿" 不是市值。"""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            files = [self._write(d, '01-a.md', '# A\n股价 $349.39；上限转股价 $444.05；市值 $4.27万亿；回购市值 838.6亿\n')]
            facts = R.collect_key_facts(files)
            self.assertEqual([h['value'] for h in facts['股价']], [349.39])
            self.assertEqual(len(facts['市值']), 1)

    def test_peer_table_and_code_block_ignored(self):
        """横评表一行多家公司的市值、代码块里工具输出的漂移股价，都不参与一致性比对。"""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            files = [self._write(d, '02-b.md',
                                 '# B\n股价 $349.39，市值 $4.27万亿\n'
                                 '| 市值 | $4.22T | $3.72T | $1.69T |\n'
                                 '```\n现价 345.51 USD × 股本 123.1 亿股 = 市值 42,528.8 亿美元\n```\n'
                                 '2025 年回购 2.40 亿股按现价计市值 838.6亿\n')]
            facts = R.collect_key_facts(files)
            self.assertEqual([h['value'] for h in facts['股价']], [349.39])
            self.assertEqual(len(facts['市值']), 1)
            self.assertAlmostEqual(facts['市值'][0]['value'], 42700.0, places=6)

    def test_meta_lines_are_ignored(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            files = [self._write(d, '最终报告.md',
                                 '# F\n| 1 | 复核：三份报告指引各不相同（$1,800–1,900亿／$1,750–1,850亿） | 已订正 |\n'
                                 '资本开支指引 $1,950-2,050亿\n')]
            facts = R.collect_key_facts(files)
            self.assertEqual(len(facts['资本开支指引']), 1)


class TestLint(unittest.TestCase):
    """CLAUDE.md 纪律：半星、主观表述为 FAIL；纯 ★ 1-5 个是合法写法。"""

    def _lint(self, text):
        import tempfile, contextlib
        with tempfile.NamedTemporaryFile('w', suffix='.md', delete=False, encoding='utf-8') as fh:
            fh.write(text)
            path = fh.name
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                return R.lint_files([path])
        finally:
            os.unlink(path)

    def test_half_star_fails(self):
        out = self._lint('管理层评分：★★★☆☆（3.5/5）\n')
        self.assertIn('HALF-STAR', {f['code'] for f in out['fails']})

    def test_subjective_wording_fails_but_quotes_exempt(self):
        out = self._lint('我认为这家公司显然很好\n> 巴菲特：显然，价格是你付出的\n')
        subj = [f for f in out['fails'] if f['code'] == 'SUBJECTIVE']
        self.assertEqual(len(subj), 1, "引用语录（> 开头）不应被判主观表述")

    def test_plain_four_stars_is_legal(self):
        out = self._lint('威胁等级 ★★★★\n护城河 ★★★★☆\n')
        self.assertEqual(out['fails'], [])
        self.assertFalse(any(w['code'] == 'STAR-COUNT' for w in out['warns']))

    def test_star_count_warns_on_malformed(self):
        out = self._lint('评分 ★★★★★★\n评分 ★★★☆\n')
        self.assertEqual(sum(1 for w in out['warns'] if w['code'] == 'STAR-COUNT'), 2)

    def test_unit_slip_warns(self):
        out = self._lint('核心净利润 $138.28亿 ≈ $1,382.8亿\n')
        self.assertTrue(any(w['code'] == 'UNIT-SLIP' for w in out['warns']))



class TestCrosscheck(unittest.TestCase):
    """双算复核：consistency 与 lint 都发现不了"一份报告自己算错"，靠两个角色独立重算。

    夹具复刻真实事故：优先股稀释率漏除存托股 1/20，被算成 7.78%（实为 0.39%）。
    """

    FIN = ("# 02\n\n## 双算复核表\n\n"
           "| 指标 | 本报告值 | 算式 |\n|---|---|---|\n"
           "| 核心TTM EPS | 10.11 | financial_rigor calc |\n"
           "| 优先股稀释率 | 7.78% | 3.35亿 × 2.842 ÷ 122.3亿 |\n"
           "| 核心PE | 34.56 | verify-valuation |\n")
    RISK = ("# 04\n\n## 双算复核表\n\n"
            "| 指标 | 本报告值 | 算式 |\n|---|---|---|\n"
            "| 优先股稀释率 | 0.39% | financial_rigor calc '335e6/20*2.842/12.23e9*100' |\n"
            "| 核心PE | 34.60 | 独立复算 |\n")

    def _run(self, required=None):
        import tempfile, contextlib
        with tempfile.TemporaryDirectory() as d:
            for name, body in (('02-fin.md', self.FIN), ('04-risk.md', self.RISK)):
                with open(os.path.join(d, name), 'w', encoding='utf-8') as fh:
                    fh.write(body)
            files = [os.path.join(d, n) for n in ('02-fin.md', '04-risk.md')]
            dual = R.collect_dual_calc(files)
            with contextlib.redirect_stdout(io.StringIO()):
                return dual, R.render_crosscheck(dual, required or [])

    def test_extracts_table(self):
        dual, _ = self._run()
        self.assertIn('核心PE', dual)
        self.assertEqual(len(dual['优先股稀释率']), 2)

    def test_twentyfold_error_detected(self):
        _, out = self._run()
        self.assertEqual([m['metric'] for m in out['mismatch']], ['优先股稀释率'])

    def test_agreement_passes(self):
        _, out = self._run()
        self.assertIn('核心PE', out['agreed'])

    def test_single_calc_reported_not_failed(self):
        _, out = self._run()
        self.assertIn('核心TTMEPS', out['single'])

    def test_no_dual_tables_is_not_reported_as_pass(self):
        import contextlib
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            out = R.render_crosscheck({}, ['核心EPS'])
        self.assertEqual(out['missing'], ['核心EPS'])
        self.assertIn('未找到任何', buf.getvalue())
        self.assertNotIn('【通过】', buf.getvalue())

    def test_required_metric_missing_is_flagged(self):
        _, out = self._run(required=['核心PE', '隐含增速'])
        self.assertEqual(out['missing'], ['隐含增速'])


class TestFileLevelLint(unittest.TestCase):
    """派生指标必须留下工具验算痕迹（禁止心算）。"""

    def _lint(self, text):
        import tempfile, contextlib
        with tempfile.NamedTemporaryFile('w', suffix='.md', delete=False, encoding='utf-8') as fh:
            fh.write(text)
            path = fh.name
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                return R.lint_files([path])
        finally:
            os.unlink(path)

    def test_warns_without_tool_trace(self):
        out = self._lint('核心EPS 为 10.11，稀释率 0.39%，隐含增速 20.8%\n')
        self.assertIn('DERIVED-NO-CALC', {w['code'] for w in out['warns']})

    def test_silent_with_tool_trace(self):
        out = self._lint('核心EPS 为 10.11，稀释率 0.39%\n\n'
                         '```\npython3 tools/financial_rigor.py calc --expr ...\n```\n')
        self.assertNotIn('DERIVED-NO-CALC', {w['code'] for w in out['warns']})

    def test_silent_for_thesis_citing_source_report(self):
        """论文文件承接研究报告的结论，不是自己心算，不应报警。"""
        out = self._lint('> 来源：/investment-team 研究，见 最终报告.md\n'
                         '核心EPS $10.11、核心PE 34.6x、隐含增速 20.8%\n')
        self.assertNotIn('DERIVED-NO-CALC', {w['code'] for w in out['warns']})

    def test_silent_when_few_derived_metrics(self):
        out = self._lint('本季营收 1,198 亿，同比 +24%\n')
        self.assertNotIn('DERIVED-NO-CALC', {w['code'] for w in out['warns']})



class TestEvidenceLedger(unittest.TestCase):
    """证据台账：汇总各视角的底稿抽查，作为 ⚠️→✅ 的升级凭据。"""

    MD = ("# 04\n\n## 底稿抽查表\n\n"
          "| 底稿条目 | 原值 | 一手来源 | 核验日期 | 结论 |\n|---|---|---|---|---|\n"
          "| 股权融资规模 | $800 亿 | SEC FWP 2026-06-02 定价清单 | 2026-09-15 | 证伪（新值：$847.5 亿） |\n"
          "| 创始人投票权 | 52.7% | DEF 14A 2026-04-24 第35页 | 2026-09-15 | 证实 |\n"
          "| AI Capex 承诺 | $8,110 亿 | 10-Q Commitments | 2026-09-15 | 核不到 |\n"
          "| 折旧影响 | $39 亿 | FY2023 10-K 附注 |  | 证实 |\n")

    def _run(self):
        import tempfile, contextlib
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, '04-risk.md')
            with open(path, 'w', encoding='utf-8') as fh:
                fh.write(self.MD)
            rows = R.collect_evidence([path])
            with contextlib.redirect_stdout(io.StringIO()):
                return rows, R.render_evidence(rows)

    def test_verdicts_bucketed(self):
        _, out = self._run()
        self.assertEqual((out['confirmed'], out['refuted'], out['unverifiable']), (2, 1, 1))

    def test_check_date_not_taken_from_source_column(self):
        """来源列里的文件日期不能被当成核验日期。"""
        rows, _ = self._run()
        by = {r['item']: r for r in rows}
        self.assertEqual(by['股权融资规模']['date'], '2026-09-15')
        self.assertEqual(by['创始人投票权']['date'], '2026-04-24' if False else '2026-09-15')

    def test_confirmed_without_date_flagged_incomplete(self):
        _, out = self._run()
        self.assertEqual([r['item'] for r in out['incomplete']], ['折旧影响'])

    def test_empty_is_not_silent(self):
        import contextlib
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            out = R.render_evidence([])
        self.assertEqual(out['refuted'], 0)
        self.assertIn('未找到任何', buf.getvalue())


if __name__ == '__main__':
    unittest.main(verbosity=2)
