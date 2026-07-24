"""Create or update the Power BI web analytics report."""

import base64
import os
from pathlib import Path

from azure.identity import AzureDeveloperCliCredential
from dotenv import load_dotenv, set_key
from microsoft_fabric_api import FabricClient
from microsoft_fabric_api.generated.report.models import (
    CreateReportRequest,
    ReportDefinition,
    ReportDefinitionPart,
    UpdateReportDefinitionRequest,
)

REPO_ROOT = Path(__file__).parents[1]
ENV_PATH = REPO_ROOT / ".env"
DEFINITION_ROOT = REPO_ROOT / "data/reports/web-analytics"

load_dotenv(ENV_PATH, override=True)

FABRIC_TENANT_ID = os.getenv("FABRIC_TENANT_ID", "").strip()
FABRIC_WORKSPACE_ID = os.getenv("FABRIC_WORKSPACE_ID", "").strip()
SEMANTIC_MODEL_ID = os.getenv(
    "FABRIC_WEB_ANALYTICS_SEMANTIC_MODEL_ID", ""
).strip()
REPORT_NAME = os.getenv(
    "FABRIC_WEB_ANALYTICS_REPORT_NAME", "Contoso Web Analytics Dashboard"
)
FABRIC_PORTAL_BASE_URL = os.getenv(
    "FABRIC_PORTAL_BASE_URL", "https://msit.powerbi.com"
).rstrip("/")


def require(value: str, name: str) -> str:
    """Return a required setting or fail with a useful message."""
    if not value:
        raise RuntimeError(f"{name} is required to create the Power BI report.")
    return value


def definition_part(path: Path, semantic_model_id: str) -> ReportDefinitionPart:
    """Encode one source-controlled PBIR definition part."""
    relative_path = path.relative_to(DEFINITION_ROOT).as_posix()
    content = path.read_text().replace("{{SEMANTIC_MODEL_ID}}", semantic_model_id)
    if "{{" in content or "}}" in content:
        raise ValueError(f"Unresolved definition marker in {relative_path}.")
    return ReportDefinitionPart(
        path=relative_path,
        payload=base64.b64encode(content.encode()).decode(),
        payload_type="InlineBase64",
    )


def build_definition(semantic_model_id: str) -> ReportDefinition:
    """Build the enhanced PBIR public definition."""
    source_files = sorted(
        path for path in DEFINITION_ROOT.rglob("*") if path.is_file()
    )
    if not source_files or not (DEFINITION_ROOT / "definition.pbir").exists():
        raise RuntimeError("The web analytics report definition is incomplete.")
    return ReportDefinition(
        format="PBIR",
        parts=[definition_part(path, semantic_model_id) for path in source_files],
    )


def find_report(client: FabricClient, workspace_id: str):
    """Find the configured report by display name."""
    return next(
        (
            item
            for item in client.report.items.list_reports(workspace_id)
            if item.display_name == REPORT_NAME
        ),
        None,
    )


def main() -> None:
    """Create or update the web analytics report and persist its identifiers."""
    tenant_id = require(FABRIC_TENANT_ID, "FABRIC_TENANT_ID")
    workspace_id = require(FABRIC_WORKSPACE_ID, "FABRIC_WORKSPACE_ID")
    semantic_model_id = require(
        SEMANTIC_MODEL_ID, "FABRIC_WEB_ANALYTICS_SEMANTIC_MODEL_ID"
    )
    credential = AzureDeveloperCliCredential(tenant_id=tenant_id)
    try:
        client = FabricClient(credential)
        definition = build_definition(semantic_model_id)
        report = find_report(client, workspace_id)
        if report is None:
            print(f"Creating Power BI report '{REPORT_NAME}'...")
            report = client.report.items.create_report(
                workspace_id,
                CreateReportRequest(
                    display_name=REPORT_NAME,
                    description="Interactive dashboard for website traffic analytics.",
                    definition=definition,
                ),
            )
        else:
            print(f"Updating Power BI report '{REPORT_NAME}'...")
            client.report.items.begin_update_report_definition(
                workspace_id,
                report.id,
                UpdateReportDefinitionRequest(definition=definition),
            ).result()
    finally:
        credential.close()

    report_url = f"{FABRIC_PORTAL_BASE_URL}/groups/{workspace_id}/reports/{report.id}"
    ENV_PATH.touch()
    for key, value in {
        "FABRIC_WEB_ANALYTICS_REPORT_ID": report.id,
        "FABRIC_WEB_ANALYTICS_REPORT_NAME": REPORT_NAME,
        "FABRIC_WEB_ANALYTICS_REPORT_URL": report_url,
    }.items():
        set_key(ENV_PATH, key, value, quote_mode="never")

    print(f"Web analytics report: {REPORT_NAME} ({report.id})")
    print(f"Power BI UI: {report_url}")


if __name__ == "__main__":
    main()