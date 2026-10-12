# Git hooks

## Reference-hull guard (`pre-commit`)

`in.mlt`, `off1.mlt`, and `out.mlt` are the tracked reference hull that every
sweep validates against. Running Godzilla (`mlt933w.exe`) or a sweep/render
script **in the repo root** overwrites them in place; a crashed or partial run
leaves them corrupted (`off1.mlt` and `out.mlt` have been seen byte-identical),
which silently breaks every downstream offset parse.

The `pre-commit` hook blocks a commit that changes any of those three files
unless the pinned SHA-256 manifest (`tests/fixtures/reference_hull.sha256`) is
updated to match — i.e. an intentional hull redesign.

### Install (once per clone)

```sh
git config core.hooksPath .githooks
```

That's it — hooks live in the repo, so no copying into `.git/hooks`.

### Layers

- **CI (always on):** `tests/test_reference_hull_integrity.py` checks the same
  hashes, so a corrupted hull fails CI even if the hook isn't installed. The
  hook is a local convenience that saves the round-trip.
- **Intentional redesign:** regenerate the manifest in the same commit:
  ```sh
  sha256sum in.mlt off1.mlt out.mlt > tests/fixtures/reference_hull.sha256
  ```
  (then restore the header comment)

### Related

`services/hull_service.launch_godzilla` already runs the binary in a scratch
subdirectory and promotes `off1.mlt` / `out.mlt` back only on a clean exit, so
the *app* path is safe. This guard covers the remaining exposure: ad-hoc
scripts run directly in the repo root.
