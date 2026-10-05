def maintenance_history(device: str, agent_state: "AgentState", limit: int = 10) -> str:
    """A machine's past incidents and maintenance from your archival memory, newest first. Use it whenever a question is about one machine: a pattern that came before a past failure is the most useful thing to point out.

    Args:
        device: The machine id, e.g. pump-2.
        limit: How many records to return (default 10).

    Returns:
        str: One record per line, each starting with its date.
    """
    # Runs inside Letta's tool sandbox: self-contained, standard library only.
    # Lists this agent's archival memory through Letta's API (LETTA_URL,
    # LETTA_PASSWORD secrets) and keeps the records tagged with the machine,
    # so finding them doesn't depend on the model choosing the right tags.
    import json
    import os
    import re
    import urllib.request

    if not re.fullmatch(r"[A-Za-z0-9_.-]+", device or ""):
        return "Unknown machine id: %r" % device
    request = urllib.request.Request(
        "%s/v1/agents/%s/archival-memory?limit=500&ascending=false" % (os.environ["LETTA_URL"], agent_state.id),
        headers={"Authorization": "Bearer " + os.environ["LETTA_PASSWORD"]})
    with urllib.request.urlopen(request, timeout=60) as response:
        passages = json.load(response)
    records = [p["text"] for p in passages if device in (p.get("tags") or [])]
    records.sort(reverse=True)  # each record starts with its date
    if not records:
        return "No maintenance history for %s in memory." % device
    return "\n".join(records[:max(1, int(limit or 10))])
