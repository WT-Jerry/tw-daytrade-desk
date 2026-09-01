#!/usr/bin/env python3
"""台股當沖 07:30 報告：screener v1 + 台指期（夜盤／盤後）閘門 + 文字報告。

用法：
  python3 ~/.hermes/scripts/finance/daytrade_report_0730.py
  python3 ~/.hermes/scripts/finance/daytrade_report_0730.py --date 20260810
  python3 ~/.hermes/scripts/finance/daytrade_report_0730.py --top 10 --json-out /tmp/r.json

輸出：stdout 為給使用者看的完整報告（ClawChat／Slack 友善 Markdown）。
副作用：寫入 ~/.hermes/cache/finance/report_0730_{D}.json 與最新 report_0730_latest.*
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import requests

CACHE_DIR = Path.home() / ".hermes" / "cache" / "finance"
SCREENER = Path.home() / ".hermes" / "scripts" / "finance" / "twse_screener.py"
WEB_ROOT = Path.home() / ".hermes" / "www" / "daytrade-tracker"
PUSH_SCRIPT = Path.home() / ".hermes" / "scripts" / "finance" / "push_daytrade_tracker_github.py"
UA = "hermes-daytrade-report-0730/1.0 (+local)"

MILD = 100.0
STRONG = 300.0
# 週一特規：那指／費半週末當否決資料（不作主閘門）
US_NQ_TICKER = "NQ=F"
US_IXIC_TICKER = "^IXIC"
US_SOX_TICKER = "^SOX"
US_NQ_BIG_PCT = 1.5  # |那指週末|≥1.5% 視為大幅
US_SOX_BIG_PCT = 2.0  # |費半週五|≥2.0% 視為大幅
SCREENER_TIMEOUT = 720  # 含 15 日均量回溯
PAGES_URL = "https://wt-jerry.github.io/tw-daytrade-desk/"
GITHUB_REPO = "git@github.com:WT-Jerry/tw-daytrade-desk.git"
TRACKER_TZ = ZoneInfo("Asia/Taipei")
TRACKER_KEEP_DAYS = 6  # 網頁只留近 6 個日曆日的報告頁；更舊的刪除


def http_get(url: str, timeout: int = 45) -> requests.Response:
    return requests.get(
        url,
        headers={
            "User-Agent": UA,
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
            "Accept": "text/html,application/json,*/*",
        },
        timeout=timeout,
    )


def _to_float(s: Any) -> Optional[float]:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    t = str(s).strip().replace(",", "").replace("%", "")
    if not t or t in {"-", "--", "—"}:
        return None
    try:
        return float(t)
    except ValueError:
        return None


def fetch_tx_cmoney() -> Dict[str, Any]:
    """從 CMoney 台指期頁抓 OHLC／漲跌（夜盤時段顯示夜盤；日間為日盤參考）。"""
    url = "https://www.cmoney.tw/forum/futures/TXF1?s=p"
    r = http_get(url, timeout=40)
    r.raise_for_status()
    html = r.text
    text = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)

    out: Dict[str, Any] = {
        "ok": False,
        "source": "cmoney_txf1",
        "url": url,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }

    # 開盤 44,937 最高 45,520 最低 44,830 均價 45,267.29 昨收 45,085 ... 漲跌幅 +0.77% 漲跌 349
    m = re.search(
        r"開盤\s*([\d,]+(?:\.\d+)?)\s*最高\s*([\d,]+(?:\.\d+)?)\s*最低\s*([\d,]+(?:\.\d+)?)"
        r"(?:\s*均價\s*([\d,]+(?:\.\d+)?))?\s*昨收\s*([\d,]+(?:\.\d+)?)",
        text,
    )
    m2 = re.search(
        r"漲跌幅\s*([+\-]?\d+(?:\.\d+)?%)\s*漲跌\s*([+\-]?\d[\d,]*(?:\.\d+)?)"
        r"(?:\s*買價\s*([\d,]+(?:\.\d+)?)\s*賣價\s*([\d,]+(?:\.\d+)?))?",
        text,
    )
    if not m or not m2:
        # fallback: 較鬆
        m2 = re.search(r"漲跌幅\s*([+\-]?\d+(?:\.\d+)?%)\s*漲跌\s*([+\-]?\d[\d,]*)", text)
        if not m2:
            out["error"] = "parse_failed"
            out["snippet"] = text[text.find("開盤") : text.find("開盤") + 220] if "開盤" in text else text[:200]
            return out

    open_p = _to_float(m.group(1)) if m else None
    high = _to_float(m.group(2)) if m else None
    low = _to_float(m.group(3)) if m else None
    avg = _to_float(m.group(4)) if m and m.group(4) else None
    prev = _to_float(m.group(5)) if m else None
    chg_pct_s = m2.group(1)
    chg = _to_float(m2.group(2))
    bid = _to_float(m2.group(3)) if m2.lastindex and m2.lastindex >= 3 else None
    ask = _to_float(m2.group(4)) if m2.lastindex and m2.lastindex >= 4 else None

    last = None
    if bid is not None and ask is not None:
        last = (bid + ask) / 2.0
    elif prev is not None and chg is not None:
        last = prev + chg

    out.update(
        {
            "ok": True,
            "open": open_p,
            "high": high,
            "low": low,
            "avg": avg,
            "prev_close": prev,
            "change": chg,
            "change_pct": chg_pct_s,
            "bid": bid,
            "ask": ask,
            "last": last,
            "label": "台指期（CMoney TXF1；07:30 視為夜盤／盤後參考）",
        }
    )
    return out


def fetch_tx_fallback_twii() -> Dict[str, Any]:
    """備援：用 yfinance ^TWII 當大盤方向參考（非期貨夜盤，需標註）。"""
    out: Dict[str, Any] = {
        "ok": False,
        "source": "yahoo_twii_fallback",
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "label": "加權指數 ^TWII（備援，非台指期夜盤）",
    }
    try:
        import yfinance as yf

        tk = yf.Ticker("^TWII")
        h = tk.history(period="5d")
        if h is None or len(h) < 2:
            out["error"] = "no_history"
            return out
        last_row = h.iloc[-1]
        prev_row = h.iloc[-2]
        last = float(last_row["Close"])
        prev = float(prev_row["Close"])
        chg = last - prev
        out.update(
            {
                "ok": True,
                "open": float(last_row["Open"]),
                "high": float(last_row["High"]),
                "low": float(last_row["Low"]),
                "prev_close": prev,
                "last": last,
                "change": chg,
                "change_pct": f"{(chg / prev) * 100:+.2f}%",
            }
        )
        return out
    except Exception as e:
        out["error"] = str(e)
        return out


def fetch_index_context() -> Dict[str, Any]:
    primary = fetch_tx_cmoney()
    if primary.get("ok") and primary.get("change") is not None:
        primary["gate_input"] = float(primary["change"])
        return primary
    fb = fetch_tx_fallback_twii()
    if fb.get("ok") and fb.get("change") is not None:
        fb["gate_input"] = float(fb["change"])
        fb["warning"] = "主源 CMoney 失敗，改用 ^TWII 日線備援（精度較差）"
        return fb
    return {
        "ok": False,
        "source": "none",
        "error": "all_sources_failed",
        "primary": primary,
        "fallback": fb,
        "gate_input": None,
        "label": "夜盤／指數（取得失敗）",
    }


def classify_gate(delta: Optional[float]) -> Dict[str, Any]:
    if delta is None:
        return {
            "tag": "未知",
            "advice": "夜盤點數取得失敗，僅依個股結構觀察，降低強進場語氣。",
            "bias_boost": "neutral",
        }
    ad = abs(delta)
    if ad < MILD:
        return {
            "tag": "中性",
            "advice": f"|Δ|={ad:.0f}<{MILD:.0f}：中性盤，多空皆可，重個股結構與開盤量能。",
            "bias_boost": "neutral",
        }
    if ad < STRONG:
        if delta > 0:
            return {
                "tag": "偏多",
                "advice": f"夜盤／參考 +{delta:.0f}（{MILD:.0f}–{STRONG:.0f}）：提高偏多池權重；慎追過度延伸。",
                "bias_boost": "long",
            }
        return {
            "tag": "偏空",
            "advice": f"夜盤／參考 {delta:.0f}（−{STRONG:.0f}–−{MILD:.0f}）：提高偏空池權重；慎追昨強勢高位。",
            "bias_boost": "short",
        }
    # |Δ| >= 300
    side = "大漲" if delta > 0 else "大跌"
    tip = (
        "防熱門股開高走低／利多出盡"
        if delta > 0
        else "防多殺多；可找相對抗跌，勿盲目抄底"
    )
    return {
        "tag": f"高波動警戒（{side}）",
        "advice": f"|Δ|={ad:.0f}≥{STRONG:.0f}：{tip}。名單可留，降低「強進場」語氣。",
        "bias_boost": "long" if delta > 0 else "short",
        "extreme": True,
    }


def is_monday_taipei(now: Optional[datetime] = None) -> bool:
    """07:30 排程的「週一」以台北日曆為準，不是資料日 D。"""
    ts = now or datetime.now(TRACKER_TZ)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=TRACKER_TZ)
    else:
        ts = ts.astimezone(TRACKER_TZ)
    return ts.weekday() == 0


def _et_date(ts: Any) -> Optional[date]:
    if ts is None:
        return None
    try:
        if getattr(ts, "tzinfo", None) is not None:
            return ts.astimezone(ZoneInfo("America/New_York")).date()
        return ts.date()
    except Exception:
        return None


def _yf_history(ticker: str, period: str = "10d") -> Optional[Any]:
    try:
        import yfinance as yf
    except Exception:
        return None
    try:
        h = yf.Ticker(ticker).history(period=period)
    except Exception:
        return None
    if h is None or getattr(h, "empty", True) or len(h) < 2:
        return None
    return h


def _bar_move(kind: str, from_d: date, to_d: date, prev: float, last: float) -> Dict[str, Any]:
    chg = last - prev
    pct = (chg / prev) * 100.0 if prev else None
    return {
        "kind": kind,
        "from": from_d.strftime("%Y-%m-%d"),
        "to": to_d.strftime("%Y-%m-%d"),
        "prev": prev,
        "last": last,
        "change": chg,
        "change_pct": pct,
    }


def _friday_then_weekend(hist: Any) -> Optional[Dict[str, Any]]:
    """最新週五收 → 其後第一根（週日／週一＝週末缺口）；沒有後續則退成週五漲跌。"""
    rows: List[Tuple[date, float]] = []
    for ts, row in hist.iterrows():
        d = _et_date(ts)
        try:
            close = float(row["Close"])
        except Exception:
            continue
        if d is None:
            continue
        rows.append((d, close))
    if len(rows) < 2:
        return None
    fri_i = None
    for i in range(len(rows) - 1, -1, -1):
        if rows[i][0].weekday() == 4:
            fri_i = i
            break
    if fri_i is None:
        d0, c0 = rows[-2]
        d1, c1 = rows[-1]
        return _bar_move("last_two", d0, d1, c0, c1)
    fri_d, fri_c = rows[fri_i]
    after = next(((d, c) for d, c in rows[fri_i + 1 :] if d > fri_d), None)
    if after:
        return _bar_move("weekend_gap", fri_d, after[0], fri_c, after[1])
    if fri_i >= 1:
        prev_d, prev_c = rows[fri_i - 1]
        return _bar_move("friday_cash", prev_d, fri_d, prev_c, fri_c)
    return None


def _side_from_pct(pct: Optional[float]) -> Optional[str]:
    if pct is None:
        return None
    if pct > 0:
        return "up"
    if pct < 0:
        return "down"
    return "flat"


def fetch_us_weekend_context() -> Dict[str, Any]:
    """那指（NQ 週末缺口，失敗用 ^IXIC 週五）+ 費半（^SOX 週五；現金無夜盤）。"""
    out: Dict[str, Any] = {
        "ok": False,
        "source": "yahoo_us_weekend",
        "fetched_at": datetime.now(TRACKER_TZ).isoformat(timespec="seconds"),
        "nq_big_pct": US_NQ_BIG_PCT,
        "sox_big_pct": US_SOX_BIG_PCT,
    }
    nq_hist = _yf_history(US_NQ_TICKER)
    ixic_hist = _yf_history(US_IXIC_TICKER)
    sox_hist = _yf_history(US_SOX_TICKER)

    nq = _friday_then_weekend(nq_hist) if nq_hist is not None else None
    ixic = _friday_then_weekend(ixic_hist) if ixic_hist is not None else None
    sox = _friday_then_weekend(sox_hist) if sox_hist is not None else None
    if sox and sox.get("kind") == "weekend_gap":
        # 費半現金不會有真正週日夜盤；若誤吃到週一後的日K，改回最新週五漲跌
        sox = None
        if sox_hist is not None:
            rows: List[Tuple[date, float]] = []
            for ts, row in sox_hist.iterrows():
                d = _et_date(ts)
                try:
                    close = float(row["Close"])
                except Exception:
                    continue
                if d is not None:
                    rows.append((d, close))
            fri_i = next((i for i in range(len(rows) - 1, -1, -1) if rows[i][0].weekday() == 4), None)
            if fri_i is not None and fri_i >= 1:
                sox = _bar_move("friday_cash", rows[fri_i - 1][0], rows[fri_i][0], rows[fri_i - 1][1], rows[fri_i][1])

    nasdaq = None
    nasdaq_ticker = None
    if nq and nq.get("change_pct") is not None:
        nasdaq = nq
        nasdaq_ticker = US_NQ_TICKER
    elif ixic and ixic.get("change_pct") is not None:
        nasdaq = ixic
        nasdaq_ticker = US_IXIC_TICKER

    if nasdaq is None and sox is None:
        out["error"] = "no_us_bars"
        return out

    nq_pct = nasdaq.get("change_pct") if nasdaq else None
    sox_pct = sox.get("change_pct") if sox else None
    nq_kind = nasdaq.get("kind") if nasdaq else None
    big_nq = nq_pct is not None and abs(nq_pct) >= US_NQ_BIG_PCT
    big_sox = sox_pct is not None and abs(sox_pct) >= US_SOX_BIG_PCT

    if nasdaq and nq_kind == "weekend_gap":
        nasdaq_text = f"那指週末 {nq_pct:+.2f}%（NQ）"
    elif nasdaq and nasdaq_ticker == US_IXIC_TICKER:
        nasdaq_text = f"那指週五 {nq_pct:+.2f}%（僅現金）"
    elif nasdaq:
        nasdaq_text = f"那指週五 {nq_pct:+.2f}%（NQ 無週日夜盤）"
    else:
        nasdaq_text = "那指 n/a"

    sox_text = f"費半週五 {sox_pct:+.2f}%" if sox_pct is not None else "費半 n/a"

    out.update(
        {
            "ok": True,
            "nasdaq": nasdaq,
            "nasdaq_ticker": nasdaq_ticker,
            "sox": sox,
            "nasdaq_pct": nq_pct,
            "sox_pct": sox_pct,
            "nasdaq_text": nasdaq_text,
            "sox_text": sox_text,
            "big_nasdaq": big_nq,
            "big_sox": big_sox,
            "big": bool(big_nq or big_sox),
        }
    )
    return out


def apply_monday_us_overlay(
    gate: Dict[str, Any],
    us: Dict[str, Any],
    night_delta: Optional[float],
) -> Dict[str, Any]:
    """週一建議欄疊那指／費半。美股只否決、不改夜盤主方向；大幅且 |夜盤|<300 才提高警戒。"""
    g = dict(gate)
    g["us_weekend"] = us
    g["monday_us"] = True
    g["bias_boost_night"] = g.get("bias_boost") or "neutral"

    if not us.get("ok"):
        g["advice"] = (g.get("advice") or "") + "｜週一：美股那指／費半取得失敗，不否決夜盤。"
        return g

    parts = "、".join(x for x in (us.get("nasdaq_text"), us.get("sox_text")) if x)
    big = bool(us.get("big"))
    boost = g.get("bias_boost") or "neutral"
    night_abs = abs(night_delta) if isinstance(night_delta, (int, float)) else None

    sides: List[str] = []
    if us.get("big_nasdaq"):
        s = _side_from_pct(us.get("nasdaq_pct"))
        if s in {"up", "down"}:
            sides.append(s)
    if us.get("big_sox"):
        s = _side_from_pct(us.get("sox_pct"))
        if s in {"up", "down"}:
            sides.append(s)
    if not sides:
        us_side = "flat"
    elif all(x == sides[0] for x in sides):
        us_side = sides[0]
    else:
        us_side = "mixed"
    g["us_side"] = us_side

    veto = big and (
        (boost == "long" and us_side == "down") or (boost == "short" and us_side == "up")
    )
    g["us_veto"] = veto
    escalate = bool(big and (night_abs is None or night_abs < STRONG))
    g["monday_alert"] = escalate

    if veto:
        g["bias_boost"] = "neutral"
        g["advice"] = (
            f"{g.get('advice') or ''}｜週一否決：{parts}（不作主資料），"
            f"與夜盤反向，偏向不升級"
            + ("；美股週末大幅，提高警戒、開盤前不追。" if big else "。")
        )
    elif big:
        g["advice"] = (
            f"{g.get('advice') or ''}｜週一：{parts}（否決資料、不作主資料）；"
            f"美股週末大幅，提高警戒、開盤前不追。"
        )
    else:
        g["advice"] = (
            f"{g.get('advice') or ''}｜週一：{parts}（否決資料），未達大幅、不改夜盤。"
        )

    if escalate:
        tag = g.get("tag") or ""
        if "高波動警戒" in tag:
            if "週一" not in tag:
                g["tag"] = f"{tag}·週一美股"
        else:
            g["tag"] = "週一警戒（美股週末大幅）"
    return g


def run_screener(date: Optional[str], top: int) -> Dict[str, Any]:
    cmd = [sys.executable, str(SCREENER), "--format", "json", "--top", str(top)]
    if date:
        cmd.extend(["--date", date])
    # always write cache from screener (useful for audit)
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=SCREENER_TIMEOUT)
    if p.returncode != 0:
        raise RuntimeError(f"screener failed rc={p.returncode}\n{p.stderr or p.stdout}")
    raw = p.stdout.strip()
    # screener may print non-json noise; find JSON object
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end < 0:
        raise RuntimeError(f"screener no json: {raw[:300]}")
    data = json.loads(raw[start : end + 1])
    return data


SCORE_CAP = 100.0
COL_WICK = "K棒"
COL_SCORE = "綜評分"


def normalize_report_labels(text: str) -> str:
    """Universal display names for every daily report (cron + archive)."""
    if not text:
        return text
    # only table headers — do not touch values like 長上影
    return (
        text.replace("| 影 |", f"| {COL_WICK} |")
        .replace("| 分 |", f"| {COL_SCORE} |")
    )


def clamp_quality_scores(rows: List[Dict[str, Any]], cap: float = SCORE_CAP) -> None:
    """Universal 0–100 cap on every report day (cron + replay)."""
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            s = float(r.get("quality_score"))
        except (TypeError, ValueError):
            continue
        if s > cap:
            r["quality_score"] = cap
        elif s < 0:
            r["quality_score"] = 0.0


def apply_night_flags(rows: List[Dict[str, Any]], gate: Dict[str, Any]) -> None:
    """Mark per-name night divergence / gap-reversal risk on existing result dicts."""
    boost = gate.get("bias_boost") or "neutral"
    extreme = bool(gate.get("extreme"))
    for r in rows:
        flags: List[str] = []
        chg = r.get("change_pct")
        pos = r.get("close_position")
        try:
            chg_f = float(chg) if chg is not None else None
            pos_f = float(pos) if pos is not None else None
        except (TypeError, ValueError):
            chg_f = pos_f = None
        if chg_f is not None and pos_f is not None:
            if boost == "short" and chg_f > 0 and pos_f >= 0.6:
                flags.append("夜盤背離")
            elif boost == "long" and chg_f < 0 and pos_f <= 0.4:
                flags.append("夜盤背離")
            if extreme and (
                (boost == "short" and chg_f > 0) or (boost == "long" and chg_f < 0)
            ):
                flags.append("缺口反轉風險")
        r["night_flags"] = flags
        if "缺口反轉風險" in flags:
            notes = list(r.get("notes") or [])
            if "缺口反轉風險" not in notes:
                notes.append("缺口反轉風險")
            r["notes"] = notes


def adjust_bias(row: Dict[str, Any], boost: str) -> str:
    base = (row.get("bias_hint") or "中性").strip()
    # map
    if boost == "long":
        if base in {"偏空"}:
            return "中性"
        if base in {"中性"}:
            return "偏多*"
        return base
    if boost == "short":
        if base in {"偏多"}:
            return "中性"
        if base in {"中性"}:
            return "偏空*"
        return base
    return base


def fmt_pct(x: Any, mult: bool = False) -> str:
    if x is None:
        return "-"
    try:
        v = float(x)
    except Exception:
        return str(x)
    if mult:  # already fraction
        v = v * 100
    return f"{v:.2f}"


def fmt_num(x: Any, nd: int = 2) -> str:
    if x is None:
        return "-"
    try:
        v = float(x)
    except Exception:
        return str(x)
    if abs(v) >= 1000:
        return f"{v:,.0f}" if nd == 0 else f"{v:,.{nd}f}"
    return f"{v:.{nd}f}"


def build_report(screen: Dict[str, Any], idx: Dict[str, Any], gate: Dict[str, Any]) -> str:
    d = screen.get("date") or "?"
    relaxed = bool(screen.get("relaxed"))
    rows: List[Dict[str, Any]] = list(screen.get("results") or [])
    boost = gate.get("bias_boost") or "neutral"
    delta = idx.get("gate_input")

    lines: List[str] = []
    lines.append("[FINANCE]")
    lines.append(f"**每日當沖觀察報告（07:30）** · 資料日 `D={d}`")
    lines.append(f"產出時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S %Z')}".rstrip())
    lines.append("")
    lines.append("### 1. 大盤／夜盤情境")
    if idx.get("ok"):
        chg = idx.get("change")
        chg_s = f"{chg:+.0f}" if isinstance(chg, (int, float)) else str(chg)
        last = idx.get("last")
        prev = idx.get("prev_close")
        lines.append(f"- 來源：{idx.get('label') or idx.get('source')}")
        lines.append(
            f"- 參考價：{fmt_num(last, 0)}｜昨收：{fmt_num(prev, 0)}｜漲跌：**{chg_s}**（{idx.get('change_pct') or '-'}）"
        )
        if idx.get("high") is not None:
            lines.append(
                f"- 區間：開 {fmt_num(idx.get('open'), 0)}／高 {fmt_num(idx.get('high'), 0)}／低 {fmt_num(idx.get('low'), 0)}"
            )
    else:
        lines.append(f"- ⚠️ 夜盤取得失敗：{idx.get('error')}")
    lines.append(f"- 情境標籤：**{gate.get('tag')}**")
    lines.append(f"- 操作建議：{gate.get('advice')}")
    us = gate.get("us_weekend") or {}
    if gate.get("monday_us"):
        if us.get("ok"):
            lines.append(
                f"- 週一美股（否決資料）：{us.get('nasdaq_text') or '那指 n/a'}｜{us.get('sox_text') or '費半 n/a'}"
            )
        else:
            lines.append("- 週一美股（否決資料）：取得失敗，不否決夜盤")
    if idx.get("warning"):
        lines.append(f"- 注意：{idx['warning']}")
    if relaxed:
        lines.append("- ⚠️ **寬鬆模式**：硬性通過檔數 < 3，已降門檻（報告已標註）")
    if gate.get("extreme"):
        lines.append("- 🚨 **|夜盤|≥300**：置頂高波動警戒，降低強進場語氣")
    lines.append("")
    lines.append("### 2. 選股規則（v1 硬性）")
    th = screen.get("thresholds") or {}
    lines.append(
        f"- 價 {th.get('price_min', 10)}–{th.get('price_max', 150)}｜量≥{int(th.get('volume_lots_min', 6000))} 張｜"
        f"額≥{float(th.get('turnover_value_min', 5e8))/1e8:.0f} 億｜當沖≥{float(th.get('daytrade_rate_min', 0.25))*100:.0f}%｜"
        f"振幅≥{float(th.get('amplitude_min', 0.05))*100:.0f}%（前收）"
    )
    meta = screen.get("meta") or {}
    ex = meta.get("excluded_in_universe") or {}
    if ex:
        lines.append(f"- 排除：注意股 {ex.get('attention', '-')}／處置 {ex.get('disposition', '-')}")
    lines.append(f"- 觀察池檔數：{len(rows)}（top 輸出）")
    lines.append("")
    lines.append("### 3. 觀察池（含點位）")
    if not rows:
        lines.append("_今日無符合硬性條件之標的（含寬鬆後仍為空）。_")
    else:
        # group-ish tables
        lines.append(
            "| 代號 | 名稱 | 收盤 | 漲跌% | 爆大量 | 均價 | K棒 | 法人 | 分點 | 綜評分 | 偏向 | S | R | 註 |"
        )
        lines.append("|---|---|---:|---:|---|---:|---|---:|---|---:|---|---:|---:|---|")
        for r in rows:
            bias = r.get("bias_hint") or adjust_bias(r, boost)
            vol = r.get("volume_lots")
            chg = r.get("change_pct")
            inst = r.get("inst_net_ratio")
            inst_s = f"{float(inst)*100:+.1f}%" if inst is not None else "-"
            br = "-"
            if r.get("broker_top5_buy_ratio") is not None or r.get("broker_top5_sell_ratio") is not None:
                br = "B{:.0f}/S{:.0f}".format(
                    float(r.get("broker_top5_buy_ratio") or 0) * 100,
                    float(r.get("broker_top5_sell_ratio") or 0) * 100,
                )
            flags = list(r.get("night_flags") or [])
            if r.get("wick_tag"):
                flags.append(r["wick_tag"])
            if r.get("near_limit"):
                flags.append(r["near_limit"])
            note = "、".join(flags) if flags else "-"
            surge = r.get("surge_volume_label") or "n/a"
            lines.append(
                "| {code} | {name} | {close} | {chg} | {surge} | {vwap} | {wick} | {inst} | {br} | {sc} | {bias} | {s} | {r} | {note} |".format(
                    code=r.get("code", ""),
                    name=r.get("name", ""),
                    close=fmt_num(r.get("close"), 2),
                    chg=fmt_pct(chg, mult=abs(float(chg)) <= 1 if chg is not None else False)
                    if chg is not None and abs(float(chg)) <= 1
                    else fmt_pct(chg),
                    surge=surge,
                    vwap=fmt_num(r.get("vwap"), 2) if r.get("vwap") else "-",
                    wick=r.get("wick_tag") or r.get("near_limit") or "-",
                    inst=inst_s,
                    br=br,
                    sc=fmt_num(r.get("quality_score"), 0),
                    bias=bias,
                    s=fmt_num(r.get("support_obs"), 2),
                    r=fmt_num(r.get("resistance_obs"), 2),
                    note=note,
                )
            )
        lines.append("")
        lines.append("**點位口徑**：壓力＝昨高、支撐＝昨低；均價＝成交額÷成交股數，可當箱內中軸。")
        lines.append("**爆大量**＝當日量 ≥ 近 15 交易日均量 × 2；否則 n/a。漲紅跌綠。")
        lines.append("**法人**＝三大法人淨買佔成交%；**分點**＝前5買超／賣超佔比。偏向 `*`＝夜盤微調。")
    lines.append("")
    lines.append("### 4. 操作紀律")
    lines.append("- 只做觀察池，不保證方向；開盤 15 分鐘量能不足則降倉。")
    lines.append("- 當沖嚴設停損；高當沖率＋高振幅常為隔日沖雜訊。")
    lines.append("- 本報告不構成投資建議。")
    lines.append("")
    lines.append("_engine: twse_screener v1 + daytrade_report_0730 · deliver ready_")
    return "\n".join(lines)


def _parse_report_date(value: Any) -> Optional[date]:
    raw = str(value or "").strip()
    if len(raw) != 8 or not raw.isdigit():
        return None
    try:
        return datetime.strptime(raw, "%Y%m%d").date()
    except ValueError:
        return None


def prune_tracker_reports(keep_days: int = TRACKER_KEEP_DAYS) -> Dict[str, Any]:
    """Drop tracker pages older than keep_days (Asia/Taipei calendar).

    Keeps at most keep_days newest dated reports. latest.json / latest.md stay.
    """
    today = datetime.now(TRACKER_TZ).date()
    cutoff = today - timedelta(days=keep_days)  # keep date > cutoff? 
    # 「近 6 天」含今天：today-5 .. today 共 6 日。cutoff = today-keep_days，保留 date > cutoff
    # 例：8/17、keep=6 → cutoff=8/11，保留 8/12–8/17。
    reports_dir = WEB_ROOT / "data" / "reports"
    index_path = WEB_ROOT / "data" / "index.json"
    reports_dir.mkdir(parents=True, exist_ok=True)

    entries: List[Dict[str, Any]] = []
    if index_path.is_file():
        try:
            old = json.loads(index_path.read_text(encoding="utf-8"))
            if isinstance(old, list):
                entries = old
            elif isinstance(old, dict) and isinstance(old.get("entries"), list):
                entries = old["entries"]
        except Exception:
            entries = []

    kept: List[Dict[str, Any]] = []
    removed_dates: List[str] = []
    for e in entries:
        d = _parse_report_date(e.get("date"))
        key = str(e.get("date") or "")
        if d is None or d <= cutoff:
            if key:
                removed_dates.append(key)
            continue
        kept.append(e)

    kept.sort(key=lambda x: str(x.get("date") or ""), reverse=True)
    if len(kept) > keep_days:
        for extra in kept[keep_days:]:
            extra_d = str(extra.get("date") or "")
            if extra_d:
                removed_dates.append(extra_d)
        kept = kept[:keep_days]

    keep_set = {str(e.get("date")) for e in kept if e.get("date")}
    removed_dates = sorted(set(removed_dates))

    deleted_files: List[str] = []
    if reports_dir.is_dir():
        for p in reports_dir.iterdir():
            if not p.is_file():
                continue
            stem = p.stem  # 20260810 or latest
            if stem in {"latest"}:
                continue
            parsed = _parse_report_date(stem)
            if parsed is None:
                continue
            if stem not in keep_set or parsed <= cutoff:
                p.unlink(missing_ok=True)
                deleted_files.append(p.name)
                if stem not in removed_dates:
                    removed_dates.append(stem)

    index_doc = {
        "updated_at": datetime.now(TRACKER_TZ).isoformat(timespec="seconds"),
        "title": "TW Daytrade Desk",
        "timezone": "Asia/Taipei",
        "keep_days": keep_days,
        "cutoff": cutoff.strftime("%Y%m%d"),
        "entries": kept,
        "count": len(kept),
    }
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(json.dumps(index_doc, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "keep_days": keep_days,
        "today": today.strftime("%Y%m%d"),
        "cutoff": cutoff.strftime("%Y%m%d"),
        "kept": [e.get("date") for e in kept],
        "removed": sorted(set(removed_dates)),
        "deleted_files": sorted(deleted_files),
    }


def publish_tracker(payload: Dict[str, Any]) -> Dict[str, str]:
    """
    僅在報告腳本被排程／手動觸發時更新追蹤網頁資料。
    寫入：
      data/reports/{D}.json
      data/reports/latest.json
      data/index.json  （清單，新→舊）
    """
    web = WEB_ROOT
    reports_dir = web / "data" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    (web / "data").mkdir(parents=True, exist_ok=True)

    d = str(payload.get("screen_date") or datetime.now().strftime("%Y%m%d"))
    clamp_quality_scores(list(payload.get("results") or []))
    if payload.get("report_text"):
        payload["report_text"] = normalize_report_labels(str(payload["report_text"]))
    # enrich copy for web
    web_payload = dict(payload)
    web_payload["tracker"] = {
        "id": d,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "source": "daytrade_report_0730",
    }

    day_path = reports_dir / f"{d}.json"
    latest_path = reports_dir / "latest.json"
    day_path.write_text(json.dumps(web_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    latest_path.write_text(json.dumps(web_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    index_path = web / "data" / "index.json"
    entries: List[Dict[str, Any]] = []
    if index_path.is_file():
        try:
            old = json.loads(index_path.read_text(encoding="utf-8"))
            if isinstance(old, list):
                entries = old
            elif isinstance(old, dict) and isinstance(old.get("entries"), list):
                entries = old["entries"]
        except Exception:
            entries = []

    gate = payload.get("gate") or {}
    idx = payload.get("index") or {}
    results = payload.get("results") or []
    summary = {
        "date": d,
        "generated_at": payload.get("generated_at"),
        "gate_tag": gate.get("tag"),
        "gate_advice": gate.get("advice"),
        "index_change": idx.get("change"),
        "index_change_pct": idx.get("change_pct"),
        "index_label": idx.get("label") or idx.get("source"),
        "count": len(results),
        "relaxed": bool((payload.get("screen_meta") or {}).get("relaxed")),
        "top_codes": [f"{r.get('code')} {r.get('name')}" for r in results[:5]],
        "file": f"data/reports/{d}.json",
    }
    # upsert by date
    entries = [e for e in entries if str(e.get("date")) != d]
    entries.append(summary)
    entries.sort(key=lambda x: str(x.get("date") or ""), reverse=True)

    index_doc = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "title": "TW Daytrade Desk",
        "timezone": "Asia/Taipei",
        "entries": entries,
        "count": len(entries),
    }
    index_path.write_text(json.dumps(index_doc, ensure_ascii=False, indent=2), encoding="utf-8")

    # also mirror markdown snapshot next to report for quick view
    md = payload.get("report_text") or ""
    if md:
        (reports_dir / f"{d}.md").write_text(md, encoding="utf-8")
        (reports_dir / "latest.md").write_text(md, encoding="utf-8")

    pruned = prune_tracker_reports(TRACKER_KEEP_DAYS)

    return {
        "web_root": str(web),
        "index": str(index_path),
        "report": str(day_path),
        "latest": str(latest_path),
        "pruned": pruned,
    }


def push_tracker_github(commit_hint: str | None = None) -> Dict[str, Any]:
    """SSH push tracker site to GitHub Pages repo."""
    if not PUSH_SCRIPT.is_file():
        return {"ok": False, "error": f"missing {PUSH_SCRIPT}"}
    cmd = [sys.executable, str(PUSH_SCRIPT)]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    out = ((p.stdout or "") + "\n" + (p.stderr or "")).strip()
    ok = p.returncode == 0 and "PUSH_OK" in (p.stdout or "")
    return {
        "ok": ok,
        "rc": p.returncode,
        "output": out[-1200:],
        "pages_url": PAGES_URL,
        "repo": GITHUB_REPO,
        "hint": commit_hint,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="資料日 YYYYMMDD（前一交易日）；省略由 screener 自動")
    ap.add_argument(
        "--monday-us",
        choices=["auto", "on", "off"],
        default="auto",
        help="週一建議欄疊那指／費半週末（否決資料）。auto=僅台北週一",
    )
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--json-out", help="額外寫入完整 JSON 路徑")
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--no-web", action="store_true", help="不更新追蹤網頁（預設會更新）")
    ap.add_argument(
        "--no-github",
        action="store_true",
        help="不推送到 GitHub Pages（預設在更新網頁後 SSH push）",
    )
    ap.add_argument(
        "--prune-only",
        action="store_true",
        help="只依 keep_days=6 清掉過期網頁報告，不重跑篩選",
    )
    args = ap.parse_args()

    if args.prune_only:
        pruned = prune_tracker_reports(TRACKER_KEEP_DAYS)
        print(json.dumps(pruned, ensure_ascii=False, indent=2))
        if not args.no_github:
            gh = push_tracker_github(commit_hint="prune-6d")
            print(json.dumps({"github": gh}, ensure_ascii=False, indent=2), file=sys.stderr)
            if gh.get("ok"):
                print(f"\n📎 追蹤網頁（GitHub Pages）：{PAGES_URL}")
        return 0

    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    try:
        screen = run_screener(args.date, args.top)
    except Exception as e:
        msg = f"[FINANCE]\n**每日當沖觀察報告（07:30）失敗**\n- screener error: {e}"
        print(msg)
        return 2

    idx = fetch_index_context()
    night_delta = (
        idx.get("gate_input")
        if isinstance(idx.get("gate_input"), (int, float))
        else _to_float(idx.get("change"))
    )
    gate = classify_gate(night_delta)
    monday = args.monday_us == "on" or (args.monday_us == "auto" and is_monday_taipei())
    if monday:
        gate = apply_monday_us_overlay(gate, fetch_us_weekend_context(), night_delta)
    rows = list(screen.get("results") or [])
    clamp_quality_scores(rows)
    apply_night_flags(rows, gate)
    for r in rows:
        r["bias_hint"] = adjust_bias(r, gate.get("bias_boost") or "neutral")
    screen["results"] = rows
    report = build_report(screen, idx, gate)
    report = normalize_report_labels(report)
    print(report)

    payload = {
        "ok": True,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "screen_date": screen.get("date"),
        "index": idx,
        "gate": gate,
        "screen_meta": {
            "count": screen.get("count"),
            "relaxed": screen.get("relaxed"),
            "thresholds": screen.get("thresholds"),
            "sources": (screen.get("meta") or {}).get("sources"),
            "excluded_in_universe": (screen.get("meta") or {}).get("excluded_in_universe"),
            "avg_volume": (screen.get("meta") or {}).get("avg_volume"),
        },
        "results": screen.get("results"),
        "report_text": report,
    }

    if not args.no_write:
        d = screen.get("date") or datetime.now().strftime("%Y%m%d")
        path = CACHE_DIR / f"report_0730_{d}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        (CACHE_DIR / "report_0730_latest.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (CACHE_DIR / "report_0730_latest.md").write_text(report, encoding="utf-8")

        if not args.no_web:
            try:
                pub = publish_tracker(payload)
                print(f"\n_tracker updated: {pub.get('report')}_", file=sys.stderr)
            except Exception as e:
                print(f"\n_tracker publish failed: {e}_", file=sys.stderr)
            else:
                if not args.no_github:
                    try:
                        gh = push_tracker_github(commit_hint=str(d))
                        if gh.get("ok"):
                            print(f"\n_github pages pushed: {PAGES_URL}_", file=sys.stderr)
                            # one-line for chat readers (stdout after report)
                            print(f"\n📎 追蹤網頁（GitHub Pages）：{PAGES_URL}")
                        else:
                            print(f"\n_github push failed: {gh}_", file=sys.stderr)
                    except Exception as e:
                        print(f"\n_github push exception: {e}_", file=sys.stderr)

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    return 0 if screen.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
