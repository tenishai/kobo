# pnyx-config

This repo is pnyx's ROOM configuration, and only that. It holds one file,
`org.yaml`, whose keys pnyx reads at boot and merges over its own local
defaults (see that file's own header comment, and `pnyx_api.org.ORG_KEYS` in
the pnyx source for the authoritative key list).

## What does *not* belong here

Never put team content in this repo: no skills, no personas, no agent
bundles, no knowledge files. That content lives in its own place —

- Skills live in the org's skills repo (see `skills_repo` below) and are
  published to the skills catalog bucket by that repo's own CI.
- Personas live directly in the personas catalog bucket; pnyx's persona
  builder writes there straight from the room, no repo in between.

This repo is config-only so that changing where the room points (a cluster,
a bucket, a model list) never touches — and never needs review from — the
teams whose skills or personas those buckets actually hold.

## Editing this file

`org.yaml` was generated once by pnyx's bootstrap CLI from whatever it was
run with. From here on it's yours: edit it directly and push — the next
pnyx boot picks up the new values. A key pnyx doesn't recognize is rejected
at boot (pnyx falls back to running unwired rather than guessing), so keep
edits to the keys already listed in the file.
