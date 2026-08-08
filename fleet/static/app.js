/* Agent Fleet — read-only dashboard.
   Polls GET /api/fleet every 3s; ages re-render every 1s so they stay live
   between polls. Never mutates agent state. */

(function () {
  "use strict";

  var POLL_MS = 3000;
  var TICK_MS = 1000;
  var STATUS_ORDER = { attention: 0, busy: 1, idle: 2, stale: 3 };
  var STATUSES = ["attention", "busy", "idle", "stale"];

  var boardEl = document.getElementById("board");
  var noticeEl = document.getElementById("notice");
  var countsEl = document.getElementById("counts");
  var linkStateEl = document.getElementById("link-state");
  var linkAgeEl = document.getElementById("link-age");

  var state = {
    payload: null,      // last good snapshot
    receivedAt: 0,      // performance clock reading when it arrived
    connected: false,
    everLoaded: false,
    renderedKey: null,  // avoids repainting identical data (keeps focus stable)
    expanded: Object.create(null), // card id -> sub-agent list open
    attention: null,    // ids currently in attention; null until first payload
    audioCtx: null
  };

  // ---------- attention chime ----------
  //
  // Fires only on the TRANSITION into attention, not while it persists —
  // otherwise a session blocked for an hour would chime every 3 seconds and
  // you would mute it permanently, which defeats the whole point.

  var SOUND_KEY = "agentfleet.sound";
  var soundOn = localStorage.getItem(SOUND_KEY) !== "off";

  function chime() {
    if (!soundOn) return;
    var Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    try {
      if (!state.audioCtx) state.audioCtx = new Ctx();
      var ctx = state.audioCtx;
      // Browsers suspend audio until a user gesture; a suspended context
      // means the page has not been interacted with yet, so stay silent
      // rather than queueing a burst of chimes for later.
      if (ctx.state === "suspended") return;

      var now = ctx.currentTime;
      var master = ctx.createGain();
      master.gain.value = 0.09;            // subtle by design
      master.connect(ctx.destination);

      // Two soft sine partials, a rising fifth — reads as a notification
      // rather than an alarm.
      [[880, 0], [1318.5, 0.11]].forEach(function (pair) {
        var osc = ctx.createOscillator();
        var gain = ctx.createGain();
        osc.type = "sine";
        osc.frequency.value = pair[0];
        var start = now + pair[1];
        gain.gain.setValueAtTime(0.0001, start);
        gain.gain.exponentialRampToValueAtTime(1, start + 0.012);
        gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.38);
        osc.connect(gain);
        gain.connect(master);
        osc.start(start);
        osc.stop(start + 0.42);
      });
    } catch (err) {
      /* audio is a nicety; never let it break the board */
    }
  }

  // A system notification, unlike the chime, still reaches you when this
  // window is minimised or covered — which is exactly when a browser throttles
  // the page and the chime becomes unreliable.
  function notify(card) {
    if (!soundOn) return;
    if (typeof Notification === "undefined") return;
    if (Notification.permission !== "granted") return;
    try {
      new Notification("Agent Fleet — " + card.name, {
        body: card.status_reason || "likely waiting on input",
        icon: "/static/icon-192.png",
        tag: "agentfleet-" + card.id,   // replaces rather than stacking
        silent: false
      });
    } catch (err) {
      /* notifications are a nicety; never let one break the board */
    }
  }

  function checkAttentionTransitions(cards) {
    var current = Object.create(null);
    var byId = Object.create(null);
    (cards || []).forEach(function (card) {
      if (card.status === "attention") {
        current[card.id] = true;
        byId[card.id] = card;
      }
    });

    // Seed silently on the first payload, so opening the page does not alert
    // for sessions that were already waiting before you looked.
    if (state.attention === null) {
      state.attention = current;
      return;
    }
    var entered = [];
    for (var id in current) {
      if (!state.attention[id]) entered.push(byId[id]);
    }
    state.attention = current;
    if (!entered.length) return;

    chime();                                  // one tone, however many entered
    entered.forEach(notify);                  // but one notification each
  }

  // ---------- helpers ----------

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  /* Server time, advanced locally between polls. Using the snapshot's own
     clock keeps ages honest and keeps them ticking while disconnected —
     stale data should visibly get older, not freeze. */
  function serverNow() {
    if (!state.payload) return Date.now() / 1000;
    return state.payload.generated_at + (performance.now() - state.receivedAt) / 1000;
  }

  function fmtAge(seconds) {
    var s = Math.max(0, Math.floor(seconds));
    if (s < 60) return s + "s";
    if (s < 3600) return Math.floor(s / 60) + "m " + pad(s % 60) + "s";
    if (s < 86400) return Math.floor(s / 3600) + "h " + pad(Math.floor(s % 3600 / 60)) + "m";
    return Math.floor(s / 86400) + "d " + Math.floor(s % 86400 / 3600) + "h";
  }

  function pad(n) { return n < 10 ? "0" + n : String(n); }

  function fmtTokens(n) {
    if (typeof n !== "number" || !isFinite(n)) return "—";
    if (n < 1000) return String(n);
    if (n < 1000000) return (n / 1000).toFixed(n < 10000 ? 1 : 0) + "K";
    return (n / 1000000).toFixed(n < 10000000 ? 2 : 1) + "M";
  }

  function fmtExact(n) {
    return typeof n === "number" ? n.toLocaleString() + " tokens" : "";
  }

  function fmtCost(value) {
    // null means "we could not price this model" — never render it as $0.00.
    if (typeof value !== "number" || !isFinite(value)) return "—";
    return "$" + value.toFixed(2);
  }

  function shortPath(path) {
    if (!path) return "";
    var p = path.replace(/^\/(?:Users|home)\/[^/]+/, "~");
    var parts = p.split("/");
    if (parts.length <= 5) return p;
    return parts.slice(0, 2).join("/") + "/…/" + parts.slice(-2).join("/");
  }

  // Elements carrying data-at are re-stamped every second by tick().
  function ageSpan(className, at) {
    var node = el("span", className, "");
    if (typeof at === "number") {
      node.dataset.at = String(at);
      node.textContent = fmtAge(serverNow() - at);
    }
    return node;
  }

  // ---------- card rendering ----------

  function renderCard(card) {
    var status = STATUSES.indexOf(card.status) >= 0 ? card.status : "stale";
    var root = el("article", "card s-" + status);
    root.dataset.card = card.id;

    var head = el("div", "head");
    head.appendChild(el("span", "dot"));
    var name = el("span", "name", card.name || card.id);
    name.title = card.name || card.id;
    head.appendChild(name);

    var badges = el("div", "badges");
    if (card.error) {
      var err = el("span", "badge badge-error", "error");
      err.title = String(card.error);
      badges.appendChild(err);
    }
    // Source badge is always present: an app card's status dot is a weaker
    // signal than a cli card's, and the viewer has to be able to tell.
    var src = el("span", "badge badge-" + (card.source === "cli" ? "cli" : "app"),
      card.source === "cli" ? "cli" : "app");
    src.title = card.source === "cli"
      ? "Claude Code CLI — liveness checked against the live process; can report attention"
      : "Claude desktop app — activity timestamps only; cannot report attention";
    badges.appendChild(src);
    head.appendChild(badges);
    root.appendChild(head);

    // status_reason verbatim, straight from the API. The attention state is an
    // inference; nothing here may harden it into a claim.
    if (card.status_reason) {
      root.appendChild(el("p", "status", card.status_reason));
    }

    var meta = el("div", "meta");
    if (card.cwd) {
      var path = el("span", "path", shortPath(card.cwd));
      path.title = card.cwd;
      meta.appendChild(path);
    }
    if (card.git_branch) meta.appendChild(el("span", "branch", card.git_branch));
    if (card.model) meta.appendChild(el("span", "model", card.model));
    if (meta.childNodes.length) root.appendChild(meta);

    if (card.error) {
      root.appendChild(el("div", "errline", String(card.error)));
    }

    var activity = renderActivity(card.last_tool);
    if (activity) root.appendChild(activity);

    var rollup = renderRollup(card);
    if (rollup) root.appendChild(rollup);

    root.appendChild(renderCost(card));
    return root;
  }

  function renderActivity(tool) {
    if (!tool || !tool.name) return null;
    var line = el("div", "activity");
    line.appendChild(el("span", "arrow", "↳"));
    var body = el("span", "body");
    body.appendChild(el("span", "tool", tool.name));
    if (tool.detail) body.appendChild(document.createTextNode(": " + tool.detail));
    line.appendChild(body);
    if (typeof tool.at === "number") {
      line.appendChild(el("span", "arrow", "·"));
      line.appendChild(ageSpan("age", tool.at));
    }
    line.title = tool.detail ? tool.name + ": " + tool.detail : tool.name;
    return line;
  }

  function renderRollup(card) {
    var running = card.subagents_running || 0;
    var done = card.subagents_done || 0;
    if (!running && !done) return null;

    var wrap = el("div", "rollup");
    var open = !!state.expanded[card.id];

    var toggle = el("button", "rollup-toggle");
    toggle.type = "button";
    toggle.setAttribute("aria-expanded", open ? "true" : "false");

    var line = el("div", "rollup-line");
    line.appendChild(el("span", "arrow caret", "▶"));
    var parts = [];
    if (running) parts.push(running + " running");
    if (done) parts.push(done + " done");
    line.appendChild(el("span", "text", parts.join(" · ")));
    toggle.appendChild(line);
    toggle.setAttribute("aria-label", "Sub-agents: " + parts.join(", "));

    toggle.addEventListener("click", function () {
      state.expanded[card.id] = !state.expanded[card.id];
      state.renderedKey = null;
      render();
    });
    wrap.appendChild(toggle);

    if (open) {
      var list = el("ul", "subagents");
      var items = (card.subagents || []).slice().sort(function (a, b) {
        return (b.running ? 1 : 0) - (a.running ? 1 : 0);
      });
      if (!items.length) {
        list.appendChild(el("li", "subagent", "no sub-agent detail available"));
      }
      items.forEach(function (sub) {
        var li = el("li", "subagent" + (sub.running ? " is-running" : ""));
        li.appendChild(el("span", "pip"));
        var desc = el("span", "desc", sub.description || sub.kind || "sub-agent");
        desc.title = (sub.kind ? sub.kind + " — " : "") + (sub.description || "");
        li.appendChild(desc);
        if (typeof sub.dispatched_at === "number") {
          li.appendChild(ageSpan("age", sub.dispatched_at));
        }
        list.appendChild(li);
      });
      // The header above reports the true total, so say what is not listed
      // rather than letting the count and the list silently disagree.
      var omitted = card.subagents_omitted || 0;
      if (omitted > 0) {
        list.appendChild(
          el("li", "subagent is-omitted", "+" + omitted + " older not shown")
        );
      }
      wrap.appendChild(list);
    }
    return wrap;
  }

  function renderCost(card) {
    var t = card.tokens || {};
    var foot = el("div", "cost");
    var tokens = el("div", "tokens");

    [["in", t.input, false],
     ["out", t.output, false],
     ["cache write", t.cache_creation, true],
     ["cache read", t.cache_read, true]].forEach(function (row) {
      var cell = el("div", "tok" + (row[2] ? " cache" : ""));
      cell.appendChild(el("span", "k", row[0]));
      var v = el("span", "v", fmtTokens(row[1]));
      v.title = fmtExact(row[1]);
      cell.appendChild(v);
      tokens.appendChild(cell);
    });
    foot.appendChild(tokens);

    var dollars = el("div", "dollars");
    dollars.appendChild(el("span", "k", "est. cost"));
    var amount = el("span", "v", fmtCost(card.cost_estimate));
    amount.title = typeof card.cost_estimate === "number"
      ? "Estimated from published list prices — not a bill."
      : "No price on file for this model.";
    dollars.appendChild(amount);
    foot.appendChild(dollars);

    return foot;
  }

  // ---------- board rendering ----------

  function sortCards(cards) {
    // attention first — they are the reason this dashboard exists.
    return cards.slice().sort(function (a, b) {
      var sa = STATUS_ORDER[a.status], sb = STATUS_ORDER[b.status];
      sa = sa === undefined ? 9 : sa;
      sb = sb === undefined ? 9 : sb;
      if (sa !== sb) return sa - sb;
      return (b.last_activity_at || 0) - (a.last_activity_at || 0);
    });
  }

  function renderCounts(counts) {
    countsEl.textContent = "";
    STATUSES.forEach(function (status) {
      var n = (counts && counts[status]) || 0;
      var chip = el("span", "count" + (n ? "" : " is-zero") +
        (status === "attention" && n ? " is-attention" : ""));
      chip.appendChild(el("b", null, String(n)));
      chip.appendChild(document.createTextNode(status));
      countsEl.appendChild(chip);
    });
  }

  function render() {
    var payload = state.payload;
    if (!payload) return;

    var key = JSON.stringify(payload.cards) + "|" + JSON.stringify(state.expanded);
    if (key === state.renderedKey) return;
    state.renderedKey = key;

    renderCounts(payload.counts);

    // Rebuilding the board drops focus; put it back where the user left it.
    var active = document.activeElement;
    var focusedCard = active && active.classList.contains("rollup-toggle") &&
      active.closest(".card") ? active.closest(".card").dataset.card : null;

    var cards = sortCards(payload.cards || []);
    var frag = document.createDocumentFragment();
    cards.forEach(function (card) { frag.appendChild(renderCard(card)); });
    boardEl.textContent = "";
    boardEl.appendChild(frag);

    if (focusedCard) {
      var restored = boardEl.querySelector(
        '[data-card="' + CSS.escape(focusedCard) + '"] .rollup-toggle');
      if (restored) restored.focus();
    }

    if (cards.length) {
      noticeEl.hidden = true;
    } else {
      noticeEl.textContent = "No agents active in the last 24 hours.";
      noticeEl.hidden = false;
    }
  }

  function renderLink() {
    if (!state.everLoaded) {
      linkStateEl.className = "link-state is-down";
      linkStateEl.textContent = "reconnecting…";
      linkAgeEl.textContent = "";
      return;
    }
    if (state.connected) {
      linkStateEl.className = "link-state is-live";
      linkStateEl.textContent = "live";
    } else {
      linkStateEl.className = "link-state is-down";
      linkStateEl.textContent = "reconnecting…";
    }
    linkAgeEl.textContent = "updated " + fmtAge(serverNow() - state.payload.generated_at) + " ago";
  }

  // ---------- polling ----------

  function poll() {
    fetch("/api/fleet", { cache: "no-store" })
      .then(function (res) {
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      })
      .then(function (payload) {
        state.payload = payload;
        state.receivedAt = performance.now();
        state.connected = true;
        state.everLoaded = true;
        // Before render(), which early-returns when the data is unchanged.
        checkAttentionTransitions(payload.cards);
        render();
        renderLink();
      })
      .catch(function () {
        // Keep the last good board on screen. A dropped poll is a lost
        // connection, not an empty fleet.
        state.connected = false;
        if (!state.everLoaded) {
          noticeEl.textContent = "Waiting for the fleet server…";
          noticeEl.hidden = false;
        }
        renderLink();
      });
  }

  function tick() {
    var now = serverNow();
    var nodes = document.querySelectorAll("[data-at]");
    for (var i = 0; i < nodes.length; i++) {
      nodes[i].textContent = fmtAge(now - Number(nodes[i].dataset.at));
    }
    if (state.everLoaded) renderLink();
  }

  poll();
  // ---------- sound toggle ----------

  var soundBtn = document.getElementById("sound");

  function audioBlocked() {
    // No context yet means nothing has woken audio, which is itself blocked.
    return !state.audioCtx || state.audioCtx.state === "suspended";
  }

  function notifyState() {
    if (typeof Notification === "undefined") return "unsupported";
    return Notification.permission;          // default | granted | denied
  }

  function renderSoundBtn() {
    var blocked = soundOn && audioBlocked();
    var notif = notifyState();
    soundBtn.setAttribute("aria-pressed", soundOn ? "true" : "false");
    soundBtn.classList.toggle("is-off", !soundOn);
    // Armed-but-blocked gets its own visible state: a silent dashboard must
    // never look like a working one.
    soundBtn.classList.toggle("is-armed", blocked);
    soundBtn.textContent = !soundOn ? "♪̸" : blocked ? "♪!" : "♪";

    if (!soundOn) {
      soundBtn.title = "Alerts off. Click to enable a chime and a system "
        + "notification when a session starts waiting on you.";
    } else if (blocked) {
      soundBtn.title = "Alerts armed, but this browser has not allowed audio "
        + "yet — click anywhere on the page once to enable it.";
    } else if (notif === "granted") {
      soundBtn.title = "Alerts on: chime + system notification when a session "
        + "starts waiting on you.";
    } else if (notif === "denied") {
      soundBtn.title = "Chime on. System notifications are blocked for this "
        + "site, so alerts may be missed while this window is minimised.";
    } else {
      soundBtn.title = "Chime on. Click again to also allow system "
        + "notifications, which still reach you when this window is hidden.";
    }
  }

  soundBtn.addEventListener("click", function () {
    var wasOn = soundOn;
    soundOn = !soundOn;
    localStorage.setItem(SOUND_KEY, soundOn ? "on" : "off");

    // A click is a user gesture, which is the only moment a browser will
    // accept a permission request. Ask whenever alerts are on and we have not
    // been told yes or no yet — including the second click, so turning it on
    // and leaving it on still eventually asks.
    if (soundOn && notifyState() === "default") {
      try {
        Notification.requestPermission().then(renderSoundBtn);
      } catch (err) { /* older callback-style API, or blocked */ }
    }
    if (soundOn && !wasOn) chime();     // confirm audibly that it works
    renderSoundBtn();
  });

  // Browsers refuse to start audio without a gesture. Any click counts, so
  // wake the context on the first one rather than demanding a special ritual.
  function wakeAudio() {
    var Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    if (!state.audioCtx) state.audioCtx = new Ctx();
    if (state.audioCtx.state === "suspended") state.audioCtx.resume();
    renderSoundBtn();
  }
  document.addEventListener("click", wakeAudio, { once: false, passive: true });
  document.addEventListener("keydown", wakeAudio, { passive: true });

  renderSoundBtn();
  setInterval(poll, POLL_MS);
  setInterval(tick, TICK_MS);
})();
