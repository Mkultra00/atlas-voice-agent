"""Deterministic SHELTER / LEAVE / CALL_AUTHORITIES table (TDD section 4.7).

Pure Python, no network. First matching rule wins.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Action = Literal["SHELTER", "LEAVE", "CALL_AUTHORITIES"]

NEAR = ("inside_building", "same_street", "within_1km")
CRIME = ("crime_followed", "crime_vehicle")

CRIME_STEPS = {
    "crime_followed": (
        "Go into the nearest busy, staffed business. "
        "Don't go back to your hotel while followed."
    ),
    "crime_vehicle": (
        "Stay calm and don't confront the driver. When the car slows in a busy "
        "area, ask to be let out at a landmark."
    ),
}


@dataclass(frozen=True)
class Decision:
    action: Action
    rule: str
    reason: str
    next_step: str
    check_in_minutes: int | None
    escalate: bool

    def spoken(self) -> str:
        out = f"{self.action}. Reason: {self.reason} First step: {self.next_step}"
        if self.check_in_minutes:
            out += f" Suggested check-in: {self.check_in_minutes} minutes."
        return out


def decide(
    threat: str,
    threat_distance: str,
    threat_trend: str,
    shelter_secure: bool,
    exit_route_clear: bool,
    anyone_injured: bool,
    caller_can_move: bool = True,
    door_hot: bool = False,
    trust_police: bool = True,
) -> Decision:
    is_crime = threat in CRIME
    near = threat_distance in NEAR

    # R1: injury, medical, trapped by fire, or violence inside the building.
    if (
        anyone_injured
        or threat == "medical"
        or (
            threat == "fire"
            and threat_distance == "inside_building"
            and not caller_can_move
        )
        or (threat == "violence_nearby" and threat_distance == "inside_building")
    ):
        if is_crime and not trust_police:
            return Decision(
                "CALL_AUTHORITIES",
                "R1",
                "Someone may be in danger, and local police may not be safe to rely on.",
                "Call your embassy emergency line now, and I'll alert our duty officer.",
                None,
                True,
            )
        return Decision(
            "CALL_AUTHORITIES",
            "R1",
            "Someone is hurt or in immediate danger, so emergency services need to come.",
            "Call the emergency number now.",
            None,
            True,
        )

    # R2b: fire with a hot door, so the hallway is not safe.
    if threat == "fire" and door_hot:
        return Decision(
            "SHELTER",
            "R2b",
            "The door is hot, so the fire may be right outside.",
            "Keep the door shut, seal the gap with a wet towel, and signal from the window.",
            2,
            True,
        )

    # R2: fire and the caller can move.
    if threat == "fire" and caller_can_move:
        return Decision(
            "LEAVE",
            "R2",
            "There is a fire and you can still get out.",
            "Take the stairs, not the lift, feel the door before opening it, and take your key.",
            2,
            False,
        )

    # R3: near, approaching, clear exit, able to move.
    if near and threat_trend == "approaching" and exit_route_clear and caller_can_move:
        return Decision(
            "LEAVE",
            "R3",
            "The danger is close and coming toward you, and your way out is clear.",
            CRIME_STEPS.get(threat, "Move now to the nearest secure building."),
            3,
            False,
        )

    # R4: near, secure shelter, and not (approaching with a clear exit).
    if (
        near
        and shelter_secure
        and (threat_trend != "approaching" or not exit_route_clear)
    ):
        return Decision(
            "SHELTER",
            "R4",
            "You are somewhere secure and leaving now would be riskier.",
            "Go to an interior room, lock the door, stay away from windows, and put your phone on silent.",
            20,
            False,
        )

    # R5: not secure, can get out.
    if not shelter_secure and exit_route_clear and caller_can_move:
        return Decision(
            "LEAVE",
            "R5",
            "Where you are is not secure, and you can get out.",
            CRIME_STEPS.get(
                threat, "Move to the nearest secure building, not far away."
            ),
            3,
            False,
        )

    # R6: non-urgent: lost documents, transport, or far-away crime.
    if threat in ("lost_documents", "transport") or (
        is_crime and threat_distance == "far"
    ):
        return Decision(
            "CALL_AUTHORITIES",
            "R6",
            "This is not an emergency, but you need an official report.",
            "Call your embassy line, and file a report at the nearest police station.",
            None,
            False,
        )

    # R7: default.
    return Decision(
        "SHELTER",
        "R7",
        "Staying put is safest until we know more.",
        CRIME_STEPS.get(
            threat, "Stay where you are and tell me what you can see and hear."
        ),
        10,
        False,
    )
