"""
knowledge/issues.py — Version-specific and module-specific Odoo bugs or structural changes.
"""

# Dict structured as: module -> dict mapping versions to known differences/issues
KNOWN_ISSUES = {
    "account": {
        "15.0": [
            "Invoice post action is synchronous and blocks the user thread on heavy models.",
            "Fiscal localization must be configured before installing any sub-ledger modules."
        ],
        "16.0": [
            "Invoicing journal changes require clearing the sequence cache via base debug options.",
            "Draft invoices require explicit tax recomputation if added via API lines."
        ],
        "17.0": [
            "Tax groups have been moved to tax categories; legacy tax templates will throw serialization errors.",
            "Settings layout changed: Invoicing is nested strictly under the 'Accounting' section."
        ]
    },
    "stock": {
        "16.0": [
            "Inventory valuation automated postings use the general journal instead of a stock-specific journal if not defined.",
        ],
        "17.0": [
            "Standard price valuation layers require active FIFO configuration on the product category level."
        ]
    },
    "website": {
        "17.0": [
            "Website pages lists are governed by website.page actions. Direct navigation to /odoo/website/pages is deprecated and redirects to frontpage."
        ]
    }
}

def get_version_issues(module: str, version: str) -> list:
    """Returns a list of known issues or version differences for the module and version."""
    module_data = KNOWN_ISSUES.get(module.lower(), {})
    if not module_data:
        return []
    
    # Return version-specific, or fall back to version match prefix
    version_key = str(version)
    if version_key in module_data:
        return module_data[version_key]
    
    for key, issues in module_data.items():
        if version_key.startswith(key) or key.startswith(version_key):
            return issues
            
    return []
