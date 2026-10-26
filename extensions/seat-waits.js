import { spawn } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, readdirSync, realpathSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

const idPattern = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;
const prPattern = /^(?:https:\/\/github\.com\/)?([\w.-]+\/[\w.-]+)(?:\/pull\/|#)(\d+)\/?$/;

function json(path, fallback) {
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    if (error.code === "ENOENT") return fallback;
    throw error;
  }
}

function seatAt(ctx) {
  if (ctx.agent.kind !== "main" || process.env.TSTACK_LANE) return;
  const root = process.env.TSTACK_INSTANCE || join(homedir(), "agents");
  if (!existsSync(root)) return;
  const instance = realpathSync(root);
  const cwd = realpathSync(ctx.cwd);
  const assistant = cwd === join(instance, "assistant");
  const project = cwd.startsWith(join(instance, "projects") + "/")
    && idPattern.test(cwd.slice(join(instance, "projects").length + 1));
  if ((assistant || project) && existsSync(join(cwd, "state"))) return { cwd, instance, assistant };
}

function candidates(seat) {
  const specs = new Map();
  const add = (kind, id, command, version = "", cwd = seat.cwd) => {
    specs.set(`${kind}:${id}`, { kind, id, command, version, cwd });
  };
  const pr = (ref) => {
    const match = typeof ref === "string" && ref.match(prPattern);
    if (match) add("pr", `${match[1]}#${match[2]}`, ["watch-pr", `${match[1]}#${match[2]}`, "--until", "change", "--json"]);
  };
  const lanes = join(seat.cwd, "state", "lanes");
  for (const id of existsSync(lanes) ? readdirSync(lanes) : []) {
    if (!idPattern.test(id)) continue;
    const record = json(join(lanes, id, "lane.json"), null);
    if (!record) continue;
    if (record.status === "running") {
      const run = record.runs?.at(-1);
      add("lane", id, ["lane", "wait", id, "--seat", seat.cwd], JSON.stringify([run?.run, run?.start]));
    }
    if (!record.merge && !record.cleaned) pr(record.pr);
  }
  const tasks = join(seat.cwd, "state", "tasks.tsv");
  if (existsSync(tasks)) {
    const [header, ...rows] = readFileSync(tasks, "utf8").trimEnd().split("\n").map(line => line.split("\t"));
    for (const cells of rows) {
      const task = Object.fromEntries(header.map((key, index) => [key, cells[index]]));
      if (!["done", "cancelled"].includes(task.state)) pr(task.pr);
    }
  }
  add("heartbeat", "heartbeat", ["sleep", "1800"]);
  if (seat.assistant) add("text", "inbox", [join(seat.instance, "bin", "text"), "inbox", "--wait"], "", seat.instance);
  return specs;
}

export default function seatWaits(pi) {
  let active;

  function start(ctx) {
    stop();
    const seat = seatAt(ctx);
    if (!seat) return;
    const state = active = { stopped: false, jobs: new Map(), finished: new Set(), failures: new Map(), children: new Set(), timers: new Set(), queue: Promise.resolve(), ctx };
    const output = join(seat.cwd, "state", "run", "seat-waits");
    mkdirSync(output, { recursive: true });

    const notify = (content) => pi.sendMessage({ customType: "seat-waits", content, display: true }, { deliverAs: "aside", triggerTurn: true });
    const reportFailure = (key, content) => {
      const count = Math.min((state.failures.get(key) || 0) + 1, 8);
      state.failures.set(key, count);
      if (count === 1) notify(content);
    };
    const save = (name, result) => {
      const path = join(output, `${name}.json`);
      writeFileSync(path, JSON.stringify(result, null, 2) + "\n");
      return path;
    };
    const enqueue = (work) => {
      state.queue = state.queue.then(() => state.stopped ? undefined : work()).catch(error => {
        if (state.stopped) return;
        const path = save("refresh", { error: String(error) });
        reportFailure("refresh", `Seat waits: refresh failed. Read ${path}.`);
      });
      return state.queue;
    };
    const later = (work, delay) => {
      const timer = ctx.setTimeout(() => {
        state.timers.delete(timer);
        return enqueue(work);
      }, delay);
      state.timers.add(timer);
    };
    const run = (command, cwd, complete) => {
      const child = spawn(command[0], command.slice(1), {
        cwd, env: { ...process.env, TSTACK_SEAT: seat.cwd }, detached: true, stdio: ["ignore", "pipe", "pipe"],
      });
      state.children.add(child);
      let stdout = "", stderr = "", failure;
      child.stdout.on("data", data => { stdout += data; });
      child.stderr.on("data", data => { stderr += data; });
      child.on("error", error => { failure = String(error); });
      child.on("close", (code, signal) => {
        state.children.delete(child);
        if (!state.stopped) later(() => complete({ code, signal, stdout, stderr, error: failure }), 0);
      });
    };
    const token = spec => `${spec.kind}:${spec.id}:${spec.version}`;
    const arm = (job) => {
      const command = job.kind === "pr" && !job.initialized
        ? ["watch-pr", job.id, "--once", "--json"] : job.command;
      run(command, job.cwd, result => complete(job, result));
    };
    const complete = async (job, result) => {
      const key = `${job.kind}:${job.id}`;
      const name = `${job.kind}-${encodeURIComponent(job.id)}`;
      let finished = false;
      let failed = result.code !== 0;
      let initial = false;
      try {
        if (job.kind === "ask" && result.code === 2) {
          finished = true;
          failed = false;
        } else if (!failed && job.kind === "pr") {
          const report = JSON.parse(result.stdout);
          finished = ["CLOSED", "MERGED"].includes(report.state);
          initial = !job.initialized;
          job.initialized = true;
        } else if (!failed && job.kind === "lane") {
          const record = json(join(seat.cwd, "state", "lanes", job.id, "lane.json"), null);
          const run = record?.runs?.at(-1);
          finished = record && (record.status !== "running" || JSON.stringify([run?.run, run?.start]) !== job.version);
        } else if (!failed && job.kind === "ask") {
          finished = true;
        }
      } catch (error) {
        failed = true;
        result.error = String(error);
      }
      const path = save(name, result);
      if (!failed) state.failures.delete(`wait:${key}`);
      if (finished) {
        state.finished.add(token(job));
        state.jobs.delete(key);
      } else if (failed) {
        const failures = state.failures.get(`wait:${key}`) || 0;
        later(() => arm(job), Math.min(5000 * 2 ** failures, 600000));
      } else {
        arm(job);
      }
      try {
        await reconcile();
      } finally {
        if (failed) reportFailure(`wait:${key}`, `Seat waits: wait failed. ${job.kind} ${job.id}. Read ${path}.`);
        else if (!initial) {
          const notice = `Seat waits: ${job.kind} ready. ${job.id}. Read ${path}.`;
          notify(job.kind === "lane" || job.kind === "text" ? `${notice}\n\n${result.stdout}` : notice);
        }
      }
    };
    const reconcile = async () => {
      for (const [key, spec] of candidates(seat)) {
        if (!state.jobs.has(key) && !state.finished.has(token(spec))) {
          const job = { ...spec, initialized: false };
          state.jobs.set(key, job);
          arm(job);
        }
      }
      const pointer = json(join(seat.cwd, "state", "board.json"), {});
      state.failures.delete("refresh");
      const tile = pointer.tile || process.env.EASL_TILE_ID;
      if (tile) {
        if (state.askScan) return;
        state.askScan = true;
        const command = ["easl", "ask", "list", "--open"];
        if (pointer.board) command.push("--board", pointer.board);
        run(command, seat.cwd, result => {
          state.askScan = false;
          if (result.code !== 0) {
            const path = save("asks", result);
            reportFailure("asks", `Seat waits: ask discovery failed. Read ${path}.`);
            return;
          }
          const asks = JSON.parse(result.stdout).objects;
          state.failures.delete("asks");
          for (const ask of asks) {
            if (ask.props?.asker?.tile !== tile || !idPattern.test(ask.id)) continue;
            const spec = { kind: "ask", id: ask.id, version: "", command: ["easl", "ask", "wait", ask.id], cwd: seat.cwd };
            const key = `ask:${ask.id}`;
            if (!state.jobs.has(key) && !state.finished.has(token(spec))) {
              state.jobs.set(key, spec);
              arm(spec);
            }
          }
        });
      }
    };
    state.reconcile = () => enqueue(reconcile);
    state.interval = ctx.setInterval(state.reconcile, 5000);
    return state.reconcile();
  }

  function stop() {
    if (!active) return;
    active.stopped = true;
    active.ctx.clearTimer(active.interval);
    for (const timer of active.timers) active.ctx.clearTimer(timer);
    for (const child of active.children) {
      try { process.kill(-child.pid, "SIGTERM"); }
      catch (error) { if (error.code !== "ESRCH") throw error; }
    }
    active = undefined;
  }

  pi.on("session_start", (_event, ctx) => start(ctx));
  pi.on("session_switch", (_event, ctx) => start(ctx));
  pi.on("tool_result", (_event, ctx) => {
    if (ctx.agent.kind === "main") return active?.reconcile();
  });
  pi.on("session_shutdown", stop);
}
