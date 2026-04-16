"""
Git repository search engine.

Clones one or more remote git repositories (public or private) and searches
file content using ``git grep``.  Cloned repos are cached in a persistent
directory so subsequent searches reuse the local copy.
"""

import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from langchain_core.language_models import BaseLLM
from loguru import logger

from ..search_engine_base import BaseSearchEngine

# Allowed URL schemes for cloning
_ALLOWED_SCHEMES = {"https", "ssh"}

# git@ URLs don't have a scheme — we detect them by pattern instead
_SSH_PATTERN = re.compile(r"^git@[\w.\-]+:[\w.\-/]+(?:\.git)?$")

_GIT_TIMEOUT_SECONDS = 120


def _repo_slug(url: str) -> str:
    """Derive a filesystem-safe cache key from a repo URL."""
    cleaned = url.rstrip("/")
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]

    # git@github.com:org/repo → org/repo
    if _SSH_PATTERN.match(cleaned):
        cleaned = cleaned.split(":", 1)[1]
    else:
        parsed = urlparse(cleaned)
        cleaned = parsed.path.lstrip("/")

    return cleaned.replace("/", "__")


def _repo_display_name(url: str) -> str:
    """Short human-readable name for a repo URL (e.g. 'org/repo')."""
    cleaned = url.rstrip("/")
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]

    if _SSH_PATTERN.match(cleaned):
        return cleaned.split(":", 1)[1]

    parsed = urlparse(cleaned)
    return parsed.path.lstrip("/")


def _validate_url(url: str) -> None:
    """Reject file://, local paths, and other unsafe URL forms."""
    stripped = url.strip()
    if _SSH_PATTERN.match(stripped):
        return  # git@host:org/repo is fine

    parsed = urlparse(stripped)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ValueError(
            f"Unsupported URL scheme '{parsed.scheme}' — "
            f"only https:// and git@ SSH URLs are allowed"
        )

    if not parsed.hostname:
        raise ValueError(f"URL has no hostname: {stripped}")


def _inject_token(url: str, token: str) -> str:
    """Insert a PAT into an HTTPS clone URL."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return url
    # https://TOKEN@github.com/org/repo.git
    netloc = f"{token}@{parsed.hostname}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return parsed._replace(netloc=netloc).geturl()


def _sanitize_git_output(text: str, token: Optional[str] = None) -> str:
    """Strip tokens from git stderr / error messages."""
    if token and token in text:
        text = text.replace(token, "[REDACTED]")
    return text


class GitRepoSearchEngine(BaseSearchEngine):
    """Search file content inside one or more cloned git repositories."""

    is_local = True
    is_lexical = True
    is_code = True
    needs_llm_relevance_filter = True

    def __init__(
        self,
        repo_urls: Optional[List[str]] = None,
        repo_url: Optional[str] = None,
        cache_dir: Optional[str] = None,
        file_patterns: Optional[List[str]] = None,
        auth_token: Optional[str] = None,
        ssh_key_path: Optional[str] = None,
        max_results: int = 30,
        llm: Optional[BaseLLM] = None,
        max_filtered_results: Optional[int] = None,
        settings_snapshot: Optional[Dict[str, Any]] = None,
        **kwargs,
    ):
        super().__init__(
            llm=llm,
            max_filtered_results=max_filtered_results,
            max_results=max_results,
            settings_snapshot=settings_snapshot,
            **kwargs,
        )

        # Accept either repo_urls (list) or repo_url (single, for back-compat)
        raw_urls = self._ensure_list(repo_urls)
        if not raw_urls and repo_url:
            raw_urls = self._ensure_list(repo_url)
        self.repo_urls: List[str] = [u.strip() for u in raw_urls if u.strip()]

        self.file_patterns = self._ensure_list(file_patterns)
        self.ssh_key_path = ssh_key_path

        # Resolve auth token: explicit param → env var
        self._auth_token: Optional[str] = None
        if auth_token and self._is_valid_api_key(auth_token):
            self._auth_token = auth_token
        else:
            env_token = os.environ.get("LDR_GIT_TOKEN", "").strip()
            if env_token and self._is_valid_api_key(env_token):
                self._auth_token = env_token

        # Cache directory
        if cache_dir:
            self._cache_dir = Path(cache_dir)
        else:
            self._cache_dir = Path.home() / ".cache" / "ldr" / "repos"

        # Maps repo_url → local Path (populated lazily)
        self._local_paths: Dict[str, Path] = {}

    # ------------------------------------------------------------------
    # Clone / update
    # ------------------------------------------------------------------

    def _ensure_repo(self, repo_url: str) -> Path:
        """Clone or update a single cached repo.  Returns the local path."""
        if repo_url in self._local_paths:
            path = self._local_paths[repo_url]
            if path.exists():
                return path

        _validate_url(repo_url)

        slug = _repo_slug(repo_url)
        local_path = self._cache_dir / slug
        env = self._git_env()

        if (local_path / ".git").is_dir():
            logger.info(f"Updating cached repo at {local_path}")
            try:
                self._run_git(
                    ["git", "pull", "--ff-only"],
                    cwd=local_path,
                    env=env,
                )
            except RuntimeError:
                logger.warning(
                    f"git pull failed for {_repo_display_name(repo_url)}, "
                    f"using existing cache"
                )
        else:
            self._cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            clone_url = repo_url
            if self._auth_token:
                clone_url = _inject_token(repo_url, self._auth_token)

            logger.info(
                f"Cloning {_repo_display_name(repo_url)} → {local_path}"
            )
            self._run_git(
                ["git", "clone", "--depth", "1", clone_url, str(local_path)],
                env=env,
            )

        self._local_paths[repo_url] = local_path
        return local_path

    def _ensure_all_repos(self) -> List[Tuple[str, Path]]:
        """Clone/update all configured repos.  Returns (url, path) pairs."""
        results: List[Tuple[str, Path]] = []
        for url in self.repo_urls:
            try:
                path = self._ensure_repo(url)
                results.append((url, path))
            except Exception:
                logger.exception(
                    f"Failed to clone/update {_repo_display_name(url)}"
                )
        return results

    def _git_env(self) -> Dict[str, str]:
        """Build environment for git subprocesses."""
        import shlex

        env = os.environ.copy()
        if self.ssh_key_path:
            key_path = Path(self.ssh_key_path).resolve()
            if not key_path.is_file():
                raise ValueError(f"SSH key not found: {self.ssh_key_path}")
            env["GIT_SSH_COMMAND"] = (
                f"ssh -i {shlex.quote(str(key_path))} "
                f"-o StrictHostKeyChecking=accept-new"
            )
        # Prevent git from prompting for credentials interactively
        env["GIT_TERMINAL_PROMPT"] = "0"
        return env

    def _run_git(
        self,
        cmd: List[str],
        cwd: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> str:
        """Run a git command and return stdout.  Raises on failure."""
        try:
            result = subprocess.run(
                cmd,
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"git command timed out after {_GIT_TIMEOUT_SECONDS}s: "
                f"{' '.join(cmd[:3])}..."
            )

        if result.returncode != 0:
            safe_stderr = _sanitize_git_output(
                result.stderr, self._auth_token
            )
            raise RuntimeError(
                f"git failed (rc={result.returncode}): {safe_stderr}"
            )

        return result.stdout

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _get_previews(self, query: str) -> List[Dict[str, Any]]:
        """Search file content with ``git grep`` across all repos."""
        if not self.repo_urls:
            logger.warning("No repo URLs configured for git_repo engine")
            return []

        repos = self._ensure_all_repos()
        if not repos:
            return []

        all_previews: List[Dict[str, Any]] = []
        per_repo_limit = max(1, self.max_results // len(repos))

        for repo_url, repo_path in repos:
            previews = self._grep_repo(query, repo_url, repo_path, per_repo_limit)
            all_previews.extend(previews)

        logger.info(
            f"git grep returned {len(all_previews)} total matches "
            f"across {len(repos)} repo(s)"
        )
        return all_previews

    def _grep_repo(
        self,
        query: str,
        repo_url: str,
        repo_path: Path,
        max_count: int,
    ) -> List[Dict[str, Any]]:
        """Run git grep on a single repo and return preview dicts."""
        # Use -e to explicitly mark the query as a pattern (prevents
        # queries starting with '-' from being interpreted as flags)
        cmd = [
            "git", "grep",
            "-n", "-i",
            f"--max-count={max_count}",
            "-e", query,
        ]

        # Restrict to specific file patterns (validated: no leading '-')
        if self.file_patterns:
            cmd.append("--")
            cmd.extend(self.file_patterns)

        env = self._git_env()
        try:
            result = subprocess.run(
                cmd,
                cwd=repo_path,
                env=env,
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                f"git grep timed out for {_repo_display_name(repo_url)}"
            )
            return []

        # git grep returns 1 when no matches — not an error
        if result.returncode not in (0, 1):
            safe_err = _sanitize_git_output(result.stderr, self._auth_token)
            logger.warning(f"git grep failed: {safe_err}")
            return []

        return self._parse_grep_output(result.stdout, repo_url)

    def _parse_grep_output(
        self, stdout: str, repo_url: str
    ) -> List[Dict[str, Any]]:
        """Parse ``git grep -n`` output into preview dicts."""
        previews: List[Dict[str, Any]] = []
        slug = _repo_slug(repo_url)
        display_name = _repo_display_name(repo_url)

        # Normalise repo URL for building web links
        base_url = repo_url.rstrip("/")
        if base_url.endswith(".git"):
            base_url = base_url[:-4]

        for line in stdout.splitlines():
            if not line.strip():
                continue

            # Format: file_path:line_num:matched_text
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue

            file_path, line_num, matched_text = parts[0], parts[1], parts[2]

            try:
                line_int = int(line_num)
            except ValueError:
                continue

            preview_id = f"{slug}:{file_path}:{line_num}"

            # Build a web-browsable link (works for GitHub/GitLab/Bitbucket)
            link = f"{base_url}/blob/HEAD/{file_path}#L{line_int}"

            previews.append(
                {
                    "id": preview_id,
                    "title": f"{file_path} (line {line_int})",
                    "snippet": matched_text.strip(),
                    "link": link,
                    "file_path": file_path,
                    "line_number": line_int,
                    "repo_url": repo_url,
                    "repo_name": display_name,
                }
            )

        return previews

    # ------------------------------------------------------------------
    # Full content
    # ------------------------------------------------------------------

    def _get_full_content(
        self, relevant_items: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Read full file content for each matched file."""
        results: List[Dict[str, Any]] = []
        # Cache: (repo_url, file_path) → content
        seen_files: Dict[Tuple[str, str], str] = {}

        for item in relevant_items:
            result = item.copy()
            file_path = item.get("file_path", "")
            repo_url = item.get("repo_url", "")
            cache_key = (repo_url, file_path)

            if file_path and cache_key not in seen_files:
                local_path = self._local_paths.get(repo_url)
                if local_path:
                    full_path = local_path / file_path
                    try:
                        content = full_path.read_text(errors="replace")
                        seen_files[cache_key] = content
                    except OSError:
                        logger.debug(f"Could not read {full_path}")
                        seen_files[cache_key] = ""

            result["full_content"] = seen_files.get(cache_key, "")
            results.append(result)

        return results
