"""Shared fixtures for deficiency tests: a checklist with checks, plus deficiency categories."""

from datetime import timedelta
from uuid import uuid4

from cya.checks.tests.support import CheckCase
from cya.core.models import CommandReceipt
from cya.deficiencies.models import (
    Deficiency,
    DeficiencyAffectedItem,
    DeficiencyCategory,
    DeficiencyEvent,
    DeficiencyEvidence,
    DeficiencyLink,
    DeficiencyProjection,
    DeficiencySeverity,
    VerificationScopeRow,
)
from cya.deficiencies.severity import ensure_default_severities

NEW = object()


class DeficiencyCase(CheckCase):
    def setUp(self):
        super().setUp()
        self.supply = self.make_user("supply")
        self.grant(self.supply, "SUPPLY_OFFICER")
        ensure_default_severities(self.org.pk, self.now - timedelta(days=1))
        self.plain = self.category("Synthetic mechanical", supply_related=False)
        self.stock = self.category("Synthetic stock", supply_related=True)

    def category(self, label, *, supply_related, revision=1, stable=None, when=None):
        return DeficiencyCategory.objects.create(
            organization=self.org,
            stable_category_id=stable or str(uuid4()),
            revision=revision,
            label=label,
            supply_related=supply_related,
            effective_at=when or self.now - timedelta(days=1),
        )

    def level(self, code, revision=1):
        return DeficiencySeverity.objects.get(organization=self.org, code=code, revision=revision)

    def new_fields(self, category=None, **overrides):
        return {
            "category_id": (category or self.plain).stable_category_id,
            "severity": "HIGH",
            "summary": "Synthetic summary",
            "description": "Synthetic description of what is wrong.",
            **overrides,
        }

    def failed(
        self, item=None, *, actor=None, deficiency=NEW, create=None, outcome="FAILED", **fields
    ):
        """Record a Failed check; by default it creates a new deficiency (None sends none)."""
        if deficiency is NEW:
            deficiency = {"create": create or self.new_fields()}
        return self.record(item=item, actor=actor, outcome=outcome, deficiency=deficiency, **fields)

    def counts(self):
        return {
            model.__name__: model.objects.count()
            for model in (
                Deficiency,
                DeficiencyAffectedItem,
                DeficiencyEvent,
                DeficiencyEvidence,
                DeficiencyLink,
                DeficiencyProjection,
                VerificationScopeRow,
                CommandReceipt,
            )
        }
