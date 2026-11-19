"""Support mailbox agent (issue #546, flywheel Loop C signal source, P0 observe-only)."""

from .classifier import ROUTE_TABLE, MailClassifier
from .digest import DigestBuilder
from .poller import MailboxPoller
from .service import MailboxAgent
from .store import FeedbackStore

__all__ = ["FeedbackStore", "MailboxPoller", "MailClassifier", "ROUTE_TABLE", "DigestBuilder", "MailboxAgent"]
