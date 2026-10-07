/*
 * The guide: how a DBA uses Tuning Buddy, in five parts that play one after another. Add the AI
 * key, connect a database, fill it with dummy data, test a slow query, read the PDF report. Each
 * part has its steps on the left (they tick off as the window does them) and lyrics under the
 * window. Nothing is fetched: every screen is a mock, and the lines below are the script.
 * guide.html#3 opens at part 3. With reduced motion nothing plays: each part shows its last
 * frame, and the lyrics are plain text. The helpers are in common.js.
 */
(() => {
  "use strict";

  const { wait, type, setSteps, centre, lyric, clearLyrics, still } = TB;
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => [...document.querySelectorAll(sel)];

  const SQL = "SELECT *\nFROM orders\nWHERE customer_id = 42\nORDER BY created_at DESC\nLIMIT 20;";
  const LOCKED = "Locked. Tuning Buddy can be used once at least one enabled provider passes the health check.";
  const READY = "Ready. 1 of 1 enabled provider passed the health check.";
  // [overlay step that is running, the overlay's lyric, how long it stays, the clock, the story line that starts with it]
  const ANALYSIS = [
    [0, "Running your query with EXPLAIN ANALYZE", 1300, "0:01", "It measures the query as it is"],
    [0, "Your query takes 184 ms", 1200, "0:03"],
    [1, "Asking your AI provider for ideas", 1500, "0:06", "asks your AI provider for index and rewrite ideas"],
    [1, "3 recommendations to test", 1000, "0:14"],
    [2, "Copying orders (500 MB) into a test schema", 1500, "0:17", "and tests each one on a copy of the tables"],
    [2, "#1 index: 99% faster ✓", 1100, "0:26"],
    [2, "#2 rewrite: 12% faster ✓", 1000, "0:31"],
    [3, "Checking the fit and ranking the results", 1200, "0:34"],
  ];

  const win = $("#win");
  const story = $("#story");
  const lyrics = $("#lyrics");
  const sql = $("#sql");
  const partText = $("#part-text");
  const pauseButton = $("#pause");
  let tabs = [];
  let stepBox = null;
  let current = 0;

  // ------------------------------------------------------------------ what the scripts are written with

  const say = (text) => lyric(story, text);

  /* Which screen of the app shows; `stage` is for the Analyze screens, as on the landing page */
  function view(scene, stage = "") {
    win.dataset.stage = stage;
    $$(".scene.g").forEach((node) => node.classList.toggle("on", node.dataset.scene === scene));
  }

  function cover(name) {
    $$(".cover").forEach((node) => node.classList.toggle("on", node.dataset.cover === name));
  }

  function nav(name) {
    $$("[data-nav]").forEach((node) => node.classList.toggle("on", node.dataset.nav === name));
  }

  /* The step of the part that is being done; the part's button fills up with it */
  function step(n) {
    setSteps(stepBox, n);
    tabs[current].style.setProperty("--fill", String(n / stepBox.children.length));
  }

  async function press(button, id) {
    button.classList.add("pressed");
    await wait(340, id);
    button.classList.remove("pressed");
  }

  async function fillIn(input, value, id, options) {
    input.classList.add("focus");
    await type(input, value, id, { ms: 34, ...options });
    await wait(200, id);
    input.classList.remove("focus");
  }

  async function choose(input, value, id) {
    input.classList.add("focus");
    await wait(380, id);
    input.textContent = value;
    await wait(280, id);
    input.classList.remove("focus");
  }

  /* The PDF viewer shows one page; the others wait above and below it */
  function showPage(n) {
    $$("#pdf-view .sheet").forEach((sheet) => {
      const page = Number(sheet.dataset.page);
      sheet.classList.toggle("is-before", page < n);
      sheet.classList.toggle("is-after", page > n);
      sheet.style.setProperty("--y", "0px");
    });
    $$("#pdf-thumbs .thumb").forEach((thumb, i) => thumb.classList.toggle("on", i === n - 1));
    $("#pdf-page").textContent = `${n} / 3`;
    spotlight(null);
  }

  /* Guided reading: frame one block of the page being read and bring it into view; the rest dims.
     A block also builds the first time it is lit (its bars grow, its flowchart draws). */
  function spotlight(name) {
    const view = $("#pdf-view");
    const sheet = view.querySelector(".sheet:not(.is-before):not(.is-after)");
    view.querySelectorAll(".lit").forEach((block) => block.classList.remove("lit"));
    view.querySelectorAll(".sheet").forEach((page) => page.classList.toggle("spot", !!name && page === sheet));
    if (!name) return;
    const block = sheet.querySelector(`[data-b="${name}"]`);
    block.classList.add("lit", "built");
    const middle = view.clientHeight / 2 - 12 - (block.offsetTop + block.offsetHeight / 2);
    const lowest = Math.min(0, view.clientHeight - 36 - sheet.offsetHeight);
    sheet.style.setProperty("--y", `${Math.round(Math.max(lowest, Math.min(0, middle)))}px`);
  }

  /* A figure runs from one value to another, slowing as it arrives */
  async function count(node, from, to, format, id) {
    for (let i = 1; i <= 16; i++) {
      node.textContent = format(from + (to - from) * (1 - Math.pow(1 - i / 16, 3)));
      await wait(45, id);
    }
  }

  // The thumbnails are the pages themselves, scaled down and already built
  function buildThumbs() {
    $("#pdf-thumbs").replaceChildren(...$$("#pdf-view .sheet").map((sheet) => {
      const copy = sheet.cloneNode(true);
      copy.className = "sheet";
      copy.querySelectorAll("[id]").forEach((node) => node.removeAttribute("id"));
      copy.querySelectorAll("[data-b]").forEach((block) => block.classList.add("built"));
      const thumb = el("div", "thumb");
      thumb.appendChild(copy);
      return thumb;
    }));
  }

  // Every screen back to how the app looks before anything was done
  function reset() {
    view(null);
    cover(null);
    $$(".input").forEach((node) => { node.textContent = ""; node.classList.remove("focus", "valid"); });
    $$(".pressed").forEach((node) => node.classList.remove("pressed"));
    $("#ai-banner").classList.remove("ready");
    $("#ai-banner-text").textContent = LOCKED;
    $("#ai-card").classList.remove("show");
    $("#ai-dot").classList.remove("ready");
    $("#ai-result").textContent = "";
    $("#ai-result").className = "result-line";
    $("#tree").classList.remove("open");
    $$(".tree-row").forEach((row) => row.classList.remove("show", "sel"));
    $("#schema-panel").classList.remove("show");
    $$(".box").forEach((box) => box.classList.remove("on"));
    $("#g-est-c").textContent = $("#g-est-o").textContent = "—";
    $("#g-order").textContent = "Tick the tables to fill";
    $("#g-go").classList.add("off");
    $("#gen-title").textContent = "Generating test data…";
    $("#gen-spinner").classList.remove("done");
    setSteps($("#gen-steps"), -1);
    $("#gd-c").textContent = $("#gd-o").textContent = $("#gd-a").textContent = "waiting";
    $("#gen-bar").style.width = "0";
    sql.textContent = "";
    clearLyrics(lyrics);
    setSteps($("#steps"), 0);
    $("#clock").textContent = "0:00";
    $("#toast").classList.remove("on");
    $$("#pdf-view [data-b]").forEach((block) => block.classList.remove("lit", "built"));
    showPage(1);
  }

  // What the parts before left behind
  function unlocked() {
    $("#ai-dot").classList.add("ready");
  }

  function connected() {
    $("#tree").classList.add("open");
    $$(".tree-row").forEach((row) => row.classList.add("show"));
    $("#tree-schema").classList.add("sel");
    $("#schema-panel").classList.add("show");
  }

  // ------------------------------------------------------------------ the five parts

  async function aiKey(id) {
    nav("ai");
    view("ai-list");
    step(0);
    say("The app starts locked: it needs one working AI provider");
    await wait(1900, id);
    await press($("#ai-add"), id);
    view("ai-form");
    step(1);
    say("Pick your provider, then paste your API key");
    await wait(500, id);
    await fillIn($("#f-name"), "Claude", id);
    await choose($("#f-type"), "Anthropic Claude", id);
    await choose($("#f-model"), "claude-sonnet-5-5", id);
    say("The key is stored encrypted on this PC");
    await fillIn($("#f-key"), "sk-ant-api03-xxxxxxxx4f2a", id, { mask: true });
    await wait(700, id);

    step(2);
    say("Test connection asks the provider for a real answer");
    await press($("#ai-test"), id);
    $("#ai-result").textContent = "Checking…";
    await wait(1400, id);
    $("#ai-result").textContent = "✓ healthy: the provider answered";
    $("#ai-result").classList.add("ok");
    await wait(1300, id);

    step(3);
    await press($("#ai-save"), id);
    view("ai-list");
    $("#ai-banner").classList.add("ready");
    $("#ai-banner-text").textContent = READY;
    $("#ai-card").classList.add("show");
    unlocked();
    say("Ready: the rest of the app unlocks");
    await wait(2500, id);
    step(4);
  }

  async function connect(id) {
    unlocked();
    nav("databases");
    view("db-tree");
    step(0);
    say("Add the PostgreSQL server you want to tune");
    await wait(1700, id);
    await press($("#c-add"), id);
    view("db-form");
    step(1);
    say("Here: a local copy with the production schema, and no data");
    await wait(500, id);
    await fillIn($("#c-name"), "Local copy", id);
    await fillIn($("#c-db"), "tuning_copy", id);
    await fillIn($("#c-host"), "localhost", id);
    await fillIn($("#c-port"), "5432", id);
    await fillIn($("#c-user"), "postgres", id);
    say("The password is stored encrypted, and never shown");
    await fillIn($("#c-pass"), "••••••••••", id, { mask: true });
    await wait(600, id);

    step(2);
    await press($("#c-save"), id);
    view("db-tree");
    $("#tree").classList.add("open");
    say("Every database on that server shows up in the tree");
    await wait(500, id);

    step(3);
    const rows = $$(".tree-row");
    for (const [i, row] of rows.entries()) {
      row.classList.add("show");
      if (i === 4) say("The tables are there, and empty");
      await wait(400, id);
    }
    $("#tree-schema").classList.add("sel");
    $("#schema-panel").classList.add("show");
    await wait(2300, id);
    step(4);
  }

  async function dummyData(id) {
    unlocked();
    connected();
    nav("databases");
    view("db-tree");
    step(0);
    say("Empty tables can't show why a query is slow");
    await wait(1900, id);
    await press($("#gen-open"), id);
    view("gen-form");
    step(1);
    say("Tick the tables, and say how much data each one gets");
    await wait(600, id);
    $("#g-row-c .box").classList.add("on");
    await fillIn($("#g-size-c"), "50 MB", id, { ms: 60 });
    $("#g-est-c").textContent = "410k";
    $("#g-order").textContent = "Order: customers";
    await wait(300, id);
    $("#g-row-o .box").classList.add("on");
    await fillIn($("#g-size-o"), "500 MB", id, { ms: 60 });
    $("#g-est-o").textContent = "4.1M";
    $("#g-order").textContent = "Order: customers → orders";
    say("Parents are filled first: orders refers to customers");
    await wait(1700, id);

    step(2);
    say("Typing the name makes sure the data goes to the right database");
    await fillIn($("#g-confirm"), "tuning_copy", id, { ms: 50 });
    $("#g-confirm").classList.add("valid");
    $("#g-go").classList.remove("off");
    await wait(800, id);

    step(3);
    await press($("#g-go"), id);
    cover("gen");
    const genSteps = $("#gen-steps");
    const bar = (mb) => { $("#gen-bar").style.width = `${Math.round((mb / 550) * 100)}%`; };
    setSteps(genSteps, 0);
    say("Values follow each column's type and name");
    for (const mb of [15, 35, 50]) {
      $("#gd-c").textContent = `${mb} MB / 50 MB · ${Math.round(mb * 8.2)}k rows`;
      bar(mb);
      await wait(420, id);
    }
    setSteps(genSteps, 1);
    say("orders fills with rows that point at real customers");
    for (const mb of [60, 130, 200, 270, 340, 410, 470, 500]) {
      $("#gd-o").textContent = `${mb} MB / 500 MB · ${(mb * 0.0082).toFixed(1)}M rows`;
      bar(50 + mb);
      await wait(420, id);
    }
    setSteps(genSteps, 2);
    $("#gd-a").textContent = "ANALYZE";
    say("Then ANALYZE, so the planner has real statistics");
    await wait(1600, id);
    setSteps(genSteps, 3);
    $("#gd-a").textContent = "done";
    $("#gen-title").textContent = "Test data is ready";
    $("#gen-spinner").classList.add("done");
    await wait(2300, id);
    step(4);
  }

  async function testQuery(id) {
    unlocked();
    nav("databases");
    view(null, "paste");
    step(0);
    say("Now paste the query that is slow in production");
    await wait(700, id);
    await type(sql, SQL, id);
    await wait(500, id);

    step(1);
    await press($("#analyze-button"), id);

    step(2);
    view(null, "analyze");
    const overlaySteps = $("#steps");
    for (const [running, text, ms, clock, line] of ANALYSIS) {
      setSteps(overlaySteps, running);
      lyric(lyrics, text);
      $("#clock").textContent = clock;
      if (line) say(line);
      await wait(ms, id);
    }
    setSteps(overlaySteps, overlaySteps.children.length);
    await wait(800, id);

    step(3);
    view(null, "results");
    say("The best option: one index, 99% faster, measured on the copy");
    await wait(2700, id);
    say("Ideas that are not faster are left out");
    await wait(2400, id);
    step(4);
  }

  async function pdfReport(id) {
    unlocked();
    nav("databases");
    sql.textContent = SQL;
    view(null, "results");
    step(0);
    say("Every analysis comes with a PDF report");
    await wait(1800, id);
    await press($("#pdf-button"), id);
    $("#toast").classList.add("on");
    await wait(1300, id);
    $("#toast").classList.remove("on");
    cover("pdf");
    await wait(500, id);

    step(1);
    spotlight("figs");
    say("Page 1 opens with the four numbers that matter");
    const ms = (value) => `${value.toFixed(2)} ms`;
    await Promise.all([
      count($("#n-orig"), 0, 184, ms, id),
      count($("#n-best"), 184, 1.2, ms, id),
      count($("#n-gain"), 0, 99.3, (value) => `${value.toFixed(1)}%`, id),
      count($("#n-recs"), 0, 3, (value) => String(Math.round(value)), id),
    ]);
    await wait(2000, id);
    spotlight("action");
    say("Then exactly what to run, and what it buys you");
    await wait(2900, id);
    spotlight("chart");
    say("Every option that was tested, side by side");
    await wait(3000, id);

    step(2);
    showPage(2);
    await wait(500, id);
    spotlight("compare");
    say("Page 2 is the evidence: the plan now, and the plan after");
    await wait(2800, id);
    spotlight("flow");
    say("The plan as a flowchart, with the bottleneck in red");
    await wait(3600, id);

    step(3);
    showPage(3);
    await wait(500, id);
    spotlight("verdict");
    say("Then one page per option: the verdict, the index, the fit");
    await wait(2900, id);
    spotlight("planafter");
    say("and its plan after the change: no sequential scan left");
    await wait(3000, id);
    spotlight(null);
    say("It stays in History, to open again any time");
    await wait(2300, id);
    step(4);
  }

  const PARTS = [
    {
      tab: "AI key", title: "Add your AI key", play: aiKey,
      lead: "Tuning Buddy asks an AI provider for tuning ideas, so the app stays locked until one provider works. This is the first screen you see.",
      steps: ["Open AI Settings and click Add Provider", "Pick the provider and paste your API key", "Click Test connection", "Save: the app unlocks"],
      tip: "A local model through Ollama or LM Studio needs no key.",
    },
    {
      tab: "Connect", title: "Connect your database", play: connect,
      lead: "Point it at the PostgreSQL server you want to tune. Here it is a local copy of production: the schema restored, with no data.",
      steps: ["Open Databases and click Add connection", "Fill in the server: localhost for this PC", "Save the connection", "Open the tree down to your tables"],
      tip: "pg_dump --schema-only copies the structure without any production rows.",
    },
    {
      tab: "Dummy data", title: "Generate dummy data", play: dummyData,
      lead: "A query is only slow on real volumes. Fill the empty tables with realistic rows, sized in MB or GB.",
      steps: ["On the schema, click Generate data", "Tick the tables and set a size for each", "Type the database name to confirm", "Generate: parents first, then ANALYZE"],
      tip: "It respects keys, unique columns, CHECK constraints, enums and partitions.",
    },
    {
      tab: "Test query", title: "Test your query", play: testQuery,
      lead: "Paste the query that is slow in production. Each idea from the AI is tested on a copy of the tables, and only the faster ones are kept.",
      steps: ["Open Analyze and paste the query", "Click Analyze Query", "Watch it measure, ask the AI and test", "Read the result"],
      tip: "Analyze runs read-only: it never changes your data.",
    },
    {
      tab: "PDF report", title: "Get the PDF report", play: pdfReport,
      lead: "The result as a document you can attach to a change request: what ran, what to do, and the evidence.",
      steps: ["On the results, click Download PDF", "Page 1: the numbers and the action", "Page 2: the plan today, and its bottleneck", "One page per option"],
      tip: "Every analysis is kept in History with its report.",
      link: ["The rest of the app, in the reference guide", "https://github.com/Athoillah21/tuning-buddy-desktop/blob/main/desktop-apps/README.md"],
    },
  ];

  // ------------------------------------------------------------------ the page around the window

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function buildTabs() {
    tabs = PARTS.map((part, i) => {
      const tab = el("button");
      tab.append(el("b", "", String(i + 1)), el("span", "", part.tab));
      tab.addEventListener("click", () => go(i));
      $("#parts").appendChild(tab);
      return tab;
    });
  }

  /* The left column: this part's title, steps and tip */
  function open(index) {
    current = index;
    const part = PARTS[index];
    tabs.forEach((tab, i) => {
      tab.classList.toggle("on", i === index);
      tab.classList.toggle("done", i < index);
      tab.style.setProperty("--fill", "0");
      if (i === index) tab.setAttribute("aria-current", "step"); else tab.removeAttribute("aria-current");
    });
    // on a phone the buttons scroll sideways: bring this one to the middle, without moving the page
    const bar = $("#parts");
    const box = tabs[index].getBoundingClientRect();
    bar.scrollLeft += box.left - bar.getBoundingClientRect().left - (bar.clientWidth - box.width) / 2;

    stepBox = el("div", "steps");
    part.steps.forEach((text, i) => {
      const row = el("div", "step");
      const icon = el("span", "step-icon", String(i + 1));
      icon.insertAdjacentHTML("beforeend", '<svg class="step-check"><use href="#i-check"/></svg>');
      row.append(icon, el("span", "step-text", text));
      stepBox.appendChild(row);
    });
    const tip = el("p", "tip");
    tip.append(el("strong", "", "Good to know. "), part.tip);
    if (part.link) {
      const link = el("a", "", part.link[0]);
      link.href = part.link[1];
      tip.append(" ", link, ".");
    }
    partText.replaceChildren(
      el("p", "eyebrow", `Part ${index + 1} of ${PARTS.length}`),
      el("h1", "", part.title),
      el("p", "lead", part.lead),
      stepBox,
      tip,
    );
    history.replaceState(null, "", `#${index + 1}`);
  }

  function setPaused(on) {
    TB.pause(on);
    document.body.classList.toggle("paused", on);
    pauseButton.textContent = on ? "Play" : "Pause";
    pauseButton.setAttribute("aria-pressed", String(on));
  }

  /* Play from part `index`, then the ones after it, round and round */
  function go(index) {
    setPaused(false);
    TB.start(async (id) => {
      for (let i = index; ; i = (i + 1) % PARTS.length) {
        open(i);
        reset();
        if (still) clearLyrics(story);
        await PARTS[i].play(id);
        if (still) return;
        await wait(1600, id);
      }
    });
  }

  function partFromHash() {
    const n = parseInt(location.hash.slice(1), 10);
    return n >= 1 && n <= PARTS.length ? n - 1 : 0;
  }

  if (still) {
    TB.instant = true;
    story.classList.add("plain");
    pauseButton.hidden = true;
  }
  buildTabs();
  buildThumbs();
  pauseButton.addEventListener("click", () => setPaused(pauseButton.getAttribute("aria-pressed") !== "true"));
  window.addEventListener("hashchange", () => { if (partFromHash() !== current) go(partFromHash()); });
  window.addEventListener("resize", () => { centre(lyrics); if (!still) centre(story); });
  go(partFromHash());
})();
