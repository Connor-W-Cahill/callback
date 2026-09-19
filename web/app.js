const $ = (s) => document.querySelector(s);
let holds = [];
let selected = null;

const money = (n) => (n == null ? "" : n.toLocaleString("en-US", { style: "currency", currency: "USD" }));

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

async function loadMailbox() {
  let m;
  try {
    m = await api("/api/mailbox");
  } catch (e) {
    return;
  }
  const bar = $("#mailbar");
  if (!m.address) {
    if (m.error) {
      bar.hidden = false;
      bar.innerHTML = `<span class="fail">Inbox unavailable: ${escapeHtml(m.error)}</span>`;
    }
    return;
  }
  bar.hidden = false;
  bar.innerHTML = `
    <span class="live-dot"></span>
    <span>Live inbox — email this address and it lands in the queue:</span>
    <code id="mailaddr">${escapeHtml(m.address)}</code>
    <button id="copyaddr" class="ghost">Copy</button>
    <span class="grow"></span>
    <span class="dim">${m.received} received · checking every ${m.poll_seconds}s</span>`;
  $("#copyaddr").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(m.address);
      $("#copyaddr").textContent = "Copied";
      setTimeout(() => ($("#copyaddr").textContent = "Copy"), 1500);
    } catch (e) {
      /* clipboard blocked; the address is on screen anyway */
    }
  });
}

async function loadStatus() {
  const s = await api("/api/status");
  $("#status").textContent = `nemotron: ${s.nemotron} · voice: ${s.elevenlabs}`;
}

async function load() {
  holds = await api("/api/holds");
  const msgs = await api("/api/messages");
  renderQueue(msgs);
  if (selected) {
    const still = holds.find((h) => h.id === selected);
    if (still) showDetail(selected);
  }
}

function renderQueue(msgs) {
  $("#count").textContent = holds.length ? `(${holds.length})` : "";
  $("#holds").innerHTML = holds
    .map((h) => {
      const high = h.score >= 0.6;
      const status = h.status === "held"
        ? `<span class="pill ${high ? "high" : "med"}">${high ? "high risk" : "review"}</span>`
        : `<span class="pill ${h.status}">${h.status}</span>`;
      return `<div class="card ${high ? "high" : ""} ${selected === h.id ? "sel" : ""}" data-id="${h.id}">
        <div class="vendor">${h.vendor_name || "Unrecognised vendor"} ${status}</div>
        <div class="sub">${h.subject || ""}</div>
        <div class="why">${h.rationale}</div>
      </div>`;
    })
    .join("");

  const cleared = msgs.filter((m) => m.status === "cleared");
  $("#cleared").innerHTML = cleared.length
    ? `<h2>Cleared — paid without friction</h2>` +
      cleared.map((m) => `<div class="row">${m.subject} <span style="float:right">${m.sender}</span></div>`).join("")
    : "";

  document.querySelectorAll(".card").forEach((c) =>
    c.addEventListener("click", () => showDetail(c.dataset.id))
  );
}

async function showDetail(id) {
  selected = id;
  document.querySelectorAll(".card").forEach((c) => c.classList.toggle("sel", c.dataset.id === id));
  const d = await api(`/api/holds/${id}`);
  const fired = d.signals.filter((s) => s.fired);
  const ver = d.verifications[0];

  $("#detail").innerHTML = `
    <h3>${d.vendor_name || "Unrecognised vendor"}</h3>
    <div class="meta">${d.subject} · from ${d.sender}${
      d.reply_to && d.reply_to !== d.sender ? ` · reply-to <b style="color:var(--danger)">${d.reply_to}</b>` : ""
    }</div>

    <div class="block">
      <h4>Why this was held · risk ${d.score}</h4>
      <div style="margin-bottom:14px">${d.rationale}</div>
      ${fired.map((s) => `<div class="sig"><span class="dot">▲</span><span>${s.detail}</span></div>`).join("")}
    </div>

    ${
      ver
        ? renderVerification(ver)
        : `<div class="block" id="callblock">
             <h4>Verification</h4>
             <div class="dial">We will dial <b>${d.phone_on_file}</b>${
               d.contact_name ? ` (${d.contact_name})` : ""
             } — the number on file for this vendor, not a number from the email.</div>
             <button id="call">Place verification call</button>
             <div class="note">The email is the channel under attack, so the check has to leave it.</div>
           </div>`
    }

    <div class="block">
      <h4>The message</h4>
      <div class="email">${escapeHtml(d.body)}</div>
    </div>`;

  const btn = $("#call");
  if (btn) btn.addEventListener("click", () => openCall(id));
}

// --- the call: agent speaks, then a human answers ------------------------

let recorder = null;
let chunks = [];

async function openCall(id) {
  const btn = $("#call");
  btn.disabled = true;
  btn.textContent = "Dialling…";
  const call = await api(`/api/holds/${id}/call`, { method: "POST" });

  $("#callblock").innerHTML = `
    <h4>Call in progress</h4>
    <div class="dial">Ringing <b>${call.dialed_number}</b>${
      call.contact_name ? ` — ${call.contact_name}` : ""
    }, the number on file. Not a number from the email.</div>
    <div class="transcript"><span class="agent">AGENT:</span> ${escapeHtml(call.agent_line)}</div>
    ${call.agent_audio_inline || call.agent_audio
        ? `<audio controls autoplay src="${call.agent_audio_inline || "/api/recording/" + call.agent_audio.split("/").pop()}"></audio>`
        : ""}
    <div class="answer">
      <h4 style="margin-top:18px">The vendor answers</h4>
      ${
        call.stt_available
          ? `<button id="rec">● Hold the mic — record the reply</button>
             <span id="recstate" class="note" style="margin-left:10px"></span>`
          : `<div class="note" style="margin-bottom:10px">No ELEVENLABS_API_KEY set, so speech-to-text is
             unavailable. Type the vendor's reply instead — the judge cannot tell the difference.</div>`
      }
      <textarea id="typed" rows="3" placeholder="…or type what the vendor says"></textarea>
      <div class="row-btns">
        <button id="submitreply">Submit reply</button>
        <button id="useseed" class="ghost">Use the scripted reply</button>
      </div>
    </div>`;

  const rec = $("#rec");
  if (rec) rec.addEventListener("click", () => toggleRecord(id));
  $("#submitreply").addEventListener("click", () => sendReply(id));
  $("#useseed").addEventListener("click", () => sendReply(id, { seeded: true }));
}

let recStartedAt = 0;

async function toggleRecord(id) {
  const btn = $("#rec");
  const state = $("#recstate");

  if (recorder && recorder.state === "recording") {
    recorder.stop();
    btn.textContent = "● Record the reply";
    btn.classList.remove("recording");
    state.textContent = "transcribing…";
    return;
  }

  // The agent's line autoplays. If it is still talking the mic records IT, and
  // the judge ends up reading our own question back -- which reads as unclear.
  document.querySelectorAll("audio").forEach((a) => {
    a.pause();
    a.currentTime = 0;
  });

  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (e) {
    state.innerHTML = '<span class="fail">microphone blocked — type the reply instead</span>';
    return;
  }

  chunks = [];
  try {
    recorder = new MediaRecorder(stream);
  } catch (e) {
    state.innerHTML = '<span class="fail">cannot record here — type the reply instead</span>';
    stream.getTracks().forEach((t) => t.stop());
    return;
  }

  recorder.ondataavailable = (e) => {
    if (e.data && e.data.size) chunks.push(e.data);
  };
  recorder.onstop = async () => {
    stream.getTracks().forEach((t) => t.stop());
    const blob = new Blob(chunks, { type: recorder.mimeType || "audio/webm" });
    const secs = (Date.now() - recStartedAt) / 1000;

    // A tiny blob means silence or a mis-selected input. Sending it produces a
    // garbage transcript and an "inconclusive" verdict that looks like the
    // product failing, when really nothing was recorded.
    if (blob.size < 2000 || secs < 0.8) {
      state.innerHTML =
        `<span class="fail">only ${secs.toFixed(1)}s / ${blob.size} bytes captured — ` +
        `check your input device, or type the reply.</span>`;
      return;
    }
    state.textContent = `sending ${(blob.size / 1024).toFixed(0)}KB…`;
    await sendReply(id, { blob });
  };

  recorder.start();
  recStartedAt = Date.now();
  btn.textContent = "■ Stop and transcribe";
  btn.classList.add("recording");
  state.innerHTML = '<span class="rec-dot"></span> listening — speak now';
}

async function sendReply(id, { blob, seeded } = {}) {
  const fd = new FormData();
  if (blob) fd.append("audio", blob, "reply.webm");
  else if (!seeded) {
    const typed = $("#typed").value.trim();
    if (typed) fd.append("text", typed);
  }

  let r;
  try {
    r = await fetch(`/api/holds/${id}/reply`, { method: "POST", body: fd });
  } catch (e) {
    showReplyError(`network error: ${e.message}`);
    return;
  }
  if (!r.ok) {
    let msg = await r.text();
    try {
      msg = JSON.parse(msg).detail || msg;
    } catch (e) {}
    showReplyError(msg);
    return;
  }
  await load();
  await showDetail(id);
}

function renderVerification(v) {
  const label = { denied: "Vendor denied the change", confirmed: "Vendor confirmed the change", unclear: "Inconclusive — escalated" }[v.judgment];
  const transcript = escapeHtml(v.transcript)
    .replace(/^AGENT:/gm, '<span class="agent">AGENT:</span>')
    .replace(/^VENDOR:/gm, '<span class="vendor">VENDOR:</span>');
  return `<div class="block">
    <h4>Verification call</h4>
    <div class="verdict">
      <span class="pill ${v.judgment === "denied" ? "blocked" : v.judgment === "confirmed" ? "approved" : "escalated"}">${v.judgment}</span>
      <strong>${label}</strong>
    </div>
    <div class="dial">Dialled <b>${v.dialed_number}</b> — the number on file, not one from the email.
      ${sourceLabel(v.reply_source)}</div>
    <div class="transcript">${transcript}</div>
    ${v.judgment === "unclear" ? `<div class="note warn">Inconclusive means the vendor neither confirmed nor
       denied — read the transcript above. If it shows our own agent line, the mic picked up the speaker;
       if it is empty or garbled, nothing was captured. Re-record, or type the reply.</div>` : ""}
    ${v.judge_quote ? `<div class="note">Judged on: “${escapeHtml(v.judge_quote)}”</div>` : ""}
    ${v.judge_reasoning ? `<div class="note">Reasoning: ${escapeHtml(v.judge_reasoning)}</div>` : ""}
    ${v.audio_path ? `<div class="note" style="margin-top:12px">Agent</div>
       <audio controls src="/api/recording/${v.audio_path.split("/").pop()}"></audio>` : ""}
    ${v.reply_audio_path ? `<div class="note">Vendor — what was actually said</div>
       <audio controls src="/api/recording/${v.reply_audio_path.split("/").pop()}"></audio>` : ""}
  </div>`;
}

function showReplyError(msg) {
  const state = $("#recstate");
  const block = $("#callblock");
  const html = `<span class="fail">${escapeHtml(msg)}</span>`;
  if (state) state.innerHTML = html;
  else if (block) block.insertAdjacentHTML("beforeend", `<div class="note">${html}</div>`);
}

function sourceLabel(src) {
  if (src === "spoken") return '<span class="src live">transcribed from speech</span>';
  if (src === "typed") return '<span class="src">typed reply</span>';
  return '<span class="src">scripted reply</span>';
}

function escapeHtml(s) {
  return (s || "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

$("#reset").addEventListener("click", async () => {
  selected = null;
  $("#detail").innerHTML = '<div class="empty">Select a held payment to see why it was stopped.</div>';
  await api("/api/reset", { method: "POST" });
  await load();
});

// Seed on first load only. A reset costs ~8 Nemotron calls and a page load is
// free to anyone with the URL, so resetting unconditionally lets a crawler or a
// link-preview bot drain the credits this demo runs on. The Reset button is
// still there for a deliberate re-run.
async function boot() {
  await loadStatus();
  await loadMailbox();
  try {
    const holds = await api("/api/holds");
    if (!holds.length) await api("/api/reset", { method: "POST" });
  } catch (e) {
    /* fall through to load() and show whatever state exists */
  }
  await load();
}

boot();

// New mail arrives on its own schedule, so the queue has to notice.
setInterval(async () => {
  if (!$("#view-engine").hidden) return;
  const before = holds.length;
  await loadMailbox();
  await load();
  if (holds.length > before) {
    const bar = $("#mailbar");
    bar.classList.add("flash");
    setTimeout(() => bar.classList.remove("flash"), 1200);
  }
}, 5000);


// --- "Under the hood": what Nemotron and ElevenLabs actually did -----------

// Keyed in pipeline order: an email is extracted and scored, the agent speaks,
// the vendor answers, and only then is the transcript judged.
const JOBS = {
  extract: ["1. Extract", "Unstructured email → structured JSON. Nemotron."],
  score: ["2. Score", "Weigh the precomputed signals, write the clerk's rationale. Nemotron."],
  tts: ["3. Speak", "Render the agent's question so a human can hear it. ElevenLabs."],
  stt: ["4. Listen", "Turn what the vendor actually said into words. ElevenLabs."],
  judge: ["5. Judge", "Read the transcript → confirmed / denied / unclear. Nemotron, model-as-judge."],
};

function switchView(view) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === view));
  $("#view-queue").hidden = view !== "queue";
  $("#view-vendors").hidden = view !== "vendors";
  $("#view-engine").hidden = view !== "engine";
  if (view === "engine") renderEngine();
  if (view === "vendors") renderVendors();
}

// --- vendor master -------------------------------------------------------

let vendors = [];
let editing = null;

async function renderVendors() {
  vendors = await api("/api/vendors");
  $("#view-vendors").innerHTML = `
    <div class="engine-inner">
      <h2>Vendor master</h2>
      <p class="lede">The record every inbound email is checked against. The bank
        account here is what a payment-change request gets compared to — edit it and
        the same email scores differently. Add a vendor whose domain matches a real
        address to make mail from it resolve to a known supplier instead of a stranger.</p>
      <button id="addvendor">Add vendor</button>
      <div class="vlist">${vendors.map(vendorCard).join("")}</div>
    </div>`;

  $("#addvendor").addEventListener("click", () => {
    editing = "__new__";
    renderVendorForm();
  });
  document.querySelectorAll("[data-edit]").forEach((b) =>
    b.addEventListener("click", () => {
      editing = b.dataset.edit;
      renderVendorForm();
    })
  );
  document.querySelectorAll("[data-del]").forEach((b) =>
    b.addEventListener("click", async () => {
      const v = vendors.find((x) => x.id === b.dataset.del);
      if (!confirm(`Delete ${v.name}? Its payment history and any holds go too.`)) return;
      await fetch(`/api/vendors/${b.dataset.del}`, { method: "DELETE" });
      await renderVendors();
      await load();
    })
  );
}

function vendorCard(v) {
  return `<div class="vcard${v.custom ? " custom" : ""}">
    <div class="vhead">
      <b>${escapeHtml(v.name)}</b>
      ${v.custom ? '<span class="pill held">edited</span>' : ""}
      <span class="grow"></span>
      <button class="ghost" data-edit="${v.id}">Edit</button>
      <button class="ghost" data-del="${v.id}">Delete</button>
    </div>
    <div class="vgrid">
      <div><span>Account on file</span><code>${escapeHtml(v.account)}</code></div>
      <div><span>Routing</span><code>${escapeHtml(v.routing || "—")}</code></div>
      <div><span>Phone we call</span><code>${escapeHtml(v.phone_on_file || "—")}</code></div>
      <div><span>Domains</span>${(v.domains || []).map((d) => `<code>${escapeHtml(d)}</code>`).join(" ") || "—"}</div>
      <div class="wide"><span>Known senders</span>${(v.known_senders || []).map((d) => `<code>${escapeHtml(d)}</code>`).join(" ") || "—"}</div>
      <div><span>Payment history</span>${v.payments} payments</div>
    </div>
  </div>`;
}

function renderVendorForm() {
  const isNew = editing === "__new__";
  const v = isNew
    ? { id: "", name: "", domains: [], known_senders: [], phone_on_file: "", contact_name: "",
        account: "", routing: "", country: "US", bank_name: "", category: "" }
    : vendors.find((x) => x.id === editing);

  $("#view-vendors").innerHTML = `
    <div class="engine-inner">
      <h2>${isNew ? "Add a vendor" : "Edit " + escapeHtml(v.name)}</h2>
      <div class="vform">
        ${field("name", "Vendor name", v.name, "Allegheny Sheet Metal")}
        ${field("contact_name", "Contact", v.contact_name || "", "Denise Perkins")}
        ${field("account", "Bank account on file", v.account, "8841003392",
                "The signal everything hangs on: a request to pay a different account is what gets held.")}
        ${field("routing", "Routing", v.routing || "", "043000096")}
        ${field("phone_on_file", "Phone to call for verification", v.phone_on_file || "", "+1-412-555-0142",
                "Never taken from the email. This is the number the voice agent dials.")}
        ${field("domains", "Domains", (v.domains || []).join(", "), "alleghenysheetmetal.com",
                "Comma separated. Mail from a near-miss of one of these is flagged as a lookalike.")}
        ${field("known_senders", "Known sender addresses", (v.known_senders || []).join(", "),
                "billing@alleghenysheetmetal.com", "Comma separated. Anything else is a first-contact signal.")}
        ${field("bank_name", "Bank", v.bank_name || "", "Dollar Bank")}
        ${field("country", "Country", v.country || "US", "US")}
        <div class="row-btns">
          <button id="savevendor">${isNew ? "Create vendor" : "Save changes"}</button>
          <button id="cancelvendor" class="ghost">Cancel</button>
          <span id="vmsg" class="note"></span>
        </div>
      </div>
    </div>`;

  $("#cancelvendor").addEventListener("click", () => {
    editing = null;
    renderVendors();
  });
  $("#savevendor").addEventListener("click", async () => {
    const body = {};
    document.querySelectorAll("[data-f]").forEach((i) => (body[i.dataset.f] = i.value));
    const r = await fetch(isNew ? "/api/vendors" : `/api/vendors/${editing}`, {
      method: isNew ? "POST" : "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      let m = await r.text();
      try { m = JSON.parse(m).detail || m; } catch (e) {}
      $("#vmsg").innerHTML = `<span class="fail">${escapeHtml(m)}</span>`;
      return;
    }
    editing = null;
    await renderVendors();
    await load();
  });
}

function field(name, label, value, placeholder, help) {
  return `<label class="vfield">
    <span>${label}</span>
    <input data-f="${name}" value="${escapeHtml(String(value ?? ""))}" placeholder="${escapeHtml(placeholder)}">
    ${help ? `<em>${escapeHtml(help)}</em>` : ""}
  </label>`;
}

async function renderEngine() {
  const d = await api("/api/activity");
  const el = $("#view-engine");
  const c = d.config;

  const nem = d.summary.filter((s) => s.service === "nemotron");
  const el11 = d.summary.filter((s) => s.service === "elevenlabs");

  el.innerHTML = `
    <div class="engine-inner">
      <h2>What the models are actually doing</h2>
      <p class="lede">Every call to Nemotron and ElevenLabs, as it happened. Nothing here is
        a mock — if a row says it succeeded, that request went out and came back.</p>

      <div class="svc">
        <h3>Nemotron <span class="${c.nemotron_live ? "ok" : "off"}">${c.nemotron_live ? "live" : "offline — using deterministic fallback"}</span></h3>
        <div class="chain">Chain, tried in order then falling back to rules:
          ${c.nemotron_chain.map((m, i) => `<code>${i + 1}. ${m.split("/").pop()}</code>`).join(" ")}
          <span class="note">timeout ${c.timeout_s}s</span></div>
        ${summaryTable(nem)}
      </div>

      <div class="svc">
        <h3>ElevenLabs <span class="${c.elevenlabs_live ? "ok" : "off"}">${c.elevenlabs_live ? "live" : "simulated"}</span></h3>
        <div class="chain">Voice <code>${c.voice_id}</code>
          <span class="note">${d.elevenlabs_chars_used_this_session.toLocaleString()} characters spent this session</span></div>
        ${summaryTable(el11)}
      </div>

      <h3 style="margin-top:28px">Call log <span class="note">newest first</span></h3>
      <div class="log">${d.calls.length ? d.calls.map(callRow).join("") : '<div class="empty">No calls yet — reset the demo or place a verification call.</div>'}</div>
    </div>`;

  el.querySelectorAll(".logrow").forEach((r) =>
    r.addEventListener("click", () => r.classList.toggle("open"))
  );
}

function summaryTable(rows) {
  if (!rows.length) return '<div class="note">No calls yet.</div>';
  return `<table class="sum">
    <tr><th>Job</th><th>What it does</th><th>Calls</th><th>Avg</th><th>Model</th></tr>
    ${rows.map((r) => {
      const [name, desc] = JOBS[r.job] || [r.job, ""];
      const models = Object.entries(r.models).map(([m, n]) => `${m.split("/").pop()} ×${n}`).join(", ");
      return `<tr>
        <td><b>${name}</b></td>
        <td class="desc">${desc}</td>
        <td>${r.ok}/${r.calls}${r.failed ? ` <span class="fail">${r.failed} failed</span>` : ""}</td>
        <td>${(r.avg_ms / 1000).toFixed(1)}s</td>
        <td class="mono">${models || "—"}</td>
      </tr>`;
    }).join("")}
  </table>`;
}

function callRow(c) {
  const [name] = JOBS[c.job] || [c.job];
  const t = new Date(c.at).toLocaleTimeString();
  return `<div class="logrow ${c.ok ? "" : "bad"}">
    <div class="head">
      <span class="badge ${c.service}">${c.service}</span>
      <b>${name}</b>
      <span class="mono dim">${(c.model || "").split("/").pop()}</span>
      <span class="grow"></span>
      <span class="dim">${(c.ms / 1000).toFixed(1)}s</span>
      <span class="${c.ok ? "ok" : "fail"}">${c.ok ? "ok" : "failed"}</span>
      <span class="dim">${t}</span>
    </div>
    ${c.detail ? `<div class="err">${escapeHtml(c.detail)}</div>` : ""}
    <div class="body">
      ${c.prompt_excerpt ? `<div class="pane"><h5>Sent</h5><pre>${escapeHtml(c.prompt_excerpt)}</pre></div>` : ""}
      ${c.response_excerpt ? `<div class="pane"><h5>Returned</h5><pre>${escapeHtml(c.response_excerpt)}</pre></div>` : ""}
    </div>
  </div>`;
}

document.querySelectorAll(".tab").forEach((t) =>
  t.addEventListener("click", () => switchView(t.dataset.view))
);
