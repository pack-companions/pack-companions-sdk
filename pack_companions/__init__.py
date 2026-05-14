"""Pack Companions SDK — Python client for the Pack Companions service."""

from pack_companions.client import (
    Comment,
    CommentResult,
    CompanionsAuthError,
    CompanionsClient,
)
from pack_companions.hashing import hash_email
from pack_companions.snapshot import (
    CompanionSnapshot,
    SnapshotActivity,
    SnapshotCourse,
    SnapshotHistory,
    SnapshotLocation,
    SnapshotProblem,
    SnapshotUser,
)

__version__ = "0.1.0"
__all__ = [
    "CompanionsClient",
    "CompanionsAuthError",
    "Comment",
    "CommentResult",
    "CompanionSnapshot",
    "SnapshotUser",
    "SnapshotLocation",
    "SnapshotActivity",
    "SnapshotProblem",
    "SnapshotCourse",
    "SnapshotHistory",
    "hash_email",
]
