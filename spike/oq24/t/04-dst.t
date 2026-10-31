#!/usr/bin/env perl
# THROWAWAY spike (issue 15 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/15)) criterion 4: idempotent obligation generation across spring-forward and
# fall-back, UTC storage (SPEC TIME-1..TIME-3, AC-4). America/New_York is a synthetic test zone;
# the real station zone is OQ-25.
use strict; use warnings;
use Test::More;
use DateTime;
use lib 'spike/oq24/lib';
use Spike::DB qw(admin_dbh app_dbh load_schema new_db_name);
use Spike::Seed qw(seed);
use Spike::Time qw(resolve_local window_for);

my $tz = 'America/New_York';
sub iso { DateTime->from_epoch(epoch => $_[0], time_zone => 'UTC')->strftime('%Y-%m-%d %H:%M:%S') }

# Ordinary days: 06:00 EST = 11:00Z; 06:00 EDT = 10:00Z.
is(iso(resolve_local($tz, 2026, 1, 15, 6, 0)), '2026-01-15 11:00:00', 'winter 06:00 local is 11:00Z');
is(iso(resolve_local($tz, 2026, 7, 15, 6, 0)), '2026-07-15 10:00:00', 'summer 06:00 local is 10:00Z');
# Spring forward 2026-03-08: 02:00-03:00 local does not exist.
is(iso(resolve_local($tz, 2026, 3, 8, 2, 30)), '2026-03-08 07:00:00', 'nonexistent 02:30 resolves to the first instant after the gap (03:00 EDT = 07:00Z)');
is(iso(resolve_local($tz, 2026, 3, 8, 6, 0)),  '2026-03-08 10:00:00', 'the 06:00 due time on spring-forward day uses the new offset');
# Fall back 2026-11-01: 01:00-02:00 local occurs twice.
is(iso(resolve_local($tz, 2026, 11, 1, 1, 30)), '2026-11-01 05:30:00', 'ambiguous 01:30 resolves to the FIRST occurrence (EDT = 05:30Z)');
is(iso(resolve_local($tz, 2026, 11, 1, 6, 0)),  '2026-11-01 11:00:00', 'the 06:00 due time on fall-back day uses the new offset');

# Windows are absolute arithmetic: +/-1h stays 2h wide even when the local day is 23 or 25 hours.
my $w = window_for($tz, 2026, 11, 1, 6, 0, 3600, 3600);
is($w->{grace} - $w->{early}, 7200, 'window width is exactly 2 hours on a 25-hour day');
$w = window_for($tz, 2026, 3, 8, 6, 0, 3600, 3600);
is($w->{grace} - $w->{early}, 7200, 'window width is exactly 2 hours on a 23-hour day');
# Spacing between consecutive 06:00 due times across the transitions.
my $d1 = resolve_local($tz, 2026, 3, 7, 6, 0); my $d2 = resolve_local($tz, 2026, 3, 8, 6, 0);
is(($d2 - $d1) / 3600, 23, 'due times across spring-forward are 23 hours apart');
$d1 = resolve_local($tz, 2026, 10, 31, 6, 0); $d2 = resolve_local($tz, 2026, 11, 1, 6, 0);
is(($d2 - $d1) / 3600, 25, 'due times across fall-back are 25 hours apart');

# Idempotent generator into the database; UTC storage; unique key; re-run and overlap runs.
my $db = new_db_name(); load_schema($db);
my $admin = admin_dbh($db); seed($admin);
my $app = app_dbh($db);
my ($tzs) = $app->selectrow_array('SELECT @@session.time_zone');
is($tzs, '+00:00', 'database session time zone is UTC');

sub generate {
    my ($from_y, $from_m, $from_d, $days) = @_;
    my ($made, $dups) = (0, 0);
    my $start = DateTime->new(year => $from_y, month => $from_m, day => $from_d, time_zone => 'UTC');
    for my $n (0 .. $days - 1) {
        my $day = $start->clone->add(days => $n);
        my $w = window_for($tz, $day->year, $day->month, $day->day, 6, 0, 3600, 3600);
        my $ok = eval {
            $app->do('INSERT INTO obligation (vehicle_id,item_id,due_at_utc,early_at_utc,grace_at_utc,requirement_version) VALUES (1,1,?,?,?,1)',
                undef, iso($w->{due}), iso($w->{early}), iso($w->{grace})); 1 };
        if ($ok) { $made++ } elsif ($app->err == 1062) { $dups++ } else { die $@ }
    }
    return ($made, $dups);
}
my ($m1, $d1c) = generate(2026, 3, 5, 7);      # spans spring-forward
is($m1, 7, 'first run created 7 daily obligations across spring-forward'); is($d1c, 0, '  with no duplicates');
my ($m2, $d2c) = generate(2026, 3, 5, 7);      # exact re-run
is($m2, 0, 're-running the generator creates nothing'); is($d2c, 7, '  every row detected as an existing duplicate by the unique key');
my ($m3, $d3c) = generate(2026, 3, 8, 7);      # overlapping run
is($m3, 3, 'an overlapping run only adds the 3 new days'); is($d3c, 4, '  and skips the 4 that exist');
my ($m4) = generate(2026, 10, 29, 6);          # spans fall-back
is($m4, 6, 'a run across fall-back creates 6 obligations');
my ($count) = $admin->selectrow_array('SELECT COUNT(*) FROM obligation');
is($count, 16, 'total obligation count matches distinct days');
my ($min_gap, $max_gap) = $admin->selectrow_array(q{SELECT MIN(TIMESTAMPDIFF(HOUR, early_at_utc, grace_at_utc)), MAX(TIMESTAMPDIFF(HOUR, early_at_utc, grace_at_utc)) FROM obligation});
is("$min_gap-$max_gap", '2-2', 'every stored window is exactly 2 hours wide in UTC');

# Plain INSERT with a caught duplicate-key error is used instead of INSERT IGNORE, because IGNORE
# also downgrades other errors (truncation, invalid values) to warnings and defeats strict mode.
$admin->do("DROP DATABASE `$db`");
done_testing;
