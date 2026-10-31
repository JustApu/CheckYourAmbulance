#!/usr/bin/env perl
# THROWAWAY spike (issue 15 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/15)) criterion 3: strict data handling. Invalid or oversized values are
# rejected, not coerced; a startup check refuses a non-strict server/session.
use strict; use warnings;
use Test::More;
use lib 'spike/oq24/lib';
use Spike::DB qw(admin_dbh app_dbh load_schema new_db_name);
use Spike::Seed qw(seed);

my $db = new_db_name();
load_schema($db);
my $admin = admin_dbh($db);
my $s = seed($admin);
my $app = app_dbh($db);
my $u = $s->{users}[0];

# Startup check (what the real application would run at boot and on every new connection).
sub assert_strict {
    my ($dbh) = @_;
    my ($mode) = $dbh->selectrow_array('SELECT @@SESSION.sql_mode');
    return ($mode =~ /STRICT_ALL_TABLES|STRICT_TRANS_TABLES/ && $mode =~ /ERROR_FOR_DIVISION_BY_ZERO/ && $mode =~ /NO_ZERO_DATE/) ? 1 : 0;
}
ok(assert_strict($app), 'application connection passes the strict-mode startup check');

my $row = sub { my ($op, $outcome, $expl) = @_;
    $app->do(q{INSERT INTO check_record (operation_id, vehicle_id, item_id, outcome, explanation, actor_user_id) VALUES (?,1,1,?,?,?)},
        undef, $op, $outcome, $expl, $u) };

# Oversized value (explanation is VARCHAR(120)).
my $ok = eval { $row->('a0000000-0000-0000-0000-000000000001', 'FAILED', 'x' x 500); 1 };
ok(!$ok, 'oversized explanation is rejected, not truncated');
like($@ || '', qr/too long|truncated|Data too long/i, '  error says data too long');

# Invalid ENUM value.
$ok = eval { $row->('a0000000-0000-0000-0000-000000000002', 'MAYBE', undef); 1 };
ok(!$ok, 'invalid enum value is rejected');

# NOT NULL.
$ok = eval { $app->do(q{INSERT INTO check_record (operation_id, vehicle_id, item_id, outcome, actor_user_id) VALUES (NULL,1,1,'PASSED',?)}, undef, $u); 1 };
ok(!$ok, 'NULL in a NOT NULL column is rejected');

# Foreign key.
$ok = eval { $app->do(q{INSERT INTO check_record (operation_id, vehicle_id, item_id, outcome, actor_user_id) VALUES ('a0000000-0000-0000-0000-000000000003',999,1,'PASSED',?)}, undef, $u); 1 };
ok(!$ok, 'foreign key violation is rejected');

# CHECK constraint (Unable to Inspect needs a reason).
$ok = eval { $row->('a0000000-0000-0000-0000-000000000004', 'UNABLE', undef); 1 };
ok(!$ok, 'CHECK constraint rejects UNABLE without explanation');
$ok = eval { $row->('a0000000-0000-0000-0000-000000000005', 'UNABLE', 'tank empty'); 1 };
ok($ok, 'CHECK constraint accepts UNABLE with explanation') or diag $@;

# Unique operation id.
$row->('a0000000-0000-0000-0000-000000000006', 'PASSED', undef);
$ok = eval { $row->('a0000000-0000-0000-0000-000000000006', 'PASSED', undef); 1 };
ok(!$ok, 'duplicate operation_id is rejected');
is($app->err, 1062, '  with duplicate-key error 1062');

# Invalid date: zero dates and out-of-range.
$ok = eval { $app->do(q{INSERT INTO obligation (vehicle_id,item_id,due_at_utc,early_at_utc,grace_at_utc,requirement_version) VALUES (1,1,'2026-02-31 00:00:00','2026-02-28 00:00:00','2026-02-28 00:00:00',1)}); 1 };
ok(!$ok, 'impossible calendar date is rejected');

# Demonstrate the risk this guards against: a NON-strict session silently coerces.
my $loose = app_dbh($db);
$loose->do(q{SET SESSION sql_mode = ''});
ok(!assert_strict($loose), 'startup check detects a non-strict session');
my $coerced = eval { $loose->do(q{INSERT INTO check_record (operation_id, vehicle_id, item_id, outcome, explanation, actor_user_id) VALUES ('a0000000-0000-0000-0000-000000000007',1,1,'PASSED',?,?)}, undef, 'y' x 500, $u); 1 };
ok($coerced, 'RISK DEMONSTRATED: a non-strict session silently truncates an oversized value');
my ($len) = $admin->selectrow_array(q{SELECT CHAR_LENGTH(explanation) FROM check_record WHERE operation_id='a0000000-0000-0000-0000-000000000007'});
is($len, 120, '  the stored value was cut to 120 characters');
# CHECK constraints do not depend on sql_mode.
$ok = eval { $loose->do(q{INSERT INTO check_record (operation_id, vehicle_id, item_id, outcome, actor_user_id) VALUES ('a0000000-0000-0000-0000-000000000008',1,1,'UNABLE',?)}, undef, $u); 1 };
ok(!$ok, 'CHECK constraint still rejects in a non-strict session (not sql_mode dependent)');

# Server-level default (what a deployer gets unconfigured) is reported for the record.
my ($global) = $admin->selectrow_array('SELECT @@GLOBAL.sql_mode');
diag "server default global sql_mode: $global";

$admin->do("DROP DATABASE `$db`");
done_testing;
