"""Structured-output contract and the S.C.O.P.E. remediation prompt."""

from __future__ import annotations

import re

from app.config import settings

STRUCTURED_OUTPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "issue_number": {"type": "integer", "description": "GitHub issue that was remediated."},
        "outcome": {
            "type": "string",
            "enum": ["fixed", "partial", "blocked"],
            "description": "fixed = issue fully resolved; partial = some work landed; blocked = could not proceed.",
        },
        "pr_url": {"type": "string", "description": "URL of the pull request opened for the fix."},
        "summary": {"type": "string", "description": "What changed and why, in two or three sentences."},
        "files_changed": {"type": "array", "items": {"type": "string"}, "description": "Repo-relative paths touched."},
        "risk_notes": {"type": "string", "description": "Residual risk, assumptions, conservative choices made."},
        "verification": {"type": "string", "description": "Commands run and their results."},
    },
    "required": ["issue_number", "outcome", "summary"],
}


def slugify(text: str, max_length: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (slug[:max_length].strip("-")) or "remediation"


def build_prompt(issue_number: int, title: str, body: str) -> str:
    repo = settings.target_repo
    base = settings.base_branch
    branch = f"devin/issue-{issue_number}-{slugify(title)}"

    return f"""You are remediating a security/quality finding filed as GitHub issue #{issue_number}.

S — SITUATION
A scanner (or a reviewer) filed issue #{issue_number} in {repo}. It is reproduced verbatim at the
end of this prompt. The repository under remediation is {repo} and the base branch is {base}.
Work ONLY in {repo}; never modify any other repository.

C — CHALLENGE
Fix exactly what the issue describes — nothing more. Produce the smallest reviewable diff that
resolves the finding. No drive-by refactors, no reformatting of untouched code, no dependency
bumps, no renames, and no changes to unrelated files or tests.

O — OPERATION
1. Clone {repo} and branch from {base} as `{branch}`.
2. Locate every occurrence of the finding described in the issue and apply the minimal fix that
   satisfies its acceptance criteria.
3. Match the surrounding code style and use libraries already present in the repository.
4. Commit with a focused message referencing issue #{issue_number}.

P — PROOF
Verify with the narrowest relevant checks only: lint the files you touched and run the tests for
the modules you touched. NEVER run the full CI suite — it is slow and out of scope. Record the
exact commands you ran and their results; if a check cannot run in the environment, say so
explicitly instead of claiming success.

E — EXIT
Open ONE pull request with base repository {repo} and base branch {base} from `{branch}`.
The PR description must cover: what changed and why, the list of files touched, verification
evidence (commands and output), a rollback note, and the line "Fixes #{issue_number}".
If the issue is ambiguous, resolve it conservatively (choose the safest, smallest interpretation),
proceed, and record the ambiguity and your choice in `risk_notes` — do NOT stop to ask.
Finally, report structured output matching the required schema: issue_number, outcome
(fixed|partial|blocked), pr_url, summary, files_changed, risk_notes, verification.

--- ISSUE #{issue_number} SPECIFICATION (verbatim) ---
TITLE: {title}

{body}
"""
