// LIFELOG Home frontend. Plain JS, no build step.
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const md = (s) => marked.parse(String(s ?? "").replace(/</g, "&lt;"));
const pct = (x, d = 0) => (x == null ? "–" : `${(x * 100).toFixed(d)}%`);
const hrs = (h) => (h == null ? "no limit" : h < 1 ? `${Math.max(1, Math.round(h * 60))} min` : h >= 48 ? `${(h / 24).toFixed(1)} days` : `${h.toFixed(1)} h`);
const temp = (t) => (t == null ? "–" : `${(+t).toFixed(1)}°C`);
// live trend (regression over the last 30 min of the real-time rollup) in one phrase
const trendText = (f) => {
  if (!f || f.trend === "unknown") return "";
  if (f.trend === "steady") return "steady";
  const arrow = f.trend === "warming" ? "↑" : "↓";
  const eta = f.minutes_to_limit != null ? ` · leaves its range in ~${hrs(f.minutes_to_limit / 60)}`
    : f.minutes_to_freeze != null ? ` · freezes in ~${hrs(f.minutes_to_freeze / 60)}` : "";
  return `${arrow} ${Math.abs(f.slope_c_per_h).toFixed(1)}°C/h${eta}`;
};
const fmt = (t) => new Date(t).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
const icon = (name) => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const isMobile = () => matchMedia("(max-width: 760px)").matches;

function toast(msg, kind = "") {
  const t = $("#toast"); t.textContent = msg; t.className = `toast show ${kind}`;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.className = "toast"), kind ? 6000 : 3500);
}
async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || `${r.status} ${r.statusText}`);
  return body;
}
const LANG_KEY = "lifelog.lang";
let lang = "en";
try { lang = localStorage.getItem(LANG_KEY) || "en"; } catch { /* storage blocked */ }
const withLang = (path) => path + (path.includes("?") ? "&" : "?") + "lang=" + lang;
const post = (path, data) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data ?? {}) });
async function busy(btn, fn) {
  if (btn) { btn.disabled = true; btn.classList.add("spin"); }
  try { return await fn(); } catch (e) { toast(e.message.includes("GEMINI_API_KEY") ? "Gemini isn't set up yet: add GEMINI_API_KEY to .env" : e.message, "warn"); console.error(e); }
  finally { if (btn) { btn.disabled = false; btn.classList.remove("spin"); } }
}
const aiTag = (r, label) => r && r.ai === false
  ? `<div class="tag offline">${icon("check")}From your data and the label · no AI</div>${r.notice ? `<p class="muted small">${esc(r.notice)}</p>` : ""}`
  : `<div class="tag">${icon("spark")}${label}</div>`;


const STATUS_COLOR = { USE: "--ok", USE_SOON: "--warn", CHECK: "--warn", ASK_PHARMACIST: "--warn", DO_NOT_USE: "--bad" };
function ring(value, size, stroke, code, label = "") {
  const r = (size - stroke) / 2, c = 2 * Math.PI * r, v = Math.max(0, Math.min(1, value ?? 0));
  return `<div class="ring" style="width:${size}px;height:${size}px" role="img" aria-label="${pct(v)} life budget left">
    <svg viewBox="0 0 ${size} ${size}"><circle class="track" cx="${size / 2}" cy="${size / 2}" r="${r}" stroke-width="${stroke}"/>
    <circle class="val" cx="${size / 2}" cy="${size / 2}" r="${r}" stroke-width="${stroke}" stroke="var(${STATUS_COLOR[code] || "--brand"})"
      stroke-dasharray="${c}" stroke-dashoffset="${c * (1 - v)}"/></svg>
    <div class="lbl" style="font-size:${Math.round(size / 4.2)}px">${pct(v)}${label}</div></div>`;
}

// ------------------------------------------------------------------ navigation
const TITLES = { meds: "My medicines", alerts: "Alerts", ask: "Ask LIFELOG", rescue: "Outage rescue", add: "Add medicine",
  voice: "Voice report", trip: "Trip check", porch: "Porch heat", tiger: "Under the hood", dev: "For developers", demo: "Demo controls" };
const loaders = {};
let currentView = "meds";
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-view]");
  if (b) { show(b.dataset.view); return; }
  if (e.target.closest("#more-btn")) $("#sheet").hidden = false;
  else if (["sheet", "help", "share-sheet"].includes(e.target.id) || e.target.closest("[data-close]")) { $("#sheet").hidden = true; $("#help").hidden = true; $("#share-sheet").hidden = true; }
});
function show(view) {
  currentView = view;
  $("#sheet").hidden = true;
  $$("[data-view]").forEach((b) => b.classList.toggle("active", b.dataset.view === view && !b.closest(".sheet-grid")));
  if (!$(`.tabbar [data-view="${view}"]`)) $("#more-btn").classList.add("active"); else $("#more-btn").classList.remove("active");
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${view}`));
  $("#page-title").textContent = TITLES[view] || "";
  setDetailOpen(view === "meds" && $("#view-meds").classList.contains("detail-open"));
  window.scrollTo(0, 0);
  loaders[view]?.();
}
function setDetailOpen(open) {
  $("#view-meds").classList.toggle("detail-open", open);
  $(".meds-layout").classList.toggle("detail-open", open);
  $("#back-btn").hidden = !(open && isMobile() && currentView === "meds");
}
$("#lang").value = lang;
$("#lang").addEventListener("change", (e) => {
  lang = e.target.value;
  try { localStorage.setItem(LANG_KEY, lang); } catch { /* storage blocked */ }
  toast(lang === "en" ? "Gemini will answer in English" : "Gemini will answer in " + e.target.selectedOptions[0].text);
});
$("#back-btn").addEventListener("click", () => { setDetailOpen(false); window.scrollTo(0, 0); });
$("#help-btn").addEventListener("click", () => {
  $("#help").hidden = false;
  $$("#help .ring-demo").forEach((el) => (el.innerHTML = ring(+el.dataset.pct / 100, 84, 9, "USE")));
});

async function health() {
  try {
    const h = await api("/api/health");
    const ext = h.extensions || {};
    $("#health").innerHTML = [["Tiger DB", h.database], [`TimescaleDB ${ext.timescaledb || ""}`, !!ext.timescaledb], ["pgvector", !!ext.vector],
      ["PostGIS", !!ext.postgis], ["Toolkit", !!ext.timescaledb_toolkit], ["Gemini", h.gemini]]
      .map(([k, on]) => `<span class="pill ${on ? "on" : "off"}">${esc(k)}</span>`).join("");
  } catch { $("#health").innerHTML = `<span class="pill off">API offline</span>`; }
}

// ------------------------------------------------------------------ medicines list
let currentItem = null, chart = null, myItems = [];
async function loadItems() {
  const items = await api("/api/items");
  myItems = items;
  const counts = items.reduce((a, i) => ((a[i.status.code] = (a[i.status.code] || 0) + 1), a), {});
  const attention = items.length - (counts.USE || 0);
  $("#meds-summary").innerHTML = !items.length ? "" : attention
    ? `<span class="pill USE">${counts.USE || 0} safe</span><span class="pill ${counts.DO_NOT_USE ? "DO_NOT_USE" : "USE_SOON"}">${attention} need${attention === 1 ? "s" : ""} attention</span>`
    : `<span class="pill USE">${icon("check")} All ${items.length} medicines are safe to use</span>`;
  $("#item-list").innerHTML = items.map((i) => `
    <button class="med ${i.id === currentItem ? "active" : ""}" data-id="${i.id}">
      ${ring(i.remaining, 56, 6, i.status.code)}
      <div><div class="name">${esc(i.nickname)}</div>
        <div class="sub">${i.stale_minutes > 20 ? `last ${temp(i.current_temp)} · sensor quiet ${hrs(i.stale_minutes / 60)}` : `${temp(i.current_temp)} · ${esc(i.zone.toLowerCase())}`}</div>
        ${i.forecast && ["warming", "cooling"].includes(i.forecast.trend) ? `<div class="sub trend ${i.forecast.minutes_to_limit != null || i.forecast.minutes_to_freeze != null ? "bad" : ""}">${esc(trendText(i.forecast))}</div>` : ""}
        <span class="pill ${i.status.code}">${esc(i.status.label)}</span></div>
    </button>`).join("") || `<div class="card"><p>No medicines yet.</p><p class="muted">Add one from its label, or open Demo controls and press Reset.</p></div>`;
  $$(".med").forEach((el) => el.addEventListener("click", () => { openItem(+el.dataset.id); }));
  for (const sel of ["#voice-item", "#trip-item"]) {
    const keep = $(sel).value;
    $(sel).innerHTML = items.map((i) => `<option value="${i.id}">${esc(i.nickname)}</option>`).join("");
    if (keep) $(sel).value = keep;
  }
  if (!currentItem && items.length && !isMobile()) openItem(items[0].id);
  return items;
}
loaders.meds = () => loadItems().then(() => currentItem && !isMobile() && openItem(currentItem, { quiet: true }));

// ------------------------------------------------------------------ medicine detail
async function openItem(id, { quiet = false } = {}) {
  const same = id === currentItem;
  const keepAi = same ? $("#ai-box")?.innerHTML : "";
  const openFolds = same ? $$("#item-detail details.fold[open]").map((d) => d.dataset.k) : ["care"];
  currentItem = id;
  $$(".med").forEach((el) => el.classList.toggle("active", +el.dataset.id === id));
  const d = await api(`/api/items/${id}`);
  const s = d.state, m = d.item.model, st = d.status;
  const uncertain = d.worst_case < s.remaining - 0.01;
  const urgent = d.precautions.filter((p) => p.level !== "info");
  const top = urgent[0];
  const maxBurn = Math.max(...d.burners.map((b) => b.burn), 0.0001);

  $("#item-detail").innerHTML = `
    <div class="card">
      <div class="hero">
        ${ring(s.remaining, 132, 12, st.code, "<small>budget left</small>")}
        <div>
          <h2>${esc(d.item.nickname)}</h2>
          <div class="muted small">${esc(d.item.product_name)}</div>
          <div class="status"><span class="pill ${st.code}">${esc(st.label)}</span><span class="why">${esc(st.why)}</span></div>
          <div class="facts">
            <span>${s.stale_minutes > 20 ? "Last reading" : "Now"} <b>${temp(s.current_temp)}</b></span>
            <span>Allowed <b>${m.target_min_c}–${m.target_max_c}°C</b></span>
            <span>At this temperature it lasts <b>${hrs(s.hours_left)}</b></span>
            ${s.stale_minutes <= 20 && d.forecast && d.forecast.trend !== "unknown" ? `<span>Trend <b class="trend ${d.forecast.minutes_to_limit != null || d.forecast.minutes_to_freeze != null ? "bad" : ""}">${esc(trendText(d.forecast))}</b></span>` : ""}
            ${s.stale_minutes <= 20 && d.forecast && ["warming", "cooling"].includes(d.forecast.trend) ? `<span>At this trend it lasts <b>${hrs(d.forecast.hours_left)}</b></span>` : ""}
            ${s.stale_minutes <= 20 ? `<span>Used in the last hour <b>${d.burn_last_hour > 0.00005 ? pct(d.burn_last_hour, 2) : "none"}</b></span>` : ""}
            ${d.dates.use_by ? `<span>Use by <b>${new Date(d.dates.use_by_date + "T12:00:00").toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" })}</b> <span class="muted">(${esc(d.dates.use_by_reason)})</span></span>` : ""}
            <button class="btn" id="dates-btn" style="min-height:30px;padding:3px 10px;font-size:13px">Dates &amp; lot</button>
          </div>
          <form id="dates-form" class="dates-form" hidden>
            <label class="small">Opened / first used<input type="date" id="f-opened" value="${d.dates.opened_at ? d.dates.opened_at.slice(0, 10) : ""}"></label>
            <label class="small">Expiration date<input type="date" id="f-expires" value="${d.dates.expires_on || ""}"></label>
            <label class="small">Lot number<input type="text" id="f-lot" value="${esc(d.item.lot || "")}" placeholder="from the box"></label>
            <button class="btn primary" type="submit">Save</button>
            <p class="muted small" style="grid-column:1/-1">${m.in_use_days ? `The label allows ${Math.round(m.in_use_days)} days after opening, whatever the temperature.` : "This label gives no in-use limit after opening."} The lot number is checked against FDA recalls.</p>
          </form>
          ${uncertain ? `<div class="range-note">The sensor missed ${hrs(d.gaps.reduce((a, g) => a + g.hours, 0))} of data, so the real budget could be as low as <b>${pct(d.worst_case)}</b> (if it sat at room temperature).</div>` : ""}
        </div>
      </div>
      ${top ? `<div class="todo ${top.level}">${icon("alert")}<div><b>Do this now</b><span>${esc(top.text)}</span></div></div>`
        : `<div class="todo ok">${icon("check")}<div><b>Nothing to do</b><span>Keep storing it the way you are.</span></div></div>`}
      <div class="actions">
        <button class="btn ai" id="advice-btn">${icon("spark")}Is it safe to use?</button>
        <label class="btn" for="photo" title="Needs Gemini">${icon("camera")}Photo check</label><input type="file" id="photo" accept="image/*" capture="environment" hidden>
        <button class="btn" id="letter-btn">${icon("file")}Refill letter</button>
        <button class="btn" id="receipt-btn" title="A verifiable record a pharmacist can check">${icon("check")}Exposure receipt</button>
        ${"bluetooth" in navigator ? `<button class="btn" id="ble-btn" title="Bluetooth thermometer (Health Thermometer or Environmental Sensing)">${icon("bolt")}Connect sensor</button>` : ""}
        <a class="btn" href="/api/items/${id}/export.csv">${icon("download")}Export log</a>
        <button class="btn" onclick="window.print()">${icon("print")}Print</button>
      </div>
      <div id="ai-box"></div>
    </div>

    <div class="card" style="margin-top:14px">
      <details class="fold" data-k="care" ${openFolds.includes("care") ? "open" : ""}>
        <summary>Care checklist <span class="muted">${urgent.length ? `${urgent.length} need${urgent.length === 1 ? "s" : ""} attention` : "from the label"}</span></summary>
        <ul class="checklist">${d.precautions.map((p) => `<li class="${p.level}">${icon(p.level === "info" ? "check" : "alert")}<span>${esc(p.text)}</span></li>`).join("")}</ul>
      </details>
      <details class="fold" data-k="chart" ${openFolds.includes("chart") || !isMobile() ? "open" : ""}>
        <summary>Last 3 days <span class="muted">temperature and life budget</span></summary>
        <div class="chart-box"><canvas id="chart" aria-label="Temperature and life budget over time"></canvas></div>
        <p class="legend-note">The <b style="color:var(--chart-temp)">green line</b> is the temperature, and its dotted end is the live forecast for the next 2 hours${d.forecast?.model ? ` (${esc(d.forecast.model)}, from the last 30 minutes of readings)` : ""}; the dashed line is the label's maximum. The <b style="color:var(--chart-budget)">purple line</b> is the life budget: it only goes down, and only while it's outside the label's range.</p>
      </details>
      <details class="fold" data-k="burn" ${openFolds.includes("burn") ? "open" : ""}>
        <summary>What used the budget <span class="muted">${d.burners.length ? `${d.burners.length} episode${d.burners.length === 1 ? "" : "s"}` : "nothing yet"}</span></summary>
        ${d.burners.length ? d.burners.map((b) => `<div class="burn-row"><div>${esc(b.zone)}<div class="muted small">${fmt(b.started)} · ${hrs(b.minutes / 60)} · peak ${temp(b.peak_temp)}</div></div>
          <div class="burn-bar" style="width:${(b.burn / maxBurn) * 100}%"></div><div class="num">−${pct(b.burn, 1)}</div></div>`).join("") : `<p class="muted">It has stayed within the label's limits.</p>`}
      </details>
      <details class="fold" data-k="history" ${openFolds.includes("history") ? "open" : ""}>
        <summary>Last 30 days <span class="muted">daily exposure calendar</span></summary>
        <div id="history-body" class="muted">Loading…</div>
      </details>
      <details class="fold" data-k="recalls" ${openFolds.includes("recalls") ? "open" : ""}>
        <summary>FDA recalls <span class="muted">live from openFDA</span></summary>
        <div id="recalls-body" class="muted">Loading…</div>
      </details>
      <details class="fold" data-k="whatif" ${openFolds.includes("whatif") ? "open" : ""}>
        <summary>What if you act now? <span class="muted">budget left after 12 hours</span></summary>
        ${s.zone === "Labeled storage" ? `<p class="small">${icon("check")} It's already in its labeled storage, so there's nothing to change. The table shows what moving it would cost.</p>` : ""}
        <div class="table-wrap"><table><tr><th>If you…</th><th class="num">Temp</th><th class="num">After 12 h</th><th class="num">Lasts</th></tr>
        ${d.whatif.map((w, i) => `<tr class="${i === 0 && w.preserved_vs_now > 0 ? "best" : ""}"><td>${esc(w.option)}</td><td class="num">${temp(w.temp_c)}</td><td class="num">${pct(w.remaining_after)}</td><td class="num">${hrs(w.hours_left)}</td></tr>`).join("")}</table></div>
      </details>
      <details class="fold" data-k="stats" ${openFolds.includes("stats") ? "open" : ""}>
        <summary>Exposure statistics <span class="muted">mean kinetic temperature, time in each zone</span></summary>
        <div class="stats">
          <div class="stat"><div class="v">${temp(d.stats.mkt_c)}</div><div class="k">mean kinetic temperature${d.stats.mkt_c > m.target_max_c ? " · above label max" : ""}</div></div>
          <div class="stat"><div class="v">${temp(d.stats.time_weighted_avg_c)}</div><div class="k">time-weighted average</div></div>
          <div class="stat"><div class="v">${temp(d.stats.min_c)} / ${temp(d.stats.max_c)}</div><div class="k">coldest / hottest</div></div>
        </div>
        <div class="table-wrap"><table><tr><th>Zone</th><th class="num">Time</th><th class="num">Budget used</th></tr>
        ${d.zones.map((z) => `<tr><td>${esc(z.zone)}</td><td class="num">${hrs(z.minutes / 60)}</td><td class="num">${z.burn > 0.0005 ? pct(z.burn, 1) : "–"}</td></tr>`).join("")}</table></div>
        <p class="muted small">Mean kinetic temperature is the pharmaceutical standard (USP &lt;1079&gt;) for cumulative heat stress: hot spells count more than a plain average.</p>
      </details>
      <details class="fold" data-k="accuracy" ${openFolds.includes("accuracy") ? "open" : ""}>
        <summary>How accurate is this? <span class="muted">cross-checks and assumptions</span></summary>
        <div id="accuracy-body" class="muted">Loading…</div>
      </details>
      <details class="fold" data-k="patterns" ${openFolds.includes("patterns") ? "open" : ""}>
        <summary>Similar histories <span class="muted">what usually happens next</span></summary>
        ${d.patterns.map((p) => `<p><b>${esc(p.name)}</b> <span class="muted small">${Math.round(p.similarity * 100)}% similar</span><br><span class="muted small">${esc(p.outcome)}</span></p>`).join("")}
      </details>
      <details class="fold" data-k="label" ${openFolds.includes("label") ? "open" : ""}>
        <summary>Label rules <span class="muted">${d.item.product_source === "fda" ? "from the FDA label" : d.item.product_source === "gemini" ? "read by Gemini" : "demo product"}</span></summary>
        ${d.item.source_url ? `<p class="source">${icon("file")}<a href="${esc(d.item.source_url)}" target="_blank" rel="noopener">${esc(d.item.source_label || "Source")}</a></p>` : ""}
        <blockquote>${esc(m.target_quote)}</blockquote>
        ${(m.bands || []).map((b) => `<blockquote>${esc(b.quote)}</blockquote>`).join("")}
        ${m.freeze_discard ? `<blockquote>${esc(m.freeze_quote)}</blockquote>` : ""}
        ${(m.discard_rules || []).map((r) => `<blockquote>${esc(r)}</blockquote>`).join("")}
        <p class="muted small">Above the highest labeled limit: ${m.above_limit_budget_hours} h of tolerance, halving for every 10°C hotter${m.above_limit_is_assumption ? " (LIFELOG's assumption: the label doesn't say)" : ""}.${m.notes ? " " + esc(m.notes) : ""}</p>
      </details>
    </div>`;

  if (keepAi) $("#ai-box").innerHTML = keepAi;
  if ($("details[data-k=chart]").open) drawChart(d);
  $("details[data-k=chart]").addEventListener("toggle", (e) => e.target.open && drawChart(d));
  for (const [k, fn] of [["history", loadHistory], ["recalls", loadRecalls]]) {
    const el = $(`details[data-k=${k}]`);
    if (el.open) fn(id);
    el.addEventListener("toggle", () => el.open && fn(id));
  }
  $("#dates-btn").onclick = () => { $("#dates-form").hidden = !$("#dates-form").hidden; };
  $("#dates-form").onsubmit = (e) => {
    e.preventDefault();
    busy(e.submitter, async () => {
      const opened = $("#f-opened").value;
      await api(`/api/items/${id}`, { method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ opened_at: opened ? new Date(opened + "T12:00:00").toISOString() : null, expires_on: $("#f-expires").value || null, lot: $("#f-lot").value }) });
      toast("Saved"); await loadItems(); openItem(id, { quiet: true }); refreshBadge();
    });
  };
  const acc = $("details[data-k=accuracy]");
  if (acc.open) loadAccuracy(id);
  acc.addEventListener("toggle", () => acc.open && loadAccuracy(id));
  $("#advice-btn").onclick = (e) => busy(e.currentTarget, async () => renderVerdict(await post(withLang(`/api/items/${id}/advice`))));
  $("#photo").onchange = async (e) => {
    const f = e.target.files[0]; if (!f) return;
    const fd = new FormData(); fd.append("photo", f);
    await busy($("label[for=photo]"), async () => {
      const v = await api(withLang(`/api/items/${id}/visual`), { method: "POST", body: fd });
      $("#ai-box").innerHTML = `<div class="ai-box"><div class="tag">${icon("spark")}Gemini photo check</div>
        <div class="verdict ${v.looks_ok ? "USE" : "DO_NOT_USE"}">${v.looks_ok ? "No visible warning signs" : "Visible warning sign"}</div>
        <ul>${v.observations.map((o) => `<li>${esc(o)}</li>`).join("")}</ul>
        ${v.matched_label_warnings.map((q) => `<blockquote>${esc(q)}</blockquote>`).join("")}
        <p class="muted small">Confidence: ${esc(v.confidence)}. A photo can't prove a medicine is safe. If in doubt, ask your pharmacist.</p></div>`;
    });
  };
  $("#letter-btn").onclick = (e) => busy(e.currentTarget, async () => {
    const r = await post(withLang(`/api/items/${id}/letter`));
    $("#ai-box").innerHTML = `<div class="ai-box">${aiTag(r, "Refill / replacement letter (Gemini)")}<pre class="log">${esc(r.letter.replace("/r/" + (r.receipt?.code || "x"), location.origin + "/r/" + (r.receipt?.code || "x")))}</pre>
      <div class="row"><button class="btn" id="copy-letter">Copy</button>${r.receipt ? `<a class="btn" href="/r/${esc(r.receipt.code)}" target="_blank">Open receipt ${esc(r.receipt.code)}</a>` : ""}</div></div>`;
    $("#copy-letter").onclick = () => navigator.clipboard.writeText(r.letter).then(() => toast("Copied"));
  });
  $("#receipt-btn").onclick = (e) => busy(e.currentTarget, async () => {
    const rc = await post(`/api/items/${id}/receipt`);
    const url = `${location.origin}/r/${rc.code}`;
    $("#ai-box").innerHTML = `<div class="ai-box"><div class="tag">${icon("check")}Exposure receipt ${esc(rc.code)}</div>
      <p class="small">A frozen, fingerprinted copy of this medicine's exposure record up to ${fmt(rc.as_of)}. A pharmacist or insurer can scan it to see the record and whether the data still matches.</p>
      <div id="rc-qr" class="qr"></div><div class="link-box">${esc(url)}</div>
      <div class="row"><button class="btn primary" id="rc-copy">Copy link</button><a class="btn" href="/r/${esc(rc.code)}" target="_blank">Open</a></div></div>`;
    if (window.QRCode) new QRCode($("#rc-qr"), { text: url, width: 150, height: 150 });
    $("#rc-copy").onclick = () => navigator.clipboard.writeText(url).then(() => toast("Copied"));
  });
  $("#ble-btn")?.addEventListener("click", () => connectBle(id));
  if (!quiet) { setDetailOpen(true); if (isMobile()) window.scrollTo(0, 0); }
}

function renderVerdict(v) {
  $("#ai-box").innerHTML = `<div class="ai-box" dir="auto">${aiTag(v, "Gemini · based on your data and the label")}
    <div class="verdict ${esc(v.verdict)}">${esc(v.headline)}</div><p>${esc(v.explanation)}</p>
    <ul>${v.actions.map((a) => `<li>${esc(a)}</li>`).join("")}</ul>${v.cited_quotes.map((q) => `<blockquote>${esc(q)}</blockquote>`).join("")}</div>`;
}

async function loadHistory(id) {
  const rows = await api(`/api/items/${id}/history?days=30`);
  const byDay = new Map(rows.map((r) => [r.day.slice(0, 10), r]));
  const today = new Date(); const cells = [];
  for (let i = 29; i >= 0; i--) {
    const d = new Date(today); d.setUTCDate(d.getUTCDate() - i);
    const key = d.toISOString().slice(0, 10), r = byDay.get(key);
    const cls = !r ? "none" : r.burn >= 0.2 ? "b3" : r.burn >= 0.02 ? "b2" : r.burn >= 0.002 ? "b1" : "";
    const tip = r ? `${key}: avg ${temp(r.avg_temp)}, peak ${temp(r.max_temp)}, used ${pct(r.burn, 1)} of the budget` : `${key}: no data`;
    cells.push(`<div class="${cls}" title="${esc(tip)}" aria-label="${esc(tip)}">${+key.slice(8)}</div>`);
  }
  const worst = rows.reduce((a, r) => (r.burn > (a?.burn ?? -1) ? r : a), null);
  $("#history-body").classList.remove("muted");
  $("#history-body").innerHTML = `<div class="cal">${cells.join("")}</div>
    <div class="cal-legend"><span><i style="background:var(--surface-2)"></i>none</span><span><i style="background:color-mix(in srgb,var(--warn) 25%,var(--surface-2))"></i>small</span>
      <span><i style="background:color-mix(in srgb,var(--warn) 55%,var(--surface-2))"></i>noticeable (2%+)</span><span><i style="background:var(--bad)"></i>major (20%+)</span></div>
    ${worst && worst.burn >= 0.002 ? `<p class="small">Biggest day: <b>${new Date(worst.day).toLocaleDateString([], { month: "short", day: "numeric", timeZone: "UTC" })}</b>, peak ${temp(worst.max_temp)}, used ${pct(worst.burn, 1)}.</p>` : `<p class="small">No meaningful exposure in the last 30 days.</p>`}
    <p class="muted small">From a daily rollup built on the 5-minute rollup in Tiger Data (a hierarchical continuous aggregate).</p>`;
}

async function loadRecalls(id) {
  const r = await api(`/api/items/${id}/recalls`);
  $("#recalls-body").classList.remove("muted");
  if (r.error) { $("#recalls-body").innerHTML = `<p class="small">Couldn't reach openFDA right now (${esc(r.error)}).</p>`; return; }
  const ongoing = r.recalls.filter((x) => x.status === "Ongoing");
  $("#recalls-body").innerHTML = (r.active_lot_match ? `<div class="banner bad"><b>An ongoing FDA recall lists your lot number.</b> Stop using it and contact your pharmacy.</div>` : "")
    + (!r.total ? `<p class="small">${icon("check")} No FDA recalls on record for ${esc(r.brand)}.</p>`
    : `<p class="small">${r.total} FDA recall${r.total === 1 ? "" : "s"} on record for ${esc(r.brand)}${ongoing.length ? `, <b>${ongoing.length} ongoing</b>` : ", none ongoing"}.
        ${r.recalls.some((x) => x.temperature_related) ? "Some were for temperature problems, the exact risk LIFELOG watches." : ""}</p>`
      + r.recalls.slice(0, 5).map((x) => `<div class="recall ${x.lot_match ? "match" : ""}"><b>${esc(x.report_date)}</b> · ${esc(x.classification)} · ${esc(x.status)}${x.lot_match ? " · <b>your lot</b>" : ""}
          <div>${esc(x.reason)}</div><div class="muted small">Lots: ${esc(x.lots || "not listed")}</div></div>`).join(""))
    + `<p class="muted small">Source: openFDA drug enforcement reports. ${esc(r.brand)} matched by brand name${r.recalls.length ? "; add your lot number under Dates & lot to check it" : ""}.</p>`;
}

async function loadAccuracy(id) {
  const a = await api(`/api/items/${id}/accuracy`);
  const mth = a.methods;
  const agree = a.max_disagreement < 0.0005;
  const sens = a.sensitivity;
  $("#accuracy-body").classList.remove("muted");
  $("#accuracy-body").innerHTML = `
    <p>${agree ? `${icon("check")} <b>Three independent calculations agree</b>` : `<b>Calculations disagree by ${pct(a.max_disagreement, 2)}</b>`}: the database's live rollup, a recomputation from every raw reading in SQL, and a separate Python implementation.</p>
    <div class="table-wrap"><table>
      <tr><th>Method</th><th class="num">Budget left</th></tr>
      <tr><td>Live rollup (what you see)</td><td class="num">${pct(mth.aggregate_exact, 2)}</td></tr>
      <tr><td>Recomputed from ${a.readings.toLocaleString()} raw readings (SQL)</td><td class="num">${pct(mth.raw_exact_sql, 2)}</td></tr>
      <tr><td>Independent Python recomputation</td><td class="num">${pct(mth.python_independent, 2)}</td></tr>
      <tr><td class="muted">Old method: averaging each 5 minutes first</td><td class="num muted">${pct(mth.aggregate_bucket_average, 2)}</td></tr>
    </table></div>
    <p class="small muted">${Math.abs(a.averaging_error) < 0.00005 ? "Averaging each 5 minutes first would give the same answer here." : `Averaging each 5 minutes first would ${a.averaging_error > 0 ? "overstate" : "understate"} the budget by ${pct(Math.abs(a.averaging_error), 2)}.`} LIFELOG scores every reading instead: damage jumps at the label's limits and grows exponentially above them, so an average hides short spikes and smears readings that straddle a limit.</p>
    <h3>What the answer depends on</h3>
    ${a.assumption_on_label ? `<p>The heat tolerance comes from the label itself.</p>`
      : new Set(sens.map((x) => Math.round(x.remaining * 100))).size === 1
        ? `<p>The label doesn't say how long it tolerates heat above its limit (LIFELOG assumes 8 hours), but that doesn't matter here: this medicine has never been above its labeled limit.</p>`
        : `<p>The label doesn't say how long it tolerates heat above its limit; LIFELOG assumes <b>8 hours</b>. Here is how the answer would change:</p>`}
    <div class="table-wrap"><table><tr><th>If the tolerance were</th>${sens.map((x) => `<th class="num">${x.above_limit_budget_hours} h</th>`).join("")}</tr>
      <tr><td>Budget left</td>${sens.map((x) => `<td class="num">${pct(x.remaining)}</td>`).join("")}</tr></table></div>
    <h3>Data quality</h3>
    <p class="small">${a.readings.toLocaleString()} readings on record, ${(+a.ingest.accepted).toLocaleString()} of them sent live through the ingest API · <b>${a.ingest.rejected}</b> rejected (impossible values or duplicates) · <b>${a.ingest.flagged}</b> flagged as sudden jumps · ${a.ingest.late_batches} late batch${a.ingest.late_batches == 1 ? "" : "es"} repaired.
    ${a.gaps.length ? `${a.gaps.length} gap${a.gaps.length === 1 ? "" : "s"} in the data (${hrs(a.gaps.reduce((s, g) => s + g.hours, 0))}), worst case ${pct(a.worst_case)}.` : "No gaps in the data."}</p>`;
}

function drawChart(d) {
  chart?.destroy();
  const ct = cssVar("--chart-temp"), cb = cssVar("--chart-budget"), muted = cssVar("--muted"), border = cssVar("--border");
  const m = d.item.model, pts = d.timeline;
  // forecast starts at the last reading; hidden while the sensor is quiet (no live trend)
  const fresh = d.state.stale_minutes != null && d.state.stale_minutes <= 20;
  const fc = fresh && pts.length ? (d.forecast?.path || []) : [];
  const t0 = pts.length ? new Date(pts.at(-1).t).getTime() : Date.now();
  const fcEnd = fc.length ? t0 + fc.at(-1).minutes * 60e3 : (pts.length ? new Date(pts.at(-1).t).getTime() : Date.now());
  const withGaps = (key, scale = 1) => {
    const out = [];
    pts.forEach((p, i) => {
      if (i && new Date(p.t) - new Date(pts[i - 1].t) > 20 * 60e3) out.push({ x: new Date(new Date(pts[i - 1].t).getTime() + 5 * 60e3), y: null });
      out.push({ x: new Date(p.t), y: p[key] == null ? null : p[key] * scale });
    });
    return out;
  };
  chart = new Chart($("#chart"), {
    type: "line",
    data: { datasets: [
      { label: "Temperature °C", data: withGaps("temp"), borderColor: ct, borderWidth: 1.5, pointRadius: 0, yAxisID: "t", spanGaps: false },
      { label: "Life budget %", data: withGaps("remaining", 100), borderColor: cb, borderWidth: 2.5, pointRadius: 0, yAxisID: "b", spanGaps: false },
      { label: `Label max ${m.target_max_c}°C`, data: pts.length ? [{ x: new Date(pts[0].t), y: m.target_max_c }, { x: new Date(fcEnd), y: m.target_max_c }] : [],
        borderColor: muted, borderDash: [5, 5], borderWidth: 1, pointRadius: 0, yAxisID: "t" },
      { label: "Forecast °C (next 2 h)", data: fc.map((p) => ({ x: new Date(t0 + p.minutes * 60e3), y: p.temp_c })),
        borderColor: ct, borderDash: [2, 3], borderWidth: 2, pointRadius: 0, yAxisID: "t" },
    ] },
    options: {
      maintainAspectRatio: false, animation: false, interaction: { mode: "index", intersect: false },
      scales: {
        x: { type: "time", grid: { color: border }, ticks: { color: muted, maxTicksLimit: isMobile() ? 4 : 8 } },
        t: { position: "left", title: { display: !isMobile(), text: "°C", color: muted }, grid: { color: border }, ticks: { color: muted } },
        b: { position: "right", min: 0, max: 100, title: { display: !isMobile(), text: "budget %", color: muted }, grid: { display: false }, ticks: { color: muted } },
      },
      plugins: { legend: { display: !isMobile(), labels: { color: muted, boxWidth: 12 } } },
    },
  });
}

// ------------------------------------------------------------------ add medicine
$("#label-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const f = $("#label-file").files[0]; if (!f) return;
  const fd = new FormData(); fd.append("label", f);
  busy(e.submitter, async () => {
    const m = await api("/api/products/extract", { method: "POST", body: fd });
    renderModel(m, { tag: "Gemini read this from your label" });
  });
});
$("#name-form").addEventListener("submit", (e) => {
  e.preventDefault();
  busy(e.submitter, async () => {
    const r = await post("/api/products/lookup", { name: $("#med-name").value });
    renderModel(r.model, {
      tag: r.method === "openfda" ? "From the official FDA label (openFDA), read by Gemini"
        : r.method === "openfda_rules" ? "From the official FDA label (openFDA), read by LIFELOG's rules (no AI): check every number"
        : "Found by Gemini web search (Google Search grounding)",
      source_label: r.source_label, source_url: r.source_url, sources: r.sources, label_text: r.label_text, notice: r.notice,
    });
  });
});

function renderModel(m, meta) {
    $("#label-result").innerHTML = `<div class="ai-box"><div class="tag">${icon("spark")}${esc(meta.tag)}</div>${meta.notice ? `<p class="muted small">${esc(meta.notice)}</p>` : ""}
      ${meta.source_url ? `<p class="source">${icon("file")}<a href="${esc(meta.source_url)}" target="_blank" rel="noopener">${esc(meta.source_label)}</a></p>` : ""}
      ${(meta.sources || []).length > 1 ? `<p class="muted small">Other sources: ${meta.sources.slice(1, 4).map((s) => `<a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.title || s.url)}</a>`).join(" · ")}</p>` : ""}
      ${meta.label_text ? `<details class="fold"><summary>Label text it read <span class="muted">check this matches your medicine</span></summary><div class="label-text">${esc(meta.label_text)}</div></details>` : ""}
      <h3>${esc(m.product_name)} <span class="muted small">${esc(m.form)}</span></h3>
      <p>Store at <b>${m.target_min_c}–${m.target_max_c}°C</b></p><blockquote>${esc(m.target_quote)}</blockquote>
      ${m.bands.map((b) => `<p><b>${esc(b.label)}</b>: ${b.min_c}–${b.max_c}°C for up to ${hrs(b.budget_hours)}</p><blockquote>${esc(b.quote)}</blockquote>`).join("")}
      <p>Freezing: ${m.freeze_discard ? `don't use if it reaches ${m.freeze_c}°C` : "not restricted"}</p><blockquote>${esc(m.freeze_quote)}</blockquote>
      <p>Heat beyond the label's limit: ${m.above_limit_budget_hours} h ${m.above_limit_is_assumption ? '<span class="pill">LIFELOG assumption</span>' : ""}</p>
      ${m.visual_checks.length ? `<p>Check before use:</p><ul>${m.visual_checks.map((v) => `<li>${esc(v)}</li>`).join("")}</ul>` : ""}
      ${m.discard_rules.length ? `<p>Throw away:</p><ul>${m.discard_rules.map((v) => `<li>${esc(v)}</li>`).join("")}</ul>` : ""}
      <details class="fold"><summary>Edit the raw rules (JSON)</summary><textarea id="model-json" rows="12">${esc(JSON.stringify(m, null, 2))}</textarea></details>
      <label class="field">Give it a name <input type="text" id="nick" value="${esc(m.product_name)}"></label>
      <button id="save-model" class="btn primary">Start tracking</button></div>`;
    $("#save-model").onclick = (ev) => busy(ev.currentTarget, async () => {
      let model;
      try { model = JSON.parse($("#model-json").value); } catch { throw new Error("The JSON has a typo. Fix it or reload the label."); }
      const r = await post("/api/products", { model, nickname: $("#nick").value, source_label: meta.source_label || null, source_url: meta.source_url || null });
      toast("Added. Connect a sensor or send a voice report."); currentItem = r.item_id; show("meds"); openItem(r.item_id);
    });
}

// ------------------------------------------------------------------ voice (WAV recorder)
let rec = null;
$("#rec-btn").addEventListener("click", async () => {
  if (rec) {
    const blob = await rec.stop(); rec = null;
    $("#rec-btn").innerHTML = `${icon("mic")}Start recording`; $("#rec-status").textContent = `${(blob.size / 1024).toFixed(0)} KB recorded`;
    return sendVoice(blob);
  }
  try { rec = await startWav(); $("#rec-btn").innerHTML = `${icon("mic")}Stop and send`; $("#rec-status").textContent = "Recording…"; }
  catch (e) { toast("Microphone unavailable: " + e.message); }
});
$("#voice-send").addEventListener("click", () => sendVoice(null));
async function sendVoice(blob) {
  const fd = new FormData();
  fd.append("item_id", $("#voice-item").value);
  if (blob) fd.append("audio", blob, "report.wav"); else fd.append("text", $("#voice-text").value);
  await busy($("#voice-send"), async () => {
    const r = await api("/api/voice", { method: "POST", body: fd });
    $("#voice-result").innerHTML = `<div class="ai-box">${aiTag(r.report, "Gemini heard")}<p>"${esc(r.report.transcript)}"</p>
      <div class="table-wrap"><table><tr><th>What happened</th><th class="num">When</th><th class="num">How long</th><th class="num">About</th></tr>
      ${r.report.events.map((e) => `<tr><td>${esc(e.summary)}</td><td class="num">${e.minutes_ago_start.toFixed(0)} min ago</td><td class="num">${e.duration_minutes.toFixed(0)} min</td><td class="num">${temp(e.estimated_temp_c)}</td></tr>`).join("")}</table></div>
      <p class="muted small">${r.ingest.inserted} readings added for "${esc(r.item)}".</p>
      <button class="btn" data-view="meds">See the updated budget</button></div>`;
  });
}
async function startWav() {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  const ctx = new AudioContext(); const src = ctx.createMediaStreamSource(stream);
  const proc = ctx.createScriptProcessor(4096, 1, 1); const chunks = [];
  proc.onaudioprocess = (e) => chunks.push(new Float32Array(e.inputBuffer.getChannelData(0)));
  src.connect(proc); proc.connect(ctx.destination);
  return {
    async stop() {
      proc.disconnect(); src.disconnect(); stream.getTracks().forEach((t) => t.stop());
      const rate = ctx.sampleRate; await ctx.close();
      const len = chunks.reduce((a, c) => a + c.length, 0);
      const buf = new ArrayBuffer(44 + len * 2); const v = new DataView(buf);
      const w = (o, s) => [...s].forEach((c, i) => v.setUint8(o + i, c.charCodeAt(0)));
      w(0, "RIFF"); v.setUint32(4, 36 + len * 2, true); w(8, "WAVE"); w(12, "fmt ");
      v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, rate, true);
      v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true); w(36, "data"); v.setUint32(40, len * 2, true);
      let o = 44; for (const c of chunks) for (const s of c) { v.setInt16(o, Math.max(-1, Math.min(1, s)) * 0x7fff, true); o += 2; }
      return new Blob([buf], { type: "audio/wav" });
    },
  };
}

// ------------------------------------------------------------------ maps
function baseMap(el, center, zoom) {
  const map = L.map(el, { scrollWheelZoom: !isMobile() }).setView(center, zoom);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { attribution: "© OpenStreetMap" }).addTo(map);
  return map;
}

// ------------------------------------------------------------------ outage rescue
let rescueMap = null, rescueLayer = null;
loaders.rescue = async () => {
  if (!rescueMap) rescueMap = baseMap("rescue-map", [42.3175, -83.2207], 13);
  setTimeout(() => rescueMap.invalidateSize(), 60);
  const r = await api("/api/rescue");
  rescueLayer?.remove(); rescueLayer = L.layerGroup().addTo(rescueMap);
  L.geoJSON(r.outages, { style: { color: "#b91c1c", weight: 1, fillOpacity: 0.1 } }).addTo(rescueLayer);
  const risk = new Map();
  r.people.forEach((p) => { const cur = risk.get(p.user_id); if (!cur || (p.hours_left ?? 1e9) < (cur.hours_left ?? 1e9)) risk.set(p.user_id, p); });
  r.users.forEach((u) => {
    const p = risk.get(u.id);
    const color = !p ? "#8a9591" : p.at_risk ? "#dc2626" : "#d97706";
    L.circleMarker([u.lat, u.lon], { radius: u.is_me ? 10 : 7, color: "#fff", weight: 2, fillColor: color, fillOpacity: 1 })
      .bindPopup(`<b>${esc(u.name)}</b>${u.can_host ? " · can host" : ""}${p ? `<br>${esc(p.medicine)}: ${hrs(p.hours_left)} left` : ""}`).addTo(rescueLayer);
  });
  r.refuges.forEach((f) => L.marker([f.lat, f.lon], { title: f.name }).bindPopup(`<b>${esc(f.name)}</b><br>has power and a fridge`).addTo(rescueLayer));
  const out = r.outages.features[0]?.properties;
  const atRisk = r.people.filter((p) => p.at_risk);
  $("#plan-btn").hidden = !r.outages.features.length;
  if (!r.outages.features.length) $("#rescue-plan").innerHTML = "";
  $("#rescue-list").innerHTML = !r.people.length ? `<div class="banner ok">No power outage right now.</div><p class="muted small">During an outage, this page ranks neighbors by how long their medicines will last versus when power returns.</p>` : `
    <div class="banner bad"><b>${esc(out?.name || "Power outage")}</b>: power back in about ${hrs(r.people[0].hours_to_restore)}, homes around ${temp(out?.indoor_temp_c)}. <b>${atRisk.length}</b> medicine${atRisk.length === 1 ? "" : "s"} will run out first.</div>
    <div class="table-wrap"><table><tr><th>Person</th><th>Medicine</th><th class="num">Lasts</th><th>Go to</th></tr>
    ${r.people.map((p) => `<tr class="${p.at_risk ? "risk" : ""}"><td>${esc(p.name)}</td><td>${esc(p.medicine)}<div class="muted small">${pct(p.remaining)} budget</div></td>
      <td class="num">${hrs(p.hours_left)}<div class="muted small">${esc(p.warming_model || "")}</div></td><td>${p.refuges[0] ? `${esc(p.refuges[0].name)}<div class="muted small">${p.refuges[0].miles} mi</div>` : "–"}</td></tr>`).join("")}</table></div>`;
};
$("#plan-btn").addEventListener("click", (e) => busy(e.currentTarget, async () => {
  const r = await post(withLang("/api/rescue/plan"));
  $("#rescue-plan").innerHTML = `<div class="ai-box">${aiTag(r, "Gemini dispatch plan")}${md(r.plan)}</div>`;
}));

// ------------------------------------------------------------------ trip
$("#trip-btn").addEventListener("click", (e) => busy(e.currentTarget, async () => {
  const r = await post(withLang(`/api/items/${$("#trip-item").value}/trip`), { itinerary: $("#trip-text").value });
  $("#trip-result").innerHTML = `
    <div class="stats"><div class="stat"><div class="v">${pct(r.start_remaining)}</div><div class="k">before the trip</div></div>
      <div class="stat"><div class="v">${pct(r.end_remaining)}</div><div class="k">after the trip</div></div>
      <div class="stat"><div class="v">${pct(r.start_remaining - r.end_remaining, 1)}</div><div class="k">the trip costs</div></div></div>
    <div class="table-wrap"><table><tr><th>Part of the trip</th><th>Where it is</th><th class="num">Up to</th><th class="num">Costs</th></tr>
    ${r.legs.map((l) => `<tr class="${l.budget_used > 0.05 ? "risk" : ""}"><td>${esc(l.summary)}<div class="muted small">${esc(l.place)} · ${fmt(l.start_iso)}</div></td>
      <td>${esc(l.setting.replace("_", " "))}</td><td class="num">${temp(l.peak_temp_c)}</td><td class="num">${pct(l.budget_used, 1)}</td></tr>`).join("")}</table></div>
    <div class="ai-box"><div class="tag">${icon("spark")}Gemini trip advice</div>${md(r.advice)}</div>`;
}));

// ------------------------------------------------------------------ porch heat
let porchMap = null, porchLayer = null;
loaders.porch = async () => {
  if (!porchMap) porchMap = baseMap("porch-map", [42.325, -83.2], 12);
  setTimeout(() => porchMap.invalidateSize(), 60);
  const rows = await api("/api/porch");
  porchLayer?.remove(); porchLayer = L.layerGroup().addTo(porchMap);
  const byZip = {};
  rows.forEach((r) => { (byZip[r.zip] ??= { ...r, carriers: [] }).carriers.push(r); });
  Object.values(byZip).forEach((z) => {
    const worst = Math.max(...z.carriers.map((c) => +c.avg_hot_hours));
    L.circle([z.lat, z.lon], { radius: 250 + worst * 180, color: "#c2410c", weight: 1, fillColor: "#ea580c", fillOpacity: 0.12 + Math.min(0.45, worst / 7) })
      .bindPopup(`<b>${esc(z.zip)} ${esc(z.name)}</b><br>${z.carriers.map((c) => `${esc(c.carrier)}: ${c.avg_hot_hours} h above 30°C`).join("<br>")}`).addTo(porchLayer);
  });
  const max = Math.max(...rows.map((r) => +r.avg_hot_hours), 1);
  $("#porch-table").innerHTML = `<div class="table-wrap"><table><tr><th>Area</th><th>Carrier</th><th>Hours above 30°C (avg)</th><th class="num">Over 2 h</th><th class="num">Hottest</th></tr>
    ${rows.map((r) => `<tr><td>${esc(r.zip)}<div class="muted small">${esc(r.name)}</div></td><td>${esc(r.carrier)}</td>
      <td><div class="bar-cell"><i style="width:${(r.avg_hot_hours / max) * 90}px"></i>${r.avg_hot_hours}</div></td><td class="num">${r.pct_over_2h}%</td><td class="num">${temp(+r.peak_mailbox_c)}</td></tr>`).join("")}</table></div>
    <p class="muted small">Mailbox temperature is estimated as the air temperature plus 12°C in daytime sun (10am–6pm), plus 2°C otherwise.</p>`;
};
$("#brief-btn").addEventListener("click", (e) => busy(e.currentTarget, async () => {
  const r = await post("/api/porch/brief");
  $("#porch-brief").innerHTML = `<div class="ai-box">${aiTag(r, "Gemini pharmacy brief")}${md(r.brief)}</div>`;
}));

// ------------------------------------------------------------------ caregiver sharing
async function loadShares() {
  const list = await api("/api/shares");
  $("#share-list").innerHTML = list.map((s) => `<div class="row between"><span>${esc(s.label || "Unnamed link")} · ${fmt(s.created_at)}</span>
    <button class="btn" data-revoke="${esc(s.token)}">Turn off</button></div>`).join("") || "None yet.";
  $$("[data-revoke]").forEach((b) => b.addEventListener("click", async () => {
    await api(`/api/share/${b.dataset.revoke}`, { method: "DELETE" }); toast("Link turned off"); $("#share-out").innerHTML = ""; loadShares();
  }));
}
$("#share-btn").addEventListener("click", () => { $("#share-sheet").hidden = false; loadShares(); });
$("#share-form").addEventListener("submit", (e) => {
  e.preventDefault();
  busy(e.submitter, async () => {
    const { token } = await post("/api/share", { label: $("#share-label").value || null });
    const url = `${location.origin}/s/${token}`;
    $("#share-out").innerHTML = `<div id="qr"></div><div class="link-box">${esc(url)}</div>
      <div class="row"><button class="btn primary" id="copy-share">Copy link</button>${navigator.share ? `<button class="btn" id="native-share">Send…</button>` : ""}</div>`;
    if (window.QRCode) new QRCode($("#qr"), { text: url, width: 160, height: 160 });
    $("#copy-share").onclick = () => navigator.clipboard.writeText(url).then(() => toast("Copied"));
    $("#native-share")?.addEventListener("click", () => navigator.share({ title: "LIFELOG", text: "My medicine status", url }));
    loadShares();
  });
});

// ------------------------------------------------------------------ notifications + offline (service worker)
let swReg = null;
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").then((r) => (swReg = r)).catch(() => {});
function notifyLabel() {
  if (!("Notification" in window)) { $("#notify-btn").hidden = true; return; }
  $("#notify-btn").textContent = Notification.permission === "granted" ? "Notifications on" : Notification.permission === "denied" ? "Notifications blocked" : "Turn on notifications";
  $("#notify-btn").disabled = Notification.permission !== "default";
}
$("#notify-btn").addEventListener("click", async () => { await Notification.requestPermission(); notifyLabel(); if (Notification.permission === "granted") toast("You'll get a notification for critical alerts"); });
notifyLabel();
async function systemNotify(a) {
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  const reg = swReg || (await navigator.serviceWorker?.ready.catch(() => null));
  const opts = { body: a.message, icon: "/static/icon.svg", badge: "/static/icon.svg", tag: `alert-${a.item_id}-${a.kind}` };
  try { reg ? await reg.showNotification("LIFELOG", opts) : new Notification("LIFELOG", opts); } catch { /* unsupported */ }
}

// ------------------------------------------------------------------ for developers
loaders.dev = async () => {
  const [devs, hooks, items] = await Promise.all([api("/api/devices"), api("/api/webhooks"), api("/api/items")]);
  $("#dev-item").innerHTML = items.map((i) => `<option value="${i.id}">${esc(i.nickname)}</option>`).join("");
  $("#dev-devices").innerHTML = devs.map((d) => `<div class="row between dev-row"><div><b>${esc(d.label || "Sensor")}</b> → ${esc(d.nickname)}
      <div class="muted small"><code>${esc(d.token.slice(0, 10))}…</code> · ${d.readings} readings · ${d.last_seen ? "last seen " + fmt(d.last_seen) : "never used"}</div></div>
      <button class="btn" data-revoke-dev="${esc(d.token)}">Revoke</button></div>`).join("") || `<p class="muted small">No device keys yet.</p>`;
  $("#dev-hooks").innerHTML = hooks.map((h) => `<div class="row between dev-row"><div><code>${esc(h.url)}</code>
      <div class="muted small">${h.last_at ? `last delivery ${fmt(h.last_at)}: ${esc(h.last_status)}` : "no deliveries yet"}</div></div>
      <button class="btn" data-del-hook="${h.id}">Remove</button></div>`).join("") || `<p class="muted small">No webhooks yet.</p>`;
  $$("[data-revoke-dev]").forEach((b) => b.onclick = async () => { await api(`/api/devices/${b.dataset.revokeDev}`, { method: "DELETE" }); loaders.dev(); });
  $$("[data-del-hook]").forEach((b) => b.onclick = async () => { await api(`/api/webhooks/${b.dataset.delHook}`, { method: "DELETE" }); loaders.dev(); });
};
$("#dev-form").addEventListener("submit", (e) => {
  e.preventDefault();
  busy(e.submitter, async () => {
    const { token } = await post("/api/devices", { item_id: +$("#dev-item").value, label: $("#dev-label").value || null });
    $("#dev-new").innerHTML = `<div class="banner ok">New key (shown once): <code>${esc(token)}</code></div>
      <pre class="log">curl -X POST ${esc(location.origin)}/v1/readings \\
  -H "Authorization: Bearer ${esc(token)}" \\
  -H "Content-Type: application/json" \\
  -d '{"temp_c": 4.6}'</pre>`;
    loaders.dev();
  });
});
$("#hook-form").addEventListener("submit", (e) => {
  e.preventDefault();
  busy(e.submitter, async () => {
    const h = await post("/api/webhooks", { url: $("#hook-url").value });
    $("#hook-new").innerHTML = `<div class="banner ok">Signing secret (shown once): <code>${esc(h.secret)}</code><br><span class="small">Each alert is POSTed as JSON with <code>X-Lifelog-Signature: sha256=HMAC(secret, body)</code>.</span></div>`;
    $("#hook-url").value = ""; loaders.dev();
  });
});

// ------------------------------------------------------------------ Bluetooth thermometer (Web Bluetooth, no app)
function ieee11073(dv, offset) {          // Health Thermometer: 32-bit IEEE-11073 FLOAT
  const raw = dv.getUint32(offset, true);
  let mant = raw & 0x00ffffff; const exp = (raw >> 24) << 24 >> 24;
  if (mant >= 0x800000) mant -= 0x1000000;
  return mant * Math.pow(10, exp);
}
async function connectBle(itemId) {
  try {
    const dev = await navigator.bluetooth.requestDevice({
      acceptAllDevices: true, optionalServices: ["health_thermometer", "environmental_sensing"] });
    const server = await dev.gatt.connect();
    let ch, parse;
    try {
      ch = await (await server.getPrimaryService("health_thermometer")).getCharacteristic("temperature_measurement");
      parse = (dv) => { const c = ieee11073(dv, 1); return dv.getUint8(0) & 1 ? (c - 32) * 5 / 9 : c; };
    } catch {
      ch = await (await server.getPrimaryService("environmental_sensing")).getCharacteristic("temperature");
      parse = (dv) => dv.getInt16(0, true) / 100;   // sint16, 0.01 °C
    }
    const btn = $("#ble-btn"); if (btn) btn.textContent = `Sensor: ${dev.name || "connected"}`;
    ch.addEventListener("characteristicvaluechanged", async (ev) => {
      const t = parse(ev.target.value);
      if (!Number.isFinite(t)) return;
      try { await post("/api/ingest", { item_id: itemId, source: "bluetooth", readings: [{ ts: new Date().toISOString(), temp_c: +t.toFixed(2) }] }); }
      catch (e) { console.error(e); }
    });
    await ch.startNotifications();
    toast(`Streaming from ${dev.name || "your sensor"}`);
    dev.addEventListener("gattserverdisconnected", () => { toast("Sensor disconnected", "warn"); const b = $("#ble-btn"); if (b) b.textContent = "Connect sensor"; });
  } catch (e) {
    if (e.name !== "NotFoundError") toast("Bluetooth: " + e.message, "warn");   // NotFoundError = picker cancelled
  }
}

// ------------------------------------------------------------------ alerts
async function refreshBadge() {
  try {
    const a = await api("/api/alerts?mine=true");
    const n = a.filter((x) => !x.acked).length;
    for (const id of ["#alert-count", "#alert-count-m"]) { $(id).hidden = !n; $(id).textContent = n; }
  } catch { /* api offline */ }
}
loaders.alerts = async () => {
  const all = $("#alerts-all").checked;
  const a = await api(`/api/alerts?mine=${!all}&include_resolved=true`);
  $("#alerts-list").innerHTML = a.map((x) => `
    <div class="alert ${x.resolved_at ? "resolved" : esc(x.severity)}">
      ${icon(x.resolved_at ? "check" : "alert")}
      <div style="flex:1"><div>${esc(x.message)}</div><div class="muted small">${all ? esc(x.user_name) + " · " : ""}${x.resolved_at ? "Resolved " + fmt(x.resolved_at) : "Since " + fmt(x.created_at)}</div></div>
      ${!x.resolved_at && !x.acked && x.is_me ? `<button class="btn" data-ack="${x.id}">Got it</button>` : ""}
    </div>`).join("") || `<div class="banner ok">No alerts. Everything is within its label's limits.</div>`;
  $$("[data-ack]").forEach((b) => b.addEventListener("click", async () => { await post(`/api/alerts/${b.dataset.ack}/ack`); loaders.alerts(); refreshBadge(); }));
};
$("#alerts-all").addEventListener("change", () => loaders.alerts());
$("#alerts-check").addEventListener("click", (e) => busy(e.currentTarget, async () => { await post("/api/alerts/check"); await loaders.alerts(); refreshBadge(); }));

// ------------------------------------------------------------------ ask LIFELOG
const history = [];
function addMsg(role, html) {
  const el = document.createElement("div"); el.className = `msg ${role}`; el.dir = "auto"; el.innerHTML = html;
  $("#chat").appendChild(el); el.scrollIntoView({ block: "end", behavior: "smooth" }); return el;
}
async function askLifelog(q) {
  if (!q.trim()) return;
  addMsg("user", esc(q)); $("#ask-input").value = "";
  const pending = addMsg("model", `<span class="spin muted">Looking at your data</span>`);
  try {
    const r = await post(withLang("/api/ask"), { question: q, history });
    history.push({ role: "user", text: q }, { role: "model", text: r.answer });
    pending.innerHTML = md(r.answer) + (r.tool_calls.length || r.model ? `<div class="tools">${r.offline ? "no AI · " : ""}looked up: ${r.tool_calls.map((c) => `${esc(c.name)}(${esc(Object.values(c.args).join(", "))})`).join(" · ")}${r.model ? ` · ${esc(r.model)}` : ""}</div>` : "");
  } catch (e) { pending.innerHTML = `<span class="muted">${esc(e.message.includes("GEMINI_API_KEY") ? "Gemini isn't set up yet: add GEMINI_API_KEY to .env." : e.message)}</span>`; }
}
$("#ask-form").addEventListener("submit", (e) => { e.preventDefault(); askLifelog($("#ask-input").value); });
$$("[data-q]").forEach((b) => b.addEventListener("click", () => askLifelog(b.dataset.q)));

// ------------------------------------------------------------------ under the hood
const bytes = (b) => (b == null ? "–" : b > 1048576 ? `${(b / 1048576).toFixed(1)} MB` : `${(b / 1024).toFixed(0)} kB`);
loaders.tiger = async (fresh = false) => {
  if (!$("#tiger-body").innerHTML) $("#tiger-body").innerHTML = `<p class="muted spin">Measuring the database</p>`;
  const t = await api(`/api/tiger${fresh ? "?fresh=1" : ""}`);
  const c = t.compression || {};
  const ratio = c.before_compression_total_bytes && c.after_compression_total_bytes ? (c.before_compression_total_bytes / c.after_compression_total_bytes).toFixed(1) : null;
  const bm = t.benchmark || {};
  const ms = (v) => (v == null ? "–" : v >= 1000 ? `${(v / 1000).toFixed(1)} s` : `${v} ms`);
  const benchRow = (label, x) => x ? `<tr><td>${label}</td><td class="num">${ms(x.continuous_aggregate_ms)}<div class="muted small">${(+x.aggregate_rows_read).toLocaleString()} rollup rows</div></td>
    <td class="num">${ms(x.raw_readings_ms)}<div class="muted small">${(+x.raw_rows_scanned).toLocaleString()} raw rows</div></td><td class="num"><b>${x.speedup ?? "–"}×</b></td></tr>` : "";
  $("#tiger-body").innerHTML = `
    <div class="stats">
      <div class="stat"><div class="v">${ratio ? ratio + "×" : "–"}</div><div class="k">smaller after compression (${bytes(c.before_compression_total_bytes)} → ${bytes(c.after_compression_total_bytes)})</div></div>
      <div class="stat"><div class="v">${ms(bm.budget_all_items?.continuous_aggregate_ms)}</div><div class="k">every budget, from the live rollup</div></div>
      <div class="stat"><div class="v">${ms(bm.budget_all_items?.raw_readings_ms)}</div><div class="k">same, from raw readings</div></div>
      <div class="stat"><div class="v">${t.listeners ?? "–"}</div><div class="k">live browser connections</div></div>
    </div>
    <h3>Rollup vs raw readings</h3>
    <div class="table-wrap"><table><tr><th>Query</th><th class="num">Continuous aggregate</th><th class="num">Raw readings</th><th class="num">Faster</th></tr>
    ${benchRow("Life budget, every medicine", bm.budget_all_items)}${benchRow("30-day calendar, every medicine", bm.calendar_30d)}</table></div>
    <p class="muted small">Median of ${bm.runs ?? 3} runs after a warm-up${bm.measured_at ? ", measured " + fmt(bm.measured_at) : ""}. Refresh to re-measure.</p>
    <h3>Time-series tables (hypertables)</h3>
    <div class="table-wrap"><table><tr><th>Table</th><th class="num">Rows</th><th class="num">Chunks</th><th class="num">Size</th><th>Compression</th></tr>
    ${t.hypertables.map((h) => `<tr><td><code>${esc(h.name)}</code></td><td class="num">${(+h.rows).toLocaleString()}</td><td class="num">${h.chunks}</td><td class="num">${bytes(h.bytes)}</td><td>${h.compression_enabled ? "on" : "–"}</td></tr>`).join("")}</table></div>
    <h3>Live rollups (continuous aggregates)</h3>
    <div class="table-wrap"><table><tr><th>View</th><th>Mode</th></tr>${t.caggs.map((v) => `<tr><td><code>${esc(v.view_name)}</code></td><td>${v.materialized_only ? "materialized only" : "real-time: stored rollup plus the newest readings"}</td></tr>`).join("")}</table></div>
    <h3>Jobs Tiger runs for us</h3>
    <div class="table-wrap"><table><tr><th>Job</th><th>Every</th><th class="num">Runs</th><th>Last run</th></tr>
    ${t.jobs.map((j) => `<tr><td><code>${esc(j.proc_name)}</code><div class="muted small">${esc(j.hypertable_name || "")}</div></td><td>${esc(j.every)}</td><td class="num">${j.total_runs ?? "–"}</td>
      <td>${j.last_run_started_at ? fmt(j.last_run_started_at) + " · " + esc(j.last_run_status || "") : "–"}</td></tr>`).join("")}</table></div>
    <h3>Extensions</h3><p>${t.extensions.map((e) => `<span class="pill on">${esc(e.extname)} ${esc(e.extversion)}</span>`).join(" ")}</p>`;
};
$("#tiger-refresh").addEventListener("click", (e) => busy(e.currentTarget, () => loaders.tiger(true)));

// ------------------------------------------------------------------ demo controls
const log = (o) => { $("#demo-log").textContent = `${new Date().toLocaleTimeString()}  ${JSON.stringify(o, null, 1)}\n` + $("#demo-log").textContent; };
$$("[data-demo]").forEach((b) => b.addEventListener("click", () => busy(b, async () => {
  if (b.dataset.demo === "reset" && !confirm("Reset erases every medicine, reading and alert in this database and loads fresh demo data. Continue?")) return;
  const r = await post(`/api/demo/${b.dataset.demo}`); log({ [b.dataset.demo]: r }); toast("Done"); await loadCompare(); health();
  if (b.dataset.demo === "reset") { currentItem = null; await loadItems(); }
})));
$("#repair-btn").addEventListener("click", (e) => busy(e.currentTarget, async () => {
  const id = await insulinId(); const r = await post(`/api/items/${id}/repair`); log({ repair: r }); await loadCompare();
}));
async function insulinId() { const items = await api("/api/items"); return (items.find((i) => /insulin/i.test(i.nickname)) || items[0])?.id; }
async function loadCompare() {
  const id = await insulinId(); if (!id) return;
  const c = await api(`/api/items/${id}/compare`);
  const diff = c.aggregate.remaining - c.raw_truth.remaining;
  $("#compare").innerHTML = `<div class="stats">
    <div class="stat"><div class="v">${pct(c.aggregate.remaining, 1)}</div><div class="k">what the dashboard shows</div></div>
    <div class="stat"><div class="v">${pct(c.raw_truth.remaining, 1)}</div><div class="k">recomputed from raw data</div></div>
    <div class="stat"><div class="v">${Math.abs(diff) < 0.001 ? "in sync" : "+" + pct(diff, 1)}</div><div class="k">overstatement</div></div></div>
    ${Math.abs(diff) >= 0.001 ? `<div class="banner bad">Late readings are hidden behind the rollup's watermark: the dashboard overstates the budget by ${pct(diff, 1)}.</div>` : `<div class="banner ok">The dashboard matches the raw data.</div>`}
    ${watermarkStrip(c)}`;
}
// Where late batches landed relative to the continuous aggregate's materialization watermark.
function watermarkStrip(c) {
  if (!c.watermark) return "";
  const now = Date.now(), wm = new Date(c.watermark).getTime();
  const late = (c.log || []).filter((l) => l.late);
  const t0 = Math.min(now - 8 * 3600e3, ...late.map((l) => new Date(l.min_ts).getTime()));
  const frac = (t) => Math.max(0, Math.min(1, (t - t0) / (now - t0)));
  const x = (t) => `${(frac(t) * 100).toFixed(2)}%`;
  const hm = (t) => new Date(t).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const band = (a, b, cls, title) => (b <= a ? "" :
    `<div class="wm-band ${cls}" style="left:${x(a)};width:calc(${x(b)} - ${x(a)} + 3px)" title="${title}"></div>`);
  // the part of a late batch behind the watermark is hidden until refreshed; the part after it is served live
  const bands = late.map((l) => {
    const a = new Date(l.min_ts).getTime(), b = new Date(l.max_ts).getTime();
    return band(a, Math.min(b, wm), l.refreshed ? "ok" : "bad",
                `${hm(a)}–${hm(Math.min(b, wm))}: ${l.refreshed ? "window refreshed" : "behind the watermark, hidden until refreshed"}`)
         + band(Math.max(a, wm), b, "live", `${hm(Math.max(a, wm))}–${hm(b)}: after the watermark, computed live`);
  }).join("");
  const open = late.some((l) => !l.refreshed);
  const edge = frac(wm) > 0.7 ? "end" : frac(wm) < 0.3 ? "start" : "mid";
  return `<div class="wm">
    <div class="wm-track">
      <div class="wm-rt" style="left:${x(wm)}"></div>${bands}
      <div class="wm-line" style="left:${x(wm)}"><span class="${edge}">watermark ${hm(wm)}</span></div>
    </div>
    <div class="wm-axis"><span>${hm(t0)}</span><span>now</span></div>
    <div class="wm-legend small muted">
      <span><i class="wm-key mat"></i>materialized rollup (served as stored)</span>
      <span><i class="wm-key rt"></i>real-time: computed from raw readings</span>
      ${late.length ? `<span><i class="wm-key ${open ? "bad" : "ok"}"></i>late readings ${open ? "behind the watermark: not in the rollup yet" : "behind the watermark: windows refreshed"}</span>` : ""}
    </div></div>`;
}
loaders.demo = () => { loadCompare(); simAction("status"); };

let simRunning = false;
async function simAction(action) {
  const st = await post(`/api/sim/${action}`);
  simRunning = st.running;
  $("#sim-btn").textContent = st.running ? "Stop live sensors" : "Start live sensors";
  $("#sim-status").textContent = st.running ? `Streaming one reading per medicine every ${st.tick_seconds} s · ${st.rows} so far` : st.rows ? `${st.rows} readings streamed` : "";
}
$("#sim-btn").addEventListener("click", (e) => busy(e.currentTarget, () => simAction(simRunning ? "stop" : "start")));

// ------------------------------------------------------------------ real time (Server-Sent Events from Postgres NOTIFY)
let es = null, refreshTimer = null, viewTimer = null;
function connectLive() {
  es?.close();
  es = new EventSource("/api/stream");
  es.addEventListener("hello", () => { $("#live").textContent = "Live"; $("#live").classList.add("on"); });
  es.addEventListener("readings", () => {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(async () => {
      if (currentView !== "meds" || document.hidden) return;
      await loadItems();
      if (currentItem && $("#view-meds").classList.contains("detail-open")) openItem(currentItem, { quiet: true });
    }, 400);
    // views whose numbers move with every reading
    clearTimeout(viewTimer);
    viewTimer = setTimeout(() => {
      if (document.hidden) return;
      if (currentView === "rescue") loaders.rescue();
      if (currentView === "demo") loadCompare();
    }, 1500);
  });
  es.addEventListener("alert", (e) => {
    const a = JSON.parse(e.data);
    refreshBadge();
    if (currentView === "alerts") loaders.alerts();
    if (!a.resolved && a.severity === "critical" && myItems.some((i) => i.id === a.item_id)) {
      toast(a.message, "critical");
      if (document.hidden || isMobile()) systemNotify(a);
    }
  });
  es.onerror = () => { $("#live").textContent = "Reconnecting…"; $("#live").classList.remove("on"); };
}

// ------------------------------------------------------------------ deep links: #view=rescue, #item=3&open=accuracy
async function applyHash() {
  const h = new URLSearchParams(location.hash.slice(1));
  if (h.get("item")) {
    show("meds");
    await openItem(+h.get("item"));
    for (const k of (h.get("open") || "").split(",").filter(Boolean)) {
      const d = $(`#item-detail details[data-k="${k}"]`); if (d) d.open = true;
    }
  } else if (h.get("view")) show(h.get("view"));
}
addEventListener("hashchange", applyHash);

// ------------------------------------------------------------------ boot
health();
refreshBadge();
if (!location.search.includes("nolive")) connectLive(); else $("#live").hidden = true;
loadItems().then(applyHash).catch((e) => { $("#item-list").innerHTML = `<div class="card"><p>${esc(e.message)}</p><p class="muted">Check .env and run scripts/setup_db.py.</p></div>`; });
addEventListener("resize", () => setDetailOpen($("#view-meds").classList.contains("detail-open")));
