"""
knowledge/settings.py — Structured knowledge about Odoo settings and configurations.
"""

KNOWN_SETTINGS = {
    # ── Invoicing / Accounting settings ──────────────────────────────────────
    "automatic_post": {
        "description": "Automatically post draft invoices when they are validated",
        "module": "account",
        "path": "Invoicing -> Settings -> Automatic Post"
    },
    "fiscal_localization": {
        "description": "Chart of accounts template based on company country",
        "module": "account",
        "path": "Accounting -> Settings -> Fiscal Localization",
        "technical_name": "chart_template_id"
    },
    "currency": {
        "description": "Primary company accounting currency settings",
        "module": "base",
        "path": "General Settings -> Companies -> Currency",
        "technical_name": "currency_id"
    },

    # ── Inventory / Stock settings ──────────────────────────────────────────
    "inventory_valuation": {
        "description": "Set stock valuation to Automated or Manual",
        "module": "stock",
        "path": "Inventory -> Configuration -> Product Categories -> Inventory Valuation"
    },
    "multi_warehouses": {
        "description": "Enable managing multiple warehouses",
        "module": "stock",
        "path": "Inventory -> Settings -> Warehouses -> Storage Locations"
    }
}

def get_setting_info(setting_name: str) -> dict:
    """Returns setting metadata dictionary or empty dictionary if not found."""
    cleaned = setting_name.lower().strip().replace(" ", "_")
    return KNOWN_SETTINGS.get(cleaned, KNOWN_SETTINGS.get(setting_name.lower(), {}))
