package Spike::Token;
# THROWAWAY spike (issue 15 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/15)): ID-token validation in the shape SPEC AUTH-3 requires, built on
# Crypt::JWT's documented decode_jwt options. NOTE: how Google marks an organization account
# (the "hd" claim and email_verified) is taken from SPEC AUTH-3 / general knowledge and is NOT
# verified against Google's documentation here (the sites were unreachable); this test proves the
# library mechanics only, with synthetic keys and a synthetic domain.
use strict;
use warnings;
use Crypt::JWT qw(decode_jwt);
use Exporter 'import';
our @EXPORT_OK = qw(validate_id_token);

# %cfg: jwks (hashref {keys=>[...]}), issuer, audience, domain
sub validate_id_token {
    my ($token, %cfg) = @_;
    my $claims = eval {
        decode_jwt(
            token         => $token,
            kid_keys      => $cfg{jwks},
            accepted_alg  => 'RS256',          # pin: defeats alg=none and HS256-with-public-key confusion
            verify_exp    => 1,                # REQUIRE exp; the default accepts a token with no exp
            verify_iat    => 1,
            leeway        => 30,
            verify_iss    => $cfg{issuer},
            verify_aud    => $cfg{audience},
        );
    };
    return (undef, "signature/claims rejected: " . ($@ =~ s/ at .*//sr)) unless $claims;
    return (undef, 'missing sub')                unless defined $claims->{sub} && length $claims->{sub};
    return (undef, 'email not verified')         unless $claims->{email_verified};
    return (undef, 'not an organization account') unless defined $claims->{hd} && $claims->{hd} eq $cfg{domain};
    # The email suffix alone is never trusted (SPEC AUTH-1); identity key is sub (AUTH-3).
    return ({ subject => $claims->{sub}, email => $claims->{email} }, undef);
}

1;
