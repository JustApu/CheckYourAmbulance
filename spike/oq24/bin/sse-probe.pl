#!/usr/bin/env perl
# THROWAWAY spike helper (issue 15 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/15)): tiny Mojolicious daemon serving one finite SSE response on
# loopback, so the test can read it with a real HTTP client. Usage: sse-probe.pl PORT
use strict; use warnings;
use Mojolicious::Lite -signatures;
app->log->level('fatal');
get '/sse-once' => sub ($c, @) {
    $c->write_sse({ comment => 'connected' } => sub ($c, @) {
        $c->write_sse({ id => 1, type => 'check', text => 'check:42' } => sub ($c, @) { $c->finish });
    });
};
my $port = shift // die "usage: sse-probe.pl PORT\n";
app->start('daemon', '-l', "http://127.0.0.1:$port");
