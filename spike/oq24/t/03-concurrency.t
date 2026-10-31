#!/usr/bin/env perl
# THROWAWAY spike (issue 15 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/15)) criterion 2: concurrent check submissions (SPEC CON-3, CON-5, REC-4,
# AC-31). Replays collapse to one record; distinct operations are never lost.
use strict; use warnings;
use Test::More;
use POSIX ':sys_wait_h';
use lib 'spike/oq24/lib';
use Spike::DB qw(admin_dbh app_dbh load_schema new_db_name);
use Spike::Seed qw(seed lock_rows);
use Spike::Check qw(record_check);

my $db = new_db_name();
load_schema($db);
my $admin = admin_dbh($db);
my $N = 8;
my $s = seed($admin, $N);

# Run $code->($index) in $count forked children; each returns a string through a pipe.
sub fan_out {
    my ($count, $code) = @_;
    my @kids;
    for my $i (0 .. $count - 1) {
        pipe(my $r, my $w) or die;
        my $pid = fork() // die;
        if ($pid == 0) {
            close $r;
            my $out = eval { $code->($i) } // "ERROR: $@";
            print {$w} $out; close $w; POSIX::_exit(0);
        }
        close $w; push @kids, [$pid, $r];
    }
    my @res;
    for my $k (@kids) { my $fh = $k->[1]; local $/; push @res, scalar <$fh>; waitpid $k->[0], 0; }
    return @res;
}
my $opid = sub { sprintf 'b%07d-0000-0000-0000-%012d', $_[0], $_[1] };

# 1. Replay: the same operation id submitted concurrently 8 times yields exactly one record.
my @r1 = fan_out($N, sub {
    my $dbh = app_dbh($db);
    my $r = record_check($dbh, operation_id => $opid->(1,1), vehicle_id => 1, item_id => 1, outcome => 'PASSED', actor_user_id => $s->{users}[0]);
    return "$r->{status}:$r->{check_id}";
});
my %seen = map { $_ => 1 } @r1;
my ($cnt) = $admin->selectrow_array(q{SELECT COUNT(*) FROM check_record WHERE vehicle_id=1 AND item_id=1});
is($cnt, 1, 'concurrent replay of one operation id created exactly one record');
my %ids = map { (split /:/)[1] => 1 } @r1;
is(scalar(keys %ids), 1, '  every replay got the same check_id back');
is(scalar(grep { /^COMMITTED/ } @r1), 1, '  exactly one call reports COMMITTED, the rest REPLAY');
is(scalar(grep { /^ERROR/ } @r1), 0, '  no call errored') or diag join "\n", @r1;

# 2. Lost response: client never saw the answer and retries with the same id.
my $dbh = app_dbh($db);
my $first = record_check($dbh, operation_id => $opid->(2,1), vehicle_id => 1, item_id => 2, outcome => 'PASSED', actor_user_id => $s->{users}[0]);
my $retry = record_check($dbh, operation_id => $opid->(2,1), vehicle_id => 1, item_id => 2, outcome => 'PASSED', actor_user_id => $s->{users}[0]);
is($retry->{status}, 'REPLAY', 'retry after a lost response is a replay');
is($retry->{check_id}, $first->{check_id}, '  and returns the originally committed record');

# 3. Distinct concurrent operations on a fresh item (item 2 has user1's check; use basis = none
#    so every other user is told about the newer check, then acknowledges). Nothing may be lost.
$admin->do(q{INSERT INTO items (name) VALUES ('Synthetic AED')});   # item 3
lock_rows($admin);
my $item = 3;
my @r3 = fan_out($N, sub {
    my $i = shift;
    my $dbh = app_dbh($db);
    my $user = $s->{users}[$i];
    my $op = $opid->(3, $i);
    my $r = record_check($dbh, operation_id => $op, vehicle_id => 1, item_id => $item, outcome => ($i % 2 ? 'FAILED' : 'PASSED'), actor_user_id => $user);
    my @trace = ($r->{status});
    my $tries = 0;
    while ($r->{status} eq 'CONFLICT' && $tries++ < 50) {
        # The user sees the warning, proceeds: a NEW operation id is not required because the
        # warning committed nothing and left $op unused (CON-3); acknowledge the newer check.
        $r = record_check($dbh, operation_id => $op, vehicle_id => 1, item_id => $item, outcome => ($i % 2 ? 'FAILED' : 'PASSED'),
            actor_user_id => $user, acknowledges_check_id => $r->{newest_id});
        push @trace, $r->{status};
    }
    return join(',', @trace) . ":$r->{check_id}";
});
my ($n3) = $admin->selectrow_array(q{SELECT COUNT(*) FROM check_record WHERE vehicle_id=1 AND item_id=?}, undef, $item);
is($n3, $N, "all $N distinct concurrent operations were recorded (none lost)");
my ($actors) = $admin->selectrow_array(q{SELECT COUNT(DISTINCT actor_user_id) FROM check_record WHERE vehicle_id=1 AND item_id=?}, undef, $item);
is($actors, $N, '  each with its own attributed actor');
my ($fails) = $admin->selectrow_array(q{SELECT COUNT(*) FROM check_record WHERE vehicle_id=1 AND item_id=? AND outcome='FAILED'}, undef, $item);
is($fails, $N / 2, '  and every FAILED result survived alongside the PASSED ones');
ok(scalar(grep { /CONFLICT/ } @r3) >= $N - 1, '  at least N-1 callers were shown a conflict warning first') or diag join "\n", @r3;
ok(!scalar(grep { /^ERROR/ } @r3), '  no worker errored') or diag join "\n", @r3;
my ($dups) = $admin->selectrow_array(q{SELECT COUNT(*) - COUNT(DISTINCT operation_id) FROM check_record});
is($dups, 0, 'no duplicate operation ids anywhere');
my ($audit) = $admin->selectrow_array(q{SELECT COUNT(*) FROM audit_event WHERE action='CHECK_RECORDED'});
my ($all)   = $admin->selectrow_array(q{SELECT COUNT(*) FROM check_record});
is($audit, $all, 'one audit event per committed check, written in the same transaction');

# 4. A conflict warning leaves the operation id reusable.
my $a = app_dbh($db); my $b = app_dbh($db);
$admin->do(q{INSERT INTO items (name) VALUES ('Synthetic glucometer')});  # item 4
lock_rows($admin);
record_check($a, operation_id => $opid->(4,1), vehicle_id => 1, item_id => 4, outcome => 'PASSED', actor_user_id => $s->{users}[0]);
my $warn = record_check($b, operation_id => $opid->(4,2), vehicle_id => 1, item_id => 4, outcome => 'FAILED', actor_user_id => $s->{users}[1], basis_check_id => undef);
is($warn->{status}, 'CONFLICT', 'second user without the newest basis gets a conflict warning');
my ($used) = $admin->selectrow_array(q{SELECT COUNT(*) FROM check_record WHERE operation_id=?}, undef, $opid->(4,2));
is($used, 0, '  and the warning committed nothing (operation id still unused)');
# A further check lands between warning and acknowledgment.
record_check($a, operation_id => $opid->(4,3), vehicle_id => 1, item_id => 4, outcome => 'PASSED', actor_user_id => $s->{users}[2], acknowledges_check_id => $warn->{newest_id});
my $stale = record_check($b, operation_id => $opid->(4,2), vehicle_id => 1, item_id => 4, outcome => 'FAILED', actor_user_id => $s->{users}[1], acknowledges_check_id => $warn->{newest_id});
is($stale->{status}, 'CONFLICT', 'acknowledging a stale warning (a newer check landed) returns a fresh warning, commits nothing');
isnt($stale->{newest_id}, $warn->{newest_id}, '  naming the newer check');
my $ok = record_check($b, operation_id => $opid->(4,2), vehicle_id => 1, item_id => 4, outcome => 'FAILED', actor_user_id => $s->{users}[1], acknowledges_check_id => $stale->{newest_id});
is($ok->{status}, 'COMMITTED', 'acknowledging the fresh warning commits the independent record');
my ($n4) = $admin->selectrow_array(q{SELECT COUNT(*) FROM check_record WHERE item_id=4});
is($n4, 3, 'item 4 now has three independent records');

my ($iso) = eval { $admin->selectrow_array('SELECT @@transaction_isolation') } // $admin->selectrow_array('SELECT @@tx_isolation');
diag "isolation level used (server default, not changed): $iso";
$admin->do("DROP DATABASE `$db`");
done_testing;
