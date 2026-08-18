/* Daytrade Desk v6 — desktop table + mobile ticket tape */
(function () {
  const $ = (id) => document.getElementById(id);
  const state = {
    index: null,
    selected: null,
    cache: {},
    rows: [],
    sortKey: "i",
    sortDir: 1,
    filterBias: "all",
  };

  function fmtDate(ymd) {
    if (!ymd || String(ymd).length !== 8) return ymd || "—";
    const s = String(ymd);
    return `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}`;
  }

  function num(x, d = 2) {
    if (x === null || x === undefined || x === "") return "—";
    const n = Number(x);
    if (Number.isNaN(n)) return String(x);
    return Math.abs(n) >= 1000
      ? n.toLocaleString("en-US", { maximumFractionDigits: d })
      : n.toFixed(d);
  }

  function chgClass(v) {
    const n = Number(v);
    if (Number.isNaN(n) || n === 0) return "flat";
    return n > 0 ? "up" : "down";
  }

  function biasClass(b) {
    const s = String(b || "");
    if (s.includes("多")) return "long";
    if (s.includes("空")) return "short";
    return "mid";
  }

  function asPct(v) {
    if (v == null || Number.isNaN(Number(v))) return null;
    const n = Number(v);
    return Math.abs(n) <= 1 ? n * 100 : n;
  }

  function fmtPct(v, d = 2) {
    const p = asPct(v);
    if (p == null) return "—";
    return p.toFixed(d);
  }

  function fmtChgSigned(v) {
    const p = asPct(v);
    if (p == null) return "—";
    if (p > 0) return "▲ " + p.toFixed(2) + "%";
    if (p < 0) return "▼ " + Math.abs(p).toFixed(2) + "%";
    return "0.00%";
  }

  function fmtLots(v) {
    if (v == null || Number.isNaN(Number(v))) return "—";
    const n = Number(v);
    if (n >= 10000) return (n / 10000).toFixed(1) + "萬";
    return num(n, 0);
  }

  function instLabel(ratio) {
    if (ratio == null || Number.isNaN(Number(ratio))) return { text: "—", cls: "flat" };
    const p = Number(ratio) * (Math.abs(Number(ratio)) <= 1 ? 100 : 1);
    if (p > 0) return { text: "買超 " + p.toFixed(1) + "%", cls: "up" };
    if (p < 0) return { text: "賣超 " + Math.abs(p).toFixed(1) + "%", cls: "down" };
    return { text: "持平 0%", cls: "flat" };
  }

  function brokerLabel(buyR, sellR) {
    const hasB = buyR != null && !Number.isNaN(Number(buyR));
    const hasS = sellR != null && !Number.isNaN(Number(sellR));
    if (!hasB && !hasS) return "—";
    const b = hasB ? (Number(buyR) * (Math.abs(Number(buyR)) <= 1 ? 100 : 1)).toFixed(0) : "—";
    const s = hasS ? (Number(sellR) * (Math.abs(Number(sellR)) <= 1 ? 100 : 1)).toFixed(0) : "—";
    return "買 " + b + "% / 賣 " + s + "%";
  }

  function brokerHtml(buyR, sellR) {
    const hasB = buyR != null && !Number.isNaN(Number(buyR));
    const hasS = sellR != null && !Number.isNaN(Number(sellR));
    if (!hasB && !hasS) return "—";
    const b = hasB ? (Number(buyR) * (Math.abs(Number(buyR)) <= 1 ? 100 : 1)).toFixed(0) : "—";
    const s = hasS ? (Number(sellR) * (Math.abs(Number(sellR)) <= 1 ? 100 : 1)).toFixed(0) : "—";
    return `<span class="up">買 ${b}%</span> / <span class="down">賣 ${s}%</span>`;
  }

  function srMeta(m) {
    const c = m.closeN;
    const s = m.supportN;
    const r = m.resistN;
    let pos = 50;
    let tag = "";
    if (Number.isFinite(c) && Number.isFinite(s) && Number.isFinite(r) && r !== s) {
      pos = Math.max(0, Math.min(100, ((c - s) / (r - s)) * 100));
    } else if (Number.isFinite(c) && Number.isFinite(r) && c >= r) {
      pos = 100;
    } else if (Number.isFinite(c) && Number.isFinite(s) && c <= s) {
      pos = 0;
    }
    if (Number.isFinite(c) && Number.isFinite(r) && c >= r - 0.011) tag = "貼壓力";
    else if (Number.isFinite(c) && Number.isFinite(s) && c <= s + 0.011) tag = "貼支撐";
    return { pos: Math.round(pos * 10) / 10, tag };
  }

  async function loadJSON(path) {
    const res = await fetch(path + (path.includes("?") ? "&" : "?") + "t=" + Date.now(), {
      cache: "no-store",
    });
    if (!res.ok) throw new Error(path + " " + res.status);
    return res.json();
  }

  function latestEntry(entries) {
    const list = (entries || []).slice();
    list.sort((a, b) => {
      const ga = String(a.generated_at || "");
      const gb = String(b.generated_at || "");
      if (ga !== gb) return gb.localeCompare(ga);
      return String(b.date || "").localeCompare(String(a.date || ""));
    });
    return list[0] || null;
  }

  function renderArchive(filter = "") {
    const box = $("archive-list");
    box.innerHTML = "";
    const q = filter.trim().toLowerCase();
    const entries = (state.index && state.index.entries) || [];
    const list = entries.filter((e) => {
      if (!q) return true;
      const blob = [e.date, e.gate_tag, e.gate_advice, ...(e.top_codes || [])]
        .join(" ")
        .toLowerCase();
      return blob.includes(q);
    });

    if (!list.length) {
      box.innerHTML = '<div class="muted empty-hint">無符合項目</div>';
      return;
    }

    list.forEach((e) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "date-btn" + (state.selected === e.date ? " active" : "");
      btn.setAttribute("role", "option");
      btn.setAttribute("aria-selected", state.selected === e.date ? "true" : "false");
      btn.innerHTML = `
        <span class="d">${fmtDate(e.date)}</span>
        <span class="g"><span class="g-gate">${e.gate_tag || "—"} · </span>${e.count ?? 0}檔</span>
      `;
      btn.addEventListener("click", () => selectDate(e.date));
      box.appendChild(btn);
    });
  }

  function buildRowModel(r, i) {
    const chgN = r.change_pct;
    const amp = fmtPct(r.amplitude);
    const dtr = fmtPct(r.daytrade_rate);
    const turn =
      r.turnover_rate_pct != null
        ? Number(r.turnover_rate_pct).toFixed(2)
        : r.turnover_rate != null
          ? (Number(r.turnover_rate) * 100).toFixed(2)
          : "—";
    const tvYi =
      r.turnover_value != null ? (Number(r.turnover_value) / 1e8).toFixed(2) : "—";
    const bias = r.bias_hint || "—";
    const turnN =
      r.turnover_rate_pct != null
        ? Number(r.turnover_rate_pct)
        : r.turnover_rate != null
          ? Number(r.turnover_rate) * 100
          : NaN;
    return {
      i,
      code: r.code || "",
      name: r.name || "",
      close: num(r.close, 2),
      closeN: Number(r.close),
      chgShow: fmtChgSigned(chgN),
      chgN: asPct(chgN),
      chgClass: chgClass(chgN),
      chgSigned: fmtChgSigned(chgN),
      amp,
      ampN: asPct(r.amplitude),
      lots: num(r.volume_lots, 0),
      lotsN: Number(r.volume_lots),
      lotsShort: fmtLots(r.volume_lots),
      tvYi,
      tvN: r.turnover_value != null ? Number(r.turnover_value) / 1e8 : NaN,
      dtr,
      dtrN: asPct(r.daytrade_rate),
      turn,
      turnN,
      volRatio: r.volume_ratio != null ? num(r.volume_ratio, 2) : "—",
      volN: r.volume_ratio != null ? Number(r.volume_ratio) : NaN,
      pos: r.close_pos_pct != null ? Number(r.close_pos_pct).toFixed(0) : (r.close_position != null ? (Number(r.close_position) * 100).toFixed(0) : "—"),
      posN: r.close_pos_pct != null ? Number(r.close_pos_pct) : (r.close_position != null ? Number(r.close_position) * 100 : NaN),
      vwap: r.vwap != null ? num(r.vwap, 2) : "—",
      vwapN: r.vwap != null ? Number(r.vwap) : NaN,
      wick: r.wick_tag || r.near_limit || "—",
      inst: instLabel(r.inst_net_ratio).text,
      instN: r.inst_net_ratio != null ? Number(r.inst_net_ratio) * (Math.abs(Number(r.inst_net_ratio)) <= 1 ? 100 : 1) : NaN,
      instCls: instLabel(r.inst_net_ratio).cls,
      broker: brokerLabel(r.broker_top5_buy_ratio, r.broker_top5_sell_ratio),
      brokerHtml: brokerHtml(r.broker_top5_buy_ratio, r.broker_top5_sell_ratio),
      night: (r.night_flags && r.night_flags.length) ? r.night_flags.join(" ") : "",
      score: (function () {
        const n = Number(r.quality_score);
        if (Number.isNaN(n)) return "—";
        return num(Math.max(0, Math.min(100, n)), 0);
      })(),
      scoreN: (function () {
        const n = Number(r.quality_score);
        if (Number.isNaN(n)) return NaN;
        return Math.max(0, Math.min(100, n));
      })(),
      bias,
      biasClass: biasClass(bias),
      support: num(r.support_obs, 2),
      supportN: Number(r.support_obs),
      resistance: num(r.resistance_obs, 2),
      resistN: Number(r.resistance_obs),
    };
  }

  function visibleRows() {
    if (state.filterBias === "all") return state.rows.slice();
    return state.rows.filter((r) => r.biasClass === state.filterBias);
  }

  function renderTable(rows) {
    const tbody = $("tbl").querySelector("tbody");
    tbody.innerHTML = "";
    const labels = [
      "#", "名稱", "收盤", "漲跌%", "振幅%", "量(張)", "額(億)",
      "當沖%", "週轉%", "量比", "收位", "均價", "K棒", "法人", "分點", "綜評分", "偏向", "支撐", "壓力",
    ];

    rows.forEach((m) => {
      const tr = document.createElement("tr");
      const cells = [
        { html: `<span class="rank">${String(m.i + 1).padStart(2, "0")}</span>` },
        {
          html: `<div class="name-cell"><span class="code">${m.code}</span><span class="nm">${m.name}</span></div>`,
        },
        { html: m.close, cls: "num" },
        { html: m.chgShow, cls: "num chg-cell " + m.chgClass },
        { html: m.amp, cls: "num" },
        { html: m.lots, cls: "num" },
        { html: m.tvYi, cls: "num" },
        { html: m.dtr, cls: "num" },
        { html: m.turn, cls: "num" },
        { html: m.volRatio, cls: "num" },
        { html: m.pos, cls: "num" },
        { html: m.vwap, cls: "num" },
        { html: m.wick, cls: "num" },
        { html: m.inst, cls: "num " + (m.instCls || "") },
        { html: m.broker, cls: "num" },
        { html: m.score, cls: "num" },
        { html: `<span class="bias ${m.biasClass}">${m.bias}</span>` },
        { html: m.support, cls: "sr-val sr-col num" },
        { html: m.resistance, cls: "sr-val sr-col num" },
      ];
      cells.forEach((c, idx) => {
        const td = document.createElement("td");
        td.setAttribute("data-label", labels[idx] || "");
        if (c.cls) td.className = c.cls;
        td.innerHTML = c.html;
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
  }

  function renderCards(rows) {
    const box = $("cards");
    box.innerHTML = "";
    if (!rows.length) {
      box.innerHTML = '<div class="muted empty-hint">本日無觀察池標的</div>';
      return;
    }

    rows.forEach((m) => {
      const sr = srMeta(m);
      const card = document.createElement("article");
      card.className = "ticket";
      card.innerHTML = `
        <button type="button" class="ticket-main" aria-expanded="false">
          <div class="tk-row1">
            <span class="tk-rank">${String(m.i + 1).padStart(2, "0")}</span>
            <span class="tk-code">${m.code}</span>
            <span class="tk-name">${m.name}</span>
            <span class="bias ${m.biasClass}">${m.bias}</span>
          </div>
          <div class="tk-row2">
            <span class="tk-close mono">${m.close}</span>
            <span class="tk-chg mono ${m.chgClass}">${m.chgSigned}</span>
            <span class="tk-score mono">綜評分 <b>${m.score}</b></span>
          </div>
          <div class="sr-rail" style="--pos:${sr.pos}%">
            <div class="sr-ends">
              <span>S ${m.support}</span>
              ${sr.tag ? `<span class="sr-tag">${sr.tag}</span>` : "<span></span>"}
              <span>R ${m.resistance}</span>
            </div>
            <div class="sr-track" aria-hidden="true">
              <i class="sr-fill"></i>
              <i class="sr-tick"></i>
            </div>
          </div>
          <div class="tk-chips" aria-label="籌碼">
            <span class="tk-chip">
              <i>法人</i>
              <b class="mono ${m.instCls}">${m.inst}</b>
            </span>
            <span class="tk-chip">
              <i>分點</i>
              <b class="mono">${m.brokerHtml}</b>
            </span>
          </div>
        </button>
        <div class="ticket-more">
          <span><i>當沖</i>${m.dtr}%</span>
          <span><i>振幅</i>${m.amp}%</span>
          <span><i>量</i>${m.lotsShort}</span>
          <span><i>額</i>${m.tvYi}億</span>
          <span><i>週轉</i>${m.turn}%</span>
          <span><i>量比</i>${m.volRatio}</span>
          <span><i>收位</i>${m.pos}%</span>
          <span><i>均價</i>${m.vwap}</span>
          <span><i>K棒</i>${m.wick}</span>
          ${m.night ? `<span><i>夜盤</i>${m.night}</span>` : ""}
        </div>
      `;
      const btn = card.querySelector(".ticket-main");
      btn.addEventListener("click", () => {
        const open = card.classList.toggle("open");
        btn.setAttribute("aria-expanded", open ? "true" : "false");
      });
      box.appendChild(card);
    });
  }

  function paint() {
    const rows = visibleRows();
    renderTable(rows);
    renderCards(rows);
  }

  async function selectDate(date) {
    state.selected = date;
    renderArchive($("search").value || "");
    $("empty").classList.add("hidden");
    $("detail").classList.remove("hidden");

    let report = state.cache[date];
    if (!report) {
      $("pill-status").textContent = "LOADING";
      $("pill-status").className = "badge";
      try {
        report = await loadJSON(`data/reports/${date}.json`);
        state.cache[date] = report;
      } catch (err) {
        $("pill-status").textContent = "ERROR";
        $("pill-status").className = "badge err";
        $("d-md").textContent = String(err);
        return;
      }
    }

    $("pill-status").textContent = "LIVE";
    $("pill-status").className = "badge ok";

    const gate = report.gate || {};
    const idx = report.index || {};
    const rawRows = report.results || [];
    const meta = report.screen_meta || {};
    const rows = rawRows.map((r, i) => buildRowModel(r, i));

    $("d-date").textContent = fmtDate(report.screen_date || date);
    $("d-gate").textContent = gate.tag || "—";
    $("d-advice").textContent = gate.advice || "—";
    $("d-mode").textContent = meta.relaxed ? "寬鬆" : "標準";
    $("d-count").textContent = String(rows.length);
    $("d-generated").textContent =
      `產出 ${report.generated_at || "—"} · D=${report.screen_date || date}`;

    const chg = idx.change;
    const chgTxt =
      chg == null
        ? "—"
        : `${Number(chg) > 0 ? "+" : ""}${num(chg, 0)} (${idx.change_pct || "—"})`;
    const chgEl = $("d-chg");
    chgEl.textContent = chgTxt;
    chgEl.className = "t-v mono " + chgClass(chg);

    const idxEl = $("d-index");
    idxEl.textContent = idx.label ? `${idx.label}` : "";
    idxEl.className = "mono muted " + chgClass(chg);

    $("d-top-codes").textContent = rows.length
      ? "Top: " + rows.slice(0, 5).map((r) => `${r.code} ${r.name}`.trim()).join(" · ")
      : "";

    state.rows = rows;
    applySort(state.sortKey, state.sortDir, false);

    $("d-md").textContent = report.report_text || "(no markdown)";
    if (location.hash !== "#" + date) history.replaceState(null, "", "#" + date);
  }

  async function boot() {
    try {
      const idx = await loadJSON("data/index.json");
      state.index = idx;
      $("updated-at").textContent = idx.updated_at ? `updated ${idx.updated_at}` : "—";
      $("pill-status").textContent = "READY";
      $("pill-status").className = "badge ok";
      const entries = idx.entries || [];
      if (!entries.length) {
        $("empty").classList.remove("hidden");
        $("detail").classList.add("hidden");
        renderArchive();
        return;
      }
      renderArchive();
      const newest = latestEntry(entries);
      await selectDate(newest.date);
    } catch (err) {
      $("pill-status").textContent = "NO DATA";
      $("pill-status").className = "badge err";
      $("empty").classList.remove("hidden");
      $("empty").querySelector("p").textContent =
        "讀不到 data/index.json，請先跑 07:30 報告。 (" + err + ")";
    }
  }

  function applySort(key, dir, toggle) {
    if (toggle && state.sortKey === key) dir = -state.sortDir;
    state.sortKey = key;
    state.sortDir = dir;
    state.rows = state.rows.slice().sort((a, b) => {
      const va = a[key];
      const vb = b[key];
      if (va == null || va === "" || Number.isNaN(va)) return 1;
      if (vb == null || vb === "" || Number.isNaN(vb)) return -1;
      if (typeof va === "string") return va.localeCompare(vb, "zh-Hant") * dir;
      return (va - vb) * dir;
    });
    document.querySelectorAll("#tbl th[data-sort]").forEach((th) => {
      th.classList.toggle("sorted", th.getAttribute("data-sort") === key);
    });
    paint();
  }

  $("search").addEventListener("input", (e) => renderArchive(e.target.value));
  document.querySelectorAll("#tbl th[data-sort]").forEach((th) => {
    th.addEventListener("click", () => applySort(th.getAttribute("data-sort"), 1, true));
  });
  document.querySelectorAll(".bias-filters .chip").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.filterBias = btn.getAttribute("data-bias") || "all";
      document.querySelectorAll(".bias-filters .chip").forEach((b) => {
        const on = b === btn;
        b.classList.toggle("on", on);
        b.setAttribute("aria-selected", on ? "true" : "false");
      });
      paint();
    });
  });
  $("advice-bar").addEventListener("click", () => {
    $("advice-bar").classList.toggle("expanded");
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && document.activeElement !== $("search") && !e.ctrlKey && !e.metaKey) {
      e.preventDefault();
      $("search").focus();
    }
  });
  boot();
})();
