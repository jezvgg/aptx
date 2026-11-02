# aptx

Run Debian packages without installing them.

```console
$ aptx cowsay "hi"
 ____
< hi >
 ----
        \   ^__^
         \  (oo)\_______
            (__)\       )\/\
                ||----w |
                ||     ||
```

`aptx` unpacks a package and its dependency closure into a cache directory, then
starts the binary inside a [bubblewrap](https://github.com/containers/bubblewrap)
sandbox where those files appear at their **real paths** (`/usr/games/cowsay`,
`/usr/share/cowsay/cows/…`) and `$HOME` is a throwaway tmpfs.

Nothing is written to `/usr`, nothing lands in your home directory, and
`aptx rm-all` leaves no trace behind.

## Why not `apt-get install`?

| | `apt-get install` | `aptx run` |
| --- | --- | --- |
| touches `/usr` | yes | no |
| touches `$HOME` | yes | no (tmpfs) |
| removal | `apt-get purge` + orphans | `aptx rm-all` |
| side effects | services, users, dpkg state | none |
| version conflicts | yes | one per package, isolated |

This is not a replacement for `apt` (it never installs into the system) and not a
security sandbox (it is not hardened against a determined attacker). It is a
tool for trying out a CLI program without committing to it.

## Install

```console
$ uv tool install git+https://example.com/aptx     # or: uv tool install .
$ uvx --from . aptx doctor
```

Requires Linux, Python ≥ 3.11, `bubblewrap`, and the `apt`/`dpkg` tooling
(`apt-cache`, `apt-get`, `dpkg`, `dpkg-deb`). Runtime dependencies are `click`
(argument parsing, generated help) and `rich` (download progress, tables); `uv`
installs them for you.

Administrative utilities work under `sudo`: `sudo aptx nvme-cli smart-log /dev/nvme1n1`
stages the package into root's cache and runs it in a sandbox whose `$HOME` is
`/root`. Under root no user namespace is created and `--cap-add ALL` is passed
instead — a device ioctl checks the *initial* user namespace, so a nested one
would deny it however root you are. The mount namespace still isolates the
filesystem, but as root the sandbox is not a security boundary.

## Usage

```console
aptx <pkg> [args...]                  shorthand for `aptx run <pkg> [args...]`
aptx run <pkg> [bin] [--] [args...]   run a binary in the sandbox
aptx install <pkg> [bin...]           stage a package and shim its binaries
aptx remove <pkg>... | --all          unstage packages and drop their shims
aptx list                             staged packages, sizes, bins, shims
aptx ls <pkg>                         plan, executables and closure (stages it)
aptx which <pkg> [bin]                store path of a binary (runs nothing)
aptx shell <pkg> [--] [args...]       /bin/sh inside the sandbox (`-- -c 'cmd'`)
aptx plan <pkg>                       the bind/merge plan only
aptx store <path|size|du>             store location and disk usage
aptx gc [--older-than 7d]             drop entries unused for N days
aptx rm-all                           drop everything
aptx pin <pkg>=<version>              pin a version (`--clear` drops it)
aptx unpin <pkg>                      same as `pin <pkg> --clear`
aptx doctor                           check the environment
```

### Shorthand

The first token is looked up as a command first; if there is no such command and
it does not start with `-`, it is a package name:

```console
aptx cowsay "hi"            # aptx run cowsay "hi"
aptx cowsay -f tux "hi"     # -f is cowsay's flag, not aptx's
```

aptx's own flags are not available in this form (`aptx --gpu cowsay` would be a
global flag), so use `aptx run --gpu cowsay` when you need them.

### Argument convention

aptx flags go **before** the package name; everything after it belongs to the
program. `--` is accepted and dropped, never required.

```console
aptx run --offline cowsay -f tux hi     # --offline is aptx's, -f is cowsay's
aptx run cowsay -- -f tux hi            # identical
aptx shell cowsay -- -c 'echo $HOME'    # /bin/sh inside the sandbox
```

The first token after the package is treated as a binary name only if the package
actually provides it; otherwise it is passed to the program like any other
argument.

When a package provides several executables, aptx uses the one named after the
package (the shallowest path wins when two files share a name, as with `git`) and
asks you to choose otherwise:

```console
$ aptx run cowsay hi          # cowsay ships cowsay, cowthink, cowsay_random
$ aptx run cowsay cowthink hi # explicit
```

```console
$ aptx run moreutils sponge hello
aptx: error: moreutils provides several executables and none is named moreutils
aptx: hint: pick one explicitly:
  chronic
  combine
  errno
  …
```

### `run` options

| flag | effect |
| --- | --- |
| `--offline` | no network namespace |
| `--gpu` | expose `/dev/dri`, `/dev/kfd` |
| `--gui` | expose X11/Wayland sockets, `XAUTHORITY`, fonts, icons |
| `--persist[=NAME]` | keep `$HOME` between runs: private per package, or shared by every package that names the same `NAME` |
| `--ephemeral` | throwaway store and `$HOME`, nothing kept |
| `--rw HOST[:TARGET]` | extra writable bind (repeatable) |
| `--rw-cwd` | make the current directory writable |
| `--dev-bind SRC[:DST]` | extra device bind (repeatable) |
| `--dev` | expose the whole host `/dev` |
| `--recommends` | follow `Recommends` transitively (rebuilds a cached closure that was resolved without it) |
| `--backend B` | `auto` (default), `bwrap`, `proot`, `none` |
| `--no-sandbox` | run the binary directly — debugging only |
| `--force-stage` | rebuild the store entry from scratch |
| `--dry-run` | print the sandbox command line, run nothing |

A device named on the program's own command line is bound in automatically, so
`aptx run nvme-cli smart-log /dev/nvme1n1` sees the device without `--dev-bind`.
Only existing character/block devices are considered, and `--dev` disables the
per-device binds in favour of the whole host `/dev`.

## How it works

1. **Resolve.** `apt-cache depends --recurse` gives the closure; only
   `Depends`/`PreDepends` are followed. Packages already installed on the host
   are not downloaded — their files come from the host bind.
2. **Unpack.** `apt-get download --print-uris` gives the exact byte count up front,
   so the download runs behind a `rich` progress bar; `dpkg -x` then unpacks into
   `~/.cache/aptx/store/<pkg>/`. A `.ready` marker is written last, so a
   half-finished entry is rebuilt.
3. **Plan.** For every provided path: if its parent exists on the host it becomes
   a *merge point* (the host has the directory, the package adds to it);
   otherwise the whole subtree is a *shadow* bound straight from the store.
4. **Stage.** A merge point is materialised as a **symlink farm** — symlinks to
   the host entries plus symlinks into the store — because overlayfs is not
   mountable in a user namespace and mount points cannot be created in a
   read-only bind.
5. **Run.** `bwrap` starts from an empty tmpfs root, binds the store at
   `/run/aptx/root`, binds each farm over its real path, and binds the host
   directories the system needs read-only. `--proc /proc` and `--dev /dev` are
   issued *before* any device bind, because `--dev` installs a fresh devtmpfs and
   would otherwise hide what was mounted earlier.

Because the farm is a real directory tree of symlinks, the package's files are
reachable at their canonical paths — which is what makes `cowsay -f tux` find
`/usr/share/cowsay/cows/tux.cow`.

### Merge priority

When a package ships a file the host already has, the **package wins**. For
`/etc` the host copy stays visible next to it as `<file>.host`, so you can diff
what the package would have changed.

## Store layout

```text
~/.cache/aptx/store/cowsay/
├── .ready            written last; its absence means "rebuild me"
├── .meta.json        version, arch, closure size, creation time
├── .provides.tsv     kind \t path \t package   (the whole closure)
├── .files.tsv        executables of the requested package
├── .deps.tsv         package \t version \t arch \t sha256
├── .stage-hash       cache key for the symlink farm
├── .last-used        touched on every run, used by `gc`
├── usr/              the unpacked package root
└── stage/            the symlink farm
```

Everything outside `aptx`'s own dotfiles belongs to `dpkg`, so the root can be
compared against `dpkg -c` at any time.

## Backends

`bwrap` is the default and the only one that gives real isolation: read-only
host system, private `$HOME`, no network unless asked. `proot` is a fallback for
machines without user namespaces — it overlays paths but hides nothing and cannot
do `--offline`. `--no-sandbox` runs the binary directly and exists for debugging.

## Limitations

- Linux only; the sandbox relies on user namespaces.
- The host's `glibc`, `libstdc++` and friends are used as-is; aptx does not
  build a private `/usr/lib`. Packages needing a newer glibc than the host has
  will not work.
- `--offline` blocks the network but not filesystem reads: the process can still
  read anything the host exposes read-only.
- Not a security boundary. Treat it as a tidiness tool.

See [SPEC.md](SPEC.md) for the full design and [PROJECT.md](PROJECT.md) for the
original brief.

## Development

```console
$ uv venv && uv pip install -e . pytest coverage
$ .venv/bin/pytest                       # everything
$ .venv/bin/pytest -m "not integration"  # fast, no network, no sandbox
$ .venv/bin/python -m coverage run -m pytest && .venv/bin/python -m coverage report
```

Unit tests never touch the network or spawn a sandbox; the integration tests are
marked and run the real thing. `coverage report` is configured with
`fail_under = 100` for `src/aptx`, so the suite doubles as the coverage gate.
