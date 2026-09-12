"""Durability primitives: atomic creation, atomic replacement, per-chat locking.

Everything this store writes goes through one of three operations, and each has
the same shape: build the complete new bytes somewhere invisible to readers,
force them to stable storage, and then make them visible with a single
kernel-atomic name operation.

* `create_exclusive` -- write, fsync, then `os.link` the temp file onto the
  final name. `link` fails with `EEXIST` if the name is taken, so it is both the
  atomic publish *and* the exclusive claim, in one step. This is what makes
  message-sequence allocation safe between processes without a lock.
* `replace` -- write, fsync, then `os.replace`. A reader sees either the whole
  previous record or the whole new one, never a mixture.
* `create_tree_exclusive` -- build a complete directory out of sight, then
  `os.rename` it into place. A chat exists complete or it does not exist.

A reader never sees a partial record because a partial record never has a name a
reader looks at: temp files live under a `.tmp` prefix and every reader filters
them out by construction, not by trying to parse them and giving up.

## Fault injection

`fault(point)` kills the process at a named point when `DORY_WRANGLER_FAULT`
names it. This is deliberately in the product code rather than in the tests: the
claim being made is about what happens when the *real* write path dies partway,
and a test that dies partway through its own copy of the write path proves
nothing about this one. It is inert unless the environment variable is set, it
cannot be reached from the HTTP surface, and the value is only ever compared for
equality against a fixed set of names.
"""

import errno
import fcntl
import os
import sys

FAULT_ENV = "DORY_WRANGLER_FAULT"

# Every point at which a write can be killed. Named so a test can enumerate them
# and assert it covered all of them, rather than asserting it covered "some".
FAULT_POINTS = (
    "mid_tmp_write",
    "tmp_written",
    "tmp_fsynced",
    "pre_publish",
    "post_publish",
    "pre_parent_fsync",
    "post_message_pre_chat_update",
)

FAULT_EXIT_CODE = 97

TEMP_PREFIX = ".tmp-"


def fault(point):
    """Die immediately if the environment names this point. Otherwise do nothing."""
    if os.environ.get(FAULT_ENV) == point:
        # os._exit, not sys.exit: no unwinding, no atexit, no buffer flushing.
        # A SIGKILL is what is being simulated, so nothing may get a last word.
        os._exit(FAULT_EXIT_CODE)


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_temp(directory, data):
    """Write `data` to a fresh temp file in `directory` and return its path."""
    name = "%s%s" % (TEMP_PREFIX, os.urandom(8).hex())
    path = os.path.join(directory, name)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        half = len(data) // 2
        os.write(fd, data[:half])
        fault("mid_tmp_write")
        os.write(fd, data[half:])
        fault("tmp_written")
        os.fsync(fd)
        fault("tmp_fsynced")
    finally:
        os.close(fd)
    return path


def create_exclusive(path, data):
    """Create `path` with exactly `data`, or raise FileExistsError.

    The name appears only once the bytes are on stable storage, and only if no
    other writer got there first. Both properties come from `os.link`, which is
    atomic and refuses an existing target.
    """
    directory = os.path.dirname(path)
    temp = _write_temp(directory, data)
    try:
        fault("pre_publish")
        os.link(temp, path)
        fault("post_publish")
    finally:
        try:
            os.unlink(temp)
        except OSError as exc:  # pragma: no cover - defensive
            if exc.errno != errno.ENOENT:
                raise
    fault("pre_parent_fsync")
    _fsync_dir(directory)


def replace(path, data):
    """Replace `path` with exactly `data`, atomically."""
    directory = os.path.dirname(path)
    temp = _write_temp(directory, data)
    fault("pre_publish")
    os.replace(temp, path)
    fault("post_publish")
    fault("pre_parent_fsync")
    _fsync_dir(directory)


def create_tree_exclusive(parent, final_name, builder):
    """Build a directory out of sight and publish it with one rename.

    `builder(staging_path)` populates the staging directory. Every regular file
    it created is fsynced, then the whole tree is renamed onto
    `parent/final_name`. Raises FileExistsError if that name is taken.
    """
    staging = os.path.join(parent, "%s%s" % (TEMP_PREFIX, os.urandom(8).hex()))
    os.mkdir(staging, 0o700)
    builder(staging)
    for root, dirs, files in os.walk(staging):
        dirs.sort()
        for name in sorted(files):
            fd = os.open(os.path.join(root, name), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        _fsync_dir(root)
    fault("pre_publish")
    target = os.path.join(parent, final_name)
    # os.rename onto an existing *directory* fails with ENOTEMPTY/EEXIST, but an
    # empty existing directory would be silently replaced. Check first; the
    # window this leaves is closed by the mkdir below being the only other
    # creator of names in this parent, and by identifiers being unguessable.
    if os.path.exists(target):
        raise FileExistsError(errno.EEXIST, "already exists", target)
    os.rename(staging, target)
    fault("post_publish")
    fault("pre_parent_fsync")
    _fsync_dir(parent)


def sweep_temp_files(root):
    """Remove temp files and staging trees left by interrupted writes.

    Called when the store is opened. This is housekeeping for space, not repair:
    nothing a reader can see depends on it, because no temp file ever carries a
    name a reader reads. Removing them cannot change what any read returns.
    """
    removed = 0
    for base, dirs, files in os.walk(root, topdown=True):
        for name in list(dirs):
            if name.startswith(TEMP_PREFIX):
                dirs.remove(name)
                _rmtree(os.path.join(base, name))
                removed += 1
        for name in files:
            if name.startswith(TEMP_PREFIX):
                try:
                    os.unlink(os.path.join(base, name))
                    removed += 1
                except OSError as exc:  # pragma: no cover - defensive
                    if exc.errno != errno.ENOENT:
                        raise
    return removed


def _rmtree(path):
    for base, dirs, files in os.walk(path, topdown=False):
        for name in files:
            os.unlink(os.path.join(base, name))
        for name in dirs:
            os.rmdir(os.path.join(base, name))
    os.rmdir(path)


class ChatLock(object):
    """An exclusive advisory lock over one chat.

    Contract 8 requires concurrency control for exactly two operations --
    opening a binding, and appending a session transition against the session's
    state -- and states that both are per-chat, so there is no global lock and
    no coordinator here. The lock file lives inside the chat's own directory and
    is held only for the duration of one read-decide-write.

    `fcntl.flock` is used rather than `fcntl.lockf`: flock locks are held by the
    open file description, so they are not dropped when an unrelated descriptor
    on the same file is closed elsewhere in the process. Neither is safe over
    NFS in the general case; see decisions/0001 risk R3.
    """

    def __init__(self, path):
        self.path = path
        self._fd = None

    def __enter__(self):
        self._fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None
        return False


def describe_environment():
    """Facts about the interpreter this store is actually running on."""
    return {
        "python_version": sys.version.split()[0],
        "platform": sys.platform,
    }
