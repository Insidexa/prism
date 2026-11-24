"""Desktop integration: the OMEN key opens Prism (GNOME custom shortcut).

system/71-prism-omen-key.hwdb gives the key a code (XF86Launch1); this binds it.
"""
from __future__ import annotations

import ast
import shutil
import subprocess

SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
BASE = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"
PATH = BASE + "prism-omen-key/"
KEY = "XF86Launch1"


def _gs(*args: str) -> str:
    return subprocess.run(["gsettings", *args], capture_output=True, text=True, timeout=10).stdout.strip()


def _paths() -> list[str]:
    raw = _gs("get", SCHEMA, "custom-keybindings")
    try:
        return list(ast.literal_eval(raw.removeprefix("@as ")))
    except (ValueError, SyntaxError):
        return []


def bind_omen_key(command: str) -> bool:
    """Make the OMEN key run `command`. Returns False without GNOME."""
    if not shutil.which("gsettings") or SCHEMA not in _gs("list-schemas"):
        return False
    paths = _paths()
    if PATH not in paths:
        _gs("set", SCHEMA, "custom-keybindings", str(paths + [PATH]))
    rel = f"{SCHEMA}.custom-keybinding:{PATH}"
    _gs("set", rel, "name", "Prism (OMEN key)")
    _gs("set", rel, "command", command)
    _gs("set", rel, "binding", KEY)
    return True


def unbind_omen_key() -> None:
    if not shutil.which("gsettings"):
        return
    paths = _paths()
    if PATH in paths:
        _gs("set", SCHEMA, "custom-keybindings", str([p for p in paths if p != PATH]))
    _gs("reset-recursively", f"{SCHEMA}.custom-keybinding:{PATH}")
