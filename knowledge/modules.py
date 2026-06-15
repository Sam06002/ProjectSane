"""
knowledge/modules.py — Structured Odoo Module Metadata.
"""

MODULES = {
    "crm": {
        "name": "CRM",
        "technical_name": "crm",
        "menu_xmlid": "crm.crm_menu_root",
        "models": ["crm.lead", "crm.stage", "crm.tag"],
        "description": "Customer Relationship Management & Pipeline Tracking"
    },
    "sale": {
        "name": "Sales",
        "technical_name": "sale",
        "menu_xmlid": "sale.menu_sale_config",
        "models": ["sale.order", "sale.order.line"],
        "description": "Quotations, Sales Orders, and Invoicing Triggers"
    },
    "account": {
        "name": "Invoicing",
        "technical_name": "account",
        "menu_xmlid": "account.menu_finance",
        "models": ["account.move", "account.journal", "account.payment"],
        "description": "Invoicing, Journals, Payments, and General Ledger"
    },
    "stock": {
        "name": "Inventory",
        "technical_name": "stock",
        "menu_xmlid": "stock.menu_stock_root",
        "models": ["stock.picking", "stock.move", "stock.warehouse", "stock.valuation.layer"],
        "description": "Inventory management, Warehouses, Shipments, and Valuation"
    },
    "purchase": {
        "name": "Purchase",
        "technical_name": "purchase",
        "menu_xmlid": "purchase.menu_purchase_root",
        "models": ["purchase.order", "purchase.order.line"],
        "description": "Purchase Orders, Requisitions, and Vendor Bills"
    },
    "contacts": {
        "name": "Contacts",
        "technical_name": "contacts",
        "menu_xmlid": "contacts.menu_contacts",
        "models": ["res.partner"],
        "description": "Address Book & Contact Management"
    },
    "website": {
        "name": "Website",
        "technical_name": "website",
        "menu_xmlid": "website.menu_website_configuration",
        "models": ["website", "website.page"],
        "description": "Website CMS, Blogs, Pages, and E-commerce portal"
    }
}

def get_module_metadata(name: str) -> dict:
    """Returns metadata for the requested module or an empty dictionary."""
    name_lower = name.lower()
    return MODULES.get(name_lower, {})
