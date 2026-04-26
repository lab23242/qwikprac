"""
Pump.fun program constants, account structures, and instruction builders.
All static PDAs are pre-computed at import time so call-sites pay no overhead.
"""
import hashlib
import struct
from dataclasses import dataclass
from typing import Optional

from solders.pubkey import Pubkey
from solders.instruction import AccountMeta, Instruction
from solders.system_program import ID as SYS_PROGRAM_ID

PUMP_PROGRAM_ID  = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
PUMP_FEE_RECIPIENT = Pubkey.from_string("CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM")

_TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
_ATA_PROGRAM   = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJe1bS4")
RENT_SYSVAR    = Pubkey.from_string("SysvarRent111111111111111111111111111111111")

# Computed once at import — never recomputed per call
_GLOBAL_PDA, _         = Pubkey.find_program_address([b"global"],              PUMP_PROGRAM_ID)
_EVENT_AUTH_PDA, _     = Pubkey.find_program_address([b"__event_authority"],   PUMP_PROGRAM_ID)

JITO_TIP_ACCOUNTS = [
    Pubkey.from_string("96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5"),
    Pubkey.from_string("HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe"),
    Pubkey.from_string("Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY"),
    Pubkey.from_string("ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1sMaC9jbm3w"),
    Pubkey.from_string("DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh"),
    Pubkey.from_string("ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt"),
    Pubkey.from_string("DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL"),
    Pubkey.from_string("3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT"),
]

BUY_DISCRIMINATOR          = hashlib.sha256(b"global:buy").digest()[:8]
CREATE_DISCRIMINATOR       = hashlib.sha256(b"global:create").digest()[:8]
CREATE_EVENT_DISCRIMINATOR = hashlib.sha256(b"event:CreateEvent").digest()[:8]


@dataclass
class CreateEvent:
    name: str
    symbol: str
    uri: str
    mint: Pubkey
    bonding_curve: Pubkey
    creator: Pubkey

    @staticmethod
    def decode(data: bytes) -> Optional["CreateEvent"]:
        try:
            offset = 8
            name,          offset = _read_str(data, offset)
            symbol,        offset = _read_str(data, offset)
            uri,           offset = _read_str(data, offset)
            mint           = Pubkey.from_bytes(data[offset:offset + 32]); offset += 32
            bonding_curve  = Pubkey.from_bytes(data[offset:offset + 32]); offset += 32
            creator        = Pubkey.from_bytes(data[offset:offset + 32])
            return CreateEvent(name=name, symbol=symbol, uri=uri,
                               mint=mint, bonding_curve=bonding_curve, creator=creator)
        except Exception:
            return None


@dataclass
class BondingCurve:
    virtual_token_reserves: int
    virtual_sol_reserves:   int
    real_token_reserves:    int
    real_sol_reserves:      int
    token_total_supply:     int
    complete: bool

    @staticmethod
    def decode(data: bytes) -> Optional["BondingCurve"]:
        try:
            vtr, vsr, rtr, rsr, tts = struct.unpack_from("<QQQQQ", data, 8)
            complete = bool(data[48])
            return BondingCurve(vtr, vsr, rtr, rsr, tts, complete)
        except Exception:
            return None

    def token_price_in_sol(self) -> float:
        if self.virtual_token_reserves == 0:
            return 0.0
        return self.virtual_sol_reserves / self.virtual_token_reserves / 1e9

    def market_cap_sol(self) -> float:
        if self.virtual_token_reserves == 0:
            return 0.0
        price_lamports = self.virtual_sol_reserves / self.virtual_token_reserves
        return price_lamports * self.token_total_supply / 1e9

    def tokens_for_sol(self, sol_amount: float) -> int:
        sol_in  = int(sol_amount * 1e9)
        new_sol = self.virtual_sol_reserves + sol_in
        new_tok = (self.virtual_sol_reserves * self.virtual_token_reserves) // new_sol
        return self.virtual_token_reserves - new_tok


# Public accessors return pre-computed singletons
def get_global_pda()         -> Pubkey: return _GLOBAL_PDA
def get_event_authority_pda() -> Pubkey: return _EVENT_AUTH_PDA


def get_bonding_curve_pda(mint: Pubkey) -> Pubkey:
    pda, _ = Pubkey.find_program_address([b"bonding-curve", bytes(mint)], PUMP_PROGRAM_ID)
    return pda


def get_ata(wallet: Pubkey, mint: Pubkey) -> Pubkey:
    """Derive Associated Token Account address without importing spl-token."""
    pda, _ = Pubkey.find_program_address(
        [bytes(wallet), bytes(_TOKEN_PROGRAM), bytes(mint)],
        _ATA_PROGRAM,
    )
    return pda


def build_buy_instruction(
    buyer: Pubkey,
    mint: Pubkey,
    bonding_curve: Pubkey,
    token_amount: int,
    max_sol_cost: int,
) -> Instruction:
    data = BUY_DISCRIMINATOR + struct.pack("<QQ", token_amount, max_sol_cost)
    accounts = [
        AccountMeta(pubkey=_GLOBAL_PDA,                          is_signer=False, is_writable=False),
        AccountMeta(pubkey=PUMP_FEE_RECIPIENT,                   is_signer=False, is_writable=True),
        AccountMeta(pubkey=mint,                                 is_signer=False, is_writable=False),
        AccountMeta(pubkey=bonding_curve,                        is_signer=False, is_writable=True),
        AccountMeta(pubkey=get_ata(bonding_curve, mint),         is_signer=False, is_writable=True),
        AccountMeta(pubkey=get_ata(buyer, mint),                 is_signer=False, is_writable=True),
        AccountMeta(pubkey=buyer,                                is_signer=True,  is_writable=True),
        AccountMeta(pubkey=SYS_PROGRAM_ID,                       is_signer=False, is_writable=False),
        AccountMeta(pubkey=_TOKEN_PROGRAM,                       is_signer=False, is_writable=False),
        AccountMeta(pubkey=RENT_SYSVAR,                          is_signer=False, is_writable=False),
        AccountMeta(pubkey=_EVENT_AUTH_PDA,                      is_signer=False, is_writable=False),
        AccountMeta(pubkey=PUMP_PROGRAM_ID,                      is_signer=False, is_writable=False),
    ]
    return Instruction(program_id=PUMP_PROGRAM_ID, data=bytes(data), accounts=accounts)


def _read_str(data: bytes, offset: int) -> tuple[str, int]:
    length = struct.unpack_from("<I", data, offset)[0]
    offset += 4
    return data[offset:offset + length].decode("utf-8"), offset + length
