/* Daytrade Desk v4 — mobile-first cards + desktop table */
(function () {
  const $ = (id) => document.getElementById(id);
  const state = { index: null, selected: null, cache: {} };

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
    const sign = p > 0 ? "+" : "";
    return sign + p.toFixed(2) + "%";
  }

  function fmtLots(v) {
    if (v == null || Number.isNaN(Number(v))) return "—";
    const n = Number(v);
    if (n >= 10000) return (n / 10000).toFixed(1) + "萬";
    return num(n, 0);
  }

  async function loadJSON(path) {
    const res = await fetch(path + (path.includes("?") ? "&" : "?") + "t=" + Date.now(), {
      cache: "no-store",
    });
    if (!res.ok) throw new Error(path + " " + res.status);
    return res.json();
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
        <span class="g">${e.gate_tag || "—"} · ${e.count ?? 0}檔</span>
      `;
      btn.addEventListener("click", () => selectDate(e.date));
      box.appendChild(btn);
    });
  }

  function buildRowModel(r, i) {
    const chgN = r.change_pct;
    const chgShow = fmtPct(chgN);
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
    return {
      i,
      code: r.code || "",
      name: r.name || "",
      close: num(r.close, 2),
      chgShow,
      chgClass: chgClass(chgN),
      chgSigned: fmtChgSigned(chgN),
      amp,
      lots: num(r.volume_lots, 0),
      lotsShort: fmtLots(r.volume_lots),
      tvYi,
      dtr,
      turn,
      volRatio: r.volume_ratio != null ? num(r.volume_ratio, 2) : "—",
      score: num(r.quality_score, 0),
      bias,
      biasClass: biasClass(bias),
      support: num(r.support_obs, 2),
      resistance: num(r.resistance_obs, 2),
    };
  }

  function renderTable(rows) {
    const tbody = $("tbl").querySelector("tbody");
    tbody.innerHTML = "";
    const labels = [
      "#", "代號", "名稱", "收盤", "漲跌%", "振幅%", "量(張)", "額(億)",
      "當沖%", "週轉%", "量比", "分", "偏向", "支撐", "壓力",
    ];

    rows.forEach((m) => {
      const tr = document.createElement("tr");
      const cells = [
        { html: `<span class="rank">${String(m.i + 1).padStart(2, "0")}</span>` },
        { html: `<span class="code">${m.code}</span>` },
        { html: m.name, cls: "name" },
        { html: m.close },
        { html: m.chgShow, cls: m.chgClass },
        { html: m.amp },
        { html: m.lots },
        { html: m.tvYi },
        { html: m.dtr },
        { html: m.turn },
        { html: m.volRatio },
        { html: m.score },
        { html: `<span class="bias ${m.biasClass}">${m.bias}</span>` },
        { html: m.support, cls: "sr-val sr-col" },
        { html: m.resistance, cls: "sr-val sr-col" },
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
      const card = document.createElement("article");
      card.className = "stock-card";
      card.innerHTML = `
        <header class="sc-head">
          <div class="sc-id">
            <span class="sc-rank">${String(m.i + 1).padStart(2, "0")}</span>
            <span class="sc-code">${m.code}</span>
            <span class="sc-name">${m.name}</span>
          </div>
          <span class="bias ${m.biasClass}">${m.bias}</span>
        </header>

        <div class="sc-price-row">
          <div class="sc-price">
            <span class="sc-k">收盤</span>
            <span class="sc-close mono">${m.close}</span>
          </div>
          <div class="sc-chg ${m.chgClass}">
            <span class="sc-k">漲跌</span>
            <span class="sc-chg-v mono">${m.chgSigned}</span>
          </div>
          <div class="sc-score">
            <span class="sc-k">品質分</span>
            <span class="sc-score-v mono">${m.score}</span>
          </div>
        </div>

        <div class="sc-sr">
          <div class="sc-sr-box s">
            <span class="sc-k">支撐 S</span>
            <span class="mono sr-val">${m.support}</span>
          </div>
          <div class="sc-sr-mid" aria-hidden="true">→</div>
          <div class="sc-sr-box r">
            <span class="sc-k">壓力 R</span>
            <span class="mono sr-val">${m.resistance}</span>
          </div>
        </div>

        <div class="sc-stats">
          <span><i>當沖</i>${m.dtr}%</span>
          <span><i>振幅</i>${m.amp}%</span>
          <span><i>量</i>${m.lotsShort}</span>
          <span><i>額</i>${m.tvYi}億</span>
          <span><i>週轉</i>${m.turn}%</span>
          <span><i>量比</i>${m.volRatio}</span>
        </div>
      `;
      box.appendChild(card);
    });
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

    renderTable(rows);
    renderCards(rows);

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
      const fromHash = (location.hash || "").replace(/^#/, "");
      const initial =
        fromHash && entries.some((e) => e.date === fromHash) ? fromHash : entries[0].date;
      await selectDate(initial);
    } catch (err) {
      $("pill-status").textContent = "NO DATA";
      $("pill-status").className = "badge err";
      $("empty").classList.remove("hidden");
      $("empty").querySelector("p").textContent =
        "讀不到 data/index.json，請先跑 07:30 報告。 (" + err + ")";
    }
  }

  $("search").addEventListener("input", (e) => renderArchive(e.target.value));
  boot();
})();
