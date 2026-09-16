#!/usr/bin/env python3
"""台股数据工具 — FinMind 开放数据 API，零外部依赖（仅 stdlib）。

为 Claude Code Skills 提供台股行情、估值、财务、月营收等数据。
设计原则：独立模块，不影响现有工具；与 ashare_data.py 同风格。

数据源：FinMind (api.finmindtrade.com)，覆盖上市(twse)/上柜(tpex)全部股票。
未注册可直接使用（有小时级请求限额）。注册后的 API token 可提升额度，
按以下优先级读取（token 只存本机，严禁提交到 git）：
    1. 环境变量 FINMIND_TOKEN
    2. 本地文件 local/finmind_token.txt（local/ 目录已被 .gitignore 永久排除）

用法（由 Skills 自动调用）：
    python3 tools/twstock_data.py quote 2330        # 最新行情 + 估值 + 市值验算
    python3 tools/twstock_data.py valuation 2330    # PER/PBR/殖利率 + 52周高低
    python3 tools/twstock_data.py financials 2330   # 近5年年度核心财务 + 最新季度
    python3 tools/twstock_data.py revenue 2330      # 近13个月月营收及同比（台股独有月度披露）
    python3 tools/twstock_data.py dividend 2330     # 近年股利政策
    python3 tools/twstock_data.py search 台積        # 搜索股票代码（支持繁体/代码）
    python3 tools/twstock_data.py datasheet 2330 --price 1000 --as-of 2026-09-15 \\
        --out reports/台积电/00-数据底稿.json         # 机器可读底稿（全队基准价）
    python3 tools/twstock_data.py datasheet --check reports/台积电/00-数据底稿.json   # 体检旧底稿

注意：
    - 所有金额单位为新台币（TWD）
    - FinMind 损益表为单季值，本工具已自动加总为年度值
    - 需要 Python >= 3.8，零外部依赖
"""

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import date, timedelta

_API = "https://api.finmindtrade.com/api/v4/data"
_TIMEOUT = 30
_TOKEN_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "local", "finmind_token.txt",
)


def _token():
    """读取 FinMind token：环境变量优先，其次本地文件；都没有则匿名访问。"""
    t = os.environ.get("FINMIND_TOKEN", "").strip()
    if t:
        return t
    try:
        with open(_TOKEN_FILE, encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def _get(dataset, data_id=None, start_date=None, end_date=None):
    """请求 FinMind API，返回 data 列表。"""
    params = {"dataset": dataset}
    if data_id:
        params["data_id"] = data_id
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date
    token = _token()
    if token:
        params["token"] = token
    url = f"{_API}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 402, 403) and token:
            raise ConnectionError(
                f"FinMind 拒绝请求（HTTP {e.code}），大概率是 token 无效或过期。"
                "请检查环境变量 FINMIND_TOKEN 或 local/finmind_token.txt 的内容；"
                "删除 token 可退回匿名访问（有小时级限额）"
            ) from e
        raise ConnectionError(f"FinMind 请求失败: HTTP {e.code} ({dataset})") from e
    except urllib.error.URLError as e:
        raise ConnectionError(f"FinMind 网络请求失败: {e.reason}") from e
    if payload.get("status") != 200:
        raise ConnectionError(f"FinMind 请求失败: {payload.get('msg')} ({dataset})")
    return payload.get("data", [])


def _fmt_yi(value):
    """新台币金额格式化为 亿/万。"""
    if value is None or value == "":
        return "-"
    try:
        v = float(value)
    except (ValueError, TypeError):
        return str(value)
    if abs(v) >= 1e8:
        return f"{v / 1e8:,.1f}亿"
    if abs(v) >= 1e4:
        return f"{v / 1e4:,.1f}万"
    return f"{v:,.2f}"


def _days_ago(n):
    return (date.today() - timedelta(days=n)).isoformat()


def _stock_name(stock_id):
    """从 TaiwanStockInfo 取股票名称与板块。"""
    try:
        rows = _get("TaiwanStockInfo", data_id=stock_id)
    except Exception:
        return stock_id, ""
    if not rows:
        return stock_id, ""
    board = {"twse": "上市", "tpex": "上柜"}.get(rows[0].get("type", ""), "")
    return rows[0].get("stock_name", stock_id), board


def _latest_shares(stock_id):
    """从 TaiwanStockShareholding 取最新发行股数。"""
    rows = _get("TaiwanStockShareholding", data_id=stock_id, start_date=_days_ago(14))
    if not rows:
        return None
    return rows[-1].get("NumberOfSharesIssued")


# ---------------------------------------------------------------------------
# 命令实现
# ---------------------------------------------------------------------------

def cmd_quote(stock_id):
    """最新行情快照 + 市值验算。"""
    name, board = _stock_name(stock_id)
    prices = _get("TaiwanStockPrice", data_id=stock_id, start_date=_days_ago(14))
    if not prices:
        print(f"❌ 未找到股票 {stock_id} 的行情数据")
        return
    p = prices[-1]
    prev_close = prices[-2]["close"] if len(prices) >= 2 else None

    pers = _get("TaiwanStockPER", data_id=stock_id, start_date=_days_ago(14))
    per = pers[-1] if pers else {}

    print("=" * 60)
    print(f"台股行情: {name} ({stock_id}) [{board}]  数据源: FinMind")
    print("=" * 60)
    print(f"  日期:       {p['date']}")
    print(f"  收盘价:     {p['close']} 新台币")
    if prev_close:
        chg = p["close"] - prev_close
        print(f"  涨跌:       {chg:+.2f} ({chg / prev_close * 100:+.2f}%)")
    print(f"  开/高/低:   {p['open']} / {p['max']} / {p['min']}")
    print(f"  成交量:     {_fmt_yi(p['Trading_Volume'])}股")
    print(f"  成交额:     {_fmt_yi(p['Trading_money'])}新台币")
    if per:
        print(f"  PER:        {per.get('PER', '-')}")
        print(f"  PBR:        {per.get('PBR', '-')}")
        print(f"  殖利率:     {per.get('dividend_yield', '-')}%")

    # 市值验算：收盘价 × 发行股数
    try:
        shares = _latest_shares(stock_id)
        if shares:
            cap = p["close"] * shares
            print(f"\n  发行股数:   {_fmt_yi(shares)}股")
            print(f"  总市值:     {_fmt_yi(cap)}新台币（= 收盘 {p['close']} × 股数，手算口径）")
    except Exception:
        print("\n  ⚠️ 发行股数获取失败，市值请另行验算")


def cmd_valuation(stock_id):
    """估值指标 + 52周高低。"""
    name, board = _stock_name(stock_id)
    prices = _get("TaiwanStockPrice", data_id=stock_id, start_date=_days_ago(370))
    if not prices:
        print(f"❌ 未找到股票 {stock_id} 的行情数据")
        return
    p = prices[-1]
    high_52w = max(r["max"] for r in prices)
    low_52w = min(r["min"] for r in prices)

    pers = _get("TaiwanStockPER", data_id=stock_id, start_date=_days_ago(370))
    per = pers[-1] if pers else {}
    per_vals = [r["PER"] for r in pers if r.get("PER")]

    print("=" * 60)
    print(f"估值指标: {name} ({stock_id}) [{board}]  数据源: FinMind")
    print("=" * 60)
    print(f"  日期:       {p['date']}")
    print(f"  收盘价:     {p['close']} 新台币")
    print(f"  PER:        {per.get('PER', '-')}")
    if per_vals:
        print(f"  PER一年区间: {min(per_vals)} ~ {max(per_vals)}")
    print(f"  PBR:        {per.get('PBR', '-')}")
    print(f"  殖利率:     {per.get('dividend_yield', '-')}%")
    print(f"  52周最高:   {high_52w}")
    print(f"  52周最低:   {low_52w}")

    try:
        shares = _latest_shares(stock_id)
        if shares:
            cap = p["close"] * shares
            print(f"  发行股数:   {_fmt_yi(shares)}股")
            print(f"  总市值:     {_fmt_yi(cap)}新台币")
    except Exception:
        pass


_IS_KEYS = {
    "Revenue": "营收",
    "GrossProfit": "毛利",
    "OperatingIncome": "营业利益",
    "EquityAttributableToOwnersOfParent": "归母净利",
    "EPS": "EPS",
}


def cmd_financials(stock_id):
    """近5年年度核心财务（单季加总）+ 年末权益推算 ROE。"""
    name, board = _stock_name(stock_id)
    start = f"{date.today().year - 5}-01-01"
    rows = _get("TaiwanStockFinancialStatements", data_id=stock_id, start_date=start)
    if not rows:
        print(f"❌ 未找到股票 {stock_id} 的财务数据")
        return

    # 按年份聚合单季值：{year: {指标: 累计值}}，并记录季度数
    years = {}
    for r in rows:
        if r["type"] not in _IS_KEYS:
            continue
        y = r["date"][:4]
        d = years.setdefault(y, {"_quarters": set()})
        d["_quarters"].add(r["date"])
        d[r["type"]] = d.get(r["type"], 0) + (r["value"] or 0)

    # 年末归母权益（Q4 资产负债表），用于简化 ROE
    equity_by_year = {}
    try:
        bs = _get("TaiwanStockBalanceSheet", data_id=stock_id, start_date=start)
        for r in bs:
            if r["type"] == "EquityAttributableToOwnersOfParent" and r["date"][5:7] == "12":
                equity_by_year[r["date"][:4]] = r["value"]
    except Exception:
        pass

    print("=" * 60)
    print(f"核心财务数据: {name} ({stock_id}) [{board}]  数据源: FinMind")
    print("=" * 60)
    print("  单位：新台币。FinMind 损益表为单季值，以下为年度加总。")

    for y in sorted(years, reverse=True):
        d = years[y]
        nq = len(d["_quarters"])
        suffix = "" if nq == 4 else f"（仅前{nq}季累计，非全年）"
        rev = d.get("Revenue")
        gp = d.get("GrossProfit")
        op = d.get("OperatingIncome")
        ni = d.get("EquityAttributableToOwnersOfParent")
        eps = d.get("EPS")
        print(f"\n  --- {y}年 {suffix} ---")
        if rev:
            print(f"  营收:       {_fmt_yi(rev)}")
        if gp and rev:
            print(f"  毛利率:     {gp / rev * 100:.1f}%")
        if op and rev:
            print(f"  营业利益率: {op / rev * 100:.1f}%")
        if ni:
            print(f"  归母净利:   {_fmt_yi(ni)}")
        if ni and rev:
            print(f"  净利率:     {ni / rev * 100:.1f}%")
        if eps:
            print(f"  EPS:        {eps:.2f}")
        eq = equity_by_year.get(y)
        if eq and ni and nq == 4:
            print(f"  ROE(简化):  {ni / eq * 100:.1f}%（归母净利/年末归母权益，非期初期末平均）")


def cmd_revenue(stock_id):
    """近13个月月营收及同比——台股独有的月度披露，跟踪基本面拐点。"""
    name, board = _stock_name(stock_id)
    rows = _get("TaiwanStockMonthRevenue", data_id=stock_id, start_date=_days_ago(800))
    if not rows:
        print(f"❌ 未找到股票 {stock_id} 的月营收数据")
        return

    by_month = {(r["revenue_year"], r["revenue_month"]): r["revenue"] for r in rows}

    print("=" * 60)
    print(f"月营收: {name} ({stock_id}) [{board}]  数据源: FinMind")
    print("=" * 60)
    print("  单位：新台币（台股每月10日前强制披露上月营收）")
    print(f"\n  {'月份':<10}{'营收':>14}{'同比':>10}")

    keys = sorted(by_month)[-13:]
    for y, m in keys:
        rev = by_month[(y, m)]
        prev = by_month.get((y - 1, m))
        yoy = f"{(rev / prev - 1) * 100:+.1f}%" if prev else "-"
        print(f"  {y}-{m:02d}   {_fmt_yi(rev):>14}{yoy:>10}")


def cmd_dividend(stock_id):
    """近年股利政策。"""
    name, board = _stock_name(stock_id)
    start = f"{date.today().year - 5}-01-01"
    rows = _get("TaiwanStockDividend", data_id=stock_id, start_date=start)
    if not rows:
        print(f"❌ 未找到股票 {stock_id} 的股利数据")
        return

    print("=" * 60)
    print(f"股利政策: {name} ({stock_id}) [{board}]  数据源: FinMind")
    print("=" * 60)
    print("  单位：新台币/股（台股常见按季配息，年度股利需自行加总）")
    print(f"\n  {'所属期间':<12}{'现金股利':>8}{'股票股利':>8}  {'除息日':<12}{'发放日':<12}")

    for r in rows:
        cash = (r.get("CashEarningsDistribution") or 0) + (r.get("CashStatutorySurplus") or 0)
        stock = (r.get("StockEarningsDistribution") or 0) + (r.get("StockStatutorySurplus") or 0)
        if not cash and not stock:
            continue
        ex_date = r.get("CashExDividendTradingDate") or "-"
        pay_date = r.get("CashDividendPaymentDate") or "-"
        print(f"  {r.get('year', ''):<12}{cash:>8.2f}{stock:>8.2f}  {ex_date:<12}{pay_date:<12}")


def cmd_search(keyword):
    """按名称/代码搜索台股（TaiwanStockInfo 全表过滤）。"""
    rows = _get("TaiwanStockInfo")
    seen = {}
    for r in rows:
        if keyword in r.get("stock_name", "") or keyword == r.get("stock_id", ""):
            sid = r["stock_id"]
            if sid not in seen:
                seen[sid] = {
                    "name": r["stock_name"],
                    "type": r.get("type", ""),
                    "industries": [],
                }
            cat = r.get("industry_category", "")
            if cat and cat not in seen[sid]["industries"]:
                seen[sid]["industries"].append(cat)

    if not seen:
        print(f"❌ 未找到匹配 '{keyword}' 的台股（提示：台股名称多为繁体，如 台積電）")
        return

    print("=" * 60)
    print(f"台股搜索结果: '{keyword}'  数据源: FinMind")
    print("=" * 60)
    for sid, d in sorted(seen.items()):
        board = {"twse": "上市", "tpex": "上柜"}.get(d["type"], d["type"])
        cats = "/".join(d["industries"][:3])
        print(f"  {sid} {d['name']} [{board}] {cats}")


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 机器可读底稿：与 usstock_data.py / ashare_data.py 同一 schema（ai-berkshire/datasheet/1）
# ---------------------------------------------------------------------------

_DATASHEET_SCHEMA = "ai-berkshire/datasheet/1"


def _statements(stock_id, years=5):
    """FinMind 损益表原始行（单季值）。"""
    start = f"{date.today().year - years}-01-01"
    return _get("TaiwanStockFinancialStatements", data_id=stock_id, start_date=start)


def _annual_rows(rows):
    years = {}
    for r in rows:
        if r["type"] not in _IS_KEYS:
            continue
        y = r["date"][:4]
        d = years.setdefault(y, {"_quarters": set()})
        d["_quarters"].add(r["date"])
        d[r["type"]] = d.get(r["type"], 0) + (r["value"] or 0)
    out = []
    for y in sorted(years):
        d = years[y]
        nq = len(d["_quarters"])
        rev, gp, op, ni = d.get("Revenue"), d.get("GrossProfit"), d.get("OperatingIncome"), d.get("EquityAttributableToOwnersOfParent")
        out.append({
            "year": y, "quarters": nq, "complete": nq == 4, "end": max(d["_quarters"]),
            "revenue_yi": round(rev / 1e8, 2) if rev else None,
            "gross_margin_pct": round(gp / rev * 100, 2) if gp and rev else None,
            "operating_margin_pct": round(op / rev * 100, 2) if op and rev else None,
            "net_income_yi": round(ni / 1e8, 2) if ni else None,
            "eps": round(d["EPS"], 2) if d.get("EPS") else None,
        })
    return out


def _quarter_rows(rows, n=8):
    qs = {}
    for r in rows:
        if r["type"] not in _IS_KEYS:
            continue
        qs.setdefault(r["date"], {})[r["type"]] = r["value"]
    out = []
    for dt in sorted(qs)[-n:]:
        d = qs[dt]
        rev, op, ni = d.get("Revenue"), d.get("OperatingIncome"), d.get("EquityAttributableToOwnersOfParent")
        out.append({"end": dt,
                    "revenue_yi": round(rev / 1e8, 2) if rev else None,
                    "operating_income_yi": round(op / 1e8, 2) if op else None,
                    "net_income_yi": round(ni / 1e8, 2) if ni else None,
                    "eps": d.get("EPS")})
    return out


def cmd_datasheet(stock_id, price=None, as_of=None, out=None):
    """产出机器可读底稿 JSON：每个区块带 as_of（数据期末）与 fetched_at（实际取数时间）。"""
    from datetime import datetime
    name, board = _stock_name(stock_id)
    fetched = datetime.now().isoformat(timespec="seconds")

    if price is None:
        prices = _get("TaiwanStockPrice", data_id=stock_id, start_date=_days_ago(14))
        if prices:
            price, as_of = prices[-1]["close"], as_of or prices[-1]["date"]
            price_src = "FinMind TaiwanStockPrice 收盘价"
        else:
            price_src = "行情不可用"
    else:
        price_src = "手动锁定（--price）——全队基准价"

    rows = _statements(stock_id)
    annual, quarterly = _annual_rows(rows), _quarter_rows(rows)
    shares = None
    try:
        shares = _latest_shares(stock_id)
    except Exception:
        pass

    doc = {
        "schema": _DATASHEET_SCHEMA,
        "company": {"code": stock_id, "market": f"台股-{board or '未知'}", "name": name},
        "basis": {
            "price": price, "currency": "TWD",
            "as_of": as_of or date.today().isoformat(),
            "price_source": price_src,
            "note": "本价为全队基准价；所有视角报告与工具调用一律传 --price 锁定，禁止各自实时取价",
        },
        "generated_at": fetched,
        "sections": {
            "shares": {"value": shares, "as_of": date.today().isoformat(),
                       "note": "FinMind TaiwanStockShareholding 发行股数（近两周最新）",
                       "source": "FinMind", "fetched_at": fetched, "evidence_level": "B-数据商转载"},
            "annual": {"rows": annual, "as_of": annual[-1]["end"] if annual else None,
                       "source": "FinMind TaiwanStockFinancialStatements（单季加总，complete=false 为非全年）",
                       "fetched_at": fetched, "evidence_level": "B-数据商转载", "unit": "亿新台币；eps 为元"},
            "quarterly": {"rows": quarterly, "as_of": quarterly[-1]["end"] if quarterly else None,
                          "source": "FinMind TaiwanStockFinancialStatements（单季值）",
                          "fetched_at": fetched, "evidence_level": "B-数据商转载"},
        },
        "stale_policy": {
            "facts_ttl_hours": 24,
            "rule": "fetched_at 超过 ttl 或 FinMind 已有更新季度 → 重跑本命令刷新；"
                    "现金流、分部、指引等字段不在本文件内，见 Markdown 底稿并按 skills/financial-data.md 台股章节与 Goodinfo 交叉验证",
        },
    }
    txt = json.dumps(doc, ensure_ascii=False, indent=2)
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(txt + "\n")
        print(f"✅ 机器可读底稿已写入 {out}")
        print(f"   基准价 {price} TWD（{doc['basis']['as_of']}，{price_src}）")
        print(f"   年度覆盖至 {doc['sections']['annual']['as_of']}，季度覆盖至 {doc['sections']['quarterly']['as_of']}")
        print(f"   取数时间 {fetched}")
    else:
        print(txt)


def cmd_datasheet_check(path, offline=False):
    """新鲜度体检：取数多久了、覆盖到哪一期、FinMind 是否已有更新季度。"""
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
        try:
            q = _quarter_rows(_statements(comp["code"], years=1), n=1)
            latest = q[-1]["end"] if q else None
            have = doc["sections"]["quarterly"].get("as_of")
            if latest and have and latest > have:
                newer = latest
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️ 无法联网核对最新季度（{e}）")
    print("-" * 66)
    if newer:
        print(f"  ❌ FinMind 已有更新季度（{newer} > 底稿的 {doc['sections']['quarterly'].get('as_of')}）→ 必须重跑 datasheet")
    elif stale:
        print(f"  ⚠️ {len(stale)} 个区块的缓存已超过 {ttl} 小时（{'、'.join(stale)}），季度未变，可继续用；如需最新价请重跑")
    else:
        print("  ✅ 底稿新鲜，可直接复用（跨轮研究无需重新取数）")
    print("=" * 66)
    return 1 if newer else 0


def main():
    parser = argparse.ArgumentParser(
        description="台股数据工具 — FinMind 开放数据 API",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    for cmd, help_text in [
        ("quote", "最新行情 + 市值验算"),
        ("valuation", "估值指标（PER/PBR/殖利率/52周高低）"),
        ("financials", "近5年年度核心财务"),
        ("revenue", "近13个月月营收及同比"),
        ("dividend", "近年股利政策"),
    ]:
        p = sub.add_parser(cmd, help=help_text)
        p.add_argument("stock_id", help="股票代码，如 2330")

    p_search = sub.add_parser("search", help="搜索股票代码")
    p_search.add_argument("keyword", help="公司名（繁体）或代码")

    p_ds = sub.add_parser("datasheet", help="产出机器可读底稿 JSON（带 as_of / fetched_at）；--check 体检旧底稿")
    p_ds.add_argument("stock_id", nargs="?", help="股票代码，如 2330（--check 时可省略）")
    p_ds.add_argument("--price", type=float, help="手动锁定基准价（全队基准价）")
    p_ds.add_argument("--as-of", dest="as_of", help="基准日 YYYY-MM-DD")
    p_ds.add_argument("--out", help="写入路径，如 reports/台积电/00-数据底稿.json")
    p_ds.add_argument("--check", metavar="JSON", help="体检已有底稿的新鲜度")
    p_ds.add_argument("--offline", action="store_true", help="--check 时不联网核对最新季度")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    try:
        if args.command == "datasheet":
            if args.check:
                sys.exit(cmd_datasheet_check(args.check, offline=args.offline))
            if not args.stock_id:
                p_ds.error("需要股票代码，或用 --check 体检已有底稿")
            cmd_datasheet(args.stock_id, price=args.price, as_of=args.as_of, out=args.out)
        elif args.command == "search":
            cmd_search(args.keyword)
        else:
            {
                "quote": cmd_quote,
                "valuation": cmd_valuation,
                "financials": cmd_financials,
                "revenue": cmd_revenue,
                "dividend": cmd_dividend,
            }[args.command](args.stock_id)
    except BrokenPipeError:
        # 输出被管道截断（如 | head），静默退出。
        # 注意 BrokenPipeError 是 ConnectionError 的子类，必须放在前面
        sys.stderr.close()
        sys.exit(0)
    except ConnectionError as e:
        print(f"❌ {e}")
        sys.exit(2)


if __name__ == "__main__":
    main()
