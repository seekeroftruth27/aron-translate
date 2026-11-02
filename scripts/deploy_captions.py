#!/usr/bin/env python3
"""
Push the web caption relay (web/captions/) to your PHP host.

This uploads ONLY the code and assets — it never touches your `config.php`
(which holds your tokens) or any live data (`captions.json`, `clips/`,
`archive/`). Files are LF-normalized on the way up so the host stays consistent.

Configure the target once, in the project's `.env` (or the real environment):

    CT_DEPLOY_SSH   user@host        (required)  e.g. me@example.com
    CT_DEPLOY_PATH  remote directory (required)  e.g. public_html/captions
    CT_DEPLOY_PORT  ssh port         (optional, default 22)

Requires SSH KEY auth to the host (no password prompt) and the `ssh`/`scp`
clients on PATH (built into Windows 10+, macOS, and Linux). Test your key with:

    ssh -o BatchMode=yes user@host echo ok

Usage:
    python scripts/deploy_captions.py            # deploy
    python scripts/deploy_captions.py --dry-run  # show what would be sent
"""

import os
import re
import sys
import subprocess
import tempfile
from pathlib import Path

# The relay's code/assets — an explicit allowlist, so a deploy can never clobber
# config.php (your tokens) or the live data (captions.json / clips/ / archive/).
FILES = [
    "publish.php", "feed.php", "clip.php", "archive.php", "session.php",
    "index.html", "config.sample.php", "README.md", ".htaccess",
]


def _dotenv_value(v):
    """A .env value as app/server.py's _load_dotenv reads it: a quoted value
    verbatim, else up to an inline comment (a "#" at the start or after
    whitespace) -- so `KEY=   # note` is blank, not the note."""
    v = v.strip()
    if v[:1] in ("'", '"'):
        end = v.find(v[0], 1)
        return v[1:end] if end != -1 else v[1:]
    return re.split(r"(?:^|\s)#", v, maxsplit=1)[0].strip()


def load_dotenv(root):
    """Merge KEY=VALUE lines from `<root>/.env` into os.environ (real env wins)."""
    env = root / ".env"
    if not env.exists():
        return
    try:
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = _dotenv_value(v)
            if k and k not in os.environ:
                os.environ[k] = v
    except OSError as exc:
        print(f"warning: could not read {env}: {exc}", file=sys.stderr)


def main():
    dry = "--dry-run" in sys.argv[1:]
    root = Path(__file__).resolve().parent.parent
    src = root / "web" / "captions"
    load_dotenv(root)

    ssh_target = (os.environ.get("CT_DEPLOY_SSH") or "").strip()
    remote_path = (os.environ.get("CT_DEPLOY_PATH") or "").strip().rstrip("/")
    port = (os.environ.get("CT_DEPLOY_PORT") or "22").strip()

    if not ssh_target or not remote_path:
        print(
            "Deploy target not configured. Set these in .env (see .env.example):\n"
            "  CT_DEPLOY_SSH=user@host\n"
            "  CT_DEPLOY_PATH=public_html/captions\n"
            "  CT_DEPLOY_PORT=22   (optional)",
            file=sys.stderr,
        )
        sys.exit(2)

    missing = [f for f in FILES if not (src / f).is_file()]
    if missing:
        print(f"error: missing files in {src}: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    ssh_base = ["ssh", "-p", port, "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=accept-new"]
    scp_base = ["scp", "-P", port, "-q", "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=accept-new"]

    print(f"Deploy web/captions/ -> {ssh_target}:{remote_path}/ (port {port})")
    print(f"  files: {', '.join(FILES)}")
    print("  preserved on host: config.php, captions.json, clips/, archive/")
    if dry:
        print("\n--dry-run: nothing sent.")
        return

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        # Strip CR so the host receives clean LF regardless of local checkout style.
        staged = []
        for f in FILES:
            (tmp / f).write_bytes((src / f).read_bytes().replace(b"\r\n", b"\n"))
            staged.append(str(tmp / f))

        # Ensure the remote directory exists, then copy the allowlist into it.
        subprocess.run(ssh_base + [ssh_target, f"mkdir -p '{remote_path}'"], check=True)
        subprocess.run(scp_base + staged + [f"{ssh_target}:{remote_path}/"], check=True)

    # First-time hint: without config.php the relay can't authenticate a publish.
    r = subprocess.run(
        ssh_base + [ssh_target, f"test -f '{remote_path}/config.php' && echo yes || echo no"],
        capture_output=True, text=True,
    )
    if r.stdout.strip() == "no":
        print(
            "\nNOTE: no config.php on the host yet. Create it there:\n"
            f"  cp {remote_path}/config.sample.php {remote_path}/config.php\n"
            "  then set PUBLISH_TOKEN (matching CT_PUBLISH_TOKEN in .env) and,\n"
            "  for archive browsing, ARCHIVE_KEY."
        )

    print(f"\nDone. Uploaded {len(FILES)} files.")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        print(f"\nDeploy failed (exit {exc.returncode}). "
              "Check CT_DEPLOY_SSH / key auth / CT_DEPLOY_PATH.", file=sys.stderr)
        sys.exit(exc.returncode or 1)
