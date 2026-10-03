/*
 * Live progress for the analysis overlay (Analyze page and Test cases).
 *
 * The page makes up an id, sends it with the analysis, and polls
 * /api/analyze/progress/<id>/ while the analysis runs. The analyzer reports its phase and a
 * running log of what it is doing ("Copying large_orders (150 MB)…", "#1 index: 97% faster ✓").
 *
 * The phase drives the step list (current step pulses, finished steps draw a check). The log is
 * shown like synced lyrics: the newest line sits in the middle, large and bright; earlier lines
 * glide up, shrink and fade; a dim "…" waits below for the next one. If the live feed cannot be
 * reached, the steps and lines advance on a timer instead, so the overlay is never blank.
 *
 * Markup (see templates/advisor/_progress_steps.html):
 *   .loading-step[data-phase="baseline|ai|testing|ranking"]   the steps
 *   [data-lp="lyrics"] > .lyrics-track                         the lyric lines
 *   [data-lp="clock"]                                          elapsed time (optional)
 */
(function () {
    const PHASES = ['baseline', 'ai', 'testing', 'ranking'];
    const FALLBACK_LINES = [
        ['baseline', 'Running your query with EXPLAIN ANALYZE'],
        ['ai', 'Asking your AI provider for optimization ideas'],
        ['testing', 'Testing each idea on copies of your tables'],
        ['testing', 'Measuring every version a few times'],
        ['ranking', 'Ranking the results'],
    ];

    function newId() {
        const bytes = new Uint8Array(16);
        (window.crypto || window.msCrypto).getRandomValues(bytes);
        return Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
    }

    function clock(seconds) {
        seconds = Math.max(0, Math.floor(seconds));
        return Math.floor(seconds / 60) + ':' + String(seconds % 60).padStart(2, '0');
    }

    function attach(overlay) {
        const part = name => overlay.querySelector(`[data-lp="${name}"]`);
        const steps = overlay.querySelectorAll('.loading-step[data-phase]');
        const lyrics = part('lyrics');
        const track = lyrics ? lyrics.querySelector('.lyrics-track') : null;
        let lines = new Map();   // key -> element, in order
        let seen = new Set();    // every key ever shown, so pruned lines never come back
        let pending = [];        // lines waiting for their turn to appear
        let revealTimer = null;
        let nextLine = null;
        let pollTimer = null, clockTimer = null, fallbackTimer = null;
        let started = 0, failures = 0, everWorked = false, request = 0;

        function setPhase(phase) {
            const current = phase === 'done' ? PHASES.length : PHASES.indexOf(phase);
            steps.forEach(step => {
                const index = PHASES.indexOf(step.dataset.phase);
                step.classList.toggle('done', index < current);
                step.classList.toggle('running', index === current);
                step.classList.toggle('active', index <= current);
            });
            const list = overlay.querySelector('.lp-steps');
            if (list) list.style.setProperty('--lp-progress', Math.min(Math.max(current, 0), PHASES.length - 1) / (PHASES.length - 1));
        }

        function resetLyrics() {
            if (!track) return;
            clearTimeout(revealTimer);
            revealTimer = null;
            pending = [];
            track.replaceChildren();
            lines = new Map();
            seen = new Set();
            nextLine = document.createElement('div');
            nextLine.className = 'lyric is-next';
            nextLine.textContent = '…';
            track.appendChild(nextLine);
            track.style.transform = 'translateY(0)';
        }

        // Queue `entries` ([{key, text}]) as lyrics. New lines play one after another, like a
        // song: a burst from one poll (three measuring runs in a second) is spread out so each
        // line can be read, and a long backlog plays faster so the screen never falls behind.
        function showLines(entries) {
            if (!track) return;
            entries.forEach(entry => {
                if (seen.has(entry.key)) return;
                seen.add(entry.key);
                pending.push(entry);
            });
            if (!revealTimer) revealNext();
        }

        function revealNext() {
            revealTimer = null;
            const entry = pending.shift();
            if (!entry) return;
            addLine(entry);
            if (pending.length) {
                revealTimer = setTimeout(revealNext, pending.length > 4 ? 140 : pending.length > 1 ? 300 : 450);
            }
        }

        function addLine(entry) {
            prune();
            const line = document.createElement('div');
            line.className = 'lyric is-entering';
            line.textContent = entry.text;
            track.insertBefore(line, nextLine);
            lines.set(entry.key, line);
            requestAnimationFrame(() => requestAnimationFrame(() => line.classList.remove('is-entering')));
            // Lines that scrolled out of the analyzer's short log stay as the faded top
            const ordered = [...lines.values()];
            const current = ordered.length - 1;
            ordered.forEach((line, i) => {
                const distance = current - i;
                line.classList.toggle('is-current', distance === 0);
                line.classList.toggle('is-past-1', distance === 1);
                line.classList.toggle('is-past-2', distance === 2);
                line.classList.toggle('is-gone', distance >= 3);
            });
            centre();
        }

        // Remove lines that have long faded out above. Done without animation and with the
        // track's offset corrected, so the visible lines do not jump.
        function prune() {
            if (lines.size <= 6) return;
            const before = lines.size;
            const first = [...lines.values()][before - 6];
            const shift = first.offsetTop;
            track.style.transition = 'none';
            while (lines.size > 6) {
                const [key, line] = lines.entries().next().value;
                line.remove();
                lines.delete(key);
            }
            const current = parseFloat((track.style.transform.match(/-?[\d.]+/) || [0])[0]);
            track.style.transform = `translateY(${Math.round(current + shift)}px)`;
            void track.offsetHeight;  // apply the jump before the transition comes back
            track.style.transition = '';
        }

        // Keep the current line in the middle of the window: the track glides, the window stays
        function centre() {
            const ordered = [...lines.values()];
            const current = ordered[ordered.length - 1];
            if (!current) return;
            const offset = lyrics.clientHeight / 2 - (current.offsetTop + current.offsetHeight / 2);
            track.style.transform = `translateY(${Math.round(offset)}px)`;
        }

        function render(state) {
            setPhase(state.phase);
            const log = state.log || [];
            const entries = log.map(line => ({key: line.at + '|' + line.text, text: line.text}));
            if (state.phase === 'failed' && state.error) entries.push({key: 'error', text: state.error});
            showLines(entries);
        }

        // The old behaviour, for when there is no live feed
        function startFallback() {
            if (fallbackTimer || everWorked) return;
            let current = 0;
            const advance = () => {
                if (current >= FALLBACK_LINES.length) return;
                const [phase, text] = FALLBACK_LINES[current];
                setPhase(phase);
                showLines(FALLBACK_LINES.slice(0, current + 1).map(([, t], i) => ({key: 'fallback' + i, text: t})));
                current++;
            };
            advance();
            fallbackTimer = setInterval(advance, 4000);
        }

        async function poll(url) {
            const mine = ++request;
            try {
                const response = await fetch(url, {headers: {'X-Requested-With': 'XMLHttpRequest'}});
                if (mine !== request) return;
                if (response.status === 404 && !everWorked && failures < 6) { failures++; return; }  // not registered yet
                if (!response.ok) throw new Error('HTTP ' + response.status);
                const state = await response.json();
                if (!state.success) throw new Error(state.error || 'no progress');
                if (!everWorked && fallbackTimer) { clearInterval(fallbackTimer); fallbackTimer = null; resetLyrics(); }
                everWorked = true;
                failures = 0;
                render(state);
            } catch (e) {
                failures++;
                if (!everWorked && failures >= 3) startFallback();
            }
        }

        window.addEventListener('resize', () => { if (pollTimer || fallbackTimer) centre(); });

        return {
            start(url) {
                this.stop();
                started = Date.now();
                failures = 0;
                everWorked = false;
                setPhase('baseline');
                resetLyrics();
                showLines([{key: 'start', text: 'Starting'}]);
                if (part('clock')) {
                    part('clock').textContent = '0:00';
                    clockTimer = setInterval(() => { part('clock').textContent = clock((Date.now() - started) / 1000); }, 500);
                }
                pollTimer = setInterval(() => poll(url), 700);
                setTimeout(() => poll(url), 250);
            },
            stop() {
                [pollTimer, clockTimer, fallbackTimer].forEach(t => t && clearInterval(t));
                pollTimer = clockTimer = fallbackTimer = null;
                clearTimeout(revealTimer);
                revealTimer = null;
                pending = [];
                request++;
            },
        };
    }

    window.ProgressOverlay = {newId, attach};
})();
