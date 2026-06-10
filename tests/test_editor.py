from __future__ import annotations

import sys

from bem.tui.screens.compose import launch_editor

# An "editor" that appends a line to the file it is given — exercises the
# shlex path because the command has arguments.
APPENDING_EDITOR = (
    f'{sys.executable} -c '
    '"import sys, pathlib; '
    "p = pathlib.Path(sys.argv[1]); "
    "p.write_text(p.read_text() + chr(10) + 'edited')\""
)

NOOP_EDITOR = f'{sys.executable} -c "pass"'
FAILING_EDITOR = f'{sys.executable} -c "import sys; sys.exit(1)"'


def test_editor_command_with_arguments_is_split():
    result = launch_editor("To: x\n\nbody", APPENDING_EDITOR)
    assert result is not None
    assert result.endswith("edited")


def test_unchanged_file_treated_as_cancel():
    assert launch_editor("To: x\n\nbody", NOOP_EDITOR) is None


def test_nonzero_exit_treated_as_cancel():
    assert launch_editor("To: x\n\nbody", FAILING_EDITOR) is None
