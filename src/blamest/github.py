"""Everything that comes from GitHub (github.com or Enterprise Server), fetched through the `gh` CLI."""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

_NOREPLY = re.compile(r"^(?:\d+\+)?([^@]+)@users\.noreply\.")


class GhError(RuntimeError):
    pass


@dataclass
class RepoInfo:
    host: str
    owner: str
    name: str
    default_branch: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"


def _gh(args: list[str], cwd: Path | None = None, stdin: str | None = None, retries: int = 3) -> str:
    for attempt in range(retries):
        proc = subprocess.run(
            ["gh", *args], cwd=cwd, input=stdin, capture_output=True, text=True, encoding="utf-8",
        )
        if proc.returncode == 0:
            return proc.stdout
        transient = any(s in proc.stderr.lower() for s in ("502", "503", "504", "timeout", "secondary rate", "eof"))
        if not transient or attempt == retries - 1:
            raise GhError(f"gh {' '.join(args[:2])} failed: {proc.stderr.strip()}")
        time.sleep(2 * (attempt + 1))
    raise AssertionError("unreachable")


def detect_repo(path: Path) -> RepoInfo:
    out = json.loads(_gh(["repo", "view", "--json", "owner,name,url,defaultBranchRef"], cwd=path))
    return RepoInfo(
        host=urlparse(out["url"]).hostname or "github.com",
        owner=out["owner"]["login"],
        name=out["name"],
        default_branch=(out.get("defaultBranchRef") or {}).get("name") or "main",
    )


class GitHub:
    def __init__(self, repo: RepoInfo):
        self.repo = repo

    def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        body = json.dumps({"query": query, "variables": variables or {}})
        out = json.loads(_gh(["api", "graphql", "--hostname", self.repo.host, "--input", "-"], stdin=body))
        if out.get("errors"):
            raise GhError("GraphQL error: " + "; ".join(e.get("message", "?") for e in out["errors"]))
        return out["data"]

    def rest_all(self, path: str) -> list[dict[str, Any]]:
        out = _gh(["api", "--hostname", self.repo.host, "--paginate", "--slurp", path])
        return [item for page in json.loads(out) for item in page]

    # ---- pull requests -------------------------------------------------------------------------

    def fetch_prs(self, since_updated: str | None, on_page: Callable[[int], None] | None = None) -> list[dict]:
        """PRs updated at/after `since_updated` (ISO timestamp), or all PRs if None, normalized."""
        prs: list[dict] = []
        cursor = None
        while True:
            data = self.graphql(PR_QUERY, {"owner": self.repo.owner, "name": self.repo.name, "after": cursor})
            conn = data["repository"]["pullRequests"]
            stop = False
            for node in conn["nodes"]:
                if since_updated and node["updatedAt"] < since_updated:
                    stop = True
                    break
                prs.append(self._normalize(node))
            if on_page:
                on_page(len(prs))
            if stop or not conn["pageInfo"]["hasNextPage"]:
                return prs
            cursor = conn["pageInfo"]["endCursor"]

    def _normalize(self, node: dict) -> dict:
        def who(actor: dict | None) -> tuple[str, str | None]:
            if not actor:
                return "ghost", None
            return actor["login"], actor.get("__typename")

        login, typename = who(node["author"])
        pr = {
            "number": node["number"],
            "title": node["title"],
            "state": node["state"],
            "created_at": node["createdAt"],
            "updated_at": node["updatedAt"],
            "merged_at": node["mergedAt"],
            "closed_at": node["closedAt"],
            "additions": node["additions"],
            "deletions": node["deletions"],
            "changed_files": node["changedFiles"],
            "base": node["baseRefName"],
            "ready_at": next((e["createdAt"] for e in node["timelineItems"]["nodes"] if e), None),
            "author": login,
            "author_type": typename,
        }
        overflow = (
            node["comments"]["pageInfo"]["hasNextPage"]
            or node["reviews"]["pageInfo"]["hasNextPage"]
            or node["reviewThreads"]["pageInfo"]["hasNextPage"]
            or any(t["comments"]["pageInfo"]["hasNextPage"] for t in node["reviewThreads"]["nodes"])
        )
        if overflow:
            pr.update(self._interactions_rest(node["number"]))
            return pr

        def entry(c: dict, at_key: str) -> dict:
            a, t = who(c["author"])
            return {"author": a, "author_type": t, "at": c[at_key]}

        pr["conversation"] = [entry(c, "createdAt") for c in node["comments"]["nodes"]]
        pr["inline"] = [entry(c, "createdAt") for t in node["reviewThreads"]["nodes"] for c in t["comments"]["nodes"]]
        pr["reviews"] = [
            {**entry(r, "submittedAt"), "state": r["state"], "has_body": bool((r["body"] or "").strip())}
            for r in node["reviews"]["nodes"] if r["submittedAt"]
        ]
        return pr

    def _interactions_rest(self, number: int) -> dict:
        """Slow path for PRs with more interactions than one GraphQL page holds."""
        base = f"repos/{self.repo.owner}/{self.repo.name}"

        def entry(c: dict, at_key: str) -> dict:
            user = c.get("user") or {}
            return {"author": user.get("login", "ghost"), "author_type": user.get("type"), "at": c[at_key]}

        return {
            "conversation": [entry(c, "created_at") for c in self.rest_all(f"{base}/issues/{number}/comments")],
            "inline": [entry(c, "created_at") for c in self.rest_all(f"{base}/pulls/{number}/comments")],
            "reviews": [
                {**entry(r, "submitted_at"), "state": r["state"], "has_body": bool((r.get("body") or "").strip())}
                for r in self.rest_all(f"{base}/pulls/{number}/reviews") if r.get("submitted_at")
            ],
        }

    # ---- identities ----------------------------------------------------------------------------

    def logins_for_commits(self, shas: Iterable[str]) -> dict[str, str | None]:
        """Map commit SHA -> GitHub login of its author (None when GitHub can't link it to an account)."""
        shas = list(shas)
        result: dict[str, str | None] = {}
        for i in range(0, len(shas), 50):
            batch = shas[i:i + 50]
            fields = " ".join(
                f'c{j}: object(oid: "{sha}") {{ ... on Commit {{ author {{ user {{ login }} }} }} }}'
                for j, sha in enumerate(batch)
            )
            query = f"query($owner:String!,$name:String!){{repository(owner:$owner,name:$name){{{fields}}}}}"
            data = self.graphql(query, {"owner": self.repo.owner, "name": self.repo.name})["repository"]
            for j, sha in enumerate(batch):
                obj = data.get(f"c{j}") or {}
                user = (obj.get("author") or {}).get("user") or {}
                result[sha] = user.get("login")
        return result


def login_from_noreply(email: str) -> str | None:
    m = _NOREPLY.match(email)
    return m.group(1) if m else None


PR_QUERY = """
query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(first: 25, after: $after, orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title state createdAt updatedAt mergedAt closedAt additions deletions changedFiles baseRefName
        author { login __typename }
        timelineItems(itemTypes: [READY_FOR_REVIEW_EVENT], first: 1) {
          nodes { ... on ReadyForReviewEvent { createdAt } }
        }
        comments(first: 100) {
          pageInfo { hasNextPage }
          nodes { author { login __typename } createdAt }
        }
        reviews(first: 100) {
          pageInfo { hasNextPage }
          nodes { author { login __typename } state body submittedAt }
        }
        reviewThreads(first: 100) {
          pageInfo { hasNextPage }
          nodes {
            comments(first: 50) {
              pageInfo { hasNextPage }
              nodes { author { login __typename } createdAt }
            }
          }
        }
      }
    }
  }
}
"""
