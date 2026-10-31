"""Stack-independent audit schema enforcement helpers (issue 23 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/23)).

Generated SQL is for a schema-owner migration, never the runtime account.
The runtime's effective grants must be verified against MariaDB itself.
"""

from __future__ import annotations

APPEND_ONLY = frozenset(
    """role_grant vehicle_status_event status_request checklist_publication requirement_binding requirement_version obligation obligation_selection obligation_determination determination_check session_participant session_event roster_snapshot roster_member session_roster_correction odometer_entry engine_hours_entry reading_event odometer_obligation odometer_obligation_link odometer_threshold check_record check_event check_obligation_link shared_satisfaction_rule_member deficiency_affected_item deficiency_event deficiency_evidence deficiency_link deficiency_age_rule deficiency_acknowledgement_rule verification_scope_row deficiency_scope_expansion check_suspension kit_seal_event kit_assignment_event kit_inspection kit_inspection_item seal_substitution seal_substitution_obligation command_receipt audit_event notification_outbox notification_delivery_event notification_read attachment""".split()
)
VERSIONED = frozenset(
    """compartment vehicle_system checklist_version checklist_item_def vehicle_override_version vehicle_override_item schedule_rule conditional_rule na_rule shared_satisfaction_rule stocking_deviation deficiency_category deficiency_severity""".split()
)
MUTABLE = frozenset(
    """organization user user_preference vehicle kit_instance checklist_template item inspection_session deficiency notification_pref notification_policy item_lock kit_lock vehicle_lock deficiency_lock session_projection deficiency_projection""".split()
)

# M classification includes immutable creation headers as well as updateable rows.
# Session and deficiency state live in their projections, so the runtime cannot UPDATE a header.
INSERT_ONLY_HEADERS = frozenset({"inspection_session", "deficiency"})

# M headers also protect id and organization_id, and any creation facts.
IMMUTABLE_HEADERS = {
    "user": ("idp_issuer", "idp_subject"),
    "inspection_session": ("vehicle_id", "checklist_version_id", "opened_at"),
    "deficiency": (
        "operation_id",
        "reported_at",
        "reporter_id",
        "category_version_id",
        "severity_version_id",
        "summary",
        "description",
        "vehicle_id",
        "item_id",
        "kit_id",
        "compartment_id",
    ),
    "user_preference": ("user_id",),
    "item_lock": ("vehicle_id", "item_id"),
    "kit_lock": ("kit_id",),
    "vehicle_lock": ("vehicle_id",),
    "deficiency_lock": ("deficiency_id",),
}


def quote_identifier(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or not value[0].isascii()
        or not value[0].isalpha()
        or not all(c.isascii() and (c.isalnum() or c == "_") for c in value)
    ):
        raise ValueError("unsafe SQL identifier")
    return "`" + value + "`"


def append_only_triggers(table: str) -> tuple[str, str]:
    """Defense-in-depth UPDATE/DELETE rejection for a known A table."""
    if table not in APPEND_ONLY:
        raise ValueError("not an append-only table")
    return tuple(
        f"CREATE TRIGGER {quote_identifier('cya_' + table + '_' + suffix)} BEFORE {operation} ON {quote_identifier(table)} FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'append-only table'"
        for operation, suffix in (("UPDATE", "bu"), ("DELETE", "bd"))
    )


def immutable_header_guard(table: str) -> str:
    """Use MariaDB null-safe equality: NULL-to-value must also be blocked."""
    if table not in IMMUTABLE_HEADERS:
        raise ValueError("header immutability definition missing")
    columns = ("id", "organization_id") + IMMUTABLE_HEADERS[table]
    changes = " OR ".join(
        f"NOT (BINARY OLD.{quote_identifier(c)} <=> BINARY NEW.{quote_identifier(c)})"
        for c in columns
    )
    return (
        f"CREATE TRIGGER {quote_identifier('cya_' + table + '_immutable_bu')} "
        f"BEFORE UPDATE ON {quote_identifier(table)} FOR EACH ROW "
        f"BEGIN IF {changes} THEN SIGNAL SQLSTATE '45000' "
        f"SET MESSAGE_TEXT = 'immutable header fields'; END IF; END"
    )


def validate_manifest(physical_tables: set[str]) -> tuple[set[str], set[str]]:
    """Return (unclassified tables, declared-but-absent tables)."""
    declared = APPEND_ONLY | VERSIONED | MUTABLE
    return physical_tables - declared, declared - physical_tables
