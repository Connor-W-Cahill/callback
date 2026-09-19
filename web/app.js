const $ = (s) => document.querySelector(s);
let selected = null;
let demoMode = true;
const emptyDeskMarkup = $("#detail").innerHTML;

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  $("#dark-mode").checked = theme === "dark";
  $('meta[name="theme-color"]').content = theme === "dark" ? "#191b19" : "#f5f2eb";
  try { localStorage.setItem("callback-theme", theme); } catch { /* Still works for this visit. */ }
}

$("#dark-mode").checked = document.documentElement.dataset.theme !== "light";
$("#dark-mode").addEventListener("change", event => setTheme(event.target.checked ? "dark" : "light"));

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
    <span class="mail-label">Receiving at</span>
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
  demoMode = s.demo_mode;
  $("#reset").hidden = !demoMode;
  $("#status").textContent = demoMode ? "Demo workspace" : "Live workspace";
  $("#status").title = `Nemotron: ${s.nemotron} · Voice: ${s.elevenlabs}`;
}

let board = { open: { needs_review: [], calling: [], escalated: [] },
              settled: { accepted: [], denied: [] }, extraneous: [] };
let bucket = "open:needs_review";

const BUCKETS = [
  ["open", "Open", [
    ["needs_review", "Needs review", "Payment-detail changes awaiting verification. Flagged when we cannot tell who sent it."],
    ["calling", "Calling", "A verification call is in flight."],
    ["escalated", "Escalated", "The call settled nothing. A human decides."],
  ]],
  ["settled", "Settled", [
    ["accepted", "Accepted", "Cleared for payment, or the banking change was confirmed by the vendor."],
    ["denied", "Denied", "The vendor denied it. The payment is blocked."],
  ]],
  ["extraneous", "Extraneous", null],
];

function bucketItems(key) {
  const [top, sub] = key.split(":");
  return sub ? (board[top] && board[top][sub]) || [] : board[top] || [];
}

function bucketBlurb(key) {
  const [top, sub] = key.split(":");
  if (top === "extraneous") return "Mail that was never about money. It never reached the fraud checks.";
  const grp = BUCKETS.find((b) => b[0] === top);
  const found = (grp && grp[2] || []).find((x) => x[0] === sub);
  return found ? found[2] : "";
}

function countOf(key) {
  return bucketItems(key).length;
}

async function load() {
  board = await api("/api/board");
  $("#queue-summary").innerHTML = [
    ["open:needs_review", "Needs review"], ["open:calling", "On a call"], ["open:escalated", "Escalated"],
  ].map(([key, label]) => `<div><strong>${String(countOf(key)).padStart(2, "0")}</strong><span>${label}</span></div>`).join("");
  renderSubtabs();
  renderBucket();
}

function renderSubtabs() {
  const curTop = bucket.split(":")[0];
  let html = BUCKETS.map(function (b) {
    const top = b[0], label = b[1], subs = b[2];
    const total = subs
      ? subs.reduce((n, x) => n + countOf(top + ":" + x[0]), 0)
      : countOf(top);
    const firstKey = subs ? top + ":" + subs[0][0] : top;
    return '<button class="stab ' + (top === curTop ? "active" : "") + '" data-b="' + firstKey + '" aria-pressed="' + (top === curTop) + '">' +
      label + (total ? ' <span class="n">' + total + "</span>" : "") + "</button>";
  }).join("");

  const grp = BUCKETS.find((b) => b[0] === curTop);
  if (grp && grp[2]) {
    html += '<div class="subsub">' + grp[2].map(function (x) {
      const key = curTop + ":" + x[0];
      const n = countOf(key);
      return '<button class="ssub ' + (bucket === key ? "active" : "") + '" data-b="' + key + '" aria-pressed="' + (bucket === key) + '">' +
        x[1] + (n ? ' <span class="n">' + n + "</span>" : "") + "</button>";
    }).join("") + "</div>";
  }
  $("#subtabs").innerHTML = html;

  document.querySelectorAll("[data-b]").forEach((b) =>
    b.addEventListener("click", () => {
      bucket = b.dataset.b;
      selected = null;
      $("#detail").innerHTML = emptyDeskMarkup;
      renderSubtabs();
      renderBucket();
    })
  );
}

function renderBucket() {
  const items = bucketItems(bucket);
  $("#holds").innerHTML =
    '<p class="blurb">' + bucketBlurb(bucket) + "</p>" +
    (items.length
      ? items.map(card).join("")
      : '<div class="empty">No items in this section. New requests appear here as they arrive.</div>');

  document.querySelectorAll(".card").forEach((c) =>
    c.addEventListener("click", () => {
      const action = c.dataset.kind === "hold" ? showDetail(c.dataset.id) : showMessage(c.dataset.id);
      action.then(() => {
        if (window.matchMedia("(max-width: 720px)").matches) {
          $("#detail").scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth", block: "start" });
        }
      }).catch(showError);
    })
  );
}

function card(h) {
  const high = h.score >= 0.6;
  const unknown = h.kind === "hold" && !h.identified;
  const pills = {
    held: unknown
      ? '<span class="pill med">unidentified sender</span>'
      : '<span class="pill ' + (high ? "high" : "med") + '">' + (high ? "high risk" : "review") + "</span>",
    verifying: '<span class="pill held">verifying…</span>',
    calling: '<span class="pill held">calling…</span>',
    escalated: '<span class="pill escalated">escalated</span>',
    approved: '<span class="pill approved">accepted</span>',
    blocked: '<span class="pill blocked">denied</span>',
    cleared: '<span class="pill approved">cleared</span>',
    extraneous: "",
  };
  const pill = pills[h.status] || "";
  return `<button type="button" class="card ${high && h.status === "held" ? "high" : ""} ${unknown ? "unknown" : ""} ${selected === h.id ? "sel" : ""}"
    data-id="${escapeHtml(h.id)}" data-kind="${escapeHtml(h.kind)}" aria-pressed="${selected === h.id}">
    <span class="card-top"><span class="card-ref">${escapeHtml(h.id.replace(/^hold-/, ""))}</span>${pill}</span>
    <span class="vendor">${escapeHtml(h.vendor_name || senderName(h.sender))}</span>
    <span class="sub">${escapeHtml(h.subject || "(no subject)")}</span>
    ${h.rationale ? `<span class="why">${escapeHtml(h.rationale)}</span>` : ""}
  </button>`;
}

function senderName(s) {
  return (s || "unknown").split("@")[0];
}

async function showMessage(id) {
  selected = id;
  const msgs = await api("/api/messages");
  if (selected !== id) return;
  const m = msgs.find((x) => x.id === id) || {};
  $("#detail").innerHTML =
    '<div class="detail-head"><p class="eyebrow">Correspondence</p>' +
    "<h3>" + escapeHtml(senderName(m.sender)) + "</h3>" +
    '<div class="meta">' + escapeHtml(m.subject || "") + " · from " + escapeHtml(m.sender || "") + "</div></div>" +
    '<div class="block"><h4>Why it is here</h4><div>' +
    (m.status === "extraneous"
      ? "Nothing in this message concerns money, so it never reached the fraud checks."
      : "This invoice cleared the checks and is ready for payment. No payment has been sent.") +
    "</div></div>";
}

async function showDetail(id) {
  selected = id;
  document.querySelectorAll(".card").forEach((c) => {
    c.classList.toggle("sel", c.dataset.id === id);
    c.setAttribute("aria-pressed", String(c.dataset.id === id));
  });
  const d = await api(`/api/holds/${id}`);
  const fired = d.signals.filter((s) => s.fired);
  if (selected !== id) return;
  const ver = d.verifications[0];
  const account = value => value ? `•••• ${escapeHtml(String(value).slice(-4))}` : "Not specified";
  const signalRow = signal => `<div class="sig"><span class="dot" aria-hidden="true">↳</span><span>${escapeHtml(signal.detail)}</span></div>`;

  $("#detail").innerHTML = `
    <div class="detail-head">
      <div class="case-caption"><span class="folio">Review / ${escapeHtml(id.replace(/^hold-/, ""))}</span><span class="folio">${escapeHtml(d.status)}</span></div>
      <h3>${escapeHtml(d.vendor_name || "Unrecognised vendor")}</h3>
      <div class="meta-subject">${escapeHtml(d.subject)}</div>
      <div class="meta">From ${escapeHtml(d.sender)}${
        d.reply_to && d.reply_to !== d.sender ? ` · Reply to <span class="fail">${escapeHtml(d.reply_to)}</span>` : ""
      }</div>
    </div>

    <div class="account-comparison" aria-label="Payment account comparison">
      <div><span>Account on file</span><strong>${account(d.vendor_account)}</strong></div>
      <span class="comparison-arrow" aria-hidden="true">→</span>
      <div><span>Account requested</span><strong>${account(d.extracted?.bank_account)}</strong></div>
      ${d.extracted?.amount != null ? `<div class="invoice-amount"><span>Invoice amount</span><strong>${money(d.extracted.amount)}</strong></div>` : ""}
    </div>
    <div class="block">
      <h4><span class="section-no">01</span>Review notes <span class="risk-readout">Risk ${d.score}</span></h4>
      <div class="rationale">${escapeHtml(d.rationale)}</div>
      ${fired.slice(0, 2).map(signalRow).join("")}
      ${fired.length > 2 ? `<details class="more-signals"><summary>${fired.length - 2} more signal${fired.length === 3 ? "" : "s"}</summary>${fired.slice(2).map(signalRow).join("")}</details>` : ""}
    </div>

    ${
      (ver ? renderVerification(ver) : "") +
      (["held", "escalated"].includes(d.status) ? `<div class="block" id="callblock">
             <h4><span class="section-no">02</span>Verify with the vendor</h4>
             <div class="dial">On-file number <b>${escapeHtml(d.phone_on_file)}</b>${
               d.contact_name ? ` (${escapeHtml(d.contact_name)})` : ""
             }. This contact comes from your vendor record.</div>
             <button id="call">Place verification call</button>
             <div class="note">Confirm the request and the payment details before making a change.</div>
           </div>` : `<div class="note">${["calling", "verifying"].includes(d.status) ? "Verification in progress…" : "Verification complete."}</div>`)
    }

    <div class="block">
      <h4><span class="section-no">03</span>Original message</h4>
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
  let call;
  try {
    if (!demoMode) {
      await api(`/api/holds/${id}/dial`, { method: "POST" });
      await load();
      await showDetail(id);
      return;
    }
    call = await api(`/api/holds/${id}/call`, { method: "POST" });
  } catch (e) {
    btn.disabled = false;
    btn.textContent = "Retry verification call";
    showError(e);
    return;
  }

  if (selected !== id) return;
  $("#callblock").innerHTML = `
    <h4>Demo verification</h4>
    <div class="dial">Contact on file <b>${escapeHtml(call.dialed_number)}</b>${
      call.contact_name ? ` — ${escapeHtml(call.contact_name)}` : ""
    }, the number on file. Not a number from the email.</div>
    <div class="transcript"><span class="agent">AGENT:</span> ${escapeHtml(call.agent_line)}</div>
    ${call.agent_audio_inline || call.agent_audio
        ? `<audio controls autoplay src="${call.agent_audio_inline || "/api/recording/" + call.agent_audio.split("/").pop()}"></audio>`
        : ""}
    <div class="answer">
      <h4 style="margin-top:18px">The vendor answers</h4>
      ${
        call.stt_available
          ? `<button id="rec">Record the reply</button>
             <span id="recstate" class="note" style="margin-left:10px"></span>`
          : `<div class="note" style="margin-bottom:10px">Speech recording is unavailable in this demo. Enter the vendor’s reply below.</div>`
      }
      <label class="reply-label" for="typed">Vendor’s reply</label>
      <textarea id="typed" rows="3" placeholder="Enter the vendor’s exact words…"></textarea>
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
    if (!typed) {
      showReplyError("Enter a reply, record one, or choose the scripted reply.");
      return;
    }
    fd.append("text", typed);
  }

  const buttons = [...document.querySelectorAll("#callblock button")];
  buttons.forEach(button => button.disabled = true);
  const submit = $("#submitreply");
  if (submit) submit.textContent = "Reviewing reply…";
  try {
    const r = await fetch(`/api/holds/${id}/reply`, { method: "POST", body: fd });
    if (!r.ok) {
      let msg = await r.text();
      try { msg = JSON.parse(msg).detail || msg; } catch {}
      throw new Error(msg);
    }
    await load();
    if (selected === id) await showDetail(id);
  } catch (e) {
    if (selected === id) showReplyError(e.message);
    else showError(e);
  } finally {
    buttons.forEach(button => button.disabled = false);
    if (submit) submit.textContent = "Submit reply";
  }
}

function renderVerification(v) {
  const label = { denied: "Vendor denied the change", confirmed: "Vendor confirmed the change", unclear: "Inconclusive — escalated" }[v.judgment];
  const transcript = escapeHtml(v.transcript)
    .replace(/^AGENT:/gm, '<span class="agent">AGENT:</span>')
    .replace(/^VENDOR:/gm, '<span class="vendor">VENDOR:</span>');
  return `<div class="block">
    <h4><span class="section-no">02</span>Verification record</h4>
    <div class="verdict">
      <span class="pill ${v.judgment === "denied" ? "blocked" : v.judgment === "confirmed" ? "approved" : "escalated"}">${v.judgment}</span>
      <strong>${label}</strong>
    </div>
    <div class="dial">Dialled <b>${escapeHtml(v.dialed_number)}</b> — the number on file, not one from the email.
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
  if (src === "phone") return '<span class="src live">recorded phone call</span>';
  if (src === "spoken") return '<span class="src live">transcribed from speech</span>';
  if (src === "typed") return '<span class="src">typed reply</span>';
  return '<span class="src">scripted reply</span>';
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function showError(error) {
  const el = $("#error");
  el.textContent = `Could not complete the request: ${error.message || error}`;
  el.hidden = false;
}

$("#reset").addEventListener("click", async () => {
  const button = $("#reset");
  button.disabled = true;
  button.textContent = "Resetting…";
  try {
    selected = null;
    $("#detail").innerHTML = emptyDeskMarkup;
    await api("/api/reset", { method: "POST" });
    await load();
    $("#error").hidden = true;
  } catch (e) {
    showError(e);
  } finally {
    button.disabled = false;
    button.textContent = "Reset demo";
  }
});

// A reload shows what actually happened -- nothing more. The queue fills from
// real email arriving in the inbox; the Reset button loads the canned demo when
// you deliberately want it.
async function boot() {
  await loadStatus();
  await loadMailbox();
  await load();
}

boot().catch(showError);

// New mail arrives on its own schedule, so the queue has to notice.
let polling = false;
setInterval(async () => {
  if (polling || !$("#view-engine").hidden) return;
  polling = true;
  try {
    const before = countOf("open:needs_review") + countOf("open:calling");
    await loadMailbox();
    await load();
    if (!demoMode && selected?.startsWith("hold-")) await showDetail(selected);
    if (countOf("open:needs_review") + countOf("open:calling") > before) {
      const bar = $("#mailbar");
      bar.classList.add("flash");
      setTimeout(() => bar.classList.remove("flash"), 1200);
    }
  } catch (e) { showError(e); }
  finally { polling = false; }
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
  document.querySelectorAll(".tab").forEach((t) => {
    t.classList.toggle("active", t.dataset.view === view);
    if (t.dataset.view === view) t.setAttribute("aria-current", "page");
    else t.removeAttribute("aria-current");
  });
  $("#view-queue").hidden = view !== "queue";
  $("#view-vendors").hidden = view !== "vendors";
  $("#view-engine").hidden = view !== "engine";
  if (view === "engine") renderEngine().catch(showError);
  if (view === "vendors") renderVendors().catch(showError);
}

// --- vendor master -------------------------------------------------------

let vendors = [];
let editing = null;

async function renderVendors() {
  vendors = await api("/api/vendors");
  $("#view-vendors").innerHTML = `
    <div class="engine-inner">
      <div class="section-heading"><div><p class="eyebrow">The source of truth</p><h2>Vendor master</h2>
      <p class="lede">Known contacts and payment details. Every incoming request is checked against this record.</p></div>
      ${demoMode ? '<button id="addvendor">Add vendor <span aria-hidden="true">+</span></button>' : '<span class="folio">Read-only in live mode</span>'}</div>
      <div class="vlist">${vendors.map(vendorCard).join("")}</div>
    </div>`;

  $("#addvendor")?.addEventListener("click", () => {
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
      await api(`/api/vendors/${b.dataset.del}`, { method: "DELETE" });
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
      ${demoMode ? `<button class="ghost" data-edit="${escapeHtml(v.id)}">Edit</button>
      <button class="ghost" data-del="${escapeHtml(v.id)}">Delete</button>` : ""}
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
      <p class="eyebrow">Vendor record</p>
      <h2>${isNew ? "Add a vendor" : "Edit " + escapeHtml(v.name)}</h2>
      <div class="vform">
        ${field("name", "Vendor name", v.name, "Allegheny Sheet Metal")}
        ${field("contact_name", "Contact", v.contact_name || "", "Denise Perkins")}
        ${field("account", "Bank account on file", v.account, "8841003392",
                "Payment requests are checked against this account.")}
        ${field("routing", "Routing", v.routing || "", "043000096")}
        ${field("phone_on_file", "Phone to call for verification", v.phone_on_file || "", "+1-412-555-0142",
                "Use a trusted contact. Verification calls go to this number.")}
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
      <div class="section-heading"><div><p class="eyebrow">Service journal</p><h2>Under the hood</h2>
      <p class="lede">A record of the services behind each decision. Requests, responses, and the time between.</p></div><span class="folio">${d.calls.length} recorded attempts</span></div>
      ${outcomeBar(d.outcome)}

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

  el.querySelectorAll(".logrow").forEach((r) => {
    const toggle = () => {
      r.classList.toggle("open");
      r.setAttribute("aria-expanded", String(r.classList.contains("open")));
    };
    r.addEventListener("click", toggle);
    r.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); toggle(); }
    });
  });
}

function outcomeBar(o) {
  if (!o || !o.requests) return "";
  const pct = (n) => Math.round((n / o.requests) * 100);
  return `<div class="outcome">
    <div><b>${o.requests}</b> requests <span class="dim">from ${o.attempts} attempts</span></div>
    <div class="obar">
      <span class="ok" style="width:${pct(o.first_try)}%"></span>
      <span class="rec" style="width:${pct(o.recovered)}%"></span>
      <span class="bad" style="width:${pct(o.failed)}%"></span>
    </div>
    <div class="okey">
      <span><i class="sw ok"></i>${o.first_try} first try</span>
      <span><i class="sw rec"></i>${o.recovered} recovered after retry</span>
      <span><i class="sw bad"></i>${o.failed} fell back to rules</span>
    </div>
    <p class="note">Retries are deliberate. NVIDIA's free tier returns 503s and 429s in
      well under a second, so a retried call costs little — what matters is the
      right-hand number.</p>
  </div>`;
}

function summaryTable(rows) {
  if (!rows.length) return '<div class="note">No calls yet.</div>';
  return `<div class="table-scroll"><table class="sum">
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
  </table></div>`;
}

function callRow(c) {
  const [name] = JOBS[c.job] || [c.job];
  const t = new Date(c.at).toLocaleTimeString();
  return `<div class="logrow ${c.ok ? "" : "bad"}" role="button" tabindex="0" aria-expanded="false">
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
