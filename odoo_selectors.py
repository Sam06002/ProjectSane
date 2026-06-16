"""
odoo_selectors.py — Centralized Selector Registry for Project Sane.
Guarantees deterministic element queries, avoiding fuzzy/AI logic matching.
"""
from typing import List

SELECTORS = {
    # ── Gateway / Duplication Selectors ──────────────────────────────────────
    "reason_input": [
        "input[name='reason']",
        "textarea[name='reason']",
        "input[id='reason']",
        "textarea[id='reason']",
        "input[placeholder*='reason']",
        "textarea[placeholder*='reason']",
        "[name='reason']"
    ],
    "submit_button": [
        "button[type='submit']",
        "input[type='submit']",
        "button:has-text('Submit')",
        "button:has-text('Login')",
        "button:has-text('Confirm')",
        "button:has-text('Ok')"
    ],
    "db_link": [
        "xpath=//*[contains(text(), 'Current database')]/a",
        "xpath=//div[contains(., 'Current database')]/a",
        "div:has-text('Current database') a",
        "a:has-text('database')",
        "a:has-text('enter')",
        "a:has-text('connect')",
        ".o_database_link",
        "a[href*='/web']"
    ],
    "duplicate_button": [
        "button:has-text('Duplicate')",
        "a:has-text('Duplicate')",
        "button:has-text('Neutralize')",
        "a:has-text('Neutralize')",
        "button:has-text('Create a copy')",
        "a:has-text('Create a copy')",
        "button:has-text('Copy')",
        ".o_btn_duplicate",
        "button[name='duplicate']",
        "input[type='submit'][value*='Duplicate']",
        "input[type='submit'][value*='Neutralize']"
    ],
    "modal_confirm": [
        ".modal-dialog button:has-text('Ok')",
        ".modal-dialog button:has-text('Confirm')",
        ".modal-dialog button:has-text('Yes')"
    ],

    # ── Portal Transition Selectors ──────────────────────────────────────────
    "arrow_toggle": [
        "a.o_frontend_to_backend",
        ".o_frontend_to_backend",
        "a[title*='Backend']",
        "a[title*='Edit']",
        "a[href*='/web']"
    ],
    "grid_toggle": [
        "a[title='Go to your Odoo Apps']",
        "[title='Go to your Odoo Apps']",
        "a.o_app_drawer_toggle",
        ".o_app_drawer_toggle",
        ".o_menu_toggle",
        "a:has-text('Go to your Odoo Apps')"
    ],

    # ── Neutralization Indicators ────────────────────────────────────────────
    "neutral_banner": [
        ".o_neutralize_banner",
        ".database_neutralized",
        ".o_test_mode_banner",
        ".o_ribbon:has-text('Neutralized')",
        ".o_ribbon:has-text('Test')"
    ],

    # ── Core Action Selectors ───────────────────────────────────────────────
    "save_button": [
        "button:has-text('Save')",
        ".o_form_button_save",
        "button.o_form_button_save"
    ],
    "discard_button": [
        "button:has-text('Discard')",
        ".o_form_button_cancel"
    ],
    "create_button": [
        "button:has-text('Create')",
        "button:has-text('New')",
        ".o_list_button_add",
        ".o_form_button_create"
    ],
    "edit_button": [
        "button:has-text('Edit')",
        ".o_form_button_edit"
    ],
    "search_input": [
        ".o_searchview_input",
        "input[placeholder='Search...']",
        ".o_setting_search input",
        "input[type='search']",
        ".o_cp_searchview input"
    ],
    "clear_search": [
        ".o_searchview_facet .o_delete",
        ".o_cp_searchview .o_facet_remove"
    ],
    "list_first_row": [
        ".o_list_view .o_data_row:first-child",
        "tr.o_data_row:first-child"
    ],
    "chatter_log_note": [
        "button:has-text('Log note')",
        ".o_chatter_button_log_note"
    ],
    "chatter_send_message": [
        "button:has-text('Send message')",
        ".o_chatter_button_new_message"
    ],
    "debug_toggle": [
        ".o_debug_manager .o_dropdown_toggler",
        ".o_debug_manager button",
        "a[href*='debug']"
    ]
}

def get_selector(name: str) -> List[str]:
    """
    Returns the list of Playwright selector patterns for a given logical name.
    If the name is not in the registry, returns a list containing the name itself
    as a plain text locator fallback.
    """
    return SELECTORS.get(name, [name])
