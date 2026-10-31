package Spike::Seed;
# THROWAWAY spike helper: synthetic seed data only.
use strict;
use warnings;
use Exporter 'import';
our @EXPORT_OK = qw(seed lock_rows);

sub seed {
    my ($admin, $n_users) = @_;
    $n_users ||= 3;
    $admin->do(q{INSERT INTO vehicles (name) VALUES ('Synthetic Ambulance 1')});
    $admin->do(q{INSERT INTO items (name) VALUES ('Synthetic suction unit'), ('Synthetic oxygen masks')});
    my @users;
    for my $i (1 .. $n_users) {
        $admin->do('INSERT INTO users (idp_subject, display_name) VALUES (?,?)', undef, "synthetic-sub-$i", "EMT $i");
        push @users, $admin->last_insert_id(undef, undef, 'users', 'user_id');
    }
    lock_rows($admin);
    return { vehicle => 1, item => 1, item2 => 2, users => \@users };
}

# Lock rows normally appear when a checklist version is published (SPEC CHK-2).
sub lock_rows {
    my ($admin) = @_;
    $admin->do('INSERT IGNORE INTO item_lock (vehicle_id, item_id) SELECT v.vehicle_id, i.item_id FROM vehicles v CROSS JOIN items i');
    return;
}
1;
