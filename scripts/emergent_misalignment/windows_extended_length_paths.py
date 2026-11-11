"""Make long paths work on Windows without any system setting, by using extended-length paths.

Windows caps ordinary paths at 259 characters unless the LongPathsEnabled registry value is 1. A path written with the
`\\\\?\\` prefix (`\\\\?\\D:\\folder\\file`, or `\\\\?\\UNC\\server\\share\\folder\\file` for a network share) bypasses that cap whatever
the registry says. This module adds and removes that prefix. On macOS and Linux every function that takes a real path
returns it unchanged, so the rest of the code can call it unconditionally.

How it is meant to be used:
  * Call `extended_length_path` on every folder or file path an entry point receives (command line arguments and
    environment-provided roots), AFTER any `resolve()` / `os.path.realpath`, because both can strip the prefix on Windows.
    Paths built from a prefixed path with `/`, `.parent`, `.with_name`, `.rglob` and `.iterdir` keep the prefix.
  * Never write a prefixed path into an output file or a message: use `path_without_extended_length_prefix`,
    `remove_extended_length_prefixes_from_text` or `relative_path_text_under_folder`.
  * Never compare a prefixed path with an unprefixed one as text: use `relative_path_text_under_folder`, which ignores the prefix.

The text functions take a `path_module` (default `os.path`) so that they can be tested on any operating system with `ntpath`.
"""

import ntpath
import os
from pathlib import Path

EXTENDED_LENGTH_PREFIX = "\\\\?\\"
EXTENDED_LENGTH_UNC_PREFIX = "\\\\?\\UNC\\"
DEVICE_PATH_PREFIX = "\\\\.\\"


def running_on_windows() -> bool:
    return os.name == "nt"


def add_extended_length_prefix_to_windows_path_text(path_text: str) -> str:
    """The extended-length form of an absolute Windows path (drive or UNC). A path that already has a `\\\\?\\` or `\\\\.\\`
    prefix is returned unchanged. A relative path (including a drive-relative `D:folder` and a drive-less `\\folder`) raises
    ValueError: the prefix turns off Windows' path normalisation, so the path must be made absolute first."""
    if path_text.startswith(EXTENDED_LENGTH_PREFIX) or path_text.startswith(DEVICE_PATH_PREFIX):
        return path_text
    drive, rest = ntpath.splitdrive(path_text)
    is_unc_share = drive.startswith(("\\\\", "//"))
    if not drive or not (is_unc_share or rest.startswith(("\\", "/"))):
        raise ValueError(f"an absolute path is needed to add the extended-length prefix, got {path_text!r}")
    normalised = ntpath.normpath(path_text)  # also turns "/" into "\" and removes "." and ".." parts, which the prefix would not
    if normalised.startswith("\\\\"):
        return EXTENDED_LENGTH_UNC_PREFIX + normalised[2:]
    return EXTENDED_LENGTH_PREFIX + normalised


def remove_extended_length_prefix_from_windows_path_text(path_text: str) -> str:
    """The ordinary form of a path: `\\\\?\\UNC\\server\\share\\x` becomes `\\\\server\\share\\x`, `\\\\?\\D:\\x` becomes `D:\\x`."""
    if path_text.startswith(EXTENDED_LENGTH_UNC_PREFIX):
        return "\\\\" + path_text[len(EXTENDED_LENGTH_UNC_PREFIX):]
    if path_text.startswith(EXTENDED_LENGTH_PREFIX):
        return path_text[len(EXTENDED_LENGTH_PREFIX):]
    return path_text


def remove_extended_length_prefixes_from_text(text: str) -> str:
    """Remove every extended-length prefix inside a longer text (terminal output, log lines, messages)."""
    return text.replace(EXTENDED_LENGTH_UNC_PREFIX, "\\\\").replace(EXTENDED_LENGTH_PREFIX, "")


def extended_length_path(path) -> Path:
    """The path in extended-length form on Windows (a relative path is made absolute from the current folder first, without
    resolving links); the same path, unchanged, on any other operating system."""
    if not running_on_windows():
        return Path(path)
    path_text = str(path)
    if not (path_text.startswith(EXTENDED_LENGTH_PREFIX) or path_text.startswith(DEVICE_PATH_PREFIX)):
        path_text = os.path.abspath(path_text)
    return Path(add_extended_length_prefix_to_windows_path_text(path_text))


def path_without_extended_length_prefix(path) -> Path:
    """The path as an ordinary path, for writing into files and messages; unchanged on any other operating system."""
    if not running_on_windows():
        return Path(path)
    return Path(remove_extended_length_prefix_from_windows_path_text(str(path)))


def relative_path_text_under_folder(path, folder, path_module=os.path):
    """The path relative to the folder as text with the folder's own separators, ignoring any extended-length prefix on either
    and, on Windows, letter case; None when the path is not inside the folder (the folder itself gives '.'). Both must be
    absolute. Nothing is resolved, so a prefixed and an unprefixed form of the same folder always give the same answer."""
    path_text = remove_extended_length_prefix_from_windows_path_text(str(path))
    folder_text = remove_extended_length_prefix_from_windows_path_text(str(folder))
    try:
        common = path_module.commonpath([path_module.normcase(path_module.normpath(path_text)),
                                         path_module.normcase(path_module.normpath(folder_text))])
    except ValueError:  # different drives, or a mix of absolute and relative paths
        return None
    normalised_folder = path_module.normcase(path_module.normpath(folder_text))
    if common != normalised_folder:
        return None
    normalised_path = path_module.normpath(path_text)
    remainder = normalised_path[len(path_module.normpath(folder_text)):].lstrip("\\/")
    return remainder or "."
