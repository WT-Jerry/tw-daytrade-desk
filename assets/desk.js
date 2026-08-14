/* TW Daytrade Desk — static JSON client */
(function () {
  const $ = (id) => document.getElementById(id);
  const state = { index: null, selected: null, cache: {} };

  function fmtDate(ymd) {
    if (!ymd || String(ymd).length !== 8) return ymd || "—";
    const s = String(ymd);
    return `${s.slice(0, 4)}.${s.slice(4, 6)}.${s.slice(6, 8)}`;
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

  function biasClass(bias) {
    const s = String(bias || "");
    if (s.includes("多")) return "long";
    if (s.includes("空")) return "short";
    return "mid";
  }

  function gateExtreme(tag) {
    return /高波動|警戒|extreme/i.test(String(tag || ""));
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

    $("archive-count").textContent = String(list.length);

    if (!list.length) {
      box.innerHTML = '<div class="dim" style="padding:14px">無符合項目</div>';
      return;
    }

    list.forEach((e, i) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.setAttribute("role", "option");
      btn.setAttribute("aria-selected", state.selected === e.date ? "true" : "false");
      btn.className = "arch-item" + (state.selected === e.date ? " active" : "");
      btn.innerHTML = `
        <div class="arch-date">${fmtDate(e.date)}</div>
        <div class="arch-tag">${e.gate_tag || "—"} · ${e.count ?? 0} 檔</div>
        <div class="arch-meta">${(e.top_codes || []).slice(0, 3).join(" · ") || "—"}</div>
      `;
      btn.addEventListener("click", () => selectDate(e.date));
      btn.style.animationDelay = `${Math.min(i, 12) * 30}ms`;
      box.appendChild(btn);
    });
  }

  async function selectDate(date) {
    state.selected = date;
    renderArchive($("search").value || "");
    $("empty").classList.add("hidden");
    $("detail").classList.remove("hidden");

    let report = state.cache[date];
    if (!report) {
      $("pill-status").textContent = "載入中";
      $("pill-status").className = "status-pill";
      try {
        report = await loadJSON(`data/reports/${date}.json`);
        state.cache[date] = report;
      } catch (err) {
        $("pill-status").textContent = "讀取失敗";
        $("pill-status").className = "status-pill err";
        $("d-md").textContent = String(err);
        return;
      }
    }

    $("pill-status").textContent = "LIVE";
    $("pill-status").className = "status-pill ok";

    const gate = report.gate || {};
    const idx = report.index || {};
    const rows = report.results || [];
    const meta = report.screen_meta || {};

    $("d-eyebrow").textContent = "OPENING SESSION · D=" + (report.screen_date || date);
    $("d-date").textContent = fmtDate(report.screen_date || date);
    $("d-generated").textContent = `產出 ${report.generated_at || "—"}`;
    $("d-gate").textContent = gate.tag || "—";
    const wrap = $("d-gate-wrap");
    wrap.classList.toggle("extreme", gateExtreme(gate.tag) || !!gate.extreme);

    const chg = idx.change;
    const chgTxt =
      chg === null || chg === undefined
        ? "—"
        : `${Number(chg) > 0 ? "+" : ""}${num(chg, 0)} (${idx.change_pct || "—"})`;
    const idxEl = $("d-index");
    idxEl.textContent = `${idx.label || idx.source || "index"} · ${chgTxt}`;
    idxEl.className = "index-line mono " + chgClass(chg);

    $("d-count").textContent = String(rows.length);
    const chgEl = $("d-chg");
    chgEl.textContent = chgTxt;
    chgEl.className = "kpi-v " + chgClass(chg);
    $("d-mode").textContent = meta.relaxed ? "寬鬆" : "標準";
    $("d-advice").textContent = gate.advice || "—";
    $("d-top-codes").textContent = rows
      .slice(0, 5)
      .map((r) => r.code)
      .join(" · ");

    const tbody = $("tbl").querySelector("tbody");
    tbody.innerHTML = "";
    const labels = [
      "#",
      "代號",
      "名稱",
      "收盤",
      "漲跌%",
      "振幅%",
      "量(張)",
      "額(億)",
      "當沖%",
      "週轉%",
      "量比",
      "分",
      "偏向",
      "支撐",
      "壓力",
    ];

    rows.forEach((r, i) => {
      const tr = document.createElement("tr");
      const chgN = r.change_pct;
      let chgShow = "—";
      if (chgN != null && !Number.isNaN(Number(chgN))) {
        const n = Number(chgN);
        chgShow = (Math.abs(n) <= 1 ? n * 100 : n).toFixed(2);
      }
      const amp =
        r.amplitude == null
          ? "—"
          : (Math.abs(Number(r.amplitude)) <= 1
              ? Number(r.amplitude) * 100
              : Number(r.amplitude)
            ).toFixed(2);
      const dtr =
        r.daytrade_rate == null
          ? "—"
          : (Math.abs(Number(r.daytrade_rate)) <= 1
              ? Number(r.daytrade_rate) * 100
              : Number(r.daytrade_rate)
            ).toFixed(2);
      const turn =
        r.turnover_rate_pct != null
          ? Number(r.turnover_rate_pct).toFixed(2)
          : r.turnover_rate != null
            ? (Number(r.turnover_rate) * 100).toFixed(2)
            : "—";
      const tvYi =
        r.turnover_value != null ? (Number(r.turnover_value) / 1e8).toFixed(2) : "—";
      const bias = r.bias_hint || "—";
      const bcls = biasClass(bias);

      const cells = [
        { html: `<span class="rank">${String(i + 1).padStart(2, "0")}</span>`, cls: "" },
        { html: `<span class="code">${r.code || ""}</span>`, cls: "" },
        { html: r.name || "", cls: "name-cell" },
        { html: num(r.close, 2), cls: "" },
        { html: chgShow, cls: chgClass(chgN) },
        { html: amp, cls: "" },
        { html: num(r.volume_lots, 0), cls: "" },
        { html: tvYi, cls: "" },
        { html: dtr, cls: "" },
        { html: turn, cls: "" },
        { html: r.volume_ratio != null ? num(r.volume_ratio, 2) : "—", cls: "" },
        { html: num(r.quality_score, 0), cls: "" },
        { html: `<span class="bias-chip ${bcls}">${bias}</span>`, cls: "" },
        { html: num(r.support_obs, 2), cls: "sr-cell" },
        { html: num(r.resistance_obs, 2), cls: "sr-cell" },
      ];

      cells.forEach((c, idxCell) => {
        const td = document.createElement("td");
        td.setAttribute("data-label", labels[idxCell] || "");
        if (c.cls) td.className = c.cls;
        td.innerHTML = c.html;
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });

    $("d-md").textContent = report.report_text || "(no markdown)";
    if (location.hash !== "#" + date) {
      history.replaceState(null, "", "#" + date);
    }
  }

  async function boot() {
    try {
      const idx = await loadJSON("data/index.json");
      state.index = idx;
      $("updated-at").textContent = "updated " + (idx.updated_at || "—");
      $("pill-status").textContent = "READY";
      $("pill-status").className = "status-pill ok";
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
      $("pill-status").className = "status-pill err";
      $("empty").classList.remove("hidden");
      $("empty").querySelector("p").textContent =
        "讀不到 data/index.json。請先跑 07:30 報告腳本。 (" + err + ")";
    }
  }

  $("search").addEventListener("input", (e) => renderArchive(e.target.value));
  boot();
})();
