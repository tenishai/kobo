# Contributing to kobo

Issues and pull requests are welcome.

## Set up

- API (Python 3.12): `cd api && uv sync --locked --extra dev --python 3.12`, the same
  install the README Quickstart and CI use, then copy `api/config.example.yaml` to
  `api/config.local.yaml` and fill it in. Run it with `make -C api run` (serves on `:8100`).
  `make -C api install` still builds a plain pip venv if you prefer one; `uv` is the
  canonical setup (decided 2026-10-18).
- UI (Node): `make -C ui install`, then `make -C ui dev` (serves on `:5173`, proxying to the API).

## Run the tests

- API: `make -C api check` (ruff lint plus pytest)
- UI: `make -C ui check` (svelte-check plus vitest)

[TESTING.md](TESTING.md) says what earns a place in the suite. Tests under `api/tests/scaffold/`
and `ui/src/scaffold/` are for driving a change and must not be part of a pull request; CI
fails any PR that carries one.

## Open a pull request

Branch from `main`, keep the change focused, add or update tests for behavior you change, and
open a PR describing what changed and why. Commit messages follow the conventional style used in
the history (`feat(scope): ...`, `fix(scope): ...`). Sign off every commit with `git commit -s`; see [Developer Certificate of Origin](#developer-certificate-of-origin).

Report security problems as described in [SECURITY.md](SECURITY.md), not in a public issue.

## On macOS, put the GNU tools first

The scripts here, and the commands in this repo's docs, are written for the GNU versions of `sed`, `awk`, `grep`, `find`, `xargs`, `tar`, `date`, `stat`, `readlink`, `env` and `diff`, as on Linux. The BSD versions that ship with macOS take different flags. A common case is `sed -i`: BSD needs a backup suffix after it, GNU doesn't. Some commands fail outright, and some print something else and carry on. Install the GNU tools and put them ahead of `/usr/bin` on your `PATH`.

With MacPorts:

```bash
sudo port install coreutils findutils gsed gawk grep gnutar diffutils
export PATH="/opt/local/libexec/gnubin:$PATH"    # in ~/.profile or ~/.zshrc
```

With Homebrew:

```bash
brew install coreutils findutils gnu-sed gawk grep gnu-tar diffutils
for f in coreutils findutils gnu-sed gawk grep gnu-tar; do
  PATH="$(brew --prefix)/opt/$f/libexec/gnubin:$PATH"
done
export PATH                                      # in ~/.profile or ~/.zshrc
```

Check: `sed --version` should print `sed (GNU sed)`. Re-open your shell, or source the profile, after changing `PATH`.

## Developer Certificate of Origin

kobo uses the [Developer Certificate of Origin](https://developercertificate.org/)
(DCO) instead of a contributor license agreement. Every commit in a pull request
must carry a `Signed-off-by:` line with your name and email, and they must match
the commit's author. `git commit -s` adds it from your `user.name` and
`user.email`:

```
Signed-off-by: Jane Doe <jane@example.com>
```

By adding that line you certify the following, quoted from
<https://developercertificate.org/>:

```
Developer Certificate of Origin
Version 1.1

Copyright (C) 2004, 2006 The Linux Foundation and its contributors.

Everyone is permitted to copy and distribute verbatim copies of this
license document, but changing it is not allowed.


Developer's Certificate of Origin 1.1

By making a contribution to this project, I certify that:

(a) The contribution was created in whole or in part by me and I
    have the right to submit it under the open source license
    indicated in the file; or

(b) The contribution is based upon previous work that, to the best
    of my knowledge, is covered under an appropriate open source
    license and I have the right under that license to submit that
    work with modifications, whether created in whole or in part
    by me, under the same open source license (unless I am
    permitted to submit under a different license), as indicated
    in the file; or

(c) The contribution was provided directly to me by some other
    person who certified (a), (b) or (c) and I have not modified
    it.

(d) I understand and agree that this project and the contribution
    are public and that a record of the contribution (including all
    personal information I submit with it, including my sign-off) is
    maintained indefinitely and may be redistributed consistent with
    this project or the open source license(s) involved.
```

The `dco` CI check runs on every pull request to `main`. It checks each
non-merge commit the pull request adds, and fails naming each commit whose
sign-off is missing or doesn't match its author (name and email, compared
without regard to case). To fix it:

```bash
# the last commit only
git commit --amend --signoff --no-edit

# every commit on your branch (this signs every rebased commit as you, which
# satisfies the check only for commits whose author is you)
git rebase --signoff origin/main

# sign-off present but under a different name or email than the author:
# set user.name / user.email to the identity you want, then
git commit --amend --reset-author --signoff --no-edit

git push --force-with-lease
```

Commits already on `main` from before this rule are not re-checked. A pull request
opened by someone with write access to this repository (admin, maintain or
write; tenish's members all have it) skips the check, including one opened
from a fork; it is enforced on pull requests from everyone else. On a same-repo pull request, commits authored exactly as
Dependabot's noreply address are skipped too, because Dependabot cannot sign off
under that address.

## Using AI tools

AI-assisted contributions are welcome; we build this way ourselves. You are
responsible for every line you submit: understand it, test it, and be able to
explain it in review. Say in the pull request description which AI tools you
used. Crediting the agent with a `Co-Authored-By:` trailer is optional. Don't
submit code a tool reproduced from a source whose license isn't compatible with
the MIT license.
