"""Identity lock order for role and active-state changes."""

from contextlib import contextmanager

from django.db import transaction

from cya.accounts.models import Organization, User


@contextmanager
def locked_identities(organization_id, user_ids, *, lock_organization=True):
    """Lock requested users in ID order, optionally organization first.

    All role/active-state changes must retain the default organization lock to
    serialize last-admin checks. Ordinary requests pass lock_organization=False
    and only read the organization; different users can then work concurrently.
    Current roles are read only after user locks. A user-only caller must not
    later acquire the organization lock or execute role/active-state mutations.
    Missing or cross-organization identities fail before yielding. No user is
    created, updated, authenticated or granted a role by this helper.
    """
    ids = sorted(set(user_ids))
    if not ids or len(ids) > 2:
        raise ValueError("Lock exactly the actor and/or target identities.")
    with transaction.atomic():
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
