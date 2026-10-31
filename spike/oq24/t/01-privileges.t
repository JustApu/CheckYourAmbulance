#!/usr/bin/env perl
# THROWAWAY spike (issue 15 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/15)) criterion 1: append-only enforcement (ARC-2, AC-25), while the
# authorized INSERT appends still work.
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
my ($u1) = @{ $s->{users} };

# Positive: authorized appends succeed for the least-privilege account.
ok(eval { $app->do(q{INSERT INTO check_record (operation_id, vehicle_id, item_id, outcome, actor_user_id)
    VALUES ('11111111-1111-1111-1111-111111111111',1,1,'PASSED',?)}, undef, $u1); 1 }, 'app can INSERT a check_record') or diag $@;
ok(eval { $app->do(q{INSERT INTO audit_event (actor_id, action) VALUES (?, 'TEST')}, undef, $u1); 1 }, 'app can INSERT an audit_event') or diag $@;
ok(eval { $app->do(q{INSERT INTO check_event (operation_id, check_id, event_type, new_outcome, actor_user_id)
    VALUES ('22222222-2222-2222-2222-222222222222',1,'CORRECTION','FAILED',?)}, undef, $u1); 1 }, 'author can append a correction') or diag $@;

# Negative: every mutation path on append-only tables must fail for the app account.
my %denied = (
    'UPDATE check_record'      => q{UPDATE check_record SET outcome='FAILED' WHERE check_id=1},
    'DELETE check_record'      => q{DELETE FROM check_record WHERE check_id=1},
    'UPDATE audit_event'       => q{UPDATE audit_event SET action='X'},
    'DELETE audit_event'       => q{DELETE FROM audit_event},
    'UPDATE check_event'       => q{UPDATE check_event SET explanation='x'},
    'TRUNCATE check_record'    => q{TRUNCATE TABLE check_record},
    'DROP TABLE check_record'  => q{DROP TABLE check_record},
    'ALTER TABLE check_record' => q{ALTER TABLE check_record ADD COLUMN x INT},
    'DROP TRIGGER'             => q{DROP TRIGGER check_record_no_update},
    'REPLACE check_record'     => q{REPLACE INTO check_record (check_id, operation_id, vehicle_id, item_id, outcome, actor_user_id) VALUES (1,'33333333-3333-3333-3333-333333333333',1,1,'PASSED',1)},
    'INSERT .. ON DUPLICATE KEY UPDATE' => q{INSERT INTO check_record (operation_id, vehicle_id, item_id, outcome, actor_user_id) VALUES ('11111111-1111-1111-1111-111111111111',1,1,'PASSED',1) ON DUPLICATE KEY UPDATE outcome='FAILED'},
);
for my $name (sort keys %denied) {
    my $ok = eval { $app->do($denied{$name}); 1 };
    ok(!$ok, "app account cannot: $name");
    like($@ || '', qr/denied|command denied|privilege/i, "  reason is a privilege error for: $name") if !$ok;
}
my ($outcome) = $admin->selectrow_array('SELECT outcome FROM check_record WHERE check_id=1');
is($outcome, 'PASSED', 'original row unchanged after all attempts');

# Defense in depth: even an account that was (mis)granted UPDATE/DELETE is stopped by the triggers.
$admin->do(q{CREATE USER 'oops'@'localhost' IDENTIFIED BY 'x'});
$admin->do("GRANT SELECT, INSERT, UPDATE, DELETE ON `$db`.check_record TO 'oops'\@'localhost'");
{
    my $dsn = 'DBI:MariaDB:database=' . $db . ';mariadb_socket=' . $ENV{SPIKE_SOCK};
    my $oops = DBI->connect($dsn, 'oops', 'x', { RaiseError => 1, PrintError => 0 });
    my $ok = eval { $oops->do(q{UPDATE check_record SET outcome='FAILED' WHERE check_id=1}); 1 };
    ok(!$ok, 'mis-granted UPDATE is still blocked by the trigger');
    like($@ || '', qr/append-only/, '  trigger message names append-only');
    $ok = eval { $oops->do(q{DELETE FROM check_record WHERE check_id=1}); 1 };
    ok(!$ok, 'mis-granted DELETE is still blocked by the trigger');
}

# Author-only correction is enforced in the database as well (COR-1).
my $other = $s->{users}[1];
my $ok = eval { $app->do(q{INSERT INTO check_event (operation_id, check_id, event_type, new_outcome, actor_user_id)
    VALUES ('44444444-4444-4444-4444-444444444444',1,'CORRECTION','PASSED',?)}, undef, $other); 1 };
ok(!$ok, 'a different user cannot append a CORRECTION to someone else\'s check');
like($@ || '', qr/original author/, '  message names the author rule');
$ok = eval { $app->do(q{INSERT INTO check_event (operation_id, check_id, event_type, explanation, actor_user_id)
    VALUES ('55555555-5555-5555-5555-555555555555',1,'INVALIDATION','admin explanation',?)}, undef, $other); 1 };
ok($ok, 'an INVALIDATION by another actor is storable (authority is checked by the application, SPEC 3.3)');
$ok = eval { $app->do(q{INSERT INTO check_event (operation_id, check_id, event_type, actor_user_id)
    VALUES ('66666666-6666-6666-6666-666666666666',1,'INVALIDATION',?)}, undef, $other); 1 };
ok(!$ok, 'an INVALIDATION without an explanation is rejected by a CHECK constraint');

$admin->do("DROP DATABASE `$db`");
$admin->do(q{DROP USER 'oops'@'localhost'});
done_testing;
