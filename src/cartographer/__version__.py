# Checked-in fallback for source/symlink installs. Build tooling writes its
# generated version to the ignored _version.py module so the checkout stays
# clean after local builds and editable installs.

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

try:
    from cartographer._version import (  # pyright: ignore[reportMissingImports]
        __commit_id__,
        __version__,
        __version_tuple__,
        commit_id,
        version,
        version_tuple,
    )
except ImportError:
    __version__ = version = "1.10.1b1+k2.1"
    __version_tuple__ = version_tuple = (1, 10, 1, "b1", "k2", 1)
    __commit_id__ = commit_id = None
