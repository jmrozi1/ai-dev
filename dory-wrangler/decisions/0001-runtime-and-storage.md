# Decision 0001: runtime and storage for the v0.1 chat shell

Status: recorded by #86 (checkpoint `choose-and-record-runtime-stack`).
Scope: Dory-wrangler v0.1 only. Nothing here binds a later release.

This is a decision rather than a default. Development happens on an external
Linux VM and the target is the internal Rocky Linux 9 network, changes reach it
through one nightly mirror, and an unstated stack assumption costs a day to
discover. The risks below are written as claims #89 can put in its assumption
register and #90 can settle by observation.

## The decision

| Layer | Choice |
| --- | --- |
| Runtime | CPython 3.9 from the platform RPM, standard library only |
| Storage | The local filesystem: one JSON record per file, published by `os.link` / `os.replace` / directory `os.rename`, with `fcntl.flock` per chat |
| Shell | `http.server.ThreadingHTTPServer` serving one self-contained HTML page, bound to `127.0.0.1` |
| Tests | `unittest` from the standard library, driven by `tests/run_tests.py` |

No third-party package, no database server, no build step, no package manager,
no network fetch at runtime or at test time.

## Why

**The interpreter is the strongest part of this.** This development VM reports
`Red Hat Enterprise Linux 9.8` and `python3-3.9.25-7.el9_8.3.x86_64`, with
`/usr/bin/python3` symlinked to `python3.9`. Rocky Linux 9 is a rebuild of the
same RHEL 9 sources, so the platform interpreter on the target is the same
package at the same major/minor version, present on a default install with no
entitlement, repository, or proxy needed. Choosing it makes the target's *least
controllable* dependency also its *most certain* one. (Note that `python3.12` is
also present here as a separate RPM; the code is written for 3.9 because 3.9 is
the one that is certain.)

**Standard library only, because the internal network is the constraint.** #85
recorded that nothing about the internal launch path can be verified from this
checkout (F2) and the bridge is unreachable from here. An internal environment
that may have no PyPI mirror, no CDN, and no outbound internet is the reason the
shell page embeds its own CSS and JavaScript and loads nothing from a font host
or a CDN. A test asserts that, because it is the sort of thing that decays
quietly.

**The filesystem, because the contract asks for very little and asks it
precisely.** Contract section 8 lists seven operations. Only two need
concurrency control and both are per-chat, so no global lock and no persistent
coordinator is required -- which removes the main reason to reach for a database
process. What the contract does demand is that an append is atomic and that a
sequence is unique within its chat, and POSIX gives both directly:

* `os.link` from a fully written, fsynced temp file both publishes the record
  and claims the name, in one atomic step that fails if the name is taken. That
  is what makes message-sequence allocation safe between processes with no lock
  at all.
* `os.replace` swaps a whole record for a whole record, so a reader sees one or
  the other.
* `os.rename` of a directory publishes a new chat complete or not at all.
* `fcntl.flock` on a file inside the chat's own directory serializes exactly the
  two operations the contract says need it.

`sqlite3` is in the standard library and was the obvious alternative. It was not
chosen because it buys transactions this design does not need -- the two
multi-record invariants are handled by storing a session and its binding in one
file -- while adding a locking model whose behaviour over a network filesystem
is a second thing to get right internally rather than the same thing. The file
layout also makes contract P4 structural: diagnostics live in a different tree,
so the transcript reader has no path that reaches them. One probe confirms that
by making the diagnostics tree unreadable and rendering a transcript anyway.

**HTTP rather than a terminal UI**, because the acceptance criteria describe a
ChatGPT-like conversation list and active conversation view, and because a
browser is the one rendering surface certain to exist on a workstation that is
already running VS Code. It binds to loopback: v0.1 has no authentication of any
kind, and deciding to expose it is not #86's call to make by default.

## Rocky Linux 9 risks, as claims to settle

These are the things this choice could be wrong about. Each says what would
settle it and what breaks if it is false. **Where this is a guess, it says so.**

**R1. The platform interpreter is present and usable on the internal host.**
*Confidence: high, and the only part of this decision I would call close to
established.* The dev VM is RHEL 9.8 with the platform RPM, and Rocky 9 rebuilds
the same sources. *Guess:* that the internal image has not removed `python3` or
pinned a different default. *Settled by:* `/usr/bin/python3 --version` and
`rpm -q python3` on the internal host. *If false:* the code is plain 3.9 with no
version-specific syntax, so a newer interpreter is very likely fine; an older
one is not, and would need the f-string-free, `ThreadingHTTPServer`-free
fallbacks that are not written.

**R2. `ThreadingHTTPServer` can bind a loopback port and a browser can reach
it.** *Guess.* Nothing about the internal workstation's local networking, proxy
configuration, or browser policy is known here. *Settled by:* run
`run_shell.py`, open the URL, send a turn. *If false:* the store and the service
layer are untouched -- only `webapp.py` is, and the shell could be re-fronted
without changing a durable record. The `--host` and `--port` arguments exist for
this.

**R3. The store lives on a filesystem where `link`, `rename`, `fsync`, and
`flock` behave.** *Guess, and the one I would test first.* Every durability
guarantee here rests on POSIX semantics that local filesystems (xfs, ext4 -- the
Rocky 9 defaults) provide and that NFS historically does not: `flock` over NFS
depends on the mount and the server, and `link`'s exclusivity and `fsync`'s
ordering are weaker. *Settled by:* `findmnt -T <store path>` on the internal
host, plus `tests/run_tests.py` with the store on that path. *If false:* the
store must be placed on local disk, which is a deployment note rather than a
redesign -- and `run_shell.py --root` already makes it a parameter. #90 should
report the filesystem type rather than assume it.

**R4. SELinux does not object.** *Guess.* Rocky 9 enforces SELinux by default.
Writing under a user's home directory and binding an unprivileged loopback port
is ordinary, but a store placed under `/var`, `/srv`, or a share may need a
label. *Settled by:* run it, then `ausearch -m avc -ts recent`. *If false:* a
label or a different store path, not a code change.

**R5. Python 3.9 is enough for the whole v0.1 loop, including #87's launcher.**
*Guess about someone else's ticket, recorded because it constrains them.*
Nothing in #86 needs anything newer. If the internal launch path needs a library
that is not in the standard library, that dependency lands in #87's launcher and
not here; the portability rule in contract section 6 already keeps it on that
side of the seam.

**R6. One nightly mirror means a wrong guess here costs a day.** *Fact, from
#85's F4.* It is listed because it is the reason R1-R4 are written as commands
to run rather than as assurances. The fastest thing #90 can do on day one is run
`tests/run_tests.py` and `validate_store.py` on the internal host, before
anything about the bridge is attempted.

## What later tickets are bound by

* The durable records are the product. `ChatStore` is the only writer, and
  anything that writes a record another way is outside the guarantees here.
* Diagnostics are addressed through `ChatStore.read_diagnostic_events`, which is
  bounded and derives nothing. It is deliberately not an HTTP route.
* The shell starts no process. #87's launcher is consumed through the contract's
  section 6 interface; #86 defines no launcher type, so #87 is free to shape it.
* The instruction bound stays `null` until something measures it (#85's U2).
  Nothing in this store asserts one.
