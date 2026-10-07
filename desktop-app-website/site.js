/*
 * The landing page's demo window: one analysis, played on a loop. Paste a query, the Analyze
 * overlay, then the results. Under the window, the story: what the app does, as lyrics in time
 * with it. Nothing is fetched: the lines below are a script. The buttons under the story jump to
 * a stage. With reduced motion there is no loop: the window shows the results, each button shows
 * its stage finished, and the story is plain text. The helpers are in common.js.
 */
(() => {
  "use strict";

  const { wait, type, setSteps, centre, lyric, clearLyrics, still } = TB;
  const $ = (sel) => document.querySelector(sel);
  const SQL = "SELECT *\nFROM large_orders\nWHERE customer_id = 42\nORDER BY created_at DESC\nLIMIT 20;";
  // [step that is running, the overlay's lyric, how long it stays, the story line that starts with it]
  const SCRIPT = [
    [0, "Running your query with EXPLAIN ANALYZE", 1300, "Tuning Buddy runs it with EXPLAIN ANALYZE"],
    [0, "Your query takes 201 ms", 1200],
    [1, "Asking your AI provider for ideas", 1600, "asks the AI provider you choose for index and rewrite ideas"],
    [1, "3 recommendations to test", 1100],
    [2, "Copying large_orders (150 MB) into a test schema", 1600, "tests each idea on a temporary copy of your tables"],
    [2, "#1 index: 97% faster ✓", 1200],
    [2, "#2 rewrite: 12% faster ✓", 1200, "and keeps only the ones that are measurably faster"],
    [3, "Checking the fit and ranking the results", 1300],
  ];
  const PASTE_STORY = "Paste a slow PostgreSQL query";
  // [the story line, how long it stays]
  const RESULTS_STORY = [
    ["You get a report: the bottleneck, and the timings before and after", 2500],
    ["Your rows stay on your PC: the AI sees the query and its plan, never the data", 2700],
    ["Also inside: a database browser, a test-data generator and 33 benchmark cases", 2800],
  ];
  const GUIDE_STORY = "Want the whole workflow, from the AI key to the PDF report?";
  const STAGES = ["paste", "analyze", "results", "guide"];

  const win = $("#demo");
  const sql = $("#sql");
  const button = $("#analyze-button");
  const steps = $("#steps");
  const lyrics = $("#lyrics");
  const story = $("#story");
  const dots = [...document.querySelectorAll("#dots button")];
  const next = $("#next");
  const chips = [...document.querySelectorAll("#chips li")];
  let clockTimer = null;

  function setStage(name) {
    win.dataset.stage = name;
    dots.forEach((dot) => dot.classList.toggle("on", dot.dataset.stage === name));
    clearInterval(clockTimer);
    if (name !== "analyze" || still) return;
    const started = Date.now();
    $("#clock").textContent = "0:00";
    clockTimer = setInterval(() => {
      const s = Math.floor((Date.now() - started) / 1000);
      $("#clock").textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
    }, 500);
  }

  function resetProgress() {
    clearLyrics(lyrics);
    setSteps(steps, 0);
  }

  // ------------------------------------------------------------------ the three stages

  async function paste(id) {
    sql.textContent = "";
    button.classList.remove("pressed");
    setStage("paste");
    lyric(story, PASTE_STORY);
    await wait(700, id);
    await type(sql, SQL, id);
    await wait(600, id);
    button.classList.add("pressed");
    await wait(380, id);
  }

  async function analyze(id) {
    sql.textContent = SQL;
    button.classList.remove("pressed");
    resetProgress();
    setStage("analyze");
    for (const [step, text, ms, line] of SCRIPT) {
      setSteps(steps, step);
      lyric(lyrics, text);
      if (line) lyric(story, line);
      await wait(ms, id);
    }
    setSteps(steps, steps.children.length);
    await wait(900, id);
  }

  async function results(id) {
    sql.textContent = SQL;
    setStage("results");
    for (const [line, ms] of RESULTS_STORY) {
      lyric(story, line);
      await wait(ms, id);
    }
  }

  /* What next: the guide's five parts light up over the results, with the way in */
  async function guide(id) {
    sql.textContent = SQL;
    chips.forEach((chip) => chip.classList.remove("lit"));
    setStage("guide");
    lyric(story, GUIDE_STORY);
    await wait(800, id);
    for (const chip of chips) {
      chip.classList.add("lit");
      await wait(420, id);
    }
    await wait(4200, id);
    while (next.matches(":hover")) await wait(400, id);  // the pointer is on it: stay
  }

  const play = [paste, analyze, results, guide];

  function loop(from) {
    TB.start(async (id) => {
      for (let i = from; ; i = (i + 1) % play.length) await play[i](id);
    });
  }

  /* Reduced motion: a stage as it looks when it has finished */
  function show(index) {
    sql.textContent = SQL;
    resetProgress();
    lyric(lyrics, SCRIPT[SCRIPT.length - 1][1]);
    setSteps(steps, steps.children.length);
    $("#clock").textContent = "0:24";
    chips.forEach((chip) => chip.classList.add("lit"));
    setStage(STAGES[index]);
  }

  function plainStory() {
    const lines = [PASTE_STORY, ...SCRIPT.map((entry) => entry[3]).filter(Boolean), ...RESULTS_STORY.map((entry) => entry[0]), GUIDE_STORY];
    story.classList.add("plain");
    story.firstElementChild.replaceChildren(...lines.map((text) => {
      const line = document.createElement("div");
      line.className = "lyric";
      line.textContent = text;
      return line;
    }));
  }

  window.addEventListener("resize", () => { centre(lyrics); if (!still) centre(story); });
  dots.forEach((dot, i) => dot.addEventListener("click", () => (still ? show(i) : loop(i))));
  if (still) { plainStory(); show(2); } else loop(0);
})();
