"""ORM model registration entry point.

Importing this module ensures all ORM models are registered with
``Base.metadata`` so Alembic autogenerate detects every table.

The actual ORM classes have moved to entity-specific subpackages:
- ``deerflow.persistence.thread_meta``
- ``deerflow.persistence.run``
- ``deerflow.persistence.feedback``
- ``deerflow.persistence.user``
- ``deerflow.persistence.orgs`` (tenant control-plane tables, PR-020A)
- ``deerflow.persistence.iam`` (IAM control-plane tables, PR-020B)
- ``deerflow.persistence.audit`` (append-only audit evidence, PR-040)

``RunEventRow`` remains in ``deerflow.persistence.models.run_event`` because
its storage implementation lives in ``deerflow.runtime.events.store.db`` and
there is no matching entity directory.
"""

from deerflow.persistence.audit.model import AuditEventRow
from deerflow.persistence.channel_connections.model import (
    ChannelConnectionRow,
    ChannelConversationRow,
    ChannelCredentialRow,
    ChannelOAuthStateRow,
)
from deerflow.persistence.feedback.model import FeedbackRow
from deerflow.persistence.iam.model import (
    ApiKeyRow,
    OidcGroupMappingRow,
    RoleBindingRow,
    RoleRow,
    ServiceAccountRow,
)
from deerflow.persistence.models.run_event import RunEventRow
from deerflow.persistence.orgs.model import (
    ExternalIdentityRow,
    OrganizationRow,
    OrgMembershipRow,
    WorkspaceRow,
)
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.persistence.user.model import UserRow

__all__ = [
    "ApiKeyRow",
    "AuditEventRow",
    "ChannelConnectionRow",
    "ChannelConversationRow",
    "ChannelCredentialRow",
    "ChannelOAuthStateRow",
    "ExternalIdentityRow",
    "FeedbackRow",
    "OidcGroupMappingRow",
    "OrgMembershipRow",
    "OrganizationRow",
    "RoleBindingRow",
    "RoleRow",
    "RunEventRow",
    "RunRow",
    "ServiceAccountRow",
    "ThreadMetaRow",
    "UserRow",
    "WorkspaceRow",
]
