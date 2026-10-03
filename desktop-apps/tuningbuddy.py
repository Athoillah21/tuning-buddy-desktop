"""PyInstaller entry script (a package module cannot be the entry point)."""
from launcher.main import run

if __name__ == "__main__":
    run()
