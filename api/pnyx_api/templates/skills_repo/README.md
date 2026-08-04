# Skills

This repo holds this org's skills — the things an assembled agent can be
given at creation time. A skill is a directory named `SKILL.md` (frontmatter:
`name`, `description`, optional `tags`) plus whatever supporting files it
needs; pnyx's catalog reads only the published bucket copy, never this repo
directly.

## Layout

Either layout works, and both may be used at once:

- `teams/<team>/<id>/SKILL.md` — team-scoped; `<team>` is whatever slug your
  org organizes skills by.
- `skills/<id>/SKILL.md` — legacy/flat; published under the team `shared`.

## How publish works

`.github/workflows/publish-skills.yml` runs on every pull request (discovery
+ frontmatter validation only, `--check` — no cloud auth needed, so a bad
skill fails review before it ever reaches the bucket) and on every push to
`main` (the same validation, then a real publish: every skill's files are
uploaded to the catalog bucket and `index.json` is rewritten to match exactly
what's in this repo). The publisher itself is
`.github/scripts/publish_skills.py`, vendored in from pnyx — see that file's
own docstring for the bucket layout it writes.

Nothing else needs to run this by hand: push to `main` and the catalog bucket
converges to match this repo within one publish run.
