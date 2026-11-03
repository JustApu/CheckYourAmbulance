"""Start-method probe for the test network guard (issue #53); run by test_network_guard.

`python -m cya.tests.network_guard_probe <start method>` installs the guard in its own main
process, as OfflineTestRunner does, then asks one pool worker started by that method whether it
is guarded: first a plain worker, then one using the parallel suite's guard installer. It prints
the answers as JSON. A worker reports its lookups only when guarded, so an unguarded worker never
attempts the network.
"""

import json
import multiprocessing
import socket
import sys

# TEST-NET-1 (RFC 5737): never routed.
UNROUTABLE = ("192.0.2.1", 9)


def guard_state():
    from cya.testing.network import NetworkAccessBlocked, guard_active

    state = {"active": guard_active()}
    if not state["active"]:
        return state
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        attempts = {
            "lookup": lambda: socket.getaddrinfo("example.invalid", 443),
            "connect": lambda: sock.connect(UNROUTABLE),
        }
        for name, attempt in attempts.items():
            try:
                attempt()
                state[name] = "not blocked"
            except NetworkAccessBlocked:
                state[name] = "blocked"
            except OSError as error:
                state[name] = f"not blocked ({type(error).__name__})"
    return state


def main(method):
    from cya.testing.network import NetworkGuard
    from cya.testing.runner import install_worker_guard

    NetworkGuard().install()
    context = multiprocessing.get_context(method)
    with context.Pool(1) as pool:
        plain = pool.apply(guard_state)
    with context.Pool(1, initializer=install_worker_guard) as pool:
        guarded = pool.apply(guard_state)
    print(json.dumps({"plain": plain, "guarded": guarded}))


if __name__ == "__main__":
    from cya.tests.network_guard_probe import main as probe_main

    probe_main(sys.argv[1])
