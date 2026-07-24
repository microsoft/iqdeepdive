"""Create a Fabric lakehouse containing deterministic web analytics data."""

import csv
import hashlib
import io
import os
from datetime import date, timedelta
from pathlib import Path

from azure.identity import AzureDeveloperCliCredential
from azure.storage.filedatalake import DataLakeServiceClient
from dotenv import load_dotenv, set_key
from microsoft_fabric_api import FabricClient
from microsoft_fabric_api.generated.lakehouse.models import (
    CreateLakehouseRequest,
    Csv,
    LoadTableRequest,
)

REPO_ROOT = Path(__file__).parents[1]
ENV_PATH = REPO_ROOT / ".env"
ONELAKE_DFS_URL = "https://onelake.dfs.fabric.microsoft.com"

load_dotenv(ENV_PATH, override=True)

FABRIC_TENANT_ID = os.getenv("FABRIC_TENANT_ID", "").strip()
FABRIC_WORKSPACE_ID = os.getenv("FABRIC_WORKSPACE_ID", "").strip()
LAKEHOUSE_NAME = os.getenv(
    "FABRIC_WEB_ANALYTICS_LAKEHOUSE_NAME", "ContosoWebAnalyticsLakehouse"
)
FABRIC_PORTAL_BASE_URL = os.getenv(
    "FABRIC_PORTAL_BASE_URL", "https://msit.powerbi.com"
).rstrip("/")

CHANNELS = (
    (1, "Organic Search", "Search"),
    (2, "Paid Search", "Search"),
    (3, "Email", "Owned"),
    (4, "Social", "Social"),
    (5, "Referral", "Referral"),
    (6, "Direct", "Direct"),
)
DEVICES = (
    (1, "Desktop", "Web"),
    (2, "Mobile", "Web"),
    (3, "Tablet", "Web"),
)
GEOGRAPHIES = (
    (1, "United States", "Washington", "North America"),
    (2, "United States", "California", "North America"),
    (3, "United States", "Texas", "North America"),
    (4, "Canada", "British Columbia", "North America"),
    (5, "United Kingdom", "England", "Europe"),
    (6, "Germany", "Bavaria", "Europe"),
)
PAGES = (
    (1, "/", "Home", "Discovery"),
    (2, "/products", "Products", "Discovery"),
    (3, "/products/power-tools", "Power Tools", "Product"),
    (4, "/products/hand-tools", "Hand Tools", "Product"),
    (5, "/guides/project-planning", "Project Planning", "Content"),
    (6, "/guides/tool-safety", "Tool Safety", "Content"),
    (7, "/offers", "Offers", "Campaign"),
    (8, "/cart", "Cart", "Conversion"),
    (9, "/checkout", "Checkout", "Conversion"),
    (10, "/confirmation", "Order Confirmation", "Conversion"),
)


def require(value: str, name: str) -> str:
    """Return a required setting or fail with a useful message."""
    if not value:
        raise RuntimeError(f"{name} is required to create web analytics assets.")
    return value


def stable_int(*values: object, modulo: int) -> int:
    """Return a deterministic integer derived from the supplied values."""
    text = "|".join(str(value) for value in values)
    digest = hashlib.sha256(text.encode()).digest()
    return int.from_bytes(digest[:8], "big") % modulo


def generate_dates(start: date, day_count: int) -> list[dict]:
    """Generate a date dimension."""
    rows = []
    for offset in range(day_count):
        current = start + timedelta(days=offset)
        rows.append(
            {
                "date_key": int(current.strftime("%Y%m%d")),
                "date": current.isoformat(),
                "year": current.year,
                "quarter": f"Q{((current.month - 1) // 3) + 1}",
                "month_number": current.month,
                "month_name": current.strftime("%B"),
                "week_start": (current - timedelta(days=current.weekday())).isoformat(),
                "day_name": current.strftime("%A"),
                "is_weekend": int(current.weekday() >= 5),
            }
        )
    return rows


def generate_traffic(dates: list[dict]) -> tuple[list[dict], list[dict]]:
    """Generate session- and page-view-grain facts with stable traffic patterns."""
    sessions = []
    page_views = []
    page_ids = [row[0] for row in PAGES]
    visitor_pool_size = 1800

    for date_row in dates:
        date_key = date_row["date_key"]
        daily_sessions = 34 + stable_int(date_key, "daily-sessions", modulo=21)
        if date_row["is_weekend"]:
            daily_sessions -= 7

        for daily_index in range(daily_sessions):
            session_id = f"S-{date_key}-{daily_index:04d}"
            visitor_id = (
                f"V-{stable_int(session_id, 'visitor', modulo=visitor_pool_size):05d}"
            )
            channel_key = 1 + stable_int(session_id, "channel", modulo=len(CHANNELS))
            device_key = 1 + stable_int(session_id, "device", modulo=len(DEVICES))
            geography_key = 1 + stable_int(
                session_id, "geography", modulo=len(GEOGRAPHIES)
            )
            landing_page_key = page_ids[
                stable_int(session_id, "landing-page", modulo=7)
            ]

            conversion_threshold = {
                1: 10,
                2: 15,
                3: 19,
                4: 7,
                5: 9,
                6: 12,
            }[channel_key]
            converted = int(
                stable_int(session_id, "converted", modulo=100) < conversion_threshold
            )
            bounced = stable_int(session_id, "bounced", modulo=100) < (
                43 if device_key == 2 else 29
            )
            page_view_count = 1 if bounced else 2 + stable_int(
                session_id, "page-count", modulo=5
            )
            if converted:
                page_view_count = max(page_view_count, 4)
            duration_seconds = 18 + stable_int(
                session_id, "duration", modulo=max(45, page_view_count * 115)
            )
            revenue = (
                round(24 + stable_int(session_id, "revenue", modulo=22700) / 100, 2)
                if converted
                else 0.0
            )

            sessions.append(
                {
                    "session_id": session_id,
                    "visitor_id": visitor_id,
                    "date_key": date_key,
                    "channel_key": channel_key,
                    "device_key": device_key,
                    "geography_key": geography_key,
                    "landing_page_key": landing_page_key,
                    "session_duration_seconds": duration_seconds,
                    "page_view_count": page_view_count,
                    "bounced": int(bounced),
                    "converted": converted,
                    "revenue": revenue,
                }
            )

            path = [landing_page_key]
            while len(path) < page_view_count:
                if converted and len(path) >= page_view_count - 3:
                    path.append((8, 9, 10)[len(path) - (page_view_count - 3)])
                else:
                    path.append(
                        page_ids[
                            stable_int(
                                session_id,
                                "page",
                                len(path),
                                modulo=7,
                            )
                        ]
                    )

            for sequence, page_key in enumerate(path, start=1):
                page_views.append(
                    {
                        "page_view_id": f"PV-{session_id}-{sequence:02d}",
                        "session_id": session_id,
                        "date_key": date_key,
                        "page_key": page_key,
                        "channel_key": channel_key,
                        "device_key": device_key,
                        "geography_key": geography_key,
                        "view_sequence": sequence,
                        "time_on_page_seconds": 8
                        + stable_int(session_id, sequence, "time", modulo=150),
                        "is_exit": int(sequence == page_view_count),
                    }
                )

    return sessions, page_views


def dimension_rows() -> dict[str, list[dict]]:
    """Return the static analytical dimensions as row dictionaries."""
    return {
        "dim_channels": [
            {"channel_key": key, "channel_name": name, "channel_group": group}
            for key, name, group in CHANNELS
        ],
        "dim_devices": [
            {"device_key": key, "device_name": name, "device_category": category}
            for key, name, category in DEVICES
        ],
        "dim_geographies": [
            {
                "geography_key": key,
                "country": country,
                "region": region,
                "market": market,
            }
            for key, country, region, market in GEOGRAPHIES
        ],
        "dim_pages": [
            {
                "page_key": key,
                "page_path": path,
                "page_name": name,
                "page_category": category,
            }
            for key, path, name, category in PAGES
        ],
    }


def validate_dataset(tables: dict[str, list[dict]]) -> None:
    """Validate keys, grains, and relationship endpoints before upload."""
    sessions = tables["fact_sessions"]
    page_views = tables["fact_page_views"]
    session_ids = {row["session_id"] for row in sessions}
    page_view_ids = {row["page_view_id"] for row in page_views}
    if len(session_ids) != len(sessions):
        raise ValueError("Session IDs must be unique.")
    if len(page_view_ids) != len(page_views):
        raise ValueError("Page-view IDs must be unique.")
    if not all(row["session_id"] in session_ids for row in page_views):
        raise ValueError("Every page view must reference a known session.")
    expected_views = {row["session_id"]: row["page_view_count"] for row in sessions}
    actual_views: dict[str, int] = {}
    for row in page_views:
        actual_views[row["session_id"]] = actual_views.get(row["session_id"], 0) + 1
    if expected_views != actual_views:
        raise ValueError("Session page-view counts do not match page-view facts.")


def to_csv_bytes(rows: list[dict]) -> bytes:
    """Serialize rows to UTF-8 CSV."""
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode()


def upload_table(
    credential: AzureDeveloperCliCredential,
    client: FabricClient,
    workspace_id: str,
    lakehouse_id: str,
    table_name: str,
    rows: list[dict],
) -> None:
    """Upload a generated CSV and overwrite its Delta table."""
    filename = f"{table_name}.csv"
    filesystem = DataLakeServiceClient(
        account_url=ONELAKE_DFS_URL,
        credential=credential,
    ).get_file_system_client(workspace_id)
    file_client = filesystem.get_directory_client(
        f"{lakehouse_id}/Files/web-analytics"
    ).get_file_client(filename)
    payload = to_csv_bytes(rows)
    print(f"Uploading {table_name} ({len(rows):,} rows)...")
    file_client.upload_data(payload, overwrite=True)
    client.lakehouse.tables.begin_load_table(
        workspace_id,
        lakehouse_id,
        table_name,
        LoadTableRequest(
            relative_path=f"Files/web-analytics/{filename}",
            path_type="File",
            mode="Overwrite",
            format_options=Csv(header=True, delimiter=","),
        ),
    ).result()


def find_lakehouse(client: FabricClient, workspace_id: str):
    """Find the configured lakehouse by display name."""
    return next(
        (
            item
            for item in client.lakehouse.items.list_lakehouses(workspace_id)
            if item.display_name == LAKEHOUSE_NAME
        ),
        None,
    )


def main() -> None:
    """Create or update the web analytics lakehouse and persist its identifiers."""
    tenant_id = require(FABRIC_TENANT_ID, "FABRIC_TENANT_ID")
    workspace_id = require(FABRIC_WORKSPACE_ID, "FABRIC_WORKSPACE_ID")
    credential = AzureDeveloperCliCredential(tenant_id=tenant_id)
    try:
        client = FabricClient(credential)
        lakehouse = find_lakehouse(client, workspace_id)
        if lakehouse is None:
            print(f"Creating Fabric lakehouse '{LAKEHOUSE_NAME}'...")
            lakehouse = client.lakehouse.items.create_lakehouse(
                workspace_id,
                CreateLakehouseRequest(
                    display_name=LAKEHOUSE_NAME,
                    description="Deterministic website traffic analytics data.",
                ),
            )
        else:
            print(f"Reusing Fabric lakehouse '{LAKEHOUSE_NAME}'.")

        dates = generate_dates(date(2026, 1, 1), 181)
        sessions, page_views = generate_traffic(dates)
        tables = {
            "dim_dates": dates,
            **dimension_rows(),
            "fact_sessions": sessions,
            "fact_page_views": page_views,
        }
        validate_dataset(tables)
        for table_name, rows in tables.items():
            upload_table(
                credential,
                client,
                workspace_id,
                lakehouse.id,
                table_name,
                rows,
            )
    finally:
        credential.close()

    lakehouse_url = (
        f"{FABRIC_PORTAL_BASE_URL}/groups/{workspace_id}/lakehouses/{lakehouse.id}"
        "?experience=fabric-developer"
    )
    ENV_PATH.touch()
    for key, value in {
        "FABRIC_WEB_ANALYTICS_LAKEHOUSE_ID": lakehouse.id,
        "FABRIC_WEB_ANALYTICS_LAKEHOUSE_NAME": LAKEHOUSE_NAME,
        "FABRIC_WEB_ANALYTICS_LAKEHOUSE_UI_URL": lakehouse_url,
    }.items():
        set_key(ENV_PATH, key, value, quote_mode="never")

    print(f"Web analytics lakehouse: {LAKEHOUSE_NAME} ({lakehouse.id})")
    print(f"Fabric UI: {lakehouse_url}")


if __name__ == "__main__":
    main()