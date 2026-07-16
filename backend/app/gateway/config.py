import os

from pydantic import BaseModel, Field

# Default Organization used by the single-Org bootstrap resolver (PR-013).
# During migration there is exactly one Organization (ADR-0001 §6); this is the
# migration org until multi-org is opened (PR-025). Overridable per-deployment
# via DEER_FLOW_DEFAULT_ORG_ID so dev/staging/prod don't share an org id.
DEFAULT_BOOTSTRAP_ORG_ID = "default"

# Display attributes for the default Organization row materialised by PR-022.
# The slug is platform-unique among non-deleted orgs; "default" cannot collide
# with a future second org because multi-org is gated behind PR-025B.
DEFAULT_ORG_SLUG = "default"
DEFAULT_ORG_NAME = "Default Organization"


class GatewayConfig(BaseModel):
    """Configuration for the API Gateway."""

    host: str = Field(default="0.0.0.0", description="Host to bind the gateway server")
    port: int = Field(default=8001, description="Port to bind the gateway server")
    enable_docs: bool = Field(default=True, description="Enable Swagger/ReDoc/OpenAPI endpoints")
    default_org_id: str = Field(
        default=DEFAULT_BOOTSTRAP_ORG_ID,
        description="Bootstrap Organization id for the single-Org tenant resolver (PR-013).",
    )


_gateway_config: GatewayConfig | None = None


def get_gateway_config() -> GatewayConfig:
    """Get gateway config, loading from environment if available."""
    global _gateway_config
    if _gateway_config is None:
        _gateway_config = GatewayConfig(
            host=os.getenv("GATEWAY_HOST", "0.0.0.0"),
            port=int(os.getenv("GATEWAY_PORT", "8001")),
            enable_docs=os.getenv("GATEWAY_ENABLE_DOCS", "true").lower() == "true",
            default_org_id=os.getenv("DEER_FLOW_DEFAULT_ORG_ID", DEFAULT_BOOTSTRAP_ORG_ID),
        )
    return _gateway_config
