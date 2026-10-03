# Tuning Buddy for Windows

A normal Windows installer for Tuning Buddy: no Docker, no Python, no terminal. The app opens in
its own window (Microsoft Edge WebView2) and keeps its data in `%LOCALAPPDATA%\TuningBuddy`.

## Installing

**`TuningBuddySetup.exe`**, from the project's [Releases page](../../../releases/latest), is all
anyone needs. It runs on Windows 10 or 11 (64-bit), and nothing else has to be installed first, not
even PostgreSQL for the demo database. **`TuningBuddyUninstall.exe`**, on the same page, removes it
again. The app installs its own copy too.

`TuningBuddy.exe` on its own does not run anywhere: it needs the folder it was built into. Always
hand out the setup.

1. Double-click `TuningBuddySetup.exe`.
2. If Windows SmartScreen says *"Windows protected your PC"*, choose **More info → Run anyway**.
   The installer is not code-signed, so Windows does not recognise the publisher.
3. Setup is one screen, in light or dark to match Windows. Everything is set up already, so
   **Install** is usually the only click:
   - **Demo database** (on): sample PostgreSQL to try the app on, see
     [Demo database](#demo-database). Loading it takes under a minute.
   - **Desktop shortcut** (off).
   - **Install for**: *Everyone* installs into `C:\Program Files` and Windows asks for admin
     permission (the shield on the button); *Just me* needs no admin.
   - **Location**: *Change* picks another folder.

   When Tuning Buddy is already installed, the button says **Update to <new version>** (or
   **Reinstall**), and *Install for* and *Location* stay as they were.
4. Progress looks like the Analyze overlay: a ring with the percentage, three numbered steps
   (copy, demo database, shortcuts) and the live detail underneath, such as each table as it
   loads. Then **Launch Tuning Buddy**.

   **Cancel** (under the progress) asks once, then:
   - while files are copied: Setup rolls back, and nothing is left on the PC;
   - while the demo database loads: a new install stops after the current table and removes itself
     again; an update keeps the new version and skips the demo database.
5. The first screen in the app is **AI Settings**: add a provider
   (for example DeepSeek, Gemini or Claude) with your API key and click **Test**. The rest of the
   app unlocks once the test passes.
6. With the demo database, the Dashboard now shows **Run test cases**. Otherwise, add your
   PostgreSQL database: **Databases → Add connection**. Use `localhost` for a database on the
   same computer.

### What's in the app

- **Analyze:** paste a slow query. Tuning Buddy tests the AI's recommendations on a full copy
  of the tables it reads, and keeps only the ones that are measurably faster. While it works, the
  progress screen shows the four steps (the current one pulses, finished ones get a ✓), with
  what is really happening scrolling underneath like synced lyrics, for example "Your query takes
  201 ms", "copying large_orders (150 MB) into a test schema" and "#1 index: 97% faster ✓".
- **Results:** the size of every table the query reads, the table where the time goes
  (marked *Bottleneck*), and whether each rewrite still returns the same rows. Each
  recommendation also gets a *Fit with this database* check: duplicate or overlapping
  indexes, the measured size of each new index, and advice for large or partitioned tables.
- **Dashboard:** connections, analyses, the typical gain and the AI provider at a glance. The
  demo database notice can be closed with its ✕, and it stays closed.
- **Databases:** your connections and a pgAdmin-style object browser, on one page.
  - **Add connection** (top right, or *Add* above the tree) opens the form in the right-hand
    panel.
  - Click a connection to see its settings, with **Test**, **Edit** and **Delete**. These are
    there even when the server can't be reached or the password expired, so it can always be
    fixed.
  - The tree on the left goes from each connection to every database on that server (opened
    with the same login), then schemas, then tables, views, materialized views, functions,
    sequences and types.
  - The panel on the right has tabs:
    - server: properties, databases, key settings and running queries;
    - table: columns, indexes (size, how often used, unused ones), partitions, constraints,
      statistics, **DDL** (a CREATE script built from the catalog) and the first 100 rows;
    - view definitions, function source, sequence and type details.
  - Drag the divider to resize the tree.
- **Query tool** (Databases → *Query tool*): read-only by default, one statement at a time in a
  read-only transaction with a 15-second limit. *Explain* and *Explain analyze* show the plan.
  - Turn on **Allow changes** to run INSERT, UPDATE, CREATE and other changes. A multi-statement
    script runs as one transaction: it is committed only if every statement succeeds.
  - It always asks first. For a server that is not on this computer, you also confirm it is not
    a production database.
- **Generate data** (Databases → a schema or table → *Generate data*): fills tables with test
  data, see [Generating test data](#generating-test-data).
- **Test cases:** the 33 benchmark queries (indexes, rewrites, joins, JSONB, full text,
  PostGIS, pgvector, partitions).
  - Click a case to open its SQL (with **Copy**), why it is slow, what fixes it, and the
    expected outcome. From there you can **Run** it, **Open in Query tool**, or **Analyze** it
    with the query filled in.
  - *Run all* runs them one after another, and each ▶ button runs one, with the same live
    progress screen as Analyze.
  - Results stay when you leave the page: each case shows its newest run from History, with a
    report icon next to ▶ (or an ⓘ with the error, if it failed). Deleting that analysis from
    History clears the case. Each case calls your AI
  provider and takes 10 to 60 seconds, and each one is saved to History with its report.
  *Stop after this case* ends a run.

### Uninstalling

Any of these works:

- **Settings → Apps → Tuning Buddy → Uninstall**;
- **Start Menu → Tuning Buddy → Uninstall Tuning Buddy**;
- double-click **`TuningBuddyUninstall.exe`** from the Releases page, which finds the installed copy.

They all open the same window as setup. It shows the version and folder and has one choice,
**Also delete my data** (connections, history, AI keys and the demo database, with its size),
which is off: your data stays, so a reinstall picks up where you left off. **Remove** then stops
the demo database, removes the app (Windows asks for admin if it was installed for everyone) and,
if you chose it, deletes the data folder of the signed-in user.

For a scripted uninstall, run `unins000.exe /VERYSILENT /SUPPRESSMSGBOXES` in the install folder
(the *QuietUninstallString* in the registry), which keeps the data.

### How setup works

`TuningBuddySetup.exe` is a small app of its own (`setup_ui\`): a frameless WebView2 window,
drawn in HTML. The installing is done by an Inno Setup engine (`installer\TuningBuddy.iss`) inside
it, which it runs silently with the choices from the window:

```
TuningBuddySetup.exe
  ├─ the window (setup_ui\web): choices, pre-flight checks, progress, done
  └─ TuningBuddyEngine.exe /SILENT /ALLUSERS|/CURRENTUSER /DIR=… /TASKS=demodb,desktopicon
       ├─ asks Windows for admin (Everyone only), copies the app, writes the uninstall entry
       ├─ /PROGRESSFILE: rewrites a small JSON file with its phase and copy percentage
       └─ TuningBuddy.exe --setup-demo --progress-file …: one line per table it loads
```

- On Windows 11 (22H2 and later) the window is glass: Windows draws its blurred acrylic backdrop
  behind the page (`setup_ui\glass.py`), and the content sits straight on it. Windows 10, high
  contrast, or `TB_SETUP_NO_GLASS=1` give a solid window instead.
- Cancel: the engine runs with `/SILENT`, because `/VERYSILENT` ignores every way of cancelling.
  Its own progress window is made invisible (transparent, click-through, no taskbar button). The
  window creates `<progress file>.cancel`. Before each file, the engine then closes its wizard,
  which is Setup's own Cancel and rolls back. `--setup-demo` checks for the same file between
  tables (exit code 3). A new install cancelled after its files are in place runs its own
  uninstaller from the still-elevated engine, so there is no second admin prompt.
- Before installing, the window checks that Tuning Buddy is closed and that the drive has room.
- `TuningBuddyUninstall.exe` is the same window in uninstall mode. It is installed into the app
  folder, and the engine points the *UninstallString* at it. It runs from a copy in `%TEMP%` (the
  folder it removes is its own) and then runs the stock `unins000.exe` silently.
- Without WebView2 (rare, on an old Windows 10), both fall back to the engine's own wizard,
  skinned to match: light or dark, with the Tuning Buddy artwork (`assets\make_installer_art.py`).

The service code is not forked. The build copies `dockerize-microservices/services/*` and runs
all four services inside one process:

```
TuningBuddy.exe
  ├─ loads keys, picks 4 free loopback ports, sets the env vars the services already read
  ├─ runs the Django migrations (SQLite)
  ├─ starts the demo PostgreSQL, if it was installed (pgsql\, 127.0.0.1 only)
  ├─ uvicorn: AI, analyzer and report services (FastAPI) on 127.0.0.1
  ├─ waitress: the Django web app on 127.0.0.1 (gunicorn does not run on Windows)
  └─ WebView2 window, or the default browser if WebView2 is unavailable
```

The three FastAPI services all use the package name `app`, which only works in separate
containers. `stage.py` copies them as `ai_service`, `analyzer_service` and `report_service`;
their imports are relative, so the code runs unchanged.

## Building

Prerequisites:

- **Python 3.11** from python.org (`py -0` must list `3.11`). Django 4.2 does not run on the
  newer default Python.
- **Inno Setup 6** from <https://jrsoftware.org/isdl.php>, for the installer only.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File desktop-apps\build.ps1                 # app + installer
powershell -NoProfile -ExecutionPolicy Bypass -File desktop-apps\build.ps1 -SkipInstaller  # app folder only
powershell -NoProfile -ExecutionPolicy Bypass -File desktop-apps\build.ps1 -Version 1.3.7
powershell -NoProfile -ExecutionPolicy Bypass -File desktop-apps\build.ps1 -SignCertThumbprint <thumbprint>  # also code-sign
```

- **The demo database** needs PostgreSQL 16 with PostGIS and pgvector on the build machine, see
  [Demo database](#demo-database). Without it, use `-NoDemoDatabase`.
- **Tests:**
  - desktop: `desktop-apps\build\venv\Scripts\python.exe -m pytest desktop-apps\tests`;
  - each FastAPI service: `pytest` in its folder;
  - the web app: `manage.py test advisor`, with `SECRET_KEY`, `DATABASE_URL=sqlite:///:memory:` and an
    `ENCRYPTION_KEY` set.

Output:

| File | What it is |
|---|---|
| `TuningBuddySetup.exe` | The installer to hand to users (replaced by each build; publish it as a GitHub Release download, it is not committed) |
| `TuningBuddyUninstall.exe` | The uninstall window; finds the installed copy (also shipped inside the app) |
| `build\engine\TuningBuddyEngine.exe` | The Inno Setup engine that `TuningBuddySetup.exe` carries and runs silently |
| `dist\TuningBuddy\TuningBuddy.exe` | The app, runnable in place without installing (the whole folder is needed) |

The first build creates `build\venv` and takes a few minutes. Later builds reuse it.

## Running from source

For working on the launcher without freezing it:

```powershell
cd desktop-apps
build\venv\Scripts\python.exe stage.py          # re-run after changing a service
build\venv\Scripts\python.exe -m launcher
```

## Your data

Everything lives in `%LOCALAPPDATA%\TuningBuddy`:

| Path | Contents |
|---|---|
| `config.json` | `SECRET_KEY` and `ENCRYPTION_KEY`, generated on first run and protected by Windows (DPAPI) |
| `web.sqlite3` | Connections, analysis history, recommendations |
| `ai.sqlite3` | AI providers and their (encrypted) API keys |
| `logs\tuningbuddy.log` | Log of the current run (`.log.1` is the previous one) |
| `demo-db\` | The demo database: `data\`, `server.log`, `setup.log` and `demo.json` (port, superuser password) |
| `webview\` | The window's browser profile |

> **Saved secrets belong to your Windows account on this PC.** Database passwords and AI API keys
> are encrypted with `ENCRYPTION_KEY`, and `config.json` stores that key protected by Windows
> (DPAPI). Only your Windows account on this PC can unlock it. A copy of the folder restored for
> another account or on another PC starts with an error. Move the folder away there, and enter the
> passwords and keys again.

Uninstalling keeps this folder unless you turn on **Also delete my data** in the uninstall window.

## Security

Since 1.3.5:

- **Only its own window can use Tuning Buddy.** The app and its services listen only on 127.0.0.1, which
  every program and every Windows account on the PC can reach. So each start makes two new random
  keys:
  - the window opens with one of them and gets a session cookie for it (`/desktop/open/`). Any other
    program, account or web page gets "Open Tuning Buddy from its own window";
  - the web app sends the other one with each call to the AI, analyzer and report services, which
    answer nothing else (except `/health`).
- **The services answer only their own host names** (`127.0.0.1`, `localhost`). That stops a website
  from reaching them through DNS rebinding.
- **A saved AI key stays where it was entered.** Testing or editing a provider with a different
  address or provider type needs the key typed again.
- **Keys at rest:** see [Your data](#your-data). Windows can't keep secrets from programs that run as
  you, which is DPAPI's documented limit. So keep your PC free of untrusted software.
  Without a usable `ENCRYPTION_KEY`, the web app refuses to save a connection instead of storing its
  password in plain text.

Since 1.3.6:

- **Analyze never changes your data.** It measures a query with `EXPLAIN ANALYZE`, which runs it. So
  the query you paste, and every rewrite the AI suggests, must be one statement starting with SELECT,
  WITH, VALUES or TABLE. It runs in a read-only transaction that is rolled back, so PostgreSQL itself
  refuses anything that would write, even a delete hidden in a `WITH`, or `nextval()`.
- **Analyze on another server asks first.** Testing recommendations copies the tables a query reads
  into a temporary `temp_test_*` schema on the analysed server, which costs disk and load there.
  - Each copy carries a marker comment with the time it was made.
  - Copies more than an hour old that a crashed analysis left behind are removed at the next analysis.
  - Schemas without the marker are never touched.
- **Remote connections are encrypted by default.** For a server that is not on this computer, Disable,
  Allow and Prefer can send the password exchange, queries and results unencrypted. So the form asks
  for Require or Verify Full, unless you tick "Allow an unencrypted connection". Existing connections
  keep working; their panel shows "may be unencrypted".
- **An API key never travels over plain http to another computer.** `http://` is refused for a provider
  on another computer when it has a key. Local Ollama and LM Studio, and keyless servers on your
  network, are fine.
- **The demo database has its own random password**, kept in `demo.json` protected by Windows. See
  [Demo database](#demo-database).
- **Setup's test switches** (`TB_SETUP_ENGINE_DIR`, `TB_SETUP_DEBUG_PORT`) only work when running from
  source; the built exes ignore them.
- **What the AI provider sees:** the query, its execution plan, and the names, columns, indexes and row
  counts of the tables it reads. It never sees the rows.
- **Code signing:** `build.ps1 -SignCertThumbprint <thumbprint>` signs every exe, if you have a
  code-signing certificate. Without one, SmartScreen warns about an unknown publisher.

**Your own PostgreSQL servers.** This isn't Tuning Buddy, but worth a check: a PostgreSQL installed
with the EDB installer listens on every network address (`listen_addresses = '*'`). If you only use it
on this PC:
- set `listen_addresses = 'localhost'` in its `postgresql.conf`;
- restart its Windows service.

All of these are off in the Docker stack: the env vars that switch them on (`TB_INTERNAL_TOKEN`,
`SERVICE_ALLOWED_HOSTS`, `TB_DESKTOP_ACCESS_TOKEN`) are set only by the desktop launcher.

## Differences from the Docker stack

- **The demo database is bundled** instead of running in a container (see below). For your own
  database on the same computer, use `localhost`, not `host.docker.internal`.
- **Local LLMs:** the AI Settings presets point at `http://localhost:11434/v1` (Ollama) and
  `http://localhost:1234/v1` (LM Studio).
- **The benchmark suite** runs from the **Test cases** page. The Docker stack has the same
  page, and `manage.py benchmark` as well.
- **App data is SQLite**, separate from the Docker stack's PostgreSQL. AI providers have to be
  added again in the desktop app.

## Demo database

The installer's *Load the demo PostgreSQL database* option installs a trimmed PostgreSQL 16
with PostGIS and pgvector into `pgsql\`. It then creates a private server under
`%LOCALAPPDATA%\TuningBuddy\demo-db` for the user who ran setup, and loads the same data as the
Docker stack's `sampledb`:

| Table | Rows | Notes |
|---|---|---|
| `customers`, `products`, `orders`, `order_items` | 1k, 200, 2k, 5k | small shop schema for joins |
| `large_orders` | 1M | no indexes, no primary key |
| `events` | 300k | JSONB payloads, no GIN index |
| `places` | 200k | PostGIS points, no spatial index |
| `documents` | 20k | pgvector embeddings, no ANN index |
| `measurements` | 600k | 12 monthly range partitions, no indexes |

The tables take about 400 MB. The server keeps its write-ahead log small (128 MB, `wal_level =
minimal`), since a local demo needs no replication; installs from before 1.3.3 shrink theirs the
first time the new version starts. The app starts the server when it opens, on `127.0.0.1` port
55432 (or a free one), and stops it when it closes. It shows up in Databases as
**Demo database** (user `demo`, database `shop`). Its password is random, made for this install
(installs from before 1.3.6 switch from the old public `demo` on their next start). Tuning Buddy
keeps it, so it never expires. To use the demo database from psql or pgAdmin, open the connection in
Databases and click **Show, for other tools**. If you delete that connection, it is not added back.

Only the Windows user who ran setup gets the demo database. To get it after installing without
it, run the installer again with the option ticked. To reset it, close the app and delete
`%LOCALAPPDATA%\TuningBuddy\demo-db` and then reinstall with the option ticked. The Tuning Buddy
data in that folder (connections, history, AI keys) is not touched.

Building it needs PostgreSQL 16 with PostGIS and pgvector on the build machine
(`-PgHome`, default `C:\Program Files\PostgreSQL\16`). `stage_pgsql.py` copies only what the
server needs, by following the DLL imports: about 110 MB, 25 MB compressed. Use
`build.ps1 -NoDemoDatabase` to build an installer without the option.

## Generating test data

The usual reason to use it: a query is slow in production, and you want to tune it on your own
computer without copying production data.

1. Dump only the structure: `pg_dump --schema-only -d proddb -f schema.sql`
2. Create an empty database on this computer and restore it:
   `createdb tuning_copy` then `psql -d tuning_copy -f schema.sql`
3. Add `tuning_copy` with **Databases → Add connection** (host `localhost`).
4. Expand it in the **Databases** tree, pick the schema, then **Generate data**.
5. Tick the tables (the empty ones are ticked for you) and set how much data to add to each,
   in MB or GB. *Set all to* fills in every ticked table at once. The ≈ Rows column is an
   estimate from sample rows.
6. Type the database name to confirm, and click **Generate data**. A progress screen shows each
   table's size and rows as it grows, the overall rate and the time left. **Cancel** stops after
   the current batch and keeps what was loaded.

How the data is made:

- **Order:** parent tables are filled before the tables that refer to them. Foreign keys pick
  random existing parent rows. A key made only of foreign keys (like
  `order_items(order_id, product_id)`) gets distinct combinations.
- **Values** follow the column type, and are realistic for common column names: email, name,
  city, country, status, category, price, quantity, dates and more. Low-cardinality columns
  such as status are skewed, as they are in real data, and about 5% of nullable columns are
  NULL.
- **Constraints and structure:**
  - UNIQUE and primary keys, and simple CHECK constraints (`IN (...)`, ranges, `a >= b`,
    `IS NOT NULL`, quoted column names), are respected.
  - A parent table that is empty blocks its children up front, including parents in other
    schemas.
  - A self-reference (like `parent_id` or `manager_id`) forms a tree, with about 20% of rows at
    the top level.
  - So are enums, domains, identity and serial columns, and range and list partitions.
  - JSONB, arrays, uuid, inet, tsvector, range types, bit, xml, PostGIS geometry and pgvector
    columns get valid values.
- **Afterwards:** every table is ANALYZEd, so the planner has real statistics.
- **Size:** each table stops when its size on disk, indexes included, has grown by the amount
  you chose. Make sure the database server's disk has room for that, plus the same again for
  WAL while loading. The largest amount per table is 100 GB.

Safety: it prefers databases on this computer. For any other host, the page shows warning signs
before you start:
- the database size;
- other sessions connected;
- replicas streaming from the server;
- tables that already have data.

You must tick *I confirm this is not a production database* and type the database name. The
analyzer service checks both again before writing anything, and it never writes to a read-only
replica.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "Tuning Buddy could not start" | Read `%LOCALAPPDATA%\TuningBuddy\logs\tuningbuddy.log` |
| Opens in the browser instead of a window | The WebView2 runtime is missing. Install it from <https://developer.microsoft.com/microsoft-edge/webview2/> |
| SmartScreen warns about the installer | Expected: the installer is not code-signed. Choose *More info → Run anyway* |
| "Tuning Buddy is already running" | Only one copy runs at a time. Close the open window first |
| No Demo database in Databases | Read `%LOCALAPPDATA%\TuningBuddy\demo-db\setup.log` (install) and `server.log` (start) |
| "The demo database could not be set up" during install | Same `setup.log`. The app works without it |
| Generate data stops with a table marked failed | Hover over the ✕ for the reason: usually a CHECK constraint it could not satisfy, or the disk is full. Rows loaded before the failure are kept |
| Stored API keys stopped working | `config.json` was lost or replaced. Re-enter the keys in AI Settings |
