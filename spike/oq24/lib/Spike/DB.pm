package Spike::DB;
# THROWAWAY spike helper (issue 15 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/15)). Connection and schema helpers for tests.
# Reads SPIKE_SOCK (unix socket of a private test server) and SPIKE_CLIENT
# (mariadb or mysql CLI used to load the schema, because it understands DELIMITER).
use strict;
use warnings;
use DBI;
use Carp qw(croak);
use File::Temp ();
use File::Spec ();
use Exporter 'import';
our @EXPORT_OK = qw(admin_dbh app_dbh load_schema new_db_name sock);

sub sock {
    my $s = $ENV{SPIKE_SOCK} or croak 'SPIKE_SOCK not set';
    return $s;
}

sub _dbh {
    my (%a) = @_;
    my $dsn = 'DBI:MariaDB:' . ($a{db} ? "database=$a{db};" : '') . 'mariadb_socket=' . sock();
    my $dbh = DBI->connect($dsn, $a{user}, $a{pass}, {
        RaiseError => 1, PrintError => 0, AutoCommit => 1, AutoInactiveDestroy => 1,
    });
    # UTC session and explicit strict mode on every connection (SPEC TIME-2, spike criterion 3).
    $dbh->do(q{SET SESSION time_zone = '+00:00'});
    $dbh->do(q{SET SESSION sql_mode = 'STRICT_ALL_TABLES,NO_ZERO_DATE,NO_ZERO_IN_DATE,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION'});
    return $dbh;
}

sub admin_dbh { my ($db) = @_; return _dbh(db => $db, user => 'root', pass => ''); }
sub app_dbh   { my ($db) = @_; return _dbh(db => $db, user => 'app', pass => 'spike-local-only'); }

sub new_db_name { return 'spk_' . $$ . '_' . int(rand(1e6)); }

sub load_schema {
    my ($db) = @_;
    my $admin = admin_dbh();
    $admin->do("CREATE DATABASE `$db` CHARACTER SET utf8mb4");
    $admin->disconnect;
    my $client = $ENV{SPIKE_CLIENT} || 'mariadb';
    open my $fh, '<', 'spike/oq24/sql/schema.sql' or croak "schema: $!";
    local $/; my $sql = <$fh>; close $fh;
    $sql =~ s/\{DB\}/`$db`/g;
    # Unpredictable private temp file (mode 0600), removed automatically.
    my $tmp = File::Temp->new(TEMPLATE => 'spkschemaXXXXXXXX', DIR => File::Spec->tmpdir, SUFFIX => '.sql', UNLINK => 1);
    print {$tmp} $sql; $tmp->flush;
    my @cmd = ($client, '--no-defaults', '-uroot', '-S', sock(), $db);
    open my $in, '<', $tmp->filename or croak $!;
    open my $saved, '>&', \*STDIN or croak $!;
    open STDIN, '<&', $in or croak $!;
    my $rc = system @cmd;
    open STDIN, '<&', $saved; close $in;
    croak "schema load failed (rc=$rc)" if $rc != 0;
    return;
}

1;
