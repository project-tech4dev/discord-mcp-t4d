"""Rebuild config.local.toml as a full copy of config.toml, keeping the local values.

Every setting and comment comes from config.toml. A setting the local file
sets is kept, and if it differs from config.toml's default it gets a
`# CUSTOM (default: ...)` line above it. Comment lines starting `# NOTE` just
above a local setting are kept. Running it again changes nothing.

Nothing is written unless the server would load the new file exactly as it
loads the old one. The old file is kept next to it as config.local.toml.bak.
It prints only setting names, never values.

A setting the local file sets keeps its value even when config.toml's default
changes later: it is then marked CUSTOM. Review the CUSTOM lines after a
rebuild, and delete a setting (both lines) to take the new default.

    python -m scripts.sync_local_config [--template config.toml] [--local config.local.toml] [--check]
"""

from __future__ import annotations

import argparse
import re
import os
import shutil
import tempfile
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

KEY = re.compile(r"^([a-z_]+)\s*=\s*")
CUSTOM = "# CUSTOM"
NOTE = "# NOTE"


class SyncError(Exception):
    pass


@dataclass
class Setting:
    first: int  # index of the key line
    count: int  # lines the setting spans
    value: str  # value text, without a trailing comment on a one-line setting
    comment: str  # trailing whitespace and comment on a one-line setting
    notes: list[str] = field(default_factory=list)


def _parses(value: str) -> bool:
    try:
        tomllib.loads(f"x = {value}")
    except tomllib.TOMLDecodeError:
        return False
    return True


def _settings(lines: list[str]) -> dict[tuple[str, str], Setting]:
    out: dict[tuple[str, str], Setting] = {}
    section, i = "", 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("["):
            section = line.split("#")[0].strip()
            i += 1
            continue
        m = KEY.match(line)
        if not m:
            i += 1
            continue
        rest = line[m.end():]
        # A one-line value: the shortest text before a '#' that parses (a '#' can sit inside a string).
        cuts = [j for j, c in enumerate(rest) if c == "#"] + [len(rest)]
        one = next((rest[:j].rstrip() for j in cuts if _parses(rest[:j].rstrip())), None)
        if one is not None:
            setting = Setting(i, 1, one, rest[len(one):])
        else:  # a value over several lines: take lines until it parses
            j = i + 1
            while j < len(lines) and not _parses("\n".join([rest, *lines[i + 1 : j + 1]])):
                j += 1
            if j == len(lines):
                raise SyncError(f"cannot read the value of {m.group(1)}")
            setting = Setting(i, j - i + 1, "\n".join([rest, *lines[i + 1 : j + 1]]), "")
        k = i - 1
        while k >= 0 and lines[k].startswith((CUSTOM, NOTE)):
            if lines[k].startswith(NOTE):
                setting.notes.insert(0, lines[k])
            k -= 1
        out[(section, m.group(1))] = setting
        i += setting.count
    return out


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def sync(template: str, local: str) -> str:
    try:
        want = _merge(tomllib.loads(template), tomllib.loads(local))
    except tomllib.TOMLDecodeError:
        raise SyncError("the local file is not valid TOML") from None
    t_lines, l_lines = template.splitlines(), local.splitlines()
    t_set, l_set = _settings(t_lines), _settings(l_lines)
    unknown = sorted(f"{s}.{k}".lstrip(".") for s, k in set(l_set) - set(t_set))
    if unknown:
        raise SyncError(f"settings not in the template: {unknown}")

    by_line = {s.first: (key, s) for key, s in t_set.items()}
    out: list[str] = []
    i = 0
    while i < len(t_lines):
        if i not in by_line:
            out.append(t_lines[i])
            i += 1
            continue
        (section, name), t = by_line[i]
        mine = l_set.get((section, name))
        if mine is None:
            out.extend(t_lines[i : i + t.count])
        else:
            out.extend(mine.notes)
            if tomllib.loads(f"x = {mine.value}") != tomllib.loads(f"x = {t.value}"):
                default = t.value if t.count == 1 else "see config.toml"
                out.append(f"{CUSTOM} (default: {default})")
            first = f"{name} = {mine.value}"
            if t.comment.strip():
                # Keep the template's comment in its column where the new value fits.
                column = len(f"{name} = {t.value}") + len(t.comment) - len(t.comment.lstrip())
                head, _, tail = first.rpartition("\n")
                last = tail if head else first
                pad = max(column - len(last), 1)
                first = f"{first}{' ' * pad}{t.comment.lstrip()}"
            out.extend(first.splitlines())
        i += t.count

    result = "\n".join(out) + "\n"
    if tomllib.loads(result) != want:
        raise SyncError("the rebuilt file would not keep the same settings")
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--template", default="config.toml")
    ap.add_argument("--local", default="config.local.toml")
    ap.add_argument("--check", action="store_true", help="only say whether the file is up to date")
    args = ap.parse_args(argv)

    from discord_mcp.config import ConfigError, load_config

    local = Path(args.local)
    tmp: Path | None = None
    try:
        if local.is_symlink():
            raise SyncError(f"{local} is a symlink; run it on the real file")
        old_text = local.read_text()
        new_text = sync(Path(args.template).read_text(), old_text)
        if new_text == old_text:
            print(f"{local} is already up to date")
            return 0
        if args.check:
            print(f"{local} is out of date with {args.template}; run without --check to rebuild it")
            return 1
        # A new file only this user can open, given the old file's mode before it replaces it.
        fd, name = tempfile.mkstemp(prefix=local.name + ".", suffix=".new", dir=local.parent)
        tmp = Path(name)
        with os.fdopen(fd, "w") as f:
            f.write(new_text)
        shutil.copymode(local, tmp)
        if load_config(local) != load_config(tmp):
            raise SyncError("the server would load the rebuilt file differently")
        shutil.copy2(local, local.with_name(local.name + ".bak"))
        tmp.replace(local)
    except (OSError, SyncError, ConfigError) as e:
        print(f"not changed: {e}", file=sys.stderr)
        return 1
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
    lines = new_text.splitlines()
    custom = sorted(k for k, s in _settings(lines).items() if s.first > 0 and lines[s.first - 1].startswith(CUSTOM))
    print(f"rebuilt {local} (old copy: {local.name}.bak); changed from defaults: "
          + ", ".join(f"{s.strip('[]')}.{k}".lstrip(".") for s, k in custom))
    return 0


if __name__ == "__main__":
    sys.exit(main())
