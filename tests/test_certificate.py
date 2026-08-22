"""certificate.py builds the node's own TLS identity (main.py writes its
output straight to SSL_CERT_FILE/SSL_KEY_FILE, which uvicorn's
ssl_certfile/ssl_keyfile and rpyc's SSLAuthenticator then load for real
mTLS handshakes) via the `cryptography` package — a dependency bump there
is exactly the kind of change that can silently produce a cert/key pair
the `ssl` module itself would reject at handshake time. Parsing the output
back with `cryptography`'s own loaders, and separately loading it into a
real `ssl.SSLContext` (what actually matters at the TLS layer), is what
catches that.
"""
import datetime
import ssl
import tempfile

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from certificate import generate_certificate


def test_returns_cert_and_key_pem_strings():
    pems = generate_certificate()
    assert set(pems) == {"cert", "key"}
    assert pems["cert"].startswith("-----BEGIN CERTIFICATE-----")
    assert pems["key"].startswith("-----BEGIN PRIVATE KEY-----")


def test_cert_is_parseable_and_self_signed_rsa_4096():
    pems = generate_certificate()
    cert = x509.load_pem_x509_certificate(pems["cert"].encode())

    assert cert.subject == cert.issuer
    assert cert.subject.rfc4514_string() == "CN=Gozargah"

    public_key = cert.public_key()
    assert isinstance(public_key, rsa.RSAPublicKey)
    assert public_key.key_size == 4096


def test_cert_serial_number_is_positive_and_rfc5280_compliant():
    # A zero/negative serial is what the old pyOpenSSL-based implementation
    # left as the default (it never set one explicitly) — `cryptography`
    # already warns on parsing such a certificate and has announced a
    # future release will make that a hard error instead.
    pems = generate_certificate()
    cert = x509.load_pem_x509_certificate(pems["cert"].encode())
    assert cert.serial_number > 0


def test_key_is_parseable_and_matches_cert_public_key():
    pems = generate_certificate()
    cert = x509.load_pem_x509_certificate(pems["cert"].encode())
    private_key = serialization.load_pem_private_key(pems["key"].encode(), password=None)

    cert_public_numbers = cert.public_key().public_numbers()
    key_public_numbers = private_key.public_key().public_numbers()
    assert cert_public_numbers == key_public_numbers


def test_cert_validity_window_covers_now_and_is_long_lived():
    pems = generate_certificate()
    cert = x509.load_pem_x509_certificate(pems["cert"].encode())

    now = datetime.datetime.now(datetime.timezone.utc)
    not_before = cert.not_valid_before_utc
    not_after = cert.not_valid_after_utc

    assert not_before <= now < not_after
    # generate_certificate() asks for a 100-year lifetime; assert "very
    # long-lived" rather than an exact value so this doesn't become
    # sensitive to leap-year/day-count rounding.
    assert (not_after - not_before).days > 365 * 90


def test_cert_and_key_load_into_a_real_ssl_context():
    """The end-to-end check that actually matters: main.py writes this
    output straight to disk for uvicorn's ssl_certfile/ssl_keyfile and
    rpyc's SSLAuthenticator to load. `cryptography`/pyOpenSSL parsing the
    PEM back is necessary but not sufficient — this is what would actually
    fail at container startup if a dependency bump changed the PEM framing
    or key format in a way OpenSSL itself rejects.
    """
    pems = generate_certificate()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".pem") as certfile, \
            tempfile.NamedTemporaryFile(mode="w", suffix=".pem") as keyfile:
        certfile.write(pems["cert"])
        certfile.flush()
        keyfile.write(pems["key"])
        keyfile.flush()

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=certfile.name, keyfile=keyfile.name)
