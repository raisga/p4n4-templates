"""Provisions the maintenance assistant in Letta. Runs once per `docker compose up`
(the letta-init service) and is safe to run again:

  1. waits for Letta, and for the chat and embedding models it was started with
  2. creates or updates the custom tools in tools/*.py
  3. creates the agent AGENT_NAME on first run: memory blocks from persona.md,
     human.md and equipment.md, archival memory seeded from history.json
     (dated relative to today)
  4. on later runs, only refreshes what the template owns: the tools, the
     tools' secrets (InfluxDB, Letta) and the chat model. Memory is the
     agent's own and is never overwritten.

Standard library only (python:3.13-slim). Settings come from ai/.env through
docker-compose.yml.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
LETTA_URL = os.environ.get("LETTA_URL", "http://letta:8283").rstrip("/")
PASSWORD = os.environ.get("LETTA_SERVER_PASSWORD", "")
AGENT_NAME = os.environ.get("AGENT_NAME", "maintenance-assistant")
# Letta's Ollama provider lists models as ollama/<name>, with Ollama's tag
CHAT_MODEL = os.environ.get("OLLAMA_MODEL", "gemma4:e2b")
EMBED_MODEL = os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text")
WAIT = int(os.environ.get("PROVISION_TIMEOUT", "600"))
# Must not exceed Ollama's context (OLLAMA_CONTEXT_LENGTH): Letta keeps the
# agent's prompt within it, summarizing older messages
CONTEXT = int(os.environ.get("OLLAMA_CONTEXT_LENGTH") or 16384)
# Letta's own tools the agent gets besides its default memory tools
BUILTIN_TOOLS = ["archival_memory_insert", "archival_memory_search"]
# The custom tools reach InfluxDB and Letta with these (the agent's "secrets")
SECRETS = {
    "INFLUXDB_URL": os.environ.get("INFLUXDB_URL", "http://influxdb:8086"),
    "INFLUXDB_TOKEN": os.environ.get("INFLUXDB_TOKEN", ""),
    "INFLUXDB_ORG": os.environ.get("INFLUXDB_ORG", "ming"),
    "INFLUXDB_BUCKET": os.environ.get("INFLUXDB_BUCKET", "raw_telemetry"),
    # Tools run inside the Letta container
    "LETTA_URL": "http://localhost:8283",
    "LETTA_PASSWORD": PASSWORD,
}


def log(message: str) -> None:
    print(f"p4n4: {message}", flush=True)


def api(method: str, path: str, body: object | None = None, timeout: float = 60) -> object:
    request = urllib.request.Request(
        LETTA_URL + path, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {PASSWORD}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{method} {path}: {exc.code} {exc.read().decode(errors='replace')[:500]}") from None
    return json.loads(raw) if raw else None


def handle(models: list[dict], name: str) -> str | None:
    """The Letta handle of Ollama model `name` ("nomic-embed-text" also matches ":latest")."""
    wanted = {f"ollama/{name}", f"ollama/{name}:latest"}
    return next((m["handle"] for m in models if m.get("handle") in wanted), None)


def wait_for_models() -> tuple[str, str]:
    deadline = time.monotonic() + WAIT
    last = ""
    while time.monotonic() < deadline:
        try:
            chat = handle(api("GET", "/v1/models/"), CHAT_MODEL)
            embed = handle(api("GET", "/v1/models/embedding"), EMBED_MODEL)
            if chat and embed:
                return chat, embed
            last = f"Letta lists no {'ollama/' + CHAT_MODEL if not chat else 'ollama/' + EMBED_MODEL}"
        except (OSError, RuntimeError) as exc:
            last = str(exc)
        time.sleep(3)
    sys.exit(f"p4n4: gave up after {WAIT}s: {last}. Letta reads Ollama's models when it starts: "
             "once Ollama has pulled them, run `docker compose restart letta letta-init`.")


def upsert_tools() -> list[str]:
    names = []
    for path in sorted((HERE / "tools").glob("*.py")):
        tool = api("PUT", "/v1/tools/", {"source_code": path.read_text(), "source_type": "python"})
        names.append(tool["name"])
    log(f"tools ready: {', '.join(names)}")
    return names


def find_agent() -> dict | None:
    agents = api("GET", "/v1/agents/?" + urllib.parse.urlencode({"name": AGENT_NAME, "limit": 50}))
    matches = [a for a in agents if a.get("name") == AGENT_NAME]
    return matches[0] if matches else None


def text(name: str) -> str:
    """A memory file, with {date:N} replaced by the date N days ago."""
    today = datetime.date.today()
    return re.sub(r"\{date:(\d+)\}", lambda m: (today - datetime.timedelta(days=int(m.group(1)))).isoformat(),
                  (HERE / name).read_text()).strip()


def create_agent(chat: str, embed: str, tools: list[str]) -> dict:
    agent = api("POST", "/v1/agents/", {
        "name": AGENT_NAME,
        "description": "Maintenance assistant for the site's pumps and fans, with a memory of their history.",
        "agent_type": "letta_v1_agent",
        "model": chat,
        "embedding": embed,
        "memory_blocks": [
            {"label": "persona", "value": text("persona.md"), "limit": 8000},
            {"label": "human", "value": text("human.md"), "limit": 4000},
            {"label": "equipment", "value": text("equipment.md"), "limit": 12000,
             "description": "The site's machines, their sensors, vibration limits and maintenance plan. "
                            "Keep it current: add new machines and lasting facts about existing ones."},
        ],
        "tools": tools + BUILTIN_TOOLS,
        "secrets": SECRETS,
        "context_window_limit": CONTEXT,
        "timezone": os.environ.get("TZ", "UTC"),
        "tags": ["p4n4", "maintenance"],
    }, timeout=120)
    log(f"created agent {AGENT_NAME} ({agent['id']}) on {chat}")
    today = datetime.datetime.now(datetime.timezone.utc)
    history = json.loads((HERE / "history.json").read_text())
    for entry in history:
        when = today - datetime.timedelta(days=entry["days_ago"])
        api("POST", f"/v1/agents/{agent['id']}/archival-memory", {
            "text": f"{when.date().isoformat()} {entry['text']}",
            "tags": entry.get("tags", []),
            "created_at": when.isoformat(),
        }, timeout=120)
    log(f"seeded archival memory with {len(history)} past maintenance records")
    return agent


def update_agent(agent: dict, chat: str, tools: list[str]) -> None:
    full = api("GET", f"/v1/agents/{agent['id']}")
    attached = {t["name"] for t in full.get("tools", [])}
    by_name = {t["name"]: t["id"] for t in api("GET", "/v1/tools/?limit=500")}
    for name in tools + BUILTIN_TOOLS:
        if name not in attached:
            api("PATCH", f"/v1/agents/{agent['id']}/tools/attach/{by_name[name]}")
            log(f"attached tool {name}")
    update = {"secrets": SECRETS, "context_window_limit": CONTEXT}
    if full.get("llm_config", {}).get("handle") not in (None, chat):
        update["model"] = chat
        log(f"switching the agent to {chat}")
    api("PATCH", f"/v1/agents/{agent['id']}", update)
    log(f"agent {AGENT_NAME} ({agent['id']}) is up to date; its memory was left as it is")


def main() -> None:
    log(f"waiting for Letta at {LETTA_URL} and models {CHAT_MODEL}, {EMBED_MODEL}")
    chat, embed = wait_for_models()
    tools = upsert_tools()
    agent = find_agent()
    if agent is None:
        create_agent(chat, embed, tools)
    else:
        update_agent(agent, chat, tools)


if __name__ == "__main__":
    main()
