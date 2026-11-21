"use strict";
/* Overmind front-end.
 * Security: every value from analysis.json (including AI text) is treated as untrusted and
 * rendered with textContent only. No innerHTML, no eval, no inline handlers (CSP enforces it). */
(function () {
  const STALE_HOURS = 5;
  const REFRESH_MS = 10 * 60 * 1000;
  const TF_NAMES = { W: "Weekly", D: "Daily", H4: "H4" };
  const BIAS = { bullish: ["Rialzista", "bull"], bearish: ["Ribassista", "bear"], neutral: ["Neutrale", "neutral"] };
  const STATUS_IT = { ok: "OK", degraded: "Parziale", down: "Non disponibile", disabled: "Non configurata" };

  let data = null, chart = null, series = null, currentTf = "H4", liveLoaded = false;

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
  const $ = (id) => document.getElementById(id);
  const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);
  const str = (v) => (typeof v === "string" ? v : "");
  const fmt = (v, d = 2) => (num(v) === null ? "—" :
    v.toLocaleString("it-IT", { minimumFractionDigits: d, maximumFractionDigits: d }));
  const clear = (node) => { while (node.firstChild) node.removeChild(node.firstChild); return node; };
  function fmtTime(iso) {
    const t = Date.parse(str(iso));
    return Number.isNaN(t) ? "—" : new Date(t).toLocaleString("it-IT", { dateStyle: "short", timeStyle: "short" });
  }
  function ago(iso) {
    const t = Date.parse(str(iso));
    if (Number.isNaN(t)) return null;
    return (Date.now() - t) / 3600000;
  }

  // ------------------------------------------------------------ banner
  function renderBanner(problems, severe) {
    const b = $("banner");
    clear(b);
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
    (Array.isArray(d.sources) ? d.sources : []).forEach((s) => {
      if (s && s.status && s.status !== "ok") problems.push(`${str(s.label)}: ${str(s.message) || (Object.hasOwn(STATUS_IT, s.status) ? STATUS_IT[s.status] : "stato sconosciuto")}`);
    });
    return { problems, severe };
  }

  // ------------------------------------------------------------ cards
  function renderCards(d) {
    const box = clear($("tf-cards"));
    const tfs = d.timeframes || {};
    for (const tf of ["W", "D", "H4"]) {
      const t = tfs[tf];
      if (!t) { box.append(el("div", { class: "card" }, el("div", { class: "tf" }, TF_NAMES[tf]), "Dati non disponibili")); continue; }
      const [label, cls] = Object.hasOwn(BIAS, t.bias) ? BIAS[t.bias] : BIAS.neutral;
      const conf = Math.max(0, Math.min(100, num(t.confidence) ?? 0));
      const bar = el("i"); bar.style.width = conf + "%";
      const card = el("div", { class: "card" },
        el("div", { class: "tf" }, TF_NAMES[tf]),
        el("div", { class: "bias " + cls }, label),
        el("div", { class: "ctx" }, str(t.context)),
        el("div", { class: "conf" },
          el("div", { class: "conf-row" }, el("span", null, "Confidenza"), el("b", null, conf + "%")),
          el("div", { class: "bar" }, bar),
          t.conflict ? el("span", { class: "conflict" }, "Segnali in conflitto · confidenza bassa") : null),
      );
      const z = t.zone_of_interest;
      if (z && num(z.low) !== null) {
        card.append(el("div", { class: "zoi" }, "Zona di interesse: ",
          el("b", null, z.low === z.high ? fmt(z.low) : `${fmt(z.low)} – ${fmt(z.high)}`),
          el("div", { class: "muted small" }, `${str(z.type)} ${z.dir === "bull" ? "rialzista" : "ribassista"} · ${fmt(z.dist_pct, 1)}% dal prezzo`)));
      } else {
        card.append(el("div", { class: "zoi muted" }, "Nessuna zona di interesse coerente con il bias"));
      }
      card.append(renderFactors(t));
      box.append(card);
    }
  }

  function renderFactors(t) {
    const det = el("details", null, el("summary", null, "Perché questa confidenza? (voti dei fattori)"));
    const tbody = el("tbody");
    (Array.isArray(t.factors) ? t.factors : []).forEach((f) => {
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
    if (pd) det.append(el("p", { class: "muted small" },
      `Range ${fmt(pd.range_low)} – ${fmt(pd.range_high)}: prezzo in ${str(pd.label)}.`));
    return det;
  }

  // ------------------------------------------------------------ AI
  function renderAI(d) {
    const box = clear($("ai"));
    const ai = d.ai;
    if (!ai || typeof ai !== "object") {
      box.append(el("p", { class: "muted" }, "Spiegazione IA non disponibile in questo aggiornamento. " +
        "Bias, confidenza e zone sopra sono comunque calcolati dal motore e validi."));
      return;
    }
    box.append(el("p", { class: "ai-summary" }, str(ai.summary)));
    for (const tf of ["W", "D", "H4"]) if (str(ai[tf])) box.append(el("p", { class: "ai-tf" }, el("b", null, TF_NAMES[tf] + ": "), str(ai[tf])));
    if (Array.isArray(ai.risks) && ai.risks.length) {
      const ul = el("ul", { class: "risks" });
      ai.risks.forEach((r) => ul.append(el("li", null, str(r))));
      box.append(el("div", { class: "muted small" }, "Rischi per lo scenario:"), ul);
    }
    box.append(el("p", { class: "muted small" }, "Testo generato dall'IA a partire dai soli dati calcolati: verifica sempre i numeri."));
  }

  // ------------------------------------------------------------ chart
  function zonesFor(tf) {
    const a = data && data.timeframes && data.timeframes[tf] && data.timeframes[tf].analysis;
    if (!a) return [];
    return [].concat(a.zones_above || [], a.zones_inside || [], a.zones_below || []);
  }

  function drawChart(tf) {
    const container = $("chart");
    const rows = data && data.chart && Array.isArray(data.chart[tf]) ? data.chart[tf] : [];
    if (!window.LightweightCharts) { clear(container).append(el("p", { class: "muted" }, "Libreria grafico non caricata.")); return; }
    if (!chart) {
      chart = LightweightCharts.createChart(container, {
        autoSize: true,
        layout: { background: { color: "transparent" }, textColor: "#8a93a6" },
        grid: { vertLines: { color: "#1a2030" }, horzLines: { color: "#1a2030" } },
        timeScale: { timeVisible: true, borderColor: "#252d40" },
        rightPriceScale: { borderColor: "#252d40" },
      });
    }
    if (series) chart.removeSeries(series);
    series = chart.addCandlestickSeries({ upColor: "#26a69a", downColor: "#ef5350", borderVisible: false,
      wickUpColor: "#26a69a", wickDownColor: "#ef5350" });
    const clean = rows.filter((r) => Array.isArray(r) && r.length === 5 && r.every((x) => num(x) !== null))
      .map((r) => ({ time: r[0], open: r[1], high: r[2], low: r[3], close: r[4] }));
    series.setData(clean);
    const zoi = data.timeframes && data.timeframes[tf] && data.timeframes[tf].zone_of_interest;
    zonesFor(tf).forEach((z) => {
      if (num(z.low) === null || num(z.high) === null) return;
      const isZoi = zoi && zoi.low === z.low && zoi.high === z.high;
      const color = z.dir === "bull" ? "#26a69a" : "#ef5350";
      const short = str(z.type).startsWith("Liquidit") ? "LIQ" : str(z.type) === "FVG" ? "FVG" : "OB";
      const opts = { color, lineWidth: isZoi ? 2 : 1, lineStyle: isZoi ? 0 : 2, axisLabelVisible: isZoi };
      series.createPriceLine({ ...opts, price: z.high, title: short + (isZoi ? " ★" : "") });
      if (z.low !== z.high) series.createPriceLine({ ...opts, price: z.low, title: "" });
    });
    // Apply the visible range after layout, otherwise the first draw (container not yet sized) is off.
    const lastN = { W: 120, D: 150, H4: 180 }[tf];
    requestAnimationFrame(() => setTimeout(() => {
      if (clean.length > lastN) chart.timeScale().setVisibleLogicalRange({ from: clean.length - lastN, to: clean.length + 5 });
      else chart.timeScale().fitContent();
    }, 30));
    $("chart-note").textContent = `Fonte: ${str(data.provider)}. Linee: OB = order block, FVG = fair value gap, ` +
      "LIQ = liquidità; verde = domanda, rosso = offerta, ★ = zona di interesse. Grafico aggiornato a ogni analisi (ogni 4h).";
  }

  function loadLive() {
    if (liveLoaded) return;
    liveLoaded = true;
    const box = $("live");
    const wrap = el("div", { class: "tradingview-widget-container" });
    wrap.style.height = "100%";
    const inner = el("div", { class: "tradingview-widget-container__widget" });
    inner.style.height = "100%";
    const s = document.createElement("script");
    s.src = "https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js";
    s.async = true;
    s.textContent = JSON.stringify({ autosize: true, symbol: "OANDA:XAUUSD", interval: "240", timezone: "Europe/Rome",
      theme: "dark", style: "1", locale: "it", allow_symbol_change: false, hide_side_toolbar: false });
    wrap.append(inner, s);
    box.append(wrap);
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
  function pct(v) { return num(v) === null ? "—" : (v > 0 ? "+" : "") + fmt(v, 2) + "%"; }
  function fmtEvTime(t) {
    return num(t) === null ? "—" : new Date(t * 1000).toLocaleString("it-IT",
      { weekday: "short", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", timeZone: "Europe/Rome" });
  }

  function renderNewsFlash(d) {
    const box = clear($("news-flash"));
    const u = d.updated_after_event;
    if (!u || !Array.isArray(u.titles) || !u.titles.length) { box.classList.add("hidden"); return; }
    box.classList.remove("hidden");
    box.append(el("b", null, "Previsione aggiornata dopo la notizia: "), u.titles.map(str).join(", "),
      u.vote ? el("div", { class: "muted small" }, str(u.vote)) : null);
  }

  function renderEvents(d) {
    const box = clear($("events"));
    const ev = d.events;
    const items = ev && Array.isArray(ev.items) ? ev.items : [];
    if (!items.length) {
      box.append(el("p", { class: "muted" }, "Nessun evento ad alto impatto disponibile per questa settimana."));
      return;
    }
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
          el("span", { class: r1 < 0 ? "bear" : r1 > 0 ? "bull" : "neutral" }, pct(r1) + (num(rx.r1h_pct) !== null ? " (1h)" : " (finora)")),
          num(rx.r4h_pct) !== null ? el("div", { class: "muted small" }, pct(rx.r4h_pct) + " (4h)" + out) : null);
      } else rxCell = el("span", { class: "muted" }, "n/d");
      tbody.append(el("tr", { class: e.status === "upcoming" ? (i === nextIdx ? "next" : "") : "past" },
        el("td", { class: "num" }, fmtEvTime(e.t)),
        el("td", null, str(e.title), el("span", { class: "tag" }, str(e.family))),
        el("td", { class: "num" }, str(e.forecast) || "—", el("div", { class: "muted small" }, "prec. " + (str(e.previous) || "—"))),
        el("td", null, scen ? el("span", { class: scen[1] }, scen[0]) : el("span", { class: "muted" }, "dipende dai toni"),
          el("div", { class: "muted small" }, str(e.why))),
        el("td", null, el("span", { class: lc }, ld),
          num(lean.confidence) !== null && lean.dir ? el("b", null, " " + lean.confidence + "%") : null,
          el("div", { class: "muted small" }, str(lean.why))),
        el("td", null, rxCell),
        el("td", { class: "num muted" }, num(e.avg_move_1h_pct) !== null ? "±" + fmt(e.avg_move_1h_pct, 2) + "%" : "in raccolta")));
    });
    box.append(el("div", { class: "ev-table" }, el("table", null, el("thead", null, el("tr", null,
      el("th", null, "Quando"), el("th", null, "Evento"), el("th", null, "Previsione"),
      el("th", null, "Se sopra le attese"), el("th", null, "Inclinazione Overmind"),
      el("th", null, "Reazione oro"), el("th", null, "Movimento medio 1h"))), tbody)));
    const st = ev.stats || {};
    box.append(el("p", { class: "muted small" },
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
    if (!tr) { box.append(el("p", { class: "muted" }, "Nessuno storico ancora.")); return; }
    const tbody = el("tbody");
    for (const tf of ["W", "D", "H4"]) {
      const s = tr[tf]; if (!s) continue;
      tbody.append(el("tr", null, el("td", null, TF_NAMES[tf]),
        el("td", { class: "num" }, s.accuracy === null ? "—" : s.accuracy + "%"),
        el("td", { class: "num muted" }, `${num(s.evaluated) ?? 0} valutate · ${num(s.pending) ?? 0} in attesa`)));
    }
    box.append(el("table", null, el("thead", null, el("tr", null, el("th", null, "TF"), el("th", null, "Bias corretti"), el("th", null, "Campione"))), tbody));
    box.append(el("p", { class: "muted small" }, "Una previsione è corretta se, dopo 4 candele Weekly / 5 Daily / 6 H4, " +
      "il prezzo si è mosso nella direzione del bias. Servono almeno 30–50 previsioni per un giudizio affidabile; " +
      "la calibrazione (accuratezza per fascia di confidenza) è nel file history.json."));
  }

  function renderSources(d) {
    const box = clear($("sources"));
    const tbody = el("tbody");
    (Array.isArray(d.sources) ? d.sources : []).forEach((s) => {
      const st = ["ok", "degraded", "down", "disabled"].includes(s.status) ? s.status : "down";
      tbody.append(el("tr", null,
        el("td", null, el("span", { class: "dot " + st }), str(s.label)),
        el("td", null, STATUS_IT[st]),
        el("td", { class: "muted small" }, str(s.message)),
        el("td", { class: "num muted small" }, s.last_success ? "ultimo OK " + fmtTime(s.last_success) : "mai")));
    });
    box.append(el("table", null, tbody));
  }

  // ------------------------------------------------------------ main
  function render(d) {
    data = d;
    $("price").textContent = num(d.price) !== null ? "$ " + fmt(d.price) : "—";
    $("updated").textContent = "Aggiornato " + fmtTime(d.generated_at);
    const { problems, severe } = collectProblems(d);
    // Each section is isolated: malformed data in one part can't blank the whole page.
    const sections = { "Bias per timeframe": renderCards, "Spiegazione IA": renderAI, "Macro": renderMacro,
      "Notizia": renderNewsFlash, "Eventi": renderEvents,
      "Track record": renderTrack, "Stato fonti": renderSources,
      "Grafico": () => { if (!$("chart").classList.contains("hidden")) drawChart(currentTf); } };
    let broken = false;
    for (const [name, fn] of Object.entries(sections)) {
      try { fn(d); } catch (e) { broken = true; problems.push(`Sezione "${name}" non visualizzabile: dati malformati.`); }
    }
    renderBanner(problems, severe || broken);
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
    }
  }

  setupTabs();
  load();
  setInterval(load, REFRESH_MS);
})();
