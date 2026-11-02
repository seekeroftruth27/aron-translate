#!/usr/bin/env python3
"""
Deploy script for Church Translate.

Orchestrates:
  1. Version bump (major/minor/patch, default patch)
  2. Git commit with version tag
  3. PyInstaller build (standalone .exe)
  4. Generate deployment docs
  5. Create release notes

Usage:
  python deploy.py              (patch: 1.1.0 -> 1.1.1)
  python deploy.py minor        (minor: 1.1.0 -> 1.2.0)
  python deploy.py major        (major: 1.1.0 -> 2.0.0)
  python deploy.py 1.1.0        (deploy exactly version 1.1.0)

Outputs:
  - dist/church-translate.exe
  - dist/DEPLOY.md (distribution guide)
  - CHANGELOG.md (version history)
  - version.txt (current version)
"""

import os
import sys
import re
import subprocess
import json
from pathlib import Path
from datetime import datetime

# Native Windows consoles default to cp1252, which can't encode the emoji used in
# this script's progress output -- without this the first such print raises
# UnicodeEncodeError and aborts the build. Force UTF-8; no-op where already UTF-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def run(cmd, check=True):
    """Run shell command, return output."""
    print(f"  → {cmd}")
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and result.returncode != 0:
        print(result.stderr)
        raise RuntimeError(f"Command failed: {cmd}")
    return result.stdout.strip()


def get_current_version():
    """Read version from version.txt, or infer from git tags."""
    version_file = Path("version.txt")
    if version_file.exists():
        return version_file.read_text().strip()
    
    # Fallback: try git describe
    try:
        return run("git describe --tags --always", check=False) or "1.0.0"
    except:
        return "1.0.0"


def parse_version(v):
    """Parse semantic version string."""
    m = re.match(r'^v?(\d+)\.(\d+)\.(\d+)', v)
    if not m:
        raise ValueError(f"Invalid version: {v}")
    return tuple(int(x) for x in m.groups())


def format_version(major, minor, patch):
    """Format version tuple."""
    return f"{major}.{minor}.{patch}"


def bump_version(current, level='minor'):
    """Bump version. Level: major|minor|patch."""
    major, minor, patch = parse_version(current)
    
    if level == 'major':
        major += 1
        minor = 0
        patch = 0
    elif level == 'minor':
        minor += 1
        patch = 0
    elif level == 'patch':
        patch += 1
    else:
        raise ValueError(f"Unknown bump level: {level}")
    
    return format_version(major, minor, patch)


def commit_and_tag(version):
    """Commit version bump and create git tag."""
    print(f"\n📌 Committing and tagging v{version}...")
    
    try:
        run("git add version.txt CHANGELOG.md")
        run(f'git commit -m "Release v{version}"')
        run(f'git tag -a v{version} -m "Version {version}"')
        print(f"   ✓ Tagged v{version}")
    except RuntimeError as e:
        print(f"   ⚠ Git commit failed (may already exist): {e}")


def build_exe(version):
    """Build a self-contained standalone .exe with PyInstaller."""
    print(f"\n🔨 Building self-contained executable (v{version})...")
    
    # Ensure venv exists
    venv_py = Path("venv/Scripts/python.exe")
    if not venv_py.exists():
        print("   Creating virtual environment...")
        run(f"{sys.executable} -m venv venv")
    
    # Install deps
    print("   Installing dependencies...")
    run(f"{venv_py} -m pip install -q -r requirements.txt pyinstaller")
    
    # Build a one-file self-contained executable. This bundles the Python runtime
    # and all imported Python packages into the exe so it can run without a
    # separate Python install on the target machine.
    print("   Running PyInstaller (one-file, bundled runtime)...")
    run(f'{venv_py} -m PyInstaller --noconfirm --clean --distpath dist church-translate.spec')
    
    exe_path = Path("dist/church-translate.exe")
    if exe_path.exists():
        size_mb = exe_path.stat().st_size / (1024 * 1024)
        print(f"   ✓ Built {exe_path.name} ({size_mb:.1f} MB)")
        return exe_path
    else:
        raise RuntimeError("Build failed: executable not found")


def generate_deploy_docs(version, exe_path):
    """Generate DEPLOY.md (distribution guide)."""
    print(f"\n📄 Generating deployment docs...")
    
    deploy_md = f"""# Church Translate v{version} - Distribution & Deployment

Built: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

## Quick Start (End User)

1. **Get the .exe and .env**
   ```
   church-translate-v{version}.exe
   .env (copy from .env.example, add your API keys)
   ```

2. **Place them in the same folder**
   ```
   folder/
     church-translate.exe
     .env
     .env.example
   ```

3. **Double-click `church-translate.exe`**
   - Console window opens (shows Flask server logs)
   - Browser automatically opens to `http://localhost:5000`
   - Click **Start Listening**

4. **Stop:** Close the console window

## Configuration

### Required API Key (in `.env`)

```
SONIOX_API_KEY=...                  # Speech-to-text, its own translation and voice (https://console.soniox.com)
```

### Optional

```
ANTHROPIC_API_KEY=sk-ant-...        # Claude translators (https://console.anthropic.com)
OPENAI_API_KEY=...                  # gpt-6 translators, the Router, an extra ear
GEMINI_API_KEY=...                  # Gemini translators, the Gemini Live ear
ELEVENLABS_API_KEY=...              # TTS spoken output (https://elevenlabs.io)
CT_EL_VOICE=...                     # ElevenLabs voice ID for your chosen voice
CT_PORT=5000                        # Port (default 5000; auto-finds free port if busy)
```

## Technical Details

- **Size:** ~80-120 MB (includes Python runtime + all bundled dependencies)
- **OS:** Windows 10/11 64-bit recommended
- **No installation required** — single `.exe`
- **No Python required on target machine** — runtime and dependencies are bundled
- **Self-contained deployment** — all required DLLs and Python packages are included in the build
- **Audio input:** Any WASAPI device (microphone, line-in, loopback recording)
- **Cloud-only:** All processing (Soniox STT, Claude MT, ElevenLabs TTS) happens in cloud; locally runs only Flask broker + in-browser Silero VAD

## Deployment Methods

### Option A: Direct Distribution
- Zip `dist/` folder (includes `.exe`, `.env.example`, bundled assets)
- Distribute to users
- Users copy `.env.example` → `.env`, fill in keys, double-click

### Option B: Hosted (Docker)
- See `docker-compose.yml` (if present) for containerized deployment
- Suitable for shared machine / sermon archive streaming

### Option C: Installer (NSIS)
- Optional: Create `.nsi` script for Windows installer
- Adds "Add to Programs" / uninstall support
- Contact dev for NSIS template

## Troubleshooting

| Issue | Solution |
|-------|----------|
| `⚠ ANTHROPIC_API_KEY … not set` | Create `.env` with your Claude API key |
| `Soniox key not configured` | Add `SONIOX_API_KEY` to `.env` and restart |
| `Speak does nothing` | Add `ELEVENLABS_API_KEY` + `CT_EL_VOICE` to `.env` |
| Microphone doesn't appear | Windows may need permission — try **Settings** > **Privacy & Security** > **Microphone** > Allow this app |
| Server doesn't start | Port 5000 may be in use; launcher auto-finds free port (check console for actual URL) |

## Version History

See `CHANGELOG.md` for release notes.

## Support

- **Repo:** https://github.com/seekeroftruth27/aron-translate
- **Issues:** Report via GitHub Issues

---

Built with ❤️ for the Church.
"""
    
    deploy_path = Path("dist/DEPLOY.md")
    deploy_path.write_text(deploy_md, encoding='utf-8')
    print(f"   ✓ {deploy_path}")


def update_changelog(current, new_version):
    """Update CHANGELOG.md with new version."""
    print(f"\n📝 Updating CHANGELOG.md...")
    
    changelog_path = Path("CHANGELOG.md")
    
    # Read existing
    if changelog_path.exists():
        existing = changelog_path.read_text(encoding='utf-8')
    else:
        existing = "# Changelog\n\nAll notable changes to Church Translate.\n\n"
    
    # Insert new version entry
    today = datetime.now().strftime("%Y-%m-%d")
    new_entry = f"""## [v{new_version}] — {today}

### Changed
- Version bump from v{current} to v{new_version}
- Built standalone Windows executable

"""
    
    # Insert after header
    lines = existing.split('\n')
    for i, line in enumerate(lines):
        if line.startswith('# ') or line.startswith('All notable'):
            if i + 1 < len(lines) and lines[i + 1].startswith('##'):
                lines.insert(i + 2, new_entry)
                break
    else:
        # No existing entries, add after header
        lines.insert(2, new_entry)
    
    changelog_path.write_text('\n'.join(lines), encoding='utf-8')
    print(f"   ✓ {changelog_path}")


def main():
    project_root = Path(__file__).parent
    os.chdir(project_root)
    
    # Determine requested version target
    arg = sys.argv[1] if len(sys.argv) > 1 else 'patch'
    if re.fullmatch(r'\d+\.\d+\.\d+', arg):
        new_version = arg
        bump_label = f"explicit {arg}"
    else:
        bump_level = arg.lower()
        if bump_level not in ('major', 'minor', 'patch'):
            print(f"Usage: python deploy.py [major|minor|patch|x.y.z]")
            sys.exit(1)
        bump_label = bump_level
        current = get_current_version()
        new_version = bump_version(current, bump_level)
        print(f"   {current} → {new_version}")
    
    print("=" * 60)
    print("Church Translate — Deployment Build")
    print("=" * 60)
    
    # 1. Set version target
    print(f"\n📦 Version target ({bump_label})...")
    current = get_current_version()
    if not re.fullmatch(r'\d+\.\d+\.\d+', arg):
        print(f"   {current} → {new_version}")
    else:
        print(f"   {current} → {new_version}")
    
    version_file = Path("version.txt")
    version_file.write_text(new_version, encoding='utf-8')
    
    # 2. Update changelog
    update_changelog(current, new_version)
    
    # 3. Commit & tag
    commit_and_tag(new_version)
    
    # 4. Build exe
    exe_path = build_exe(new_version)
    
    # 5. Generate docs
    generate_deploy_docs(new_version, exe_path)
    
    print("\n" + "=" * 60)
    print("✓ Deployment build complete!")
    print("=" * 60)
    print(f"\nArtifacts:")
    print(f"  - {exe_path}")
    print(f"  - dist/DEPLOY.md")
    print(f"  - CHANGELOG.md")
    print(f"  - version.txt ({new_version})")
    print(f"\nNext: Zip dist/ folder or create installer.")
    print()


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(f"\n❌ Error: {e}")
        sys.exit(1)
