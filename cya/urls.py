"""URL configuration. Further routes are added by the feature issues."""

from django.urls import path

from cya.accounts import views as accounts
from cya.attention import api as attention_api
from cya.attention import readiness_views
from cya.authorization import views as authorization
from cya.checks import api as check_api
from cya.conditions import api as condition_api
from cya.deficiencies import api as deficiency_api
from cya.inspections import api as inspection_api
from cya.inspections import settings_views as inspection_settings_views
from cya.inspections import views as inspection_views
from cya.inspections import workflow_views
from cya.kits import (
    definition_admin_views,
    inventory_views,
    kit_instance_views,
    kit_list_views,
    position_views,
    vehicle_views,
)
from cya.kits import views as kit_views
from cya.notifications import api as notifications_api
from cya.notifications import relay_views
from cya.odometer import api as odometer_api
from cya.operations import api as operations_api
from cya.operations import (
    binding_admin_views,
    crew_views,
    item_requirement_views,
    override_admin_views,
    template_admin_views,
    vehicle_admin_views,
)
from cya.operations import views as operations_views
from cya.seals import api as seal_api
from cya.seals import views as seal_views
from cya.shell import brand_views
from cya.shell import views as shell

urlpatterns = [
    path("", shell.home, name="home"),
    path("manifest.webmanifest", brand_views.manifest, name="web-manifest"),
    path("icons/<str:name>", brand_views.icon, name="brand-icon"),
    path("fleet/readiness/", readiness_views.screen, name="fleet-readiness"),
    path("fleet/readiness/panel/", readiness_views.panel, name="fleet-readiness-panel"),
    path("inspection/", inspection_views.vehicles, name="inspection"),
    path(
        "inspection/vehicles/<str:vehicle_id>/",
        inspection_views.vehicle,
        name="inspection-vehicle",
    ),
    path(
        "inspection/vehicles/<str:vehicle_id>/seals/",
        seal_views.vehicle_seals,
        name="inspection-vehicle-seals",
    ),
    path(
        "inspection/vehicles/<str:vehicle_id>/print/",
        inspection_views.vehicle_print,
        name="inspection-vehicle-print",
    ),
    path(
        "inspection/kit-reference/",
        inspection_views.kit_reference,
        name="inspection-kit-reference",
    ),
    path(
        "inspection/sessions/<str:session_id>/",
        inspection_views.session,
        name="inspection-session",
    ),
    path("problems/", workflow_views.deficiencies, name="deficiencies"),
    path("problems/queue/", workflow_views.queue, name="deficiency-queue"),
    path(
        "problems/<str:deficiency_id>/verification/",
        workflow_views.verification,
        name="deficiency-verification",
    ),
    path(
        "problems/<str:deficiency_id>/verification-items/",
        workflow_views.verification_items,
        name="deficiency-verification-items",
    ),
    path("problems/<str:deficiency_id>/", workflow_views.deficiency, name="deficiencies-detail"),
    path(
        "inspection/vehicles/<str:vehicle_id>/problems/new/",
        workflow_views.report,
        name="inspection-deficiency-report",
    ),
    path(
        "inspection/vehicles/<str:vehicle_id>/check-history/",
        workflow_views.checks,
        name="inspection-check-history",
    ),
    path("status/", operations_views.vehicles, name="status"),
    path("status/crew/", crew_views.crew, name="vehicle-crew"),
    path(
        "status/vehicles/<str:vehicle_id>/",
        operations_views.vehicle,
        name="status-vehicle",
    ),
    path(
        "status/vehicles/<str:vehicle_id>/panel/",
        operations_views.panel,
        name="status-vehicle-panel",
    ),
    path("administration/", shell.administration, name="administration"),
    path("administration/roles/", authorization.roles, name="administration-roles"),
    path(
        "administration/roles/<uuid:target_id>/history/",
        authorization.role_history,
        name="administration-role-history",
    ),
    path("administration/mail-relay/", relay_views.mail_relay, name="mail-relay"),
    path(
        "administration/crew-assignments/",
        crew_views.limit_settings,
        name="crew-assignment-settings",
    ),
    path(
        "administration/inspection-sessions/",
        inspection_settings_views.resume_limit_settings,
        name="inspection-session-settings",
    ),
    path("administration/kits/", kit_views.library, name="kit-library"),
    path(
        "administration/kits/template.<str:fmt>",
        kit_views.template,
        name="kit-library-template",
    ),
    path("administration/kits/export.<str:fmt>", kit_views.export, name="kit-library-export"),
    path(
        "administration/kit-definitions/",
        definition_admin_views.definitions,
        name="kit-definitions",
    ),
    path(
        "administration/kit-definitions/<uuid:definition_id>/",
        definition_admin_views.definition,
        name="kit-definition",
    ),
    path(
        "administration/kit-definitions/<uuid:definition_id>/items/new/",
        definition_admin_views.definition_content,
        name="kit-definition-content-new",
    ),
    path(
        "administration/kit-definitions/<uuid:definition_id>/items/<uuid:item_id>/",
        definition_admin_views.definition_content,
        name="kit-definition-content",
    ),
    path(
        "administration/kit-definitions/<uuid:definition_id>/duplicate/",
        definition_admin_views.duplicate,
        name="kit-definition-duplicate",
    ),
    path("administration/kit-lists/", kit_list_views.vehicles, name="kit-list-vehicles"),
    path(
        "administration/kit-lists/<uuid:vehicle_id>/",
        kit_list_views.vehicle_kit_list,
        name="vehicle-kit-list",
    ),
    path(
        "administration/kit-instances/",
        inventory_views.inventory,
        name="kit-instance-inventory",
    ),
    path(
        "administration/kit-instances/vehicles/",
        kit_instance_views.vehicles,
        name="kit-instance-vehicles",
    ),
    path(
        "administration/kit-instances/duplicate/",
        kit_instance_views.duplicate,
        name="kit-instance-duplicate",
    ),
    path(
        "administration/kit-instances/<uuid:vehicle_id>/",
        kit_instance_views.vehicle_kit_instances,
        name="vehicle-kit-instances",
    ),
    path("administration/vehicles/", vehicle_admin_views.vehicles, name="vehicle-admin-list"),
    path(
        "administration/vehicles/<uuid:vehicle_id>/",
        vehicle_admin_views.vehicle,
        name="vehicle-admin",
    ),
    path(
        "administration/vehicles/<uuid:vehicle_id>/bindings/",
        binding_admin_views.bindings,
        name="vehicle-bindings",
    ),
    path(
        "administration/vehicles/<uuid:vehicle_id>/duplicate/",
        vehicle_admin_views.duplicate,
        name="vehicle-duplicate",
    ),
    path(
        "administration/vehicles/<uuid:vehicle_id>/item-requirements/",
        item_requirement_views.item_requirements,
        name="vehicle-item-requirements",
    ),
    path("administration/positions/", position_views.vehicles, name="position-vehicles"),
    path(
        "administration/positions/<uuid:vehicle_id>/",
        position_views.vehicle_positions,
        name="vehicle-positions",
    ),
    path(
        "administration/checklists/",
        template_admin_views.templates,
        name="checklist-templates",
    ),
    path(
        "administration/checklists/<uuid:template_id>/",
        template_admin_views.template,
        name="checklist-template",
    ),
    path(
        "administration/checklists/<uuid:template_id>/items/new/",
        template_admin_views.template_item,
        name="checklist-template-item-new",
    ),
    path(
        "administration/checklists/<uuid:template_id>/items/<uuid:item_id>/",
        template_admin_views.template_item,
        name="checklist-template-item",
    ),
    path(
        "administration/checklists/<uuid:template_id>/duplicate/",
        template_admin_views.duplicate,
        name="checklist-template-duplicate",
    ),
    path(
        "administration/checklists/<uuid:template_id>/vehicles/<uuid:vehicle_id>/",
        override_admin_views.override,
        name="checklist-override",
    ),
    path(
        "administration/checklists/<uuid:template_id>/vehicles/<uuid:vehicle_id>/items/new/",
        override_admin_views.override_item,
        name="checklist-override-item-new",
    ),
    path(
        "administration/checklists/<uuid:template_id>/vehicles/<uuid:vehicle_id>/items/"
        "<uuid:item_id>/",
        override_admin_views.override_item,
        name="checklist-override-item",
    ),
    path(
        "administration/vehicle-files/",
        vehicle_views.vehicle_files_page,
        name="vehicle-files",
    ),
    path(
        "administration/vehicle-files/template.<str:fmt>",
        vehicle_views.template,
        name="vehicle-file-template",
    ),
    path(
        "administration/vehicle-files/<uuid:vehicle_id>/export.<str:fmt>",
        vehicle_views.export,
        name="vehicle-file-export",
    ),
    path("api/v1/checks", check_api.checks, name="api-checks"),
    path("api/v1/seals/<str:action>", seal_api.command, name="api-seal-command"),
    path(
        "api/v1/vehicles/<str:vehicle_id>/seals",
        seal_api.vehicle_subjects,
        name="api-seal-subjects",
    ),
    path(
        "api/v1/vehicles/<str:vehicle_id>/seals/<str:kind>/<str:subject_id>",
        seal_api.inspection_scope,
        name="api-seal-scope",
    ),
    path("api/v1/inspection-sessions", inspection_api.sessions, name="api-inspection-sessions"),
    path(
        "api/v1/inspection-sessions/<str:session_id>/reopen",
        inspection_api.reopen,
        name="api-inspection-session-reopen",
    ),
    path(
        "api/v1/inspection-sessions/<str:session_id>/end",
        inspection_api.end,
        name="api-inspection-session-end",
    ),
    path(
        "api/v1/vehicles/<str:vehicle_id>/checklist",
        attention_api.checklist,
        name="api-vehicle-checklist",
    ),
    path(
        "api/v1/me/view-preferences",
        attention_api.view_preferences,
        name="api-view-preferences",
    ),
    path(
        "api/v1/obligations/<str:obligation_id>/unable-completions",
        condition_api.complete_unable,
        name="api-obligation-unable-completions",
    ),
    path(
        "api/v1/obligation-determinations/<str:determination_id>/invalidations",
        condition_api.invalidate_completion,
        name="api-obligation-determination-invalidations",
    ),
    path(
        "api/v1/checks/<str:check_id>/corrections",
        check_api.corrections,
        name="api-check-corrections",
    ),
    path(
        "api/v1/checks/<str:check_id>/invalidations",
        check_api.invalidations,
        name="api-check-invalidations",
    ),
    path("api/v1/fleet-readiness", readiness_views.api, name="api-fleet-readiness"),
    path("api/v1/deficiencies", deficiency_api.deficiencies, name="api-deficiencies"),
    path(
        "api/v1/deficiency-severities",
        deficiency_api.severities,
        name="api-deficiency-severities",
    ),
    path(
        "api/v1/deficiencies/suggestions",
        deficiency_api.suggestions,
        name="api-deficiency-suggestions",
    ),
    path(
        "api/v1/deficiencies/<str:deficiency_id>/links",
        deficiency_api.links,
        name="api-deficiency-links",
    ),
    path(
        "api/v1/deficiencies/<str:deficiency_id>/link-removals",
        deficiency_api.link_removals,
        name="api-deficiency-link-removals",
    ),
    path(
        "api/v1/deficiencies/<str:deficiency_id>/severity-changes",
        deficiency_api.severity_changes,
        name="api-deficiency-severity-changes",
    ),
    path(
        "api/v1/deficiencies/<str:deficiency_id>/severity",
        deficiency_api.severity,
        name="api-deficiency-severity",
    ),
    path(
        "api/v1/deficiencies/<str:deficiency_id>/assignments",
        deficiency_api.assignments,
        name="api-deficiency-assignments",
    ),
    path(
        "api/v1/deficiencies/<str:deficiency_id>/deadlines",
        deficiency_api.deadlines,
        name="api-deficiency-deadlines",
    ),
    path(
        "api/v1/deficiencies/<str:deficiency_id>/work-starts",
        deficiency_api.work_starts,
        name="api-deficiency-work-starts",
    ),
    path(
        "api/v1/deficiencies/<str:deficiency_id>/corrections",
        deficiency_api.corrections,
        name="api-deficiency-corrections",
    ),
    path(
        "api/v1/deficiencies/<str:deficiency_id>/assignment",
        deficiency_api.assignment,
        name="api-deficiency-assignment",
    ),
    path(
        "api/v1/deficiencies/<str:deficiency_id>/dependencies",
        deficiency_api.dependencies,
        name="api-deficiency-dependencies",
    ),
    path(
        "api/v1/deficiencies/<str:deficiency_id>/dependency-revisions",
        deficiency_api.dependency_revisions,
        name="api-deficiency-dependency-revisions",
    ),
    path(
        "api/v1/deficiencies/<str:deficiency_id>/dependency-exits",
        deficiency_api.dependency_exits,
        name="api-deficiency-dependency-exits",
    ),
    path(
        "api/v1/deficiencies/<str:deficiency_id>/dependency",
        deficiency_api.dependency_record,
        name="api-deficiency-dependency",
    ),
    path(
        "api/v1/deficiencies/<str:deficiency_id>/progress-notes",
        deficiency_api.progress_notes,
        name="api-deficiency-progress-notes",
    ),
    path(
        "api/v1/deficiencies/<str:deficiency_id>/extension-requests",
        deficiency_api.extension_requests,
        name="api-deficiency-extension-requests",
    ),
    path(
        "api/v1/deficiencies/<str:deficiency_id>/extension-decisions",
        deficiency_api.extension_decisions,
        name="api-deficiency-extension-decisions",
    ),
    path(
        "api/v1/deficiencies/<str:deficiency_id>/deadline",
        deficiency_api.deadline_record,
        name="api-deficiency-deadline",
    ),
    path(
        "api/v1/deficiencies/<str:deficiency_id>/escalation-acknowledgements",
        deficiency_api.escalation_acknowledgements,
        name="api-deficiency-escalation-acknowledgements",
    ),
    path(
        "api/v1/deficiencies/<str:deficiency_id>/escalation-acknowledgement",
        deficiency_api.escalation_acknowledgement,
        name="api-deficiency-escalation-acknowledgement",
    ),
    path(
        "api/v1/deficiencies/<str:deficiency_id>/administrator-closures",
        deficiency_api.administrator_closures,
        name="api-deficiency-administrator-closures",
    ),
    path(
        "api/v1/deficiencies/<str:deficiency_id>/empty-scope-closures",
        deficiency_api.empty_scope_closures,
        name="api-deficiency-empty-scope-closures",
    ),
    path(
        "api/v1/deficiencies/<str:deficiency_id>/scope-expansions",
        deficiency_api.scope_expansions,
        name="api-deficiency-scope-expansions",
    ),
    path(
        "api/v1/deficiencies/<str:deficiency_id>/check-suspensions",
        deficiency_api.check_suspensions,
        name="api-deficiency-check-suspensions",
    ),
    path(
        "api/v1/deficiencies/<str:deficiency_id>/check-suspension-ends",
        deficiency_api.check_suspension_ends,
        name="api-deficiency-check-suspension-ends",
    ),
    path(
        "api/v1/deficiencies/<str:deficiency_id>/added-verifications",
        deficiency_api.added_verifications,
        name="api-deficiency-added-verifications",
    ),
    path(
        "api/v1/deficiencies/<str:deficiency_id>/administrator-reopenings",
        deficiency_api.administrator_reopenings,
        name="api-deficiency-administrator-reopenings",
    ),
    path(
        "api/v1/deficiency-acknowledgement-maximum",
        deficiency_api.acknowledgement_maximum,
        name="api-deficiency-acknowledgement-maximum",
    ),
    path(
        "api/v1/deficiency-age-rule",
        deficiency_api.age_rule,
        name="api-deficiency-age-rule",
    ),
    path(
        "api/v1/vehicles/<str:vehicle_id>/status",
        operations_api.vehicle_status,
        name="api-vehicle-status",
    ),
    path(
        "api/v1/vehicles/<str:vehicle_id>/ad-hoc-inspections",
        operations_api.ad_hoc_inspections,
        name="api-ad-hoc-inspections",
    ),
    path(
        "api/v1/vehicles/<str:vehicle_id>/status-changes",
        operations_api.status_changes,
        name="api-vehicle-status-changes",
    ),
    path(
        "api/v1/vehicles/<str:vehicle_id>/status-requests",
        operations_api.status_requests,
        name="api-vehicle-status-requests",
    ),
    path(
        "api/v1/status-requests",
        operations_api.pending_status_requests,
        name="api-status-requests",
    ),
    path(
        "api/v1/status-requests/<str:request_id>/decisions",
        operations_api.decisions,
        name="api-status-request-decisions",
    ),
    path("api/v1/odometer-entries", odometer_api.odometer_entries, name="api-odometer-entries"),
    path(
        "api/v1/readings/<str:kind>/<str:reading_id>/corrections",
        odometer_api.reading_corrections,
        name="api-reading-corrections",
    ),
    path(
        "api/v1/readings/<str:kind>/<str:reading_id>/invalidations",
        odometer_api.reading_invalidations,
        name="api-reading-invalidations",
    ),
    path("auth/login/", accounts.sign_in, name="sign-in"),
    path("auth/callback/", accounts.callback, name="sign-in-callback"),
    path("api/v1/notifications", notifications_api.notifications, name="api-notifications"),
    path(
        "api/v1/notifications/<str:notification_id>/reads",
        notifications_api.reads,
        name="api-notification-reads",
    ),
    path(
        "api/v1/notification-preferences",
        notifications_api.preferences,
        name="api-notification-preferences",
    ),
    path("api/v1/notification-policy", notifications_api.policy, name="api-notification-policy"),
    path(
        "api/v1/notification-recipients",
        notifications_api.recipient_rules,
        name="api-notification-recipients",
    ),
    path(
        "api/v1/notification-failures", notifications_api.failures, name="api-notification-failures"
    ),
    path("auth/status/", accounts.status, name="sign-in-status"),
    path("auth/logout/", accounts.sign_out, name="sign-out"),
]
