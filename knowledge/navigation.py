"""
knowledge/navigation.py — Centralized Odoo Navigation Paths.
"""

NAVIGATION_PATHS = {
    # ── Top-level application paths ──────────────────────────────────────────
    "contacts": "/odoo/contacts",
    "contact": "/odoo/contacts",
    "crm": "/odoo/crm",
    "sales": "/odoo/sales",
    "sale": "/odoo/sales",
    "purchase": "/odoo/purchase",
    "inventory": "/odoo/inventory",
    "stock": "/odoo/inventory",
    "accounting": "/odoo/accounting",
    "invoicing": "/odoo/accounting",
    "account": "/odoo/accounting",
    "website": "/odoo/website",
    "settings": "/odoo/settings",
    "companies": "/odoo/companies",
    "users": "/odoo/users",
    "discuss": "/odoo/discuss",
    "calendar": "/odoo/calendar",
    "apps": "/odoo/action-base.open_module_tree",

    # ── Specialized view paths ───────────────────────────────────────────────
    "website_pages": "/odoo/action-website.action_website_pages_list",
    "pages": "/odoo/action-website.action_website_pages_list",
    "general_settings": "/web#action=base_setup.action_general_configuration",
    "modules": "/odoo/action-base.open_module_tree"
}

def get_navigation_path(feature: str) -> str:
    """
    Returns the typical Odoo navigation path suffix for the given feature name.
    Falls back to '/odoo' if the feature is not registered.
    """
    cleaned = feature.lower().strip().replace(" ", "_")
    return NAVIGATION_PATHS.get(cleaned, NAVIGATION_PATHS.get(feature.lower(), "/odoo"))
