package Spike::Check;
# THROWAWAY spike (issue 15 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/15)): the check-recording algorithm of SPEC 7.3-7.4 (REC-4, CON-3, CON-5).
# One transaction: take the per-(vehicle,item) row lock, replay-check the operation id, compare the
# client's basis with the newest check, then insert. Distinct operations are never discarded.
use strict;
use warnings;
use Exporter 'import';
our @EXPORT_OK = qw(record_check);

# Returns { status => 'COMMITTED'|'REPLAY'|'CONFLICT', check_id => N, newest_id => N|undef }
sub record_check {
    my ($dbh, %a) = @_;
    for my $k (qw(operation_id vehicle_id item_id outcome actor_user_id)) {
        die "missing $k\n" unless defined $a{$k};
    }
    # InnoDB may abort one transaction on a deadlock (error 1213). The transaction is fully rolled
    # back, nothing was committed, and the operation id makes a retry idempotent, so a bounded
    # retry is safe (unlike retrying an ambiguous transport failure).
    my $attempt = 0;
    while (1) {
        my $r = eval { _once($dbh, %a) };
        return $r if $r;
        my $err = $@;
        my $code = $dbh->err // 0;
        eval { $dbh->rollback } if !$dbh->{AutoCommit};
        die $err unless $code == 1213 && ++$attempt < 6;
    }
}

sub _once {
    my ($dbh, %a) = @_;
    $dbh->begin_work;
    # Lock rows are created when the checklist version is published (SPIKE: by the seed). Creating
    # them lazily here with INSERT-then-lock deadlocks under concurrency (observed in this spike).
    my ($locked) = $dbh->selectrow_array('SELECT 1 FROM item_lock WHERE vehicle_id=? AND item_id=? FOR UPDATE', undef, @a{qw(vehicle_id item_id)});
    die "no item_lock row for vehicle $a{vehicle_id} item $a{item_id}\n" unless $locked;

    my ($existing) = $dbh->selectrow_array('SELECT check_id FROM check_record WHERE operation_id=?', undef, $a{operation_id});
    if (defined $existing) { $dbh->commit; return { status => 'REPLAY', check_id => $existing }; }

    my ($newest, $newest_actor) = $dbh->selectrow_array(
        'SELECT check_id, actor_user_id FROM check_record WHERE vehicle_id=? AND item_id=? ORDER BY check_id DESC LIMIT 1',
        undef, @a{qw(vehicle_id item_id)});
    if (defined $newest && $newest_actor != $a{actor_user_id}) {
        my $seen = defined $a{acknowledges_check_id} ? $a{acknowledges_check_id}
                 : defined $a{basis_check_id}        ? $a{basis_check_id} : -1;
        # A conflict warning commits nothing and leaves the operation id unused (CON-3).
        if ($seen != $newest) { $dbh->rollback; return { status => 'CONFLICT', newest_id => $newest }; }
    }
    $dbh->do(q{INSERT INTO check_record
        (operation_id, vehicle_id, item_id, outcome, explanation, actor_user_id, basis_check_id, acknowledges_check_id)
        VALUES (?,?,?,?,?,?,?,?)}, undef,
        @a{qw(operation_id vehicle_id item_id outcome explanation actor_user_id basis_check_id acknowledges_check_id)});
    my $id = $dbh->last_insert_id(undef, undef, 'check_record', 'check_id');
    $dbh->do('INSERT INTO audit_event (actor_id, action, target, detail) VALUES (?,?,?,?)', undef,
        $a{actor_user_id}, 'CHECK_RECORDED', "check:$id", "op=$a{operation_id}");
    $dbh->commit;
    return { status => 'COMMITTED', check_id => $id };
}

1;
