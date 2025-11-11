#!/usr/bin/env python3
"""
Fetch logo assets from GitHub repositories listed in the Upstream project catalog.

The script inspects the JSON at `src/data/upstream-projects.json` (configurable)
and, for every project with GitHub repositories attached, attempts to locate a
logo-like asset inside the repo. If no obvious candidate is found it falls back to
GitHub's Open Graph preview image for the repository.

Usage:
  python scripts/fetch_github_logos.py \
      --projects-json src/data/upstream-projects.json \
      --out public/images/project-logos

Optional environment variables:
  GITHUB_TOKEN   Personal access token to raise the GitHub rate limit.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

API_ROOT = "https://api.github.com"
OG_IMAGE_TEMPLATE = "https://opengraph.githubassets.com/1/{owner}/{repo}"
LOGO_NAME_PATTERN = re.compile(r"(logo|brand|icon|badge|mark)", re.IGNORECASE)
LOGO_DIR_HINT = {"assets", "static", "public", "logo", "logos", "branding", "docs"}
IMAGE_EXTENSIONS = (".svg", ".png", ".jpg", ".jpeg", ".webp")


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "project"


def parse_repo_slug(url: str) -> Optional[Tuple[str, str]]:
    parsed = urlparse(url)
    if parsed.netloc not in {"github.com", "www.github.com"}:
        return None
    parts = [segment for segment in parsed.path.strip("/").split("/") if segment]
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1]
    return owner, repo.rstrip(".git")


@dataclass
class RepoLogo:
    owner: str
    repo: str
    url: str
    ext: str
    source: str


class GitHubClient:
    def __init__(self, token: Optional[str] = None):
        self.token = token

    def _request(self, url: str, accept: str = "application/vnd.github+json") -> bytes:
        headers = {
            "User-Agent": "UpstreamLogoCrawler/1.0",
            "Accept": accept,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = Request(url, headers=headers)
        with urlopen(req) as resp:
            return resp.read()

    def json(self, url: str):
        data = self._request(url)
        return json.loads(data.decode("utf-8"))

    def download(self, url: str) -> bytes:
        return self._request(url, accept="application/octet-stream")


def list_directory(
    client: GitHubClient, owner: str, repo: str, path: str = ""
) -> Sequence[dict]:
    quoted = quote(path) if path else ""
    api_url = f"{API_ROOT}/repos/{owner}/{repo}/contents"
    if quoted:
        api_url = f"{api_url}/{quoted}"
    return client.json(api_url)


def score_candidate(name: str, parent: str) -> int:
    score = 0
    lower = name.lower()
    if "logo" in lower:
        score += 5
    if LOGO_NAME_PATTERN.search(name):
        score += 3
    if lower.endswith(".svg"):
        score += 2
    if any(lower.endswith(ext) for ext in IMAGE_EXTENSIONS):
        score += 1
    if parent:
        parent_dir = parent.split("/", 1)[0]
        if parent_dir.lower() in LOGO_DIR_HINT:
            score += 2
    return score


def find_repo_logo(
    client: GitHubClient, owner: str, repo: str, max_depth: int
) -> Optional[RepoLogo]:
    queue = [("", 0)]
    best: Optional[Tuple[int, dict, str]] = None
    while queue:
        current_path, depth = queue.pop(0)
        try:
            entries = list_directory(client, owner, repo, current_path)
        except HTTPError as exc:
            if exc.code == 404:
                return None
            raise
        except URLError:
            raise

        for entry in entries:
            entry_path = "/".join(filter(None, [current_path, entry["name"]]))
            if entry["type"] == "dir" and depth < max_depth:
                hint = entry["name"].lower()
                if hint in LOGO_DIR_HINT or depth == 0:
                    queue.append((entry_path, depth + 1))
                continue

            if entry["type"] != "file":
                continue

            lower_name = entry["name"].lower()
            if not lower_name.endswith(IMAGE_EXTENSIONS):
                continue
            score = score_candidate(entry["name"], current_path)
            if score == 0:
                continue
            if not best or score > best[0]:
                best = (score, entry, entry_path)

    if best:
        _, entry, entry_path = best
        ext = Path(entry["name"]).suffix.lower().lstrip(".")
        download_url = entry.get("download_url")
        if not download_url:
            return None
        return RepoLogo(
            owner=owner,
            repo=repo,
            url=download_url,
            ext=ext or "png",
            source=f"{owner}/{repo}:{entry_path}",
        )

    og_url = OG_IMAGE_TEMPLATE.format(owner=owner, repo=repo)
    return RepoLogo(
        owner=owner,
        repo=repo,
        url=og_url,
        ext="png",
        source="github-opengraph",
    )


def save_logo(content: bytes, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def iter_projects(data: Iterable[dict], filter_names: Optional[set]) -> Iterable[dict]:
    for project in data:
        name = project.get("name")
        if not name:
            continue
        if filter_names and name not in filter_names:
            continue
        yield project


def main() -> int:
    parser = argparse.ArgumentParser(description="Download project logos from GitHub.")
    parser.add_argument(
        "--projects-json",
        default="src/data/upstream-projects.json",
        help="Path to the project metadata JSON file.",
    )
    parser.add_argument(
        "--out",
        default="public/images/project-logos",
        help="Directory where logos should be saved.",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=2,
        help="Depth to traverse inside each repository when searching for logo files.",
    )
    parser.add_argument(
        "--only",
        nargs="*",
        help="Optional subset of project names to process.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download logos even if the destination file already exists.",
    )
    args = parser.parse_args()

    projects_path = Path(args.projects_json)
    out_dir = Path(args.out)
    if not projects_path.exists():
        print(f"[error] Project file {projects_path} not found.", file=sys.stderr)
        return 1

    data = json.loads(projects_path.read_text(encoding="utf-8"))
    filter_names = set(args.only) if args.only else None
    token = os.environ.get("GITHUB_TOKEN")
    client = GitHubClient(token=token)

    processed = 0
    for project in iter_projects(data, filter_names):
        name = project["name"]
        repos = project.get("github") or []
        if not repos:
            print(f"[skip] {name}: no GitHub repository listed.")
            continue
        project_slug = slugify(name)
        dest_path = out_dir / f"{project_slug}.png"
        if dest_path.exists() and not args.force:
            print(f"[skip] {name}: logo already exists at {dest_path}.")
            continue

        logo_downloaded = False
        for repo_url in repos:
            slug = parse_repo_slug(repo_url)
            if not slug:
                continue
            owner, repo = slug
            try:
                candidate = find_repo_logo(client, owner, repo, args.max_depth)
            except HTTPError as exc:
                print(f"[error] {name}: {owner}/{repo} -> HTTP {exc.code}", file=sys.stderr)
                continue
            except URLError as exc:
                print(f"[error] {name}: {owner}/{repo} -> {exc}", file=sys.stderr)
                continue

            if not candidate:
                continue

            try:
                content = client.download(candidate.url)
            except HTTPError as exc:
                print(
                    f"[error] {name}: unable to download {candidate.url} (HTTP {exc.code})",
                    file=sys.stderr,
                )
                continue
            save_logo(content, dest_path.with_suffix(f".{candidate.ext}"))
            print(f"[ok] {name}: saved logo from {candidate.source}")
            logo_downloaded = True
            break

        if not logo_downloaded:
            print(f"[warn] {name}: could not locate a logo asset.")
        else:
            processed += 1

    print(f"[done] Downloaded {processed} logos into {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
