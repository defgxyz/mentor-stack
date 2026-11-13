# hermes-agent-src/ pin

`hermes-agent-src/` (repo root) is a reference checkout of the real upstream Hermes-Agent
project, used to read source/history before touching anything in `hermes/patches/` — never
wired into any build. It's gitignored here (own separate git history, same as
`open-wearables/`), so this file is the only record of which commit it must be.

- Repo: https://github.com/NousResearch/Hermes-Agent.git
- Pinned commit: `9c9e7ab6e5cfb52ed8863126cf9ff54479ef2b73`
- Matches the `org.opencontainers.image.revision` label on the `nousresearch/hermes-agent`
  base image that the currently-running `mentor-hermes` image was built from (verified via
  `docker image inspect` against the live container) — i.e. this is source for the exact
  code actually running, not whatever upstream `:latest` happens to be today.

To reproduce the checkout:

```bash
mkdir hermes-agent-src && cd hermes-agent-src
git init -q
git fetch --depth 1 https://github.com/NousResearch/Hermes-Agent.git 9c9e7ab6e5cfb52ed8863126cf9ff54479ef2b73
git checkout FETCH_HEAD
```

If the running `hermes` image is ever rebuilt against a newer base, update the commit here
to match its new `org.opencontainers.image.revision` label.
