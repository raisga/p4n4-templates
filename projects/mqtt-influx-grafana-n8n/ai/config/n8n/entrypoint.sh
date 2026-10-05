#!/bin/sh
# ==============================================================================
# n8n entrypoint: provisions the instance from ai/.env, then starts n8n
# ==============================================================================
#   1. Owner account from N8N_OWNER_EMAIL / N8N_OWNER_PASSWORD (hashed here;
#      n8n applies it on every start, so changing .env changes the login)
#   2. The SMTP credential the workflows send with, from SMTP_* (every start)
#   3. The workflows in /p4n4/workflows, imported and published on first
#      start only, so edits made in the editor survive restarts. Set
#      N8N_REIMPORT_WORKFLOWS=true to overwrite them with the template's.
# ==============================================================================
set -eu

WORKFLOWS="coldChainMonitor coldChainAckForm coldChainReports"
MARKER=/home/node/.n8n/.p4n4-workflows-imported

[ -n "${N8N_OWNER_PASSWORD:-}" ] || { echo "p4n4: N8N_OWNER_PASSWORD is empty; set it in ai/.env" >&2; exit 1; }
N8N_INSTANCE_OWNER_PASSWORD_HASH="$(node -e '
    const bcrypt = require("/usr/local/lib/node_modules/n8n/node_modules/bcryptjs");
    console.log(bcrypt.hashSync(process.env.N8N_OWNER_PASSWORD, 10));')"
export N8N_INSTANCE_OWNER_PASSWORD_HASH
unset N8N_OWNER_PASSWORD

# The credential holds the SMTP password, so it never lives in the template
umask 077
node -e '
    const e = process.env;
    const data = { host: e.SMTP_HOST, port: Number(e.SMTP_PORT || 25),
                   secure: e.SMTP_SECURE === "true", disableStartTls: e.SMTP_STARTTLS === "false" };
    if (e.SMTP_USER) { data.user = e.SMTP_USER; data.password = e.SMTP_PASSWORD || ""; }
    console.log(JSON.stringify([{ id: "coldchainSmtp", name: "Cold chain SMTP", type: "smtp", data }]));
' > /tmp/credentials.json
n8n import:credentials --input=/tmp/credentials.json
rm -f /tmp/credentials.json

if [ ! -f "$MARKER" ] || [ "${N8N_REIMPORT_WORKFLOWS:-false}" = true ]; then
    echo "p4n4: importing workflows"
    n8n import:workflow --separate --input=/p4n4/workflows
    for id in $WORKFLOWS; do
        n8n publish:workflow --id="$id"
    done
    touch "$MARKER"
fi

exec /docker-entrypoint.sh start
