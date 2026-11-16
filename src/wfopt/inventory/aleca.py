"""Locate and decrypt AlecaFrame's local inventory cache (`lastData.dat`).

Read-only: we only read the file AlecaFrame already writes. Nothing here touches the game.
Key/IV come from Sainan's open-source alecaframe-inventory-parser.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

KEY = bytes([76, 69, 79, 45, 65, 76, 69, 67, 9, 69, 79, 45, 65, 76, 69, 67])  # "LEO-ALEC\tEO-ALEC"
IV = bytes([49, 50, 70, 71, 66, 51, 54, 45, 76, 69, 51, 45, 113, 61, 57, 0])  # "12FGB36-LE3-q=9\0"


class InventoryError(RuntimeError):
    pass


@dataclass
class InventoryFile:
    path: Path
    mtime: float
    data: dict[str, Any]

    @property
    def age_hours(self) -> float:
        return max(0.0, (time.time() - self.mtime) / 3600)


def decrypt_bytes(raw: bytes) -> bytes:
    if len(raw) == 0 or len(raw) % 16:
        raise InventoryError(f"File size {len(raw)} is not a multiple of the AES block size")
    dec = Cipher(algorithms.AES(KEY), modes.CBC(IV)).decryptor()
    padded = dec.update(raw) + dec.finalize()
    unpad = padding.PKCS7(128).unpadder()
    try:
        return unpad.update(padded) + unpad.finalize()
    except ValueError as e:
        raise InventoryError("Bad padding after decryption: key/IV may have changed") from e


def encrypt_bytes(plain: bytes) -> bytes:
    """Inverse of decrypt_bytes. Only used to build test fixtures."""
    pad = padding.PKCS7(128).padder()
    padded = pad.update(plain) + pad.finalize()
    enc = Cipher(algorithms.AES(KEY), modes.CBC(IV)).encryptor()
    return enc.update(padded) + enc.finalize()


def parse_payload(plain: bytes) -> dict[str, Any]:
    try:
        top = json.loads(plain.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise InventoryError("Decrypted payload is not UTF-8 JSON") from e
    # Some AlecaFrame versions wrap the raw inventory in an InventoryJson string.
    if isinstance(top, dict) and isinstance(top.get("InventoryJson"), str):
        top = json.loads(top["InventoryJson"])
    if not isinstance(top, dict):
        raise InventoryError("Inventory payload is not a JSON object")
    return top


def load_inventory(path: Path) -> InventoryFile:
    if not path.exists():
        raise InventoryError(f"Inventory file not found: {path}. Is AlecaFrame installed and has it run?")
    raw = path.read_bytes()
    data = parse_payload(decrypt_bytes(raw))
    return InventoryFile(path=path, mtime=path.stat().st_mtime, data=data)
