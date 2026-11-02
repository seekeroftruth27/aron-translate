#!/usr/bin/env python3
"""
Launcher for Church Translate — starts the Flask server in the background and
opens a browser window. Handles graceful shutdown and system tray integration.

When frozen with PyInstaller, this becomes the entry point (church-translate.exe).
"""
import os
import re
import sys
import subprocess
import time
import webbrowser
import threading
import atexit
from pathlib import Path

# Fix Windows console encoding so Unicode characters don't crash the launcher.
# Native Windows consoles default to cp1252 which can't encode many characters.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# When running as a PyInstaller bundle, sys.executable points to the exe.
# We need the Python interpreter inside the bundle (_internal/python.exe on Windows).
def get_python_executable():
    if getattr(sys, 'frozen', False):
        # Running as PyInstaller bundle
        bundle_dir = Path(sys.executable).parent
        python_exe = bundle_dir / '_internal' / 'python.exe'
        if python_exe.exists():
            return str(python_exe)
    return sys.executable


def resolve_app_dir():
    """Locate the app directory for both source-tree and frozen builds."""
    if getattr(sys, 'frozen', False):
        roots = []
        meipass = getattr(sys, '_MEIPASS', None)
        if meipass:
            roots.append(Path(meipass).resolve())
        roots.append(Path(sys.executable).resolve().parent)
        roots.append(Path.cwd().resolve())
    else:
        roots = [Path(__file__).resolve().parent, Path.cwd().resolve()]

    for base in roots:
        if not base:
            continue
        for candidate in (base / 'app', base):
            if candidate.exists() and (candidate / 'server.py').exists():
                return candidate

    return (roots[0] / 'app') if roots else Path('app')


def resolve_env_file(app_dir):
    """Locate a runtime env file from the bundled app or executable folder."""
    candidates = [
        app_dir / '.env',
        app_dir.parent / '.env',
        Path(sys.executable).resolve().parent / '.env',
        Path.cwd().resolve() / '.env',
        # Notepad's default "Text documents" type saves ".env" as ".env.txt"
        Path(sys.executable).resolve().parent / '.env.txt',
        Path.cwd().resolve() / '.env.txt',
        app_dir / '.env.example',
        app_dir.parent / '.env.example',
        Path(sys.executable).resolve().parent / '.env.example',
        Path.cwd().resolve() / '.env.example',
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _dotenv_value(v):
    """A .env value as app/server.py's _load_dotenv reads it: a quoted value
    verbatim, else up to an inline comment (a "#" at the start or after
    whitespace) -- so `KEY=   # note` is blank, not the note."""
    v = v.strip()
    if v[:1] in ("'", '"'):
        end = v.find(v[0], 1)
        return v[1:end] if end != -1 else v[1:]
    return re.split(r"(?:^|\s)#", v, maxsplit=1)[0].strip()


def load_env_file(path):
    """Parse KEY=VALUE lines from `path` into os.environ and return the set of
    names actually set from the file.

    This mirrors server.py's _load_dotenv() but runs HERE, in the launcher,
    BEFORE the server module is imported/started. In a PyInstaller build the
    server module's __file__ points into the temporary _MEIPASS extraction dir,
    so its own .env lookup misses the real file sitting next to the executable --
    which is why keys in a bundled .env were silently ignored. resolve_env_file()
    finds the real file; loading it here makes the .env method work in the frozen
    build too. Uses setdefault semantics (a real environment variable is never
    overridden), and the value propagates to the server whether it runs in-process
    (frozen: a thread) or as a subprocess (source: inherits os.environ).
    """
    loaded = set()
    try:
        # utf-8-sig: Notepad may start the file with a byte-order mark, which
        # would otherwise glue itself to the first key's name and hide that key.
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = _dotenv_value(v)
                if k and k not in os.environ:   # real env vars win (setdefault)
                    os.environ[k] = v
                    loaded.add(k)
    except OSError as exc:
        print(f"WARNING: could not read {path}: {exc}")
    return loaded


STARTER_HEAD = """\
# Áron settings. To start, paste your Soniox key straight after the = on the
# SONIOX_API_KEY= line below (no spaces, no quotes), then save (Ctrl+S) and
# close Notepad. Áron carries on by itself. Get a key at https://console.soniox.com.
# Everything else in this file is optional; the comments say what each line does.
"""

# Linux/macOS: the key is asked for in the terminal and written in here.
STARTER_HEAD_TERMINAL = """\
# Áron settings. Your Soniox key goes straight after the = on the
# SONIOX_API_KEY= line below (no spaces, no quotes); on the first run Áron asks
# for it in the terminal and writes it in. Get a key at https://console.soniox.com.
# Everything else in this file is optional; the comments say what each line does.
"""


def write_starter_env(app_dir, exe_dir):
    """First run with no keys and no .env: write a starter .env next to the
    program, from the .env.example the exe carries, with its placeholder values
    blanked (copied as they are, "your-soniox-key-here" would pass for a key)
    and its "copy this file" opening replaced by what to do. Returns the new
    file, or None when there is no example to copy or the folder is read-only
    (Program Files): then the console instructions are all there is."""
    for example in (app_dir.parent / '.env.example', app_dir / '.env.example',
                    exe_dir / '.env.example'):
        if example.exists():
            break
    else:
        return None
    target = exe_dir / '.env'
    try:
        lines = example.read_text(encoding='utf-8-sig').splitlines()
        # drop the example's own opening comment ("Copy this file to .env ...")
        while lines and lines[0].startswith('#'):
            lines.pop(0)
        body = [re.sub(r'^([A-Z0-9_]+)=.*\byour\b.*$', r'\1=', l) for l in lines]
        head = STARTER_HEAD if sys.platform == 'win32' else STARTER_HEAD_TERMINAL
        target.write_text(head + '\n'.join(body) + '\n', encoding='utf-8')
    except OSError as exc:
        print(f"Could not write {target}: {exc}")
        return None
    return target


def edit_in_notepad(path):
    """Open `path` in Notepad and wait for it to close (Windows); elsewhere just
    say where the file is."""
    if sys.platform != 'win32':
        print(f"Open {path}, add your key, and start the program again.")
        return False
    try:
        subprocess.call(['notepad.exe', str(path)])
        return True
    except OSError as exc:
        print(f"Could not open Notepad ({exc}); edit {path} by hand.")
        return False


def ask_key_in_terminal(path):
    """Linux/macOS: ask for the Soniox key in the terminal and write it into
    `path`. With no terminal (started from a file manager) just say where the
    file is."""
    if not sys.stdin or not sys.stdin.isatty():
        print(f"Open {path}, add your key, and start the program again.")
        return False
    try:
        key = input("Paste your Soniox key and press Enter: ").strip().strip('"\'')
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if not key:
        print(f"No key given. Add it to {path} and start the program again.")
        return False
    try:
        text = path.read_text(encoding='utf-8-sig')
        line = f"SONIOX_API_KEY={key}"
        if re.search(r'^SONIOX_API_KEY=.*$', text, flags=re.M):
            text = re.sub(r'^SONIOX_API_KEY=.*$', lambda m: line, text, count=1, flags=re.M)
        else:
            text += ('' if text.endswith('\n') else '\n') + line + '\n'
        path.write_text(text, encoding='utf-8')
        print(f"Saved to {path}.")
        return True
    except OSError as exc:
        print(f"Could not write {path}: {exc}")
        return False


def find_free_port(start=5000, end=5100):
    """Find an available TCP port."""
    import socket
    for port in range(start, end):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(('127.0.0.1', port))
            sock.close()
            return port
        except OSError:
            continue
    raise RuntimeError("No free port found in range 5000-5100")


def main():
    # Resolve the app directory for both source-tree and frozen builds.
    app_dir = resolve_app_dir()
    server_py = app_dir / 'server.py'
    if not server_py.exists():
        print(f"ERROR: server.py not found at {server_py}")
        input("Press Enter to exit...")
        sys.exit(1)

    # Find available port
    port = find_free_port()
    os.environ['CT_PORT'] = str(port)
    os.environ['CT_OPEN'] = '0'

    # API keys can come from EITHER the system environment (set once as Windows
    # environment variables) or a .env file next to the program. The system
    # environment always wins. SONIOX alone is required (speech-to-text, its own
    # translation and voice); the LLM keys and ELEVENLABS are optional.
    REQUIRED = ('SONIOX_API_KEY',)
    OPTIONAL = ('ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'GEMINI_API_KEY', 'XAI_API_KEY',
                'ELEVENLABS_API_KEY')

    # Record which required keys the true system environment supplies BEFORE we
    # merge in the .env, so we can report each key's real source.
    from_system = {k for k in REQUIRED if os.environ.get(k)}

    # Load the .env ourselves (see load_env_file) so it works in the frozen build,
    # where the server's own lookup can't find the file next to the executable.
    env_file = resolve_env_file(app_dir)
    have_env_file = env_file.exists() and env_file.name != '.env.example'
    from_dotenv = load_env_file(env_file) if have_env_file else set()

    have_all_required = all(os.environ.get(k) for k in REQUIRED)

    # The exe with no Soniox key: on a first run (no .env beside it) make the
    # .env for the operator, rather than asking them to copy a file they never
    # downloaded; with a .env that still lacks the key, open that one again.
    # Once Notepad closes (Linux/macOS: once the key is typed in the terminal),
    # read it and carry on.
    exe_dir = Path(sys.executable).resolve().parent
    if not have_all_required and getattr(sys, 'frozen', False):
        made = not have_env_file
        starter = env_file if have_env_file else write_starter_env(app_dir, exe_dir)
        if starter:
            print("=" * 60)
            print("Áron needs your Soniox key.")
            print("=" * 60)
            print(("A settings file was made for you: " if made else "Your settings file: ") + str(starter))
            if sys.platform == 'win32':
                print("It is open in Notepad. Paste your key after SONIOX_API_KEY=,")
                print("save (Ctrl+S) and close Notepad. Áron starts when you do.")
            print()
            got_key = (edit_in_notepad(starter) if sys.platform == 'win32'
                       else ask_key_in_terminal(starter))
            if got_key:
                # Forget the blanks the first read set, or the re-read (which
                # never overrides) would keep them over the key just pasted.
                for k in from_dotenv:
                    if not os.environ.get(k):
                        os.environ.pop(k, None)
                env_file, have_env_file = starter, True
                from_dotenv = load_env_file(starter)
                have_all_required = all(os.environ.get(k) for k in REQUIRED)

    if have_all_required:
        sources = []
        if from_system:
            sources.append("system environment")
        if from_dotenv & set(REQUIRED):
            sources.append(str(env_file))
        print("API keys loaded" + (" (" + " + ".join(sources) + ")" if sources else "") + ".")
        extra = [k.split('_')[0].title() for k in OPTIONAL if os.environ.get(k)]
        if extra:
            print("  (also: " + ", ".join(extra) + ")")
    else:
        missing = [k for k in REQUIRED if not os.environ.get(k)]
        print("=" * 60)
        print("SETUP REQUIRED: missing API key(s): " + ", ".join(missing))
        print("=" * 60)
        print()
        print("Provide your API keys in EITHER of these ways:")
        print()
        print("  1. Set them as Windows environment variables (recommended for a")
        print("     permanent install), then restart this program:")
        print("       SONIOX_API_KEY (+ any of " + ", ".join(OPTIONAL) + ")")
        print()
        print("  2. Put them in a .env file next to the program:")
        print(f"       {env_file if have_env_file else (exe_dir / '.env')}")
        if not have_env_file:
            print("     (copy .env.example to .env and fill it in)")
        print()
        print("You need:")
        print("  - SONIOX_API_KEY (from https://console.soniox.com) -- enough on its own")
        print("  - optional, for better translation: ANTHROPIC_API_KEY, OPENAI_API_KEY,")
        print("    GEMINI_API_KEY or XAI_API_KEY; ELEVENLABS_API_KEY for its voices")
        found = [k for k in REQUIRED if os.environ.get(k)]
        if found:
            print()
            print(f"  Found so far: {', '.join(found)}")
        print()
        print("Continuing anyway; add the missing key(s) before using cloud features.")
        print()

    # Start Flask server
    print(f"Starting Church Translate server on port {port}...")
    print()

    server_process = None
    if getattr(sys, 'frozen', False):
        import importlib.util

        spec = importlib.util.spec_from_file_location('church_server', server_py)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Could not load server module from {server_py}")

        server_module = importlib.util.module_from_spec(spec)
        sys.modules['church_server'] = server_module
        spec.loader.exec_module(server_module)

        server_thread = threading.Thread(
            target=server_module.app.run,
            kwargs={'host': '0.0.0.0', 'port': server_module.PORT, 'threaded': True},
            daemon=True,
        )
        server_thread.start()
    else:
        python_exe = get_python_executable()
        server_process = subprocess.Popen(
            [python_exe, str(server_py)],
            cwd=str(app_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

    # Register cleanup for subprocess-based runs only.
    def cleanup():
        if server_process is not None and server_process.poll() is None:
            print("\nShutting down server...")
            server_process.terminate()
            try:
                server_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server_process.kill()

    atexit.register(cleanup)

    # Wait for server to be ready (up to 30 seconds)
    print("Waiting for server to start...")
    url = f'http://localhost:{port}'
    max_tries = 30
    for attempt in range(max_tries):
        try:
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            result = sock.connect_ex(('127.0.0.1', port))
            sock.close()
            if result == 0:
                print("✓ Server ready!")
                break
        except Exception:
            pass
        time.sleep(1)
    else:
        print(f"⚠ Server did not respond after {max_tries}s — trying to open browser anyway...")

    # Open browser
    print(f"Opening {url} in browser...")
    webbrowser.open(url)
    print()
    print("=" * 60)
    print("Church Translate is running")
    print("=" * 60)
    print(f"URL: {url}")
    print("Close this window to stop the server")
    print()

    # Keep the launcher alive so the bundled server stays up.
    try:
        if server_process is None:
            while True:
                time.sleep(1)
        for line in server_process.stdout:
            if line:
                print(f"[server] {line.rstrip()}")
    except KeyboardInterrupt:
        print("\nKeyboard interrupt received")
    finally:
        cleanup()


if __name__ == '__main__':
    main()
