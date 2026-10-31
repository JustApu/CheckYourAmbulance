"""THROWAWAY spike: Python port of the check-recording algorithm (SPEC 7.3-7.4), raw driver version."""
import MySQLdb


def record_check(conn, operation_id, vehicle_id, item_id, outcome, actor_user_id,
                 explanation=None, basis_check_id=None, acknowledges_check_id=None):
    """Return dict(status=COMMITTED|REPLAY|CONFLICT, check_id=, newest_id=). Retries only on deadlock."""
    for attempt in range(6):
        try:
            return _once(conn, operation_id, vehicle_id, item_id, outcome, actor_user_id,
                         explanation, basis_check_id, acknowledges_check_id)
        except MySQLdb.OperationalError as exc:
            conn.rollback()
            if exc.args[0] != 1213 or attempt == 5:   # deadlock: fully rolled back, safe to retry
                raise
    raise AssertionError("unreachable")


def _once(conn, op, v, i, outcome, actor, explanation, basis, ack):
    conn.autocommit(False)
    cur = conn.cursor()
    try:
        cur.execute("SELECT 1 FROM item_lock WHERE vehicle_id=%s AND item_id=%s FOR UPDATE", (v, i))
        if cur.fetchone() is None:
            raise RuntimeError(f"no item_lock row for vehicle {v} item {i}")
        cur.execute("SELECT check_id FROM check_record WHERE operation_id=%s", (op,))
        row = cur.fetchone()
        if row:
            conn.commit()
            return dict(status="REPLAY", check_id=row[0])
        cur.execute("SELECT check_id, actor_user_id FROM check_record WHERE vehicle_id=%s AND item_id=%s "
                    "ORDER BY check_id DESC LIMIT 1", (v, i))
        newest = cur.fetchone()
        if newest and newest[1] != actor:
            seen = ack if ack is not None else basis if basis is not None else -1
            if seen != newest[0]:
                conn.rollback()                      # a warning commits nothing; the operation id stays unused
                return dict(status="CONFLICT", newest_id=newest[0])
        cur.execute("INSERT INTO check_record (operation_id, vehicle_id, item_id, outcome, explanation, actor_user_id,"
                    " basis_check_id, acknowledges_check_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (op, v, i, outcome, explanation, actor, basis, ack))
        check_id = cur.lastrowid
        cur.execute("INSERT INTO audit_event (actor_id, action, target, detail) VALUES (%s,%s,%s,%s)",
                    (actor, "CHECK_RECORDED", f"check:{check_id}", f"op={op}"))
        conn.commit()
        return dict(status="COMMITTED", check_id=check_id)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.autocommit(True)
        cur.close()
