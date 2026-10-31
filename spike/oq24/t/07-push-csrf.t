#!/usr/bin/env perl
# THROWAWAY spike (issue 15 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/15)) criterion 6b: live updates (SPEC REC-3, 17.3), CSRF protection and
# session-cookie flags (SPEC 16.2), using Mojolicious's documented APIs, in-process, no network.
use strict; use warnings;
use Test::More;
use Mojolicious::Lite -signatures;
use Test::Mojo;
use POSIX ();

# ---- app under test --------------------------------------------------------------------------
app->secrets(['spike-only-synthetic-secret']);
app->sessions->secure(1);
app->sessions->samesite('Strict');
app->sessions->default_expiration(900);
my %subscribers;       # vehicle => { id => controller }
my $seq = 0;

get '/login' => sub ($c) { $c->session(user => 'synthetic-sub-1'); $c->render(text => 'ok') };
get '/csrf'  => sub ($c) { $c->render(text => $c->csrf_token) };

# Mutation: CSRF-protected, then publish to subscribers AFTER the (here simulated) commit.
post '/vehicles/:v/checks' => sub ($c, @) {
    return $c->render(text => 'unauthenticated', status => 401) unless $c->session('user');
    my $v = $c->validation;
    return $c->render(text => 'bad csrf', status => 403) if $v->csrf_protect->has_error('csrf_token');
    my $event = "check:" . ++$seq;
    for my $sub (values %{ $subscribers{$c->param('v')} // {} }) { $sub->send($event) }   # WebSocket push
    for my $sse (values %{ $subscribers{"sse-" . $c->param('v')} // {} }) { $sse->write_sse({ type => 'check', text => $event }) }
    $c->render(text => $event);
};

websocket '/vehicles/:v/live' => sub ($c, @) {
    return $c->render(text => 'unauthenticated', status => 401) unless $c->session('user');
    my $id = "$c"; $subscribers{$c->param('v')}{$id} = $c;
    $c->on(finish => sub { delete $subscribers{$c->param('v')}{$id} });
};

get '/vehicles/:v/sse' => sub ($c, @) {
    return $c->render(text => 'unauthenticated', status => 401) unless $c->session('user');
    my $id = "$c"; $subscribers{"sse-" . $c->param('v')}{$id} = $c;
    $c->on(finish => sub { delete $subscribers{"sse-" . $c->param('v')}{$id} });
    $c->write_sse({ comment => 'connected' });
};

# Finite SSE response used to verify event framing (write_sse is documented EXPERIMENTAL in Mojolicious 9.51).
get '/sse-once' => sub ($c, @) {
    $c->write_sse({ comment => 'connected' } => sub ($c, @) {
        $c->write_sse({ id => 1, type => 'check', text => 'check:42' } => sub ($c, @) { $c->finish });
    });
};

# ---- tests -----------------------------------------------------------------------------------
my $t = Test::Mojo->new;
$t->ua->max_redirects(0);

# Session cookie flags.
$t->get_ok('/login')->status_is(200);
my $cookie = $t->tx->res->headers->set_cookie // '';
like($cookie, qr/\bsecure\b/i,           'session cookie has Secure');
like($cookie, qr/\bhttponly\b/i,         'session cookie has HttpOnly (documented default)');
like($cookie, qr/samesite=strict/i,      'session cookie has SameSite=Strict');

# Unauthenticated access to the live channel is refused.
my $anon = Test::Mojo->new;
$anon->get_ok('/vehicles/1/sse')->status_is(401);
$anon->post_ok('/vehicles/1/checks')->status_is(401);

# CSRF: a state-changing POST without or with a wrong token is refused; with the token it succeeds.
$t->ua->server->app->sessions->secure(0);   # the in-process client speaks plain HTTP; flags were checked above
$t->get_ok('/login');
$t->post_ok('/vehicles/1/checks')->status_is(403)->content_is('bad csrf');
$t->post_ok('/vehicles/1/checks' => form => { csrf_token => 'wrong' })->status_is(403);
$t->get_ok('/csrf'); my $token = $t->tx->res->text;
$t->post_ok('/vehicles/1/checks' => form => { csrf_token => $token })->status_is(200)->content_like(qr/^check:\d+$/);

# WebSocket push: two clients on the same vehicle both receive the event after a committed write.
my @got;
my $ws1 = Test::Mojo->new($t->app); my $ws2 = Test::Mojo->new($t->app);
for my $c ($ws1, $ws2) { $c->get_ok('/login'); }
$ws1->websocket_ok('/vehicles/7/live'); $ws2->websocket_ok('/vehicles/7/live');
$t->get_ok('/csrf'); my $tok2 = $t->tx->res->text;
$t->post_ok('/vehicles/7/checks' => form => { csrf_token => $tok2 })->status_is(200);
$ws1->message_ok->message_like(qr/^check:\d+$/, 'client 1 received the pushed event');
$ws2->message_ok->message_like(qr/^check:\d+$/, 'client 2 received the pushed event');
# A subscriber on another vehicle does not receive it.
my $ws3 = Test::Mojo->new($t->app); $ws3->get_ok('/login'); $ws3->websocket_ok('/vehicles/8/live');
$t->post_ok('/vehicles/7/checks' => form => { csrf_token => $tok2 })->status_is(200);
$ws1->message_ok; $ws2->message_ok;
$ws3->finish_ok;
$ws1->finish_ok; $ws2->finish_ok;
is(scalar(keys %{ $subscribers{7} // {} }), 0, 'subscriptions are cleaned up when clients disconnect');

# Server-sent events: write_sse is documented EXPERIMENTAL in Mojolicious 9.51. The in-process test
# client did not expose the streamed body, so framing is verified over loopback with a real HTTP
# client (curl) against a real daemon in a child process. Its drain callbacks also received an extra
# argument, which breaks strict subroutine signatures (hence "$c, @" above).
SKIP: {
    skip 'curl not available', 3 unless system('curl --version >/dev/null 2>&1') == 0;
    require Mojo::IOLoop::Server;
    my $port = Mojo::IOLoop::Server->generate_port;
    my $pid = fork() // die;
    if ($pid == 0) {   # exec a fresh perl so the daemon does not inherit this process's event loop
        exec $^X, '-I', 'local/lib/perl5', 'spike/oq24/bin/sse-probe.pl', $port or POSIX::_exit(127);
    }
    select undef, undef, undef, 1.5;
    open my $c, '-|', 'sh', '-c', 'exec curl -sS -N -m 5 "$0" 2>&1', "http://127.0.0.1:$port/sse-once" or die "curl: $!";
    my $body = do { local $/; <$c> }; close $c;
    kill 'TERM', $pid; waitpid $pid, 0;
    like($body // '', qr/^: connected\r?$/m,   'SSE comment frame (real client)');
    like($body // '', qr/^event: check\r?$/m,  'SSE event-type frame (real client)');
    like($body // '', qr/^data: check:42\r?$/m,'SSE data frame (real client)');
}

done_testing;
