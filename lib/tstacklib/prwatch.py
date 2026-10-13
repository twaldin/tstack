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
      nodes{id,isResolved,comments(last:1){nodes{updatedAt,body}}}
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


def marker(item: dict) -> str | None:
    """The lane id in a `<!-- tstack-lane:<id> -->` marker, which lanes put in every PR body they write."""
    match = LANE_MARKER.search(item.get("body") or "")
    return match.group(1) if match else None


def fingerprint(pr: dict, threads) -> dict:
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
             "u": item.get("submittedAt", "?"), "b": body_digest(item), "m": marker(item)}
            for item in pr.get("reviews", [])
        ], key=lambda item: item["i"]),
        "comments": sorted([
            {"i": item.get("id", "?"), "u": item.get("updatedAt") or item.get("createdAt", "?"),
             "b": body_digest(item), "m": marker(item)}
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


def threads(url: str) -> list[dict]:
    owner, repo, number = parse_url(url)
    pages = gh_json(["api", "graphql", "--paginate", "--slurp", "-f", f"query={THREADS_QUERY}",
                     "-f", f"o={owner}", "-f", f"r={repo}", "-F", f"n={number}"])
    rows = []
    for page in pages:
        for item in page["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]:
            last = next(iter(item["comments"]["nodes"]), {})
            rows.append({"i": item["id"], "r": item["isResolved"], "u": last.get("updatedAt", ""), "m": marker(last)})
    return sorted(rows, key=lambda item: item["i"])


def snapshot(url: str) -> dict:
    return fingerprint(view(url), threads(url))


def actionable(old: dict | None, new: dict, lane_id: str | None = None) -> list[str]:
    """What a waiting lane must hear about, comparing the last handled snapshot with this one.
    Comments, reviews and thread replies carrying this lane's marker are its own and never wake it."""
    if new["state"] == "MERGED":
        return ["merged"]
    if new["state"] == "CLOSED":
        return ["closed"]
    events = []
    finished = new["ci"] in ("green", "red")
    if finished and (old is None or (old["head"], old["ci"]) != (new["head"], new["ci"])):
        events.append(f"checks-{new['ci']}" + (f" ({', '.join(new['failing'])})" if new["failing"] else ""))
    conflict = new["mergeable"] == "CONFLICTING" or new["mergeState"] == "DIRTY"
    if conflict and (old is None or not (old["mergeable"] == "CONFLICTING" or old["mergeState"] == "DIRTY")):
        events.append("conflict")
    if old is None:
        return events

    def fresh(key: str, version: str) -> bool:
        seen = {item["i"]: item.get(version) for item in old.get(key) or []}
        return any(item["i"] not in seen or seen[item["i"]] != item.get(version)
                   for item in new.get(key) or [] if item.get("m") != lane_id and not item.get("r"))
    if fresh("reviewEvents", "b"):
        events.append("review")
    if fresh("comments", "b"):
        events.append("comment")
    if fresh("threads", "u"):
        events.append("thread")
    return events
