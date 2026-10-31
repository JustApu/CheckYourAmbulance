from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from django.core.management import call_command
from django.db import DatabaseError, connection, connections, transaction
from django.test import RequestFactory, TransactionTestCase

from cya.accounts.models import Organization, User
from cya.authorization.locking import locked_identities
from cya.authorization.requests import authorized_request
from cya.authorization.roles import grant_initial_emt


class IdentityLockTests(TransactionTestCase):
    def tearDown(self):
        # Django's normal TransactionTestCase cleanup issues DELETE, which our
        # attribution trigger correctly refuses. The test schema owner may
        # TRUNCATE its disposable database; runtime grants must forbid DDL.
        call_command("flush", verbosity=0, interactive=False, reset_sequences=True)
        super().tearDown()

    def setUp(self):
        self.organization = Organization.objects.create(name="Example Agency", station_zone="UTC")
        self.user = User.objects.create(
            organization=self.organization,
            idp_issuer="https://identity.example.test",
            idp_subject="member-subject",
            email="member@example.test",
            display_name="Example Member",
        )

    def contender(self, user_id):
        # Django connections are thread-local: this is a separate real session.
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET SESSION innodb_lock_wait_timeout = 1")
            try:
                with locked_identities(self.organization.pk, [user_id]):
                    return "acquired"
            except DatabaseError as error:
                return error.args[0]
        finally:
            connections.close_all()

    def test_same_organization_lock_serializes_real_connections(self):
        other = User.objects.create(
            organization=self.organization,
            idp_issuer=self.user.idp_issuer,
            idp_subject="another-subject",
            email="another@example.test",
            display_name="Another Member",
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            with locked_identities(self.organization.pk, [self.user.pk]):
                # Different target users ensure this proves the organization
                # anchor, not merely contention on a shared user row.
                contender = executor.submit(self.contender, other.pk)
                # 1205 is MariaDB's lock-wait timeout, not a mocked ORM result.
                self.assertEqual(contender.result(timeout=10), 1205)
            self.assertEqual(
                executor.submit(self.contender, other.pk).result(timeout=10), "acquired"
            )

    def test_identity_locks_are_returned_in_stable_id_order(self):
        other = User.objects.create(
            organization=self.organization,
            idp_issuer=self.user.idp_issuer,
            idp_subject="another-subject",
            email="another@example.test",
            display_name="Another Member",
        )
        ids = [self.user.pk, other.pk]
        with locked_identities(self.organization.pk, reversed(ids)) as (_, users):
            self.assertEqual(list(users), sorted(ids))

    def guarded_contender(self, user):
        request = RequestFactory().post("/synthetic/protected/")
        request.cya_user = user
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET SESSION innodb_lock_wait_timeout = 1")
            try:
                with authorized_request(request, self.organization.pk, "record_check"):
                    return "entered"
            except DatabaseError as error:
                return error.args[0]
        finally:
            connections.close_all()

    def test_different_users_can_enter_guarded_bodies_concurrently(self):
        other = User.objects.create(
            organization=self.organization,
            idp_issuer=self.user.idp_issuer,
            idp_subject="concurrent-member",
            email="other@example.test",
        )
        for user in (self.user, other):
            with transaction.atomic():
                grant_initial_emt(user=user, command_id=str(uuid4()))
        request = RequestFactory().post("/synthetic/protected/")
        request.cya_user = self.user
        with ThreadPoolExecutor(max_workers=1) as executor:
            with authorized_request(request, self.organization.pk, "record_check"):
                # The first body remains open until the second has entered and
                # exited its own real transaction. No elapsed-time heuristic.
                self.assertEqual(
                    executor.submit(self.guarded_contender, other).result(timeout=10), "entered"
                )

    def test_same_user_guard_still_holds_its_identity_lock_until_exit(self):
        with transaction.atomic():
            grant_initial_emt(user=self.user, command_id=str(uuid4()))
        request = RequestFactory().post("/synthetic/protected/")
        request.cya_user = self.user
        with ThreadPoolExecutor(max_workers=1) as executor:
            with authorized_request(request, self.organization.pk, "record_check"):
                self.assertEqual(
                    executor.submit(self.guarded_contender, self.user).result(timeout=10), 1205
                )
            self.assertEqual(
                executor.submit(self.guarded_contender, self.user).result(timeout=10), "entered"
            )
