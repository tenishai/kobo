# Security policy

## Reporting a vulnerability

Please don't open a public issue for a security problem.

Report it privately to **security@tenish.ai**.

If this repository's **Security** tab shows a **Report a vulnerability**
button, you can use that instead. GitHub offers it only on public
repositories where it has been turned on.

Include what you can of:

- what the problem is and what an attacker could do with it;
- the version, commit, or tag you tested;
- steps to reproduce, or a proof of concept;
- any configuration it depends on.

You don't need a complete write-up to report. A clear description of the
problem is enough to start.

## What happens next

- We acknowledge your report within **14 days**.
- We tell you whether we've confirmed the problem as soon as we know.
- We fix confirmed problems in the next release, or sooner for serious ones,
  and tell you before the fix is public.
- We credit you in the release notes unless you'd rather we didn't.

We ask that you give us a reasonable chance to fix a problem before disclosing
it publicly, and that you don't access or change data that isn't yours while
testing.

## Supported versions

kobo is pre-1.0. Security fixes go into the **latest release** only; older
releases are not patched. If you're running an older version, upgrade to get
fixes.

## Scope

kobo is the room where people assemble, deploy and talk to agents. In scope:

- access: anything that lets a signed-in person see, change, chat with or
  destroy an agent they were not granted, through the web routes or the MCP
  server: the proxy identity check, group namespaces, the owner and operator
  gates, cross-group invites and consent, the OAuth and token flows;
- secrets and tokens: a secret value, an identity token, or a switchboard pass
  reaching a log, a response body, an audit row, or an agent's repository in
  the clear;
- repositories: a write the room makes to a connected team's repository, or
  a push to any branch it was told never to touch;
- provisioning and destruction: a pipeline step that can run twice for one
  agent, or a destroy that removes a resource belonging to another;
- the agent's own tooling: a bundled tool or skill that the enforcement
  rules should refuse and do not.

**Out of scope:** vulnerabilities in third-party dependencies (please report
those upstream, and tell us if we ship an affected version), and problems that
require an attacker to already control the cluster, the operator account, or
the identity the service runs under.
