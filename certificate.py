import datetime

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def generate_certificate():
    key = rsa.generate_private_key(public_exponent=65537, key_size=4096)

    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Gozargah")])
    now = datetime.datetime.now(datetime.timezone.utc)

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        # A zero/negative serial (what this file's old pyOpenSSL-based
        # implementation left as the default, since it never called
        # set_serial_number) violates RFC 5280 — `cryptography` already
        # warns on it when parsing such a certificate back and has
        # announced that a future release turns that into a hard error.
        # random_serial_number() is `cryptography`'s own helper for a
        # spec-compliant positive serial.
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=100 * 365))
        .sign(key, hashes.SHA512())
    )

    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")

    return {
        "cert": cert_pem,
        "key": key_pem
    }
