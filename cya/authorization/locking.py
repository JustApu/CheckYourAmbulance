"""Identity lock order for role and active-state changes."""

from contextlib import contextmanager

from django.db import transaction

from cya.accounts.models import Organization, User


@contextmanager
def locked_identities(
    organization_id, user_ids, *, lock_organization=True, share_organization=False
):
    """Lock requested users in ID order, optionally organization first.

    All role/active-state changes must retain the default organization lock to
    serialize last-admin checks. Ordinary requests pass lock_organization=False
    and only read the organization; different users can then work concurrently.
    share_organization (with lock_organization=False) takes the organization row
    in share mode first, in the same lock order: such callers run concurrently
    with each other but wait for, and are waited for by, an exclusive holder (a
    session open against a change of the resume limit, SPEC 6.3 SES-8).
    Current roles are read only after user locks. A user-only caller must not
    later acquire the organization lock or execute role/active-state mutations.
    Missing or cross-organization identities fail before yielding. No user is
    created, updated, authenticated or granted a role by this helper.
    """
    ids = sorted(set(user_ids))
    if not ids or len(ids) > 2:
        raise ValueError("Lock exactly the actor and/or target identities.")
    if share_organization and lock_organization:
        raise ValueError("Take the organization lock in one mode.")
    with transaction.atomic():
        if share_organization:
            with transaction.get_connection().cursor() as cursor:
                cursor.execute(
                    "SELECT id FROM organization WHERE id=%s LOCK IN SHARE MODE",
                    [str(organization_id)],
                )
        organizations = Organization.objects.all()
        if lock_organization:
            organizations = organizations.select_for_update()
        organization = organizations.get(pk=organization_id)
        users = list(
            User.objects.select_for_update()
            .filter(organization_id=organization_id, pk__in=ids)
            .order_by("pk")
        )
        if len(users) != len(ids):
            raise User.DoesNotExist("Required identity is unavailable.")
        yield organization, {user.pk: user for user in users}
