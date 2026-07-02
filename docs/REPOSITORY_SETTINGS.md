# Repository Settings

Recommended GitHub settings for the public repository.

## General

- Default branch: `main`
- Issues: enabled
- Discussions: optional
- Wiki: disabled unless project documentation outgrows the README
- Projects: optional
- Merge strategy: squash merge enabled; merge commits and rebase merge disabled
- Delete head branches after merge: enabled

Apply the non-branch settings with GitHub CLI:

```bash
gh repo edit marlonfcaraujo/code-intel \
  --description "Self-contained repository intelligence for faster AI-assisted code changes" \
  --enable-issues=true \
  --enable-wiki=false \
  --enable-projects=false \
  --enable-squash-merge=true \
  --enable-merge-commit=false \
  --enable-rebase-merge=false \
  --delete-branch-on-merge=true \
  --allow-update-branch=true \
  --enable-secret-scanning=true \
  --enable-secret-scanning-push-protection=true
```

## Protect `main`

GitHub's branch-protection endpoint requires the branch to exist first. If the
remote is empty, push `main` before applying protection:

```bash
git push -u origin dev:main
```

Then apply branch protection:

```bash
gh api \
  --method PUT \
  -H "Accept: application/vnd.github+json" \
  repos/marlonfcaraujo/code-intel/branches/main/protection \
  --input - <<'JSON'
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["test"]
  },
  "enforce_admins": true,
  "required_pull_request_reviews": {
    "dismiss_stale_reviews": true,
    "require_code_owner_reviews": true,
    "required_approving_review_count": 1,
    "require_last_push_approval": false
  },
  "restrictions": null,
  "required_linear_history": true,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "block_creations": false,
  "required_conversation_resolution": true,
  "lock_branch": false,
  "allow_fork_syncing": true
}
JSON
```

Expected result:

- direct pushes to `main` are blocked unless they satisfy protection
- pull requests require one approval
- CODEOWNERS review is required
- stale approvals are dismissed after new commits
- the `test` CI job must pass
- force pushes and branch deletion are blocked
