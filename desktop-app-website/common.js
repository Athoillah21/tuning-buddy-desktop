/*
 * What both pages' demos are made of (site.js, guide.js): a run that can be stopped or paused,
 * typing, the step list and the lyrics. The step list and lyrics follow the app's Analyze overlay
 * (static/js/progress-overlay.js).
 */
window.TB = (() => {
  "use strict";

  const STOP = Symbol("stop");
  let run = 0;  // each start() is a new run; the waits of the one before stop it
  let paused = false;

  const api = {
    still: matchMedia("(prefers-reduced-motion: reduce)").matches,
    instant: false,  // waits end at once: a script runs straight to its last frame
    start, wait, type, setSteps, centre, lyric, clearLyrics,
    pause(on) { paused = on; },
  };

  /* Run `script(id)`; starting another run ends this one at its next wait */
  function start(script) {
    const id = ++run;
    script(id).catch((e) => { if (e !== STOP) throw e; });
  }

  function wait(ms, id) {
    if (id !== run) return Promise.reject(STOP);
    if (api.instant) return Promise.resolve();
    return new Promise((resolve, reject) => {
      let left = ms;
      let last = performance.now();
      const tick = () => {
        if (id !== run) { reject(STOP); return; }
        const now = performance.now();
        if (!paused) left -= now - last;
        last = now;
        if (left <= 0.5) resolve(); else setTimeout(tick, paused ? 120 : Math.min(left, 120));
      };
      setTimeout(tick, Math.min(ms, 120));
    });
  }

  /* `text` types itself into `el`; `mask` shows dots, as a password field does */
  async function type(el, text, id, { ms = 28, mask = false } = {}) {
    for (let i = 1; i <= text.length; i++) {
      el.textContent = mask ? "•".repeat(i) : text.slice(0, i);
      await wait(text[i - 1] === "\n" ? ms * 6 : ms, id);
    }
  }

  /* `current` is the running step; steps before it are done. The connecting line fills up to it. */
  function setSteps(box, current) {
    const steps = [...box.children];
    steps.forEach((node, i) => {
      node.className = `step ${i < current ? "done" : i === current ? "active" : ""}`.trim();
    });
    const reached = Math.max(0, Math.min(current, steps.length - 1));
    box.style.setProperty("--p", steps.length > 1 ? String(reached / (steps.length - 1)) : "0");
  }

  // Keep the newest line in the middle of the box: the track glides, the box stays
  function centre(box) {
    const track = box.firstElementChild;
    const line = track.lastElementChild;
    if (!line) return;
    const offset = box.clientHeight / 2 - (line.offsetTop + line.offsetHeight / 2);
    track.style.transform = `translateY(${Math.round(offset)}px)`;
  }

  /* A new line arrives in the middle of `box`, earlier ones move up and fade */
  function lyric(box, text) {
    const track = box.firstElementChild;
    if (track.children.length > 5 && !box.classList.contains("plain")) {
      // lines that faded out long ago go, without the visible ones jumping
      track.style.transition = "none";
      while (track.children.length > 3) track.firstElementChild.remove();
      centre(box);
      void track.offsetHeight;
      track.style.transition = "";
    }
    const line = document.createElement("div");
    line.className = "lyric entering";
    line.textContent = text;
    track.appendChild(line);
    const lines = [...track.children];
    lines.forEach((node, i) => {
      const age = lines.length - 1 - i;
      node.classList.toggle("current", age === 0);
      node.classList.toggle("past-1", age === 1);
      node.classList.toggle("past-2", age === 2);
    });
    centre(box);
    requestAnimationFrame(() => requestAnimationFrame(() => line.classList.remove("entering")));
  }

  function clearLyrics(box) {
    box.firstElementChild.replaceChildren();
    box.firstElementChild.style.transform = "";
  }

  return api;
})();
