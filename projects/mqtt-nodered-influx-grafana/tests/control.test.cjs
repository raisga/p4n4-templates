const assert = require("node:assert/strict");
const { test } = require("node:test");
const nodes = require("../config/node-red/flows/flows.json");

function controller() {
    let now = Date.UTC(2026, 0, 1);
    const data = {};
    const flow = { get: key => data[key], set: (key, value) => { data[key] = value; } };
    const settings = { STALE_AFTER: 6, VALVE_MAX_RUN: 12, VALVE_LOCKOUT: 30 };
    class Clock extends Date {
        constructor(...args) { super(...(args.length ? args : [now])); }
        static now() { return now; }
    }
    const functions = Object.fromEntries(nodes.filter(node => node.type === "function").map(node => [
        node.id, new Function("msg", "flow", "env", "node", "Date", node.func)
    ]));
    const run = (id, msg = {}) => functions[id](
        msg, flow, { get: key => settings[key] }, { status() {}, warn() {} }, Clock
    );
    return {
        data,
        run,
        advance: ms => { now += ms; },
        mode: mode => run("fn-decide", run("fn-mode", {
            topic: "actuators/zone-a/valve/mode", payload: mode
        })),
        watchdog: () => run("fn-watchdog")[0].map(msg => run("fn-decide", msg))
    };
}

test("a forced-on valve still times out and cannot bypass its lockout", () => {
    const c = controller();
    assert.equal(c.mode("on").payload.state, "on");
    c.advance(7000);
    assert.deepEqual(c.watchdog(), []); // Manual mode does not need a soil sensor.
    c.advance(5000);
    const [cutoff] = c.watchdog();
    assert.equal(cutoff.payload.state, "off");
    assert.match(cutoff.payload.reason, /locked out/);
    assert.equal(c.mode("on"), null);
    assert.equal(c.data.last["zone-a/valve"].state, "off");
    c.advance(30000);
    assert.equal(c.mode("on").payload.state, "on");
});

test("changing mode while continuously on does not reset the maximum run timer", () => {
    const c = controller();
    c.data.auto = { "zone-a/valve": { state: "on", reason: "dry" } };
    const first = c.mode("auto");
    c.advance(11000);
    const manual = c.mode("on");
    assert.equal(manual.payload.onSince, first.payload.onSince);
    c.advance(1000);
    assert.equal(c.watchdog()[0].payload.state, "off");
});

test("the continuous-on timer survives retained-command recovery", () => {
    const c = controller();
    const first = c.mode("on");
    c.advance(11000);
    c.data.auto = { "zone-a/valve": { state: "on", reason: "dry" } };
    const changed = c.mode("auto");
    delete c.data.last;
    c.run("fn-remember", { topic: changed.topic, payload: changed.payload });
    assert.equal(c.data.last["zone-a/valve"].onSince, first.payload.onSince);
    c.advance(1000);
    assert.equal(c.watchdog()[0].payload.state, "off");
});

test("replaying a mode before its retained command cannot restart the timer", () => {
    const c = controller();
    const first = c.mode("on");
    c.advance(11000);
    delete c.data.last;
    const replayed = c.mode("on");
    const corrected = c.run("fn-remember", first);
    assert.equal(corrected.payload.onSince, first.payload.onSince);
    assert.equal(corrected.retain, true);
    assert.equal(c.run("fn-remember", replayed), null);
    assert.equal(c.run("fn-remember", corrected), null);
    c.advance(1000);
    assert.equal(c.watchdog()[0].payload.state, "off");
});

test("automatic mode keeps its stale-sensor cutoff without affecting forced fans", () => {
    const c = controller();
    c.data.auto = { "zone-a/valve": { state: "on", reason: "dry" } };
    c.mode("auto");
    c.data.last["zone-a/fan"] = { state: "on", mode: "on", at: new Date(0).toISOString() };
    const [cutoff] = c.watchdog();
    assert.equal(cutoff.payload.state, "off");
    assert.match(cutoff.payload.reason, /soil_moisture/);
    assert.equal(c.data.last["zone-a/fan"].state, "on");
});

test("missing, invalid or future retained valve start times fail closed", () => {
    for (const at of [undefined, "not a date", "9999-12-31T23:59:59Z"]) {
        const c = controller();
        c.data.mode = { "zone-a/valve": "on" };
        const retained = {
            topic: "actuators/zone-a/valve/set", payload: { state: "on", mode: "on", at }
        };
        c.run("fn-remember", retained);
        assert.equal(c.watchdog()[0].payload.state, "off");
        const replayed = controller();
        replayed.mode("on");
        const corrected = replayed.run("fn-remember", retained);
        assert.equal(replayed.run("fn-remember", corrected), null);
        assert.equal(replayed.watchdog()[0].payload.state, "off");
    }
});
