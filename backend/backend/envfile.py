"""Project-root ``.env`` loader.

Precedence (highest first):
1. Values really exported in the terminal (non-empty) always win over ``.env``.
2. Values this loader injected earlier are NOT terminal exports. Their names are
   recorded in the private marker variable ``JWD_DOTENV_KEYS``, so a child
   process (runserver autoreload child, re-import) re-reads the file and
   overrides only those names. A recorded name deleted from ``.env`` is removed.
3. An empty environment variable counts as unset and is overridden.
"""
import os
from pathlib import Path

MARKER = 'JWD_DOTENV_KEYS'


def _parse(text):
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        name, value = line.split('=', 1)
        name, value = name.strip(), value.strip()
        if not name.isidentifier():
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        yield name, value


def load_dotenv(path, environ=None):
    """Load ``path`` into ``environ``; return the sorted names injected."""
    environ = os.environ if environ is None else environ
    previous = {n for n in environ.get(MARKER, '').split(',') if n}
    parsed = {}
    path = Path(path)
    if path.is_file():
        for name, value in _parse(path.read_text()):
            parsed[name] = value
    injected = set()
    for name, value in parsed.items():
        if name == MARKER:
            continue
        if name in previous or not environ.get(name):
            environ[name] = value
            injected.add(name)
    for name in previous - set(parsed):
        environ.pop(name, None)
    if injected:
        environ[MARKER] = ','.join(sorted(injected))
    else:
        environ.pop(MARKER, None)
    return sorted(injected)
