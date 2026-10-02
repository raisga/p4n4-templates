# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6", "jsonschema>=4"]
# ///
"""Validate every template in the registry (or the ones named on the command line).

    uv run scripts/validate.py                  # all templates
    uv run scripts/validate.py mqtt-influx-grafana
    uv run scripts/validate.py --list           # template names as JSON (CI matrix)

Checks that each template is complete, that template.yaml, .p4n4.json and
docker-compose.yml agree with each other, and that every variable the compose
file uses is documented in .env.example. Runs `docker compose config` when the
Docker CLI is available (no daemon needed); pass --no-docker to skip it.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import jsonschema
import yaml

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((ROOT / "schema" / "template.schema.json").read_text())
METADATA_FILE = "template.yaml"
REQUIRED_FILES = (METADATA_FILE, ".p4n4.json", ".env.example", "docker-compose.yml", "README.md")
MANIFEST_SCHEMA_VERSION = 1
# Read by Compose itself rather than interpolated into the file
COMPOSE_ENV_KEYS = {"COMPOSE_PROFILES", "COMPOSE_PROJECT_NAME"}
COMPOSE_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)")


def discover() -> list[Path]:
    return sorted(p.parent for p in ROOT.glob(f"*/{METADATA_FILE}"))


def env_keys(path: Path) -> set[str]:
    keys = set()
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            keys.add(line.partition("=")[0].strip())
    return keys


def host_ports(service: dict) -> set[int]:
    ports = set()
    for entry in service.get("ports", []):
        if isinstance(entry, dict):
            if "published" in entry:
                ports.add(int(entry["published"]))
            continue
        # "HOST:CONTAINER", "IP:HOST:CONTAINER" or "CONTAINER" (no host port)
        parts = str(entry).split("/")[0].split(":")
        if len(parts) >= 2:
            ports.add(int(parts[-2]))
    return ports


def git_tracked(template: Path, name: str) -> bool:
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", name],
        cwd=template,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def validate(template: Path, use_docker: bool) -> list[str]:
    errors: list[str] = []

    missing = [f for f in REQUIRED_FILES if not (template / f).is_file()]
    if missing:
        return [f"missing file: {f}" for f in missing]

    meta = yaml.safe_load((template / METADATA_FILE).read_text())
    schema_errors = sorted(
        jsonschema.Draft202012Validator(SCHEMA).iter_errors(meta), key=lambda e: e.path
    )
    for err in schema_errors:
        where = "/".join(str(p) for p in err.absolute_path) or "(root)"
        errors.append(f"{METADATA_FILE}: {where}: {err.message}")
    if schema_errors:
        return errors

    if meta["name"] != template.name:
        errors.append(f"{METADATA_FILE}: name '{meta['name']}' != directory '{template.name}'")

    # .p4n4.json must describe the same template
    manifest = json.loads((template / ".p4n4.json").read_text())
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        errors.append(f".p4n4.json: schema_version must be {MANIFEST_SCHEMA_VERSION}")
    if manifest.get("layers") != meta["layers"]:
        errors.append(f".p4n4.json: layers {manifest.get('layers')} != {METADATA_FILE} {meta['layers']}")
    expected = {"name": meta["name"], "version": meta["version"]}
    if manifest.get("template") != expected:
        errors.append(f".p4n4.json: template must be {json.dumps(expected)}")

    # template.yaml services must mirror docker-compose.yml
    compose_text = (template / "docker-compose.yml").read_text()
    compose = yaml.safe_load(compose_text)
    compose_services = compose.get("services", {})
    declared = {s["name"]: s for s in meta["services"]}
    for name in sorted(set(compose_services) - set(declared)):
        errors.append(f"{METADATA_FILE}: service '{name}' is in docker-compose.yml but not declared")
    for name in sorted(set(declared) - set(compose_services)):
        errors.append(f"{METADATA_FILE}: service '{name}' is declared but not in docker-compose.yml")
    for name in sorted(set(declared) & set(compose_services)):
        service, spec = compose_services[name], declared[name]
        if host_ports(service) != set(spec.get("ports", [])):
            errors.append(
                f"{METADATA_FILE}: service '{name}' ports {sorted(spec.get('ports', []))} "
                f"!= docker-compose.yml {sorted(host_ports(service))}"
            )
        profiles = service.get("profiles", [])
        if spec.get("profile") not in (profiles[0] if profiles else None,) or len(profiles) > 1:
            errors.append(f"{METADATA_FILE}: service '{name}' profile does not match docker-compose.yml")

    # Every compose variable is documented, and nothing in .env.example is dead
    documented = env_keys(template / ".env.example")
    used = set(COMPOSE_VAR.findall(compose_text))
    for key in sorted(used - documented):
        errors.append(f".env.example: missing {key} (used in docker-compose.yml)")
    for key in sorted(documented - used - COMPOSE_ENV_KEYS):
        errors.append(f".env.example: {key} is not used in docker-compose.yml")

    if (template / ".env").exists() and git_tracked(template, ".env"):
        errors.append(".env is committed; only .env.example may be")

    smoke = meta.get("smoke_test")
    if smoke:
        path = template / smoke
        if not path.is_file():
            errors.append(f"smoke_test {smoke} does not exist")
        elif not path.stat().st_mode & 0o111:
            errors.append(f"smoke_test {smoke} is not executable")

    for path in sorted(template.rglob("*.json")):
        try:
            json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            errors.append(f"{path.relative_to(template)}: invalid JSON ({exc})")

    if use_docker:
        result = subprocess.run(
            ["docker", "compose", "--env-file", ".env.example", "config", "--quiet"],
            cwd=template,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            errors.append(f"docker compose config failed: {result.stderr.strip()}")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("templates", nargs="*", help="template names (default: all)")
    parser.add_argument("--list", action="store_true", help="print template names as JSON and exit")
    parser.add_argument("--no-docker", action="store_true", help="skip `docker compose config`")
    args = parser.parse_args()

    all_templates = discover()
    if args.list:
        print(json.dumps([t.name for t in all_templates]))
        return 0

    by_name = {t.name: t for t in all_templates}
    unknown = [n for n in args.templates if n not in by_name]
    if unknown:
        print(f"unknown template(s): {', '.join(unknown)}", file=sys.stderr)
        return 2
    selected = [by_name[n] for n in args.templates] or all_templates
    if not selected:
        print("no templates found", file=sys.stderr)
        return 1

    use_docker = not args.no_docker and shutil.which("docker") is not None
    if not args.no_docker and not use_docker:
        print("docker not found: skipping `docker compose config`", file=sys.stderr)

    failed = 0
    for template in selected:
        errors = validate(template, use_docker)
        if errors:
            failed += 1
            print(f"FAIL {template.name}")
            for err in errors:
                print(f"     - {err}")
        else:
            print(f"ok   {template.name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
