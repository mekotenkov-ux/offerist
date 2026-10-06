"""Install standalone Offerist skills and preserve their shared resources."""
import argparse
from pathlib import Path
import re
import shutil
import json

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins/offerist"


def install(destination, selected=None, dry_run=False):
    destination = Path(destination).expanduser().resolve()
    available = {p.name: p for p in (PLUGIN / "skills").iterdir() if (p / "SKILL.md").is_file()}
    names = sorted(available if selected is None else set(selected))
    if not names or set(names) - available.keys():
        raise ValueError("Choose existing skill names: " + ", ".join(sorted(available)))
    if destination == ROOT or destination.is_relative_to(PLUGIN):
        raise ValueError("Choose a destination outside the source plugin")
    version = json.loads((PLUGIN / "plugin.json").read_text(encoding="utf-8"))["version"]
    bundle = destination / ".offerist-bundles" / version
    targets = [destination / name for name in names]
    for target in [bundle, *targets]:
        if target.exists() or target.is_symlink():
            raise ValueError("Destination already exists; preserve it: " + str(target))
        if not target.resolve().is_relative_to(destination):
            raise ValueError("Unsafe destination")
    if any(p.is_symlink() for p in PLUGIN.rglob("*")):
        raise ValueError("Source contains symlinks")
    plan = {"destination": str(destination), "bundle": str(bundle), "skills": names, "dry_run": dry_run}
    if dry_run:
        return plan
    bundle.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(PLUGIN, bundle, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name, target in zip(names, targets):
        source = bundle / "skills" / name
        shutil.copytree(source, target)
        for file in target.rglob("*.md"):
            source_file = source / file.relative_to(target)
            def rewrite(match):
                raw = match.group(1)
                link = raw.strip("<>")
                if re.match(r"[a-zA-Z]+:", link) or link.startswith("#"):
                    return match.group(0)
                path, separator, anchor = link.partition("#")
                resolved = (source_file.parent / path).resolve()
                if not resolved.is_relative_to(bundle) or not resolved.exists():
                    raise ValueError("Missing or unsafe source link: " + link)
                if resolved.is_relative_to(source):
                    return match.group(0)
                return "](<" + resolved.as_posix() + (separator + anchor if separator else "") + ">)"
            body = re.sub(r"\]\(([^)]+)\)", rewrite, file.read_text(encoding="utf-8"))
            if file.name == "SKILL.md":
                body += "\n\n## Полный локальный комплект\n\nОбщие документы, скрипты и ресурсы: `" + bundle.as_posix() + "`. Относительные пути общего плагина выполняй от этой папки.\n"
            file.write_text(body, encoding="utf-8")
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--skill", action="append")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(install(args.destination, args.skill, args.dry_run), ensure_ascii=False, indent=2))
    except (ValueError, OSError) as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
