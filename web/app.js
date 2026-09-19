const $ = (s) => document.querySelector(s);
let holds = [];
let selected = null;

const money = (n) => (n == null ? "" : n.toLocaleString("en-US", { style: "currency", currency: "USD" }));

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
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
        : `<div class="block">
             <h4>Verification</h4>
             <div class="dial">We will dial <b>${d.phone_on_file}</b> — the number on file for this vendor,
               not a number from the email.</div>
             <button id="call">Place verification call</button>
             <div class="note">The email is the channel under attack, so the check has to leave it.</div>
           </div>`
    }

    <div class="block">
      <h4>The message</h4>
      <div class="email">${escapeHtml(d.body)}</div>
    </div>`;

  const btn = $("#call");
  if (btn)
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      btn.textContent = "Dialling…";
      await api(`/api/holds/${id}/verify`, { method: "POST" });
      await load();
      await showDetail(id);
    });
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
    <div class="dial">Dialled <b>${v.dialed_number}</b> — the number on file, not one from the email.</div>
    <div class="transcript">${transcript}</div>
    ${v.judge_quote ? `<div class="note">Judged on: “${escapeHtml(v.judge_quote)}”</div>` : ""}
    ${v.audio_path ? `<audio controls src="/api/recording/${v.audio_path.split("/").pop()}"></audio>` : ""}
  </div>`;
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

loadStatus();
api("/api/reset", { method: "POST" }).then(load);
