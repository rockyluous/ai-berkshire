#!/usr/bin/env python3
"""美股数据工具 — SEC EDGAR XBRL companyfacts（一手财务）+ Yahoo 行情，零外部依赖（仅 stdlib）。

为 Claude Code Skills 提供美股行情、估值、年度/季度核心财务。
设计原则：独立模块，与 twstock_data.py / ashare_data.py 同风格；
财务数据只取 SEC 一手（10-K / 10-Q 的 XBRL），不抓第三方网页——
macrotrends 对自动化访问经常 403，本工具就是 skills/financial-data.md 里说的兜底主源。

数据源：
    - 财务：https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json（免费、无需 key）
    - 代码：https://www.sec.gov/files/company_tickers.json（ticker → CIK）
    - 行情：Yahoo Finance chart API（仅取最新价；被拦截时用 --price 手动给）

SEC 要求请求带能联系到你的 User-Agent（公司/项目名 + 邮箱），限速 10 次/秒。
按以下优先级设置（只存本机，不入库）：
    1. 环境变量 SEC_USER_AGENT，如 "MyResearch admin@example.com"
    2. 本地文件 local/sec_user_agent.txt（local/ 已被 .gitignore 永久排除）
    3. 都没有 → 退回浏览器 UA：财务数据（data.sec.gov）可用，代码表（www.sec.gov）不可用，
       此时用 --cik 直接给公司 CIK（8-K/10-K 网址里就有，如 Alphabet 1652044）

用法（由 Skills 自动调用）：
    python3 tools/usstock_data.py search alphabet      # 搜代码 / 公司名 → ticker + CIK
    python3 tools/usstock_data.py quote GOOGL          # 最新价 + 股本 + 市值验算
    python3 tools/usstock_data.py financials GOOGL     # 近5个财年：营收/经营利润/净利润/OCF/Capex/FCF/EPS/ROE
    python3 tools/usstock_data.py quarterly GOOGL      # 近8个季度（Q4 由年度−前三季推算）
    python3 tools/usstock_data.py valuation GOOGL      # PE(TTM)/PB/PS/FCF收益率 + 非经营损益占比预警
    任何子命令加 --json 输出机器可读 JSON（供数据底稿直接引用）

注意：
    - 金额单位美元；表格按"亿美元"显示，JSON 里是原始美元
    - 财年按报告期末日标记：FY 结束于 2025-09 的公司标 FY2025（与公司自身口径一致）
    - 数据来源于公司自报 XBRL，重述后取最新一次申报的值
    - 需要 Python >= 3.8，零外部依赖
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CACHE_DIR = os.path.join(_ROOT, "local", "sec_cache")
_UA_FILE = os.path.join(_ROOT, "local", "sec_user_agent.txt")
# SEC 会拒绝"未声明的自动化工具"：UA 要么是浏览器样式，要么是 "名称 邮箱"。
# 浏览器 UA 能过 data.sec.gov（财务数据），过不了 www.sec.gov/files（代码表）。
_BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
_UA_HINT = ('SEC 要求声明式 User-Agent（"名称 邮箱"）。设置方法任选其一：\n'
            '    export SEC_USER_AGENT="Your Name you@example.com"\n'
            '    echo "Your Name you@example.com" > local/sec_user_agent.txt   # local/ 不入库\n'
            '  未设置时财务数据仍可用（浏览器 UA），但代码表不可用——请用 --cik 直接给 CIK。')

_SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
_SEC_FACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
_YF_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=5d&interval=1d"
_TIMEOUT = 30

# XBRL 标签候选（按优先级；不同公司/年份用的标签不同）
_TAGS = {
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                "SalesRevenueNet", "RevenueFromContractWithCustomerIncludingAssessedTax"],
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss", "ProfitLoss", "NetIncomeLossAvailableToCommonStockholdersBasic"],
    "nonoperating": ["NonoperatingIncomeExpense", "OtherNonoperatingIncomeExpense"],
    "ocf": ["NetCashProvidedByUsedInOperatingActivities"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment",
              "PaymentsToAcquireProductiveAssets"],
    "eps_diluted": ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"],
    "diluted_shares": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
    "equity": ["StockholdersEquity",
               "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "assets": ["Assets"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue"],
    "marketable": ["MarketableSecuritiesCurrent", "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
                   "ShortTermInvestments"],
    "lt_debt": ["LongTermDebtNoncurrent", "LongTermDebt"],
    "buyback": ["PaymentsForRepurchaseOfCommonStock"],
    "dividends": ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends"],
}
_INSTANT = {"equity", "assets", "cash", "marketable", "lt_debt"}
_PER_SHARE = {"eps_diluted"}
_SHARE_COUNT = {"diluted_shares"}


# ---------------------------------------------------------------------------
# HTTP + 缓存
# ---------------------------------------------------------------------------

def _user_agent():
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if ua:
        return ua
    try:
        with open(_UA_FILE, encoding="utf-8") as f:
            ua = f.read().strip()
            if ua:
                return ua
    except OSError:
        pass
    return _BROWSER_UA


def _ua_declared():
    return _user_agent() != _BROWSER_UA


def _cache_path(key):
    os.makedirs(_CACHE_DIR, exist_ok=True)
    return os.path.join(_CACHE_DIR, key)


def _http_json(url, cache_key=None, ttl_hours=24, headers=None):
    """GET JSON；命中本地缓存（未过期）则不请求。"""
    if cache_key:
        path = _cache_path(cache_key)
        try:
            if time.time() - os.path.getmtime(path) < ttl_hours * 3600:
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
        except OSError:
            pass
    hdrs = {"User-Agent": _user_agent(), "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise ConnectionError(f"HTTP {e.code}: {url}") from e
    except urllib.error.URLError as e:
        raise ConnectionError(f"网络请求失败: {e.reason} ({url})") from e
    if cache_key:
        try:
            with open(_cache_path(cache_key), "w", encoding="utf-8") as f:
                json.dump(payload, f)
        except OSError:
            pass
    return payload


# ---------------------------------------------------------------------------
# ticker → CIK
# ---------------------------------------------------------------------------

def _ticker_table():
    try:
        data = _http_json(_SEC_TICKERS, cache_key="company_tickers.json", ttl_hours=24 * 7)
    except ConnectionError as e:
        if "403" in str(e) and not _ua_declared():
            raise ConnectionError("SEC 代码表拒绝未声明的 User-Agent（HTTP 403）。\n  " + _UA_HINT) from e
        raise
    rows = data.values() if isinstance(data, dict) else data
    return [{"ticker": r["ticker"].upper(), "cik": int(r["cik_str"]), "title": r["title"]}
            for r in rows]


def _cik_for(ticker, cik=None):
    if cik:
        return int(cik), f"CIK {int(cik):010d}（--cik 直接指定，未查代码表）"
    t = ticker.upper().replace(".", "-")
    for r in _ticker_table():
        if r["ticker"] == t:
            return r["cik"], r["title"]
    raise LookupError(f"SEC 代码表里找不到 {ticker}（试试 `search {ticker}`；ADR/OTC 可能不在表内）")


def _load_facts(cik):
    return _http_json(_SEC_FACTS.format(cik=cik), cache_key=f"facts_{cik}.json", ttl_hours=24)


# ---------------------------------------------------------------------------
# XBRL 序列整理（纯函数，便于离线测试）
# ---------------------------------------------------------------------------

def _entries(facts, key):
    """合并所有候选标签的条目（公司会在不同年份换标签，如 Alphabet 2022 年营收用
    RevenueFromContract…、其余年份用 Revenues）。每条带 prio（标签优先级），
    同一期末多标签并存时取 prio 小的。"""
    gaap = facts.get("facts", {}).get("us-gaap", {})
    out = []
    for prio, tag in enumerate(_TAGS[key]):
        units = gaap.get(tag, {}).get("units", {})
        if key in _PER_SHARE:
            rows = units.get("USD/shares", [])
        elif key in _SHARE_COUNT:
            rows = units.get("shares", [])
        else:
            rows = units.get("USD", [])
        for r in rows:
            if r.get("form", "").startswith(("10-K", "10-Q", "20-F", "40-F")):
                rr = dict(r)
                rr["_prio"] = prio
                out.append(rr)
    return out


def _days(r):
    try:
        return (date.fromisoformat(r["end"]) - date.fromisoformat(r["start"])).days
    except (KeyError, ValueError):
        return None


def _better(new, cur):
    """同一期末两条记录谁更可信：标签优先级高者；同级取最新申报（重述后的值）。"""
    if cur is None:
        return True
    if new.get("_prio", 0) != cur.get("_prio", 0):
        return new.get("_prio", 0) < cur.get("_prio", 0)
    return new.get("filed", "") > cur.get("filed", "")


def _series(rows, lo, hi):
    """时长在 [lo, hi] 天内的期间值，按 end 去重。"""
    out = {}
    for r in rows:
        d = _days(r)
        if d is None or not (lo <= d <= hi):
            continue
        if _better(r, out.get(r["end"])):
            out[r["end"]] = r
    return {k: v["val"] for k, v in out.items()}


def _instants(rows):
    out = {}
    for r in rows:
        if "start" in r:
            continue
        if _better(r, out.get(r["end"])):
            out[r["end"]] = r
    return {k: v["val"] for k, v in out.items()}


def annual_series(rows):
    return _series(rows, 350, 380)


def quarter_series(rows):
    """单季值。三个月的条目直接用；只报 YTD 的科目（现金流表最典型）以及 Q4，
    用同一起始日（财年初）的累计值做差分：Q(end) = YTD(end) − YTD(上一季末)。
    返回 (dict end→val, 差分推算得到的 end 集合)。"""
    best = {}
    for r in rows:
        d = _days(r)
        if d is None or d < 80 or d > 380:
            continue
        key = (r.get("start"), r["end"])
        if _better(r, best.get(key)):
            best[key] = r
    by_start = {}
    for (start, end), r in best.items():
        by_start.setdefault(start, []).append((end, r["val"], _days(r)))
    q, derived = {}, set()
    for start, items in by_start.items():
        items.sort()
        prev_end, prev_val = None, None
        for end, val, d in items:
            if 80 <= d <= 100:
                # 真实单季值，覆盖任何差分推算值
                q[end] = val
                derived.discard(end)
            elif prev_end is not None:
                gap = (date.fromisoformat(end) - date.fromisoformat(prev_end)).days
                if 80 <= gap <= 100 and end not in q:
                    q[end] = val - prev_val
                    derived.add(end)
            prev_end, prev_val = end, val
    return q, derived


def fy_label(end):
    return f"FY{end[:4]}"


def q_label(end):
    d = date.fromisoformat(end)
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def _nearest_instant(inst, end, tol_days=10):
    """取与期末日最接近的时点值（10-Q/10-K 期末日偶有 1-2 天差）。"""
    if end in inst:
        return inst[end]
    e = date.fromisoformat(end)
    best, best_gap = None, tol_days + 1
    for k, v in inst.items():
        gap = abs((date.fromisoformat(k) - e).days)
        if gap < best_gap:
            best, best_gap = v, gap
    return best


def build_annual(facts, years=5):
    rev = annual_series(_entries(facts, "revenue"))
    ends = sorted(rev)[-years:]
    if not ends:
        return []
    cols = {k: annual_series(_entries(facts, k)) for k in
            ("operating_income", "net_income", "nonoperating", "ocf", "capex",
             "eps_diluted", "diluted_shares", "buyback", "dividends")}
    inst = {k: _instants(_entries(facts, k)) for k in ("equity", "assets")}
    rows = []
    prev_eq = None
    for end in ends:
        eq = _nearest_instant(inst["equity"], end)
        r = {"period": fy_label(end), "end": end, "revenue": rev.get(end)}
        for k, s in cols.items():
            r[k] = s.get(end)
        r["equity"] = eq
        r["assets"] = _nearest_instant(inst["assets"], end)
        ocf, capex = r["ocf"], r["capex"]
        r["fcf"] = (ocf - capex) if (ocf is not None and capex is not None) else None
        ni = r["net_income"]
        r["roe"] = (ni / ((eq + prev_eq) / 2)) if (ni is not None and eq and prev_eq) else \
                   ((ni / eq) if (ni is not None and eq) else None)
        r["op_margin"] = (r["operating_income"] / rev[end]) if (r["operating_income"] is not None and rev[end]) else None
        prev_eq = eq
        rows.append(r)
    return rows


def build_quarterly(facts, n=8):
    rev, rev_derived = quarter_series(_entries(facts, "revenue"))
    ends = sorted(rev)[-n:]
    if not ends:
        return []
    cols = {}
    derived_flags = {}
    for k in ("operating_income", "net_income", "nonoperating", "ocf", "capex", "eps_diluted"):
        cols[k], derived_flags[k] = quarter_series(_entries(facts, k))
    rows = []
    for end in ends:
        r = {"period": q_label(end), "end": end, "revenue": rev.get(end),
             "derived_q4": end in rev_derived}
        for k, s in cols.items():
            r[k] = s.get(end)
        ocf, capex = r["ocf"], r["capex"]
        r["fcf"] = (ocf - capex) if (ocf is not None and capex is not None) else None
        r["op_margin"] = (r["operating_income"] / r["revenue"]) if (r["operating_income"] is not None and r["revenue"]) else None
        rows.append(r)
    return rows


def shares_outstanding(facts):
    """总股本。优先 dei 封面页股数（多类别在同一日期有多条 → 加总）；
    多类别公司的封面页股数是维度数据，companyfacts 常常不给（如 Alphabet），
    此时退回最新单季稀释加权平均股数。返回 (股数, 日期, 说明)。"""
    dei = facts.get("facts", {}).get("dei", {})
    rows = dei.get("EntityCommonStockSharesOutstanding", {}).get("units", {}).get("shares", [])
    if rows:
        latest_end = max(r["end"] for r in rows)
        latest_filed = max(r.get("filed", "") for r in rows if r["end"] == latest_end)
        same = [r for r in rows if r["end"] == latest_end and r.get("filed", "") == latest_filed]
        vals = {}
        for r in same:
            vals[(r.get("frame"), r["val"])] = r["val"]
        total = sum(vals.values())
        note = "封面页股数（多类别加总）" if len(vals) > 1 else "封面页股数"
        return total, latest_end, note
    q, _ = quarter_series(_entries(facts, "diluted_shares"))
    if q:
        end = max(q)
        return q[end], end, "最新单季稀释加权平均股数（dei 无封面页股数时的替代，含稀释影响）"
    return None, None, "dei 无封面页股数，且无稀释股数"


def _fetch_price(ticker):
    url = _YF_CHART.format(ticker=urllib.parse.quote(ticker.upper()))
    data = _http_json(url, headers={"User-Agent": "Mozilla/5.0"})
    meta = data["chart"]["result"][0]["meta"]
    ts = meta.get("regularMarketTime")
    return {
        "price": meta.get("regularMarketPrice"),
        "prev_close": meta.get("chartPreviousClose") or meta.get("previousClose"),
        "currency": meta.get("currency", "USD"),
        "exchange": meta.get("exchangeName", ""),
        "time": datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M UTC") if ts else "",
        "source": "Yahoo Finance chart API",
    }


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

def _yi(v):
    if v is None:
        return "-"
    return f"{v / 1e8:,.1f}"


def _pct(v):
    return "-" if v is None else f"{v * 100:.1f}%"


def _num(v, nd=2):
    return "-" if v is None else f"{v:,.{nd}f}"


def _header(title, cik, name):
    print("=" * 72)
    print(f"{title}  —  {name}  (CIK {cik:010d})")
    print("=" * 72)


def _print_annual(rows):
    print(f"{'财年':8} {'期末':11} {'营收':>9} {'经营利润':>9} {'经营利润率':>8} {'净利润':>9} "
          f"{'非经营损益':>9} {'OCF':>9} {'Capex':>9} {'FCF':>9} {'EPS(稀释)':>9} {'ROE':>7}")
    print("  单位：亿美元；EPS 美元/股；ROE = 净利润 / 平均股东权益")
    for r in rows:
        print(f"{r['period']:8} {r['end']:11} {_yi(r['revenue']):>9} {_yi(r['operating_income']):>9} "
              f"{_pct(r['op_margin']):>8} {_yi(r['net_income']):>9} {_yi(r['nonoperating']):>9} "
              f"{_yi(r['ocf']):>9} {_yi(r['capex']):>9} {_yi(r['fcf']):>9} "
              f"{_num(r['eps_diluted']):>9} {_pct(r['roe']):>7}")


def _print_quarterly(rows):
    print(f"{'季度':7} {'期末':11} {'营收':>9} {'经营利润':>9} {'利润率':>7} {'净利润':>9} "
          f"{'非经营损益':>9} {'OCF':>9} {'Capex':>9} {'FCF':>9} {'EPS':>7}")
    print("  单位：亿美元；带 * 的 Q4 由 年度 − 前三季YTD 推算")
    for r in rows:
        tag = "*" if r["derived_q4"] else " "
        print(f"{r['period']:6}{tag} {r['end']:11} {_yi(r['revenue']):>9} {_yi(r['operating_income']):>9} "
              f"{_pct(r['op_margin']):>7} {_yi(r['net_income']):>9} {_yi(r['nonoperating']):>9} "
              f"{_yi(r['ocf']):>9} {_yi(r['capex']):>9} {_yi(r['fcf']):>9} {_num(r['eps_diluted']):>7}")


def _oneoff_warning(rows):
    """TTM 非经营损益 / 经营利润 > 15% 时提醒：GAAP 净利润含大额非经营项，PE 需剔除后再看。"""
    last4 = rows[-4:]
    non = [r["nonoperating"] for r in last4 if r["nonoperating"] is not None]
    op = [r["operating_income"] for r in last4 if r["operating_income"] is not None]
    if len(non) < 4 or len(op) < 4 or sum(op) == 0:
        return None
    ratio = sum(non) / sum(op)
    if abs(ratio) > 0.15:
        return (f"⚠️ 近 4 季非经营损益合计 {_yi(sum(non))} 亿，占经营利润 {ratio*100:.0f}%——"
                f"GAAP 净利润/EPS 含大额非经营项（投资浮盈/减值/罚款等），估值前先剔除")
    return None


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------

def cmd_search(keyword, as_json=False):
    kw = keyword.lower()
    hits = [r for r in _ticker_table()
            if kw in r["ticker"].lower() or kw in r["title"].lower()][:20]
    if as_json:
        print(json.dumps(hits, ensure_ascii=False, indent=2))
        return
    if not hits:
        print(f"未找到 {keyword}")
        return
    for r in hits:
        print(f"  {r['ticker']:8} CIK {r['cik']:010d}  {r['title']}")


def cmd_quote(ticker, price=None, shares=None, as_json=False, cik=None):
    cik, name = _cik_for(ticker, cik)
    facts = _load_facts(cik)
    px = {"price": price, "source": "手动 --price"} if price else None
    if px is None:
        try:
            px = _fetch_price(ticker)
            px["source"] += "（实时拉取——若研究已有基准价，请传 --price 锁定，避免多份报告股价漂移）"
        except Exception as e:  # noqa: BLE001
            px = {"price": None, "source": f"行情不可用（{e}）；请用 --price 手动给"}
    sh, sh_date, sh_note = shares_outstanding(facts)
    if shares:
        sh, sh_date, sh_note = shares, "手动", "手动 --shares"
    mcap = px["price"] * sh if (px.get("price") and sh) else None
    out = {"ticker": ticker.upper(), "cik": cik, "name": name, **px,
           "shares": sh, "shares_date": sh_date, "shares_note": sh_note, "market_cap": mcap}
    if as_json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return
    _header("最新行情与市值验算", cik, name)
    print(f"  股价:       {_num(px.get('price'))} {px.get('currency', 'USD')}   {px.get('time', '')}   来源: {px['source']}")
    print(f"  总股本:     {_num(sh, 0) if sh else '-'} 股  ≈ {_yi(sh)} 亿股   （{sh_note}，截至 {sh_date}）")
    print(f"  市值验算:   股价 × 股本 = {_yi(mcap)} 亿美元" + (f"  ≈ {mcap/1e12:.2f} 万亿美元" if mcap and mcap >= 1e12 else ""))
    print("  → 与报告市值对比：偏差 >1% 需在报告中标注（可用 financial_rigor.py verify-market-cap 复算）")


def cmd_financials(ticker, years=5, as_json=False, cik=None):
    cik, name = _cik_for(ticker, cik)
    rows = build_annual(_load_facts(cik), years)
    if as_json:
        print(json.dumps({"ticker": ticker.upper(), "cik": cik, "name": name, "annual": rows},
                         ensure_ascii=False, indent=2))
        return
    _header(f"近 {len(rows)} 个财年核心财务（SEC 10-K XBRL）", cik, name)
    _print_annual(rows)


def cmd_quarterly(ticker, n=8, as_json=False, cik=None):
    cik, name = _cik_for(ticker, cik)
    rows = build_quarterly(_load_facts(cik), n)
    if as_json:
        print(json.dumps({"ticker": ticker.upper(), "cik": cik, "name": name, "quarterly": rows},
                         ensure_ascii=False, indent=2))
        return
    _header(f"近 {len(rows)} 个季度核心财务（SEC 10-Q/10-K XBRL）", cik, name)
    _print_quarterly(rows)
    w = _oneoff_warning(rows)
    if w:
        print(f"\n  {w}")


def cmd_valuation(ticker, price=None, shares=None, as_json=False, cik=None):
    cik, name = _cik_for(ticker, cik)
    facts = _load_facts(cik)
    q = build_quarterly(facts, 8)
    if len(q) < 4:
        raise RuntimeError("季度数据不足 4 季，无法计算 TTM")
    price_note = ""
    if price is None:
        try:
            price = _fetch_price(ticker)["price"]
            price_note = "  ⚠️ 实时拉取价——研究已有基准价时请传 --price 锁定"
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"行情不可用（{e}），请用 --price 手动给") from e
    sh = shares or shares_outstanding(facts)[0]
    last4 = q[-4:]

    def ttm(k):
        vals = [r[k] for r in last4 if r[k] is not None]
        return sum(vals) if len(vals) == 4 else None

    rev, ni, ocf, capex, eps, non, op = (ttm(k) for k in
                                         ("revenue", "net_income", "ocf", "capex", "eps_diluted",
                                          "nonoperating", "operating_income"))
    eq = _nearest_instant(_instants(_entries(facts, "equity")), last4[-1]["end"], 40)
    cash = _nearest_instant(_instants(_entries(facts, "cash")), last4[-1]["end"], 40)
    mkt = _nearest_instant(_instants(_entries(facts, "marketable")), last4[-1]["end"], 40)
    debt = _nearest_instant(_instants(_entries(facts, "lt_debt")), last4[-1]["end"], 40)
    mcap = price * sh if sh else None
    fcf = (ocf - capex) if (ocf is not None and capex is not None) else None
    # 核心 EPS：剔除非经营损益（按 TTM 有效税率近似税后）
    core_eps = None
    if eps is not None and non is not None and ni and sh:
        tax_rate = 0.21
        core_ni = ni - non * (1 - tax_rate)
        core_eps = core_ni / sh
    out = {
        "ticker": ticker.upper(), "cik": cik, "name": name, "price": price, "shares": sh,
        "market_cap": mcap, "ttm_end": last4[-1]["end"],
        "ttm_revenue": rev, "ttm_net_income": ni, "ttm_eps_diluted": eps, "ttm_fcf": fcf,
        "ttm_nonoperating": non, "ttm_operating_income": op,
        "pe_ttm": (price / eps) if eps else None,
        "core_eps_est": core_eps, "pe_core_est": (price / core_eps) if core_eps else None,
        "ps_ttm": (mcap / rev) if (mcap and rev) else None,
        "pb": (mcap / eq) if (mcap and eq) else None,
        "fcf_yield": (fcf / mcap) if (fcf is not None and mcap) else None,
        "net_cash": ((cash or 0) + (mkt or 0) - (debt or 0)) if cash is not None else None,
    }
    if as_json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return
    _header("估值指标（TTM，SEC 一手数据 + 现价）", cik, name)
    print(f"  现价 {_num(price)} USD × 股本 {_yi(sh)} 亿股 = 市值 {_yi(mcap)} 亿美元     TTM 截至 {out['ttm_end']}{price_note}")
    print(f"  TTM 营收 {_yi(rev)} 亿   TTM 净利润 {_yi(ni)} 亿   TTM FCF {_yi(fcf)} 亿   TTM 稀释EPS {_num(eps)}")
    print(f"  PE(TTM, GAAP)  {_num(out['pe_ttm'])}x")
    if core_eps is not None and non and abs(non / (op or 1)) > 0.15:
        print(f"  PE(TTM, 核心估计) {_num(out['pe_core_est'])}x   ← 剔除非经营损益 {_yi(non)} 亿（按 21% 税率近似税后），核心EPS≈{_num(core_eps)}")
    print(f"  PS(TTM)  {_num(out['ps_ttm'])}x    PB  {_num(out['pb'])}x    FCF收益率  {_pct(out['fcf_yield'])}")
    print(f"  净现金（现金+短期有价证券−长期债务）≈ {_yi(out['net_cash'])} 亿美元")
    w = _oneoff_warning(q)
    if w:
        print(f"\n  {w}")
    print("\n  → 交叉验证：与 stockanalysis.com/stocks/{ticker}/statistics 对照，误差 >1% 须标注")


def main():
    parser = argparse.ArgumentParser(description="美股数据工具（SEC EDGAR XBRL + Yahoo 行情）")
    sub = parser.add_subparsers(dest="command")
    for cmd, help_text in (("quote", "最新价 + 股本 + 市值验算"),
                           ("financials", "近5个财年核心财务"),
                           ("quarterly", "近8个季度核心财务"),
                           ("valuation", "PE/PB/PS/FCF收益率 + 非经营损益预警")):
        p = sub.add_parser(cmd, help=help_text)
        p.add_argument("ticker")
        p.add_argument("--json", action="store_true", help="输出 JSON")
        p.add_argument("--cik", type=int, help="直接给 CIK，跳过代码表（SEC 代码表需声明式 UA）")
        if cmd in ("quote", "valuation"):
            p.add_argument("--price", type=float, help="手动指定股价（行情接口被拦截时）")
            p.add_argument("--shares", type=float, help="手动指定总股本（股）")
        if cmd == "financials":
            p.add_argument("--years", type=int, default=5)
        if cmd == "quarterly":
            p.add_argument("--n", type=int, default=8)
    p_search = sub.add_parser("search", help="搜索 ticker / 公司名")
    p_search.add_argument("keyword")
    p_search.add_argument("--json", action="store_true")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return
    try:
        if args.command == "search":
            cmd_search(args.keyword, args.json)
        elif args.command == "quote":
            cmd_quote(args.ticker, args.price, args.shares, args.json, args.cik)
        elif args.command == "financials":
            cmd_financials(args.ticker, args.years, args.json, args.cik)
        elif args.command == "quarterly":
            cmd_quarterly(args.ticker, args.n, args.json, args.cik)
        elif args.command == "valuation":
            cmd_valuation(args.ticker, args.price, args.shares, args.json, args.cik)
    except (ConnectionError, LookupError, RuntimeError) as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
