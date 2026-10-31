#!/usr/bin/env perl
# THROWAWAY spike (issue 15 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/15)) criterion 5: backup plus point-in-time restore to a chosen instant, then
# rebuild/compare the derived status (SPEC ARC-7, AC-26, AC-27). Uses the server's own dump and
# binary-log tools. Needs SPIKE_BASE (private instance dir), SPIKE_DUMP, SPIKE_BINLOG, SPIKE_CLIENT.
use strict; use warnings;
use Test::More;
use File::Temp qw(tempdir);
use lib 'spike/oq24/lib';
use Spike::DB qw(admin_dbh app_dbh load_schema new_db_name sock);
use Spike::Seed qw(seed lock_rows);
use Spike::Check qw(record_check);

my ($base, $dump, $binlog, $client) = map { $ENV{$_} or plan skip_all => "$_ not set" } qw(SPIKE_BASE SPIKE_DUMP SPIKE_BINLOG SPIKE_CLIENT);
my $sock = sock();

my $db = new_db_name(); load_schema($db);
my $admin = admin_dbh($db); my $s = seed($admin, 3);
my $app = app_dbh($db);
my $n = 0;
sub add { my ($user, $item, $outcome) = @_;
    $n++;
    my $r = record_check($app, operation_id => sprintf('c%07d-0000-0000-0000-000000000000', $n), vehicle_id => 1, item_id => $item,
        outcome => $outcome, actor_user_id => $s->{users}[$user], acknowledges_check_id => undef, basis_check_id => undef);
    # conflicts are expected between different users on the same item; acknowledge to proceed.
    if ($r->{status} eq 'CONFLICT') {
        $r = record_check($app, operation_id => sprintf('c%07d-0000-0000-0000-000000000000', $n), vehicle_id => 1, item_id => $item,
            outcome => $outcome, actor_user_id => $s->{users}[$user], acknowledges_check_id => $r->{newest_id});
    }
    die "not committed: $r->{status}" unless $r->{status} eq 'COMMITTED';
    return $r->{check_id};
}
# Derived status per item: latest check outcome (projection rebuilt from the append-only events).
sub projection { my ($dbh, $d) = @_;
    my $rows = $dbh->selectall_arrayref(qq{SELECT c.item_id, c.outcome FROM `$d`.check_record c
        JOIN (SELECT item_id, MAX(check_id) m FROM `$d`.check_record GROUP BY item_id) x ON x.m = c.check_id ORDER BY c.item_id});
    return join ',', map { "$_->[0]=$_->[1]" } @$rows;
}
sub run { my @cmd = @_; my $rc = system @cmd; return $rc == 0; }
sub capture { my @cmd = @_; open my $p, '-|', @cmd or die "exec @cmd: $!"; local $/; my $o = <$p>; close $p; return $o; }

add(0, 1, 'PASSED'); add(1, 2, 'PASSED');
$admin->do('FLUSH BINARY LOGS');

# Full backup (consistent snapshot, records the binary-log coordinates it corresponds to).
my $tmp = tempdir('spkrestoreXXXX', DIR => '/tmp', CLEANUP => 1);
chmod 0700, $tmp;
my $dumpfile = "$tmp/full.sql";
ok(run($dump, '--no-defaults', '-uroot', "-S$sock", '--single-transaction', '--master-data=2', '--routines', '--triggers', "--result-file=$dumpfile", $db), 'full logical backup taken');
my $dumptext = do { local $/; open my $f, '<', $dumpfile or die; <$f> };
my ($binfile, $binpos) = $dumptext =~ /MASTER_LOG_FILE='([^']+)',\s*MASTER_LOG_POS=(\d+)/;
ok($binfile && $binpos, "backup recorded binary-log coordinates ($binfile @ $binpos)");

# Activity after the backup; the target instant T is after event 5, before events 6-7.
add(0, 1, 'FAILED'); add(2, 1, 'PASSED'); add(1, 2, 'FAILED');
my ($t_file, $t_pos) = $admin->selectrow_array('SHOW MASTER STATUS');
my $expected = projection($admin, $db);
my ($rows_at_t) = $admin->selectrow_array("SELECT COUNT(*) FROM `$db`.check_record");
# Mistake/loss after T: these must NOT appear in the restored state.
add(2, 2, 'PASSED'); add(0, 1, 'PASSED');
my ($rows_now) = $admin->selectrow_array("SELECT COUNT(*) FROM `$db`.check_record");
cmp_ok($rows_now, '>', $rows_at_t, 'activity continued after the target instant');

# Restore into a new database: load the backup, then replay binary logs up to T only.
my $restored = $db . '_rst';
$admin->do("CREATE DATABASE `$restored`");
ok(run('sh', '-c', '"$0" --no-defaults -uroot -S"$1" "$2" < "$3"', $client, $sock, $restored, $dumpfile), 'backup loaded into the restore database');
my @logs = ($binfile);   # single log file in this test
# Observed difference: MariaDB's mysqlbinlog applies --database to the ORIGINAL database name, while
# MySQL 8's applies it AFTER --rewrite-db, so it must name the NEW database.
my $filter_db = ($ENV{SPIKE_FLAVOR} // '') eq 'mysql' ? $restored : $db;
my $replay = capture($binlog, '--no-defaults', "--database=$filter_db", "--rewrite-db=$db->$restored", "--start-position=$binpos", "--stop-position=$t_pos", "$base/binlog/$binfile");
diag "dump coords: $binfile @ $binpos; target: $t_file @ $t_pos; replay bytes: " . length($replay) . "; BINLOG stmts: " . scalar(() = $replay =~ /^BINLOG /mg);
ok(length $replay, "binary-log replay produced statements");
my $sqlfile = "$tmp/replay.sql";
open my $o, '>', $sqlfile or die; print {$o} $replay; close $o;
ok(run('sh', '-c', '"$0" --no-defaults -uroot -S"$1" < "$2"', $client, $sock, $sqlfile), 'replay applied to the restore database');

my ($rows_restored) = $admin->selectrow_array("SELECT COUNT(*) FROM `$restored`.check_record");
is($rows_restored, $rows_at_t, 'restored row count equals the count at the chosen instant');
is(projection($admin, $restored), $expected, 'rebuilt status projection equals the live projection captured at that instant (AC-26)');
my ($audit_at) = $admin->selectrow_array("SELECT COUNT(*) FROM `$restored`.audit_event");
is($audit_at, $rows_at_t, 'audit events restored consistently with the checks');
isnt(projection($admin, $db), $expected, 'sanity: the live (post-T) state differs from the restored state');

# The restored database is still append-only (triggers were restored with the dump).
my $ok = eval { $admin->do("UPDATE `$restored`.check_record SET outcome='FAILED' WHERE check_id=1"); 1 };
ok(!$ok, 'append-only triggers survive the restore');

$admin->do("DROP DATABASE `$db`"); $admin->do("DROP DATABASE `$restored`");
done_testing;
