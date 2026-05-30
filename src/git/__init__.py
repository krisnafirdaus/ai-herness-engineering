"""Git workspace management: clone/copy, branch, base-ref capture, diff, rollback.

The ``base_ref`` captured at preparation time is the rollback anchor: if a run
exhausts its retry budget, the workspace is hard-reset to that ref so no partial,
unverified edits survive. The same ref produces the final diff that becomes a PR.
"""
from .repo_manager import RepoManager

__all__ = ["RepoManager"]
