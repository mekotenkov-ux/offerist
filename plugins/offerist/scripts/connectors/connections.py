"""Discover configured connections without printing credentials or calling APIs."""
import argparse
import json
import os
from pathlib import Path

XMLRIVER_NAMES = ("XMLRIVER_USER_ID", "XMLRIVER_KEY", "XMLRIVER_API")


def user_variable(name):
    if os.name != "nt":
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            return winreg.QueryValueEx(key, name)[0]
    except (FileNotFoundError, PermissionError):
        return None


def xmlriver_credentials(env_file=None):
    """Return auth in memory only; an explicit file never loads other services."""
    values = {}
    if env_file is not None:
        for line in Path(env_file).read_text(encoding="utf-8-sig").splitlines():
            name, separator, value = line.strip().partition("=")
            if separator and name.strip() in XMLRIVER_NAMES:
                values[name.strip()] = value.strip().strip('"').strip("'")
    else:
        values = {name: os.environ.get(name) or user_variable(name) or ""
                  for name in XMLRIVER_NAMES}
    user, key = (values.get(name, "").strip() for name in XMLRIVER_NAMES[:2])
    if not (user and key):
        combined = values.get("XMLRIVER_API", "").strip()
        if ":" in combined:
            user, key = combined.split(":", 1)
    if not (user.strip() and key.strip()):
        raise RuntimeError("XMLRiver credentials unavailable; check configured variable names or explicit env file")
    return {"user": user.strip(), "key": key.strip()}


def discover(registry=None, env_file=None):
    registry = registry or os.environ.get("CODEX_CONNECTIONS_REGISTRY") or user_variable("CODEX_CONNECTIONS_REGISTRY")
    if not registry and os.name == "nt":
        registry = Path(os.environ.get("LOCALAPPDATA", "")) / "SEOMEAT/connections/registry.json"
    report = {"registry": str(registry) if registry else None,
              "registry_status": "not_configured", "registered_services": [],
              "api_connectivity": "not_tested"}
    if registry:
        try:
            data = json.loads(Path(registry).read_text(encoding="utf-8-sig"))
            report["registered_services"] = sorted(data.get("connections", {}))
            report["registry_status"] = "read"
        except PermissionError:
            report["registry_status"] = "permission_denied"
        except FileNotFoundError:
            report["registry_status"] = "not_found"
        except (ValueError, OSError):
            report["registry_status"] = "unreadable"
    try:
        xmlriver_credentials(env_file)
        present = True
    except (RuntimeError, OSError):
        present = False
    report["xmlriver"] = {"credentials_present": present, "variable_names": list(XMLRIVER_NAMES),
                          "source": "explicit_file" if env_file else "process_or_windows_user",
                          "registry_entry_required": False}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--xmlriver-env-file", type=Path, help="Explicit private file; only XMLRIVER_* names are read")
    args = parser.parse_args()
    print(json.dumps(discover(args.registry, args.xmlriver_env_file), ensure_ascii=False))


if __name__ == "__main__":
    main()
