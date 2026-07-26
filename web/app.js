/* ARC/180 frontend — renders real state from /api/state */
"use strict";

let USER = "";              // both resolved from the server at boot, which reads
let RIVAL = "";             // the knowledge base's whoami file
let S = null;               // last state from the server
const cap = s => s.charAt(0).toUpperCase() + s.slice(1);
const IC = () => cap(USER)[0], RIC = () => cap(RIVAL)[0];
let provider = "claude";
let currentConvo = null;
let localChecks = [];       // local check state when the KB has no subtasks yet

const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.remove("show"), 2600);
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
  return r.json();
}

/* ---------------- navigation ---------------- */
document.querySelectorAll("[data-go]").forEach(btn => {
  btn.addEventListener("click", () => {
    const id = btn.dataset.go;
    document.querySelectorAll(".screen").forEach(s => s.classList.toggle("on", s.id === id));
    document.querySelectorAll(".tab[data-go],.sb-item[data-go],.chat-fab").forEach(b =>
      b.classList.toggle("active", b.dataset.go === id));
    window.scrollTo({ top: 0 });
  });
});

/* ---------------- render ---------------- */
function greeting() {
  const h = new Date().getHours();
  return h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening";
}

function pad3(n) { return String(n).padStart(3, "0"); }

function render() {
  const me = S.users[USER], rv = S.users[RIVAL], t = S.today;
  $("loading").style.display = "none";
  if (!document.querySelector(".screen.on")) $("today").classList.add("on");

  $("day-chip").innerHTML =
    `DAY <b>${pad3(t.day_n)}</b>/${t.plan_days} · 🔥 <b>${me.streak}</b>`;
  $("sb-av").textContent = IC();
  $("sb-name").textContent = cap(USER);
  if (S.kb_name) {
    $("chat-eyebrow").textContent = `Grounded in ${S.kb_name}`;
    $("notes-eyebrow").textContent = `Two-way sync with ${S.kb_name}`;
  }

  /* overview */
  $("greet-title").textContent = `${greeting()}, ${cap(USER)}`;
  $("greet-sub").textContent =
    `${t.pretty} · Day ${pad3(t.day_n)}/${t.plan_days}` +
    (t.week ? ` · Week ${t.week}` : "") +
    (t.week_title ? ` — ${t.week_title.replace(/^Week \d+ — /, "")}` : "");
  $("mission-eyebrow").textContent =
    (t.kind === "build" ? "Build day · " : t.kind === "rest" ? "Rest day · " : "Today's mission · ") +
    (t.month_title || "The course");
  $("mission-xp").textContent = t.scheduled ? `+${t.xp} XP` : "+0 XP";
  $("mission-title").textContent = t.title;
  $("mission-theme").textContent = t.week_title || "Off the schedule — recover, review, wander the wiki.";
  $("mission-task").textContent = t.task;

  const checksEl = $("mission-checks");
  checksEl.innerHTML = "";
  if (t.subtasks.length) {
    t.subtasks.forEach(st => {
      const b = document.createElement("div");
      b.className = "check kb" + (st.done ? " done" : "");
      b.innerHTML = `<span class="box">✓</span><span class="lbl">${esc(st.text)}</span><span class="src">FROM KB</span>`;
      checksEl.appendChild(b);
    });
  } else if (t.scheduled && !t.done) {
    if (!localChecks.length) localChecks = [
      { text: "Studied the topic", done: false },
      { text: "Deliverable filed to the wiki", done: false },
    ];
    localChecks.forEach((st, i) => {
      const b = document.createElement("button");
      b.className = "check" + (st.done ? " done" : "");
      b.innerHTML = `<span class="box">✓</span><span class="lbl">${esc(st.text)}</span>`;
      b.addEventListener("click", () => { localChecks[i].done = !localChecks[i].done; render(); });
      checksEl.appendChild(b);
    });
  }

  const btn = $("complete-btn"), msg = $("done-msg");
  if (t.done) {
    btn.style.display = "none";
    msg.style.display = "block";
    msg.textContent = `DAY ${pad3(t.day_n)} CLAIMED · 🔥 STREAK ${me.streak} · +${t.xp} XP`;
  } else if (!t.scheduled) {
    btn.style.display = "none";
    msg.style.display = "block";
    msg.textContent = "REST DAY — NOTHING TO CLAIM. SEE YOU TOMORROW.";
  } else {
    btn.style.display = "block";
    msg.style.display = "none";
    const kbDone = t.subtasks.length && t.subtasks.every(s => s.done);
    const localDone = !t.subtasks.length && localChecks.length && localChecks.every(s => s.done);
    btn.disabled = !(kbDone || localDone);
    btn.textContent = btn.disabled ? "Finish the checklist to claim the day"
                                   : `Claim Day ${pad3(t.day_n)} · +${t.xp} XP`;
  }

  const ping = $("rival-ping");
  const rvName = cap(RIVAL);
  const rvDoneToday = S.calendar.days[t.date] && S.calendar.days[t.date].status[RIVAL];
  if (rvDoneToday) {
    ping.innerHTML = `<span class="avatar">${RIC()}</span>
      <p><strong>${rvName} already claimed today.</strong>
      <span class="t">Claim yours before midnight to hold the pace.</span></p>`;
  } else if (rv.xp > 0 || me.xp > 0) {
    const diff = me.xp - rv.xp;
    ping.innerHTML = `<span class="avatar">${RIC()}</span>
      <p><strong>${diff >= 0 ? `You lead ${rvName} by ${diff} XP.` : `${rvName} leads by ${-diff} XP.`}</strong>
      <span class="t">${rvName} hasn't claimed today yet — strike first.</span></p>`;
  } else {
    ping.innerHTML = `<span class="avatar">${RIC()}</span>
      <p><strong>The duel starts at zero.</strong>
      <span class="t">First claim in the app takes the lead — ${rvName}'s side syncs when his KB progress lands.</span></p>`;
  }

  $("stat-streak").innerHTML = `${me.streak}<small> days</small>`;
  $("stat-xp").innerHTML = `${me.xp}<small> XP</small>`;
  $("stat-rank").textContent = `Level ${me.level} · ${me.rank}`;

  /* course rail */
  document.querySelectorAll("[data-rail]").forEach(el => {
    const months = ["JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];
    const mPct = Math.max(S.plan.progress_pct[USER], 0.8);
    const aPct = Math.max(S.plan.progress_pct[RIVAL], 0.8);
    let html = `<div class="line"></div><div class="fill" style="width:${Math.max(mPct, aPct)}%"></div>`;
    months.forEach((m, i) => {
      const pct = (i / 6) * 100;
      html += `<div class="station ${i === 0 ? "hit" : ""}" style="left:${pct}%"></div>` +
              `<span class="st-lbl" style="left:${pct}%">${m}</span>`;
    });
    html += `<div class="station" style="left:100%"></div>`;
    html += `<div class="racer m num" style="left:${mPct}%" title="${cap(USER)} — ${S.users[USER].done.length} days done">${IC()}</div>`;
    html += `<div class="racer a num" style="left:${aPct}%" title="${cap(RIVAL)} — ${S.users[RIVAL].done.length} days done">${RIC()}</div>`;
    el.innerHTML = html;
  });

  renderCalendar();
  renderArena();
  renderNotes();
  renderChat();
}

/* ---------------- calendar ---------------- */
function renderCalendar() {
  const cal = S.calendar;
  $("cal-title").textContent = cal.month_name;
  const first = new Date(cal.year, cal.month - 1, 1);
  const daysInMonth = new Date(cal.year, cal.month, 0).getDate();
  const lead = (first.getDay() + 6) % 7;          // Monday-first
  const todayIso = S.today.date;
  let html = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
    .map(d => `<span class="dow">${d}</span>`).join("");
  for (let i = 0; i < lead; i++) html += `<div class="day out"></div>`;
  const monthLabel = S.today.month_title || "";
  $("cal-month-label").textContent = monthLabel.toUpperCase() || cal.month_name.toUpperCase();

  for (let d = 1; d <= daysInMonth; d++) {
    const iso = `${cal.year}-${String(cal.month).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
    const info = cal.days[iso] || { topic: "", kind: "rest", status: {} };
    const cls = ["day",
      info.kind === "build" ? "build" : "",
      info.kind === "review" || info.kind === "rest" ? "rest" : "",
      iso === todayIso ? "today-cell" : ""].filter(Boolean).join(" ");
    const past = iso < todayIso, isToday = iso === todayIso;
    const pip = who => {
      const done = info.status[who];
      const wait = who === USER ? "m-wait" : "a-wait";
      const dcls = who === USER ? "m-done" : "a-done";
      const miss = who === USER ? "m-miss" : "a-miss";
      if (info.kind === "rest" || info.kind === "review") return "";
      if (done) return `<span class="pip ${dcls}"></span>`;
      if (isToday) return `<span class="pip ${wait}"></span>`;
      if (past) return `<span class="pip ${miss}"></span>`;
      return `<span class="pip future"></span>`;
    };
    html += `<div class="${cls}"><span class="n num">${d}</span>` +
            `<span class="topic">${esc(info.topic)}</span>` +
            `<div class="dots">${pip(USER)}${pip(RIVAL)}</div></div>`;
  }
  $("cal-grid").innerHTML = html;
  $("lg-me").textContent = `${cap(USER)} done`;
  $("lg-rival").textContent = `${cap(RIVAL)} done`;
  $("cal-hint").textContent = `Every dot is a rival. Yellow = you, blue = ${cap(RIVAL)}, hollow red = missed.`;

  $("week-list").innerHTML = S.weeks.map(w =>
    `<article class="card week-item"><span class="wno">W${w.week}</span>` +
    `<span class="wt">${esc(w.title.replace(/^Week \d+ — /, ""))}</span>` +
    `<span class="wd">${esc(w.dates.toUpperCase())}</span></article>`).join("");
}

/* ---------------- arena ---------------- */
function renderArena() {
  const me = S.users[USER], rv = S.users[RIVAL];
  const duels = S.duels || [];
  // duel scores arrive keyed to the configured player order; flip to the viewer's side
  const first = S.users_order ? S.users_order[0] : USER;
  const mine = d => (USER === first ? d.m : d.a);
  const theirs = d => (USER === first ? d.a : d.m);

  let meWon = 0, rvWon = 0;
  duels.forEach(d => { if (!d.live) { if (mine(d) > theirs(d)) meWon++; else if (theirs(d) > mine(d)) rvWon++; } });

  $("duel-banner").innerHTML = `
    <article class="card player me">
      <div class="who"><span class="avatar">${IC()}</span>
        <div><div class="name">${cap(USER)}</div><div class="rank">LVL ${me.level} · ${esc(me.rank)}</div></div></div>
      <div class="xp num">${me.xp}<small> XP</small></div>
      <div class="sub num"><span>🔥 <b>${me.streak}</b> streak</span><span>🏆 <b>${meWon}</b> weeks won</span></div>
    </article>
    <span class="vs">VS</span>
    <article class="card player rival">
      <div class="who"><span class="avatar">${RIC()}</span>
        <div><div class="name">${cap(RIVAL)}</div><div class="rank">LVL ${rv.level} · ${esc(rv.rank)}</div></div></div>
      <div class="xp num">${rv.xp}<small> XP</small></div>
      <div class="sub num"><span>🔥 <b>${rv.streak}</b> streak</span><span>🏆 <b>${rvWon}</b> weeks won</span></div>
    </article>`;

  const diff = me.xp - rv.xp;
  const lead = $("lead-note");
  if (diff > 0) { lead.style.color = "var(--yellow)"; lead.textContent = `▲ YOU LEAD BY ${diff} XP — KEEP THE PRESSURE ON`; }
  else if (diff < 0) { lead.style.color = "var(--blue)"; lead.textContent = `▲ ${cap(RIVAL).toUpperCase()} LEADS BY ${-diff} XP`; }
  else { lead.style.color = "var(--muted)"; lead.textContent = "DEAD EVEN — NEXT CLAIM TAKES THE LEAD"; }

  $("duel-rows").innerHTML = duels.map(d => {
    const meS = mine(d), rvS = theirs(d), total = meS + rvS;
    const mw = total ? Math.round(100 * meS / total) : 0;
    const aw = total ? 100 - mw : 0;
    let res;
    if (d.live) res = `<span class="res live num">LIVE · ${meS}–${rvS}</span>`;
    else if (meS > rvS) res = `<span class="res m num">${cap(USER).toUpperCase()} ${meS}–${rvS}</span>`;
    else if (rvS > meS) res = `<span class="res a num">${cap(RIVAL).toUpperCase()} ${rvS}–${meS}</span>`;
    else res = `<span class="res tie num">TIE ${meS}–${rvS}</span>`;
    return `<div class="duel-row"><span class="w num">W${d.week}</span>` +
      `<div class="duel-bar"><div class="m-side" style="width:${mw}%"></div><div class="a-side" style="width:${aw}%"></div></div>${res}</div>`;
  }).join("") || `<div class="empty-note">No completed weeks yet — the first duel is live now.</div>`;

  const badges = [
    { ic: "🩸", bn: "First Blood", bd: "First deliverable filed", m: me.done.length > 0, a: rv.done.length > 0 },
    { ic: "🔥", bn: "One Week Strong", bd: "A 6-day streak", m: me.streak >= 6, a: rv.streak >= 6 },
    { ic: "🏆", bn: "Duel Winner", bd: "Won a weekly duel", m: mWon > 0, a: aWon > 0 },
    { ic: "🔩", bn: "Ship It", bd: "Completed a Saturday build", m: hasBuildDone(me), a: hasBuildDone(rv) },
    { ic: "🏛️", bn: "Iron Month", bd: "A full month, no misses", m: false, a: false },
    { ic: "🧠", bn: "Deep Diver", bd: "10 grounded KB conversations", m: (S.convos || []).length >= 10, a: false },
    { ic: "💯", bn: "Century", bd: "100-day streak — halfway legend", m: false, a: false },
  ];
  $("badge-grid").innerHTML = badges.map(b => {
    const owned = b.m || b.a;
    const own = (b.m ? `<span class="mini-pip m">${IC()}</span>` : "") + (b.a ? `<span class="mini-pip a">${RIC()}</span>` : "");
    return `<div class="badge ${owned ? "" : "locked"}"><div class="ic">${b.ic}</div>` +
      `<div class="bn">${b.bn}</div><div class="bd">${b.bd}</div>` +
      (owned ? `<div class="own">${own}</div>` : "") + `</div>`;
  }).join("");
}

function hasBuildDone(u) {
  return (u.done || []).some(iso => {
    const d = S.calendar.days[iso];
    return d && d.kind === "build";
  });
}

function computeDuelsFromState() { return S.duels || []; }

/* ---------------- notes ---------------- */
let noteTimer = null;
function renderNotes() {
  $("note-title").textContent = `Today · ${S.today.date}.md`;
  const ta = $("note-text");
  if (document.activeElement !== ta) ta.value = S.note || "";
  $("note-hint").innerHTML =
    `Saved straight into <code>people/${USER}/notes/${S.today.date}.md</code> in the KB — ` +
    `open it in Claude Code and it's already there. Edit the file on disk and refresh to pull it back.`;

  $("note-recents").innerHTML = (S.recent_notes || []).map(n =>
    `<div class="nf"><span class="ic">📝</span><div><div class="tt">${esc(n.first_line || n.file)}</div>` +
    `<div class="pp">${esc(n.path)}</div></div><span class="t">${esc(n.mtime.slice(5, 16).replace("T", " "))}</span></div>`
  ).join("") || `<div class="empty-note">No notes in the KB yet — today's note will be the first.</div>`;

  $("kb-feed").innerHTML = (S.kb_feed || []).map(f =>
    `<div class="feed-item"><span class="who-dot"></span><code>${esc(f.path)}</code>` +
    `<span class="t">${esc(f.mtime.slice(5, 16).replace("T", " "))}</span></div>`
  ).join("") || `<div class="empty-note">Nothing recent.</div>`;
}

$("note-text").addEventListener("input", () => {
  const el = $("note-sync");
  el.textContent = "SAVING…";
  el.classList.add("saving");
  clearTimeout(noteTimer);
  noteTimer = setTimeout(async () => {
    try {
      await api("/api/note", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user: USER, date: S.today.date, content: $("note-text").value }),
      });
      el.textContent = "IN THE KB · JUST NOW";
      el.classList.remove("saving");
      S.note = $("note-text").value;
    } catch (e) {
      el.textContent = "SAVE FAILED";
      toast("Note save failed: " + e.message);
    }
  }, 900);
});

/* ---------------- chat ---------------- */
function renderChat() {
  const chips = $("provider-chips");
  const provs = [["claude", "Claude"], ["codex", "Codex"], ["antigravity", "Antigravity"]];
  chips.innerHTML = provs.map(([k, label]) => {
    const avail = S.providers[k];
    const cls = "kb-chip" + (k === provider ? " on" : "") + (avail ? "" : " off");
    return `<button class="${cls}" data-prov="${k}" ${avail ? "" : 'title="CLI not found on this PC"'}>` +
           `${k === provider ? "◉ " : ""}${label}</button>`;
  }).join("");
  chips.querySelectorAll("[data-prov]").forEach(b => b.addEventListener("click", () => {
    if (!S.providers[b.dataset.prov]) { toast(`${b.dataset.prov} CLI not found on this PC`); return; }
    provider = b.dataset.prov;
    renderChat();
  }));

  $("convo-list").innerHTML = (S.convos || []).map(c =>
    `<button class="card convo" data-convo="${c.id}">` +
    `<span class="prov ${esc(c.provider)}">${c.provider.slice(0, 3).toUpperCase()}</span>` +
    `<span class="body"><span class="tt">${esc(c.title)}</span>` +
    `<span class="snip">${c.count} messages</span></span>` +
    `<span class="meta"><span class="t">${esc((c.when || "").slice(5, 16).replace("T", " "))}</span></span></button>`
  ).join("") || `<div class="card empty-note">No conversations yet.<br>Ask your first question below — it runs against the real wiki.</div>`;

  document.querySelectorAll("[data-convo]").forEach(b =>
    b.addEventListener("click", () => openConvo(b.dataset.convo)));
}

async function openConvo(id) {
  const c = await api(`/api/convo?id=${id}`);
  currentConvo = id;
  $("chat-history").style.display = "none";
  $("chat-thread").style.display = "block";
  $("thread-msgs").innerHTML = c.messages.map(m =>
    m.role === "user"
      ? `<div class="msg user">${esc(m.text)}</div>`
      : `<div class="msg bot ${m.ok === false ? "err" : ""}">` +
        `<div class="from">${esc(m.provider || c.provider)} · grounded in the KB</div>${esc(m.text)}</div>`
  ).join("");
}

$("back-to-history").addEventListener("click", () => {
  currentConvo = null;
  $("chat-thread").style.display = "none";
  $("chat-history").style.display = "block";
});

async function sendChat(inputEl, convoId) {
  const message = inputEl.value.trim();
  if (!message) return;
  if (!S.providers[provider]) { toast(`${provider} CLI not found on this PC`); return; }
  inputEl.value = "";
  const thinkingHtml = `<div class="msg user">${esc(message)}</div>` +
    `<div class="msg bot thinking">${provider} is reading the KB… this can take a minute or two.</div>`;
  if (convoId) {
    $("thread-msgs").insertAdjacentHTML("beforeend", thinkingHtml);
  } else {
    currentConvo = "pending";
    $("chat-history").style.display = "none";
    $("chat-thread").style.display = "block";
    $("thread-msgs").innerHTML = thinkingHtml;
  }
  $("thread-send").disabled = true;
  $("new-chat-send").disabled = true;
  try {
    const r = await api("/api/chat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user: USER, provider, message, convo_id: convoId || undefined }),
    });
    currentConvo = r.convo_id;
    await openConvo(r.convo_id);
    const st = await api(`/api/state?user=${USER}`);
    S.convos = st.convos;
  } catch (e) {
    toast("Chat failed: " + e.message);
    document.querySelector(".msg.thinking")?.remove();
  } finally {
    $("thread-send").disabled = false;
    $("new-chat-send").disabled = false;
  }
}

$("new-chat-send").addEventListener("click", () => sendChat($("new-chat-input"), null));
$("new-chat-input").addEventListener("keydown", e => { if (e.key === "Enter") sendChat($("new-chat-input"), null); });
$("thread-send").addEventListener("click", () => sendChat($("thread-input"), currentConvo));
$("thread-input").addEventListener("keydown", e => { if (e.key === "Enter") sendChat($("thread-input"), currentConvo); });

/* ---------------- claim ---------------- */
$("complete-btn").addEventListener("click", async () => {
  const btn = $("complete-btn");
  btn.disabled = true;
  btn.textContent = "Claiming…";
  try {
    S = await api("/api/claim", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user: USER, date: S.today.date }),
    });
    localChecks = [];
    render();
    toast(`Day ${pad3(S.today.day_n)} claimed · +${S.today.xp} XP 🔥`);
  } catch (e) {
    toast("Claim failed: " + e.message);
    btn.disabled = false;
  }
});

/* ---------------- boot ---------------- */
async function refreshState() {
  const fresh = await api("/api/state");
  const rivalHadClaimed = S && S.calendar.days[S.today.date]?.status[RIVAL];
  S = fresh;
  render();
  const rivalNowClaimed = S.calendar.days[S.today.date]?.status[RIVAL];
  if (!rivalHadClaimed && rivalNowClaimed) toast(`${cap(RIVAL)} just claimed today ⚔️`);
}

function startRealtime() {
  const label = $("sb-sync-label");
  if (label) label.textContent = S.cloud?.connected ? "Supabase · synced" : "Local only · cloud offline";
  // live push: any scoreboard change -> refetch. Fallback poll covers offline gaps.
  if (S.cloud?.connected && window.supabase) {
    try {
      const sb = window.supabase.createClient(S.cloud.url, S.cloud.anon_key);
      sb.channel("scoreboard")
        .on("postgres_changes", { event: "*", schema: "public", table: "claims" },
          () => { clearTimeout(startRealtime._t); startRealtime._t = setTimeout(refreshState, 800); })
        .subscribe();
    } catch (e) { /* fall back to polling */ }
  }
  setInterval(() => refreshState().catch(() => {}), 60000);
}

async function boot() {
  try {
    S = await api("/api/state");        // server resolves user from whoami.local.md
    USER = S.user;
    RIVAL = S.rival;
    render();
    startRealtime();
  } catch (e) {
    $("loading").textContent = "Could not reach the ARC/180 server — is server.py running? (" + e.message + ")";
  }
}
boot();

if ("serviceWorker" in navigator && window.isSecureContext) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}
