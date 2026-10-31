"""Post-commit wake-up for delivery; best effort, because the outbox row is the durable intent.

enqueue registers one robust on_commit callback per command. Dispatchers are plain callables
taking the organization id (for example one that starts the bounded delivery job); with none
registered the periodic `deliver_notifications` command picks the work up. A failing dispatcher
is logged by class only and never turns a committed command into a failure.
"""

import logging

logger = logging.getLogger(__name__)
_DISPATCHERS = []


def register_dispatcher(dispatcher):
    if dispatcher not in _DISPATCHERS:
        _DISPATCHERS.append(dispatcher)


def clear_dispatchers():
    _DISPATCHERS.clear()


def wake(organization_id):
    for dispatcher in tuple(_DISPATCHERS):
        try:
            dispatcher(organization_id)
        except Exception as error:
            logger.error("notification dispatcher failed (%s)", type(error).__name__)
