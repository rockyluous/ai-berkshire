#!/usr/bin/env python3
"""A股数据工具 — 腾讯行情 + 东方财富搜索/财务，零外部依赖（仅 stdlib）。

为 Claude Code Skills 提供 A 股实时行情、财务数据等数据。
设计原则：独立模块，不影响现有工具；使用 curl 直连绕过系统代理。

用法（由 Skills 自动调用）：
    python3.11 tools/ashare_data.py quote 600519                    # 实时行情
    python3.11 tools/ashare_data.py financials 600519               # 核心财务数据（近5年）
    python3.11 tools/ashare_data.py valuation 600519                # 估值指标
    python3.11 tools/ashare_data.py search 茅台                      # 搜索股票代码
    python3 tools/ashare_data.py datasheet 600519 --price 1500 --as-of 2026-09-15 \\
        --out reports/茅台/00-数据底稿.json                            # 机器可读底稿（全队基准价）
    python3 tools/ashare_data.py datasheet --check reports/茅台/00-数据底稿.json   # 体检旧底稿

需要 Python >= 3.8，零外部依赖。
"""

import argparse
import json
import os
import subprocess
import sys
from decimal import Decimal, ROUND_HALF_EVEN

_TIMEOUT = 15


def _curl(url):
    """用 curl --noproxy 直连，绕过系统代理。"""
    result = subprocess.run(
        ["/usr/bin/curl", "-s", "--noproxy", "*",
         "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
         url],
        capture_output=True, timeout=_TIMEOUT,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise ConnectionError(f"请求失败: {url}")
    # 腾讯行情 API 返回 GBK 编码，其他返回 UTF-8
    try:
        return result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return result.stdout.decode("gbk")


def _curl_json(url, params=None):
    """curl 获取 JSON。"""
    if params:
        from urllib.parse import urlencode
        url = f"{url}?{urlencode(params)}"
    return json.loads(_curl(url))


# ---------------------------------------------------------------------------
# 腾讯行情 API（稳定可靠，无需鉴权）
# ---------------------------------------------------------------------------

def _qq_code(code: str) -> str:
    """将股票代码转为腾讯行情格式。"""
    code = code.strip().replace(".SH", "").replace(".SZ", "").replace(".BJ", "")
    if code.startswith(("6", "9", "5")):
        return f"sh{code}"
    elif code.startswith(("0", "3", "2", "1")):
        return f"sz{code}"
    elif code.startswith(("4", "8")):
        return f"bj{code}"
    return f"sh{code}"


def _parse_qq_quote(raw: str) -> dict:
    """解析腾讯行情数据。格式：v_shXXXXXX="字段1~字段2~..."; """
    start = raw.find('"')
    end = raw.rfind('"')
    if start < 0 or end <= start:
        return {}
    fields = raw[start + 1:end].split("~")
    if len(fields) < 50:
        return {}
    return {
        "name": fields[1],
        "code": fields[2],
        "price": fields[3],
        "prev_close": fields[4],
        "open": fields[5],
        "volume": fields[6],         # 手
        "buy_vol": fields[7],
        "sell_vol": fields[8],
        "high": fields[33] if len(fields) > 33 else fields[3],
        "low": fields[34] if len(fields) > 34 else fields[3],
        "change_pct": fields[32],
        "change_amt": fields[31],
        "turnover_amt": fields[37] if len(fields) > 37 else "-",
        "turnover_rate": fields[38] if len(fields) > 38 else "-",
        "pe": fields[39] if len(fields) > 39 else "-",
        "market_cap": fields[45] if len(fields) > 45 else "-",    # 总市值（亿）
        "float_cap": fields[44] if len(fields) > 44 else "-",     # 流通市值（亿）
        "pb": fields[46] if len(fields) > 46 else "-",
        # 注意：腾讯 ~ 分隔协议第 47/48 位是当日涨停价/跌停价，不是 52 周极值（issue #70）
        "limit_up": fields[47] if len(fields) > 47 else "-",
        "limit_down": fields[48] if len(fields) > 48 else "-",
        "total_shares": fields[38] if len(fields) > 38 else "-",  # will recalculate
    }


def _em_secid(code: str) -> str:
    """将股票代码转为东方财富 secid 格式：沪市前缀 1.，深市/北交所前缀 0.。"""
    code = code.strip().replace(".SH", "").replace(".SZ", "").replace(".BJ", "")
    if code.startswith(("6", "9", "5")):
        return f"1.{code}"
    return f"0.{code}"


def _fetch_52w(code: str) -> tuple:
    """从东方财富取 52 周最高/最低（f174/f175）。

    腾讯行情协议无此数据。优先 push2delay（主站 push2 对连续请求限流较严，
    52 周极值不受延时行情影响），失败回退 push2。取不到返回 ("-", "-")。
    """
    secid = _em_secid(code)
    query = f"api/qt/stock/get?secid={secid}&fields=f174,f175&invt=2&fltt=2"
    for host in ("push2delay.eastmoney.com", "push2.eastmoney.com"):
        try:
            data = _curl_json(f"https://{host}/{query}").get("data") or {}
            high, low = data.get("f174"), data.get("f175")
            if high not in (None, "-") and low not in (None, "-"):
                return high, low
        except Exception:
            continue
    return "-", "-"


def _fmt_yi(value) -> str:
    if value is None or value == "-" or value == "":
        return "-"
    try:
        v = float(value)
    except (ValueError, TypeError):
        return str(value)
    if abs(v) >= 1e8:
        return f"{v / 1e8:.2f}亿"
    if abs(v) >= 1e4:
        return f"{v / 1e4:.2f}万"
    return f"{v:.2f}"


def _fmt_pct(value) -> str:
    if value is None or value == "-" or value == "":
        return "-"
    try:
        return f"{float(value):.2f}%"
    except (ValueError, TypeError):
        return str(value)


# ---------------------------------------------------------------------------
# 命令实现
# ---------------------------------------------------------------------------

def cmd_quote(code: str):
    """实时行情快照。"""
    qq_code = _qq_code(code)
    raw = _curl(f"https://qt.gtimg.cn/q={qq_code}")
    d = _parse_qq_quote(raw)
    if not d:
        print(f"❌ 未找到股票 {code}")
        return

    print("=" * 60)
    print(f"实时行情: {d['name']} ({d['code']})")
    print("=" * 60)
    print(f"  当前价:     {d['price']}")
    print(f"  涨跌幅:     {d['change_pct']}%")
    print(f"  涨跌额:     {d['change_amt']}")
    print(f"  今开:       {d['open']}")
    print(f"  最高:       {d['high']}")
    print(f"  最低:       {d['low']}")
    print(f"  昨收:       {d['prev_close']}")
    print(f"  成交量:     {d['volume']} 手")
    print(f"  成交额:     {d['turnover_amt']}万")
    print(f"  总市值:     {d['market_cap']}亿")
    print(f"  流通市值:   {d['float_cap']}亿")
    print(f"  PE(动):     {d['pe']}")
    print(f"  PB:         {d['pb']}")
    print(f"  换手率:     {d['turnover_rate']}%")
    high_52w, low_52w = _fetch_52w(code)
    print(f"  52周最高:   {high_52w}")
    print(f"  52周最低:   {low_52w}")


def cmd_valuation(code: str):
    """估值指标汇总。"""
    qq_code = _qq_code(code)
    raw = _curl(f"https://qt.gtimg.cn/q={qq_code}")
    d = _parse_qq_quote(raw)
    if not d:
        print(f"❌ 未找到股票 {code}")
        return

    price = d["price"]
    market_cap_yi = d["market_cap"]

    print("=" * 60)
    print(f"估值指标: {d['name']} ({d['code']})")
    print("=" * 60)
    print(f"  当前价:     {price}")
    print(f"  总市值:     {market_cap_yi}亿")
    print(f"  流通市值:   {d['float_cap']}亿")
    print(f"  PE(动):     {d['pe']}")
    print(f"  PB:         {d['pb']}")
    high_52w, low_52w = _fetch_52w(code)
    print(f"  52周最高:   {high_52w}")
    print(f"  52周最低:   {low_52w}")

    # 市值验算
    try:
        p = Decimal(price)
        cap = Decimal(market_cap_yi) * Decimal("1e8")
        shares = cap / p
        print(f"\n  推算总股本: {_fmt_yi(float(shares))}股")
        calc_cap = p * shares
        reported_cap = Decimal(market_cap_yi) * Decimal("1e8")
        diff = abs(calc_cap - reported_cap) / reported_cap * 100
        print(f"  市值验算:   ✅ 一致（推算法，偏差 {float(diff):.1f}%）")
    except Exception:
        pass


def cmd_financials(code: str):
    """近5年核心财务数据。"""
    qq_code = _qq_code(code)
    raw = _curl(f"https://qt.gtimg.cn/q={qq_code}")
    d = _parse_qq_quote(raw)
    name = d.get("name", code) if d else code

    code_clean = code.strip().replace(".SH", "").replace(".SZ", "").replace(".BJ", "")
    market = "SH" if code_clean.startswith(("6", "9", "5")) else "SZ"

    # 东方财富 datacenter API（年报数据）
    fin_url = "https://datacenter.eastmoney.com/securities/api/data/get"
    params = {
        "type": "RPT_F10_FINANCE_MAINFINADATA",
        "sty": "ALL",
        "filter": f'(SECUCODE="{code_clean}.{market}")(REPORT_TYPE="年报")',
        "p": "1",
        "ps": "5",
        "sr": "-1",
        "st": "REPORT_DATE",
        "source": "HSF10",
        "client": "PC",
    }
    reports = []
    try:
        data = _curl_json(fin_url, params)
        reports = data.get("result", {}).get("data", [])
    except Exception:
        pass

    # 如果年报筛选无结果，去掉年报限制
    if not reports:
        params["filter"] = f'(SECUCODE="{code_clean}.{market}")'
        try:
            data = _curl_json(fin_url, params)
            reports = data.get("result", {}).get("data", [])
        except Exception:
            pass

    print("=" * 60)
    print(f"核心财务数据: {name} ({code_clean})")
    print("=" * 60)

    if not reports:
        print("  ⚠️ 未能获取财务数据，建议通过 WebSearch 补充")
        return

    for r in reports[:5]:
        date = r.get("REPORT_DATE", "")[:10]
        report_name = r.get("REPORT_DATE_NAME", "")
        revenue = r.get("TOTALOPERATEREVE")
        net_profit = r.get("PARENTNETPROFIT")
        eps = r.get("EPSJB")
        bps = r.get("BPS")
        roe = r.get("ROEJQ")
        rev_growth = r.get("TOTALOPERATEREVETZ")
        profit_growth = r.get("PARENTNETPROFITTZ")

        print(f"\n  --- {date} {report_name} ---")
        if revenue is not None:
            print(f"  营收:           {_fmt_yi(revenue)}")
        if rev_growth is not None:
            print(f"  营收增速:       {_fmt_pct(rev_growth)}")
        if net_profit is not None:
            print(f"  归母净利润:     {_fmt_yi(net_profit)}")
        if profit_growth is not None:
            print(f"  净利润增速:     {_fmt_pct(profit_growth)}")
        if eps is not None:
            print(f"  基本每股收益:   {eps}")
        if bps is not None:
            print(f"  每股净资产:     {bps:.2f}")
        if roe is not None:
            print(f"  ROE(加权):      {_fmt_pct(roe)}")


def cmd_search(keyword: str):
    """搜索股票代码。"""
    url = "https://searchadapter.eastmoney.com/api/suggest/get"
    # Use env var or fall back to the public eastmoney search token
    token = os.environ.get("EASTMONEY_SEARCH_TOKEN") or "D43BF722C8E33BDC906FB84D85E326E8"
    params = {
        "input": keyword,
        "type": "14",
        "token": token,
        "count": "10",
    }
    data = _curl_json(url, params)
    results = data.get("QuotationCodeTable", {}).get("Data", [])

    if not results:
        print(f"❌ 未找到匹配 '{keyword}' 的股票")
        return

    print("=" * 60)
    print(f"搜索结果: '{keyword}'")
    print("=" * 60)
    for r in results:
        code = r.get("Code", "")
        name = r.get("Name", "")
        market = r.get("MktNum", "")
        mkt_label = {"1": "沪", "2": "深", "3": "北"}.get(str(market), "")
        print(f"  {code} {name} [{mkt_label}]")


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 机器可读底稿：与 usstock_data.py datasheet 同一 schema（ai-berkshire/datasheet/1）
#
# 为什么 A 股也要：/investment-team 的"底稿先行 + 跨轮复用 + 机器判过期"此前只对美股成立，
# 而本仓库覆盖最深的恰是 A 股/港股（茅台、小米…）。本命令让 A 股研究也能一条命令体检旧底稿。
# 数据源：腾讯行情（价、总市值）+ 东方财富 F10 主要指标（年报 / 季报，含总股本）。
# ---------------------------------------------------------------------------

_EM_FIN_URL = "https://datacenter.eastmoney.com/securities/api/data/get"
_DATASHEET_SCHEMA = "ai-berkshire/datasheet/1"


def _code_market(code: str):
    code_clean = code.strip().replace(".SH", "").replace(".SZ", "").replace(".BJ", "")
    if code_clean.startswith(("6", "9", "5")):
        market = "SH"
    elif code_clean.startswith(("4", "8")):
        market = "BJ"
    else:
        market = "SZ"
    return code_clean, market


def _em_reports(code_clean: str, market: str, annual_only: bool, ps: int) -> list:
    """东方财富 F10 主要财务指标：annual_only=True 只取年报，否则按报告期倒序取全部（含季报/中报）。"""
    params = {
        "type": "RPT_F10_FINANCE_MAINFINADATA", "sty": "ALL",
        "filter": f'(SECUCODE="{code_clean}.{market}")' + ('(REPORT_TYPE="年报")' if annual_only else ""),
        "p": "1", "ps": str(ps), "sr": "-1", "st": "REPORT_DATE", "source": "HSF10", "client": "PC",
    }
    try:
        return _curl_json(_EM_FIN_URL, params).get("result", {}).get("data", []) or []
    except Exception:
        return []


def _yi(v):
    try:
        return round(float(v) / 1e8, 2)
    except (TypeError, ValueError):
        return None


def _em_row(r: dict) -> dict:
    return {
        "end": (r.get("REPORT_DATE") or "")[:10],
        "report": r.get("REPORT_DATE_NAME", ""),
        "revenue_yi": _yi(r.get("TOTALOPERATEREVE")),
        "revenue_yoy_pct": r.get("TOTALOPERATEREVETZ"),
        "net_profit_yi": _yi(r.get("PARENTNETPROFIT")),
        "net_profit_yoy_pct": r.get("PARENTNETPROFITTZ"),
        "gross_margin_pct": r.get("XSMLL"),
        "eps": r.get("EPSJB"),
        "bvps": r.get("BPS"),
        "roe_pct": r.get("ROEJQ"),
        "total_shares": r.get("TOTAL_SHARE"),
    }


def cmd_datasheet(code: str, price=None, as_of=None, out=None):
    """产出机器可读底稿 JSON：每个区块带 as_of（数据期末）与 fetched_at（实际取数时间）。"""
    from datetime import datetime, date as _date
    code_clean, market = _code_market(code)
    fetched = datetime.now().isoformat(timespec="seconds")

    d = {}
    try:
        d = _parse_qq_quote(_curl(f"https://qt.gtimg.cn/q={_qq_code(code)}")) or {}
    except Exception:
        pass
    name = d.get("name", code_clean)

    if price is None:
        price = float(d["price"]) if d.get("price") not in (None, "", "-") else None
        price_src = "腾讯行情 qt.gtimg.cn 实时价" if price is not None else "行情不可用"
        as_of = as_of or _date.today().isoformat()
    else:
        price_src = "手动锁定（--price）——全队基准价"

    annual = [_em_row(r) for r in _em_reports(code_clean, market, True, 5)][::-1]
    quarterly = [_em_row(r) for r in _em_reports(code_clean, market, False, 8)][::-1]
    latest = quarterly[-1] if quarterly else (annual[-1] if annual else {})
    shares = latest.get("total_shares")
    sh_note = "东方财富 F10 主要指标 TOTAL_SHARE（报告期末总股本）"
    if not shares and d.get("market_cap") not in (None, "", "-") and price:
        try:
            shares = round(float(d["market_cap"]) * 1e8 / float(price))
            sh_note = "由腾讯行情总市值 ÷ 现价推算（非披露值，须与年报股本核对）"
        except (TypeError, ValueError, ZeroDivisionError):
            shares = None

    doc = {
        "schema": _DATASHEET_SCHEMA,
        "company": {"code": code_clean, "market": f"A股-{market}", "name": name},
        "basis": {
            "price": price, "currency": "CNY",
            "as_of": as_of or _date.today().isoformat(),
            "price_source": price_src,
            "note": "本价为全队基准价；所有视角报告与工具调用一律传 --price 锁定，禁止各自实时取价",
        },
        "generated_at": fetched,
        "sections": {
            "shares": {"value": shares, "as_of": latest.get("end"), "note": sh_note,
                       "source": "东方财富 F10 / 腾讯行情", "fetched_at": fetched,
                       "evidence_level": "B-数据商转载（须与年报/交易所公告核对后升 A）"},
            "annual": {"rows": annual, "as_of": annual[-1]["end"] if annual else None,
                       "source": "东方财富 F10 主要指标（年报）", "fetched_at": fetched,
                       "evidence_level": "B-数据商转载", "unit": "亿元人民币；eps/bvps 为元"},
            "quarterly": {"rows": quarterly, "as_of": quarterly[-1]["end"] if quarterly else None,
                          "source": "东方财富 F10 主要指标（一季报/中报/三季报/年报，累计值口径）",
                          "fetched_at": fetched, "evidence_level": "B-数据商转载",
                          "note": "A 股季报为年初至报告期累计值，单季须相减"},
        },
        "stale_policy": {
            "facts_ttl_hours": 24,
            "rule": "fetched_at 超过 ttl 或交易所已有更新报告期 → 重跑本命令刷新；"
                    "现金流、分部、指引等非 F10 字段不在本文件内，见 Markdown 底稿并以巨潮公告为准",
        },
    }
    txt = json.dumps(doc, ensure_ascii=False, indent=2)
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(txt + "\n")
        print(f"✅ 机器可读底稿已写入 {out}")
        print(f"   基准价 {price} CNY（{doc['basis']['as_of']}，{price_src}）")
        print(f"   年度覆盖至 {doc['sections']['annual']['as_of']}，报告期覆盖至 {doc['sections']['quarterly']['as_of']}")
        print(f"   取数时间 {fetched}")
    else:
        print(txt)


def cmd_datasheet_check(path: str, offline=False) -> int:
    """新鲜度体检：取数多久了、覆盖到哪一期、交易所是否已有更新的报告期。"""
    from datetime import datetime
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    if doc.get("schema") != _DATASHEET_SCHEMA:
        raise RuntimeError(f"不是 datasheet 底稿（schema={doc.get('schema')!r}）")
    comp = doc["company"]
    print("=" * 66)
    print(f"底稿新鲜度体检 — {comp.get('name')}（{comp.get('code')}，{comp.get('market')}）")
    print("=" * 66)
    print(f"  基准价 {doc['basis']['price']} {doc['basis']['currency']}   基准日 {doc['basis']['as_of']}")
    ttl = doc.get("stale_policy", {}).get("facts_ttl_hours", 24)
    stale, now = [], datetime.now()
    for key, sec in doc["sections"].items():
        fetched = sec.get("fetched_at", "")
        try:
            age_h = (now - datetime.fromisoformat(fetched)).total_seconds() / 3600
        except ValueError:
            age_h = float("inf")
        if age_h > ttl:
            stale.append(key)
        print(f"  {key:<10s} 覆盖至 {str(sec.get('as_of')):<12s} 取数于 {fetched[:16]:<17s} "
              f"（{age_h:.0f} 小时前，{'过期' if age_h > ttl else '新鲜'}）")
    newer = None
    if not offline:
        code_clean, market = _code_market(comp.get("code", ""))
        rows = _em_reports(code_clean, market, False, 1)
        latest = (rows[0].get("REPORT_DATE") or "")[:10] if rows else None
        have = doc["sections"]["quarterly"].get("as_of")
        if latest and have and latest > have:
            newer = latest
        elif not rows:
            print("  ⚠️ 无法联网核对最新报告期")
    print("-" * 66)
    if newer:
        print(f"  ❌ 已有更新报告期（{newer} > 底稿的 {doc['sections']['quarterly'].get('as_of')}）→ 必须重跑 datasheet")
    elif stale:
        print(f"  ⚠️ {len(stale)} 个区块的缓存已超过 {ttl} 小时（{'、'.join(stale)}），报告期未变，可继续用；如需最新价请重跑")
    else:
        print("  ✅ 底稿新鲜，可直接复用（跨轮研究无需重新取数）")
    print("=" * 66)
    return 1 if newer else 0


def main():
    parser = argparse.ArgumentParser(
        description="A股数据工具 — 腾讯行情 + 东方财富财务数据",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    p_quote = sub.add_parser("quote", help="实时行情")
    p_quote.add_argument("code", help="股票代码，如 600519")

    p_fin = sub.add_parser("financials", help="核心财务数据（近5年）")
    p_fin.add_argument("code", help="股票代码")

    p_val = sub.add_parser("valuation", help="估值指标")
    p_val.add_argument("code", help="股票代码")

    p_search = sub.add_parser("search", help="搜索股票代码")
    p_search.add_argument("keyword", help="公司名或关键词")

    p_ds = sub.add_parser("datasheet", help="产出机器可读底稿 JSON（带 as_of / fetched_at）；--check 体检旧底稿")
    p_ds.add_argument("code", nargs="?", help="股票代码，如 600519（--check 时可省略）")
    p_ds.add_argument("--price", type=float, help="手动锁定基准价（全队基准价）")
    p_ds.add_argument("--as-of", dest="as_of", help="基准日 YYYY-MM-DD")
    p_ds.add_argument("--out", help="写入路径，如 reports/茅台/00-数据底稿.json")
    p_ds.add_argument("--check", metavar="JSON", help="体检已有底稿的新鲜度")
    p_ds.add_argument("--offline", action="store_true", help="--check 时不联网核对最新报告期")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "datasheet":
        if args.check:
            sys.exit(cmd_datasheet_check(args.check, offline=args.offline))
        if not args.code:
            p_ds.error("需要股票代码，或用 --check 体检已有底稿")
        cmd_datasheet(args.code, price=args.price, as_of=args.as_of, out=args.out)
        return

    cmds = {
        "quote": lambda: cmd_quote(args.code),
        "financials": lambda: cmd_financials(args.code),
        "valuation": lambda: cmd_valuation(args.code),
        "search": lambda: cmd_search(args.keyword),
    }
    cmds[args.command]()


if __name__ == "__main__":
    main()
