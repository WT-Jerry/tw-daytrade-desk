#!/usr/bin/env python3
"""
台股當沖觀察池篩選器 — 選股規則 v1

對齊 skills:
  - day-trading-report-workflow
  - twse-daily-screener

用法:
  python3 twse_screener.py --date 20260717
  python3 twse_screener.py --date 20260717 --relaxed-auto
  python3 twse_screener.py --date 20260717 --top 10 --format table
  python3 twse_screener.py                 # 使用 openapi 最新交易日

輸出:
  JSON（預設）寫入 ~/.hermes/cache/finance/screener_{date}.json
  並印到 stdout
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover
    yaml = None


DEFAULT_CONFIG: Dict[str, Any] = {
    "price_min": 10.0,
    "price_max": 150.0,
    "volume_lots_min": 6000,
    "turnover_value_min": 500_000_000,
    "daytrade_rate_min": 0.25,
    "amplitude_min": 0.05,
    "output_top_n": 15,
    "relax_if_count_lt": 3,
    "relax": {
        "daytrade_rate_min": 0.20,
        "amplitude_min": 0.04,
        "turnover_value_min": 300_000_000,
        "price_max": 155.0,
    },
    "scoring": {
        "turnover_rate_bonus_1": 0.03,
        "turnover_rate_bonus_2": 0.05,
        "daytrade_rate_bonus": 0.30,
        "daytrade_rate_bonus_2": 0.40,
        "daytrade_amount_bonus": 300_000_000,
        "abs_change_pct_bonus": 0.03,
        "rel_volume_mult": 1.5,
        "avg_volume_lookback_days": 5,   # 前 N 個交易日（不含資料日 D）
        "avg_volume_min_days": 3,       # 至少幾日才計算均量加分
        "surge_volume_lookback_days": 15,  # 爆大量：近 15 交易日均量
        "surge_volume_min_days": 10,       # 至少幾日才判定爆大量；不足則 n/a
        "surge_volume_mult": 2.0,          # 當日量 ≥ 均量 × 此倍數
        "inst_net_ratio_min": 0.03,
        "inst_net_ratio_strong": 0.05,
        "broker_top5_ratio_min": 0.03,
        "broker_top5_ratio_strong": 0.05,
        "broker_daytrade_lots": 300,
    },
    "exclude": {
        "etf_and_leverage": True,
        "code_suffixes_block": list("LRUKBCTAD"),
        "attention_notice": True,   # 注意股
        "disposition": True,        # 處置股
    },
    "features": {
        "auto_shares": True,        # 自動抓已發行普通股數 → 週轉率加分
        "auto_watchlists": True,    # 自動抓注意/處置
        "auto_avg_volume": True,    # 自動抓前 5 交易日均量 → 相對量能加分
        "auto_institutional": True, # TWSE T86
        "auto_broker": True,        # 分點（指定資料日）
    },
    "request": {
        "timeout_sec": 45,
        "interval_sec": 1.0,  # 每次打 TWSE／OpenAPI 至少隔 1 秒，避免被擋
        "user_agent": "hermes-daytrade-screener/1.0 (+local)",
    },
    "paths": {
        "cache_dir": "~/.hermes/cache/finance",
        "output_dir": "~/.hermes/cache/finance",
    },
}


SCORE_CAP = 100.0

_last_http_mono = 0.0
_http_interval_sec = 1.0


def set_http_interval_sec(sec: float) -> None:
    """設定 TWSE／OpenAPI 兩次請求的最短間隔（秒）。0 關閉。"""
    global _http_interval_sec
    try:
        _http_interval_sec = max(0.0, float(sec))
    except (TypeError, ValueError):
        _http_interval_sec = 1.0


def _wait_http_interval() -> None:
    """上一筆結束後再等 interval_sec，才發下一筆。"""
    if _http_interval_sec <= 0 or _last_http_mono <= 0:
        return
    wait = _http_interval_sec - (time.monotonic() - _last_http_mono)
    if wait > 0:
        time.sleep(wait)


def _mark_http_done() -> None:
    global _last_http_mono
    _last_http_mono = time.monotonic()


def clamp_score(x: float, cap: float = SCORE_CAP) -> float:
    return max(0.0, min(float(cap), float(x)))

def _expand(p: str) -> Path:
    return Path(p).expanduser().resolve()


def _parse_simple_yaml_scalars(text: str) -> Dict[str, Any]:
    """Very small subset parser for our config if PyYAML missing."""
    # Prefer real yaml when available; this is fallback only.
    data: Dict[str, Any] = {}
    stack: List[Tuple[int, dict]] = [( -1, data )]
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if line.endswith(":") and ":" == line[-1] and line.count(":") == 1:
            key = line[:-1].strip().strip("'\"")
            while stack and indent <= stack[-1][0]:
                stack.pop()
            parent = stack[-1][1]
            parent[key] = {}
            stack.append((indent, parent[key]))
            continue
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key = key.strip().strip("'\"")
        val = val.strip()
        if val == "":
            node: Any = {}
        elif val.lower() in {"true", "false"}:
            node = val.lower() == "true"
        elif val.startswith("[") and val.endswith("]"):
            inner = val[1:-1].strip()
            if not inner:
                node = []
            else:
                parts = [p.strip().strip("'\"") for p in inner.split(",")]
                node = parts
        else:
            val_u = val.strip("'\"")
            try:
                if "." in val_u:
                    node = float(val_u)
                else:
                    node = int(val_u)
            except ValueError:
                node = val_u
        while stack and indent <= stack[-1][0]:
            stack.pop()
        stack[-1][1][key] = node
        if isinstance(node, dict):
            stack.append((indent, node))
    return data


def load_config(path: Optional[str]) -> Dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy via json
    candidates = []
    if path:
        candidates.append(Path(path))
    else:
        here = Path(__file__).resolve().parent
        candidates.append(here / "daytrade_screener_v1.yaml")
        candidates.append(here / "daytrade_screener_v1.yml")
    for c in candidates:
        if c.is_file():
            text = c.read_text(encoding="utf-8")
            if yaml is not None:
                data = yaml.safe_load(text) or {}
            else:
                data = _parse_simple_yaml_scalars(text)
            block = data.get("daytrade_screener_v1", data)
            if isinstance(block, dict):
                _deep_merge(cfg, block)
            break
    return cfg


def _deep_merge(base: dict, overlay: dict) -> dict:
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def http_get(url: str, timeout: int, ua: str, retries: int = 3) -> str:
    last_err: Optional[Exception] = None
    _wait_http_interval()
    try:
        for i in range(retries):
            try:
                req = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": ua,
                        "Accept": "application/json,text/plain,*/*",
                    },
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return resp.read().decode("utf-8-sig", errors="replace")
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(1.2 * (i + 1))
        raise RuntimeError(f"GET failed {url}: {last_err}")
    finally:
        _mark_http_done()


def to_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    s = str(x).strip().replace(",", "").replace("%", "").replace("元", "")
    if s in {"", "-", "--", "---", "null", "None", "X", "x"}:
        return None
    # 漲跌價差可能帶 + / -
    s = s.replace("+", "")
    try:
        return float(s)
    except ValueError:
        return None


def to_int(x: Any) -> Optional[int]:
    f = to_float(x)
    if f is None:
        return None
    return int(round(f))


def roc_date_to_ymd(s: str) -> Optional[str]:
    """'1150806' or '115/08/06' -> '20260806'"""
    s = str(s).strip().replace("-", "").replace("/", "")
    if len(s) == 7 and s.isdigit():
        y = int(s[:3]) + 1911
        return f"{y}{s[3:]}"
    if len(s) == 8 and s.isdigit():
        return s
    return None


def ymd_to_roc_slash(ymd: str) -> str:
    y, m, d = int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8])
    return f"{y - 1911}/{m:02d}/{d:02d}"


# ---------------------------------------------------------------------------
# data models
# ---------------------------------------------------------------------------

@dataclass
class StockRow:
    code: str
    name: str
    date: str  # YYYYMMDD
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    prev_close: Optional[float] = None
    change: Optional[float] = None
    volume_shares: Optional[int] = None
    turnover_value: Optional[float] = None
    transactions: Optional[int] = None
    # daytrade
    daytrade_shares: Optional[int] = None
    daytrade_buy_value: Optional[float] = None
    daytrade_sell_value: Optional[float] = None
    daytrade_suspend_flag: str = ""
    # optional
    shares_outstanding: Optional[float] = None
    avg_volume_5d_lots: Optional[float] = None
    avg_volume_15d_lots: Optional[float] = None
    market: str = "TWSE"
    # watchlist flags
    is_attention: bool = False
    is_disposition: bool = False
    attention_reason: str = ""
    disposition_period: str = ""
    # 三大法人（股，買賣超）
    inst_net_shares: Optional[int] = None
    inst_foreign_shares: Optional[int] = None
    inst_trust_shares: Optional[int] = None
    inst_dealer_shares: Optional[int] = None


@dataclass
class ScreenResult:
    code: str
    name: str
    date: str
    market: str
    close: float
    open: Optional[float]
    high: float
    low: float
    prev_close: float
    change_pct: float
    amplitude: float
    volume_lots: float
    turnover_value: float
    daytrade_rate: float
    daytrade_shares: int
    daytrade_buy_value: Optional[float]
    daytrade_sell_value: Optional[float]
    close_position: float
    turnover_rate: Optional[float]
    avg_volume_5d_lots: Optional[float]
    volume_ratio: Optional[float]  # 當日量 / 5日均量
    quality_score: float
    score_breakdown: Dict[str, float]
    bias_hint: str
    support_obs: float
    resistance_obs: float
    notes: List[str] = field(default_factory=list)
    relaxed: bool = False
    shares_outstanding: Optional[float] = None
    vwap: Optional[float] = None
    upper_wick_frac: Optional[float] = None
    lower_wick_frac: Optional[float] = None
    body_frac: Optional[float] = None
    wick_tag: str = ""
    near_limit: str = ""
    inst_net_lots: Optional[float] = None
    inst_net_ratio: Optional[float] = None
    inst_foreign_lots: Optional[float] = None
    inst_trust_lots: Optional[float] = None
    broker_top5_buy_lots: Optional[float] = None
    broker_top5_buy_ratio: Optional[float] = None
    broker_top5_sell_lots: Optional[float] = None
    broker_top5_sell_ratio: Optional[float] = None
    broker_top_buy: str = ""
    broker_top_sell: str = ""
    broker_daytrade_hint: bool = False
    avg_volume_15d_lots: Optional[float] = None
    volume_ratio_15d: Optional[float] = None  # 當日量 / 15 日均量
    surge_volume: bool = False
    surge_volume_label: str = "n/a"
    surge_volume_dir: str = ""  # up / down / ""

    def to_public_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # round for readability
        for k in (
            "close",
            "open",
            "high",
            "low",
            "prev_close",
            "change_pct",
            "amplitude",
            "volume_lots",
            "daytrade_rate",
            "close_position",
            "quality_score",
            "support_obs",
            "resistance_obs",
            "turnover_rate",
            "avg_volume_5d_lots",
            "volume_ratio",
            "avg_volume_15d_lots",
            "volume_ratio_15d",
            "vwap",
            "upper_wick_frac",
            "lower_wick_frac",
            "body_frac",
            "inst_net_lots",
            "inst_net_ratio",
            "inst_foreign_lots",
            "inst_trust_lots",
            "broker_top5_buy_lots",
            "broker_top5_buy_ratio",
            "broker_top5_sell_lots",
            "broker_top5_sell_ratio",
        ):
            if d.get(k) is not None and isinstance(d[k], float):
                d[k] = round(
                    d[k],
                    6
                    if k == "turnover_rate"
                    else (
                        3
                        if k in {"volume_ratio", "volume_ratio_15d", "inst_net_ratio", "broker_top5_buy_ratio", "broker_top5_sell_ratio", "upper_wick_frac", "lower_wick_frac", "body_frac"}
                        else (4 if k in {"daytrade_rate", "close_position", "amplitude", "change_pct"} else 2)
                    ),
                )
        d["turnover_value"] = int(d["turnover_value"]) if d.get("turnover_value") is not None else None
        d["daytrade_rate_pct"] = round(self.daytrade_rate * 100, 2)
        d["amplitude_pct"] = round(self.amplitude * 100, 2)
        d["change_pct_display"] = round(self.change_pct * 100, 2)
        d["close_pos_pct"] = round(self.close_position * 100, 1)
        if self.turnover_rate is not None:
            d["turnover_rate_pct"] = round(self.turnover_rate * 100, 3)
        return d


# ---------------------------------------------------------------------------
# fetchers
# ---------------------------------------------------------------------------

def fetch_stock_day_all(timeout: int, ua: str) -> Tuple[str, Dict[str, StockRow]]:
    """OpenAPI latest full market day bars. Returns (YYYYMMDD, rows)."""
    url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
    raw = http_get(url, timeout, ua)
    data = json.loads(raw)
    if not isinstance(data, list) or not data:
        raise RuntimeError("STOCK_DAY_ALL empty")
    out: Dict[str, StockRow] = {}
    date_ymd = None
    for item in data:
        code = str(item.get("Code", "")).strip()
        if not code:
            continue
        d = roc_date_to_ymd(str(item.get("Date", "")))
        if d:
            date_ymd = d
        close = to_float(item.get("ClosingPrice"))
        chg = to_float(item.get("Change"))
        prev = None
        if close is not None and chg is not None:
            prev = close - chg
        row = StockRow(
            code=code,
            name=str(item.get("Name", "")).strip(),
            date=d or "",
            open=to_float(item.get("OpeningPrice")),
            high=to_float(item.get("HighestPrice")),
            low=to_float(item.get("LowestPrice")),
            close=close,
            prev_close=prev,
            change=chg,
            volume_shares=to_int(item.get("TradeVolume")),
            turnover_value=to_float(item.get("TradeValue")),
            transactions=to_int(item.get("Transaction")),
            market="TWSE",
        )
        out[code] = row
    if not date_ymd:
        raise RuntimeError("STOCK_DAY_ALL missing date")
    for r in out.values():
        if not r.date:
            r.date = date_ymd
    return date_ymd, out


def fetch_mi_index(date_ymd: str, timeout: int, ua: str) -> Dict[str, StockRow]:
    """Historical daily quotes via MI_INDEX."""
    url = (
        "https://www.twse.com.tw/exchangeReport/MI_INDEX"
        f"?response=json&date={date_ymd}&type=ALL"
    )
    raw = http_get(url, timeout, ua)
    payload = json.loads(raw)
    if str(payload.get("stat", "")).upper() not in {"OK", "ＯＫ"} and payload.get("stat") != "OK":
        # TWSE uses stat OK
        if payload.get("stat") != "OK":
            raise RuntimeError(f"MI_INDEX stat={payload.get('stat')} date={date_ymd}")

    tables = []
    if isinstance(payload.get("tables"), list):
        tables = payload["tables"]
    else:
        # legacy: fields9/data9 or scan data*
        for key, val in payload.items():
            if not key.startswith("data"):
                continue
            if not isinstance(val, list) or not val:
                continue
            fkey = "fields" + key[4:]
            fields = payload.get(fkey) or payload.get("fields")
            if fields:
                tables.append({"fields": fields, "data": val, "title": key})

    out: Dict[str, StockRow] = {}
    for t in tables:
        fields = [str(x) for x in (t.get("fields") or [])]
        if not fields:
            continue
        # must look like equity table
        joined = ",".join(fields)
        if "收盤" not in joined and "收盤價" not in joined:
            continue
        if "證券代號" not in joined and "代號" not in joined:
            continue
        idx = {name: i for i, name in enumerate(fields)}

        def col(*names: str) -> Optional[int]:
            for n in names:
                if n in idx:
                    return idx[n]
            return None

        i_code = col("證券代號", "代號")
        i_name = col("證券名稱", "名稱")
        i_vol = col("成交股數", "成交量")
        i_val = col("成交金額")
        i_open = col("開盤價", "開盤")
        i_high = col("最高價", "最高")
        i_low = col("最低價", "最低")
        i_close = col("收盤價", "收盤")
        i_sign = col("漲跌(+/-)", "漲跌")
        i_diff = col("漲跌價差")
        i_txn = col("成交筆數")
        if i_code is None or i_close is None:
            continue
        for row in t.get("data") or []:
            if not row or len(row) <= i_code:
                continue
            code = str(row[i_code]).strip()
            if not code:
                continue
            close = to_float(row[i_close]) if i_close is not None else None
            diff = to_float(row[i_diff]) if i_diff is not None else None
            sign = str(row[i_sign]).strip() if i_sign is not None and i_sign < len(row) else ""
            change = None
            if diff is not None:
                if sign in {"-", "－", "–", "−"}:
                    change = -abs(diff)
                elif sign in {"+", "＋"}:
                    change = abs(diff)
                else:
                    # X or empty
                    change = diff if sign not in {"X", "x"} else 0.0
            prev = close - change if (close is not None and change is not None) else None
            out[code] = StockRow(
                code=code,
                name=str(row[i_name]).strip() if i_name is not None else "",
                date=date_ymd,
                open=to_float(row[i_open]) if i_open is not None else None,
                high=to_float(row[i_high]) if i_high is not None else None,
                low=to_float(row[i_low]) if i_low is not None else None,
                close=close,
                prev_close=prev,
                change=change,
                volume_shares=to_int(row[i_vol]) if i_vol is not None else None,
                turnover_value=to_float(row[i_val]) if i_val is not None else None,
                transactions=to_int(row[i_txn]) if i_txn is not None else None,
                market="TWSE",
            )
    if not out:
        raise RuntimeError(f"MI_INDEX parsed 0 rows for {date_ymd}")
    return out


def fetch_twtb4u(date_ymd: str, timeout: int, ua: str) -> Dict[str, Dict[str, Any]]:
    """Day-trade stats keyed by code."""
    url = (
        "https://www.twse.com.tw/exchangeReport/TWTB4U"
        f"?response=json&date={date_ymd}"
    )
    try:
        raw = http_get(url, timeout, ua)
        payload = json.loads(raw)
    except Exception:
        # CSV fallback
        url_csv = (
            "https://www.twse.com.tw/exchangeReport/TWTB4U"
            f"?response=csv&date={date_ymd}&selectType=All"
        )
        raw = http_get(url_csv, timeout, ua)
        return _parse_twtb4u_csv(raw)

    if payload.get("stat") != "OK":
        # try csv
        url_csv = (
            "https://www.twse.com.tw/exchangeReport/TWTB4U"
            f"?response=csv&date={date_ymd}&selectType=All"
        )
        raw = http_get(url_csv, timeout, ua)
        return _parse_twtb4u_csv(raw)

    out: Dict[str, Dict[str, Any]] = {}
    for t in payload.get("tables") or []:
        fields = [str(x) for x in (t.get("fields") or [])]
        if "證券代號" not in fields:
            continue
        idx = {n: i for i, n in enumerate(fields)}
        for row in t.get("data") or []:
            code = str(row[idx["證券代號"]]).strip().lstrip("=")
            if not code:
                continue
            out[code] = {
                "name": str(row[idx.get("證券名稱", 1)]).strip() if "證券名稱" in idx else "",
                "suspend": str(row[idx["暫停現股賣出後現款買進當沖註記"]]).strip()
                if "暫停現股賣出後現款買進當沖註記" in idx
                else "",
                "daytrade_shares": to_int(row[idx["當日沖銷交易成交股數"]])
                if "當日沖銷交易成交股數" in idx
                else None,
                "daytrade_buy_value": to_float(row[idx["當日沖銷交易買進成交金額"]])
                if "當日沖銷交易買進成交金額" in idx
                else None,
                "daytrade_sell_value": to_float(row[idx["當日沖銷交易賣出成交金額"]])
                if "當日沖銷交易賣出成交金額" in idx
                else None,
            }
    return out


def _parse_twtb4u_csv(text: str) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    # find header line with 證券代號
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if "證券代號" in line and "當日沖銷" in line:
            start = i
            break
    if start is None:
        return out
    # normalize TWSE csv which may use irregular commas
    buf = "\n".join(lines[start:])
    reader = csv.reader(io.StringIO(buf))
    rows = list(reader)
    if not rows:
        return out
    header = [h.strip() for h in rows[0]]
    # map
    def find(*cands: str) -> Optional[int]:
        for c in cands:
            for i, h in enumerate(header):
                if c in h:
                    return i
        return None

    i_code = find("證券代號")
    i_name = find("證券名稱")
    i_sus = find("暫停")
    i_sh = find("當日沖銷交易成交股數")
    i_buy = find("買進成交金額")
    i_sell = find("賣出成交金額")
    if i_code is None:
        return out
    for row in rows[1:]:
        if not row or len(row) <= i_code:
            continue
        code = row[i_code].strip().lstrip("=").strip('"')
        if not code or not re.match(r"^[0-9A-Z]+$", code):
            continue
        out[code] = {
            "name": row[i_name].strip() if i_name is not None and i_name < len(row) else "",
            "suspend": row[i_sus].strip() if i_sus is not None and i_sus < len(row) else "",
            "daytrade_shares": to_int(row[i_sh]) if i_sh is not None and i_sh < len(row) else None,
            "daytrade_buy_value": to_float(row[i_buy]) if i_buy is not None and i_buy < len(row) else None,
            "daytrade_sell_value": to_float(row[i_sell]) if i_sell is not None and i_sell < len(row) else None,
        }
    return out


def merge_daytrade(rows: Dict[str, StockRow], dt: Dict[str, Dict[str, Any]]) -> None:
    for code, info in dt.items():
        if code in rows:
            r = rows[code]
            r.daytrade_shares = info.get("daytrade_shares")
            r.daytrade_buy_value = info.get("daytrade_buy_value")
            r.daytrade_sell_value = info.get("daytrade_sell_value")
            r.daytrade_suspend_flag = info.get("suspend") or ""
        # daytrade-only names not in bars: skip (can't compute rate)


def fetch_shares_outstanding_auto(timeout: int, ua: str) -> Dict[str, float]:
    """TWSE openapi t187ap03_L — 已發行普通股數。"""
    url = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
    raw = http_get(url, timeout, ua)
    data = json.loads(raw)
    out: Dict[str, float] = {}
    if not isinstance(data, list):
        return out
    key = "已發行普通股數或TDR原股發行股數"
    for item in data:
        code = str(item.get("公司代號", "")).strip()
        sh = to_float(item.get(key))
        if code and sh and sh > 0:
            out[code] = sh
    return out


def _roc_slash_to_ymd(s: str) -> Optional[str]:
    """'115/08/12' or '115.08.12' or '*115/08/11' -> YYYYMMDD"""
    s = str(s).strip().lstrip("*").replace(".", "/")
    m = re.match(r"^(\d{2,3})/(\d{1,2})/(\d{1,2})$", s)
    if not m:
        return None
    y = int(m.group(1)) + 1911
    return f"{y}{int(m.group(2)):02d}{int(m.group(3)):02d}"


def _parse_disposition_period(period: str) -> Tuple[Optional[str], Optional[str]]:
    """'115/08/12～115/08/18' -> (start_ymd, end_ymd)"""
    if not period:
        return None, None
    p = str(period).replace(" ", "").replace("~", "～").replace("─", "～").replace("-", "～")
    if "～" not in p:
        one = _roc_slash_to_ymd(p)
        return one, one
    a, b = p.split("～", 1)
    return _roc_slash_to_ymd(a), _roc_slash_to_ymd(b)


def fetch_attention_codes(date_ymd: str, timeout: int, ua: str) -> Dict[str, str]:
    """
    資料日 D 當日公布之注意股（隔日開盤仍應避開）。
    優先 www startDate/endDate；失敗再試 openapi（可能僅近端空殼）。
    """
    out: Dict[str, str] = {}
    urls = [
        (
            "https://www.twse.com.tw/announcement/notice"
            f"?response=json&startDate={date_ymd}&endDate={date_ymd}"
        ),
        (
            "https://www.twse.com.tw/rwd/zh/announcement/notice"
            f"?response=json&startDate={date_ymd}&endDate={date_ymd}"
        ),
    ]
    for url in urls:
        try:
            raw = http_get(url, timeout, ua)
            payload = json.loads(raw)
            if payload.get("stat") != "OK":
                continue
            fields = [str(x) for x in (payload.get("fields") or [])]
            idx = {n: i for i, n in enumerate(fields)}
            i_code = idx.get("證券代號", 1)
            i_reason = idx.get("注意交易資訊", 4)
            for row in payload.get("data") or []:
                if not row or len(row) <= i_code:
                    continue
                code = str(row[i_code]).strip()
                if re.fullmatch(r"\d{4}", code):
                    reason = str(row[i_reason]) if i_reason < len(row) else ""
                    reason = re.sub(r"<[^>]+>", "", reason)
                    out[code] = reason[:120]
            if out:
                return out
        except Exception:
            continue
    # openapi fallback (often empty/dummy)
    try:
        raw = http_get("https://openapi.twse.com.tw/v1/announcement/notice", timeout, ua)
        data = json.loads(raw)
        if isinstance(data, list):
            for item in data:
                code = str(item.get("Code", "")).strip()
                if re.fullmatch(r"\d{4}", code):
                    out[code] = str(item.get("TradingInfoForAttention", ""))[:120]
    except Exception:
        pass
    return out


def fetch_disposition_codes(date_ymd: str, timeout: int, ua: str) -> Dict[str, str]:
    """
    若 date_ymd 落在處置起迄期間內則列入。
    來源：openapi /announcement/punish（含 DispositionPeriod）。
    """
    out: Dict[str, str] = {}
    # Prefer openapi structured period
    try:
        raw = http_get("https://openapi.twse.com.tw/v1/announcement/punish", timeout, ua)
        data = json.loads(raw)
        if isinstance(data, list):
            for item in data:
                code = str(item.get("Code", "")).strip()
                if not re.fullmatch(r"\d{4}", code):
                    continue
                period = str(item.get("DispositionPeriod", "")).strip()
                start, end = _parse_disposition_period(period)
                active = False
                if start and end and start <= date_ymd <= end:
                    active = True
                elif not start and not end:
                    # unknown period → still flag (safer)
                    active = True
                    period = period or "period_unknown"
                if active:
                    meas = str(item.get("DispositionMeasures", "")).strip()
                    out[code] = f"{period} {meas}".strip()
    except Exception:
        pass

    # Supplement / fallback via www range (±30 calendar days window ending date_ymd)
    try:
        # rough window start
        dt = datetime.strptime(date_ymd, "%Y%m%d") - timedelta(days=40)
        start = dt.strftime("%Y%m%d")
        url = (
            "https://www.twse.com.tw/announcement/punish"
            f"?response=json&startDate={start}&endDate={date_ymd}"
        )
        raw = http_get(url, timeout, ua)
        payload = json.loads(raw)
        if payload.get("stat") == "OK":
            fields = [str(x) for x in (payload.get("fields") or [])]
            idx = {n: i for i, n in enumerate(fields)}
            i_code = idx.get("證券代號", 2)
            i_period = idx.get("處置起迄時間", 6)
            i_meas = idx.get("處置措施", 7)
            for row in payload.get("data") or []:
                if not row or len(row) <= i_code:
                    continue
                code = str(row[i_code]).strip()
                if not re.fullmatch(r"\d{4}", code):
                    continue
                period = str(row[i_period]).strip() if i_period < len(row) else ""
                start_p, end_p = _parse_disposition_period(period)
                if start_p and end_p and start_p <= date_ymd <= end_p:
                    meas = str(row[i_meas]).strip() if i_meas < len(row) else ""
                    out[code] = f"{period} {meas}".strip()
    except Exception:
        pass
    return out


def apply_watchlists(
    rows: Dict[str, StockRow],
    attention: Dict[str, str],
    disposition: Dict[str, str],
) -> None:
    for code, r in rows.items():
        if code in attention:
            r.is_attention = True
            r.attention_reason = attention[code]
        if code in disposition:
            r.is_disposition = True
            r.disposition_period = disposition[code]


def load_shares_outstanding(path: Optional[str]) -> Dict[str, float]:
    """Optional JSON: {\"2330\": 25930380000, ...} shares."""
    if not path:
        return {}
    p = Path(path).expanduser()
    if not p.is_file():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    out = {}
    for k, v in data.items():
        f = to_float(v)
        if f and f > 0:
            out[str(k)] = f
    return out


def load_avg_volume(path: Optional[str]) -> Dict[str, float]:
    """Optional JSON: {\"2330\": 35000.0} in 張."""
    if not path:
        return {}
    p = Path(path).expanduser()
    if not p.is_file():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    out = {}
    for k, v in data.items():
        f = to_float(v)
        if f and f > 0:
            out[str(k)] = f
    return out


def _roc_compact_to_ymd(s: str) -> Optional[str]:
    """'1150101' -> '20260101'"""
    s = str(s).strip()
    if len(s) == 7 and s.isdigit():
        return f"{int(s[:3]) + 1911}{s[3:]}"
    if len(s) == 8 and s.isdigit():
        return s
    return None


def fetch_twse_closed_dates(
    years: Iterable[int],
    timeout: int,
    ua: str,
) -> Tuple[set, Dict[str, Any]]:
    """
    證交所休市日（國定假日／補假／僅結算無交易等）。
    來源：openapi /holidaySchedule/holidaySchedule
    排除名稱含「開始交易」「最後交易」的標註日（那些是交易日）。
    """
    closed: set = set()
    meta: Dict[str, Any] = {"source": "holidaySchedule", "years": list(years), "n": 0}
    # openapi returns current-year-centric schedule; also try www per year
    try:
        raw = http_get(
            "https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule",
            timeout,
            ua,
        )
        data = json.loads(raw)
        if isinstance(data, list):
            for item in data:
                name = str(item.get("Name", ""))
                if "開始交易" in name or "最後交易" in name:
                    continue
                ymd = _roc_compact_to_ymd(str(item.get("Date", "")))
                if ymd:
                    closed.add(ymd)
    except Exception as e:  # noqa: BLE001
        meta["openapi_error"] = str(e)

    for y in sorted(set(int(x) for x in years)):
        for url in (
            f"https://www.twse.com.tw/holidaySchedule/holidaySchedule?response=json&queryYear={y}",
            f"https://www.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?response=json&queryYear={y}",
        ):
            try:
                raw = http_get(url, timeout, ua)
                payload = json.loads(raw)
                if str(payload.get("stat", "")).lower() not in {"ok", "ＯＫ"} and payload.get("stat") != "ok":
                    # some return stat ok lowercase
                    if payload.get("stat") not in {"ok", "OK"}:
                        continue
                fields = [str(x) for x in (payload.get("fields") or [])]
                idx = {n: i for i, n in enumerate(fields)}
                i_date = idx.get("日期", 0)
                i_name = idx.get("名稱", 1)
                for row in payload.get("data") or []:
                    if not row:
                        continue
                    name = str(row[i_name]) if i_name < len(row) else ""
                    if "開始交易" in name or "最後交易" in name:
                        continue
                    ds = str(row[i_date]).strip()
                    # '2026-01-01' or '1150101'
                    if "-" in ds:
                        ymd = ds.replace("-", "")
                    else:
                        ymd = _roc_compact_to_ymd(ds) or ""
                    if len(ymd) == 8 and ymd.isdigit():
                        closed.add(ymd)
                break
            except Exception:
                continue

    meta["n"] = len(closed)
    return closed, meta


def is_weekend_ymd(ymd: str) -> bool:
    dt = datetime.strptime(ymd, "%Y%m%d")
    return dt.weekday() >= 5  # Sat=5 Sun=6


def avg_volume_from_series(
    volumes: Dict[str, List[float]],
    lookback_days: int,
    min_days: int,
) -> Dict[str, float]:
    """從「最近→更早」的量序列算均量。不足 min_days 的代號不進表。"""
    lookback_days = max(1, int(lookback_days))
    min_days = max(1, int(min_days))
    avgs: Dict[str, float] = {}
    for code, series in volumes.items():
        sample = series[:lookback_days]
        if len(sample) >= min_days:
            avgs[code] = float(sum(sample) / len(sample))
    return avgs


SURGE_LABEL_NA = "n/a"


def classify_surge_volume(
    volume_lots: Optional[float],
    avg_15d_lots: Optional[float],
    change_pct: Optional[float],
    mult: float = 2.0,
) -> Tuple[bool, str, str, Optional[float]]:
    """
    已過硬性篩選後的附加欄：當日量 ≥ 近 15 交易日均量 × 2 → 爆大量。
    回傳 (hit, label, dir, ratio_15d)。dir 為 up / down / \"\"。
    資料不足或不達標 → n/a。不加分、不淘汰。
    """
    if volume_lots is None or avg_15d_lots is None or avg_15d_lots <= 0:
        return False, SURGE_LABEL_NA, "", None
    ratio = float(volume_lots) / float(avg_15d_lots)
    if ratio < float(mult):
        return False, SURGE_LABEL_NA, "", ratio
    if change_pct is None:
        return True, "爆大量", "", ratio
    if change_pct > 0:
        return True, "爆大量(漲)", "up", ratio
    if change_pct < 0:
        return True, "爆大量(跌)", "down", ratio
    return True, "爆大量", "", ratio


def fetch_prior_avg_volume_lots(
    date_ymd: str,
    timeout: int,
    ua: str,
    lookback_days: int = 5,
    min_days: int = 3,
    max_calendar_span: int = 40,
) -> Tuple[Dict[str, float], Dict[str, Any], Dict[str, List[float]]]:
    """
    計算「資料日 D 之前」近 N 個**交易日**的平均成交量（張）。
    不含 D 本身。第三個回傳值是各股量序列（最近日在前），供 5 日／15 日共用。

    非交易日處理（中華民國／台股）：
      1. 週末（六、日）直接跳過，不打 API
      2. 證交所 holidaySchedule 休市日（國定假日、補假、僅結算無交易）跳過
      3. 其餘日期打 MI_INDEX；無資料（颱風假等臨時休市）亦跳過
    只把「成功取回行情」的日期計入均量。
    """
    lookback_days = max(1, int(lookback_days))
    min_days = max(1, int(min_days))
    base = datetime.strptime(date_ymd, "%Y%m%d")

    # years spanning lookback window
    years = {base.year, (base - timedelta(days=max_calendar_span)).year}
    closed, hol_meta = fetch_twse_closed_dates(years, timeout, ua)

    volumes: Dict[str, List[float]] = {}
    days_used: List[str] = []
    skipped_weekend: List[str] = []
    skipped_holiday: List[str] = []
    skipped_no_session: List[str] = []
    errors: List[str] = []

    for delta in range(1, max_calendar_span + 1):
        if len(days_used) >= lookback_days:
            break
        d = (base - timedelta(days=delta)).strftime("%Y%m%d")

        if is_weekend_ymd(d):
            skipped_weekend.append(d)
            continue
        if d in closed:
            skipped_holiday.append(d)
            continue

        try:
            rows = fetch_mi_index(d, timeout, ua)
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            skipped_no_session.append(d)
            errors.append(f"{d}:{msg}")
            continue

        if not rows:
            skipped_no_session.append(d)
            continue

        days_used.append(d)
        for code, r in rows.items():
            if r.volume_shares is None or r.volume_shares <= 0:
                continue
            volumes.setdefault(code, []).append(r.volume_shares / 1000.0)

    avgs = avg_volume_from_series(volumes, lookback_days, min_days)

    meta = {
        "days_used": days_used,
        "n_days": len(days_used),
        "lookback_days": lookback_days,
        "min_days": min_days,
        "n_codes": len(avgs),
        "skipped_weekend": skipped_weekend,
        "skipped_weekend_n": len(skipped_weekend),
        "skipped_holiday": skipped_holiday,
        "skipped_holiday_n": len(skipped_holiday),
        "skipped_no_session": skipped_no_session,
        "skipped_no_session_n": len(skipped_no_session),
        "holiday_calendar": hol_meta,
        "errors_n": len(errors),
        "note": "均量僅含實際有 MI_INDEX 行情之交易日；已排除週末與證交所休市曆",
    }
    if errors and len(errors) <= 6:
        meta["errors_sample"] = errors[:6]
    return avgs, meta, volumes


# ---------------------------------------------------------------------------
# filters & scoring
# ---------------------------------------------------------------------------

def is_excluded_instrument(
    code: str,
    name: str,
    cfg: dict,
    row: Optional[StockRow] = None,
) -> Tuple[bool, str]:
    ex = cfg.get("exclude") or {}
    if ex.get("disposition", True) and row is not None and row.is_disposition:
        return True, f"處置股({row.disposition_period or 'active'})"
    if ex.get("attention_notice", True) and row is not None and row.is_attention:
        return True, "注意股"

    if not ex.get("etf_and_leverage", True):
        return False, ""

    # ETF / beneficiary certificates often 00xx
    if code.startswith("00"):
        return True, "ETF/受益憑證(00*)"
    # leverage / inverse / foreign currency suffixes
    suffixes = ex.get("code_suffixes_block") or list("LRUKBCTAD")
    if len(code) >= 5:
        last = code[-1]
        if last in suffixes:
            return True, f"特殊商品後綴({last})"
    # name heuristics
    for kw in ("ETF", "槓桿", "反向", "期元大", "期街口", "債券", "正2", "反1"):
        if kw in name:
            return True, f"名稱排除({kw})"
    # non common equity length (warrants etc.)
    if not re.match(r"^\d{4}$", code):
        # allow 4-digit only for main pool
        if re.match(r"^\d{4}[A-Z]$", code):
            return True, "非純四碼普通股"
        if not re.match(r"^\d{4}$", code):
            return True, "非四碼代號"
    return False, ""


def compute_metrics(r: StockRow) -> Optional[Dict[str, float]]:
    if r.close is None or r.high is None or r.low is None:
        return None
    if r.volume_shares is None or r.turnover_value is None:
        return None
    prev = r.prev_close
    if prev is None or prev <= 0:
        # fallback: use open or close
        prev = r.open if r.open and r.open > 0 else r.close
    if prev is None or prev <= 0:
        return None
    if r.high < r.low:
        return None
    amp = (r.high - r.low) / prev
    chg = (r.close - prev) / prev
    lots = r.volume_shares / 1000.0
    if r.daytrade_shares is None:
        dtr = None
    else:
        if r.volume_shares <= 0:
            dtr = 0.0
        else:
            dtr = r.daytrade_shares / r.volume_shares
    if r.high == r.low:
        pos = 0.5
        upper_w = lower_w = 0.0
        body_f = 1.0
    else:
        pos = (r.close - r.low) / (r.high - r.low)
        rng = r.high - r.low
        o = r.open if r.open is not None else r.close
        upper_w = (r.high - max(o, r.close)) / rng
        lower_w = (min(o, r.close) - r.low) / rng
        body_f = abs(r.close - o) / rng
    vwap = None
    if r.volume_shares and r.volume_shares > 0 and r.turnover_value:
        vwap = r.turnover_value / r.volume_shares
    wick_tag = ""
    if upper_w >= 0.40 and body_f <= 0.45:
        wick_tag = "長上影"
    elif lower_w >= 0.40 and body_f <= 0.45:
        wick_tag = "長下影"
    near_limit = ""
    chg_abs = abs((r.close - prev) / prev)
    if chg_abs >= 0.095:
        near_limit = "近漲停" if r.close >= prev else "近跌停"
    inst_lots = (r.inst_net_shares / 1000.0) if r.inst_net_shares is not None else None
    inst_ratio = None
    if r.inst_net_shares is not None and r.volume_shares and r.volume_shares > 0:
        inst_ratio = r.inst_net_shares / r.volume_shares
    return {
        "prev_close": float(prev),
        "amplitude": float(amp),
        "change_pct": float(chg),
        "volume_lots": float(lots),
        "daytrade_rate": float(dtr) if dtr is not None else float("nan"),
        "close_position": float(pos),
        "vwap": float(vwap) if vwap else None,
        "upper_wick_frac": float(max(0.0, upper_w)),
        "lower_wick_frac": float(max(0.0, lower_w)),
        "body_frac": float(max(0.0, min(1.0, body_f))),
        "wick_tag": wick_tag,
        "near_limit": near_limit,
        "inst_net_lots": inst_lots,
        "inst_net_ratio": inst_ratio,
        "inst_foreign_lots": (r.inst_foreign_shares / 1000.0) if r.inst_foreign_shares is not None else None,
        "inst_trust_lots": (r.inst_trust_shares / 1000.0) if r.inst_trust_shares is not None else None,
    }


def hard_pass(r: StockRow, m: Dict[str, float], thr: dict, require_daytrade: bool) -> Tuple[bool, str]:
    c = r.close
    assert c is not None
    if c < thr["price_min"] or c > thr["price_max"]:
        return False, "price"
    if m["volume_lots"] < thr["volume_lots_min"]:
        return False, "volume"
    if (r.turnover_value or 0) < thr["turnover_value_min"]:
        return False, "turnover_value"
    if m["amplitude"] < thr["amplitude_min"]:
        return False, "amplitude"
    if require_daytrade:
        dtr = m["daytrade_rate"]
        if dtr != dtr:  # NaN
            return False, "no_daytrade"
        if dtr < thr["daytrade_rate_min"]:
            return False, "daytrade_rate"
        # must be on daytrade list with shares known
        if r.daytrade_shares is None:
            return False, "no_daytrade"
    return True, "ok"


def score_row(r: StockRow, m: Dict[str, float], cfg: dict) -> Tuple[float, Dict[str, float], List[str]]:
    sc = cfg.get("scoring") or {}
    parts: Dict[str, float] = {}
    notes: List[str] = []

    # 週轉率加分
    if r.shares_outstanding and r.shares_outstanding > 0 and r.volume_shares:
        tr = r.volume_shares / r.shares_outstanding
        if tr >= float(sc.get("turnover_rate_bonus_2", 0.05)):
            parts["turnover_rate"] = 15.0
            notes.append(f"週轉率{tr*100:.2f}%")
        elif tr >= float(sc.get("turnover_rate_bonus_1", 0.03)):
            parts["turnover_rate"] = 8.0
            notes.append(f"週轉率{tr*100:.2f}%")

    # 相對量能
    if r.avg_volume_5d_lots and r.avg_volume_5d_lots > 0:
        ratio = m["volume_lots"] / r.avg_volume_5d_lots
        mult = float(sc.get("rel_volume_mult", 1.5))
        if ratio >= mult:
            parts["rel_volume"] = 10.0
            notes.append(f"量比{ratio:.2f}x")
        elif ratio >= mult * 0.9:
            # soft near-miss not scored; keep quiet
            pass

    # 方向波動
    if abs(m["change_pct"]) >= float(sc.get("abs_change_pct_bonus", 0.03)):
        parts["abs_change"] = 8.0

    # 收盤位置
    if m["close_position"] >= 0.5:
        parts["close_pos_strong"] = 7.0
    else:
        parts["close_pos_weak"] = 7.0

    # 當沖活躍
    dtr = m["daytrade_rate"]
    if dtr == dtr:  # not NaN
        if dtr >= float(sc.get("daytrade_rate_bonus_2", 0.40)):
            parts["daytrade_rate"] = 15.0
        elif dtr >= float(sc.get("daytrade_rate_bonus", 0.30)):
            parts["daytrade_rate"] = 10.0
        buy = r.daytrade_buy_value or 0
        sell = r.daytrade_sell_value or 0
        if max(buy, sell) >= float(sc.get("daytrade_amount_bonus", 300_000_000)):
            parts["daytrade_amount"] = 5.0

    # 三大法人佔成交
    inst_ratio = m.get("inst_net_ratio")
    if inst_ratio is not None:
        if inst_ratio >= float(sc.get("inst_net_ratio_strong", 0.05)):
            parts["inst_buy"] = float(sc.get("inst_score_strong", 12))
            notes.append(f"法人買超{inst_ratio*100:.1f}%")
        elif inst_ratio >= float(sc.get("inst_net_ratio_min", 0.03)):
            parts["inst_buy"] = float(sc.get("inst_score_min", 8))
            notes.append(f"法人買超{inst_ratio*100:.1f}%")
        elif inst_ratio <= -float(sc.get("inst_net_ratio_strong", 0.05)):
            parts["inst_sell"] = float(sc.get("inst_score_strong", 12))
            notes.append(f"法人賣超{abs(inst_ratio)*100:.1f}%")
        elif inst_ratio <= -float(sc.get("inst_net_ratio_min", 0.03)):
            parts["inst_sell"] = float(sc.get("inst_score_min", 8))
            notes.append(f"法人賣超{abs(inst_ratio)*100:.1f}%")

    if m.get("wick_tag"):
        notes.append(str(m["wick_tag"]))
    if m.get("near_limit"):
        notes.append(str(m["near_limit"]))

    total = clamp_score(sum(parts.values()), float(sc.get("score_cap", SCORE_CAP)))
    return total, parts, notes


def bias_hint(m: Dict[str, float], parts: Dict[str, float]) -> str:
    # structural only (night session applied by workflow)
    strong = m["close_position"] >= 0.5 and m["change_pct"] >= 0
    weak = m["close_position"] < 0.5 and m["change_pct"] < 0
    if strong:
        return "偏多"
    if weak:
        return "偏空"
    return "中性"


def thresholds_from_cfg(cfg: dict, relaxed: bool) -> dict:
    thr = {
        "price_min": float(cfg["price_min"]),
        "price_max": float(cfg["price_max"]),
        "volume_lots_min": float(cfg["volume_lots_min"]),
        "turnover_value_min": float(cfg["turnover_value_min"]),
        "daytrade_rate_min": float(cfg["daytrade_rate_min"]),
        "amplitude_min": float(cfg["amplitude_min"]),
    }
    if relaxed:
        rel = cfg.get("relax") or {}
        thr["daytrade_rate_min"] = float(rel.get("daytrade_rate_min", thr["daytrade_rate_min"]))
        thr["amplitude_min"] = float(rel.get("amplitude_min", thr["amplitude_min"]))
        thr["turnover_value_min"] = float(rel.get("turnover_value_min", thr["turnover_value_min"]))
        thr["price_max"] = float(rel.get("price_max", thr["price_max"]))
    return thr


def screen(
    rows: Dict[str, StockRow],
    cfg: dict,
    relaxed: bool,
) -> List[ScreenResult]:
    thr = thresholds_from_cfg(cfg, relaxed=relaxed)
    results: List[ScreenResult] = []
    excluded_watch: Dict[str, int] = {"attention": 0, "disposition": 0}
    for code, r in rows.items():
        excl, reason = is_excluded_instrument(code, r.name, cfg, row=r)
        if excl:
            if reason.startswith("注意"):
                excluded_watch["attention"] += 1
            elif reason.startswith("處置"):
                excluded_watch["disposition"] += 1
            continue
        # missing daytrade entirely → fail hard (not in daytrade universe effectively)
        m = compute_metrics(r)
        if m is None:
            continue
        ok, why = hard_pass(r, m, thr, require_daytrade=True)
        if not ok:
            continue
        score, parts, notes = score_row(r, m, cfg)
        if relaxed:
            notes = list(notes) + ["寬鬆模式"]
        if r.daytrade_suspend_flag:
            notes.append(f"當沖註記:{r.daytrade_suspend_flag}")
        tr = None
        if r.shares_outstanding and r.shares_outstanding > 0 and r.volume_shares:
            tr = r.volume_shares / r.shares_outstanding
        vol_ratio = None
        if r.avg_volume_5d_lots and r.avg_volume_5d_lots > 0:
            vol_ratio = m["volume_lots"] / r.avg_volume_5d_lots
        vol_ratio_15 = None
        if r.avg_volume_15d_lots and r.avg_volume_15d_lots > 0:
            vol_ratio_15 = m["volume_lots"] / r.avg_volume_15d_lots
        sc = cfg.get("scoring") or {}
        surge_hit, surge_label, surge_dir, _ratio15 = classify_surge_volume(
            m["volume_lots"],
            r.avg_volume_15d_lots,
            m.get("change_pct"),
            mult=float(sc.get("surge_volume_mult", 2.0)),
        )
        if surge_hit:
            notes = list(notes) + [surge_label]
        res = ScreenResult(
            code=code,
            name=r.name,
            date=r.date,
            market=r.market,
            close=float(r.close or 0),
            open=r.open,
            high=float(r.high or 0),
            low=float(r.low or 0),
            prev_close=m["prev_close"],
            change_pct=m["change_pct"],
            amplitude=m["amplitude"],
            volume_lots=m["volume_lots"],
            turnover_value=float(r.turnover_value or 0),
            daytrade_rate=m["daytrade_rate"],
            daytrade_shares=int(r.daytrade_shares or 0),
            daytrade_buy_value=r.daytrade_buy_value,
            daytrade_sell_value=r.daytrade_sell_value,
            close_position=m["close_position"],
            turnover_rate=tr,
            avg_volume_5d_lots=r.avg_volume_5d_lots,
            volume_ratio=vol_ratio,
            quality_score=score,
            score_breakdown=parts,
            bias_hint=bias_hint(m, parts),
            support_obs=float(r.low or 0),
            resistance_obs=float(r.high or 0),
            notes=notes,
            relaxed=relaxed,
            shares_outstanding=r.shares_outstanding,
            vwap=m.get("vwap"),
            upper_wick_frac=m.get("upper_wick_frac"),
            lower_wick_frac=m.get("lower_wick_frac"),
            body_frac=m.get("body_frac"),
            wick_tag=m.get("wick_tag") or "",
            near_limit=m.get("near_limit") or "",
            inst_net_lots=m.get("inst_net_lots"),
            inst_net_ratio=m.get("inst_net_ratio"),
            inst_foreign_lots=m.get("inst_foreign_lots"),
            inst_trust_lots=m.get("inst_trust_lots"),
            avg_volume_15d_lots=r.avg_volume_15d_lots,
            volume_ratio_15d=vol_ratio_15,
            surge_volume=surge_hit,
            surge_volume_label=surge_label,
            surge_volume_dir=surge_dir,
        )
        results.append(res)
    results.sort(key=lambda x: (-x.quality_score, -x.daytrade_rate, -x.volume_lots))
    # stash exclusion counts on function attribute for meta (simple)
    screen.last_excluded_watch = excluded_watch  # type: ignore[attr-defined]
    return results


# ---------------------------------------------------------------------------
# universe load
# ---------------------------------------------------------------------------

def resolve_universe(
    date_arg: Optional[str],
    timeout: int,
    ua: str,
) -> Tuple[str, Dict[str, StockRow], Dict[str, Any]]:
    """
    Returns date_ymd, rows, meta.
    If date_arg is None: use STOCK_DAY_ALL latest.
    Else: try MI_INDEX for that date; if fails and matches latest openapi date, fallback.
    """
    meta: Dict[str, Any] = {"sources": []}
    latest_date, latest_rows = None, None
    try:
        latest_date, latest_rows = fetch_stock_day_all(timeout, ua)
        meta["sources"].append({"name": "STOCK_DAY_ALL", "date": latest_date, "n": len(latest_rows)})
    except Exception as e:  # noqa: BLE001
        meta["STOCK_DAY_ALL_error"] = str(e)

    if not date_arg:
        if not latest_rows or not latest_date:
            raise RuntimeError("無法取得 STOCK_DAY_ALL，且未指定 --date")
        date_ymd = latest_date
        rows = latest_rows
    else:
        date_ymd = date_arg
        rows = None
        try:
            rows = fetch_mi_index(date_ymd, timeout, ua)
            meta["sources"].append({"name": "MI_INDEX", "date": date_ymd, "n": len(rows)})
        except Exception as e:  # noqa: BLE001
            meta["MI_INDEX_error"] = str(e)
            if latest_rows and latest_date == date_ymd:
                rows = latest_rows
                meta["sources"].append(
                    {"name": "STOCK_DAY_ALL_fallback", "date": date_ymd, "n": len(rows)}
                )
            elif latest_rows and not date_arg:
                pass
            else:
                # last resort: if user asked latest-ish and openapi is only source
                if latest_rows and latest_date:
                    # still try daytrade on requested date with empty bars? no
                    raise RuntimeError(
                        f"無法取得 {date_ymd} 日行情（MI_INDEX 失敗: {e}）；"
                        f"OpenAPI 最新日為 {latest_date}"
                    )
                raise
        if rows is None:
            raise RuntimeError(f"無資料: {date_ymd}")

    dt = fetch_twtb4u(date_ymd, timeout, ua)
    meta["sources"].append({"name": "TWTB4U", "date": date_ymd, "n": len(dt)})
    merge_daytrade(rows, dt)
    return date_ymd, rows, meta


def merge_institutional(rows: Dict[str, StockRow], inst: Dict[str, Dict[str, Any]]) -> int:
    n = 0
    for code, info in inst.items():
        if code not in rows:
            continue
        r = rows[code]
        r.inst_net_shares = info.get("inst_net_shares")
        r.inst_foreign_shares = info.get("foreign_net_shares")
        r.inst_trust_shares = info.get("trust_net_shares")
        r.inst_dealer_shares = info.get("dealer_net_shares")
        n += 1
    return n


def enrich_broker_and_rescore(results: List[ScreenResult], date_ymd: str, cfg: dict, timeout: int, ua: str, limit: int = 20) -> None:
    """Fetch 分點 for top candidates, add chip scores, resort in place."""
    try:
        from daytrade_chips import fetch_broker_branch, is_daytrade_broker
    except Exception:
        return
    sc = cfg.get("scoring") or {}
    buy_min = float(sc.get("broker_top5_ratio_min", 0.03))
    buy_strong = float(sc.get("broker_top5_ratio_strong", 0.05))
    hint_lots = float(sc.get("broker_daytrade_lots", 300))
    br_strong = float(sc.get("broker_score_strong", 10))
    br_min = float(sc.get("broker_score_min", 6))
    dt_score = float(sc.get("broker_daytrade_score", 8))
    for res in results[:limit]:
        data = fetch_broker_branch(res.code, date_ymd, timeout, ua)
        if not data or data.get("error") or data.get("date_mismatch"):
            continue
        buy = data.get("buy") or {}
        sell = data.get("sell") or {}
        res.broker_top5_buy_lots = buy.get("lots")
        res.broker_top5_buy_ratio = buy.get("ratio")
        res.broker_top5_sell_lots = sell.get("lots")
        res.broker_top5_sell_ratio = sell.get("ratio")
        names_b = buy.get("names") or []
        names_s = sell.get("names") or []
        res.broker_top_buy = names_b[0] if names_b else ""
        res.broker_top_sell = names_s[0] if names_s else ""
        res.broker_daytrade_hint = any(is_daytrade_broker(n) for n in names_b[:3])
        extra = 0.0
        br = res.broker_top5_buy_ratio
        sr = res.broker_top5_sell_ratio
        if br is not None:
            if br >= buy_strong:
                extra += br_strong
                res.notes.append(f"分點買超{br*100:.1f}%")
                res.score_breakdown["broker_buy"] = br_strong
            elif br >= buy_min:
                extra += br_min
                res.notes.append(f"分點買超{br*100:.1f}%")
                res.score_breakdown["broker_buy"] = br_min
        if sr is not None and sr >= buy_min:
            pts = br_strong if sr >= buy_strong else br_min
            extra += pts
            res.notes.append(f"分點賣超{sr*100:.1f}%")
            res.score_breakdown["broker_sell"] = pts
        if res.broker_daytrade_hint and (res.broker_top5_buy_lots or 0) >= hint_lots:
            extra += dt_score
            res.score_breakdown["broker_daytrade"] = dt_score
            res.notes.append(f"隔日沖券商:{res.broker_top_buy}")
        res.quality_score = clamp_score(float(res.quality_score) + extra, float(sc.get("score_cap", SCORE_CAP)))
        # refine structural bias with chips
        inst_r = res.inst_net_ratio or 0.0
        if res.bias_hint == "偏多" and inst_r <= -0.03:
            res.bias_hint = "中性"
            res.notes.append("法人賣超對沖偏多")
        elif res.bias_hint == "偏空" and inst_r >= 0.03:
            res.bias_hint = "中性"
            res.notes.append("法人買超對沖偏空")
        elif res.bias_hint == "中性":
            if inst_r >= 0.05:
                res.bias_hint = "偏多"
            elif inst_r <= -0.05:
                res.bias_hint = "偏空"
    results.sort(key=lambda x: (-x.quality_score, -x.daytrade_rate, -x.volume_lots))


# ---------------------------------------------------------------------------
# CLI / output
# ---------------------------------------------------------------------------

def format_table(results: List[ScreenResult]) -> str:
    headers = [
        "代碼",
        "名稱",
        "收盤",
        "漲跌%",
        "振幅%",
        "量(張)",
        "額(億)",
        "當沖%",
        "週轉%",
        "量比",
        "爆大量",
        "均價",
        "K棒",
        "法人",
        "分點",
        "綜評分",
        "偏向",
        "支撐",
        "壓力",
    ]
    lines = [" | ".join(headers), "-|-".join(["----"] * len(headers))]
    for r in results:
        tr = f"{r.turnover_rate*100:.2f}" if r.turnover_rate is not None else "-"
        vr = f"{r.volume_ratio:.2f}" if r.volume_ratio is not None else "-"
        inst = f"{(r.inst_net_ratio or 0)*100:+.1f}%" if r.inst_net_ratio is not None else "-"
        if r.broker_top5_buy_ratio is not None or r.broker_top5_sell_ratio is not None:
            br = f"B{(r.broker_top5_buy_ratio or 0)*100:.0f}/S{(r.broker_top5_sell_ratio or 0)*100:.0f}"
        else:
            br = "-"
        lines.append(
            " | ".join(
                [
                    r.code,
                    r.name[:6],
                    f"{r.close:.2f}",
                    f"{r.change_pct*100:+.2f}",
                    f"{r.amplitude*100:.2f}",
                    f"{r.volume_lots:,.0f}",
                    f"{r.turnover_value/1e8:.2f}",
                    f"{r.daytrade_rate*100:.1f}",
                    tr,
                    vr,
                    r.surge_volume_label or "n/a",
                    f"{r.vwap:.2f}" if r.vwap else "-",
                    r.wick_tag or r.near_limit or "-",
                    inst,
                    br,
                    f"{r.quality_score:.0f}",
                    r.bias_hint,
                    f"{r.support_obs:.2f}",
                    f"{r.resistance_obs:.2f}",
                ]
            )
        )
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="台股當沖觀察池篩選器 v1")
    parser.add_argument("--date", help="資料日 YYYYMMDD（前一交易日）。省略則用 OpenAPI 最新日")
    parser.add_argument("--config", help="yaml 設定路徑")
    parser.add_argument("--top", type=int, help="輸出前 N 檔（覆寫 config）")
    parser.add_argument(
        "--relaxed-auto",
        dest="relaxed_auto",
        action="store_true",
        default=True,
        help="通過數 < relax_if_count_lt 時自動寬鬆（預設開）",
    )
    parser.add_argument(
        "--no-relax-auto",
        dest="relaxed_auto",
        action="store_false",
        help="停用自動寬鬆",
    )
    parser.add_argument("--relaxed", action="store_true", help="強制寬鬆門檻")
    parser.add_argument("--format", choices=["json", "table", "both"], default="both")
    parser.add_argument("--shares-file", help="可選股本 JSON {code: shares}")
    parser.add_argument("--avg-volume-file", help="可選 5 日均量(張) JSON")
    parser.add_argument("--no-write", action="store_true", help="不寫 cache 檔")
    parser.add_argument("--quiet-meta", action="store_true")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    if args.top:
        cfg["output_top_n"] = args.top

    timeout = int((cfg.get("request") or {}).get("timeout_sec") or 45)
    ua = str((cfg.get("request") or {}).get("user_agent") or DEFAULT_CONFIG["request"]["user_agent"])
    set_http_interval_sec((cfg.get("request") or {}).get("interval_sec", 1.0))

    date_arg = args.date
    if date_arg:
        if not re.fullmatch(r"\d{8}", date_arg):
            print("ERROR: --date 須為 YYYYMMDD", file=sys.stderr)
            return 2

    try:
        date_ymd, rows, meta = resolve_universe(date_arg, timeout, ua)
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False, indent=2))
        return 1

    feats = cfg.get("features") or {}
    # 1) shares outstanding → turnover rate bonus
    shares: Dict[str, float] = {}
    if feats.get("auto_shares", True):
        try:
            shares = fetch_shares_outstanding_auto(timeout, ua)
            meta["sources"].append({"name": "t187ap03_L_shares", "n": len(shares)})
        except Exception as e:  # noqa: BLE001
            meta["shares_auto_error"] = str(e)
    # manual file overrides / fills
    file_shares = load_shares_outstanding(args.shares_file)
    if file_shares:
        shares.update(file_shares)
        meta["sources"].append({"name": "shares_file", "n": len(file_shares)})

    avgs = load_avg_volume(args.avg_volume_file)
    avgs_15: Dict[str, float] = {}
    if feats.get("auto_avg_volume", True):
        sc = cfg.get("scoring") or {}
        lookback_5 = int(sc.get("avg_volume_lookback_days", 5))
        min_5 = int(sc.get("avg_volume_min_days", 3))
        lookback_15 = int(sc.get("surge_volume_lookback_days", 15))
        min_15 = int(sc.get("surge_volume_min_days", 10))
        fetch_n = max(lookback_5, lookback_15)
        span = max(40, fetch_n * 4)
        try:
            _raw_avgs, avg_meta, vol_series = fetch_prior_avg_volume_lots(
                date_ymd,
                timeout,
                ua,
                lookback_days=fetch_n,
                min_days=1,
                max_calendar_span=span,
            )
            auto_avgs = avg_volume_from_series(vol_series, lookback_5, min_5)
            avgs_15 = avg_volume_from_series(vol_series, lookback_15, min_15)
            # file overrides auto (僅 5 日均量／量比加分)
            for code, v in auto_avgs.items():
                if code not in avgs:
                    avgs[code] = v
            meta["sources"].append(
                {
                    "name": "avg_volume_5d_prior",
                    "date": date_ymd,
                    "n": len(auto_avgs),
                    "days_used": (avg_meta.get("days_used") or [])[:lookback_5],
                    "n_days": min(int(avg_meta.get("n_days") or 0), lookback_5),
                }
            )
            meta["sources"].append(
                {
                    "name": "avg_volume_15d_prior",
                    "date": date_ymd,
                    "n": len(avgs_15),
                    "days_used": avg_meta.get("days_used"),
                    "n_days": avg_meta.get("n_days"),
                    "lookback_days": lookback_15,
                    "min_days": min_15,
                    "mult": float(sc.get("surge_volume_mult", 2.0)),
                }
            )
            meta["avg_volume"] = avg_meta
            meta["avg_volume_15d"] = {
                "lookback_days": lookback_15,
                "min_days": min_15,
                "n_codes": len(avgs_15),
                "days_used": avg_meta.get("days_used"),
                "n_days": avg_meta.get("n_days"),
            }
        except Exception as e:  # noqa: BLE001
            meta["avg_volume_error"] = str(e)
    elif avgs:
        meta["sources"].append({"name": "avg_volume_file", "n": len(avgs)})

    for code, r in rows.items():
        if code in shares:
            r.shares_outstanding = shares[code]
        if code in avgs:
            r.avg_volume_5d_lots = avgs[code]
        if code in avgs_15:
            r.avg_volume_15d_lots = avgs_15[code]

    # 2) attention / disposition watchlists
    if feats.get("auto_watchlists", True):
        attention: Dict[str, str] = {}
        disposition: Dict[str, str] = {}
        try:
            attention = fetch_attention_codes(date_ymd, timeout, ua)
            meta["sources"].append({"name": "notice_attention", "date": date_ymd, "n": len(attention)})
            meta["attention_codes"] = sorted(attention.keys())
        except Exception as e:  # noqa: BLE001
            meta["attention_error"] = str(e)
        try:
            disposition = fetch_disposition_codes(date_ymd, timeout, ua)
            meta["sources"].append({"name": "punish_disposition", "date": date_ymd, "n": len(disposition)})
            meta["disposition_codes"] = sorted(disposition.keys())
        except Exception as e:  # noqa: BLE001
            meta["disposition_error"] = str(e)
        apply_watchlists(rows, attention, disposition)

    # 3) 三大法人 T86
    if feats.get("auto_institutional", True):
        try:
            from daytrade_chips import fetch_t86

            inst = fetch_t86(date_ymd, timeout, ua)
            n = merge_institutional(rows, inst)
            meta["sources"].append({"name": "T86_institutional", "date": date_ymd, "n": len(inst), "merged": n})
        except Exception as e:  # noqa: BLE001
            meta["institutional_error"] = str(e)

    force_relaxed = bool(args.relaxed)
    auto = bool(args.relaxed_auto)

    relaxed_flag = force_relaxed
    results = screen(rows, cfg, relaxed=relaxed_flag)
    if (not force_relaxed) and auto and len(results) < int(cfg.get("relax_if_count_lt") or 3):
        relaxed_flag = True
        results = screen(rows, cfg, relaxed=True)

    if feats.get("auto_broker", True) and results:
        try:
            enrich_broker_and_rescore(results, date_ymd, cfg, timeout, ua, limit=20)
            meta["sources"].append({"name": "broker_branch_top", "date": date_ymd, "n": min(20, len(results))})
        except Exception as e:  # noqa: BLE001
            meta["broker_error"] = str(e)

    top_n = int(cfg.get("output_top_n") or 15)
    results = results[:top_n]

    excl_watch = getattr(screen, "last_excluded_watch", {}) or {}
    meta["excluded_in_universe"] = excl_watch

    payload = {
        "ok": True,
        "version": "daytrade_screener_v1",
        "date": date_ymd,
        "relaxed": relaxed_flag,
        "count": len(results),
        "thresholds": thresholds_from_cfg(cfg, relaxed=relaxed_flag),
        "meta": meta if not args.quiet_meta else {},
        "results": [r.to_public_dict() for r in results],
    }

    out_dir = _expand((cfg.get("paths") or {}).get("output_dir") or "~/.hermes/cache/finance")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"screener_{date_ymd}.json"
    payload["output_path"] = str(out_path)
    if not args.no_write:
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.format in {"json", "both"}:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.format in {"table", "both"}:
        print()
        print(f"【當沖觀察池 v1】資料日 {date_ymd}  寬鬆={relaxed_flag}  檔數={len(results)}")
        print(format_table(results) if results else "(無符合條件股票)")
        if not args.no_write:
            print(f"\nJSON: {out_path}")

    return 0


if __name__ == "__main__":
    # support --no-relax-auto without adding noisy help conflict
    sys.exit(main())
