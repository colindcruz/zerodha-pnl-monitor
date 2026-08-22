/*
 * Shared rendering logic between dashboard.html (live) and replay.html
 * (historical step-through) — one render(data) function taking the same
 * state-payload shape state.py's DashboardState.latest produces, whether it
 * arrived over the live WebSocket or was pulled out of a prebuilt replay
 * array. Kept as a single file so the two pages' rendering can never drift
 * out of sync with each other.
 */
"use strict";

  const fmtPts = (v) => v == null ? "—" : (v >= 0 ? "+" : "") + v.toFixed(1);
  const fmtPrice = (v) => v == null ? "—" : v.toLocaleString("en-IN", { maximumFractionDigits: 2 });
  // Rounded to the nearest integer — used only for the header's NIFTY spot
  // display, which is colored green/red/white against the previous 5-min
  // bar's close (see render()) rather than showing decimal precision.
  const fmtSpotInt = (v) => v == null ? "—" : Math.round(v).toLocaleString("en-IN");
  const fmtTime = (iso) => {
    if (!iso) return "—";
    return new Date(iso).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  };
  const fmtHM = (iso) => {
    if (!iso) return "—";
    return new Date(iso).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", hour12: false });
  };
  const el = (id) => document.getElementById(id);

  // ---------------------------------------------------------------
  // Small render helpers
  // ---------------------------------------------------------------
  function toneClass(kind, value) {
    if (kind === "decision") return value === "ENTER" ? "good" : value === "WAIT_FOR_PULLBACK" ? "warn" : "neutral";
    if (kind === "trend") {
      if (!value) return "neutral";
      if (value.includes("BULL")) return "good";
      if (value.includes("BEAR")) return "bad";
      return "neutral";
    }
    if (kind === "entry") return value >= 4 ? "good" : value >= 3 ? "warn" : "neutral";
    if (kind === "extension") return value === "VERY_EXTENDED" ? "bad" : value === "EXTENDED" ? "warn" : "good";
    if (kind === "runway") return value === "POOR" ? "bad" : value === "MARGINAL" ? "warn" : "good";
    return "neutral";
  }

  const TREND_ICONS = { STRONG_BULL: "\u{1F402}", BULL: "\u{1F402}", WEAK_BULL: "\u{1F402}",
    NEUTRAL: "➖", WEAK_BEAR: "\u{1F43B}", BEAR: "\u{1F43B}", STRONG_BEAR: "\u{1F43B}" };

  const MOMENTUM_SUBTITLE = {
    STRENGTHENING: "Trend is strong and strengthening",
    STABLE: "Trend is holding steady",
    MATURING: "Trend is showing signs of fatigue",
    DETERIORATING: "Trend momentum is fading",
  };

  const SCORE_SUBTITLE = (abs) => abs >= 4 ? "Very Strong Alignment" : abs === 3 ? "Strong Alignment" :
    abs === 2 ? "Moderate Alignment" : abs === 1 ? "Weak Alignment" : "Mixed Signals";

  const PM_LADDER = [
    { state: "TREND_HEALTHY", icon: "✅", label: "TREND HEALTHY", tone: "good",
      bullets: ["Structure intact", "ADX rising", "Aroon dominance", "Price above VWAP"],
      action: "HOLD", actionSub: "Normal trail" },
    { state: "TREND_MATURING", icon: "\u{1F4C8}", label: "TREND MATURING", tone: "warn",
      bullets: ["ADX flat or slowing", "Smaller candles", "Price near resistance"],
      action: "TIGHTEN TRAIL", actionSub: "Take partial" },
    { state: "MOMENTUM_DETERIORATING", icon: "⚠️", label: "MOMENTUM DETERIORATING", tone: "warn",
      bullets: ["Aroon weakening", "DMI lines converging", "Lower highs forming"],
      action: "TIGHTEN HARD", actionSub: "Reduce" },
    { state: "STRUCTURE_AT_RISK", icon: "⚠️", label: "STRUCTURE AT RISK", tone: "bad",
      bullets: ["HL broken (in uptrend)", "Price losing VWAP", "ADX falling"],
      action: "PROTECT PROFIT", actionSub: "Move SL" },
    { state: "TREND_FAILED", icon: "❌", label: "TREND FAILED", tone: "bad",
      bullets: ["Aroon cross (bearish)", "DMI bear cross", "Structure broken", "Price below VWAP"],
      action: "EXIT TRADE", actionSub: "Don't hope" },
  ];

  function render(data) {
    if (!data || !data.decision) return;

    // ---- header: NIFTY spot (green above / red below / white equal to the
    // previous 5-min bar's close) ----
    const spotEl = el("header-spot-value");
    spotEl.textContent = fmtSpotInt(data.spot);
    const prevClose = data.prev_5min_close;
    let spotTone = "same";
    if (data.spot != null && prevClose != null) {
      spotTone = data.spot > prevClose ? "up" : data.spot < prevClose ? "down" : "same";
    }
    spotEl.className = "v " + spotTone;

    // ---- hero: market state ----
    const t = data.trend || {};
    const isORB = t.mode === "OPENING_RANGE_BREAKOUT";
    el("market-state-icon").textContent = TREND_ICONS[t.direction] || "➖";
    const msv = el("market-state-value");
    msv.textContent = (t.direction || "—").replaceAll("_", " ");
    msv.className = "hero-value " + toneClass("trend", t.direction);
    el("market-state-sub").textContent = isORB
      ? "Opening-range breakout read (early session — standard trend not ready yet)"
      : (MOMENTUM_SUBTITLE[t.momentum] || "—");

    // ---- hero: trend score + vote bars ----
    const tsv = el("trend-score-value");
    tsv.textContent = t.score != null ? (t.score >= 0 ? "+" : "") + t.score + " / 5" : "—";
    tsv.className = "hero-value " + toneClass("trend", t.direction);
    el("trend-score-sub").textContent = isORB
      ? "ORB mode"
      : (t.score != null ? SCORE_SUBTITLE(Math.abs(t.score)) : "—");
    const votesEl = el("vote-bars");
    votesEl.innerHTML = "";
    if (isORB) {
      votesEl.innerHTML = '<div style="font-size:10px; color:var(--warn);">Standard 5-vote system not yet available — using opening-range breakout for the first '
        + '~hour of the session.</div>';
    }
    const VOTE_LABELS = { aroon: "Aroon", ema_structure: "EMA", vwap: "VWAP", dmi: "DMI", price_structure: "Structure" };
    Object.entries(t.votes || {}).forEach(([key, val]) => {
      const row = document.createElement("div");
      row.className = "vote-bar-row";
      const fillClass = val > 0 ? "pos" : val < 0 ? "neg" : "zero";
      const width = val === 0 ? 15 : 100;
      row.innerHTML = `<span class="name">${VOTE_LABELS[key] || key}</span>
        <span class="vote-bar-track"><span class="vote-bar-fill ${fillClass}" style="width:${width}%"></span></span>`;
      votesEl.appendChild(row);
    });

    // ---- hero: trading decision ----
    const dec = data.decision;
    const dv = el("decision-value");
    dv.textContent = dec.permission.replaceAll("_", " ");
    dv.className = "hero-value " + toneClass("decision", dec.permission);
    el("decision-sub").textContent = (dec.reasons && dec.reasons[dec.reasons.length - 1]) || "—";

    // ---- hero: entry score + checklist ----
    const e = data.entry || {};
    const esv = el("entry-score-value");
    esv.textContent = e.score != null ? `${e.score} / 5` : "—";
    esv.className = "hero-value " + toneClass("entry", e.score);
    const checklistEl = el("entry-checklist");
    checklistEl.innerHTML = "";
    const COMPONENT_LABELS = { ema_slope: "EMA Slope", proximity: "8-Bar Extreme", wick_reversal: "Wick Reversal",
      close_location: "Close Location", confirmation: "Confirmation" };
    Object.entries(e.components || {}).forEach(([key, val]) => {
      const row = document.createElement("div");
      row.className = "checklist-row";
      row.innerHTML = `<span>${COMPONENT_LABELS[key] || key}</span>
        <span class="checkmark ${val ? "yes" : "no"}">${val ? "✓" : "✗"}</span>`;
      checklistEl.appendChild(row);
    });

    // ---- Trend Engine detail rows ----
    const td = data.trend_detail || {};
    const persistence = data.vote_persistence || {};
    const persistLabel = (key) => persistence[key] ? `<span class="persist">Persistence: ${persistence[key].count}/${persistence[key].of}</span>` : "";
    const trendRows = [
      { name: "Aroon (14)", vote: t.votes && t.votes.aroon,
        detail: `${fmtNum(td.aroon_up)} / ${fmtNum(td.aroon_down)}${persistLabel("aroon")}` },
      { name: `EMA ${td.ema_fast_period ?? "?"}/${td.ema_slow_period ?? "?"}`, vote: t.votes && t.votes.ema_structure,
        detail: (td.ema_fast != null && td.ema_slow != null)
          ? `EMA${td.ema_fast_period ?? "?"} ${td.ema_fast > td.ema_slow ? "&gt;" : "&lt;"} EMA${td.ema_slow_period ?? "?"} `
            + `(${fmtPrice(td.ema_fast)} / ${fmtPrice(td.ema_slow)})`
          : "—" },
      { name: "VWAP", vote: t.votes && t.votes.vwap,
        detail: `Distance: ${fmtPts(td.vwap_distance_points)} pts` },
      { name: "DMI (14)", vote: t.votes && t.votes.dmi,
        detail: `+DI ${fmtNum(td.plus_di, 1)} &nbsp; -DI ${fmtNum(td.minus_di, 1)}` },
      { name: "ADX (14)", vote: null,
        detail: `${fmtNum(td.adx, 1)} ${t.adx_direction === "UP" ? "↑" : t.adx_direction === "DOWN" ? "↓" : "→"} ${t.strength || ""}` },
      { name: "Structure", vote: t.votes && t.votes.price_structure,
        detail: td.structure_label || "insufficient data" },
    ];
    const trendRowsEl = el("trend-rows");
    trendRowsEl.innerHTML = "";
    trendRows.forEach((r) => {
      const badgeClass = r.vote > 0 ? "good" : r.vote < 0 ? "bad" : "neutral";
      const badgeText = r.vote > 0 ? "BULLISH" : r.vote < 0 ? "BEARISH" : r.vote === 0 ? "NEUTRAL" : "";
      const row = document.createElement("div");
      row.className = "ind-row";
      row.innerHTML = `<div class="ind-name">${r.name}</div>
        <div>${badgeText ? `<span class="ind-badge ${badgeClass}">${badgeText}</span>` : ""}</div>
        <div class="ind-detail">${r.detail}</div>`;
      trendRowsEl.appendChild(row);
    });

    // ---- Location & Extension rows ----
    const loc = data.location || {};
    const levelsByName = {};
    ((data.key_levels || {}).levels || []).forEach((lv) => { levelsByName[lv.name] = lv; });
    const locRows = [
      ["Price vs VWAP", `${fmtPts(td.vwap_distance_points)} pts`],
      ["VWAP Distance %", td.vwap != null && td.vwap ? `${(Math.abs(td.vwap_distance_points) / td.vwap * 100).toFixed(2)}%` : "—"],
      [`Distance from EMA${td.ema_fast_period ?? "?"}`,
        (td.ema_fast != null && data.spot != null) ? `${fmtPts(data.spot - td.ema_fast)} pts` : "—"],
      ["5-Min ATR", td.atr != null ? `${td.atr.toFixed(1)} pts` : "—"],
      ["Extension Status", loc.extension || "—"],
      ["Trend Age", `${data.trend_age_bars != null ? data.trend_age_bars : "—"} bars`],
      ["Runway", loc.runway || "—"],
      ["Reward : Stop", loc.reward_to_stop != null ? `${loc.reward_to_stop.toFixed(2)} : 1` : "—"],
      ["Opening Range High", levelsByName["Opening Range High"] ? fmtPrice(levelsByName["Opening Range High"].price) : "—"],
      ["Opening Range Low", levelsByName["Opening Range Low"] ? fmtPrice(levelsByName["Opening Range Low"].price) : "—"],
      ["Prev Day High", levelsByName["Prev Day High"] ? fmtPrice(levelsByName["Prev Day High"].price) : "—"],
      ["Prev Day Low", levelsByName["Prev Day Low"] ? fmtPrice(levelsByName["Prev Day Low"].price) : "—"],
      ["Prev Day Close", levelsByName["Prev Day Close"] ? fmtPrice(levelsByName["Prev Day Close"].price) : "—"],
    ];
    const locRowsEl = el("location-rows");
    locRowsEl.innerHTML = "";
    locRows.forEach(([label, value]) => {
      const row = document.createElement("div");
      row.className = "kv-row";
      row.innerHTML = `<span class="kv-label">${label}</span><span class="kv-value">${value}</span>`;
      locRowsEl.appendChild(row);
    });

    // ---- Position Management ladder ----
    const health = data.position_health || {};
    const healthStates = Object.values(health).map((h) => h.state);
    // "worst" = latest in the ladder array that appears among tracked positions.
    const currentState = PM_LADDER.map((r) => r.state).reverse().find((s) => healthStates.includes(s)) || null;
    const pmRowsEl = el("pm-rows");
    pmRowsEl.innerHTML = "";
    PM_LADDER.forEach((r) => {
      const row = document.createElement("div");
      row.className = "pm-row" + (r.state === currentState ? " current" : "");
      row.innerHTML = `<div class="pm-icon">${r.icon}</div>
        <div><div class="pm-title" style="color:var(--${r.tone})">${r.label}</div>
          <ul class="pm-bullets">${r.bullets.map((b) => `<li>${b}</li>`).join("")}</ul></div>
        <div><div class="pm-action-label">Action</div>
          <div class="pm-action-value" style="color:var(--${r.tone})">${r.action}</div>
          <div class="pm-action-label" style="margin-top:2px;">${r.actionSub}</div></div>`;
      pmRowsEl.appendChild(row);
    });

    // ---- Key Levels ----
    const klRowsEl = el("key-level-rows");
    klRowsEl.innerHTML = "";
    el("twap-flag").style.display = data.vwap_is_twap_fallback ? "inline-block" : "none";
    ((data.key_levels || {}).levels || []).forEach((lv) => {
      const row = document.createElement("div");
      row.className = "kv-row";
      const cls = lv.distance_points >= 0 ? "pos" : "neg";
      row.innerHTML = `<span class="kv-label">${lv.name}</span><span class="kv-value ${cls}">${fmtPrice(lv.price)}</span>`;
      klRowsEl.appendChild(row);
    });

    // ---- Open Position ----
    const opEl = el("open-position");
    const positions = data.positions || [];
    if (!positions.length) {
      opEl.innerHTML = '<div class="empty-note">No open NIFTY positions.</div>';
    } else {
      opEl.innerHTML = "";
      positions.forEach((p) => {
        const direction = p.quantity > 0 ? "LONG" : "SHORT";
        const ltp = p.last_price;
        const entry = p.average_price;
        const pnlPts = (ltp != null && entry != null) ? (direction === "LONG" ? ltp - entry : entry - ltp) : null;
        const pnlPct = (pnlPts != null && entry) ? (pnlPts / entry * 100) : null;
        const lod = p.long_option_detail;
        const card = document.createElement("div");
        card.style.marginBottom = "12px";
        let html = `<div class="op-header"><span class="op-symbol">${p.tradingsymbol}</span>
          <span class="op-dir ${direction}">${direction}</span></div>
          <div class="owner-tag" style="margin-bottom:6px;">${p.owner}</div>
          <div class="op-grid">
            <span class="k">Qty</span><span class="v">${Math.abs(p.quantity)}</span>
            <span class="k">Entry Price</span><span class="v">${fmtPrice(entry)}</span>
            <span class="k">LTP</span><span class="v">${fmtPrice(ltp)}</span>
            <span class="k">P&amp;L (Pts)</span><span class="v ${pnlPts >= 0 ? "" : ""}" style="color:${pnlPts >= 0 ? "var(--good)" : "var(--bad)"}">${fmtPts(pnlPts)}</span>
            <span class="k">P&amp;L (%)</span><span class="v" style="color:${pnlPct >= 0 ? "var(--good)" : "var(--bad)"}">${pnlPct != null ? pnlPct.toFixed(2) + "%" : "—"}</span>
          </div>`;
        if (lod) {
          html += `<div style="display:flex; gap:8px; margin:10px 0;">
              <span class="t-chip ${lod.t1_hit ? "hit" : "pending"}">T1: ${lod.t1_label} ${lod.t1_hit ? "HIT" : "PENDING"}</span>
              <span class="t-chip ${lod.t2_hit ? "hit" : "pending"}">T2: ${lod.t2_label} ${lod.t2_hit ? "HIT" : "PENDING"}</span>
            </div>
            <div class="op-grid">
              <span class="k">Stop Loss</span><span class="v">${fmtPrice(lod.current_stop)}</span>
              <span class="k">Stage</span><span class="v">${(lod.trade_state || "—").replaceAll("_", " ")}</span>
            </div>`;
        }
        card.innerHTML = html;
        opEl.appendChild(card);
      });
    }

    // ---- Trend Summary ----
    const summaryEl = el("trend-summary");
    const summaryItems = [
      { icon: t.direction && t.direction.includes("BEAR") ? "↓" : "↑", label: "Direction", value: (t.direction || "—").split("_")[0] },
      { icon: "\u{1F4CA}", label: "Strength", value: t.strength || "—" },
      { icon: "⚡", label: "Momentum", value: t.momentum || "—" },
      { icon: "\u{1F30A}", label: "Volatility", value: t.volatility || "—" },
      { icon: data.market_tone === "POSITIVE" ? "\u{1F642}" : data.market_tone === "NEGATIVE" ? "☹️" : "\u{1F610}",
        label: "Market Tone", value: data.market_tone || "—" },
    ];
    summaryEl.innerHTML = summaryItems.map((s) => `<div class="summary-item">
        <div class="summary-icon">${s.icon}</div>
        <div class="summary-label">${s.label}</div>
        <div class="summary-value">${s.value}</div>
      </div>`).join("");

    // ---- Intraday Bias ----
    const bias = data.intraday_bias || {};
    const bv = el("bias-value");
    bv.textContent = bias.label || "—";
    bv.className = "bias-value " + (bias.label || "").replaceAll(" ", "_");
    el("bias-msg").textContent = bias.message || "—";

    // ---- Latest Signal ----
    const ls = data.latest_signal;
    el("latest-signal").innerHTML = ls
      ? `<div class="feed-row"><span class="feed-time">${fmtHM(ls.timestamp)}</span><br>${ls.message}</div>`
      : '<div class="empty-note">No signal changes yet this session.</div>';

    // ---- Upcoming Levels ----
    const upcoming = data.upcoming_levels || [];
    el("upcoming-levels").innerHTML = upcoming.length
      ? upcoming.map((lv) => `<div class="feed-row">${fmtPrice(lv.price)} &nbsp;-&nbsp; ${lv.name}</div>`).join("")
      : '<div class="empty-note">No nearby levels yet.</div>';

    // ---- Alerts ----
    const events = data.events || [];
    el("alerts-count").textContent = events.length ? `(${events.length})` : "";
    el("alerts-feed").innerHTML = events.length
      ? events.slice().reverse().map((ev) =>
          `<div class="feed-row"><span class="feed-time">${fmtHM(ev.timestamp)}</span> &nbsp;${ev.message}</div>`).join("")
      : '<div class="empty-note">No alerts yet this session.</div>';
  }

  function fmtNum(v, digits) {
    if (v == null) return "—";
    return v.toFixed(digits == null ? 0 : digits);
  }
