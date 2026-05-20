from enum import StrEnum


class AppEnv(StrEnum):
    LOCAL = "local"
    PRODUCTION = "production"


class PostStatus(StrEnum):
    NEW = "new"
    VIEWS_TOO_LOW = "views_too_low"
    COMMENTS_CLOSED = "comments_closed"
    FORBIDDEN_TOPIC = "forbidden_topic"
    READY_TO_COMMENT = "ready_to_comment"
    COMMENT_GENERATED = "comment_generated"
    PUBLISHED = "published"
    PUBLISH_FAILED = "publish_failed"
    SKIPPED = "skipped"
    ERROR = "error"


class CommentStatus(StrEnum):
    GENERATED = "generated"
    PUBLISHED = "published"
    FAILED = "failed"


class LogLevel(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
