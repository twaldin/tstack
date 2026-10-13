// Keeps secret values out of agent transcripts on this machine.
// - Blocks tool calls that would read a secret file or dump a secret store: `.env*` files (not
//   .env.example/.sample/.template/.dist), `auth.json`, `relay.json`, anything under `.secrets/`,
//   keychain files, private keys, gcloud credentials, `gcloud secrets versions access`,
//   `kubectl get secret -o yaml/json`, bare `printenv`/`env`, keychain `-w` and dumps.
// - Redacts secret-shaped `NAME=value` lines (also numbered, as read and grep print them) and JSON
//   credential fields from every tool result, as a backstop.
// Agents check secrets with `secret-check` (existence, length, hash prefix, compare, HTTP probe),
// which never prints a value. This is a guard against accidents, not a sandbox: an agent that
// sets out to evade it (encoding, renamed copies, custom programs) can.
// @ts-nocheck

const CHECKER = "secret-check";
const HINT =
  "Secret values must not enter the transcript. Use `secret-check` instead: " +
  "`secret-check info env:<file>:<KEY>` / `gsm:<project>/<secret>[@version]` / `envvar:<NAME>` " +
  "(exists, length, sha256 prefix), `secret-check compare <spec> <spec>`, " +
  "`secret-check probe <spec> --url <url> --header 'Authorization: Bearer {}'` (HTTP status only).";

const SAFE_ENV_SUFFIX = /^\.env\.(example|sample|template|dist)$/i;

// The file a tool path names, without read-tool selectors: `?q=…&raw=true` queries, `#…`, and trailing
// `:raw`, `:img`, `:conflicts`, `:N-M`, `:-N`, `:N+M`, `:A-B,C-D` in any order. An archive member
// (`x.zip:dir/.secrets/token`) reads as a path inside the archive: its `:` is a directory separator.
function underlyingPath(p: string): string {
  let clean = p.replace(/[?#].*$/s, "");
  for (let prev = ""; prev !== clean; ) {
    prev = clean;
    clean = clean.replace(/:(?:raw|img|conflicts|-?\d[\d,+\-]*)$/i, "");
  }
  return clean.replace(/:/g, "/").replace(/\/+$/, "");
}

function isSecretPath(p: string): boolean {
  const clean = underlyingPath(p);
  const base = clean.split("/").pop() ?? "";
  if (/^\.env(\..+)?$/i.test(base) && !SAFE_ENV_SUFFIX.test(base)) return true;
  if (/\.(pem|p12|pfx|key)$/i.test(base) && !/\.pub$/i.test(base)) return true;
  if (/^id_(rsa|ecdsa|ed25519|dsa)$/.test(base)) return true;
  if (/^(auth|relay)\.json$/i.test(base)) return true;
  if (/(^|\/)\.secrets(\/|$)/.test(clean)) return true;
  if (/\.keychain(-db)?$/i.test(base) || /(^|\/)Library\/Keychains(\/|$)/.test(clean)) return true;
  if (/(^|\/)\.config\/gcloud\/.*(credentials|access_tokens|legacy_credentials)/i.test(clean)) return true;
  if (/(^|\/)\.config\/gcloud\/(credentials\.db|access_tokens\.db|application_default_credentials\.json)$/i.test(clean)) return true;
  return false;
}

// A `.env` path token inside free text (shell or code), e.g. `cat ~/app/.env`, `open('.env')`,
// `read(".env:raw")`. A token may end at a read-tool selector (`?`, `#`, `:`).
const ENV_TOKEN = /(?:^|[\s'"`=/<(:])\.env(?:\.(?!example\b|sample\b|template\b|dist\b)[\w.-]+)?(?=$|[\s'"`;|&)>,?#:])/i;
// Credential stores named in free text: auth.json, relay.json, ~/.secrets, keychain files.
const STORE_TOKEN =
  /(?:^|[\s'"`=/<(:])(?:auth|relay)\.json(?=$|[\s'"`;|&)>,?#:])|(?:^|[\s'"`=/<(:~])\.secrets(?=$|[\s/'"`;|&)>,?#:])|\.keychain(?:-db)?\b|\bLibrary\/Keychains\b/i;
const SHELL_DENY: Array<[RegExp, string]> = [
  [/\bgcloud\s+secrets\s+versions\s+access\b/i, "gcloud secrets versions access"],
  [/\bkubectl\s+get\s+secrets?\b[^|;&]*\s-o\s*=?\s*(yaml|json|jsonpath|go-template)/i, "kubectl get secret -o"],
  [/(^|[;&|(]\s*|\bthen\s+|\bdo\s+)printenv\s*($|[;&|)])/, "bare printenv"],
  [/(^|[;&|(]\s*)env\s*($|[;&|)])/, "bare env dump"],
  [/\bprintenv\s+[A-Za-z0-9_]*(KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|PRIVATE|OAUTH)/i, "printenv of a secret variable"],
  [/\b(?:echo|printf)\b[^|;&]*\$\{?[A-Z0-9_]*(KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|PRIVATE|OAUTH)[A-Z0-9_]*/, "echo of a secret variable"],
  [/\bsecurity\s+find-(generic|internet)-password\b[^|;&]*\s-w\b/, "keychain password -w"],
  [/\bsecurity\s+dump-keychain\b/, "keychain dump"],
  [/\/\.config\/gcloud\/[^\s'"]*(credentials|access_tokens)/i, "gcloud credential files"],
  [/\b(?:cat|less|more|head|tail|bat|xxd|od|strings|base64)\b[^|;&]*\.(?:pem|p12|pfx)\b/i, "private key file"],
  [/\b(?:cat|less|more|head|tail|bat|xxd|od|strings|base64)\b[^|;&]*\bid_(?:rsa|ecdsa|ed25519|dsa)\b(?!\.pub)/, "private key file"],
];

// The checker alone, with no chaining, redirection or subshell.
const CHECKER_ONLY = new RegExp(`^\\s*(?:\\S*/)?${CHECKER}(?:\\s[^;&|\`$()<>]*)?$`);

function shellReason(cmd: string): string | undefined {
  if (CHECKER_ONLY.test(cmd)) return undefined;
  if (ENV_TOKEN.test(cmd)) return "reads a .env file";
  if (STORE_TOKEN.test(cmd)) return "reads a credential store";
  for (const [re, what] of SHELL_DENY) if (re.test(cmd)) return what;
  return undefined;
}

function pathsOf(input: Record<string, unknown>): string[] {
  const out: string[] = [];
  for (const k of ["path", "file", "file_path", "paths"]) {
    const v = input?.[k];
    if (typeof v === "string") out.push(...v.split(";").map((s) => s.trim()).filter(Boolean));
    else if (Array.isArray(v)) out.push(...v.filter((s) => typeof s === "string"));
  }
  return out;
}

// `NAME=value` / `export NAME=value` / `NAME: value` lines whose name looks like a secret, also behind a
// line-number prefix (`12:`, `*12:`, `path:12:`, `12#AB:`) as read and grep print them.
const SECRET_LINE =
  /^(\s*(?:(?:[^\s:=]+:)?\s*\*?\d+(?:#[0-9A-Za-z]{2,8})?[:|-]\s?)?(?:export\s+)?[A-Z][A-Z0-9_]*(?:KEY|SECRET|TOKEN|PASSWORD|PASSWD|PASS|AUTH|CREDENTIALS?|PRIVATE|OAUTH|DSN|WEBHOOK)[A-Z0-9_]*\s*[=:]\s*)(["']?)([^\s"']{8,})\2\s*$/gm;
// `"apiKey": "value"` and the like in JSON.
const SECRET_FIELD =
  /("[A-Za-z0-9_.-]*?(?:key|secret|token|password|passwd|credential|auth(?!or)|private|dsn|webhook)[A-Za-z0-9_.-]*"\s*:\s*")([^"\\\s]{8,})(")/gi;

function redact(text: string): string {
  return text
    .replace(SECRET_LINE, (_m, head, q) => `${head}${q}[REDACTED by secret-guard]${q}`)
    .replace(SECRET_FIELD, (_m, head, _v, tail) => `${head}[REDACTED by secret-guard]${tail}`);
}

export default function secretGuard(pi) {
  pi.on("tool_call", async (event) => {
    const name = String(event.toolName ?? "");
    const input = event.input ?? {};
    if (name === "bash") {
      const why = shellReason(String(input.command ?? ""));
      if (why) return { block: true, reason: `secret-guard blocked this command (${why}). ${HINT}` };
      return;
    }
    if (name === "eval") {
      const code = String(input.code ?? "");
      if (ENV_TOKEN.test(code) || STORE_TOKEN.test(code) || /\bgcloud\s+secrets\s+versions\s+access\b/i.test(code))
        return { block: true, reason: `secret-guard blocked this cell (reads a .env file or secret store). ${HINT}` };
      return;
    }
    const hit = pathsOf(input).find(isSecretPath);
    if (hit) return { block: true, reason: `secret-guard blocked ${name} on ${hit} (secret file). ${HINT}` };
    if ((name === "grep" || name === "find") && input.gitignore === false)
      return {
        block: true,
        reason: "secret-guard blocked a search with gitignore:false (it can reach .env files). Search without it, or name the files.",
      };
  });

  pi.on("tool_result", async (event) => {
    if (!Array.isArray(event.content)) return;
    let changed = false;
    const content = event.content.map((chunk) => {
      if (chunk?.type !== "text" || typeof chunk.text !== "string") return chunk;
      const text = redact(chunk.text);
      if (text === chunk.text) return chunk;
      changed = true;
      return { ...chunk, text };
    });
    if (changed) return { content };
  });
}

export const _test = { isSecretPath, shellReason, redact };
