from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class Role(str, Enum):
    INITIATOR = "initiator"
    RESPONDER = "responder"

    @property
    def display_name(self) -> str:
        if self is Role.INITIATOR:
            return "Initiator / Paper Rx — Send C1 → Wait C2"
        return "Responder / Paper Tx — Wait C1 → Send C2"


class HandshakeState(str, Enum):
    IDLE = "IDLE"
    INIT_RECORDING = "INIT_RECORDING"
    PRE_ROLL = "PRE_ROLL"
    WAIT_ARM_ACK = "WAIT_ARM_ACK"
    LISTEN_C1 = "LISTEN_C1"
    C1_PLAY = "C1_PLAY"
    WAIT_C2 = "WAIT_C2"
    C1_DETECTED = "C1_DETECTED"
    C2_IMMEDIATE_RESPONSE = "C2_IMMEDIATE_RESPONSE"
    C2_DETECTED = "C2_DETECTED"
    POST_ROLL = "POST_ROLL"
    PRECISE_ANALYSIS = "PRECISE_ANALYSIS"
    RIR_EXTRACTION = "RIR_EXTRACTION"
    TOF_CALCULATION = "TOF_CALCULATION"
    SEND_METADATA = "SEND_METADATA"
    DONE = "DONE"
    FAILED = "FAILED"


@dataclass(slots=True)
class NetworkMetadata:
    protocol: str
    session_id: str
    role: str
    state: str
    timestamp_basis: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "protocol_version": 1,
            "session_id": self.session_id,
            "role": self.role.upper(),
            "state": self.state,
            "timestamp_basis": self.timestamp_basis,
            **self.payload,
        }
