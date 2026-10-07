#!/usr/bin/env python3
"""
IPO last-day GMP alert -> Telegram
==================================

Runs once each weekday morning. Finds Indian IPOs whose subscription closes
TODAY (IST), keeps the ones whose grey-market premium (GMP) is at least
GMP_THRESHOLD_PCT of the issue price, and sends each to Telegram and/or
WhatsApp (via CallMeBot) with a three-year snapshot of the company's
restated financials.

Data (public pages from the same publisher):
  * InvestorGain live GMP feed       -> GMP, price, lot, dates, subscription
  * Chittorgarh IPO list + IPO page  -> restated financials, valuation, about

Usage:
  python ipo_alert.py                    # normal daily run
  python ipo_alert.py --test             # sample alert now (ignores date & GMP filters)
  python ipo_alert.py --dry-run          # print messages instead of sending
  python ipo_alert.py --date 2026-10-05  # pretend it's that morning
"""
from __future__ import annotations

import argparse
import calendar
import html
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------- settings --
GMP_THRESHOLD_PCT = float(os.getenv("GMP_THRESHOLD_PCT", "15"))
INCLUDE_SME = os.getenv("INCLUDE_SME", "true").strip().lower() in {"1", "true", "yes", "y"}
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
WHATSAPP_PHONE = os.getenv("WHATSAPP_PHONE", "").replace(" ", "").strip()   # e.g. +919876543210
CALLMEBOT_APIKEY = os.getenv("CALLMEBOT_APIKEY", "").strip()

IST = ZoneInfo("Asia/Kolkata")
GMP_FEED = ("https://webnodejs.investorgain.com/cloud/v2/report/data-read/331/1/"
            "{month}/{year}/{fy}/0/all?search=")
LIST_FEED = ("https://webnodejs.chittorgarh.com/cloud/report/data-read/82/1/"
             "{month}/{year}/{fy}/0/all/0?search=&v=14-08")
CHITTORGARH = "https://www.chittorgarh.com"
INVESTORGAIN = "https://www.investorgain.com"
GMP_PAGE = "https://www.investorgain.com/report/live-ipo-gmp/331/"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


# ------------------------------------------------------------------- HTTP --
class FetchError(RuntimeError):
    pass


_session = requests.Session()


def http_get(url: str, *, page: bool = False) -> bytes:
    """GET with browser-like headers and 3 attempts. Returns the raw body."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": ("text/html,application/xhtml+xml,*/*;q=0.8" if page
                   else "application/json, text/plain, */*"),
        "Accept-Language": "en-IN,en;q=0.9",
        "Origin": CHITTORGARH,
        "Referer": CHITTORGARH + "/",
    }
    problem = ""
    for attempt in range(3):
        try:
            resp = _session.get(url, headers=headers, timeout=30)
            if resp.status_code == 200:
                return resp.content
            problem = f"HTTP {resp.status_code}"
        except requests.RequestException as exc:
            problem = f"{type(exc).__name__}: {str(exc)[:120]}"
        time.sleep(3 * (attempt + 1))
    raise FetchError(f"{problem} from {url}")


def get_json(url: str):
    body = http_get(url)
    try:
        return json.loads(body.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise FetchError(f"Expected JSON but got something else from {url}") from exc


# ---------------------------------------------------------------- parsing --
MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}


def clean(value) -> str:
    """Strip HTML tags/entities and squash whitespace."""
    if value is None:
        return ""
    text = html.unescape(re.sub(r"<[^>]+>", " ", str(value)))
    return re.sub(r"\s+", " ", text).strip()


def to_num(value) -> float | None:
    """First number in a cell: '₹1,234.5 Cr' -> 1234.5, '(12.3)' -> -12.3."""
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"(\(?)\s*(-?\d+(?:\.\d+)?)", clean(value).replace(",", ""))
    if not m:
        return None
    n = float(m.group(2))
    return -abs(n) if m.group(1) == "(" else n


def _year(token: str | None, fallback: int | None) -> int | None:
    if not token:
        return fallback
    y = int(token)
    return y + 2000 if y < 100 else y


def _make_date(y: int | None, m: int | None, d: int | None) -> date | None:
    if not (y and m):
        return None
    last = calendar.monthrange(y, m)[1]
    return date(y, m, min(d or last, last))


def parse_date(value, ref: date | None = None) -> date | None:
    """Feed / header dates: '2026-10-05', '5-Oct', '31 Mar 2026', 'Mar 31, 2026',
    '31/03/2026', 'FY26', 'FY 2025-26', 'Mar-26'."""
    text = clean(value)
    if not text or text in {"-", "--", "NA", "N/A"}:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return (dt.astimezone(IST) if dt.tzinfo else dt).date()
    except ValueError:
        pass
    default_year = ref.year if ref else None
    found = None
    m = re.search(r"\b(\d{1,2})[\s\-/.]+([A-Za-z]{3,9})\.?(?:[\s\-/.,']+(\d{4}|\d{2}))?\b", text)
    if m and m.group(2)[:3].lower() in MONTHS:
        found = _make_date(_year(m.group(3), default_year), MONTHS[m.group(2)[:3].lower()],
                           int(m.group(1)))
    if not found:
        m = re.search(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})\b", text)
        if m and m.group(1)[:3].lower() in MONTHS:
            found = _make_date(int(m.group(3)), MONTHS[m.group(1)[:3].lower()], int(m.group(2)))
    if not found:
        m = re.search(r"\b(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})\b", text)
        if m and 1 <= int(m.group(2)) <= 12:
            found = _make_date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    if not found:
        m = re.search(r"\bFY\s*'?(\d{2,4})(?:\s*[-/]\s*(\d{2,4}))?", text, re.I)
        if m:
            found = _make_date(_year(m.group(2) or m.group(1), None), 3, 31)
    if not found:
        m = re.search(r"\b([A-Za-z]{3,9})[\s\-']+(\d{4}|\d{2})\b", text)
        if m and m.group(1)[:3].lower() in MONTHS:
            found = _make_date(_year(m.group(2), None), MONTHS[m.group(1)[:3].lower()], None)
    # '2-Jan' read on 30 Dec belongs to next year
    if found and ref and not re.search(r"\d{4}", text) and (ref - found).days > 180:
        found = found.replace(year=found.year + 1)
    return found


def norm_key(text) -> str:
    """'Nityas Gems & Jewellery Ltd.' and 'nityas-gems-jewellery-ipo' -> 'nityasgemsjewellery'."""
    t = clean(text).lower().replace("&", " ")
    t = re.sub(r"\b(ltd|limited|ipo|gmp|sme|nse|bse|emerge|and|the)\b", " ", t)
    return re.sub(r"[^a-z0-9]+", "", t)


def pick(row: dict, *names: str, prefix: bool = False):
    """First non-empty value among the given keys (optionally by key prefix)."""
    for name in names:
        if row.get(name) not in (None, ""):
            return row[name]
    if prefix:
        for name in names:
            for key, value in row.items():
                if key.lower().startswith(name.lower()) and value not in (None, ""):
                    return value
    return None


# -------------------------------------------------------------- GMP feed --
@dataclass
class Ipo:
    name: str
    is_sme: bool
    price: float | None
    gmp: float | None
    gmp_pct: float | None
    lot: int | None
    open_date: date | None
    close_date: date | None
    listing_date: date | None
    subscribed: float | None
    size: str
    gmp_updated: str
    gmp_url: str | None
    keys: list[str]
    page_url: str | None = None  # Chittorgarh IPO page, matched later


def fy_parts(day: date, shift: int = 0) -> tuple[int, int, str]:
    """(month, year, 'YYYY-YY') path segments; shift=-1 asks about the previous FY."""
    start = (day.year if day.month >= 4 else day.year - 1) + shift
    if shift:
        day = date(start + 1, 3, 31)
    return day.month, day.year, f"{start}-{(start + 1) % 100:02d}"


def parse_gmp_row(row: dict, today: date) -> Ipo | None:
    name_html = str(row.get("Name") or "")
    name = clean(row.get("~ipo_name"))
    if not name:
        link = BeautifulSoup(name_html, "html.parser").find("a")
        name = clean(link.get_text(" ") if link else name_html)
    name = re.sub(r"\s+IPO$", "", name, flags=re.I).strip()
    if not name:
        return None

    category = clean(row.get("~IPO_Category"))
    is_sme = bool(re.search(r"\bSME\b", category or clean(name_html), re.I))

    price = to_num(pick(row, "Price", prefix=True))
    gmp_cell = clean(row.get("GMP"))
    gmp = to_num(gmp_cell) if re.search(r"\d", gmp_cell) else None
    if gmp is not None and price:
        gmp_pct = round(gmp / price * 100, 2)
    else:
        gmp_pct = to_num(row.get("~gmp_percent_calc"))
        if gmp_pct is None:
            m = re.search(r"\((-?\d+(?:\.\d+)?)\s*%\)", gmp_cell)
            gmp_pct = float(m.group(1)) if m else None

    folder = str(row.get("~urlrewrite_folder_name") or "").strip()
    if not folder:
        m = re.search(r'href="([^"]+)"', name_html)
        folder = m.group(1) if m else ""
    gmp_url = None
    if folder:
        gmp_url = folder if folder.startswith("http") else f"{INVESTORGAIN}/{folder.lstrip('/')}"
    slug = next((p for p in reversed(folder.strip("/").split("/"))
                 if p and not p.isdigit() and p.lower() != "gmp" and "." not in p), "")
    keys = [k for k in dict.fromkeys([norm_key(slug), norm_key(name)]) if k]

    lot = to_num(row.get("Lot"))
    return Ipo(
        name=name,
        is_sme=is_sme,
        price=price,
        gmp=gmp,
        gmp_pct=gmp_pct,
        lot=int(lot) if lot else None,
        open_date=parse_date(pick(row, "~Srt_Open", "Open"), today),
        close_date=parse_date(pick(row, "~Srt_Close", "Close"), today),
        listing_date=parse_date(pick(row, "~Str_Listing", "~Srt_Listing", "Listing"), today),
        subscribed=to_num(row.get("Sub")),
        size=clean(pick(row, "IPO Size", prefix=True)),
        gmp_updated=clean(row.get("Updated-On")),
        gmp_url=gmp_url,
        keys=keys,
    )


def fetch_ipos(today: date) -> list[Ipo]:
    month, year, fy = fy_parts(today)
    payload = get_json(GMP_FEED.format(month=month, year=year, fy=fy))
    rows = payload.get("reportTableData") if isinstance(payload, dict) else None
    if rows is None:
        raise FetchError("The GMP feed's format has changed (no 'reportTableData').")
    return [ipo for ipo in (parse_gmp_row(r, today) for r in rows if isinstance(r, dict)) if ipo]


# -------------------------------------------------- Chittorgarh IPO pages --
def fetch_page_index(today: date, shift: int = 0) -> dict[str, str]:
    """Map normalised company keys -> Chittorgarh IPO page URL."""
    month, year, fy = fy_parts(today, shift)
    payload = get_json(LIST_FEED.format(month=month, year=year, fy=fy))
    index: dict[str, str] = {}
    for row in (payload.get("reportTableData") or []) if isinstance(payload, dict) else []:
        company = str(row.get("Company") or "")
        m = re.search(r'href="([^"]*/ipo/([^/"]+)/(\d+)/?)"', company)
        if not m:
            continue
        url = m.group(1) if m.group(1).startswith("http") else f"{CHITTORGARH}/{m.group(1).lstrip('/')}"
        for key in (norm_key(m.group(2)), norm_key(company), norm_key(row.get("~compare_name"))):
            if key:
                index.setdefault(key, url)
    return index


def attach_page_urls(ipos: list[Ipo], today: date) -> None:
    missing = [i for i in ipos if not i.page_url]
    for shift in (0, -1):  # current FY list, then previous FY (April edge case)
        if not missing:
            return
        try:
            index = fetch_page_index(today, shift)
        except Exception as exc:  # financials are a bonus; never block the alert
            print(f"  Chittorgarh list unavailable: {exc}", file=sys.stderr)
            continue
        for ipo in list(missing):
            url = next((index[k] for k in ipo.keys if k in index), None)
            if not url:  # loose match, e.g. 'acmeindia' vs 'acmeindiaindustries'
                url = next((u for k in ipo.keys for ik, u in index.items()
                            if len(k) >= 6 and len(ik) >= 6 and (k in ik or ik in k)), None)
            if url:
                ipo.page_url = url
                missing.remove(ipo)


@dataclass
class Financials:
    labels: list[str]                       # oldest -> newest, e.g. FY24 FY25 FY26
    rows: dict[str, list[float | None]]     # metric -> values aligned with labels
    span_years: int
    stub_label: str | None = None           # latest part-year period, if disclosed
    stub: dict[str, float | None] = field(default_factory=dict)
    converted_from: str | None = None


@dataclass
class Brief:
    about: str | None = None
    fin: Financials | None = None
    pe_pre: float | None = None
    pe_post: float | None = None
    mcap_cr: float | None = None


HEADING_TAGS = ["h1", "h2", "h3", "h4", "h5", "h6", "caption", "strong", "b", "p", "div", "span", "a"]
FIN_METRICS = [
    ("revenue", r"^revenue|revenue from operations|total revenue"),
    ("income", r"total income"),
    ("ebitda", r"ebitda"),
    ("pat", r"profit.{0,12}after tax|^pat\b|net profit"),
    ("networth", r"net\s*worth"),
    ("debt", r"borrowing|total debt"),
    ("assets", r"^(total )?assets"),
]


def metric_key(label: str) -> str | None:
    label = clean(label).lower()
    return next((key for key, rx in FIN_METRICS if re.search(rx, label)), None)


def table_grid(table) -> list[list[str]]:
    grid = [[c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
            for tr in table.find_all("tr")]
    return [r for r in grid if any(r)]


def find_table_after(soup, heading_rx: str, must_have_rx: str):
    for tag in soup.find_all(HEADING_TAGS):
        text = tag.get_text(" ", strip=True)
        if not text or len(text) > 90 or not re.search(heading_rx, text, re.I):
            continue
        table = tag.find_next("table")
        if table is not None and re.search(must_have_rx, table.get_text(" ", strip=True), re.I):
            return table
    return None


def unit_factor(text: str) -> tuple[float, str | None]:
    """Multiplier that converts the table's unit to ₹ crore."""
    t = text.lower()
    if re.search(r"lakh|\blacs?\b", t):
        return 0.01, "₹ lakh"
    if re.search(r"million|\bmn\b", t):
        return 0.1, "₹ million"
    return 1.0, None


def parse_financials(soup) -> Financials | None:
    table = find_table_after(soup, r"financ", r"profit.{0,12}after tax|\bPAT\b|total income|revenue")
    if table is None:
        for t in soup.find_all("table"):
            txt = t.get_text(" ", strip=True)
            if re.search(r"profit.{0,12}after tax", txt, re.I) and \
                    len(re.findall(r"\b(?:Mar|Jun|Sep|Dec)[a-z]*\b", txt)) >= 2:
                table = t
                break
    if table is None:
        return None

    context = table.get_text(" ", strip=True)
    for sib in list(table.next_siblings)[:3]:
        text = sib.get_text(" ", strip=True) if hasattr(sib, "get_text") else str(sib)
        context += " " + clean(text)[:200]
    factor, converted = unit_factor(context)

    grid = table_grid(table)
    series: dict[str, dict[date, float]] = {}
    header_i, period_cols = None, {}
    for i, row in enumerate(grid[:3]):  # periods across the top (Chittorgarh layout)
        cols = {j: d for j, cell in enumerate(row[1:], start=1) if (d := parse_date(cell))}
        if len(cols) >= 2:
            header_i, period_cols = i, cols
            break
    if header_i is not None:
        for row in grid[header_i + 1:]:
            key = metric_key(row[0]) if row else None
            for j, d in period_cols.items():
                if key and j < len(row) and (v := to_num(row[j])) is not None:
                    series.setdefault(key, {})[d] = v * factor
    elif grid:  # periods down the first column
        keys = {j: metric_key(h) for j, h in enumerate(grid[0])}
        for row in grid[1:]:
            d = parse_date(row[0]) if row else None
            for j, key in keys.items():
                if d and key and j < len(row) and (v := to_num(row[j])) is not None:
                    series.setdefault(key, {})[d] = v * factor
    if not ({"pat", "income", "revenue"} & series.keys()):
        return None

    periods = sorted({d for s in series.values() for d in s}, reverse=True)
    year_ends = [d for d in periods if d.month == 3] or periods
    years = sorted(year_ends[:3])
    latest = years[-1]
    stubs = [d for d in periods if d > latest]

    def label(d: date) -> str:
        return f"FY{d.year % 100:02d}" if d.month == 3 else f"{d:%b}'{d:%y}"

    fin = Financials(
        labels=[label(d) for d in years],
        rows={k: [s.get(d) for d in years] for k, s in series.items()},
        span_years=max(1, years[-1].year - years[0].year),
        converted_from=converted,
    )
    if stubs:
        stub = stubs[0]
        months = (stub.year - latest.year) * 12 + stub.month - latest.month
        fin.stub_label = f"{months}M to {stub:%b}'{stub:%y}"
        fin.stub = {k: s.get(stub) for k, s in series.items()}
    return fin


def parse_valuation(soup, brief: Brief) -> None:
    table = find_table_after(soup, r"valuation", r"P\s*/\s*E|EPS|market cap")
    if table is not None:
        grid = table_grid(table)
        pre_i = post_i = None
        for row in grid[:2]:
            for j, cell in enumerate(row):
                if pre_i is None and re.search(r"\bpre\b", cell, re.I):
                    pre_i = j
                if post_i is None and re.search(r"\bpost\b", cell, re.I):
                    post_i = j
        for row in grid:
            label = row[0].lower() if row else ""
            if re.search(r"p\s*/\s*e|price.?to.?earn", label):
                vals = {j: to_num(c) for j, c in enumerate(row) if j and re.search(r"\d", c)}
                if pre_i is not None or post_i is not None:
                    brief.pe_pre, brief.pe_post = vals.get(pre_i), vals.get(post_i)
                else:
                    nums = list(vals.values())
                    brief.pe_pre = nums[0] if nums else None
                    brief.pe_post = nums[1] if len(nums) > 1 else None
            elif "market cap" in label:
                cell = next((c for c in row[1:] if re.search(r"\d", c)), "")
                if (v := to_num(cell)) is not None:
                    brief.mcap_cr = v * unit_factor(cell)[0]
    if brief.mcap_cr is None:
        m = re.search(r"Market Cap\w*[^₹\d]{0,40}₹\s*([\d,]+(?:\.\d+)?)\s*(Cr|crore|lakh)",
                      soup.get_text(" ", strip=True), re.I)
        if m:
            brief.mcap_cr = float(m.group(1).replace(",", "")) * unit_factor(m.group(2))[0]


def parse_about(soup) -> str | None:
    for tag in soup.find_all(["h2", "h3"]):
        heading = tag.get_text(" ", strip=True)
        if not re.match(r"about\b", heading, re.I) or "chittorgarh" in heading.lower():
            continue
        para = tag.find_next("p")
        text = re.sub(r"\s+", " ", para.get_text(" ", strip=True)) if para else ""
        if len(text) < 40:
            continue
        summary = ""
        for sentence in re.split(r"(?<=[.!?])\s+(?=[A-Z])", text):
            summary = f"{summary} {sentence}".strip()
            if len(summary) > 110:
                break
        return summary if len(summary) <= 240 else summary[:237].rstrip() + "…"
    return None


def fetch_brief(url: str) -> Brief:
    soup = BeautifulSoup(http_get(url, page=True).decode("utf-8", errors="replace"), "html.parser")
    brief = Brief()
    for step in (lambda: setattr(brief, "about", parse_about(soup)),
                 lambda: setattr(brief, "fin", parse_financials(soup)),
                 lambda: parse_valuation(soup, brief)):
        try:
            step()
        except Exception as exc:  # one odd table shouldn't sink the rest
            print(f"  parse warning ({url}): {exc}", file=sys.stderr)
    return brief


# -------------------------------------------------------------- messages --
def esc(text: str) -> str:
    """Escape text for Telegram HTML (keeps apostrophes readable)."""
    return html.escape(str(text), quote=False)


def inr(n: float) -> str:
    """Indian grouping: 1234567 -> 12,34,567."""
    s = f"{abs(round(n)):d}"
    if len(s) > 3:
        s = re.sub(r"(\d)(?=(\d{2})+$)", r"\1,", s[:-3]) + "," + s[-3:]
    return ("-" if n < 0 else "") + s


def dm(d: date, weekday: bool = False) -> str:
    """5 Oct, or Mon 5 Oct."""
    return (f"{d:%a} " if weekday else "") + f"{d.day} {d:%b}"


def amt(n: float | None) -> str:
    if n is None:
        return "?"
    return inr(n) if abs(n - round(n)) < 0.005 else f"{n:,.2f}"


def cell(v: float | None, scale: float | None = None) -> str:
    """Format ₹ Cr figures; `scale` (the row's largest value) keeps decimals consistent."""
    if v is None:
        return "–"
    ref = abs(scale if scale is not None else v)
    if ref >= 1000:
        return inr(v)
    return f"{v:.1f}" if ref >= 10 else f"{v:.2f}"


def row_cells(values: list[float | None]) -> list[str]:
    scale = max((abs(v) for v in values if v is not None), default=None)
    return [cell(v, scale) for v in values]


def crore(v: float) -> str:
    return inr(v) if abs(v) >= 1000 else f"{v:.1f}".removesuffix(".0")


def growth(a: float | None, b: float | None, years: int) -> float | None:
    if a is None or b is None or a <= 0 or b <= 0:
        return None
    return ((b / a) ** (1 / years) - 1) * 100


def financial_block(fin: Financials) -> list[str]:
    e = esc
    n = len(fin.labels)
    blank = [None] * n
    rev_key = "revenue" if any(v is not None for v in fin.rows.get("revenue", [])) else "income"
    rev_label = "Revenue" if rev_key == "revenue" else "Total income"
    rev, pat = fin.rows.get(rev_key, blank), fin.rows.get("pat", blank)
    nw, debt = fin.rows.get("networth", blank), fin.rows.get("debt", blank)

    table_rows = []
    for label, key in [(rev_label, rev_key), ("EBITDA", "ebitda"), ("PAT", "pat"),
                       ("PAT margin", None), ("Net worth", "networth"), ("Borrowings", "debt")]:
        if key is None:
            vals = [p / r * 100 if p is not None and r else None for p, r in zip(pat, rev)]
            cells = [f"{v:.1f}%" if v is not None else "–" for v in vals]
        else:
            cells = row_cells(fin.rows.get(key, blank))
        if any(c != "–" for c in cells):
            table_rows.append((label, cells))
    lw = max(len("₹ Cr"), *(len(lbl) for lbl, _ in table_rows))
    cw = max(7, *(len(c) + 2 for _, cs in table_rows for c in cs))
    lines = [f"{'₹ Cr':<{lw}}" + "".join(f"{lbl:>{cw}}" for lbl in fin.labels)]
    lines += [f"{lbl:<{lw}}" + "".join(f"{c:>{cw}}" for c in cs) for lbl, cs in table_rows]

    unit = ", converted from " + fin.converted_from if fin.converted_from else ""
    out = [f"📊 <b>Financials</b> (restated{unit})", "<pre>" + e("\n".join(lines)) + "</pre>"]

    word = "CAGR" if fin.span_years >= 2 else "growth"
    bits = []
    if (g := growth(rev[0], rev[-1], fin.span_years)) is not None:
        bits.append(f"{rev_label} {word} {g:+.0f}%")
    if (g := growth(pat[0], pat[-1], fin.span_years)) is not None:
        bits.append(f"PAT {word} {g:+.0f}%")
    if bits and n > 1:
        out.append(" · ".join(bits) + f" ({fin.labels[0]}→{fin.labels[-1]})")
    bits = []
    if debt[-1] is not None and nw[-1] and nw[-1] > 0:
        bits.append(f"Debt/Equity {debt[-1] / nw[-1]:.2f}")
    if pat[-1] is not None and nw[-1] and nw[-1] > 0:
        bits.append(f"RoNW ≈{pat[-1] / nw[-1] * 100:.0f}%")
    if bits:
        out.append(" · ".join(bits) + f" ({fin.labels[-1]})")
    if fin.stub_label and (fin.stub.get(rev_key) is not None or fin.stub.get("pat") is not None):
        out.append(f"Latest {e(fin.stub_label)}: {rev_label} {cell(fin.stub.get(rev_key))} · "
                   f"PAT {cell(fin.stub.get('pat'))}")

    notes = []
    losses = [lbl for lbl, p in zip(fin.labels, pat) if p is not None and p < 0]
    if losses:
        notes.append("Loss in " + ", ".join(losses))
    if n >= 2:
        if rev[-2] and rev[-1] is not None and rev[-2] > 0 and rev[-1] < rev[-2]:
            notes.append(f"{rev_label} fell {(1 - rev[-1] / rev[-2]) * 100:.0f}% in {fin.labels[-1]}")
        if pat[-2] and pat[-1] is not None and pat[-2] > 0 and 0 <= pat[-1] < pat[-2]:
            notes.append(f"PAT fell {(1 - pat[-1] / pat[-2]) * 100:.0f}% in {fin.labels[-1]}")
        if pat[-2] and pat[-1] and pat[-2] > 0 and pat[-1] / pat[-2] >= 2.5:
            notes.append(f"PAT up {pat[-1] / pat[-2]:.1f}x in {fin.labels[-1]} (check for one-offs)")
    if nw[-1] is not None and nw[-1] < 0:
        notes.append("Negative net worth")
    out += ["⚠️ " + e(note) for note in notes]
    return out


def valuation_line(brief: Brief) -> str | None:
    bits = []
    if brief.mcap_cr:
        bits.append(f"Mcap ₹{crore(brief.mcap_cr)} Cr")
    if brief.pe_pre is not None and brief.pe_post is not None:
        bits.append(f"P/E {brief.pe_pre:.1f}x pre / {brief.pe_post:.1f}x post-issue")
    elif (pe := brief.pe_post if brief.pe_post is not None else brief.pe_pre) is not None:
        bits.append(f"P/E {pe:.1f}x")
    return "💰 " + " · ".join(bits) if bits else None


def compose(ipo: Ipo, brief: Brief | None, today: date, test: bool = False) -> str:
    e = esc
    title = f"{e(ipo.name)} IPO" if test else f"Last day: {e(ipo.name)} IPO"
    out = [("🧪 <b>TEST</b> · " if test else "🔔 ") + f"<b>{title}</b>"]
    out.append("<i>" + " · ".join(["SME" if ipo.is_sme else "Mainboard"] +
                                  ([e(ipo.size)] if ipo.size else [])) + "</i>")
    if brief and brief.about:
        out.append(e(brief.about))
    out.append("")

    if ipo.gmp is not None and ipo.price:
        out.append(f"📈 <b>GMP ₹{amt(ipo.gmp)} ({ipo.gmp_pct:+.1f}%)</b> on ₹{amt(ipo.price)} "
                   f"→ est. listing ≈ ₹{amt(ipo.price + ipo.gmp)}")
    elif ipo.gmp_pct is not None:
        out.append(f"📈 <b>GMP {ipo.gmp_pct:+.1f}%</b>")
    if ipo.lot and ipo.price:
        line = f"Lot {inr(ipo.lot)} shares = ₹{inr(ipo.lot * ipo.price)}"
        if ipo.gmp:
            line += f" · GMP per lot ≈ ₹{inr(ipo.lot * ipo.gmp)}"
        out.append(line)
    bits = []
    if ipo.subscribed is not None:
        bits.append(f"Subscribed so far {ipo.subscribed:.2f}x")
    if ipo.gmp_updated:
        bits.append(f"GMP as of {e(ipo.gmp_updated)}")
    if bits:
        out.append(" · ".join(bits))
    if ipo.close_date == today:
        when = "⏰ Closes today, 5 PM (many brokers stop earlier)"
    else:
        when = f"⏰ Closes {dm(ipo.close_date, True)}" if ipo.close_date else "⏰ Close date n/a"
    if ipo.listing_date:
        when += f" · lists {dm(ipo.listing_date)}"
    out += [when, ""]

    if brief and brief.fin:
        out += financial_block(brief.fin)
    else:
        out.append("📊 Financials: couldn't read them automatically — see the IPO page below."
                   if ipo.page_url else
                   "📊 Financials: couldn't find this company on Chittorgarh — check its RHP.")
    if brief and (val := valuation_line(brief)):
        out.append(e(val))
    out.append("")

    links = []
    if ipo.page_url:
        links.append(f'<a href="{html.escape(ipo.page_url)}">IPO details</a>')
    if ipo.gmp_url:
        links.append(f'<a href="{html.escape(ipo.gmp_url)}">GMP trend</a>')
    if links:
        out.append(" · ".join(links))
    out.append("<i>GMP is unofficial and can move fast. Not a promise of listing gains.</i>")
    return "\n".join(out)


def compose_test_summary(today: date, ipos: list[Ipo], sample: bool) -> str:
    e = esc
    rule = f"alert on an IPO's last day if GMP ≥ {GMP_THRESHOLD_PCT:g}%"
    if not INCLUDE_SME:
        rule += " (mainboard only)"
    lines = [f"🧪 <b>IPO alert — test run</b> · {dm(today, True)} {today.year}", f"Rule: {rule}", ""]
    if not ipos:
        lines.append("No IPOs are open right now. The feed is working, so you'll hear from me "
                     "when one qualifies.")
        return "\n".join(lines)
    lines.append("<b>Open / upcoming</b> (GMP · closes):")
    for i in ipos[:15]:
        mark = "✅" if i.gmp_pct is not None and i.gmp_pct >= GMP_THRESHOLD_PCT else "▫️"
        pct = f"{i.gmp_pct:+.1f}%" if i.gmp_pct is not None else "no GMP"
        lines.append(f"{mark} {e(i.name)}{' (SME)' if i.is_sme else ''} · {pct} · "
                     f"{dm(i.close_date, True)}")
    lines += ["", "✅ = would trigger an alert on its last day."
              + (" Sample brief below." if sample else "")]
    return "\n".join(lines)


def compose_error(err: Exception) -> str:
    return ("⚠️ <b>IPO alert couldn't check today's IPOs</b>\n"
            f"<code>{esc(str(err))[:400]}</code>\n\n"
            f'Check manually: <a href="{GMP_PAGE}">InvestorGain live GMP</a>\n'
            "If this keeps happening, the data source has probably changed its format.")


# ---------------------------------------------------------------- sending --
def channels() -> list[str]:
    out = []
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        out.append("telegram")
    if WHATSAPP_PHONE and CALLMEBOT_APIKEY:
        out.append("whatsapp")
    return out


def to_whatsapp(text: str) -> str:
    """Telegram HTML -> WhatsApp formatting (*bold*, _italic_, ```monospace```)."""
    t = re.sub(r"</a>\s*·\s*<a ", "</a>\n<a ", text)               # one link per line
    t = re.sub(r'<a href="([^"]+)">(.*?)</a>', r"\2: \1", t, flags=re.S)
    t = re.sub(r"<pre>(.*?)</pre>", r"```\n\1\n```", t, flags=re.S)
    t = re.sub(r"</?b>", "*", t)
    t = re.sub(r"</?i>", "_", t)
    t = re.sub(r"</?code>", "```", t)
    t = re.sub(r"<[^>]+>", "", t)
    return html.unescape(t)


def chunks(text: str, limit: int = 1800) -> list[str]:
    """Split long texts at blank lines so each WhatsApp message stays a sane size."""
    parts, cur = [], ""
    for block in text.split("\n\n"):
        if cur and len(cur) + len(block) + 2 > limit:
            parts.append(cur)
            cur = block
        else:
            cur = f"{cur}\n\n{block}" if cur else block
    return [p[:limit] for p in parts + [cur] if p.strip()]


def send_telegram(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, timeout=30, json={
        "chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True}})
    if resp.status_code == 400 and "parse" in resp.text.lower():
        plain = html.unescape(re.sub(r"<[^>]+>", "", text))  # never lose an alert to formatting
        resp = requests.post(url, timeout=30, json={"chat_id": TELEGRAM_CHAT_ID, "text": plain[:4096]})
    if not resp.ok:
        raise RuntimeError(f"Telegram returned {resp.status_code}: {resp.text[:200]}")


def send_whatsapp(text: str) -> None:
    for part in chunks(to_whatsapp(text)):
        resp = None
        for attempt in range(3):  # CallMeBot is a free service and sometimes slow
            try:
                resp = requests.get("https://api.callmebot.com/whatsapp.php", timeout=60, params={
                    "phone": WHATSAPP_PHONE, "text": part, "apikey": CALLMEBOT_APIKEY})
                body = clean(resp.text)
                if resp.ok and not re.search(r"invalid|error|not (?:yet )?activated|blocked|wrong",
                                             body, re.I):
                    break
            except requests.RequestException as exc:
                body = str(exc)
            time.sleep(5 * (attempt + 1))
        else:
            code = resp.status_code if resp is not None else "no response"
            raise RuntimeError(f"WhatsApp (CallMeBot) failed ({code}): {body[:200]}")
        time.sleep(2)  # keep multi-part messages in order


def send(text: str, dry_run: bool) -> None:
    """Send to every configured channel; one failing channel doesn't stop the other."""
    if dry_run:
        print("\n" + "-" * 44 + "\n" + text + "\n")
        return
    errors = []
    for name, fn in (("telegram", send_telegram), ("whatsapp", send_whatsapp)):
        if name in channels():
            try:
                fn(text)
            except Exception as exc:
                errors.append(str(exc))
                print(f"  {exc}", file=sys.stderr)
    if errors and len(errors) == len(channels()):
        raise RuntimeError("; ".join(errors))
    SEND_ERRORS.extend(errors)


SEND_ERRORS: list[str] = []


def deliver(ipos: list[Ipo], today: date, dry_run: bool, test: bool = False) -> None:
    attach_page_urls(ipos, today)
    for ipo in sorted(ipos, key=lambda i: -(i.gmp_pct or 0)):
        brief = None
        if ipo.page_url:
            try:
                brief = fetch_brief(ipo.page_url)
            except Exception as exc:
                print(f"  financials unavailable for {ipo.name}: {exc}", file=sys.stderr)
        else:
            print(f"  no Chittorgarh page found for {ipo.name}", file=sys.stderr)
        send(compose(ipo, brief, today, test=test), dry_run)
        pct = f" ({ipo.gmp_pct:+.1f}%)" if ipo.gmp_pct is not None else ""
        print(f"  sent: {ipo.name}{pct}")
        time.sleep(1)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Last-day IPO GMP alert -> Telegram")
    ap.add_argument("--test", action="store_true",
                    help="send a sample alert now, ignoring the closing-day and GMP filters")
    ap.add_argument("--dry-run", action="store_true", help="print messages instead of sending")
    ap.add_argument("--date", type=date.fromisoformat, help="pretend today is YYYY-MM-DD")
    args = ap.parse_args(argv)

    dry_run = args.dry_run
    if not dry_run and not channels():
        if os.getenv("GITHUB_ACTIONS") == "true":
            print("ERROR: add repository secrets for at least one channel: TELEGRAM_BOT_TOKEN + "
                  "TELEGRAM_CHAT_ID, and/or WHATSAPP_PHONE + CALLMEBOT_APIKEY.", file=sys.stderr)
            return 2
        print("(No Telegram or WhatsApp credentials set, so printing instead of sending.)")
        dry_run = True

    today = args.date or datetime.now(IST).date()
    print(f"Date (IST) {today} · threshold {GMP_THRESHOLD_PCT:g}% · SME {'on' if INCLUDE_SME else 'off'}"
          f" · sending to {', '.join(channels()) if not dry_run else 'screen (dry run)'}")

    try:
        ipos = fetch_ipos(today)
    except Exception as exc:
        print(f"Couldn't read the GMP feed: {exc}", file=sys.stderr)
        try:
            send(compose_error(exc), dry_run)
        except Exception as send_exc:
            print(f"...and couldn't send the warning either: {send_exc}", file=sys.stderr)
        return 1
    if not INCLUDE_SME:
        ipos = [i for i in ipos if not i.is_sme]
    print(f"{len(ipos)} IPOs in the GMP feed")

    if args.test:
        live = [i for i in ipos if i.close_date and i.close_date >= today
                and (i.open_date is None or i.open_date <= today)]
        live = live or [i for i in ipos if i.close_date and i.close_date >= today]
        live.sort(key=lambda i: (i.gmp_pct is None, -(i.gmp_pct or 0)))
        sample = bool(live) and live[0].gmp_pct is not None
        send(compose_test_summary(today, live, sample), dry_run)
        if sample:
            deliver([live[0]], today, dry_run, test=True)
        return 1 if SEND_ERRORS else 0

    closing = [i for i in ipos if i.close_date == today]
    for i in closing:
        print(f"  closes today: {i.name} · GMP {i.gmp_pct if i.gmp_pct is not None else 'n/a'}%")
    due = [i for i in closing if i.gmp_pct is not None and i.gmp_pct >= GMP_THRESHOLD_PCT]
    if not due:
        print("Nothing qualifies today.")
        return 0
    deliver(due, today, dry_run)
    return 1 if SEND_ERRORS else 0  # a failed channel turns the run red so GitHub emails you


if __name__ == "__main__":
    sys.exit(main())
