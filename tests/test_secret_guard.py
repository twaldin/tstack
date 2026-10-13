"""The omp secret guard, loaded by bun the way omp loads it: tool calls it blocks and results it redacts.
All values here are fake."""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from support import BUN, CODE

HARNESS = """
import guard from %s;
const handlers = {};
guard({ on: (name, fn) => { handlers[name] = fn; } });
const input = JSON.parse(await Bun.stdin.text());
const calls = [];
for (const call of input.calls) calls.push((await handlers.tool_call(call)) ?? null);
const results = [];
for (const text of input.results) {
  const out = await handlers.tool_result({ content: [{ type: "text", text }] });
  results.push(out ? out.content[0].text : text);
}
console.log(JSON.stringify({ calls, results }));
"""
FAKE = "fake-value-0123456789"


@unittest.skipUnless(BUN, "bun loads the omp extension")
class SecretGuard(unittest.TestCase):
    def guard(self, calls=(), results=()) -> dict:
        folder = Path(tempfile.mkdtemp(prefix="tstack-guard-"))
        self.addCleanup(shutil.rmtree, folder, True)
        harness = folder / "harness.ts"
        harness.write_text(HARNESS % json.dumps(str(CODE / "extensions" / "secret-guard.ts")))
        done = subprocess.run([BUN, str(harness)], input=json.dumps({"calls": list(calls), "results": list(results)}),
                              capture_output=True, text=True, timeout=60, check=True)
        return json.loads(done.stdout)

    def test_credential_stores_are_blocked(self):
        blocked = [
            {"toolName": "read", "input": {"path": "/srv/u/.config/app/auth.json"}},
            {"toolName": "read", "input": {"path": "~/.omp/agent/relay.json"}},
            {"toolName": "read", "input": {"path": "~/.secrets/service-token"}},
            {"toolName": "read", "input": {"path": "~/Library/Keychains/login.keychain-db"}},
            {"toolName": "grep", "input": {"pattern": "x", "path": "/srv/u/.secrets"}},
            {"toolName": "bash", "input": {"command": "cat ~/.config/app/auth.json"}},
            {"toolName": "bash", "input": {"command": "jq . relay.json"}},
            {"toolName": "bash", "input": {"command": "cat ~/.secrets/service-token"}},
            {"toolName": "bash", "input": {"command": "security dump-keychain"}},
            {"toolName": "eval", "input": {"code": "open('/srv/u/.secrets/token').read()"}},
            {"toolName": "read", "input": {"path": "/fictional/auth.json?q=.apiKey&raw=true"}},
            {"toolName": "read", "input": {"path": "/fictional/relay.json?q=.token&raw=true"}},
            {"toolName": "read", "input": {"path": "/fictional/auth.json:raw"}},
            {"toolName": "read", "input": {"path": "/fictional/auth.json:2-4:raw"}},
            {"toolName": "read", "input": {"path": "/fictional/relay.json:raw:-5"}},
            {"toolName": "read", "input": {"path": "/fictional/app/.env:1+3"}},
            {"toolName": "read", "input": {"path": "~/.secrets/service-token:1-2,4-5"}},
            {"toolName": "read", "input": {"path": "/fictional/backup.zip:home/auth.json"}},
            {"toolName": "read", "input": {"path": "/fictional/backup.zip:.secrets/service-token:raw"}},
            {"toolName": "read", "input": {"path": "/fictional/home.tar.gz:u/.secrets/service-token"}},
            {"toolName": "read", "input": {"path": "/fictional/backup.zip:Library/Keychains/login.keychain-db"}},
            {"toolName": "read", "input": {"path": "/fictional/backup.zip:.config/gcloud/credentials.db"}},
            {"toolName": "eval", "input": {"code": "print(read('/fictional/auth.json?q=.apiKey&raw=true'))"}},
            {"toolName": "eval", "input": {"code": "print(read('/fictional/app/.env:raw'))"}},
        ]
        allowed = [
            {"toolName": "read", "input": {"path": "src/auth.ts"}},
            {"toolName": "bash", "input": {"command": "git log --oneline"}},
            {"toolName": "read", "input": {"path": "package.json?q=.name&raw=true"}},
            {"toolName": "read", "input": {"path": "src/auth.ts:10-20:raw"}},
            {"toolName": "read", "input": {"path": "/fictional/backup.zip:docs/secrets.md:raw"}},
            {"toolName": "bash", "input": {"command": "secret-check info file:~/.secrets/service-token"}},
        ]
        out = self.guard(calls=blocked + allowed)["calls"]
        for call, verdict in zip(blocked, out):
            with self.subTest(call=call):
                self.assertTrue(verdict and verdict["block"], verdict)
        self.assertEqual(out[len(blocked):], [None] * len(allowed))

    def test_results_are_redacted(self):
        results = [
            '{"apiKey":"%s"}' % FAKE,
            '  "client_secret": "%s",' % FAKE,
            "1:OPENAI_API_KEY=%s" % FAKE,
            "*12:SERVICE_TOKEN=%s" % FAKE,
            "config/app.env:3:export DB_PASSWORD='%s'" % FAKE,
            "OPENAI_API_KEY=%s" % FAKE,
        ]
        kept = ['{"name": "web", "author": "Someone"}', "12:const tokenCount = 3;"]
        out = self.guard(results=results + kept)["results"]
        for text in out[:len(results)]:
            self.assertNotIn(FAKE, text)
            self.assertIn("[REDACTED by secret-guard]", text)
        self.assertEqual(out[len(results):], kept)
