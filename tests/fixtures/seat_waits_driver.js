import { readFileSync, readdirSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { createInterface } from "node:readline";
import seatWaits from "../../extensions/seat-waits.js";

const handlers = new Map();
const timers = new Map();
const notices = [];
let nextTimer = 0;
let context;
const children = () => {
  if (process.platform !== "linux") {
    const result = spawnSync("ps", ["-axo", "pid=,ppid="], { encoding: "utf8" });
    if (result.error || result.status !== 0) throw result.error || new Error(result.stderr);
    return result.stdout.trim().split("\n").flatMap(line => {
      const [pid, parent] = line.trim().split(/\s+/).map(Number);
      return parent === process.pid && pid !== result.pid ? [pid] : [];
    });
  }
  return [...new Set(readdirSync(`/proc/${process.pid}/task`).flatMap(task => {
    try {
      return readFileSync(`/proc/${process.pid}/task/${task}/children`, "utf8")
        .trim().split(/\s+/).filter(Boolean).map(Number);
    } catch (error) {
      if (error.code === "ENOENT") return [];
      throw error;
    }
  }))];
};
const api = {
  on(name, handler) { handlers.set(name, handler); },
  sendMessage(message, options) {
    notices.push({ message, options, children: children(), timers: [...timers.values()].map(t => t.delay) });
  },
};
seatWaits(api);
function makeContext(command) {
  return {
    cwd: command.cwd,
    agent: { kind: command.kind || "main" },
    setTimeout(callback, delay) {
      const id = ++nextTimer;
      const timer = { callback, delay };
      timers.set(id, timer);
      if (delay === 0) timer.native = setTimeout(() => {
        timers.delete(id);
        callback();
      }, 0);
      return id;
    },
    setInterval(callback, delay) {
      const id = ++nextTimer;
      timers.set(id, { callback, delay, interval: true });
      return id;
    },
    clearTimer(id) {
      const timer = timers.get(id);
      if (timer?.native) clearTimeout(timer.native);
      timers.delete(id);
    },
  };
}
for await (const line of createInterface({ input: process.stdin })) {
  try {
    const command = JSON.parse(line);
    if (command.op === "start" || command.op === "switch") {
      context = makeContext(command);
      await handlers.get(command.op === "start" ? "session_start" : "session_switch")({}, context);
    } else if (command.op === "tool") {
      await handlers.get("tool_result")({}, context);
    } else if (command.op === "tick") {
      for (const [id, timer] of [...timers]) {
        if (timer.delay === 0) continue;
        if (!timer.interval) timers.delete(id);
        await timer.callback();
      }
    } else if (command.op === "shutdown" || command.op === "quit") {
      await handlers.get("session_shutdown")({}, context);
    }
    await new Promise(resolve => setTimeout(resolve, 15));
    console.log(JSON.stringify({ notices, children: children(), timers: [...timers.values()].map(t => t.delay) }));
    if (command.op === "quit") break;
  } catch (error) {
    console.log(JSON.stringify({ error: String(error), stack: error.stack }));
  }
}
handlers.get("session_shutdown")({}, context);
