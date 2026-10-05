# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6", "jsonschema>=4"]
# ///
"""Validate every template in the registry, projects/<name>/ (or the ones named on the command line).

    uv run scripts/validate.py                  # all templates
    uv run scripts/validate.py mqtt-influx-grafana
    uv run scripts/validate.py --list           # template names as JSON (CI matrix)

Checks that each template is complete, that template.yaml, .p4n4.json and
docker-compose.yml agree with each other, and that every variable the compose
file uses is documented in .env.example. A multi-layer template has those two
files in each layer's <layer>/ directory instead, as p4n4 lays out projects.
Runs `docker compose config` when the Docker CLI is available (no daemon
needed); pass --no-docker to skip it.
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
# One directory per template, named after it
TEMPLATES = ROOT / "projects"
SCHEMA = json.loads((ROOT / "schema" / "template.schema.json").read_text())
THEME_SCHEMA = json.loads((ROOT / "schema" / "theme.schema.json").read_text())
# Fonts the dashboard falls back to when a theme doesn't set them (lib/core/brand.dart)
DEFAULT_FONTS = {"display": "Plus Jakarta Sans", "mono": "JetBrains Mono"}
METADATA_FILE = "template.yaml"
COMPOSE_FILE = "docker-compose.yml"
REQUIRED_FILES = (METADATA_FILE, ".p4n4.json", "README.md")
# In the template root, or in each <layer>/ of a multi-layer template
LAYER_FILES = (".env.example", COMPOSE_FILE)
MANIFEST_SCHEMA_VERSION = 1
# Read by Compose itself rather than interpolated into the file
COMPOSE_ENV_KEYS = {"COMPOSE_PROFILES", "COMPOSE_PROJECT_NAME"}
COMPOSE_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)")
# p4n4-dashboard tabs a manifest's "dashboard" block may list (p4n4_lib.manifest.DASHBOARD_TABS)
DASHBOARD_TABS = {"services", "edge", "agent", "grafana", "video"}
GRAFANA_DASHBOARD_PATH = re.compile(r"^/d/([^/?#]+)")
CAMERA_ID = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def discover() -> list[Path]:
    return sorted(p.parent for p in TEMPLATES.glob(f"*/{METADATA_FILE}"))


def layer_dirs(template: Path, layers: list[str]) -> dict[str, Path]:
    """
    Where each layer's stack lives, as p4n4_lib.layout lays out projects: the
    template root for a single layer, <layer>/ for several. Separate
    directories run as separate Compose projects, so `p4n4 up` starts them in
    dependency order and the ai and edge layers join the p4n4-net network the
    iot layer creates.
    """
    if len(layers) == 1:
        return {layers[0]: template}
    return {name: template / name for name in layers}


def label(template: Path, path: Path) -> str:
    """`path` relative to the template, for messages."""
    return path.relative_to(template).as_posix()


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


def provisioned_dashboard_uids(dirs: list[Path]) -> set[str]:
    uids = set()
    paths = (p for d in dirs for p in d.glob("config/grafana/provisioning/dashboards/**/*.json"))
    for path in paths:
        try:
            uid = json.loads(path.read_text()).get("uid")
        except json.JSONDecodeError:
            continue  # reported by the JSON check
        if uid:
            uids.add(uid)
    return uids


def published_ports(dirs: list[Path]) -> set[int]:
    """Host ports the services of these layer directories publish."""
    ports: set[int] = set()
    for d in dirs:
        services = (yaml.safe_load((d / COMPOSE_FILE).read_text()) or {}).get("services") or {}
        for service in services.values():
            ports |= host_ports(service)
    return ports


def dashboard_errors(template: Path, block: object, dirs: list[Path]) -> list[str]:
    """Check .p4n4.json's optional "dashboard" block, which p4n4-dashboard reads via p4n4-api."""
    if not isinstance(block, dict):
        return [".p4n4.json: dashboard must be an object"]
    errors = [
        f".p4n4.json: dashboard.{key} is not a known setting"
        for key in sorted(set(block) - {"grafana_path", "tabs", "theme", "cameras"})
    ]
    if "theme" in block:
        errors.extend(theme_errors(template, block["theme"]))

    tabs = block.get("tabs")
    if tabs is not None:
        if not isinstance(tabs, list) or not tabs:
            errors.append(".p4n4.json: dashboard.tabs must be a non-empty list")
        elif unknown := [t for t in tabs if t not in DASHBOARD_TABS]:
            errors.append(f".p4n4.json: unknown dashboard.tabs {unknown}")
        elif "grafana" in tabs and "grafana_path" not in block:
            errors.append('.p4n4.json: dashboard.tabs has "grafana" but no grafana_path')
        elif "video" in tabs and "cameras" not in block:
            errors.append('.p4n4.json: dashboard.tabs has "video" but no cameras')
    if "cameras" in block:
        errors.extend(camera_errors(block["cameras"], published_ports(dirs)))

    path = block.get("grafana_path")
    if path is not None:
        match = GRAFANA_DASHBOARD_PATH.match(path) if isinstance(path, str) else None
        if not match:
            errors.append('.p4n4.json: dashboard.grafana_path must look like "/d/<uid>/<slug>"')
        elif match[1] not in provisioned_dashboard_uids(dirs):
            errors.append(f".p4n4.json: dashboard.grafana_path uid '{match[1]}' is not a provisioned dashboard")
    return errors


def camera_errors(cameras: object, published: set[int]) -> list[str]:
    """
    Check dashboard.cameras (p4n4_lib.manifest.camera_errors), and that each
    port-based camera points at a port one of the template's services publishes.
    """
    if not isinstance(cameras, list) or not cameras:
        return [".p4n4.json: dashboard.cameras must be a non-empty list"]
    errors, ids = [], set()
    for i, camera in enumerate(cameras):
        where = f".p4n4.json: dashboard.cameras[{i}]"
        if not isinstance(camera, dict):
            errors.append(f"{where} must be an object")
            continue
        errors += [f"{where}.{k} is not a known setting" for k in sorted(set(camera) - {"id", "name", "url", "port", "path"})]
        cam_id = camera.get("id")
        if not (isinstance(cam_id, str) and CAMERA_ID.match(cam_id)):
            errors.append(f"{where}.id must be lowercase letters, digits and dashes")
        elif cam_id in ids:
            errors.append(f"{where}.id '{cam_id}' is used twice")
        ids.add(cam_id)
        if not (isinstance(camera.get("name"), str) and camera["name"].strip()):
            errors.append(f"{where}.name must be a non-empty string")
        url, port, path = camera.get("url"), camera.get("port"), camera.get("path")
        if (url is None) == (port is None):
            errors.append(f"{where} needs either url or port (with an optional path)")
        elif url is not None:
            if not (isinstance(url, str) and re.match(r"^https?://[^/\s]+", url)):
                errors.append(f"{where}.url must be an absolute http(s) URL")
            if path is not None:
                errors.append(f"{where}.path only goes with port")
        elif isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
            errors.append(f"{where}.port must be a TCP port (1-65535)")
        else:
            if port not in published:
                errors.append(f"{where}.port {port} is not published by any service")
            if path is not None and not (isinstance(path, str) and path.startswith("/")):
                errors.append(f'{where}.path must start with "/"')
    return errors


def theme_errors(template: Path, rel: object) -> list[str]:
    """Check the theme directory .p4n4.json dashboard.theme names (schema/theme.schema.json)."""
    if not isinstance(rel, str) or not rel or Path(rel).is_absolute() or ".." in Path(rel).parts:
        return [".p4n4.json: dashboard.theme must be a directory inside the template"]
    theme = template / rel
    brand_file = theme / "brand.json"
    if not brand_file.is_file():
        return [f"{rel}/brand.json is missing (.p4n4.json dashboard.theme)"]
    try:
        brand = json.loads(brand_file.read_text())
    except json.JSONDecodeError:
        return []  # reported by the JSON check
    schema_errors = sorted(
        jsonschema.Draft202012Validator(THEME_SCHEMA).iter_errors(brand), key=lambda e: e.path
    )
    errors = [
        f"{rel}/brand.json: {'/'.join(str(p) for p in err.absolute_path) or '(root)'}: {err.message}"
        for err in schema_errors
    ]
    if schema_errors:
        return errors

    if "logo" in brand and not (theme / brand["logo"]).is_file():
        errors.append(f"{rel}/brand.json: logo file {brand['logo']} not found")
    # Fonts ship with the theme so the dashboard builds offline: at least the
    # regular weight and the license of each family
    fonts = {**DEFAULT_FONTS, **brand.get("fonts", {})}
    for family in sorted(set(fonts.values())):
        stem = family.replace(" ", "")
        for name in (f"{stem}-Regular.ttf", f"{stem}-LICENSE.txt"):
            if not (theme / "fonts" / name).is_file():
                errors.append(
                    f"{rel}/fonts/{name} is missing "
                    f"(in p4n4-dashboard: dart run tool/brand.dart fonts <path to {rel}>)"
                )
    return errors


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

    layers = layer_dirs(template, meta["layers"])
    multi = len(layers) > 1
    # p4n4 runs a root compose file as the whole project, ignoring <layer>/
    if multi and (template / COMPOSE_FILE).exists():
        errors.append(f"{COMPOSE_FILE}: a multi-layer template keeps one in each layer's directory, not the root")
    missing = [
        label(template, d / f) for d in layers.values() for f in LAYER_FILES if not (d / f).is_file()
    ]
    if missing:
        return errors + [f"missing file: {f}" for f in missing]

    # .p4n4.json must describe the same template
    manifest = json.loads((template / ".p4n4.json").read_text())
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        errors.append(f".p4n4.json: schema_version must be {MANIFEST_SCHEMA_VERSION}")
    if manifest.get("layers") != meta["layers"]:
        errors.append(f".p4n4.json: layers {manifest.get('layers')} != {METADATA_FILE} {meta['layers']}")
    expected = {"name": meta["name"], "version": meta["version"]}
    if manifest.get("template") != expected:
        errors.append(f".p4n4.json: template must be {json.dumps(expected)}")
    if "dashboard" in manifest:
        errors.extend(dashboard_errors(template, manifest["dashboard"], list(layers.values())))

    # template.yaml services must mirror the compose files: service -> (layer, compose service)
    compose_services: dict[str, tuple[str, dict]] = {}
    for layer, d in layers.items():
        compose_label = label(template, d / COMPOSE_FILE)
        compose_text = (d / COMPOSE_FILE).read_text()
        for name, service in (yaml.safe_load(compose_text).get("services") or {}).items():
            if name in compose_services:
                errors.append(f"{compose_label}: service '{name}' is also in layer '{compose_services[name][0]}'")
            compose_services[name] = (layer, service)

        # Every compose variable is documented, and nothing in .env.example is dead
        env_label = label(template, d / ".env.example")
        documented = env_keys(d / ".env.example")
        used = set(COMPOSE_VAR.findall(compose_text))
        for key in sorted(used - documented):
            errors.append(f"{env_label}: missing {key} (used in {compose_label})")
        for key in sorted(documented - used - COMPOSE_ENV_KEYS):
            errors.append(f"{env_label}: {key} is not used in {compose_label}")

        env = label(template, d / ".env")
        if (d / ".env").exists() and git_tracked(template, env):
            errors.append(f"{env} is committed; only .env.example may be")

    declared = {s["name"]: s for s in meta["services"]}
    for name in sorted(set(compose_services) - set(declared)):
        errors.append(f"{METADATA_FILE}: service '{name}' is in a compose file but not declared")
    for name in sorted(set(declared) - set(compose_services)):
        errors.append(f"{METADATA_FILE}: service '{name}' is declared but not in a compose file")
    owners: dict[int, str] = {}
    for name in sorted(set(declared) & set(compose_services)):
        (layer, service), spec = compose_services[name], declared[name]
        compose_label = label(template, layers[layer] / COMPOSE_FILE)
        if multi and spec.get("layer") != layer:
            errors.append(f"{METADATA_FILE}: service '{name}' needs layer: {layer} (it is in {compose_label})")
        elif not multi and "layer" in spec:
            errors.append(f"{METADATA_FILE}: service '{name}': layer is only for multi-layer templates")
        if host_ports(service) != set(spec.get("ports", [])):
            errors.append(
                f"{METADATA_FILE}: service '{name}' ports {sorted(spec.get('ports', []))} "
                f"!= {compose_label} {sorted(host_ports(service))}"
            )
        # Layers run as separate Compose projects, so Compose can't catch these
        for port in sorted(host_ports(service)):
            if port in owners:
                errors.append(f"{compose_label}: service '{name}' publishes port {port}, as '{owners[port]}' does")
            owners[port] = name
        profiles = service.get("profiles", [])
        if spec.get("profile") not in (profiles[0] if profiles else None,) or len(profiles) > 1:
            errors.append(f"{METADATA_FILE}: service '{name}' profile does not match {compose_label}")

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
        for d in layers.values():
            result = subprocess.run(
                ["docker", "compose", "--env-file", ".env.example", "config", "--quiet"],
                cwd=d,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                where = f" ({label(template, d)})" if multi else ""
                errors.append(f"docker compose config failed{where}: {result.stderr.strip()}")

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
