#!/usr/bin/env node
import { realpathSync, writeSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

function usage(message) {
  if (message) process.stderr.write(`${message}\n`);
  process.stderr.write(
    "usage: pi_resume_session.mjs <probe|resume> --package-root <dir> --session-file <jsonl> --worktree <dir>\n",
  );
  process.exit(2);
}

function options(argv) {
  const result = {};
  for (let index = 0; index < argv.length; index += 2) {
    const key = argv[index];
    const value = argv[index + 1];
    if (!key?.startsWith("--") || value === undefined) usage("invalid arguments");
    result[key.slice(2)] = value;
  }
  return result;
}

async function stdinJson() {
  let text = "";
  process.stdin.setEncoding("utf8");
  for await (const chunk of process.stdin) text += chunk;
  return JSON.parse(text);
}

function ack(payload) {
  const raw = process.env.NVSOP_REPAIR_ACK_FD;
  if (!raw) return;
  const descriptor = Number(raw);
  if (!Number.isInteger(descriptor) || descriptor < 0) {
    throw new Error("NVSOP_REPAIR_ACK_FD is invalid");
  }
  writeSync(descriptor, `${JSON.stringify(payload)}\n`);
}

function repairPrompt(handoff) {
  const encoded = JSON.stringify(handoff, null, 2);
  return `A landing attempt for your bound nvsop task was blocked and ownership has returned to DEVELOPMENT.\n\nPrivate repair handoff:\n${encoded}\n\nFollow the repository workflow. Work only in the handoff worktree. Before any tracked source edit, re-read AGENTS.md and run:\nmake pr-land-repair PR=${handoff.pr} ACTION=preflight WORKTREE=${handoff.worktree} EXPECTED_GENERATION=${handoff.event}\n\nDiagnose the concrete blocked evidence and make the smallest required repair. Re-run affected checks and the required final gate; obtain any required independent read-only review. Do not broaden scope, do not bypass hooks, and do not merge, close Issues, clean worktrees, or re-enqueue a changed candidate without the repository-required lifecycle authorization. The plaintext repair claim is private; never post or log it publicly. If a later authorized re-enqueue needs it, use the claim from this handoff only for that matching repair generation.`;
}

const command = process.argv[2];
if (!new Set(["probe", "resume"]).has(command)) usage();
const args = options(process.argv.slice(3));
for (const key of ["package-root", "session-file", "worktree"]) {
  if (!args[key]) usage(`missing --${key}`);
}

const packageRoot = realpathSync(args["package-root"]);
const sessionFile = realpathSync(args["session-file"]);
const worktree = realpathSync(args.worktree);
const pi = await import(pathToFileURL(join(packageRoot, "dist", "index.js")).href);
const sessionManager = pi.SessionManager.open(sessionFile, undefined, worktree);
const sessionId = sessionManager.getSessionId();
if (!sessionId) throw new Error("Pi session has no session id");

if (command === "probe") {
  process.stdout.write(`${JSON.stringify({ session_id: sessionId, cwd: sessionManager.getCwd() })}\n`);
  process.exit(0);
}

const handoff = await stdinJson();
if (handoff.session_id !== sessionId) throw new Error("Pi session id does not match repair handoff");
if (realpathSync(handoff.worktree) !== worktree) {
  throw new Error("Pi repair handoff worktree does not match cwd override");
}
const agentDir = process.env.PI_CODING_AGENT_DIR || join(homedir(), ".pi", "agent");
const createRuntime = async ({ cwd, agentDir: targetAgentDir, sessionManager: targetSession, sessionStartEvent }) => {
  const services = await pi.createAgentSessionServices({
    cwd,
    agentDir: targetAgentDir,
    modelRuntimeSignal: AbortSignal.timeout(15000),
  });
  const errors = services.diagnostics.filter((item) => item.type === "error");
  if (errors.length) throw new Error(errors.map((item) => item.message).join("; "));
  const created = await pi.createAgentSessionFromServices({
    services,
    sessionManager: targetSession,
    sessionStartEvent,
  });
  if (!created.session.model) throw new Error("Pi session has no configured model");
  return { ...created, services, diagnostics: services.diagnostics };
};
const runtime = await pi.createAgentSessionRuntime(createRuntime, {
  cwd: worktree,
  agentDir,
  sessionManager,
  sessionStartEvent: { type: "session_start", reason: "resume" },
});
let acknowledged = false;
const unsubscribeAck = runtime.session.subscribe((event) => {
  if (!acknowledged && event.type === "agent_start") {
    ack({ state: "accepted", session_id: sessionId, cwd: runtime.cwd });
    acknowledged = true;
  }
});
try {
  const exitCode = await pi.runPrintMode(runtime, {
    mode: "text",
    initialMessage: repairPrompt(handoff),
  });
  process.exitCode = exitCode;
} finally {
  unsubscribeAck();
}
