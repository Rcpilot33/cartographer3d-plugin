# Checked-in fallback for K2 source/symlink installs (no wheel build).
# Build tooling may replace this file. The local suffix identifies the
# upstream snapshot, not an official Cartographer release number.

__all__ = [
    "__version__",
    "__version_tuple__",
    "version",
    "version_tuple",
    "__commit_id__",
    "commit_id",
]

TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Tuple, Union

    VERSION_TUPLE = Tuple[Union[int, str], ...]
    COMMIT_ID = Union[str, None]
else:
    VERSION_TUPLE = object
    COMMIT_ID = object

version: str
__version__: str
__version_tuple__: VERSION_TUPLE
version_tuple: VERSION_TUPLE
commit_id: COMMIT_ID
__commit_id__: COMMIT_ID

__version__ = version = "1.5.0+k2.upstream.8c3b0cc"
__version_tuple__ = version_tuple = (1, 5, 0, "k2.upstream.8c3b0cc")

__commit_id__ = commit_id = None
