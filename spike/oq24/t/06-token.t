#!/usr/bin/env perl
# THROWAWAY spike (issue 15 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/15)) criterion 6a: ID-token validation mechanics (AC-22), offline, synthetic
# RSA keys. Proves what the library enforces; does NOT prove Google's real claim semantics.
use strict; use warnings;
use Test::More;
use Crypt::PK::RSA;
use Crypt::JWT qw(encode_jwt);
use MIME::Base64 qw(encode_base64url);
use JSON::PP;
use lib 'spike/oq24/lib';
use Spike::Token qw(validate_id_token);

my $k1 = Crypt::PK::RSA->new; $k1->generate_key(256, 65537);   # 2048-bit
my $k2 = Crypt::PK::RSA->new; $k2->generate_key(256, 65537);
my $evil = Crypt::PK::RSA->new; $evil->generate_key(256, 65537);
my $jwk = sub { my ($k, $kid) = @_; my $h = $k->export_key_jwk('public', 1); $h->{kid} = $kid; $h };
my %cfg = (
    jwks     => { keys => [ $jwk->($k1, 'k1'), $jwk->($k2, 'k2') ] },
    issuer   => 'https://idp.example.invalid',
    audience => 'synthetic-client-id',
    domain   => 'agency.example.invalid',
);
my $now = time;
my %good = (iss => $cfg{issuer}, aud => $cfg{audience}, sub => 'synthetic-sub-1', email => 'emt1@agency.example.invalid',
            email_verified => JSON::PP::true, hd => $cfg{domain}, iat => $now - 5, exp => $now + 600);
sub tok { my ($claims, %o) = @_; encode_jwt(payload => $claims, alg => 'RS256', key => $o{key} // $k1, extra_headers => { kid => $o{kid} // 'k1' }) }
sub rejected { my ($name, $token) = @_; my ($id, $err) = validate_id_token($token, %cfg); ok(!$id, "rejected: $name") or diag explain $id; diag "   -> $err" if $ENV{SPIKE_VERBOSE} && $err; }

my ($id, $err) = validate_id_token(tok(\%good), %cfg);
ok($id, 'valid organization token accepted') or diag $err;
is($id->{subject}, 'synthetic-sub-1', '  identity key is the stable subject');
($id) = validate_id_token(tok(\%good, key => $k2, kid => 'k2'), %cfg);
ok($id, 'token signed by a second published key (rotation) is accepted via kid');

rejected('wrong signing key', tok(\%good, key => $evil));
rejected('unknown kid',       tok(\%good, key => $k1, kid => 'nope'));
rejected('expired',           tok({ %good, exp => $now - 3600, iat => $now - 7200 }));
{
    # Evidence for the claim in the next test name: with the library default (verify_exp unset), a token
    # with no exp claim is accepted. Our wrapper therefore sets verify_exp => 1.
    use Crypt::JWT qw(decode_jwt);
    my $noexp = tok({ map { $_ => $good{$_} } grep { $_ ne 'exp' } keys %good });
    my $c = eval { decode_jwt(token => $noexp, kid_keys => $cfg{jwks}, accepted_alg => 'RS256') };
    ok($c, 'RISK DEMONSTRATED: Crypt::JWT default accepts a token with no exp claim');
}
rejected('missing exp (library default would accept it)', tok({ map { $_ => $good{$_} } grep { $_ ne 'exp' } keys %good }));
rejected('issued in the future', tok({ %good, iat => $now + 3600, exp => $now + 7200 }));
rejected('wrong issuer',      tok({ %good, iss => 'https://evil.example.invalid' }));
rejected('wrong audience',    tok({ %good, aud => 'someone-elses-client' }));
rejected('missing audience',  tok({ map { $_ => $good{$_} } grep { $_ ne 'aud' } keys %good }));
rejected('personal account (no hd)', tok({ map { $_ => $good{$_} } grep { $_ ne 'hd' } keys %good }));
rejected('other organization (hd differs)', tok({ %good, hd => 'other.example.invalid' }));
rejected('email claims the domain but hd missing', tok({ %good, hd => undef }));
rejected('email_verified false', tok({ %good, email_verified => JSON::PP::false }));
rejected('missing subject', tok({ map { $_ => $good{$_} } grep { $_ ne 'sub' } keys %good }));

# Tampering: change the payload, keep the signature.
{
    my ($h, $p, $sig) = split /\./, tok(\%good);
    my $payload = decode_json(MIME::Base64::decode_base64url($p));
    $payload->{sub} = 'synthetic-admin';
    rejected('tampered payload', join '.', $h, encode_base64url(encode_json($payload)), $sig);
}
# alg=none.
{
    my $h = encode_base64url(encode_json({ alg => 'none', typ => 'JWT', kid => 'k1' }));
    my $p = encode_base64url(encode_json(\%good));
    rejected('alg=none', "$h.$p.");
}
# Algorithm confusion: HS256 signed using the public key bytes as the secret.
{
    my $pub_pem = $k1->export_key_pem('public');
    my $forged = encode_jwt(payload => \%good, alg => 'HS256', key => $pub_pem, extra_headers => { kid => 'k1' });
    rejected('alg confusion (HS256 with public key as secret)', $forged);
}
rejected('garbage', 'not.a.token');
rejected('empty', '');
done_testing;
