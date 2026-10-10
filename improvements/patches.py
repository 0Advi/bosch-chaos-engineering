"""Isolated, bounded patch preparation. Never executes commands from a patch."""
from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path, PurePosixPath

from controller.schemas.contracts import ALLOWED_PATCH_PREFIXES, Patch

MAX_DIFF_BYTES = 512_000
MAX_FILES = 20


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git(root: Path, *args: str) -> str:
    return subprocess.run(['git', '-C', str(root), *args], check=True,
                          capture_output=True, text=True, timeout=60).stdout.strip()


def inspect_diff(data: bytes) -> list[str]:
    if not data or len(data) > MAX_DIFF_BYTES or b'\0' in data:
        raise ValueError('patch must be nonempty UTF-8 text within 512000 bytes')
    text = data.decode('utf-8')
    files = []
    # This initial workflow accepts ordinary same-path text edits only.
    for line in text.splitlines():
        if line.startswith(('rename ', 'copy ', 'GIT binary patch', 'Binary files ',
                            'new file mode ', 'deleted file mode ', 'old mode ', 'new mode ')):
            raise ValueError('renames, binary changes, file creation/deletion and mode changes are unsupported')
        if line.startswith('diff --git '):
            match = re.fullmatch(r'diff --git a/([^\s]+) b/([^\s]+)', line)
            if not match or match[1] != match[2]:
                raise ValueError('only unquoted same-path edits are supported')
            name = match[1]
            path = PurePosixPath(name)
            if (path.is_absolute() or '..' in path.parts or '\\' in name
                    or not name.startswith(ALLOWED_PATCH_PREFIXES)):
                raise ValueError('patch path outside approved application scope')
            files.append(name)
        elif line.startswith(('--- ', '+++ ')):
            prefix = 'a/' if line.startswith('--- ') else 'b/'
            if not files or line[4:] != prefix + files[-1]:
                raise ValueError('unified diff headers do not match the declared path')
    if not files or len(files) > MAX_FILES or len(set(files)) != len(files):
        raise ValueError('patch needs 1–20 unique files')
    return files


def prepare(source: Path, workspace: Path, data: bytes, patch_id: str,
            base_revision: str, rationale: str) -> Patch:
    files = inspect_diff(data)
    patch = Patch(patch_id=patch_id, base_revision=base_revision,
                  diff_sha256=sha256(data), affected_files=files, rationale=rationale)
    if workspace.exists():
        raise ValueError('isolated workspace must not already exist')
    # Verify the base exists locally. No fetch, branch switching or upstream edits.
    if git(source, 'rev-parse', base_revision + '^{commit}') != base_revision:
        raise ValueError('base revision must be an exact existing commit')
    workspace.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['git', 'clone', '--local', '--no-hardlinks', '--no-checkout',
                    str(source.resolve()), str(workspace.resolve())], check=True,
                   capture_output=True, timeout=60)
    git(workspace, 'checkout', '--detach', base_revision)
    for name in files:
        p = workspace / name
        if not p.is_file() or p.is_symlink() or any(parent.is_symlink() for parent in p.parents):
            raise ValueError('only existing regular application files may be patched')
        if not p.resolve().is_relative_to(workspace.resolve()):
            raise ValueError('patch path escapes isolated workspace')
    diff_file = workspace / '.git' / 'reviewed.patch'
    diff_file.write_bytes(data)
    git(workspace, 'apply', '--check', str(diff_file.resolve()))
    git(workspace, 'apply', str(diff_file.resolve()))
    changed = git(workspace, 'diff', '--name-only').splitlines()
    if sorted(changed) != sorted(files):
        raise ValueError('applied changes differ from the reviewed file list')
    git(workspace, 'diff', '--check')
    (workspace / '.git' / 'reviewed-source.sha256').write_text(
        sha256(git(workspace, 'diff', '--binary', 'HEAD').encode()) + '\n')
    return patch


def structural_checks(workspace: Path, patch: Patch) -> dict:
    """Bounded non-executing checks; these never count as application tests."""
    reviewed = workspace / '.git' / 'reviewed.patch'
    if sha256(reviewed.read_bytes()) != patch.diff_sha256:
        raise ValueError('isolated patch bytes changed')
    if git(workspace, 'rev-parse', 'HEAD') != patch.base_revision:
        raise ValueError('isolated base revision changed')
    expected = workspace / '.git' / 'reviewed-source.sha256'
    actual = sha256(git(workspace, 'diff', '--binary', 'HEAD').encode())
    if not expected.exists() or expected.read_text().strip() != actual:
        raise ValueError('prepared application source changed after review')
    if git(workspace, 'ls-files', '--others', '--exclude-standard'):
        raise ValueError('untracked application input is not reviewed')
    git(workspace, 'diff', '--check')
    return {'source_matches_review': True, 'whitespace': True,
            'application_tests': 'not_run', 'deployment': 'blocked'}
