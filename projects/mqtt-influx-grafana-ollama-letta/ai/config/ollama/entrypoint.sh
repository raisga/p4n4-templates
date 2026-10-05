#!/bin/sh
# ==============================================================================
# Ollama entrypoint
# ==============================================================================
# Starts the Ollama server, then pulls OLLAMA_MODEL and OLLAMA_EMBED_MODEL
# once in the background if they aren't in the ollama-data volume yet. Later
# starts skip the pull, so the stack also comes up offline. A failed pull is
# logged and the server keeps running; restart the container (or `ollama
# pull` by hand) to retry. The healthcheck passes once both are present, and
# Letta waits for it: Letta lists Ollama's models only when it starts.
#
#   OLLAMA_MODEL         Chat model, e.g. gemma4:e2b (empty = pull nothing)
#   OLLAMA_EMBED_MODEL   Embedding model for archival memory, e.g. nomic-embed-text
# ==============================================================================
set -u

ollama serve &
pid=$!
# sh is PID 1: pass `docker stop` on to the server
trap 'kill -TERM "$pid" 2>/dev/null' TERM INT

until ollama list >/dev/null 2>&1; do
    kill -0 "$pid" 2>/dev/null || exit 1
    sleep 1
done
for model in ${OLLAMA_MODEL:-} ${OLLAMA_EMBED_MODEL:-}; do
    if ollama show "$model" >/dev/null 2>&1; then
        echo "p4n4: model $model is ready"
        continue
    fi
    echo "p4n4: pulling $model (first start only; this can take a while)"
    # The progress bar would flood `docker compose logs`: keep it in a file
    # and show its last lines (the error) only if the pull fails
    if ollama pull "$model" >/tmp/pull.log 2>&1; then
        echo "p4n4: model $model is ready"
    else
        # Last lines that aren't progress-bar redraws (terminal escape codes)
        tr '\r' '\n' </tmp/pull.log | grep -v "$(printf '\033')" | grep -v '^\s*$' | tail -n 3 >&2
        echo "p4n4: could not pull $model; restart the container to retry" >&2
    fi
    rm -f /tmp/pull.log
done

# wait returns early (>128) when the trap fires; wait again for the server's status
wait "$pid"
status=$?
[ "$status" -gt 128 ] && { wait "$pid"; status=$?; }
exit "$status"
