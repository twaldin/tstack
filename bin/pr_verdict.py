import json
import re

SHA = re.compile(r"[0-9a-f]{40}")
MODEL = re.compile(r"([a-z0-9-]+)/([a-z0-9][a-z0-9.-]*)")
VERDICT = re.compile(r"[A-Z][A-Z_]*")
FIELDS = {"head", "reviewer_model", "author_model", "verdict"}


def model_family(model):
    if not isinstance(model, str):
        return None
    match = MODEL.fullmatch(model)
    if not match:
        return None
    provider, name = match.groups()
    if provider in {"openai", "openai-codex"} and name.startswith("gpt-"):
        return "openai"
    if provider == "anthropic" and name.startswith("claude-"):
        return "anthropic"
    if provider == "google" and name.startswith("gemini-"):
        return "google"
    if provider == "xai" and name.startswith("grok-"):
        return "xai"
    return None


def metadata_lines(body, prefix):
    if isinstance(body, str):
        for line in body.splitlines():
            if line.startswith(prefix):
                yield line[len(prefix):].strip()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate metadata field")
        result[key] = value
    return result


def verdict_fact(pr, lane_model=None):
    """Return a JSON-safe fact; lane_model is a resolved model, never a model alias.

    PR input uses gh pr view's body, headRefOid, comments and reviews fields.
    A later exact-head record replaces an earlier one, even if it is not READY.
    Dismissed reviews remain evidence of a withdrawal, not an approval.
    """
    models = list(metadata_lines(pr.get("body"), "tstack-author-model: "))
    author = lane_model if model_family(lane_model) else (models[0] if len(models) == 1 else None)
    if not model_family(author):
        author = None
    head = pr.get("headRefOid")
    fact = {"ready": False, "head": None, "author_model": author,
            "reviewer_model": None, "verdict": None, "source": None, "at": None}
    latest = None
    for source, timestamp in (("comments", "createdAt"), ("reviews", "submittedAt")):
        for item in pr.get(source) or []:
            if source == "reviews" and item.get("state") == "PENDING":
                continue
            for position, text in enumerate(metadata_lines(item.get("body"), "tstack-verdict: ")):
                try:
                    record = json.loads(text, object_pairs_hook=unique_object)
                except (ValueError, TypeError):
                    continue
                if (not isinstance(record, dict) or set(record) != FIELDS
                        or not all(isinstance(value, str) for value in record.values())
                        or not SHA.fullmatch(record["head"]) or record["head"] != head
                        or not VERDICT.fullmatch(record["verdict"])):
                    continue
                at = item.get(timestamp)
                if not isinstance(at, str) or not at:
                    continue
                order = (at, position)
                if latest is not None and order < latest:
                    continue
                latest = order
                reviewer = record["reviewer_model"]
                family = model_family(reviewer)
                fact.update(head=head, reviewer_model=reviewer, verdict=record["verdict"], source=source, at=at)
                fact["ready"] = bool(author and record["author_model"] == author and family
                                     and family != model_family(author) and record["verdict"] == "READY"
                                     and item.get("state") not in {"DISMISSED", "CHANGES_REQUESTED"})
    return fact


def render_verdict(fact):
    if fact["ready"]:
        return (f"other-family verdict READY at {fact['head']}: "
                f"{fact['reviewer_model']} reviewed {fact['author_model']}")
    return "other-family verdict: no READY current-head verdict"
