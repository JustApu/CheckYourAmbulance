"""URL configuration. Further routes are added by the feature issues."""

from django.urls import path

from cya.accounts import views as accounts
from cya.attention import api as attention_api
from cya.authorization import views as authorization
from cya.checks import api as check_api
from cya.conditions import api as condition_api
from cya.deficiencies import api as deficiency_api
from cya.inspections import api as inspection_api
from cya.inspections import views as inspection_views
from cya.inspections import workflow_views
from cya.notifications import api as notifications_api
from cya.odometer import api as odometer_api
from cya.operations import api as operations_api
from cya.operations import views as operations_views
from cya.shell import views as shell

urlpatterns = [
    path("", shell.home, name="home"),
    path("fleet/", shell.fleet, name="fleet"),
    path("inspection/", inspection_views.vehicles, name="inspection"),
    path(
        "inspection/vehicles/<str:vehicle_id>/",
        inspection_views.vehicle,
        name="inspection-vehicle",
    ),
    path(
        "inspection/sessions/<str:session_id>/",
        inspection_views.session,
        name="inspection-session",
    ),
    path("deficiencies/", workflow_views.deficiencies, name="deficiencies"),
    path(
        "deficiencies/<str:deficiency_id>/verification/",
        workflow_views.verification,
        name="deficiency-verification",
    ),
    path(
        "deficiencies/<str:deficiency_id>/verification-items/",
        workflow_views.verification_items,
        name="deficiency-verification-items",
    ),
    path(
        "deficiencies/<str:deficiency_id>/", workflow_views.deficiency, name="deficiencies-detail"
    ),
    path(
        "inspection/vehicles/<str:vehicle_id>/deficiencies/new/",
        workflow_views.report,
        name="inspection-deficiency-report",
    ),
    path(
        "inspection/vehicles/<str:vehicle_id>/check-history/",
        workflow_views.checks,
        name="inspection-check-history",
    ),
    path("status/", operations_views.vehicles, name="status"),
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
    path("api/v1/checks", check_api.checks, name="api-checks"),
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
