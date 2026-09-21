"""Installed dependencies must support the real App JWT signing algorithm."""
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from fl4write import appauth


def test_app_jwt_signs_and_verifies_with_an_ephemeral_rsa_key(tmp_path, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path / "ephemeral.pem"
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                      serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))
    monkeypatch.setattr(appauth, "KEY_PATH", path)
    monkeypatch.setattr(appauth, "APP_ID", 123)
    token = appauth._make_jwt()
    claims = jwt.decode(token, key.public_key(), algorithms=["RS256"], issuer="123")
    assert claims["exp"] - claims["iat"] == 660
