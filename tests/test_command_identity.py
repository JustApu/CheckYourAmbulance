import json
import unittest
import uuid

from tools.command_identity import (
    canonical_uuid,
    human_operation_id,
    receipt_identity,
    worker_operation_id,
)


class CommandIdentityTests(unittest.TestCase):
    def test_human_v4_only(self):
        human_operation_id(str(uuid.uuid4()))
        with self.assertRaises(ValueError):
            human_operation_id(str(uuid.uuid5(uuid.NAMESPACE_DNS, "example")))

    def test_canonical_case_and_variant(self):
        value = str(uuid.uuid4())
        with self.assertRaises(ValueError):
            canonical_uuid(value.upper())
        with self.assertRaises(ValueError):
            canonical_uuid("{" + value + "}")
        with self.assertRaises(ValueError):
            canonical_uuid("00000000-0000-0000-0000-000000000000")

    def test_worker_uuid5_is_deterministic_and_key_separated(self):
        org = str(uuid.uuid4())
        key = json.dumps(["vehicle", "2026-10-20"], separators=(",", ":"))
        first = worker_operation_id(org, "scheduler", "generate", key)
        self.assertEqual(first.version, 5)
        self.assertEqual(first, worker_operation_id(org, "scheduler", "generate", key))
        self.assertNotEqual(first, worker_operation_id(org, "scheduler", "reconcile", key))
        self.assertNotEqual(
            first, worker_operation_id(org, "scheduler", "generate", '["vehicle","2026-10-21"]')
        )

    def test_worker_job_key_requires_canonical_json(self):
        org = str(uuid.uuid4())
        for key in ('["vehicle", "date"]', '{"a":1}', "[]", '["é"]'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                worker_operation_id(org, "scheduler", "generate", key)

    def test_human_service_identity_exclusive(self):
        actor = str(uuid.uuid4())
        op = str(uuid.uuid4())
        self.assertEqual(
            receipt_identity(
                operation_id=op, actor_user_id=actor, service_identity=None, job_key=None
            ),
            "human",
        )
        with self.assertRaises(ValueError):
            receipt_identity(
                operation_id=op, actor_user_id=actor, service_identity="worker", job_key="[]"
            )

    def test_service_requires_v5_and_no_actor(self):
        org = str(uuid.uuid4())
        key = '["vehicle"]'
        op = str(worker_operation_id(org, "scheduler", "generate", key))
        self.assertEqual(
            receipt_identity(
                operation_id=op, actor_user_id=None, service_identity="scheduler", job_key=key
            ),
            "worker",
        )
        with self.assertRaises(ValueError):
            receipt_identity(
                operation_id=str(uuid.uuid4()),
                actor_user_id=None,
                service_identity="scheduler",
                job_key=key,
            )


if __name__ == "__main__":
    unittest.main()
