/*
 * Tuning Buddy setup window. Talks to Python through window.pywebview.api (setup_ui/api.py).
 * Opened with ?mock=<scenario> (in a plain browser) it fakes the API, to preview every screen:
 * install, update, reinstall, nodemo, running, lowdisk, error, denied, uninstall, uninstall-missing,
 * uninstall-error. Add &glass=1 for the glass look over a fake desktop.
 */
(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const RING = 2 * Math.PI * 54;
  const LINE = 24;  // a lyric line plus its gap
  let api = null;
  let info = null;
  let booted = false;
  let current = null;
  let pollTimer = null;
  let steps = [];
  let resultActions = {};

  const opts = { scope: "all", folder: "", demo: true, shortcut: false, folderPicked: false, deleteData: false };

  // ------------------------------------------------------------------ helpers

  function inlineLogos() {
    // <use> clones can't be animated one by one, so the big logos get their own copy
    const symbol = document.getElementById("logo");
    document.querySelectorAll("svg.logo").forEach((svg) => {
      svg.setAttribute("viewBox", "0 0 100 100");
      svg.innerHTML = symbol.innerHTML;
    });
  }

  function show(id) {
    const next = document.getElementById(id);
    if (current === next) return;
    if (current) current.classList.remove("active");
    next.classList.add("active");
    current = next;
    const busy = id === "s-progress";
    $("#btn-close").disabled = busy;
    $("#btn-close").title = busy ? "Use Cancel below" : "Close";
  }

  function esc(text) {
    return String(text).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  function clock(seconds) {
    const s = Math.max(0, Math.floor(seconds || 0));
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  }

  // ------------------------------------------------------------------ boot

  function boot(theApi) {
    if (booted) return;
    booted = true;
    api = theApi;
    inlineLogos();
    $("#btn-min").addEventListener("click", () => api.minimize());
    $("#btn-close").addEventListener("click", () => api.close());
    $("#btn-result-primary").addEventListener("click", () => resultActions.primary && resultActions.primary());
    $("#btn-result-secondary").addEventListener("click", () => resultActions.secondary && resultActions.secondary());
    $("#btn-details").addEventListener("click", toggleDetails);
    $("#btn-cancel").addEventListener("click", () => askCancel(true));
    $("#btn-cancel-no").addEventListener("click", () => askCancel(false));
    $("#btn-cancel-yes").addEventListener("click", confirmCancel);
    api.info().then((data) => {
      info = data;
      document.documentElement.classList.toggle("glass", !!info.glass);
      if (info.glass && api.set_dark) {
        // the acrylic tint follows the window's light/dark mode, which follows Windows
        const dark = matchMedia("(prefers-color-scheme: dark)");
        api.set_dark(dark.matches);
        dark.addEventListener("change", (e) => api.set_dark(e.matches));
      }
      if (info.mode === "uninstall") setUpUninstall(); else setUpInstall();
      requestAnimationFrame(() => document.body.classList.remove("booting"));
    });
  }

  window.addEventListener("pywebviewready", () => boot(window.pywebview.api));
  if (window.pywebview && window.pywebview.api && window.pywebview.api.info) boot(window.pywebview.api);
  const mockScenario = new URLSearchParams(location.search).get("mock");
  if (mockScenario) boot(mockApi(mockScenario));

  document.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || !current) return;
    const primary = { "s-welcome": "#btn-install", "s-confirm": "#btn-remove", "s-result": "#btn-result-primary" }[current.id];
    const button = primary && $(primary);
    if (button && !button.disabled && button.offsetParent) button.click();
  });

  // ------------------------------------------------------------------ install: welcome

  function setUpInstall() {
    document.title = "Tuning Buddy Setup";
    $("#ver").textContent = info.version;
    const existing = info.existing;
    if (info.action === "update") {
      $("#version-line").innerHTML = `Update <span class="update">${esc(existing.version || "")} → ${esc(info.version)}</span>`;
    } else if (info.action === "reinstall") {
      $("#version-line").textContent = `Version ${info.version} · already installed`;
    }
    $("#install-label").textContent = { update: "Update", reinstall: "Reinstall" }[info.action] || "Install";

    if (!info.demo_available) {
      $("#row-demo").remove();
      opts.demo = false;
    }
    if (existing) {
      // The engine requires an update to stay in the same scope and folder
      opts.scope = existing.scope === "user" ? "user" : "all";
      opts.folder = existing.location || info.defaults[opts.scope];
      $("#row-scope").classList.add("locked");
      $("#row-folder").classList.add("locked");
    } else {
      opts.folder = info.defaults.all;
    }

    const demo = $("#opt-demo");
    if (demo) demo.addEventListener("change", (e) => { opts.demo = e.target.checked; recheck(); });
    $("#opt-shortcut").addEventListener("change", (e) => { opts.shortcut = e.target.checked; });
    document.querySelectorAll("#opt-scope button").forEach((b) => b.addEventListener("click", () => {
      opts.scope = b.dataset.value;
      if (!opts.folderPicked) opts.folder = info.defaults[opts.scope];
      renderWelcome();
      recheck();
    }));
    $("#btn-folder").addEventListener("click", () => {
      api.choose_folder(opts.folder).then((folder) => {
        if (!folder) return;
        opts.folder = folder;
        opts.folderPicked = true;
        renderWelcome();
        recheck();
      });
    });
    $("#btn-install").addEventListener("click", startInstall);

    renderWelcome();
    show("s-welcome");
    recheck();
    setInterval(() => { if (current && current.id === "s-welcome") recheck(); }, 2000);
  }

  function renderWelcome() {
    const seg = $("#opt-scope");
    seg.dataset.value = opts.scope;
    seg.querySelectorAll("button").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.value === opts.scope)));
    $("#scope-note").textContent = info.existing ? "same as before" : opts.scope === "all" ? "admin needed" : "";
    $("#folder").textContent = opts.folder;
    $("#folder").title = opts.folder;
    $("#uac").toggleAttribute("hidden", opts.scope !== "all");
  }

  function recheck() {
    return api.check({ scope: opts.scope, folder: opts.folder, demo: opts.demo }).then((c) => {
      let text = "";
      let danger = false;
      if (c.app_running) {
        text = "Tuning Buddy is open. Close it to continue.";
      } else if (c.disk_ok === false) {
        text = `Not enough space on this drive: needs ${c.needed_text}, ${c.free_text} free.`;
        danger = true;
      }
      const alert = info.mode === "uninstall" ? $("#uninstall-alert") : $("#install-alert");
      alert.textContent = text;
      alert.classList.toggle("danger", danger);
      alert.hidden = !text;
      (info.mode === "uninstall" ? $("#btn-remove") : $("#btn-install")).disabled = !!text;
      return !text;
    });
  }

  // ------------------------------------------------------------------ progress (shared)

  function prepareProgress(title, stepList, cancellable) {
    steps = stepList;
    $("#progress-title").textContent = title;
    const box = $("#steps");
    box.innerHTML = "";
    box.style.setProperty("--p", "0");
    steps.forEach((s, i) => {
      const node = document.createElement("div");
      node.className = "step";
      node.innerHTML = `<span class="step-icon">${i + 1}<svg class="step-check" viewBox="0 0 24 24">` +
        `<polyline points="5 12.5 10 17 19 7.5"/></svg></span><span class="step-text">${esc(s.title)}</span>`;
      box.appendChild(node);
      s.node = node;
    });
    $("#lyrics-track").innerHTML = "";
    $("#lyrics-track").style.transform = "";
    $("#lyrics").classList.remove("failed");
    lyricKey = null;
    const ring = $("#ring");
    ring.classList.remove("complete");
    ring.classList.add("indeterminate");
    shownPct = 0;
    targetPct = 0;
    $("#pct").textContent = "0";
    setRing(0);
    $("#elapsed").textContent = "0:00";
    $("#cancel-area").hidden = !cancellable;
    $("#btn-cancel").hidden = false;
    $("#btn-cancel").disabled = false;
    $("#btn-cancel").textContent = "Cancel";
    $("#cancel-confirm").hidden = true;
  }

  function poll() {
    clearTimeout(pollTimer);
    api.status().then((st) => {
      if (info.mode === "uninstall") renderUninstallProgress(st); else renderInstallProgress(st);
      if (!st.finished) pollTimer = setTimeout(poll, 280);
    });
  }

  // The number counts up smoothly towards what the engine last reported, and never runs backwards
  let shownPct = 0;
  let targetPct = 0;
  let tweening = false;
  function setPct(pct) {
    targetPct = Math.max(targetPct, pct);
    setRing(targetPct);
    if (tweening) return;
    tweening = true;
    const step = () => {
      const gap = targetPct - shownPct;
      shownPct = gap < 0.5 ? targetPct : shownPct + Math.max(0.35, gap * 0.12);
      $("#pct").textContent = String(Math.round(shownPct));
      if (shownPct < targetPct) requestAnimationFrame(step); else tweening = false;
    };
    requestAnimationFrame(step);
  }

  function setRing(pct) {
    $("#ring-bar").style.strokeDashoffset = String(RING * (1 - pct / 100));
  }

  /* states: pending | active | done | failed. The connecting line fills up to the active step. */
  function setSteps(states) {
    let reached = 0;
    steps.forEach((s, i) => {
      const state = states[s.id] || "pending";
      s.node.className = `step ${state === "pending" ? "" : state}`.trim();
      if (state !== "pending") reached = i;
    });
    $("#steps").style.setProperty("--p", steps.length > 1 ? String(reached / (steps.length - 1)) : "0");
  }

  /* Lyrics, as in the Analyze overlay: a new line arrives in the middle, earlier ones move up and
     fade. A line with the same key ("Copying files · 42%" → 43%) just updates in place. */
  let lyricKey = null;
  function lyric(key, text) {
    const track = $("#lyrics-track");
    const lines = track.children;
    if (key === lyricKey && lines.length) {
      const last = lines[lines.length - 1];
      if (last.textContent !== text) last.textContent = text;
      return;
    }
    lyricKey = key;
    const line = document.createElement("div");
    line.className = "lyric entering";
    line.textContent = text;
    track.appendChild(line);
    const count = lines.length;
    [...lines].forEach((node, i) => {
      const age = count - 1 - i;
      node.classList.toggle("current", age === 0);
      node.classList.toggle("past-1", age === 1);
      node.classList.toggle("past-2", age === 2);
    });
    // the newest line sits in the middle of the 72px box
    track.style.transform = `translateY(${36 - 10 - (count - 1) * LINE}px)`;
    requestAnimationFrame(() => requestAnimationFrame(() => line.classList.remove("entering")));
  }

  // ------------------------------------------------------------------ install: run

  function startInstall() {
    $("#btn-install").disabled = true;
    recheck().then((ok) => {
      if (!ok) return;
      const list = [{ id: "files", title: "Copy Tuning Buddy" }];
      if (opts.demo) list.push({ id: "demo", title: "Load the demo database" });
      list.push({ id: "finishing", title: "Create shortcuts" });
      prepareProgress(info.action === "update" ? "Updating Tuning Buddy" : "Installing Tuning Buddy", list, true);
      api.start({ scope: opts.scope, folder: opts.folder, demo: opts.demo, shortcut: opts.shortcut }).then((r) => {
        if (!r.started) {
          showFailure(r.error || "Setup could not start.");
          return;
        }
        show("s-progress");
        poll();
      });
    });
  }

  function renderInstallProgress(st) {
    $("#elapsed").textContent = clock(st.elapsed);
    const phase = st.phase;
    const ring = $("#ring");
    const waiting = phase === "starting" || phase === "idle";
    ring.classList.toggle("indeterminate", waiting && !st.finished);
    if (!waiting && !st.cancelling) setPct(st.percent || 0);

    if (st.finished) {
      finishInstall(st);
      return;
    }

    // Cancel: offered until the last step, which is too short to stop
    const canCancel = ["starting", "preparing", "files", "demo"].includes(phase);
    if (st.cancelling) {
      $("#btn-cancel").hidden = false;
      $("#btn-cancel").disabled = true;
      $("#btn-cancel").textContent = "Stopping…";
      $("#cancel-confirm").hidden = true;
    } else if (!canCancel) {
      $("#cancel-area").hidden = true;
    }
    if (phase === "demo" && !st.cancelling) {
      $("#cancel-question").textContent = st.updating ? "Skip the demo database?" : "Stop and remove Tuning Buddy again?";
      $("#btn-cancel-yes").textContent = st.updating ? "Skip" : "Stop";
    }

    if (st.cancelling) {
      ring.classList.add("indeterminate");
      lyric(`cancel:${phase}`, phase === "removing" ? "Removing what was copied"
        : phase === "demo" ? "Stopping after the current table" : "Stopping and putting everything back");
      return;
    }

    const states = {};
    if (phase === "starting") {
      states.files = "active";
      const asking = opts.scope === "all" && (st.elapsed || 0) > 1.2;
      lyric(asking ? "uac" : "start", asking ? "Waiting for permission from Windows" : "Getting ready");
    } else if (phase === "preparing") {
      states.files = "active";
      lyric("prep", "Getting the folder ready");
    } else if (phase === "files") {
      states.files = "active";
      lyric("files", `Copying files · ${st.file_percent || 0}%`);
    } else if (phase === "demo") {
      states.files = "done";
      states.demo = "active";
      const lines = st.demo_lines || [];
      lyric(`demo${lines.length}`, lines.length ? lines[lines.length - 1] : "Starting the database server");
    } else if (phase === "finishing" || phase === "done") {
      states.files = "done";
      states.demo = st.demo_result === "failed" ? "failed" : "done";
      states.finishing = phase === "done" ? "done" : "active";
      if (st.demo_result === "failed") {
        $("#lyrics").classList.add("failed");
        lyric("demo-failed", "The demo database was skipped · see setup.log");
      } else {
        lyric("fin", "Creating shortcuts");
      }
    }
    setSteps(states);
  }

  function finishInstall(st) {
    const ring = $("#ring");
    const updated = info.action === "update";
    if (st.cancelled && !st.kept) {
      showResult({
        emblem: "stopped",
        title: updated ? "The update was stopped" : "Installation cancelled",
        text: updated ? "Run setup again to finish updating Tuning Buddy." : "Nothing was installed.",
        primary: ["Start over", () => { show("s-welcome"); recheck(); }],
        secondary: ["Close", () => api.close()],
      });
      return;
    }
    if (!st.ok) {
      showFailure(st.error);
      return;
    }
    ring.classList.remove("indeterminate");
    setPct(100);
    ring.classList.add("complete");
    setSteps(Object.fromEntries(steps.map((s) => [s.id, s.id === "demo" && st.demo_result === "failed" ? "failed" : "done"])));
    let text = "Connect your PostgreSQL and run your first analysis.";
    if (st.cancelled) text = "The demo database was skipped. Everything else is ready.";
    else if (opts.demo && st.demo_result === "failed") text = "The demo database couldn't be set up, but Tuning Buddy works fine with your own PostgreSQL.";
    else if (opts.demo) text = "The demo database is loaded and connected. Open Tuning Buddy and run your first analysis.";
    else if (updated) text = "Your connections and history are just as you left them.";
    setTimeout(() => showResult({
      emblem: "ok",
      title: updated ? `Updated to ${info.version}` : "You're all set",
      text,
      primary: ["Launch Tuning Buddy", () => api.launch()],
      secondary: ["Close", () => api.close()],
    }), 700);
  }

  // ------------------------------------------------------------------ cancel

  function askCancel(open) {
    $("#btn-cancel").hidden = open;
    $("#cancel-confirm").hidden = !open;
  }

  function confirmCancel() {
    $("#cancel-confirm").hidden = true;
    $("#btn-cancel").hidden = false;
    $("#btn-cancel").disabled = true;
    $("#btn-cancel").textContent = "Stopping…";
    api.cancel();
  }

  // ------------------------------------------------------------------ result screen

  function showResult({ emblem, title, text, primary, secondary, details }) {
    $("#emblem").className = `emblem ${emblem}`;
    $("#result-title").textContent = title;
    $("#result-text").textContent = text || "";
    $("#btn-details").hidden = !details;
    $("#btn-details").textContent = "Show details";
    $("#details").hidden = true;
    const p = $("#btn-result-primary");
    const s = $("#btn-result-secondary");
    p.hidden = !primary;
    s.hidden = !secondary;
    if (primary) p.textContent = primary[0];
    if (secondary) s.textContent = secondary[0];
    resultActions = { primary: primary && primary[1], secondary: secondary && secondary[1] };
    show("s-result");
  }

  function showFailure(message) {
    const uninstall = info.mode === "uninstall";
    showResult({
      emblem: "warn",
      title: uninstall ? "Tuning Buddy wasn't removed" : "Setup didn't finish",
      text: message || "Something went wrong.",
      details: true,
      primary: ["Try again", () => { show(uninstall ? "s-confirm" : "s-welcome"); recheck(); }],
      secondary: ["Close", () => api.close()],
    });
  }

  function toggleDetails() {
    const pre = $("#details");
    if (!pre.hidden) {
      pre.hidden = true;
      $("#btn-details").textContent = "Show details";
      return;
    }
    api.details().then((text) => {
      pre.textContent = text || "No details were recorded.";
      pre.hidden = false;
      pre.scrollTop = pre.scrollHeight;
      $("#btn-details").textContent = "Hide details";
    });
  }

  // ------------------------------------------------------------------ uninstall

  function setUpUninstall() {
    document.title = "Uninstall Tuning Buddy";
    $("#window-title").textContent = "Uninstall Tuning Buddy";
    const existing = info.existing;
    if (!existing) {
      showResult({
        emblem: "stopped",
        title: "Tuning Buddy isn't installed",
        text: "There's nothing to remove on this PC.",
        primary: ["Close", () => api.close()],
      });
      return;
    }
    $("#confirm-sub").textContent = `Version ${existing.version || "unknown"} · installed for ${existing.scope === "user" ? "you" : "everyone"}`;
    $("#confirm-path").textContent = existing.location || "";
    $("#confirm-path").title = existing.location || "";
    const toggle = $("#opt-data");
    const defaultNote = $("#data-note").textContent;
    if (info.data_exists) {
      $("#data-size").textContent = info.data_size_text;
    } else {
      $("#data-size").textContent = "nothing saved";
      toggle.disabled = true;
    }
    toggle.addEventListener("change", () => {
      opts.deleteData = toggle.checked;
      $("#data-note").textContent = opts.deleteData
        ? `Connections, history, AI keys and the demo database in ${info.data_dir} are deleted for good.`
        : defaultNote;
      $("#data-note").classList.toggle("danger", opts.deleteData);
      $("#btn-remove").textContent = opts.deleteData ? "Remove everything" : "Remove";
    });
    $("#btn-keep").addEventListener("click", () => api.close());
    $("#btn-remove").addEventListener("click", startUninstall);
    show("s-confirm");
    recheck();
    setInterval(() => { if (current && current.id === "s-confirm") recheck(); }, 2000);
  }

  function startUninstall() {
    $("#btn-remove").disabled = true;
    recheck().then((ok) => {
      if (!ok) return;
      const list = [{ id: "stopping", title: "Stop the demo database" }, { id: "removing", title: "Remove the app" }];
      if (opts.deleteData) list.push({ id: "data", title: "Delete your data" });
      prepareProgress("Removing Tuning Buddy", list, false);  // the stock uninstaller can't be stopped halfway
      api.start({ delete_data: opts.deleteData }).then((r) => {
        if (!r.started) return;
        show("s-progress");
        poll();
      });
    });
  }

  function renderUninstallProgress(st) {
    $("#elapsed").textContent = clock(st.elapsed);
    const order = ["idle", "stopping", "removing", "data", "done"];
    const at = order.indexOf(st.phase);
    const texts = {
      stopping: "Making sure nothing is still running",
      removing: (st.elapsed || 0) > 2.5 ? "Removing files, shortcuts and the Start menu entry" : "Waiting for Windows",
      data: "Deleting connections, history and the demo database",
    };
    const states = {};
    steps.forEach((s) => {
      const i = order.indexOf(s.id);
      states[s.id] = at === i ? "active" : (at > i ? "done" : "pending");
    });
    setSteps(states);
    if (texts[st.phase]) lyric(`${st.phase}:${texts[st.phase]}`, texts[st.phase]);
    if (!st.finished) return;
    if (!st.ok) {
      showFailure(st.error);
      return;
    }
    const ring = $("#ring");
    ring.classList.remove("indeterminate");
    setPct(100);
    ring.classList.add("complete");
    setSteps(Object.fromEntries(steps.map((s) => [s.id, "done"])));
    setTimeout(() => showResult({
      emblem: "ok",
      title: "Tuning Buddy has been removed",
      text: st.data_deleted ? "Your data was deleted too. Thanks for trying Tuning Buddy."
        : `Your data is still in ${st.data_dir}, ready if you reinstall.`,
      primary: ["Close", () => api.close()],
    }), 700);
  }

  // ------------------------------------------------------------------ preview without Python

  function mockApi(scenario) {
    const uninstall = scenario.startsWith("uninstall");
    const ok = (v) => Promise.resolve(v);
    const glass = new URLSearchParams(location.search).get("glass") === "1";
    if (glass) document.documentElement.classList.add("glass-preview");
    const t0 = { at: 0, demo: true, cancelAt: 0 };
    const existing = { scope: "all", version: "1.3.3", location: "C:\\Program Files\\Tuning Buddy", uninstaller: "" };
    const lines = ["Creating the demo database server", "Creating the demo user and extensions",
      "Loading the shop tables · customers, orders, products", "Loading large_orders · 1M rows",
      "Loading places · 200k PostGIS points", "Loading documents · 20k pgvector embeddings",
      "Loading events · 300k JSONB rows", "Loading measurements · 600k rows in 12 partitions", "The demo database is ready."];
    return {
      info: () => ok(uninstall ? {
        mode: "uninstall", glass,
        existing: scenario === "uninstall-missing" ? null : existing,
        version: "1.3.3",
        data_dir: "C:\\Users\\you\\AppData\\Local\\TuningBuddy",
        data_exists: true, data_size: 1181116006, data_size_text: "1.1 GB",
      } : {
        mode: "install", glass, version: "1.3.4", demo_available: scenario !== "nodemo",
        defaults: { all: "C:\\Program Files\\Tuning Buddy", user: "C:\\Users\\you\\AppData\\Local\\Programs\\Tuning Buddy" },
        existing: scenario === "update" ? existing : scenario === "reinstall" ? { ...existing, version: "1.3.4" } : null,
        action: scenario === "update" ? "update" : scenario === "reinstall" ? "reinstall" : "install",
      }),
      check: () => ok({ app_running: scenario === "running", disk_ok: scenario !== "lowdisk", needed_text: "1.1 GB", free_text: "640 MB" }),
      choose_folder: () => ok("D:\\Apps\\Tuning Buddy"),
      start: (o) => { t0.at = performance.now(); t0.demo = o.demo !== false; t0.del = o.delete_data; t0.cancelAt = 0; return ok({ started: true }); },
      cancel: () => { t0.cancelAt = performance.now(); return ok({ cancelling: true }); },
      status: () => {
        const t = (performance.now() - t0.at) / 1000;
        if (uninstall) {
          const phase = t < 1.2 ? "stopping" : t < 4 ? "removing" : t0.del && t < 5.5 ? "data" : "done";
          const failed = scenario === "uninstall-error" && t > 3;
          return ok({ phase: failed ? "removing" : phase, elapsed: t, finished: failed || phase === "done", ok: !failed,
                      error: failed ? "Tuning Buddy could not be removed. If Windows asked for administrator permission, it may have been declined." : undefined,
                      data_deleted: !!t0.del, data_dir: "C:\\Users\\you\\AppData\\Local\\TuningBuddy" });
        }
        if (scenario === "denied" && t > 2.5) {
          return ok({ phase: "starting", elapsed: t, finished: true, ok: false,
                      error: "Setup didn't start. If Windows asked for administrator permission, it may have been declined. To install without it, choose Just me." });
        }
        const filesEnd = 6.5;
        const demoEnd = t0.demo ? filesEnd + lines.length * 0.9 : filesEnd;
        const phase = t < 1.4 ? "starting" : t < 2 ? "preparing" : t < filesEnd ? "files" : t < demoEnd ? "demo" : t < demoEnd + 0.8 ? "finishing" : "done";
        const filePct = phase === "files" ? Math.round(((t - 2) / (filesEnd - 2)) * 100) : 100;
        const shown = phase === "demo" ? lines.slice(0, Math.floor((t - filesEnd) / 0.9) + 1) : [];
        const share = t0.demo ? 55 : 95;
        const pct = phase === "preparing" ? 2 : phase === "files" ? 3 + Math.round(share * filePct / 100)
          : phase === "demo" ? Math.min(97, 3 + share + 3 * shown.length) : phase === "finishing" ? 99 : phase === "done" ? 100 : 0;
        const updating = scenario === "update";
        if (t0.cancelAt) {
          const since = (performance.now() - t0.cancelAt) / 1000;
          const late = phase === "demo" || phase === "finishing" || phase === "done";
          if (since < 2.2) {
            return ok({ phase: late && !updating && since > 1 ? "removing" : phase === "files" ? "cancelling" : phase,
                        percent: pct, file_percent: filePct, elapsed: t, finished: false, cancelling: true, updating });
          }
          const kept = late && updating;
          return ok({ phase: kept ? "done" : "cancelled", percent: pct, elapsed: t, finished: true, cancelled: true,
                      kept, ok: kept, demo_result: "cancelled", updating });
        }
        if (scenario === "error" && t > 4.2) {
          return ok({ phase: "files", percent: pct, file_percent: filePct, elapsed: t, finished: true, ok: false,
                      error: "Setup couldn't write one of its files and stopped, so nothing was changed. Show details says which one." });
        }
        return ok({ phase, percent: pct, file_percent: filePct, demo_lines: shown, updating,
                    demo_result: phase === "finishing" || phase === "done" ? "ok" : "",
                    elapsed: t, finished: phase === "done", ok: phase === "done" });
      },
      details: () => ok("2026-10-01 20:41:07.112   Log opened.\n2026-10-01 20:41:07.113   Setup version: Inno Setup version 6.7.3\n...\n2026-10-01 20:41:11.480   Exception message: Access is denied."),
      launch: () => ok(true),
      minimize: () => ok(),
      close: () => ok(),
    };
  }
})();
