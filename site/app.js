"use strict";
/* Overmind front-end.
 * Security: every value from analysis.json (including AI text) is treated as untrusted and
 * rendered with textContent / numeric SVG attributes only. No innerHTML, no eval, no inline
 * handlers (CSP enforces it). */
(function () {
  const STALE_HOURS = 5;
  const REFRESH_MS = 10 * 60 * 1000;
  const TF_NAMES = { W: "Weekly", D: "Daily", H4: "H4" };
  const BIAS = { bullish: ["Rialzista", "bull"], bearish: ["Ribassista", "bear"], neutral: ["Neutrale", "neutral"] };
  const STATUS_IT = { ok: "OK", degraded: "Parziale", down: "Non disponibile", disabled: "Non configurata" };
  const VIEWS = ["analisi", "previsioni"];
  const C = { bull: "#3ddc97", bear: "#f2546b", gold: "#d9b45a", text: "#8d877a", grid: "rgba(217,180,90,0.06)", border: "rgba(217,180,90,0.16)" };
  const SVGNS = "http://www.w3.org/2000/svg";

  let brkLines = [];
  let data = null, chart = null, series = null, currentTf = "H4", liveLoaded = false, view = "analisi";

  // ------------------------------------------------------------ helpers
  function el(tag, attrs, ...children) {
    const e = document.createElement(tag);
    if (attrs) for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") e.className = v; else e.setAttribute(k, v);
    }
    for (const c of children) {
      if (c == null || c === false) continue;
      e.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return e;
  }
  function svg(tag, attrs, text) {
    const e = document.createElementNS(SVGNS, tag);
    if (attrs) for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, String(v));
    if (text != null) e.textContent = String(text);
    return e;
  }
  const $ = (id) => document.getElementById(id);
  const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);
  const str = (v) => (typeof v === "string" ? v : "");
  const arr = (v) => (Array.isArray(v) ? v : []);
  const fmt = (v, d = 2) => (num(v) === null ? "—" :
    v.toLocaleString("it-IT", { minimumFractionDigits: d, maximumFractionDigits: d }));
  const clear = (node) => { while (node.firstChild) node.removeChild(node.firstChild); return node; };
  const biasOf = (b) => (Object.hasOwn(BIAS, b) ? BIAS[b] : BIAS.neutral);
  function fmtTime(iso) {
    const t = Date.parse(str(iso));
    return Number.isNaN(t) ? "—" : new Date(t).toLocaleString("it-IT", { dateStyle: "short", timeStyle: "short" });
  }
  function ago(iso) {
    const t = Date.parse(str(iso));
    return Number.isNaN(t) ? null : (Date.now() - t) / 3600000;
  }
  function pct(v) { return num(v) === null ? "—" : (v > 0 ? "+" : "") + fmt(v, 2) + "%"; }
  function fmtEvTime(t) {
    return num(t) === null ? "—" : new Date(t * 1000).toLocaleString("it-IT",
      { weekday: "short", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", timeZone: "Europe/Rome" });
  }
  function emptyNote(box, text) { box.append(el("p", { class: "muted" }, text)); }

  // ------------------------------------------------------------ banner / status
  function renderBanner(problems, severe) {
    const b = clear($("banner"));
    if (!problems.length) { b.classList.add("hidden"); return; }
    b.classList.remove("hidden");
    b.classList.toggle("down", severe);
    b.append(el("b", null, severe ? "⚠ Il sito non funziona correttamente" : "⚠ Funzionamento parziale"));
    const ul = el("ul");
    problems.forEach((p) => ul.append(el("li", null, p)));
    b.append(ul);
  }

  function collectProblems(d) {
    const problems = [];
    let severe = false;
    const h = ago(d.generated_at);
    if (h === null || h > STALE_HOURS) {
      severe = true;
      problems.push(`Dati non aggiornati da ${h === null ? "?" : Math.round(h)} ore: l'esecuzione automatica ` +
        "(GitHub Actions) potrebbe essere ferma o fallita.");
    }
    if (d.analysis_stale) {
      severe = true;
      problems.push(`Prezzi non disponibili: mostro l'ultima analisi valida del ${fmtTime(d.analysis_time)}.`);
    }
    arr(d.sources).forEach((s) => {
      if (s && s.status && s.status !== "ok") problems.push(`${str(s.label)}: ${str(s.message) || (Object.hasOwn(STATUS_IT, s.status) ? STATUS_IT[s.status] : "stato sconosciuto")}`);
    });
    return { problems, severe };
  }

  function renderStatus(d, severe) {
    const dot = $("sys-dot"), txt = $("sys-text");
    const level = severe ? "down" : d.health === "ok" ? "ok" : "degraded";
    dot.className = "dot-pulse " + level;
    txt.textContent = { ok: "Sistema operativo", degraded: "Operativo · dati parziali", down: "Attenzione · dati non affidabili" }[level];
  }

  // ------------------------------------------------------------ gauges
  function gauge(value, cls, centerText, unit, tfName, label) {
    const R = 42, CIRC = 2 * Math.PI * R;
    const v = Math.max(0, Math.min(100, num(value) ?? 0));
    const s = svg("svg", { viewBox: "0 0 110 110", role: "img", "aria-label": `${tfName}: ${label} ${Math.round(v)}%` });
    s.append(svg("circle", { class: "track", cx: 55, cy: 55, r: R, fill: "none", "stroke-width": 7 }));
    for (const extra of ["glow", ""]) {
      s.append(svg("circle", { class: `arc ${cls} ${extra}`, cx: 55, cy: 55, r: R, fill: "none", "stroke-width": extra ? 10 : 7,
        "stroke-dasharray": `${(v / 100) * CIRC} ${CIRC}`, transform: "rotate(-90 55 55)" }));
    }
    s.append(svg("text", { class: "val", x: 55, y: 58, "text-anchor": "middle" }, centerText));
    s.append(svg("text", { class: "unit", x: 55, y: 74, "text-anchor": "middle" }, unit));
    return el("div", { class: "gauge" }, s, el("div", { class: "tfname" }, tfName), el("div", { class: "glabel " + cls }, label));
  }

  // ------------------------------------------------------------ hero (analisi)
  function renderHero(d) {
    const tfs = d.timeframes || {};
    const h1 = clear($("verdict"));
    const biases = ["W", "D", "H4"].map((tf) => tfs[tf] && tfs[tf].bias);
    if (biases.every((b) => b && b === biases[0]) && biases[0] !== "neutral") {
      const [label, cls] = biasOf(biases[0]);
      h1.append(el("span", { class: cls }, label), " su tutti i timeframe");
    } else {
      ["W", "D", "H4"].forEach((tf, i) => {
        const t = tfs[tf];
        if (i) h1.append(el("span", { class: "sep" }, "·"));
        const [label, cls] = biasOf(t && t.bias);
        const corr = t && str(t.context).includes("correzione");
        h1.append(TF_NAMES[tf] + " ", el("span", { class: cls }, corr ? "in correzione" : label.toLowerCase()));
      });
    }
    const ai = d.ai && typeof d.ai === "object" ? d.ai : null;
    $("ai-summary").textContent = ai && str(ai.summary) ? str(ai.summary) :
      "Spiegazione testuale non disponibile in questo aggiornamento: bias, confidenza e zone sono comunque calcolati dal motore.";
    $("updated").textContent = "· " + fmtTime(d.generated_at);

    const g = clear($("gauges"));
    for (const tf of ["W", "D", "H4"]) {
      const t = tfs[tf];
      const [label, cls] = biasOf(t && t.bias);
      g.append(gauge(t ? t.confidence : 0, cls, t ? `${Math.round(num(t.confidence) ?? 0)}%` : "—", "CONFIDENZA", TF_NAMES[tf], t ? label : "n/d"));
    }
  }

  // ------------------------------------------------------------ cards
  function renderCards(d) {
    const box = clear($("tf-cards"));
    const tfs = d.timeframes || {};
    for (const tf of ["W", "D", "H4"]) {
      const t = tfs[tf];
      if (!t) { box.append(el("div", { class: "panel" }, el("div", { class: "tf" }, TF_NAMES[tf]), "Dati non disponibili")); continue; }
      const [label, cls] = biasOf(t.bias);
      const conf = Math.max(0, Math.min(100, num(t.confidence) ?? 0));
      const bar = el("i"); bar.style.width = conf + "%";
      const card = el("div", { class: "panel" },
        el("div", { class: "card-top" }, el("span", { class: "tf" }, TF_NAMES[tf]), el("span", { class: "bias " + cls }, label)),
        el("div", { class: "ctx" }, str(t.context)),
        el("div", { class: "conf-row" }, el("span", null, "Confidenza"), el("b", null, conf + "%")),
        el("div", { class: "meter" }, bar),
        t.conflict ? el("span", { class: "conflict" }, "Segnali in conflitto · confidenza bassa") : null);
      const z = t.zone_of_interest;
      if (z && num(z.low) !== null) {
        card.append(el("div", { class: "zoi" }, el("div", { class: "zl" }, "Zona di interesse"),
          el("b", null, z.low === z.high ? fmt(z.low) : `${fmt(z.low)} – ${fmt(z.high)}`),
          el("div", { class: "muted small" }, `${str(z.type)} ${z.dir === "bull" ? "rialzista" : "ribassista"} · ${fmt(z.dist_pct, 1)}% dal prezzo`)));
      } else {
        card.append(el("div", { class: "zoi none" }, "Nessuna zona di interesse coerente con il bias"));
      }
      card.append(renderFactors(t));
      box.append(card);
    }
  }

  function renderFactors(t) {
    const det = el("details", null, el("summary", null, "Voti dei fattori"));
    const tbody = el("tbody");
    arr(t.factors).forEach((f) => {
      const v = num(f.vote);
      const cls = v === null ? "muted" : v > 0 ? "bull" : v < 0 ? "bear" : "neutral";
      tbody.append(el("tr", null,
        el("td", null, str(f.label), el("div", { class: "muted small" }, str(f.why))),
        el("td", { class: "num vote " + cls }, v === null ? "n/d" : (v > 0 ? "+" : "") + v),
        el("td", { class: "num muted" }, "×" + fmt(num(f.weight), 1))));
    });
    det.append(el("table", null, el("thead", null, el("tr", null, el("th", null, "Fattore"), el("th", null, "Voto"), el("th", null, "Peso"))), tbody));
    const c = t.components || {};
    det.append(el("p", { class: "muted small" },
      `Accordo ${Math.round((num(c.agreement) ?? 0) * 100)}% · Forza ${Math.round((num(c.strength) ?? 0) * 100)}% · ` +
      `Copertura dati ${Math.round((num(c.coverage) ?? 0) * 100)}%. Confidenza = copertura × (60% accordo + 40% forza).`));
    const pd = t.analysis && t.analysis.premium_discount;
    if (pd) det.append(el("p", { class: "muted small" }, `Range ${fmt(pd.range_low)} – ${fmt(pd.range_high)}: prezzo in ${str(pd.label)}.`));
    return det;
  }

  // ------------------------------------------------------------ AI
  function renderAI(d) {
    const box = clear($("ai"));
    const ai = d.ai;
    if (!ai || typeof ai !== "object") {
      emptyNote(box, "Spiegazione IA non disponibile in questo aggiornamento. Bias, confidenza e zone sono comunque calcolati dal motore e validi.");
      return;
    }
    for (const tf of ["W", "D", "H4"]) if (str(ai[tf])) box.append(el("p", { class: "ai-tf" }, el("b", null, TF_NAMES[tf].toUpperCase()), str(ai[tf])));
    if (arr(ai.risks).length) {
      const ul = el("ul", { class: "risks" });
      ai.risks.forEach((r) => ul.append(el("li", null, str(r))));
      box.append(el("div", { class: "muted small" }, "Rischi per lo scenario:"), ul);
    }
    box.append(el("p", { class: "note" }, "Testo generato dall'IA a partire dai soli dati calcolati: verifica sempre i numeri."));
  }

  // ------------------------------------------------------------ chart
  function zonesFor(tf) {
    const a = data && data.timeframes && data.timeframes[tf] && data.timeframes[tf].analysis;
    if (!a) return [];
    return [].concat(arr(a.zones_above), arr(a.zones_inside), arr(a.zones_below));
  }

  function drawChart(tf) {
    const container = $("chart");
    const rows = data && data.chart && Array.isArray(data.chart[tf]) ? data.chart[tf] : [];
    if (!window.LightweightCharts) { clear(container).append(el("p", { class: "muted" }, "Libreria grafico non caricata.")); return; }
    if (!chart) {
      chart = LightweightCharts.createChart(container, {
        autoSize: true,
        layout: { background: { color: "transparent" }, textColor: C.text, fontFamily: "Consolas, ui-monospace, monospace" },
        grid: { vertLines: { color: C.grid }, horzLines: { color: C.grid } },
        crosshair: { vertLine: { color: "rgba(217,180,90,0.4)" }, horzLine: { color: "rgba(217,180,90,0.4)" } },
        timeScale: { timeVisible: true, borderColor: C.border },
        rightPriceScale: { borderColor: C.border },
      });
    }
    brkLines.forEach((l) => chart.removeSeries(l));
    brkLines = [];
    if (series) chart.removeSeries(series);
    series = chart.addCandlestickSeries({ upColor: C.bull, downColor: C.bear, borderVisible: false,
      wickUpColor: C.bull, wickDownColor: C.bear });
    const clean = rows.filter((r) => Array.isArray(r) && r.length === 5 && r.every((x) => num(x) !== null))
      .map((r) => ({ time: r[0], open: r[1], high: r[2], low: r[3], close: r[4] }));
    series.setData(clean);
    const zoi = data.timeframes && data.timeframes[tf] && data.timeframes[tf].zone_of_interest;
    zonesFor(tf).forEach((z) => {
      if (num(z.low) === null || num(z.high) === null) return;
      const isZoi = zoi && zoi.low === z.low && zoi.high === z.high;
      const color = z.dir === "bull" ? C.bull : C.bear;
      const short = str(z.type).startsWith("Liquidit") ? "LIQ" : str(z.type) === "FVG" ? "FVG" : "OB";
      const opts = { color, lineWidth: isZoi ? 2 : 1, lineStyle: isZoi ? 0 : 2, axisLabelVisible: isZoi };
      series.createPriceLine({ ...opts, price: z.high, title: short + (isZoi ? " ★" : "") });
      if (z.low !== z.high) series.createPriceLine({ ...opts, price: z.low, title: "" });
    });
    drawBreaks(tf, clean);
    // Apply the visible range after layout, otherwise the first draw (container not yet sized) is off.
    const lastN = { W: 120, D: 150, H4: 180 }[tf];
    requestAnimationFrame(() => setTimeout(() => {
      if (clean.length > lastN) chart.timeScale().setVisibleLogicalRange({ from: clean.length - lastN, to: clean.length + 5 });
      else chart.timeScale().fitContent();
    }, 30));
    $("chart-note").textContent = `Fonte: ${str(data.provider)}. OB = order block, FVG = fair value gap, ` +
      "LIQ = liquidità; verde = domanda, rosso = offerta, ★ = zona di interesse. Frecce = rotture di struttura " +
      "(BOS/CHoCH) con il livello rotto tratteggiato; linee oro puntinate = prossimo livello di rottura. Aggiornato a ogni analisi.";
  }

  // ------------------------------------------------------------ structure breaks
  const BRK_TFS = ["D", "H4"];
  const NEW_BARS = { W: 1, D: 3, H4: 6 };
  const BRK_DIR = { up: ["rialzista", "bull", "▲", "sopra"], down: ["ribassista", "bear", "▼", "sotto"] };
  const brkDir = (d) => (Object.hasOwn(BRK_DIR, d) ? BRK_DIR[d] : null);
  const brkType = (t) => (t === "CHoCH" || t === "BOS" ? t : null);

  function breaksOf(tf) {
    const t = data && data.timeframes && data.timeframes[tf];
    const b = t && t.analysis && t.analysis.breaks;
    return b && typeof b === "object" ? b : null;
  }
  // Only well-formed entries survive: unknown type/dir or non-numeric fields are dropped.
  function validBreaks(b) {
    return arr(b && b.recent).filter((x) => x && brkType(x.type) && brkDir(x.dir) &&
      num(x.level) !== null && num(x.time) !== null && num(x.swing_time) !== null && num(x.bars_ago) !== null);
  }
  function barTime(t, tf) {
    const o = tf === "H4" ? { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" } :
      tf === "W" ? { day: "2-digit", month: "short", year: "2-digit" } : { day: "2-digit", month: "short" };
    return new Date(t * 1000).toLocaleString("it-IT", { ...o, timeZone: "Europe/Rome" });
  }
  function barsAgo(n) { return n <= 0 ? "ultima candela chiusa" : n === 1 ? "1 candela fa" : `${n} candele fa`; }

  function watchRow(w, d, tf) {
    const [dirTxt, cls, arrow, rel] = BRK_DIR[d];
    if (!w || typeof w !== "object" || num(w.level) === null || !brkType(w.type)) {
      return el("div", { class: "watch" }, el("span", { class: "arrow " + cls }, arrow),
        el("span", { class: "lv muted" }, "—"),
        el("span", { class: "eff" }, `Nessun ${d === "up" ? "massimo" : "minimo"} di swing ancora valido: si attende che se ne formi uno nuovo.`));
    }
    const choch = w.type === "CHoCH";
    return el("div", { class: "watch" }, el("span", { class: "arrow " + cls }, arrow),
      el("span", { class: "lv" }, fmt(w.level), num(w.dist_pct) !== null ? el("small", null, pct(w.dist_pct)) : null),
      el("span", { class: "eff" }, `Chiusura ${TF_NAMES[tf]} ${rel} → `,
        el("b", { class: choch ? "warn" : "" }, choch ? `CHoCH ${dirTxt}: possibile inversione` : `BOS ${dirTxt}: continuazione`)));
  }

  function renderBreaks() {
    const box = clear($("breaks"));
    for (const tf of BRK_TFS) {
      const b = breaksOf(tf);
      const col = el("div", { class: "brk-col" });
      const trend = brkDir(b && b.trend);
      col.append(el("div", { class: "brk-top" }, el("span", { class: "tf" }, TF_NAMES[tf]),
        el("span", { class: "brk-state " + (trend ? trend[1] : "neutral") }, trend ? `Struttura ${trend[0]}` : "Struttura non definita")));
      const list = validBreaks(b);
      const last = list[0];
      if (!last) {
        col.append(el("p", { class: "muted" }, b ? "Nessuna rottura di struttura nelle candele analizzate." :
          "Dati di struttura non disponibili: compariranno dopo la prossima analisi completa."));
      } else {
        const [dirTxt, cls, arrow, rel] = brkDir(last.dir);
        const choch = last.type === "CHoCH";
        const other = last.dir === "up" ? "ribassista" : "rialzista";
        const prev = list[1];
        const confirmed = !choch && prev && prev.type === "CHoCH" && prev.dir === last.dir;
        const title = choch ? `Cambio di tendenza: da ${other} a ${dirTxt}` :
          confirmed ? `Inversione ${dirTxt} confermata` : `Continuazione ${dirTxt}`;
        const note = choch ? `Da confermare: serve un BOS ${dirTxt}, cioè una nuova chiusura oltre lo swing successivo.` :
          confirmed ? "Al CHoCH è seguito un BOS nella stessa direzione: il nuovo trend è confermato." :
          `Il trend ${dirTxt} prosegue.`;
        col.append(el("div", { class: `brk-last ${cls}${choch ? " choch" : ""}` },
          el("div", { class: "brk-kind" }, `Ultima rottura · ${last.type} ${arrow}`,
            last.bars_ago <= NEW_BARS[tf] ? el("span", { class: "tag new" }, "NUOVA") : null),
          el("div", { class: "brk-title " + cls }, title),
          el("div", { class: "brk-sub" }, `Chiusura ${rel} `, el("b", null, fmt(last.level)),
            ` · ${barsAgo(last.bars_ago)} (${barTime(last.time, tf)})`),
          el("div", { class: "brk-sub" }, note)));
      }
      const wt = b && b.watch && typeof b.watch === "object" ? b.watch : {};
      col.append(el("div", { class: "brk-h" }, "Prossimi livelli di rottura"),
        watchRow(wt.up, "up", tf), watchRow(wt.down, "down", tf));
      if (list.length > 1) {
        const ul = el("ul", { class: "brk-list" });
        list.slice(0, 6).forEach((x) => {
          const [, cls, arrow] = brkDir(x.dir);
          ul.append(el("li", { class: x.type === "CHoCH" ? "choch" : "" },
            el("span", { class: cls }, arrow), el("span", { class: "t" }, x.type),
            el("span", { class: "lvl" }, fmt(x.level)), el("span", { class: "when" }, barTime(x.time, tf))));
        });
        col.append(el("div", { class: "brk-h" }, "Storico rotture"), ul);
      }
      box.append(col);
    }
  }

  // Hero: highlight a break that just happened on D or H4.
  function renderBreakAlert() {
    const box = clear($("brk-alert"));
    for (const tf of BRK_TFS) {
      const last = validBreaks(breaksOf(tf))[0];
      if (!last || last.bars_ago > NEW_BARS[tf]) continue;
      const [dirTxt, cls, arrow] = brkDir(last.dir);
      box.append(el("span", { class: "brk-pill " + cls },
        `${arrow} ${last.type} ${TF_NAMES[tf]} ${dirTxt}${last.type === "CHoCH" ? " · possibile inversione" : ""} · ${barsAgo(last.bars_ago)}`));
    }
    box.classList.toggle("hidden", !box.firstChild);
  }

  // Chart: arrow on the breaking candle, dashed segment from the swing to the break,
  // dotted gold price lines on the next break levels.
  function drawBreaks(tf, clean) {
    if (!clean.length) return;
    const first = clean[0].time, times = new Set(clean.map((r) => r.time));
    const list = validBreaks(breaksOf(tf)).filter((x) => times.has(x.time) && x.swing_time >= first && x.swing_time < x.time);
    series.setMarkers(list.slice().sort((a, b) => a.time - b.time).map((x) => {
      const up = x.dir === "up";
      return { time: x.time, position: up ? "belowBar" : "aboveBar", color: up ? C.bull : C.bear,
        shape: up ? "arrowUp" : "arrowDown", text: x.type, size: x.type === "CHoCH" ? 2 : 1 };
    }));
    list.forEach((x) => {
      const l = chart.addLineSeries({ color: x.dir === "up" ? C.bull : C.bear, lineWidth: x.type === "CHoCH" ? 2 : 1,
        lineStyle: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
      l.setData([{ time: x.swing_time, value: x.level }, { time: x.time, value: x.level }]);
      brkLines.push(l);
    });
    const b = breaksOf(tf), wt = b && b.watch && typeof b.watch === "object" ? b.watch : {};
    [["up", "↑"], ["down", "↓"]].forEach(([d, a]) => {
      const w = wt[d];
      if (w && typeof w === "object" && num(w.level) !== null && brkType(w.type))
        series.createPriceLine({ price: w.level, color: C.gold, lineWidth: 1, lineStyle: 1, axisLabelVisible: true, title: `${w.type} ${a}` });
    });
  }

  function loadLive() {
    if (liveLoaded) return;
    liveLoaded = true;
    const wrap = el("div", { class: "tradingview-widget-container" });
    wrap.style.height = "100%";
    const inner = el("div", { class: "tradingview-widget-container__widget" });
    inner.style.height = "100%";
    const s = document.createElement("script");
    s.src = "https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js";
    s.async = true;
    s.textContent = JSON.stringify({ autosize: true, symbol: "OANDA:XAUUSD", interval: "240", timezone: "Europe/Rome",
      theme: "dark", style: "1", locale: "it", allow_symbol_change: false, hide_side_toolbar: false,
      backgroundColor: "rgba(5,6,8,1)", gridColor: "rgba(217,180,90,0.06)" });
    wrap.append(inner, s);
    $("live").append(wrap);
  }

  function setupTabs() {
    $("chart-tabs").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-tf]");
      if (!b) return;
      document.querySelectorAll("#chart-tabs button").forEach((x) => x.classList.toggle("active", x === b));
      const tf = b.dataset.tf;
      const live = tf === "LIVE";
      $("chart").classList.toggle("hidden", live);
      $("live").classList.toggle("hidden", !live);
      if (live) {
        loadLive();
        $("chart-note").textContent = "Grafico live TradingView (OANDA:XAUUSD, spot). Le zone di Overmind sono nelle schede W/D/H4.";
      } else if (["W", "D", "H4"].includes(tf)) { currentTf = tf; if (data) drawChart(tf); }
    });
  }

  // ------------------------------------------------------------ events
  const DIR_IT = { bullish: ["rialzista", "bull"], bearish: ["ribassista", "bear"] };
  function dirLabel(d) { return Object.hasOwn(DIR_IT, d) ? DIR_IT[d] : ["nessuna", "neutral"]; }

  function renderNewsFlash(d) {
    const box = clear($("news-flash"));
    const u = d.updated_after_event;
    if (!u || !arr(u.titles).length) { box.classList.add("hidden"); return; }
    box.classList.remove("hidden");
    box.append(el("b", null, "Previsione aggiornata dopo la notizia: "), u.titles.map(str).join(", "),
      u.vote ? el("div", { class: "muted small" }, str(u.vote)) : null);
  }

  function renderEvents(d) {
    const box = clear($("events"));
    const ev = d.events;
    const items = ev ? arr(ev.items) : [];
    if (!items.length) { emptyNote(box, "Nessun evento ad alto impatto disponibile per questa settimana."); return; }
    const now = Date.now() / 1000;
    const nextIdx = items.findIndex((e) => e.status === "upcoming");
    const tbody = el("tbody");
    items.forEach((e, i) => {
      const s = num(e.sign);
      const scen = s > 0 ? ["rialzista", "bull"] : s < 0 ? ["ribassista", "bear"] : null;
      const lean = e.lean || {};
      const [ld, lc] = dirLabel(lean.dir);
      const rx = e.reaction || {};
      let rxCell;
      if (e.status === "upcoming") rxCell = el("span", { class: "muted" }, "in attesa");
      else if (num(rx.r1h_pct) !== null || num(rx.so_far_pct) !== null) {
        const r1 = num(rx.r1h_pct) !== null ? rx.r1h_pct : rx.so_far_pct;
        const out = e.outcome === "hit" ? " ✓" : e.outcome === "miss" ? " ✗" : e.outcome === "flat" ? " · piatta" : "";
        rxCell = el("span", null,
          el("span", { class: "num " + (r1 < 0 ? "bear" : r1 > 0 ? "bull" : "neutral") }, pct(r1) + (num(rx.r1h_pct) !== null ? " (1h)" : " (finora)")),
          num(rx.r4h_pct) !== null ? el("div", { class: "muted small num" }, pct(rx.r4h_pct) + " (4h)" + out) : null);
      } else rxCell = el("span", { class: "muted" }, "n/d");
      tbody.append(el("tr", { class: e.status === "upcoming" ? (i === nextIdx ? "next" : "") : "past" },
        el("td", { class: "num" }, fmtEvTime(e.t)),
        el("td", null, str(e.title), el("span", { class: "tag" }, str(e.family))),
        el("td", { class: "num" }, str(e.forecast) || "—", el("div", { class: "muted small" }, "prec. " + (str(e.previous) || "—"))),
        el("td", null, scen ? el("span", { class: scen[1] }, scen[0]) : el("span", { class: "muted" }, "dipende dai toni"),
          el("div", { class: "muted small" }, str(e.why))),
        el("td", null, el("span", { class: lc }, ld),
          num(lean.confidence) !== null && lean.dir ? el("b", { class: "num" }, " " + lean.confidence + "%") : null,
          el("div", { class: "muted small" }, str(lean.why))),
        el("td", null, rxCell),
        el("td", { class: "num muted" }, num(e.avg_move_1h_pct) !== null ? "±" + fmt(e.avg_move_1h_pct, 2) + "%" : "in raccolta")));
    });
    box.append(el("div", { class: "scroll-x" }, el("table", null, el("thead", null, el("tr", null,
      el("th", null, "Quando"), el("th", null, "Evento"), el("th", null, "Previsione"),
      el("th", null, "Se sopra le attese"), el("th", null, "Inclinazione"),
      el("th", null, "Reazione oro"), el("th", null, "Mov. medio 1h"))), tbody)));
    const st = ev.stats || {};
    box.append(el("p", { class: "note" },
      `Accuratezza delle inclinazioni: ${num(st.accuracy) === null ? "—" : st.accuracy + "%"} su ${num(st.evaluated) ?? 0} notizie valutate. ` +
      "L'inclinazione indica la direzione più probabile della reazione nelle 4h dopo l'uscita; la confidenza è limitata al 65% " +
      "perché il segno della sorpresa resta quasi imprevedibile. Orari in ora italiana. " +
      (items.every((e) => e.t < now) ? "Il calendario della prossima settimana viene pubblicato nel weekend." : "")));
  }

  // ------------------------------------------------------------ macro / track / sources
  function renderMacro(d) {
    const box = clear($("macro"));
    const m = d.macro || {};
    const names = { DFII10: ["Rendimento reale 10Y", "%"], DGS2: ["Treasury 2Y", "%"], DTWEXBGS: ["Dollaro (indice broad)", ""] };
    const tbody = el("tbody");
    for (const [k, [label, unit]] of Object.entries(names)) {
      const r = m[k];
      tbody.append(el("tr", null, el("td", null, label),
        el("td", { class: "num" }, r ? fmt(num(r.value), 2) + unit : "n/d"),
        el("td", { class: "num muted" }, r ? str(r.date) : "")));
    }
    const c = d.cot;
    tbody.append(el("tr", null, el("td", null, "Fondi netti long (COT)"),
      el("td", { class: "num" }, c ? fmt(num(c.net_pct_oi), 1) + "% OI" : "n/d"),
      el("td", { class: "num muted" }, c ? str(c.report_date) : "")));
    if (c) tbody.append(el("tr", null, el("td", null, "Variazione 4 sett. · percentile 3 anni"),
      el("td", { class: "num" }, `${fmt(num(c.change_4w), 1)} pt · ${fmt(num(c.percentile_3y), 0)}°`), el("td")));
    box.append(el("table", null, el("thead", null, el("tr", null, el("th", null, "Indicatore"), el("th", null, "Valore"), el("th", null, "Data"))), tbody));
  }

  function renderTrack(d) {
    const box = clear($("track"));
    const tr = d.track_record;
    if (!tr) { emptyNote(box, "Nessuno storico ancora."); return; }
    const tbody = el("tbody");
    for (const tf of ["W", "D", "H4"]) {
      const s = tr[tf]; if (!s) continue;
      tbody.append(el("tr", null, el("td", null, TF_NAMES[tf]),
        el("td", { class: "num" }, num(s.accuracy) === null ? "—" : s.accuracy + "%"),
        el("td", { class: "num muted" }, `${num(s.evaluated) ?? 0} valutate · ${num(s.pending) ?? 0} in attesa`)));
    }
    box.append(el("table", null, el("thead", null, el("tr", null, el("th", null, "TF"), el("th", null, "Bias corretti"), el("th", null, "Campione"))), tbody));
    box.append(el("p", { class: "note" }, "Una previsione è corretta se, dopo 4 candele Weekly / 5 Daily / 6 H4, " +
      "il prezzo si è mosso nella direzione del bias. Servono almeno 30–50 previsioni per un giudizio affidabile."));
  }

  function renderSources(d) {
    const box = clear($("sources"));
    const tbody = el("tbody");
    arr(d.sources).forEach((s) => {
      if (!s || typeof s !== "object") return;
      const st = ["ok", "degraded", "down", "disabled"].includes(s.status) ? s.status : "down";
      tbody.append(el("tr", null,
        el("td", null, el("span", { class: "dot " + st }), str(s.label)),
        el("td", null, STATUS_IT[st]),
        el("td", { class: "muted small" }, str(s.message)),
        el("td", { class: "num muted small" }, s.last_success ? "ultimo OK " + fmtTime(s.last_success) : "mai")));
    });
    box.append(el("div", { class: "scroll-x" }, el("table", null, tbody)));
  }

  // ------------------------------------------------------------ forecast
  function forecast() { return data && data.forecast && typeof data.forecast === "object" ? data.forecast : null; }

  function renderFcHero(d) {
    const f = forecast();
    const h1 = clear($("fc-title")), g = clear($("fc-gauge")), tug = clear($("fc-tug"));
    if (!f) {
      h1.textContent = "Previsione non disponibile";
      $("fc-week").textContent = "Verrà calcolata alla prossima analisi completa.";
      $("fc-made").textContent = "";
      $("fc-eyebrow").textContent = "Previsione settimanale";
      return;
    }
    const inProgress = num(f.week_start) !== null && Date.now() / 1000 >= f.week_start;
    const whenLabel = inProgress ? "Settimana in corso" : "Prossima settimana";
    $("fc-eyebrow").textContent = "Previsione · " + whenLabel;
    const dir = Object.hasOwn(BIAS, f.direction) ? f.direction : "neutral";
    const [label, cls] = biasOf(dir);
    const prob = Math.round(num(f.probability) ?? 50);
    if (dir === "neutral") h1.append("Settimana ", el("span", { class: cls }, "incerta"), ": nessuna direzione netta");
    else h1.append("Settimana ", el("span", { class: cls }, label.toLowerCase()), ` · ${prob}% di probabilità`);
    $("fc-week").textContent = `${whenLabel}: ${str(f.label)} · prezzo di riferimento ${fmt(num(f.ref_price))}. ` +
      (dir === "neutral" ? "Modello macro/strutturale e statistiche storiche si contraddicono." :
        `Probabilità che la settimana chiuda ${dir === "bullish" ? "sopra" : "sotto"} il prezzo attuale.`);
    $("fc-made").textContent = "· " + fmtTime(f.made_at);
    const up = Math.max(0, Math.min(100, num(f.p_up) ?? 50));
    const b = el("div", { class: "b" }), u = el("div", { class: "u" });
    b.style.width = (100 - up) + "%"; u.style.width = up + "%";
    tug.append(el("div", { class: "tug-bar" }, b, u, el("div", { class: "tug-mid" })),
      el("div", { class: "tug-legend" }, el("span", { class: "bear" }, `▼ Ribasso ${100 - up}%`), el("span", { class: "bull" }, `Rialzo ${up}% ▲`)));
    g.append(gauge(prob, cls, prob + "%", "PROBABILITÀ", whenLabel, dir === "neutral" ? "Incerta" : label));
  }

  function renderFcChart() {
    const f = forecast();
    const box = clear($("fc-chart"));
    const rows = data && data.chart ? arr(data.chart.D) : [];
    const bars = rows.filter((r) => Array.isArray(r) && r.length === 5 && r.every((x) => num(x) !== null)).slice(-30)
      .map((r) => ({ t: r[0], o: r[1], h: Math.max(r[1], r[2], r[3], r[4]), l: Math.min(r[1], r[2], r[3], r[4]), c: r[4] }));
    const pts = bars.map((b) => [b.t, b.c]);
    const v = f && f.volatility;
    if (!f || !v || pts.length < 5 || !Array.isArray(v.range68) || !Array.isArray(v.range95)) {
      emptyNote(box, "Proiezione non disponibile.");
      return;
    }
    const price = num(f.ref_price) ?? pts[pts.length - 1][1];
    const t0 = Math.max(pts[pts.length - 1][0], Date.now() / 1000 - 86400);
    const ws = num(f.week_start), we = num(f.week_end);
    if (ws === null || we === null || we <= t0) { emptyNote(box, "Proiezione non disponibile."); return; }
    const [lo68, hi68] = v.range68.map(num), [lo95, hi95] = v.range95.map(num);
    if ([lo68, hi68, lo95, hi95].some((x) => x === null)) { emptyNote(box, "Proiezione non disponibile."); return; }
    const levels = [].concat(arr(f.levels && f.levels.resistance).slice(0, 2), arr(f.levels && f.levels.support).slice(0, 2))
      .filter((z) => num(z.low) !== null && num(z.high) !== null);

    const narrow = box.clientWidth > 0 && box.clientWidth < 600;
    const W = narrow ? 460 : 900, H = narrow ? 300 : 340, L = 8, R = narrow ? 64 : 92, T = 26, B = 30;
    // Horizontal axis in candles: no holes on weekends; after the last candle, one slot per day.
    const slotOf = (t) => {
      const n = bars.length;
      if (t >= bars[n - 1].t) return n - 1 + (t - bars[n - 1].t) / 86400;
      if (t <= bars[0].t) return 0;
      const i = bars.findIndex((b) => b.t > t);
      return i - 1 + (t - bars[i - 1].t) / (bars[i].t - bars[i - 1].t);
    };
    const slots = slotOf(we) + 0.5;
    let yMin = Math.min(lo95, ...bars.map((b) => b.l)), yMax = Math.max(hi95, ...bars.map((b) => b.h));
    levels.forEach((z) => { if (z.low >= yMin * 0.97 && z.high <= yMax * 1.03) { yMin = Math.min(yMin, z.low); yMax = Math.max(yMax, z.high); } });
    const pad = (yMax - yMin) * 0.06; yMin -= pad; yMax += pad;
    const x = (t) => L + (slotOf(t) + 0.5) / (slots + 0.5) * (W - L - R);
    const y = (p) => T + (yMax - p) / (yMax - yMin) * (H - T - B);
    const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Proiezione del prezzo con range 68% e 95%" });

    for (let i = 0; i < 4; i++) {
      const p = yMin + (yMax - yMin) * i / 4;
      s.append(svg("line", { class: "grid", x1: L, x2: W - R, y1: y(p), y2: y(p) }));
      if (Math.abs(y(p) - y(hi68)) > 14 && Math.abs(y(p) - y(lo68)) > 14)
        s.append(svg("text", { class: "axis", x: W - R + 8, y: y(p) + 4 }, fmt(p, 0)));
    }
    s.append(svg("rect", { class: "week", x: x(ws), y: T - 14, width: Math.max(2, x(we) - x(ws)), height: H - T - B + 14 }));
    s.append(svg("text", { class: "week-label", x: x(we) - 4, y: T - 3, "text-anchor": "end" },
      str(f.label).replace(/\s\d{4}$/, "").toUpperCase()));
    // date ticks
    pts.forEach((p, i) => {
      if (i % (narrow ? 12 : 7) !== 0) return;
      s.append(svg("text", { class: "axis", x: x(p[0]), y: H - 8, "text-anchor": i === 0 ? "start" : "middle" },
        new Date(p[0] * 1000).toLocaleDateString("it-IT", { day: "2-digit", month: "short" })));
    });

    // cones: width grows with the square root of time
    const sh68 = Math.log(hi68 / price), sl68 = Math.log(price / lo68);
    const sh95 = Math.log(hi95 / price), sl95 = Math.log(price / lo95);
    function cone(su, sd) {
      const up = [], dn = [];
      for (let k = 0; k <= 24; k++) {
        const fr = k / 24, t = t0 + (we - t0) * fr, sq = Math.sqrt(fr);
        up.push(`${x(t).toFixed(1)},${y(price * Math.exp(su * sq)).toFixed(1)}`);
        dn.push(`${x(t).toFixed(1)},${y(price * Math.exp(-sd * sq)).toFixed(1)}`);
      }
      return { poly: up.concat(dn.reverse()).join(" "), up: up.join(" "), dn: dn.join(" ") };
    }
    const c95 = cone(sh95, sl95), c68 = cone(sh68, sl68);
    s.append(svg("polygon", { class: "c95", points: c95.poly }));
    s.append(svg("polygon", { class: "c68", points: c68.poly }));
    s.append(svg("polyline", { class: "c-edge", points: c68.up }));
    s.append(svg("polyline", { class: "c-edge", points: c68.dn }));

    const bodyW = Math.max(2, (W - L - R) / (slots + 0.5) * 0.68);
    bars.forEach((b) => {
      const cls = b.c >= b.o ? "bull" : "bear", cx = x(b.t);
      const top = y(Math.max(b.o, b.c)), bottom = y(Math.min(b.o, b.c));
      s.append(svg("line", { class: "wick " + cls, x1: cx, x2: cx, y1: y(b.h), y2: y(b.l) }));
      s.append(svg("rect", { class: "body " + cls, x: cx - bodyW / 2, y: top, width: bodyW, height: Math.max(1, bottom - top) }));
    });

    const usedY = [];
    levels.forEach((z) => {
      const cls = z.dir === "bull" ? "bull" : "bear";
      const mid = (z.low + z.high) / 2;
      if (mid < yMin || mid > yMax || usedY.some((u) => Math.abs(u - y(mid)) < 16)) return;
      usedY.push(y(mid));
      s.append(svg("line", { class: "lvl " + cls, x1: x(pts[Math.floor(pts.length / 2)][0]), x2: W - R, y1: y(mid), y2: y(mid) }));
      s.append(svg("text", { class: "lvl-label " + cls, x: x(pts[Math.floor(pts.length / 2)][0]), y: y(mid) - 5 },
        `${str(z.type) === "Order block" ? "OB" : str(z.type).slice(0, 4)} ${str(z.tf)} ${fmt(z.low, 0)}–${fmt(z.high, 0)}`));
    });

    s.append(svg("circle", { class: "now-ring", cx: x(t0), cy: y(price), r: 8 }));
    s.append(svg("circle", { class: "now", cx: x(t0), cy: y(price), r: 3.5 }));
    s.append(svg("text", { class: "end-label", x: x(we) + 6, y: y(hi68) + 4 }, fmt(hi68, 0)));
    s.append(svg("text", { class: "end-label", x: x(we) + 6, y: y(lo68) + 4 }, fmt(lo68, 0)));
    box.append(s);
    $("fc-chart-note").textContent = "Candele Daily (verde = rialzo, rosso = ribasso). Cono chiaro: range 68% (circa 2 settimane su 3 la chiusura cade qui); " +
      "cono tenue: range 95%. Il cono si allarga con la radice del tempo; linee tratteggiate: zone W/D più vicine.";
  }

  function stat(label, value, sub) {
    return el("div", { class: "panel" }, el("div", { class: "stat-l" }, label), el("div", { class: "stat-v" }, value), el("div", { class: "stat-s" }, sub));
  }

  function renderFcRanges() {
    const box = clear($("fc-ranges"));
    const f = forecast(), v = f && f.volatility;
    if (!v) { box.classList.add("hidden"); return; }
    box.classList.remove("hidden");
    const r68 = arr(v.range68), r95 = arr(v.range95);
    box.append(
      stat("Range probabile 68%", `${fmt(num(r68[0]), 0)} – ${fmt(num(r68[1]), 0)}`, "chiusura della settimana"),
      stat("Range estremo 95%", `${fmt(num(r95[0]), 0)} – ${fmt(num(r95[1]), 0)}`, "solo 1 settimana su 20 fuori"),
      stat("Escursione tipica", num(v.typical_range_usd) === null ? "—" : `${fmt(v.typical_range_usd, 0)} $`,
        num(v.typical_range_pct) === null ? "" : `massimo–minimo, ${fmt(v.typical_range_pct, 1)}% (mediana 26 sett.)`),
      stat("Volatilità settimanale", `${fmt(num(v.sigma_week_pct), 2)}%`,
        v.event_boost ? "aumentata: dati USD chiave in calendario" : "deviazione standard ultime 52 settimane"));
  }

  function renderFcScenarios() {
    const box = clear($("fc-scenarios"));
    const f = forecast();
    const sc = f ? arr(f.scenarios) : [];
    if (!sc.length) { emptyNote(box, "Scenari non disponibili."); return; }
    sc.forEach((s) => {
      const [, cls] = biasOf(s.dir);
      const name = s.kind === "main" ? "Scenario principale" : "Scenario alternativo";
      box.append(el("div", { class: "scen " + cls },
        el("div", { class: "scen-h" }, el("span", null, name), num(s.prob) !== null ? el("b", null, s.prob + "%") : el("span", null, "se attivato")),
        el("p", null, str(s.text))));
    });
  }

  function renderFcDrivers() {
    const box = clear($("fc-drivers"));
    const f = forecast();
    const dr = f ? arr(f.drivers) : [];
    if (!dr.length) { emptyNote(box, "Nessun fattore con voto netto."); return; }
    dr.forEach((x) => {
      const v = num(x.vote) ?? 0;
      box.append(el("div", { class: "drv" },
        el("span", { class: "drv-l" }, str(x.label), el("span", { class: "tag" }, str(x.tf))),
        el("span", { class: "drv-v " + (v > 0 ? "bull" : v < 0 ? "bear" : "neutral") }, (v > 0 ? "+" : "") + v + (v > 0 ? " rialzista" : v < 0 ? " ribassista" : "")),
        el("span", { class: "drv-w" }, str(x.why))));
    });
    const m = f.components && f.components.model;
    if (m) box.append(el("p", { class: "note" }, `Modello Overmind (macro, COT, struttura, momentum di Weekly e Daily): ` +
      `probabilità di rialzo ${fmt(num(m.p_up), 0)}%. Pesa per metà nella previsione finale.`));
  }

  function renderFcAnalogs() {
    const box = clear($("fc-analogs"));
    const f = forecast();
    const a = f && f.components && f.components.analogs;
    if (!a) { emptyNote(box, "Storico insufficiente per gli analoghi."); return; }
    const raw = num(a.raw_up_pct);
    box.append(el("div", { class: "big" }, raw === null ? "—" : raw + "%"),
      el("p", { class: "muted small" }, `delle ${num(a.n) ?? 0} settimane con lo stesso assetto negli ultimi ${fmt(num(a.years), 0)} anni ` +
        `la settimana successiva ha chiuso in rialzo (media di tutte le settimane: ${num(a.base_rate_pct) ?? "—"}%).`));
    const ul = el("ul", { class: "state" });
    arr(a.state).forEach((s) => ul.append(el("li", null, str(s))));
    box.append(el("div", { class: "stat-l" }, "Assetto attuale"), ul,
      el("p", { class: "note" }, `Variazione mediana della settimana successiva: ${pct(num(a.median_ret_pct))}. ` +
        `Dopo la correzione per campione piccolo: ${fmt(num(a.p_up), 0)}% di rialzo. Pesa per metà nella previsione finale.`));
  }

  function renderFcEvents() {
    const box = clear($("fc-events"));
    const f = forecast();
    const evs = f ? arr(f.events) : [];
    if (!evs.length) {
      emptyNote(box, "Nessun evento USD ad alto impatto noto fino alla fine della settimana prevista. " +
        "Il calendario della settimana successiva viene pubblicato nel weekend: la previsione si aggiorna da sola.");
      return;
    }
    const key = new Set(arr(f.key_events).map(str));
    const tbody = el("tbody");
    evs.forEach((e) => tbody.append(el("tr", null, el("td", { class: "num" }, fmtEvTime(e.t)),
      el("td", null, str(e.title), key.has(str(e.title)) ? el("span", { class: "tag" }, "chiave") : null))));
    box.append(el("table", null, tbody));
    if (key.size) box.append(el("p", { class: "note" }, "Con dati chiave (CPI, PCE, NFP, Fed) il range previsto è allargato del 15%."));
  }

  function renderFcAI(d) {
    const box = clear($("fc-ai"));
    const t = d.ai && typeof d.ai === "object" ? str(d.ai.next_week) : "";
    if (!t) { emptyNote(box, "Lettura IA della settimana non disponibile in questo aggiornamento: i numeri sopra sono comunque calcolati dal motore."); return; }
    box.append(el("p", { class: "lead" }, t), el("p", { class: "note" }, "Testo generato dall'IA a partire dai soli dati calcolati."));
  }

  function renderFcTrack(d) {
    const box = clear($("fc-track"));
    const tr = d.forecast_track;
    const st = tr && tr.stats ? tr.stats : {};
    box.append(el("div", { class: "stats two" },
      stat("Direzione corretta", num(st.accuracy) === null ? "—" : st.accuracy + "%", `${num(st.evaluated) ?? 0} settimane valutate`),
      stat("Chiusura nel range 68%", num(st.in_range68_pct) === null ? "—" : st.in_range68_pct + "%", "ideale ≈ 68% se il range è calibrato")));
    const recent = tr ? arr(tr.recent) : [];
    if (recent.length) {
      const tbody = el("tbody");
      recent.slice().reverse().forEach((e) => {
        const [label, cls] = biasOf(e.direction);
        tbody.append(el("tr", null, el("td", null, str(e.label)),
          el("td", null, el("span", { class: cls }, e.direction === "neutral" ? "Incerta" : label), num(e.probability) !== null ? el("span", { class: "muted num" }, ` ${e.probability}%`) : null),
          el("td", { class: "num" }, fmt(num(e.ref_price)) + " → " + fmt(num(e.close))),
          el("td", null, e.outcome === "hit" ? el("span", { class: "bull" }, "✓ corretta") : e.outcome === "miss" ? el("span", { class: "bear" }, "✗ errata") : el("span", { class: "muted" }, "non valutata")),
          el("td", { class: "muted" }, e.in_range68 === true ? "nel range" : e.in_range68 === false ? "fuori range" : "")));
      });
      box.append(el("div", { class: "scroll-x" }, el("table", null, el("thead", null, el("tr", null, el("th", null, "Settimana"),
        el("th", null, "Previsione"), el("th", null, "Prezzo"), el("th", null, "Esito"), el("th", null, "Range 68%"))), tbody)));
    }
    box.append(el("p", { class: "note" }, "La previsione sul sito si aggiorna durante la settimana; per il track record conta quella " +
      "calcolata prima dell'apertura, che viene congelata e verificata alla chiusura del venerdì. La prima verifica arriva dopo la prima settimana completa."));
  }

  // ------------------------------------------------------------ views
  function setView(v) {
    view = VIEWS.includes(v) ? v : "analisi";
    VIEWS.forEach((name) => $("view-" + name).classList.toggle("hidden", name !== view));
    document.querySelectorAll(".views a").forEach((a) => a.classList.toggle("active", a.dataset.view === view));
    if (data && view === "analisi" && !$("chart").classList.contains("hidden")) drawChart(currentTf);
    if (data && view === "previsioni") { try { renderFcChart(); } catch (e) { /* section isolated */ } }
  }

  // ------------------------------------------------------------ main
  function render(d) {
    data = d;
    $("price").textContent = num(d.price) !== null ? fmt(d.price) : "—";
    const { problems, severe } = collectProblems(d);
    // Each section is isolated: malformed data in one part can't blank the whole page.
    const sections = {
      "Verdetto": renderHero, "Bias per timeframe": renderCards, "Rotture": renderBreaks, "Avviso rotture": renderBreakAlert, "Spiegazione IA": renderAI, "Macro": renderMacro,
      "Notizia": renderNewsFlash, "Eventi": renderEvents, "Track record": renderTrack, "Stato fonti": renderSources,
      "Previsione": renderFcHero, "Proiezione": renderFcChart, "Range": renderFcRanges, "Scenari": renderFcScenarios,
      "Fattori previsione": renderFcDrivers, "Analoghi": renderFcAnalogs, "Eventi in arrivo": renderFcEvents,
      "IA previsione": renderFcAI, "Track record previsioni": renderFcTrack,
      "Grafico": () => { if (view === "analisi" && !$("chart").classList.contains("hidden")) drawChart(currentTf); },
    };
    let broken = false;
    for (const [name, fn] of Object.entries(sections)) {
      try { fn(d); } catch (e) { broken = true; problems.push(`Sezione "${name}" non visualizzabile: dati malformati.`); }
    }
    renderBanner(problems, severe || broken);
    renderStatus(d, severe || broken);
  }

  async function load() {
    try {
      const res = await fetch("data/analysis.json", { cache: "no-store" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const d = await res.json();
      if (!d || typeof d !== "object") throw new Error("formato non valido");
      render(d);
    } catch (e) {
      renderBanner([`Impossibile caricare i dati dell'analisi (${e.message}). ` +
        (data ? "Mostro l'ultima versione caricata." : "Riprova tra qualche minuto.")], true);
      $("sys-dot").className = "dot-pulse down";
      $("sys-text").textContent = "Dati non raggiungibili";
    }
  }

  let resizeTimer = null, lastNarrow = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      const n = window.innerWidth < 640;
      if (n !== lastNarrow && data) { lastNarrow = n; try { renderFcChart(); } catch (e) { /* section isolated */ } }
    }, 200);
  });
  setupTabs();
  window.addEventListener("hashchange", () => setView(location.hash.slice(1)));
  setView(location.hash.slice(1));
  load();
  setInterval(load, REFRESH_MS);
})();
