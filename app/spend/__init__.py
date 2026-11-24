"""Spend-category taxonomy (merchant/budget labels for the pie)."""

from app.spend.categories import (
    SpendCategory,
    TransactionSpendLabel,
    clear_manual_label,
    get_labels_by_transaction_ids,
    list_spend_categories,
    set_manual_label,
    set_spend_label,
)
from app.spend.rules import (
    SpendLabelRule,
    insert_spend_label_rule,
    list_enabled_rules,
    match_payee_rule,
    normalize_payee,
)
from app.spend.suggest import (
    SuggestBatch,
    SuggestionGroup,
    accept_all_rule_suggestions,
    accept_suggestion_group,
    reject_all_vault_suggestions,
    reject_suggestion_group,
    run_suggest,
)

__all__ = [
    "SpendCategory",
    "SpendLabelRule",
    "SuggestBatch",
    "SuggestionGroup",
    "TransactionSpendLabel",
    "accept_all_rule_suggestions",
    "accept_suggestion_group",
    "clear_manual_label",
    "get_labels_by_transaction_ids",
    "insert_spend_label_rule",
    "list_enabled_rules",
    "list_spend_categories",
    "match_payee_rule",
    "normalize_payee",
    "reject_all_vault_suggestions",
    "reject_suggestion_group",
    "run_suggest",
    "set_manual_label",
    "set_spend_label",
]
