import os
from pathlib import Path
import subprocess
import sys


def browser_available():
    executable = os.environ.get('BROWSER_USE_PY', sys.executable)
    try:
        result = subprocess.run(
            [executable, '-c', 'import importlib.util; raise SystemExit(0 if importlib.util.find_spec("browser_use") else 1)'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def main():
    from .settings import load_env_file

    load_env_file()
    print(f'Python: {sys.version.split()[0]}')
    print(f'Browser Python: {os.environ.get("BROWSER_USE_PY", sys.executable)}')
    print(f'browser-use importable: {browser_available()}')
    executable = os.environ.get('BROWSER_USE_EXECUTABLE', r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe')
    print(f'Edge executable exists: {Path(executable).is_file()}')
    print(f'Explicit CDP session configured: {bool(os.environ.get("BROWSER_USE_CDP_URL"))}')
    print(f'AMAP_KEY configured: {bool(os.environ.get("AMAP_KEY"))}')
    print('This check does not connect to Ollama, Amap, or external websites.')


if __name__ == '__main__':
    main()
