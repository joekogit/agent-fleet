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
    expanded: Object.create(null) // card id -> sub-agent list open
  };

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
      ? "Claude Code CLI — live process heartbeat"
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
  setInterval(poll, POLL_MS);
  setInterval(tick, TICK_MS);
})();
