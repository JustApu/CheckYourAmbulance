package Spike::Time;
# THROWAWAY spike (issue 15 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/15)): SPEC TIME-3. Resolve a station-local wall-clock time to a UTC instant.
#   * nonexistent local time (spring forward) -> the first valid instant after the gap
#   * ambiguous local time (fall back)        -> the FIRST occurrence
# Windows are plain arithmetic on absolute instants (SPEC 5.3). Offsets come from DateTime::TimeZone.
use strict;
use warnings;
use DateTime;
use DateTime::TimeZone;
use Exporter 'import';
our @EXPORT_OK = qw(resolve_local window_for);

sub resolve_local {
    my ($tzname, $y, $m, $d, $H, $M) = @_;
    my $tz = DateTime::TimeZone->new(name => $tzname);
    my $naive = DateTime->new(year => $y, month => $m, day => $d, hour => $H, minute => $M, time_zone => 'UTC');
    my $naive_epoch = $naive->epoch;
    # Candidate offsets: those in force a day before and a day after the local time.
    my %cand;
    for my $shift (-86400, 86400) {
        my $probe = DateTime->from_epoch(epoch => $naive_epoch + $shift, time_zone => 'UTC');
        my $off = $tz->offset_for_datetime($probe);
        my $utc = $naive_epoch - $off;
        # Valid only if the zone really has that offset at that instant.
        my $check = $tz->offset_for_datetime(DateTime->from_epoch(epoch => $utc, time_zone => 'UTC'));
        $cand{$utc} = 1 if $check == $off;
    }
    my @valid = sort { $a <=> $b } keys %cand;
    return $valid[0] if @valid;          # unique, or ambiguous -> first occurrence
    # Nonexistent: find the first instant after the gap (the transition).
    my $off_before = $tz->offset_for_datetime(DateTime->from_epoch(epoch => $naive_epoch - 86400, time_zone => 'UTC'));
    my $t = $naive_epoch - $off_before;  # lies after the transition
    $t -= 60 while $tz->offset_for_datetime(DateTime->from_epoch(epoch => $t - 60, time_zone => 'UTC')) != $off_before
                   && $tz->offset_for_datetime(DateTime->from_epoch(epoch => $t, time_zone => 'UTC')) != $off_before;
    # $t is now the first instant with the new offset.
    return $t;
}

# early/due/grace instants for a daily local due time, window in seconds each side.
sub window_for {
    my ($tzname, $y, $m, $d, $H, $M, $early_s, $grace_s) = @_;
    my $due = resolve_local($tzname, $y, $m, $d, $H, $M);
    return { early => $due - $early_s, due => $due, grace => $due + $grace_s };
}

1;
