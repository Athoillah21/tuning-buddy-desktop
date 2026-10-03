"""
Copy a trimmed, relocatable PostgreSQL into build/pgsql/ for the optional demo database.

Source: a Windows PostgreSQL 16 install (EDB layout) that also has PostGIS and pgvector.
Only what the demo server needs is taken: the server programs, the DLLs they and the
bundled extensions load (found by walking PE imports), the catalog files initdb reads,
and the extension scripts. Documentation, pgAdmin, translations, GDAL, SFCGAL and PROJ
grids are left out, which takes ~9 GB down to ~80 MB.

Usage: python stage_pgsql.py [PGHOME]   (default: C:\\Program Files\\PostgreSQL\\16)
"""
import os
import shutil
import sys
from pathlib import Path

import pefile

HERE = Path(__file__).resolve().parent
OUT = HERE / "build" / "pgsql"
DEFAULT_HOME = Path(r"C:\Program Files\PostgreSQL\16")

PROGRAMS = ["postgres.exe", "initdb.exe", "pg_ctl.exe"]

# Extensions available in the demo database: the seeds need postgis and vector, and the
# others are ones the AI recommends (trigram and btree_gin/gist indexes, pg_stat_statements)
EXTENSIONS = ["plpgsql", "postgis", "vector", "pg_trgm", "btree_gin", "btree_gist", "pg_stat_statements"]
EXTENSION_LIBS = ["postgis-3", "vector", "plpgsql", "pg_trgm", "btree_gin", "btree_gist", "pg_stat_statements"]

# Loaded by name at runtime rather than imported: snowball dictionaries (initdb), the encoding
# conversions (any client encoding), and logical/physical replication support
RUNTIME_LIB_PATTERNS = ["dict_snowball.dll", "*_and_*.dll", "euc2004_sjis2004.dll", "libpqwalreceiver.dll", "pgoutput.dll"]

# Visual C++ runtime DLLs may be missing on a clean Windows; they are allowed to ship app-local
VC_RUNTIME = {"vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll"}

SHARE_FILES = ["postgres.bki", "information_schema.sql", "snowball_create.sql", "sql_features.txt",
               "system_constraints.sql", "system_functions.sql", "system_views.sql", "errcodes.txt",
               "postgresql.conf.sample", "pg_hba.conf.sample", "pg_ident.conf.sample"]
SHARE_DIRS = ["timezone", "timezonesets", "tsearch_data"]

LICENSE_NOTE = """\
This folder is a trimmed copy of PostgreSQL 16 with PostGIS and pgvector. Tuning Buddy uses
it only for the optional demo database, which listens on 127.0.0.1.

PostgreSQL        PostgreSQL License        https://www.postgresql.org/about/licence/
pgvector          PostgreSQL License        https://github.com/pgvector/pgvector
PostGIS           GNU GPL v2 or later       https://postgis.net/ (source: https://download.osgeo.org/postgis/source/)
GEOS              GNU LGPL v2.1             https://libgeos.org/
PROJ              MIT                       https://proj.org/
ICU, OpenSSL, libxml2, zlib, lz4, zstd, libiconv, gettext, curl, libtiff, SQLite, protobuf-c:
                  their own open-source licenses; see each project.

The PostgreSQL server license follows.

"""


def _system_dir() -> Path:
    return Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"


def _imports(path: Path) -> list:
    pe = pefile.PE(str(path), fast_load=True)
    pe.parse_data_directories(directories=[
        pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
        pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"],
    ])
    names = []
    for attr in ("DIRECTORY_ENTRY_IMPORT", "DIRECTORY_ENTRY_DELAY_IMPORT"):
        names += [entry.dll.decode() for entry in getattr(pe, attr, [])]
    pe.close()
    return names


def _dependency_closure(home: Path, roots: list) -> tuple:
    """(PostgreSQL DLLs needed by roots, VC runtime DLLs needed). Windows' own DLLs are skipped."""
    found, runtime = {}, set()
    todo = list(roots)
    while todo:
        path = todo.pop()
        if path.name.lower() in found:
            continue
        found[path.name.lower()] = path
        for name in _imports(path):
            lower = name.lower()
            if lower in VC_RUNTIME:
                runtime.add(lower)
                continue
            for folder in (home / "bin", home / "lib"):
                candidate = folder / name
                if candidate.exists():
                    todo.append(candidate)
                    break
    # The runtime DLLs import each other (msvcp140.dll needs vcruntime140_1.dll), and this PC
    # having them all in System32 hides a gap until the demo runs on a PC without the
    # Visual C++ redistributable: follow them too
    pending = list(runtime)
    while pending:
        for name in _imports(_system_dir() / pending.pop()):
            lower = name.lower()
            if lower in VC_RUNTIME and lower not in runtime:
                runtime.add(lower)
                pending.append(lower)
    return found, runtime


def stage(home: Path = DEFAULT_HOME) -> Path:
    if not (home / "bin" / "postgres.exe").exists():
        raise SystemExit(f"No PostgreSQL at {home} (bin\\postgres.exe is missing)")
    missing = [lib for lib in EXTENSION_LIBS if not (home / "lib" / f"{lib}.dll").exists()]
    if missing:
        raise SystemExit(f"{home} lacks {', '.join(missing)}: install PostGIS and pgvector into it first")

    if OUT.exists():
        shutil.rmtree(OUT)
    for folder in ("bin", "lib", "share/extension"):
        (OUT / folder).mkdir(parents=True)

    roots = [home / "bin" / program for program in PROGRAMS]
    roots += [home / "lib" / f"{lib}.dll" for lib in EXTENSION_LIBS]
    for pattern in RUNTIME_LIB_PATTERNS:
        roots += sorted((home / "lib").glob(pattern))
    files, runtime = _dependency_closure(home, roots)

    for path in files.values():
        target = OUT / path.parent.name / path.name  # keeps the bin/ or lib/ split
        shutil.copy2(path, target)
    for name in sorted(runtime):
        shutil.copy2(_system_dir() / name, OUT / "bin" / name)

    share = home / "share"
    for name in SHARE_FILES:
        shutil.copy2(share / name, OUT / "share" / name)
    for name in SHARE_DIRS:
        shutil.copytree(share / name, OUT / "share" / name)
    for extension in EXTENSIONS:
        for path in (share / "extension").glob(f"{extension}*"):
            # postgis* would also match postgis_raster, postgis_topology, ...
            stem = path.name.split("--")[0].removesuffix(".control").removesuffix(".sql")
            if stem == extension:
                shutil.copy2(path, OUT / "share" / "extension" / path.name)
    # PROJ's database, where PostGIS looks for it; the datum grids are not needed
    for proj_db in share.glob("contrib/postgis-*/proj/proj.db"):
        target = OUT / proj_db.relative_to(home)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(proj_db, target)

    license_text = (home / "server_license.txt").read_text(encoding="utf-8", errors="replace") \
        if (home / "server_license.txt").exists() else ""
    (OUT / "LICENSES.txt").write_text(LICENSE_NOTE + license_text, encoding="utf-8")

    size = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file())
    print(f"Staged PostgreSQL from {home} into {OUT}: {len(files)} binaries, {size / 1e6:.0f} MB")
    return OUT


if __name__ == "__main__":
    stage(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_HOME)
    sys.exit(0)
