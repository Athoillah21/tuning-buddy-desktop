# Tuning Buddy website

Two static pages for the desktop app, each fitting on one screen:

- **`index.html`, the landing page.** On the left, what it is and the download button. On the
  right, a demo of one analysis (paste a query, the Analyze overlay, the results), with what the
  app does shown as lyrics underneath, in time with the demo.
- **`guide.html`, the guide.** How a DBA uses the app, in five animated parts that play one after
  another: add the AI key, connect a database, generate dummy data, test a slow query, get the
  PDF report. `guide.html#3` opens at part 3.

| File | What it is |
|---|---|
| `index.html`, `site.js` | The landing page and its demo |
| `guide.html`, `guide.css`, `guide.js` | The guide: its page, its mock screens and its five scripts |
| `site.css` | The look of both pages, taken from the setup window (`desktop-apps/setup_ui/web/app.css`) |
| `common.js` | What both demos use: a run that can be paused, typing, the step list, the lyrics |
| `favicon.svg` | The gauge logo |

Nothing is fetched and nothing is real: every screen is a mock, and the numbers in them are
examples.

## Preview

Open `index.html` in a browser. Or serve the folder:

```powershell
python -m http.server 8000 --directory desktop-app-website
```

## Publish

There is no build step: upload the folder to any static host (GitHub Pages, Netlify, a web server).

The download button points at
`https://github.com/Athoillah21/tuning-buddy-desktop/releases/latest/download/TuningBuddySetup.exe`,
which always serves the newest release. So the pages need no change when a new version comes out,
as long as the release has a file named `TuningBuddySetup.exe`.
