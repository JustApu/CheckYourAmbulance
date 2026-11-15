"""Temporary synthetic lock-contention benchmark for issue #156.

Run only on the isolated test host and remove before the implementation PR is handed off.
"""

import json
import math
import statistics
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest import mock

from django.core.management import call_command
from django.db import connection

from cya.checklists import commands as checklist_commands
from cya.checklists.models import ChecklistTemplate
from cya.checks.commands import record_check
from cya.kits.commands import add_kit_instance
from cya.kits.definitions import (
    create_kit_definition,
    publish_kit_definition_version,
    save_kit_definition_draft,
)
from cya.kits.kit_lists import publish_vehicle_kit_list, save_vehicle_kit_list_draft
from cya.kits.models import KitDefinition, VehicleKitRequirement
from cya.kits.sealable import designate_compartment_sealable, designate_kit_sealable
from cya.kits.tests.support import KitCase
from cya.seals import locking as seal_locking
from cya.seals.commands import break_seal, start_inspection, verify_seal
from cya.seals.reseal import reseal


class Issue156LockBenchmark(KitCase):
    """Measure command and row-lock waits with two independent vehicle actors."""

    iterations = 12

    def setUp(self):
        super().setUp()
        self._benchmark_setup_complete = False
        self.addCleanup(self._flush_failed_setup)
        self.publish_schedule("benchmark-0600")
        self.published_version(self.base_items())
        self.vehicle_b_template = ChecklistTemplate.objects.create(
            organization=self.org,
            label="Synthetic vehicle B checklist",
        )
        version_b = checklist_commands.save_template_draft(
            template_id=str(self.vehicle_b_template.pk),
            items=self.base_items(),
            **self.context(self.supervisor),
        ).body["version_id"]
        checklist_commands.publish_template_version(
            version_id=version_b,
            **self.context(self.supervisor),
        )
        for index, vehicle in enumerate(self.vehicles):
            self.bind(
                vehicle=vehicle,
                template=self.template if index == 0 else self.vehicle_b_template,
                slot="benchmark-0600",
                reconcile=False,
            )

        self.definitions = []
        for index, _vehicle in enumerate(self.vehicles):
            definition_id = create_kit_definition(
                key=f"issue156-benchmark-kit-{index}",
                label=f"Synthetic benchmark kit {index}",
                **self.context(self.supervisor),
            ).body["definition_id"]
            kit_version = save_kit_definition_draft(
                definition_id=definition_id,
                contents=[
                    {
                        "item_id": str(self.kit_item.pk),
                        "display_order": 1,
                        "kind": "QUANTITY",
                        "expected_quantity": 2,
                        "allowed_outcomes": ["PASSED", "FAILED", "UNABLE_TO_INSPECT"],
                    }
                ],
                **self.context(self.supervisor),
            ).body["version_id"]
            publish_kit_definition_version(
                version_id=kit_version,
                **self.context(self.supervisor),
            )
            presence_item = str(
                KitDefinition.objects.get(pk=definition_id).presence_item_id
            )
            self.definitions.append((definition_id, presence_item))

        self.subjects = []
        for index, vehicle in enumerate(self.vehicles):
            compartment = self.cabinet if index == 0 else self.cabinet_b
            definition_id, presence_item = self.definitions[index]
            kit_id = add_kit_instance(
                label=f"Synthetic benchmark kit {index}",
                vehicle_id=str(vehicle.pk),
                compartment_id=str(compartment.pk),
                definition_id=definition_id,
                **self.context(self.supervisor),
            ).body["kit_id"]
            list_version = save_vehicle_kit_list_draft(
                vehicle_id=str(vehicle.pk),
                lines=[
                    {
                        "definition_id": definition_id,
                        "compartment_id": str(compartment.pk),
                        "required_count": 1,
                        "display_order": 1,
                    }
                ],
                **self.context(self.supervisor),
            ).body["version_id"]
            publish_vehicle_kit_list(version_id=list_version, **self.context(self.supervisor))
            line = VehicleKitRequirement.objects.get(version_id=list_version)
            designate_compartment_sealable(
                vehicle_id=str(vehicle.pk),
                compartment_id=str(compartment.pk),
                sealable=True,
                **self.context(self.supervisor),
            )
            designate_kit_sealable(
                kit_id=kit_id, sealable=True, **self.context(self.supervisor)
            )
            # Create the initial intact kit seal using only synthetic, versioned kit contents.
            marking = f"synthetic-{kit_id}-0"
            initial = reseal(
                vehicle_id=str(vehicle.pk),
                kit_id=kit_id,
                marking=marking,
                inspected_attestation=True,
                written_attestation=True,
                observations=[
                    {
                        "item_id": str(self.kit_item.pk),
                        "kit_id": kit_id,
                        "outcome": "PASSED",
                        "count": 2,
                        "expiration": None,
                        "measurement": None,
                    }
                ],
                **self.context(self.crew),
            )
            worker = self.make_user(f"bench-worker-{index}")
            publisher = self.make_user(f"bench-publisher-{index}")
            self.grant(publisher, "SUPERVISOR")
            self.subjects.append(
                {
                    "vehicle": vehicle,
                    "compartment": compartment,
                    "kit_id": kit_id,
                    "line_id": str(line.pk),
                    "presence_item": presence_item,
                    "seal_id": initial.body["event_id"],
                    "marking": marking,
                    "reseal_sequence": 0,
                    "actor": worker,
                    "publisher": publisher,
                }
            )

        # Add a small synthetic organization-wide configuration tail to represent a pilot fleet.
        ChecklistTemplate.objects.bulk_create(
            ChecklistTemplate(
                organization=self.org,
                label=f"Synthetic unused checklist {index}",
            )
            for index in range(8)
        )
        for index in range(8):
            create_kit_definition(
                key=f"issue156-unused-{index}",
                label=f"Synthetic unused kit {index}",
                **self.context(self.supervisor),
            )

        self.samples = defaultdict(list)
        self.lock_samples = []
        self.action = threading.local()
        self.mode = threading.local()
        self.lock_guard = threading.Lock()
        self.current_mode = "setup"
        self.connection_actions = {}
        self.innodb_wait_events = []
        self.active_innodb_waits = {}
        self.monitor_stop = threading.Event()
        self.monitor_error = []
        self._benchmark_setup_complete = True

    def _flush_failed_setup(self):
        if not self._benchmark_setup_complete:
            call_command("flush", verbosity=0, interactive=False, reset_sequences=True)

    def _connection_id(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT CONNECTION_ID()")
            return int(cursor.fetchone()[0])

    def _watch_innodb_waits(self):
        """Sample MariaDB's documented waiter/blocker view during the measured workload."""
        try:
            while not self.monitor_stop.wait(0.02):
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT waiting_lock_id, blocking_lock_id, waiting_pid, "
                        "waiting_query, locked_table, blocking_pid, blocking_query "
                        "FROM sys.innodb_lock_waits"
                    )
                    rows = cursor.fetchall()
                self._record_innodb_wait_snapshot(rows)
        except Exception as error:
            self.monitor_error.append(error)
        finally:
            self._record_innodb_wait_snapshot([])
            connection.close()

    def _record_innodb_wait_snapshot(self, rows):
        now = time.monotonic()
        with self.lock_guard:
            seen = set()
            for (
                waiting_lock_id,
                blocking_lock_id,
                waiting_pid,
                _waiting_query,
                table_name,
                blocking_pid,
                _blocking_query,
            ) in rows:
                waiting_pid, blocking_pid = int(waiting_pid), int(blocking_pid)
                # Lock IDs distinguish successive waits by the same connections and table.
                key = (waiting_lock_id, blocking_lock_id)
                seen.add(key)
                current = self.active_innodb_waits.get(key)
                if current is None:
                    self.active_innodb_waits[key] = {
                        "mode": self.current_mode,
                        "first_seen": now,
                        "last_seen": now,
                        "waiting_action": self.connection_actions.get(waiting_pid),
                        "blocking_action": self.connection_actions.get(blocking_pid),
                        "locked_table": table_name,
                    }
                else:
                    current["last_seen"] = now
            for key in set(self.active_innodb_waits) - seen:
                event = self.active_innodb_waits.pop(key)
                event["observed_s"] = event.pop("last_seen") - event.pop("first_seen")
                self.innodb_wait_events.append(event)

    def _record_lock(self, resource, original, *args, **kwargs):
        started = time.monotonic()
        try:
            return original(*args, **kwargs)
        finally:
            ended = time.monotonic()
            with self.lock_guard:
                self.lock_samples.append(
                    {
                        "action": getattr(self.action, "name", "setup"),
                        "resource": resource,
                        "wait_s": ended - started,
                    }
                )

    def _timed(self, label, callback):
        mode = getattr(self.mode, "name", "setup")
        self.action.name = label
        pid = self._connection_id()
        with self.lock_guard:
            self.connection_actions[pid] = f"{mode}:{label}"
        started = time.monotonic()
        try:
            result = callback()
        finally:
            elapsed = time.monotonic() - started
            self.samples[f"{mode}:{label}"].append(elapsed)
            with self.lock_guard:
                self.connection_actions.pop(pid, None)
            self.action.name = "idle"
        return result

    def _verify(self, subject):
        return self._timed(
            "verify",
            lambda: verify_seal(
                vehicle_id=str(subject["vehicle"].pk),
                kit_id=subject["kit_id"],
                expected_tip_id=subject["seal_id"],
                marking=subject["marking"],
                attested=True,
                **self.context(subject["actor"]),
            ),
        )

    def _reseal(self, subject):
        broken = break_seal(
            vehicle_id=str(subject["vehicle"].pk),
            kit_id=subject["kit_id"],
            expected_tip_id=subject["seal_id"],
            reason="Synthetic benchmark cycle",
            **self.context(subject["actor"]),
        )
        subject["seal_id"] = broken.body["event_id"]
        reseal_sequence = subject["reseal_sequence"] + 1
        marking = f"synthetic-{subject['kit_id']}-{reseal_sequence}"
        result = self._timed(
            "reseal",
            lambda: reseal(
                vehicle_id=str(subject["vehicle"].pk),
                kit_id=subject["kit_id"],
                expected_tip_id=subject["seal_id"],
                marking=marking,
                inspected_attestation=True,
                written_attestation=True,
                observations=[
                    {
                        "item_id": str(self.kit_item.pk),
                        "kit_id": subject["kit_id"],
                        "outcome": "PASSED",
                        "count": 2,
                        "expiration": None,
                        "measurement": None,
                    }
                ],
                **self.context(subject["actor"]),
            ),
        )
        subject["seal_id"] = result.body["event_id"]
        subject["marking"] = marking
        subject["reseal_sequence"] = reseal_sequence
        return result

    def _count(self, subject):
        start = self.context(subject["actor"])
        start_inspection(
            vehicle_id=str(subject["vehicle"].pk),
            slot_id=str(subject["compartment"].stable_slot_id),
            **start,
        )
        return self._timed(
            "standalone-count",
            lambda: record_check(
                vehicle_id=str(subject["vehicle"].pk),
                item_id=subject["presence_item"],
                checklist_version_id=None,
                kit_requirement_id=subject["line_id"],
                inspection_start_operation_id=start["operation_id"],
                quantity=1,
                **self.context(subject["actor"]),
            ),
        )

    def _publish(self, subject):
        return self._timed(
            "template-publication",
            lambda: checklist_commands.publish_template_version(
                version_id=self.publication_version,
                effective_at=self.clock.now + timedelta(days=1),
                **self.context(subject["publisher"]),
            ),
        )

    @staticmethod
    def _summary(values):
        ordered = sorted(values)
        if not ordered:
            return {"n": 0}
        return {
            "n": len(ordered),
            "median_s": statistics.median(ordered),
            "p95_s": ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)],
            "max_s": ordered[-1],
        }

    def test_alone_then_concurrent_with_template_publication(self):
        original_locked_ids = seal_locking._locked_ids
        original_locked = checklist_commands._locked
        original_lock_vehicles = checklist_commands._lock_vehicles

        def timed_locked_ids(query, *args, **kwargs):
            return self._record_lock(
                f"{query.model.__name__}.select_for_update(seal)",
                original_locked_ids,
                query,
                *args,
                **kwargs,
            )

        def timed_locked(model, *args, **kwargs):
            resource = f"{model.__name__}.select_for_update"
            return self._record_lock(resource, original_locked, model, *args, **kwargs)

        def timed_lock_vehicles(*args, **kwargs):
            return self._record_lock(
                "Vehicle.select_for_update(publication)", original_lock_vehicles, *args, **kwargs
            )

        monitor = threading.Thread(target=self._watch_innodb_waits, daemon=True)
        monitor.start()
        with (
            mock.patch.object(seal_locking, "_locked_ids", side_effect=timed_locked_ids),
            mock.patch.object(checklist_commands, "_locked", side_effect=timed_locked),
            mock.patch.object(
                checklist_commands, "_lock_vehicles", side_effect=timed_lock_vehicles
            ),
        ):
            # Baseline with one command thread at a time on each vehicle.
            self.mode.name = "alone"
            self.current_mode = "alone"
            for iteration in range(self.iterations):
                for subject_index, subject in enumerate(self.subjects):
                    print(
                        f"ISSUE156_PROGRESS mode=alone iteration={iteration + 1}/"
                        f"{self.iterations} vehicle={subject_index + 1}",
                        flush=True,
                    )
                    self._verify(subject)
                    self._reseal(subject)
                    self._count(subject)

            publication_start = self.draft(self.base_items(), actor=self.supervisor)
            self.publication_version = publication_start
            barrier = threading.Barrier(len(self.subjects) + 1)

            def worker(subject):
                try:
                    barrier.wait(timeout=20)
                    self.mode.name = "contended"
                    self.current_mode = "contended"
                    for iteration in range(self.iterations):
                        print(
                            f"ISSUE156_PROGRESS mode=contended iteration={iteration + 1}/"
                            f"{self.iterations} vehicle={self.subjects.index(subject) + 1}",
                            flush=True,
                        )
                        self._verify(subject)
                        self._reseal(subject)
                        self._count(subject)
                finally:
                    connection.close()

            def publish():
                try:
                    barrier.wait(timeout=20)
                    self.mode.name = "contended"
                    self.current_mode = "contended"
                    print(
                        "ISSUE156_PROGRESS mode=contended action=template-publication",
                        flush=True,
                    )
                    result = self._publish(self.subjects[0])
                    print("ISSUE156_PROGRESS mode=contended publication=complete", flush=True)
                    return result
                finally:
                    connection.close()

            with ThreadPoolExecutor(max_workers=len(self.subjects) + 1) as pool:
                futures = [pool.submit(worker, subject) for subject in self.subjects]
                publication = pool.submit(publish)
                for future in futures:
                    future.result(timeout=180)
                publication.result(timeout=180)

        self.monitor_stop.set()
        monitor.join(timeout=5)
        self.assertFalse(monitor.is_alive(), "InnoDB lock-wait monitor did not stop")
        self.assertFalse(
            self.monitor_error,
            f"InnoDB lock-wait monitor failed: {self.monitor_error}",
        )
        report = {
            "database_vendor": connection.vendor,
            "vehicles": len(self.subjects),
            "templates": ChecklistTemplate.objects.filter(organization=self.org).count(),
            "kit_definitions": KitDefinition.objects.filter(organization=self.org).count(),
            "iterations_per_vehicle_per_action_per_mode": self.iterations,
            "commands_alone": {
                name.removeprefix("alone:"): self._summary(values)
                for name, values in self.samples.items()
                if name.startswith("alone:")
            },
            "commands_contended_and_publication": {
                name.removeprefix("contended:"): self._summary(values)
                for name, values in self.samples.items()
                if name.startswith("contended:")
            },
            "max_lock_query_elapsed_s": max(
                (sample["wait_s"] for sample in self.lock_samples), default=0
            ),
            "max_lock_query_detail": max(
                self.lock_samples, key=lambda sample: sample["wait_s"], default=None
            ),
            "lock_query_samples": len(self.lock_samples),
            "innodb_lock_wait_event_count": len(self.innodb_wait_events),
            "innodb_lock_waits_by_mode": {
                mode: self._summary(
                    [row["observed_s"] for row in self.innodb_wait_events if row["mode"] == mode]
                )
                for mode in ("alone", "contended")
            },
            "longest_innodb_lock_wait": max(
                self.innodb_wait_events, key=lambda row: row["observed_s"], default=None
            ),
            "innodb_monitor_error": str(self.monitor_error[0]) if self.monitor_error else None,
        }
        print("ISSUE156_BENCHMARK=" + json.dumps(report, sort_keys=True), flush=True)
