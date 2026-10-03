"""
Copy the service sources into build/stage/ so they can share one process.

All three FastAPI services are packaged as `app`, which only works in separate containers.
Here they become ai_service/, analyzer_service/ and report_service/; their internal imports
are relative, so the code runs unchanged. The Docker sources are never modified.
"""
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVICES = HERE.parent / "dockerize-microservices" / "services"
STAGE = HERE / "build" / "stage"

FASTAPI_PACKAGES = {
    "ai_service": SERVICES / "ai" / "app",
    "analyzer_service": SERVICES / "analyzer" / "app",
    "report_service": SERVICES / "report" / "app",
}

WEB_ITEMS = ["advisor", "tuning_buddy", "static", "manage.py"]

IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "tests", "tests.py", "*.sqlite3", "*.db", ".env")


def stage() -> Path:
    if STAGE.exists():
        shutil.rmtree(STAGE)
    STAGE.mkdir(parents=True)

    for name, source in FASTAPI_PACKAGES.items():
        shutil.copytree(source, STAGE / name, ignore=IGNORE)

    for item in WEB_ITEMS:
        source = SERVICES / "web" / item
        if source.is_dir():
            shutil.copytree(source, STAGE / item, ignore=IGNORE)
        else:
            shutil.copy2(source, STAGE / item)

    return STAGE


if __name__ == "__main__":
    print(f"Staged services into {stage()}")
    sys.exit(0)
