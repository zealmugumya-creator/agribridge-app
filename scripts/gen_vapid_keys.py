# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Generate a VAPID key pair for Web Push (run once, locally).

    python scripts/gen_vapid_keys.py

Put the two values in Render's environment as VAPID_PUBLIC_KEY and
VAPID_PRIVATE_KEY (VAPID_SUBJECT is optional). Never commit the
private key.
"""
import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def main() -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    private_raw = key.private_numbers().private_value.to_bytes(32, "big")
    public_raw = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    print(f"VAPID_PUBLIC_KEY={_b64u(public_raw)}")
    print(f"VAPID_PRIVATE_KEY={_b64u(private_raw)}")


if __name__ == "__main__":
    main()
