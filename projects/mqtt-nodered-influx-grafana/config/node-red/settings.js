/**
 * Node-RED Settings
 * https://nodered.org/docs/user-guide/runtime/configuration
 */

const crypto = require("crypto");

// Editor and Admin API login, from NODE_RED_USER / NODE_RED_PASSWORD in .env.
// With no password set, every login is refused.
const adminUser = process.env.NODE_RED_USER || "admin";
const adminPassword = process.env.NODE_RED_PASSWORD || "";

if (!adminPassword) {
    console.warn("NODE_RED_PASSWORD is not set: the Node-RED editor will refuse every login.");
}

function sha256(value) {
    return crypto.createHash("sha256").update(String(value)).digest();
}

function passwordMatches(password) {
    // Compare fixed-length digests so the comparison time doesn't depend on the input
    return adminPassword !== "" && crypto.timingSafeEqual(sha256(password), sha256(adminPassword));
}

module.exports = {
    // Flow file configuration
    // Path relative to userDir. The FLOWS env var in docker-compose.yml overrides it.
    flowFile: 'flows/flows.json',
    flowFilePretty: true,

    // User directory for storing flows and credentials
    userDir: '/data',

    // Node-RED UI settings
    uiPort: process.env.PORT || 1880,
    uiHost: "0.0.0.0",

    // HTTP admin root
    httpAdminRoot: '/',
    httpNodeRoot: '/',

    // Require a login for the editor and the Admin API
    adminAuth: {
        type: "credentials",
        users: function (username) {
            return Promise.resolve(
                username === adminUser ? { username: adminUser, permissions: "*" } : null
            );
        },
        authenticate: function (username, password) {
            const ok = username === adminUser && passwordMatches(password);
            return Promise.resolve(ok ? { username: adminUser, permissions: "*" } : null);
        }
    },

    // Palette manager. The flow uses core nodes only; nodes you install live
    // in the node-red-data volume, not in this template.
    externalModules: {
        palette: {
            allowInstall: true
        }
    },

    // Disable tours for cleaner UI
    tours: false,

    // Editor theme
    editorTheme: {
        projects: {
            enabled: false
        },
        menu: {
            "menu-item-help": {
                label: "Template README",
                url: "https://github.com/raisga/p4n4-templates/tree/main/projects/mqtt-nodered-influx-grafana"
            }
        }
    },

    // Logging configuration
    logging: {
        console: {
            level: "info",
            metrics: false,
            audit: false
        }
    },

    // Function node settings
    functionGlobalContext: {
        // Add global context items here
    },

    // Export global context with flows
    exportGlobalContextKeys: false,

    // Debugging
    debugMaxLength: 1000,

    // MQTT broker defaults (for convenience)
    mqttReconnectTime: 15000,

    // Disable runtime context menu
    contextStorage: {
        default: {
            module: "memory"
        },
        file: {
            module: "localfilesystem"
        }
    }
};
