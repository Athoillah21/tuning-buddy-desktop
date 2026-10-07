# Tuning Buddy

Find out why a PostgreSQL query is slow, and how to make it fast.

Paste a slow query. Tuning Buddy:
1. runs it with `EXPLAIN ANALYZE`;
2. asks the AI provider you choose for index and rewrite ideas;
3. tests each idea on a temporary copy of the tables, and keeps only the ones that are measurably
   faster.

Each analysis ends in a report: the bottleneck, the before-and-after timings, whether a rewrite still
returns the same rows, and how each new index fits the database.

It also includes:
- a pgAdmin-style database browser with a SQL query tool;
- a test-data generator for empty tables;
- 33 benchmark test cases (indexes, joins, JSONB, full text, PostGIS, pgvector, partitions) on a
  bundled demo database.

## Download for Windows

**[Download the latest TuningBuddySetup.exe](../../releases/latest)** from the Releases page.

- Windows 10 or 11, 64-bit. Nothing else to install first: no Python, no Docker, no PostgreSQL.
- Setup offers to load a demo PostgreSQL database (about 400 MB) to try the app on.
- The installer isn't code-signed yet. If Windows SmartScreen says *"Windows protected your PC"*,
  choose **More info → Run anyway**.
- You need an API key for an AI provider: Claude, Gemini, DeepSeek, OpenAI, or any OpenAI-compatible
  service. A local model through Ollama or LM Studio also works, with no key.

The full guide, covering setup, uninstall, your data, security and building it yourself, is in
[desktop-apps/README.md](desktop-apps/README.md).

## Run it with Docker

The same app as four microservices (Django web app, analyzer, AI and report services):

```bash
cd dockerize-microservices
cp .env.example .env      # then fill in SECRET_KEY and ENCRYPTION_KEY
docker compose --profile demo up -d
```

See [dockerize-microservices/README.md](dockerize-microservices/README.md), and
[docs/REPORT_AND_TESTING.md](dockerize-microservices/docs/REPORT_AND_TESTING.md) for how measurements
and reports work.

## Repository layout

| Folder | What it is |
|---|---|
| `dockerize-microservices/services/` | The application: `web` (Django UI), `analyzer`, `ai`, `report` (FastAPI) |
| `dockerize-microservices/infra/` | Docker database setup and the demo database seeds |
| `desktop-apps/launcher/` | Runs the four services in one Windows process, with a WebView2 window |
| `desktop-apps/setup_ui/` | The custom installer and uninstaller window |
| `desktop-apps/installer/` | The Inno Setup script the installer runs silently |
| `desktop-apps/build.ps1` | Builds the app, `TuningBuddySetup.exe` and `TuningBuddyUninstall.exe` |
| `desktop-app-website/` | The download page: a static site with a demo of the app |

The desktop app uses the services' code unchanged. `desktop-apps/stage.py` copies it in at build time.

## Privacy and security

- **What the AI provider sees:** your query, its execution plan, and the names, columns, indexes and
  row counts of the tables it reads. It never sees the rows themselves.
- **Saved secrets:** database passwords and API keys are encrypted. On Windows, their key is protected
  by your Windows account (DPAPI).
- **Only on this PC:** the desktop app listens only on `127.0.0.1` and answers only its own window.
- **Your data stays safe:** Analyze runs your query and the AI's rewrites read-only, so it never
  changes your data.

Details are in the [Security section](desktop-apps/README.md#security).

## License

[MIT](LICENSE). The bundled PostgreSQL, PostGIS and pgvector keep their own licenses; the installer
includes them in `pgsql\LICENSES.txt`.
