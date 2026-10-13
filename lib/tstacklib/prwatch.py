"""GitHub PR snapshots through gh: CI classification, fingerprints and their diffs (shared by watch-pr and tstack wait)."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess

GREEN = {"SUCCESS", "NEUTRAL", "SKIPPED"}
PENDING = {"PENDING", "EXPECTED", "QUEUED", "IN_PROGRESS", "STALE", "WAITING", "REQUESTED", ""}
EVENT_FIELDS = ("headRefOid,headRefName,state,isDraft,mergeable,mergeStateStatus,latestReviews,"
                "reviewRequests,reviews,comments,statusCheckRollup")
THREADS_QUERY = """query($o:String!,$r:String!,$n:Int!,$endCursor:String){
  repository(owner:$o,name:$r){pullRequest(number:$n){
    reviewThreads(first:100,after:$endCursor){
      nodes{id,isResolved,comments(last:1){nodes{updatedAt,body,author{login}}}}
      pageInfo{hasNextPage,endCursor}
    }
  }}
}"""
PR_URL = re.compile(r"https://github\.com/([^/]+)/([^/]+)/pull/([1-9][0-9]*)")
LANE_MARKER = re.compile(r"<!-- tstack-lane:([a-z0-9][a-z0-9-]*) -->")


def parse_url(url: str) -> tuple[str, str, str]:
    match = PR_URL.fullmatch(url or "")
    if not match:
        raise ValueError("need a full https://github.com/<owner>/<repo>/pull/<n> URL")
    return match.groups()


def check_state(item: dict) -> str:
    return item.get("conclusion") or item.get("state") or "PENDING"


def check_name(item: dict) -> str:
    return item.get("name") or item.get("context") or "?"


def classify(pr: dict) -> str:
    """green, pending or red. An empty rollup is green: the repo has no CI."""
    rollup = pr["statusCheckRollup"]
    if not isinstance(rollup, list):
        raise ValueError("statusCheckRollup must be an array")
    states = [check_state(item) for item in rollup]
    if any(state not in GREEN | PENDING for state in states):
        return "red"
    return "pending" if any(state in PENDING for state in states) else "green"


def failing_checks(pr: dict) -> list[str]:
    return sorted({check_name(item) for item in pr.get("statusCheckRollup") or []
                   if check_state(item) not in GREEN | PENDING})


def body_digest(item: dict) -> str:
    return hashlib.sha256((item.get("body") or "").encode()).hexdigest()


def marker(item: dict, me: str | None) -> str | None:
    """The lane id of a `<!-- tstack-lane:<id> -->` marker that is the body's last non-empty line, outside a
    quote block, on an item the authenticated gh login (`me`) wrote. Lanes end every PR body they write with
    one; a marker quoted or written by anyone else does not count."""
    if not me or (item.get("author") or {}).get("login") != me:
        return None
    lines = (item.get("body") or "").rstrip().splitlines()
    if not lines:
        return None
    match = LANE_MARKER.fullmatch(lines[-1].strip())
    # a line right after a `>` line continues that quote block
    if not match or len(lines) > 1 and lines[-2].lstrip().startswith(">"):
        return None
    return match.group(1)


def fingerprint(pr: dict, threads, me: str | None = None) -> dict:
    """`me`: the authenticated gh login, for lane markers; without it no item counts as a lane's own."""
    return {
        "head": pr["headRefOid"],
        "state": pr["state"],
        "mergeable": pr["mergeable"],
        "mergeState": pr["mergeStateStatus"],
        "ci": classify(pr),
        "failing": failing_checks(pr),
        "reviews": sorted([
            {"a": (item.get("author") or {}).get("login", "?"), "s": item["state"]}
            for item in pr.get("latestReviews", [])
        ], key=lambda item: item["a"]),
        "reviewCount": len(pr.get("reviews", [])),
        "reviewEvents": sorted([
            {"i": item.get("id", "?"), "s": item["state"],
             "u": item.get("submittedAt", "?"), "b": body_digest(item), "m": marker(item, me)}
            for item in pr.get("reviews", [])
        ], key=lambda item: item["i"]),
        "comments": sorted([
            {"i": item.get("id", "?"), "u": item.get("updatedAt") or item.get("createdAt", "?"),
             "b": body_digest(item), "m": marker(item, me)}
            for item in pr.get("comments", [])
        ], key=lambda item: item["i"]),
        "reviewRequests": sorted(item.get("login") or item.get("name", "?")
                                 for item in pr.get("reviewRequests", [])),
        "threads": threads,
        "unresolved": None if threads is None else sum(not item["r"] for item in threads),
    }


def diff(old: dict, new: dict) -> str:
    return ",".join(sorted(key for key in old.keys() | new.keys() if old.get(key) != new.get(key)))


def initial_keys(fp: dict) -> str:
    return ",".join(key for key, actionable in [
        ("ci", fp["ci"] == "red"),
        ("mergeState", fp["mergeable"] == "CONFLICTING" or fp["mergeState"] == "DIRTY"),
        ("unresolved", (fp["unresolved"] or 0) > 0),
        ("comments", bool(fp["comments"])),
        ("reviews", bool(fp.get("reviewEvents")) or bool(fp["reviews"])),
    ] if actionable)


def gh_json(args: list[str], timeout: float = 30):
    result = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout, check=True)
    return json.loads(result.stdout)


def view(url: str, fields: str = EVENT_FIELDS) -> dict:
    return gh_json(["pr", "view", url, "--json", fields])


def viewer() -> str:
    """The authenticated gh login: the author of every PR body a lane writes."""
    return gh_json(["api", "user"])["login"]


def threads(url: str, me: str | None = None) -> list[dict]:
    owner, repo, number = parse_url(url)
    pages = gh_json(["api", "graphql", "--paginate", "--slurp", "-f", f"query={THREADS_QUERY}",
                     "-f", f"o={owner}", "-f", f"r={repo}", "-F", f"n={number}"])
    rows = []
    for page in pages:
        for item in page["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]:
            last = next(iter(item["comments"]["nodes"]), {})
            rows.append({"i": item["id"], "r": item["isResolved"], "u": last.get("updatedAt", ""), "m": marker(last, me)})
    return sorted(rows, key=lambda item: item["i"])


def check_runs(pr: dict) -> str | None:
    """Identity of the check runs behind a finished CI verdict (the failing runs when red, every run when green),
    each by name, result, completion time and run URL; None while CI is pending. A rerun of the same head that
    fails the same way has a new completion time and run URL, so a new identity."""
    verdict = classify(pr)
    if verdict == "pending":
        return None
    runs = sorted([check_name(item), check_state(item), item.get("completedAt") or "",
                   item.get("detailsUrl") or item.get("targetUrl") or ""]
                  for item in pr.get("statusCheckRollup") or []
                  if verdict == "green" or check_state(item) not in GREEN | PENDING)
    return hashlib.sha256(json.dumps(runs).encode()).hexdigest()[:16]


def wait_snapshot(pr: dict, threads, me: str) -> dict:
    """The fingerprint tstack wait keeps per lane: watch-pr's fingerprint plus `runs`, the check-run identity."""
    return {**fingerprint(pr, threads, me), "runs": check_runs(pr)}


def actionable(old: dict | None, new: dict, lane_id: str | None = None) -> list[tuple[str, list[str]]]:
    """What a waiting lane must hear about, comparing the last handled wait snapshot with this one. Each event
    comes with the ids of the GitHub occurrences behind it: the check runs behind the verdict, the comment,
    review or thread reply version, the merge or close. Comments, reviews and thread replies carrying this
    lane's marker are its own and never wake it."""
    if new["state"] in ("MERGED", "CLOSED"):
        return [(new["state"].lower(), [f"{new['state'].lower()}:{new['head']}"])]
    events = []
    finished = new["ci"] in ("green", "red")
    rerun = old is not None and old.get("runs") is not None and old["runs"] != new["runs"]
    if finished and (old is None or (old["head"], old["ci"]) != (new["head"], new["ci"]) or rerun):
        events.append((f"checks-{new['ci']}" + (f" ({', '.join(new['failing'])})" if new["failing"] else ""),
                       [f"checks:{new['head']}:{new['runs']}"]))
    conflict = new["mergeable"] == "CONFLICTING" or new["mergeState"] == "DIRTY"
    if conflict and (old is None or not (old["mergeable"] == "CONFLICTING" or old["mergeState"] == "DIRTY")):
        events.append(("conflict", [f"conflict:{new['head']}"]))
    if old is None:
        return events

    def fresh(key: str, version: str) -> list[str]:
        seen = {item["i"]: item.get(version) for item in old.get(key) or []}
        return [f"{key}:{item['i']}:{item.get(version)}" for item in new.get(key) or []
                if item.get("m") != lane_id and not item.get("r")
                and (item["i"] not in seen or seen[item["i"]] != item.get(version))]
    for key, version, label in (("reviewEvents", "b", "review"), ("comments", "b", "comment"), ("threads", "u", "thread")):
        ids = fresh(key, version)
        if ids:
            events.append((label, ids))
    return events
