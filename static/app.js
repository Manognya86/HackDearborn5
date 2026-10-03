// LIFELOG Home frontend. Plain JS, no build step.
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const md = (s) => marked.parse(String(s ?? "").replace(/</g, "&lt;"));
const pct = (x) => (x == null ? "–" : `${(x * 100).toFixed(1)}%`);
const hrs = (h) => (h == null ? "stable" : h >= 48 ? `${(h / 24).toFixed(1)} d` : `${h.toFixed(1)} h`);
const temp = (t) => (t == null ? "–" : `${t.toFixed(1)}°C`);
const fmt = (t) => new Date(t).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });

function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.remove("show"), 3500);
}

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || `${r.status} ${r.statusText}`);
  return body;
}
const post = (path, data) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data ?? {}) });

async function busy(btn, fn) {
  const label = btn.textContent; btn.disabled = true; btn.classList.add("spin");
  try { return await fn(); } catch (e) { toast(e.message); console.error(e); } finally { btn.disabled = false; btn.classList.remove("spin"); btn.textContent = label; }
}

const barClass = (r) => (r < 0.3 ? "low" : r < 0.7 ? "mid" : "");

// ------------------------------------------------------------------ navigation
const loaders = {};
$$("#nav button").forEach((b) => b.addEventListener("click", () => show(b.dataset.view)));
function show(view) {
  $$("#nav button").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${view}`));
  loaders[view]?.();
}

async function health() {
  try {
    const h = await api("/api/health");
    const ext = h.extensions || {};
    $("#health").innerHTML = [
      [`Tiger DB`, h.database], [`timescaledb ${ext.timescaledb || ""}`, !!ext.timescaledb],
      [`pgvector`, !!ext.vector], [`PostGIS`, !!ext.postgis], [`Gemini`, h.gemini],
    ].map(([k, on]) => `<span class="pill ${on ? "on" : "off"}">${esc(k)}</span>`).join("");
  } catch { $("#health").innerHTML = `<span class="pill off">API offline</span>`; }
}

// ------------------------------------------------------------------ medicines
let currentItem = null, chart = null;

async function loadItems() {
  const items = await api("/api/items");
  $("#item-list").innerHTML = items.map((i) => `
    <div class="med ${i.id === currentItem ? "active" : ""}" data-id="${i.id}">
      <div class="name">${esc(i.nickname)}</div>
      <div class="muted small">${esc(i.product_name)}${i.product_source === "gemini" ? " · from label" : ""}</div>
      <div class="bar ${barClass(i.remaining)}"><span style="width:${(i.remaining * 100).toFixed(1)}%"></span></div>
      <div class="small muted">${pct(i.remaining)} budget · ${temp(i.current_temp)} · ${esc(i.zone)}${i.stale_minutes > 20 ? " · <b>sensor silent</b>" : ""}</div>
    </div>`).join("") || `<p class="muted pad">No medicines yet. Use Demo controls → Reset.</p>`;
  $$(".med").forEach((el) => el.addEventListener("click", () => openItem(+el.dataset.id)));
  for (const sel of ["#voice-item", "#trip-item"]) {
    $(sel).innerHTML = items.map((i) => `<option value="${i.id}">${esc(i.nickname)}</option>`).join("");
  }
  if (!currentItem && items.length) openItem(items[0].id);
  return items;
}
loaders.meds = () => loadItems().then(() => currentItem && openItem(currentItem));

async function openItem(id) {
  currentItem = id;
  $$(".med").forEach((el) => el.classList.toggle("active", +el.dataset.id === id));
  const d = await api(`/api/items/${id}`);
  const s = d.state, m = d.item.model;
  const ongoingGap = d.gaps.find((g) => g.ongoing);
  const maxBurn = Math.max(...d.burners.map((b) => b.burn), 0.0001);
  $("#item-detail").innerHTML = `
    <div class="row between">
      <div><h2>${esc(d.item.nickname)}</h2><div class="muted small">${esc(d.item.product_name)} · budget counted since ${fmt(d.item.started_at)}</div></div>
      <div class="row">
        <button id="advice-btn" class="primary">Is it safe to use?</button>
        <label class="small" style="margin:0"><input type="file" id="photo" accept="image/*" hidden><button id="photo-btn">Photo check</button></label>
        <button id="letter-btn">Refill letter</button>
      </div>
    </div>
    ${ongoingGap ? `<div class="banner warn">Sensor silent for ${hrs(ongoingGap.hours)}. The budget below can't account for that time until the data arrives.</div>` : ""}
    ${s.remaining <= 0 ? `<div class="banner bad">Life budget exhausted.</div>` : ""}
    <div class="stats">
      <div class="stat"><div class="v">${pct(s.remaining)}</div><div class="k">life budget left</div></div>
      <div class="stat"><div class="v">${hrs(s.hours_left)}</div><div class="k">left at current temp</div></div>
      <div class="stat"><div class="v">${temp(s.current_temp)}</div><div class="k">current (${esc(s.zone)})</div></div>
      <div class="stat"><div class="v">${d.burners.length}</div><div class="k">budget-consuming episodes</div></div>
    </div>
    <div id="ai-box"></div>
    <div class="chart-box"><canvas id="chart"></canvas></div>

    <h3>What consumed the budget</h3>
    ${d.burners.length ? d.burners.map((b) => `
      <div class="burn-row">
        <div>${esc(b.zone)} <span class="muted small">${fmt(b.started)} · ${(b.minutes / 60).toFixed(1)} h · peak ${temp(b.peak_temp)}</span></div>
        <div><div class="burn-bar" style="width:${(b.burn / maxBurn) * 100}%"></div></div>
        <div class="num">-${(b.burn * 100).toFixed(1)}%</div>
      </div>`).join("") : `<p class="muted">Nothing yet. Stored within labeled conditions.</p>`}

    <h3>What if you act now? <span class="muted small">budget left after 12 h</span></h3>
    <table><tr><th>Action</th><th class="num">Temp</th><th class="num">Left after 12 h</th><th class="num">Saved vs now</th><th class="num">Lasts</th></tr>
    ${d.whatif.map((w, i) => `<tr class="${i === 0 ? "best" : ""}"><td>${esc(w.option)}</td><td class="num">${temp(w.temp_c)}</td>
      <td class="num">${pct(w.remaining_after)}</td><td class="num">${w.preserved_vs_now > 0 ? "+" + pct(w.preserved_vs_now) : "–"}</td><td class="num">${hrs(w.hours_left)}</td></tr>`).join("")}
    </table>

    <h3>Similar exposure histories <span class="muted small">pgvector search over the pattern library</span></h3>
    <table><tr><th>Pattern</th><th>Typical outcome</th><th class="num">Similarity</th></tr>
    ${d.patterns.map((p) => `<tr><td><b>${esc(p.name)}</b><div class="muted small">${esc(p.description)}</div></td><td>${esc(p.outcome)}</td><td class="num">${(+p.similarity).toFixed(2)}</td></tr>`).join("")}
    </table>

    <h3>Label model</h3>
    <blockquote>${esc(m.target_quote)}</blockquote>
    ${(m.bands || []).map((b) => `<blockquote>${esc(b.quote)}</blockquote>`).join("")}
    ${m.freeze_discard ? `<blockquote>${esc(m.freeze_quote)}</blockquote>` : ""}
    <p class="muted small">Above highest labeled limit: ${m.above_limit_budget_hours} h budget, doubling per 10°C${m.above_limit_is_assumption ? " (LIFELOG assumption, not on label)" : ""}.</p>`;

  drawChart(d);
  $("#advice-btn").onclick = (e) => busy(e.target, async () => renderVerdict(await post(`/api/items/${id}/advice`)));
  $("#photo-btn").onclick = (e) => { e.preventDefault(); $("#photo").click(); };
  $("#photo").onchange = async (e) => {
    const f = e.target.files[0]; if (!f) return;
    const fd = new FormData(); fd.append("photo", f);
    await busy($("#photo-btn"), async () => {
      const v = await api(`/api/items/${id}/visual`, { method: "POST", body: fd });
      $("#ai-box").innerHTML = `<div class="ai"><div class="tag">Gemini photo check</div>
        <div class="verdict ${v.looks_ok ? "USE" : "DO_NOT_USE"}">${v.looks_ok ? "No visible warning signs" : "Visible warning sign"}</div>
        <ul>${v.observations.map((o) => `<li>${esc(o)}</li>`).join("")}</ul>
        ${v.matched_label_warnings.map((q) => `<blockquote>${esc(q)}</blockquote>`).join("")}
        <div class="muted small">Confidence: ${esc(v.confidence)}. A photo can't prove a medicine is safe; ask your pharmacist if in doubt.</div></div>`;
    });
  };
  $("#letter-btn").onclick = (e) => busy(e.target, async () => {
    const r = await post(`/api/items/${id}/letter`);
    $("#ai-box").innerHTML = `<div class="ai"><div class="tag">Gemini refill / replacement letter</div><pre class="log">${esc(r.letter)}</pre>
      <button id="copy-letter">Copy</button></div>`;
    $("#copy-letter").onclick = () => navigator.clipboard.writeText(r.letter).then(() => toast("Copied"));
  });
}

function renderVerdict(v) {
  $("#ai-box").innerHTML = `<div class="ai"><div class="tag">Gemini · grounded in your data and the label</div>
    <div class="verdict ${esc(v.verdict)}">${esc(v.verdict.replace(/_/g, " "))}: ${esc(v.headline)}</div>
    <p>${esc(v.explanation)}</p>
    <ul>${v.actions.map((a) => `<li>${esc(a)}</li>`).join("")}</ul>
    ${v.cited_quotes.map((q) => `<blockquote>${esc(q)}</blockquote>`).join("")}</div>`;
}

function drawChart(d) {
  chart?.destroy();
  const css = getComputedStyle(document.documentElement);
  const teal = css.getPropertyValue("--teal").trim(), purple = css.getPropertyValue("--purple").trim();
  const muted = css.getPropertyValue("--muted").trim(), border = css.getPropertyValue("--border").trim();
  const m = d.item.model;
  const pts = d.timeline;
  // insert nulls across gaps so lines break instead of bridging missing data
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
    data: {
      datasets: [
        { label: "Temperature °C", data: withGaps("temp"), borderColor: teal, borderWidth: 1.5, pointRadius: 0, yAxisID: "t", spanGaps: false },
        { label: "Life budget %", data: withGaps("remaining", 100), borderColor: purple, borderWidth: 2, pointRadius: 0, yAxisID: "b", spanGaps: false },
        { label: `Labeled max ${m.target_max_c}°C`, data: pts.length ? [{ x: new Date(pts[0].t), y: m.target_max_c }, { x: new Date(pts[pts.length - 1].t), y: m.target_max_c }] : [],
          borderColor: muted, borderDash: [4, 4], borderWidth: 1, pointRadius: 0, yAxisID: "t" },
      ],
    },
    options: {
      maintainAspectRatio: false, animation: false, interaction: { mode: "index", intersect: false },
      scales: {
        x: { type: "time", grid: { color: border }, ticks: { color: muted } },
        t: { position: "left", title: { display: true, text: "°C", color: muted }, grid: { color: border }, ticks: { color: muted } },
        b: { position: "right", min: 0, max: 100, title: { display: true, text: "budget %", color: muted }, grid: { display: false }, ticks: { color: muted } },
      },
      plugins: { legend: { labels: { color: muted, boxWidth: 12 } } },
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
    $("#label-result").innerHTML = `<div class="ai"><div class="tag">Gemini extracted this stability model</div>
      <h3>${esc(m.product_name)} <span class="muted small">${esc(m.form)}</span></h3>
      <p>Labeled storage <b>${m.target_min_c}–${m.target_max_c}°C</b></p><blockquote>${esc(m.target_quote)}</blockquote>
      ${m.bands.map((b) => `<p><b>${esc(b.label)}</b>: ${b.min_c}–${b.max_c}°C for ${hrs(b.budget_hours)}</p><blockquote>${esc(b.quote)}</blockquote>`).join("")}
      <p>Freezing: ${m.freeze_discard ? `discard at ≤ ${m.freeze_c}°C` : "not restricted"}</p><blockquote>${esc(m.freeze_quote)}</blockquote>
      <p>Above highest limit: ${m.above_limit_budget_hours} h ${m.above_limit_is_assumption ? "<span class='pill'>assumption</span>" : ""}</p>
      ${m.visual_checks.length ? `<p>Visual checks:</p><ul>${m.visual_checks.map((v) => `<li>${esc(v)}</li>`).join("")}</ul>` : ""}
      ${m.discard_rules.length ? `<p>Discard rules:</p><ul>${m.discard_rules.map((v) => `<li>${esc(v)}</li>`).join("")}</ul>` : ""}
      <details><summary class="muted small">Edit raw model (JSON)</summary><textarea id="model-json" rows="14">${esc(JSON.stringify(m, null, 2))}</textarea></details>
      <label>Nickname <input type="text" id="nick" value="${esc(m.product_name)}"></label>
      <button id="save-model" class="primary">Track this medicine</button></div>`;
    $("#save-model").onclick = (ev) => busy(ev.target, async () => {
      const model = JSON.parse($("#model-json").value);
      const r = await post("/api/products", { model, nickname: $("#nick").value });
      toast("Added. Stream readings to /api/ingest or send a voice report.");
      currentItem = r.item_id; show("meds");
    });
  });
});

// ------------------------------------------------------------------ voice (WAV recorder)
let rec = null;
$("#rec-btn").addEventListener("click", async () => {
  if (rec) { const blob = await rec.stop(); rec = null; $("#rec-btn").textContent = "Start recording"; $("#rec-status").textContent = `${(blob.size / 1024).toFixed(0)} KB recorded`; return sendVoice(blob); }
  try { rec = await startWav(); $("#rec-btn").textContent = "Stop & send"; $("#rec-status").textContent = "Recording…"; }
  catch (e) { toast("Microphone unavailable: " + e.message); }
});
$("#voice-send").addEventListener("click", () => sendVoice(null));

async function sendVoice(blob) {
  const fd = new FormData();
  fd.append("item_id", $("#voice-item").value);
  if (blob) fd.append("audio", blob, "report.wav");
  else fd.append("text", $("#voice-text").value);
  await busy($("#voice-send"), async () => {
    const r = await api("/api/voice", { method: "POST", body: fd });
    $("#voice-result").innerHTML = `<div class="ai"><div class="tag">Gemini heard</div><p>"${esc(r.report.transcript)}"</p>
      <table><tr><th>Event</th><th class="num">Started</th><th class="num">Duration</th><th class="num">Est. temp</th></tr>
      ${r.report.events.map((e) => `<tr><td>${esc(e.summary)} <span class="muted small">(${esc(e.setting)})</span></td><td class="num">${e.minutes_ago_start.toFixed(0)} min ago</td>
        <td class="num">${e.duration_minutes.toFixed(0)} min</td><td class="num">${temp(e.estimated_temp_c)}</td></tr>`).join("")}</table>
      <p class="muted small">${r.ingest.inserted} readings written to Tiger for "${esc(r.item)}".</p>
      <button onclick="show('meds')">See the updated budget</button></div>`;
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

// ------------------------------------------------------------------ outage rescue
let rescueMap = null, rescueLayer = null;
loaders.rescue = async () => {
  if (!rescueMap) {
    rescueMap = L.map("rescue-map").setView([42.3175, -83.2207], 13);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { attribution: "© OpenStreetMap" }).addTo(rescueMap);
  }
  setTimeout(() => rescueMap.invalidateSize(), 50);
  const r = await api("/api/rescue");
  rescueLayer?.remove(); rescueLayer = L.layerGroup().addTo(rescueMap);
  L.geoJSON(r.outages, { style: { color: "#a32d2d", weight: 1, fillOpacity: 0.12 } }).addTo(rescueLayer);
  const risk = new Map(r.people.map((p) => [p.user_id, p]));
  r.users.forEach((u) => {
    const p = risk.get(u.id);
    const color = !p ? "#888780" : p.at_risk ? "#e24b4a" : "#ef9f27";
    L.circleMarker([u.lat, u.lon], { radius: u.is_me ? 9 : 6, color, fillColor: color, fillOpacity: 0.85, weight: u.can_host ? 3 : 1 })
      .bindPopup(`<b>${esc(u.name)}</b>${u.can_host ? " (can host)" : ""}${p ? `<br>${esc(p.medicine)}: ${hrs(p.hours_left)} left` : ""}`).addTo(rescueLayer);
  });
  r.refuges.forEach((f) => L.marker([f.lat, f.lon], { title: f.name }).bindPopup(esc(f.name)).addTo(rescueLayer));
  const out = r.outages.features[0]?.properties;
  $("#rescue-list").innerHTML = !r.people.length ? `<p class="muted">No active outage. Demo controls → Summer storm outage.</p>` : `
    <div class="banner bad">${esc(out?.name || "Outage")}: power back in ~${hrs(r.people[0].hours_to_restore)} · indoor temp ~${temp(out?.indoor_temp_c)} ·
      <b>${r.people.filter((p) => p.at_risk).length}</b> medicines run out first</div>
    <table><tr><th>Person</th><th>Medicine</th><th class="num">Budget</th><th class="num">Life left</th><th>Nearest refuge</th></tr>
    ${r.people.map((p) => `<tr class="${p.at_risk ? "risk" : ""}"><td>${esc(p.name)}</td><td>${esc(p.medicine)}</td><td class="num">${pct(p.remaining)}</td>
      <td class="num">${hrs(p.hours_left)}</td><td>${p.refuges[0] ? `${esc(p.refuges[0].name)} <span class="muted small">${p.refuges[0].miles} mi</span>` : "–"}</td></tr>`).join("")}</table>`;
};
$("#plan-btn").addEventListener("click", (e) => busy(e.target, async () => {
  const r = await post("/api/rescue/plan");
  $("#rescue-plan").innerHTML = `<div class="ai"><div class="tag">Gemini dispatch plan</div>${md(r.plan)}</div>`;
}));

// ------------------------------------------------------------------ trip
$("#trip-btn").addEventListener("click", (e) => busy(e.target, async () => {
  const r = await post(`/api/items/${$("#trip-item").value}/trip`, { itinerary: $("#trip-text").value });
  $("#trip-result").innerHTML = `
    <div class="stats"><div class="stat"><div class="v">${pct(r.start_remaining)}</div><div class="k">budget before trip</div></div>
    <div class="stat"><div class="v">${pct(r.end_remaining)}</div><div class="k">budget after trip</div></div>
    <div class="stat"><div class="v">${pct(r.start_remaining - r.end_remaining)}</div><div class="k">trip cost</div></div>
    <div class="stat"><div class="v">${r.legs.length}</div><div class="k">legs</div></div></div>
    <table><tr><th>Leg</th><th>Where it is</th><th class="num">Avg / peak</th><th class="num">Budget used</th><th>Weather</th></tr>
    ${r.legs.map((l) => `<tr class="${l.budget_used > 0.05 ? "risk" : ""}"><td>${esc(l.summary)}<div class="muted small">${esc(l.place)} · ${fmt(l.start_iso)} → ${fmt(l.end_iso)}</div></td>
      <td>${esc(l.setting.replace("_", " "))}</td><td class="num">${temp(l.avg_temp_c)} / ${temp(l.peak_temp_c)}</td><td class="num">${pct(l.budget_used)}</td><td class="small muted">${esc(l.weather_source)}</td></tr>`).join("")}</table>
    <div class="ai"><div class="tag">Gemini trip advice</div>${md(r.advice)}</div>`;
}));

// ------------------------------------------------------------------ porch heat
let porchMap = null, porchLayer = null;
loaders.porch = async () => {
  if (!porchMap) {
    porchMap = L.map("porch-map").setView([42.325, -83.2], 12);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { attribution: "© OpenStreetMap" }).addTo(porchMap);
  }
  setTimeout(() => porchMap.invalidateSize(), 50);
  const rows = await api("/api/porch");
  porchLayer?.remove(); porchLayer = L.layerGroup().addTo(porchMap);
  const byZip = {};
  rows.forEach((r) => { (byZip[r.zip] ??= { ...r, carriers: [] }).carriers.push(r); });
  Object.values(byZip).forEach((z) => {
    const worst = Math.max(...z.carriers.map((c) => +c.avg_hot_hours));
    L.circle([z.lat, z.lon], { radius: 250 + worst * 180, color: "#d85a30", fillColor: "#d85a30", fillOpacity: 0.15 + Math.min(0.5, worst / 6) })
      .bindPopup(`<b>${esc(z.zip)} ${esc(z.name)}</b><br>${z.carriers.map((c) => `${esc(c.carrier)}: ${c.avg_hot_hours} h >30°C avg`).join("<br>")}`).addTo(porchLayer);
  });
  $("#porch-table").innerHTML = `<table><tr><th>ZIP</th><th>Carrier</th><th class="num">Deliveries</th><th class="num">Avg h &gt;30°C</th><th class="num">% over 2 h</th><th class="num">Peak mailbox</th></tr>
    ${rows.map((r) => `<tr class="${r.pct_over_2h > 40 ? "risk" : ""}"><td>${esc(r.zip)} <span class="muted small">${esc(r.name)}</span></td><td>${esc(r.carrier)}</td><td class="num">${r.deliveries}</td>
      <td class="num">${r.avg_hot_hours}</td><td class="num">${r.pct_over_2h}%</td><td class="num">${temp(+r.peak_mailbox_c)}</td></tr>`).join("")}</table>
    <p class="muted small">Mailbox temperature = air temperature + 12°C (10am–6pm) or +2°C otherwise. Modeling assumption.</p>`;
};
$("#brief-btn").addEventListener("click", (e) => busy(e.target, async () => {
  const r = await post("/api/porch/brief");
  $("#porch-brief").innerHTML = `<div class="ai"><div class="tag">Gemini pharmacy brief</div>${md(r.brief)}</div>`;
}));

// ------------------------------------------------------------------ demo controls
const log = (o) => { $("#demo-log").textContent = `${new Date().toLocaleTimeString()}  ${JSON.stringify(o, null, 1)}\n` + $("#demo-log").textContent; };
$$("[data-demo]").forEach((b) => b.addEventListener("click", () => busy(b, async () => {
  const r = await post(`/api/demo/${b.dataset.demo}`); log({ [b.dataset.demo]: r }); toast("Done"); await loadCompare(); health();
})));
$("#repair-btn").addEventListener("click", (e) => busy(e.target, async () => {
  const id = await insulinId(); const r = await post(`/api/items/${id}/repair`); log({ repair: r }); await loadCompare();
}));

async function insulinId() {
  const items = await api("/api/items");
  return (items.find((i) => /insulin/i.test(i.nickname)) || items[0])?.id;
}
async function loadCompare() {
  const id = await insulinId(); if (!id) return;
  const c = await api(`/api/items/${id}/compare`);
  const diff = c.aggregate.remaining - c.raw_truth.remaining;
  $("#compare").innerHTML = `<div class="stats">
    <div class="stat"><div class="v">${pct(c.aggregate.remaining)}</div><div class="k">dashboard (continuous aggregate)</div></div>
    <div class="stat"><div class="v">${pct(c.raw_truth.remaining)}</div><div class="k">recomputed from raw readings</div></div>
    <div class="stat"><div class="v">${Math.abs(diff) < 0.001 ? "in sync" : pct(diff)}</div><div class="k">overstatement</div></div>
    <div class="stat"><div class="v small">${c.watermark ? fmt(c.watermark) : "–"}</div><div class="k">materialization watermark</div></div></div>
    ${Math.abs(diff) >= 0.001 ? `<div class="banner bad">Late readings are hidden behind the watermark. The dashboard overstates remaining life by ${pct(diff)}.</div>` : `<div class="banner ok">Aggregate matches raw data.</div>`}`;
}
loaders.demo = loadCompare;

// ------------------------------------------------------------------ boot
health();
loadItems().catch((e) => { $("#item-list").innerHTML = `<p class="pad muted">${esc(e.message)}. Check .env and run scripts/setup_db.py.</p>`; });
