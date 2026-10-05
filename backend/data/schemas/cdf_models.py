"""
CDF (Common Data Format) Pydantic models.

This is the runtime contract every agent imports. It mirrors cdf_unified.json
exactly — if you change one, change the other. The JSON schema is the
spec/documentation; these models are what the code actually validates against.
"""

from __future__ import annotations
from datetime import date, datetime
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field, confloat, conint


class VendorSource(str, Enum):
    OPTA = "opta"
    STATSBOMB = "statsbomb"
    WYSCOUT = "wyscout"
    GENIUS_SPORTS = "genius_sports"
    UNKNOWN = "unknown"


class EventType(str, Enum):
    PASS = "pass"
    SHOT = "shot"
    TACKLE = "tackle"
    FOUL = "foul"
    CARD = "card"
    SUBSTITUTION = "substitution"
    CORNER = "corner"
    THROW_IN = "throw_in"
    GOAL_KICK = "goal_kick"
    OFFSIDE = "offside"
    GOAL = "goal"
    SAVE = "save"
    INTERCEPTION = "interception"
    CLEARANCE = "clearance"
    DRIBBLE = "dribble"
    OTHER = "other"


Confidence = confloat(ge=0.0, le=1.0)
Coordinate = confloat(ge=0.0, le=100.0)


class MatchInfo(BaseModel):
    match_id: str
    date: date
    home_team: str
    away_team: str
    venue: Optional[str] = None
    season: Optional[int] = None
    competition: Optional[str] = None


class CDFMetadata(BaseModel):
    source_vendors: list[VendorSource]
    coordinate_system: str = Field(
        default="normalized_0_100",
        description="Always 0-100 x 0-100. Guaranteed by Coordinate Translator agent.",
    )
    timestamp_precision: str = "seconds"
    quality_confidence: Confidence
    generation_timestamp: datetime
    warnings: list[str] = Field(default_factory=list)
    orchestration_log_ref: Optional[str] = None

    class Config:
        use_enum_values = True


class PlayerRecord(BaseModel):
    player_id: str
    name: Optional[str] = None
    jersey_number: Optional[int] = None
    position: Optional[str] = None
    team: Optional[str] = None
    vendor_ids: dict[str, str] = Field(default_factory=dict)
    resolution_confidence: Confidence
    resolution_attempts: conint(ge=1) = 1


class CDFEvent(BaseModel):
    event_id: str
    event_type: EventType
    timestamp: confloat(ge=0.0)
    period: conint(ge=1, le=4)
    player_id: Optional[str] = None
    team: Optional[str] = None
    x: Coordinate
    y: Coordinate
    end_x: Optional[Coordinate] = None
    end_y: Optional[Coordinate] = None
    outcome: Optional[bool] = None
    quality_confidence: Confidence
    vendor_source: VendorSource
    metadata: dict = Field(default_factory=dict)

    class Config:
        use_enum_values = True


class CDFDocument(BaseModel):
    """Top-level object. This is what every agent passes down the pipeline
    and what gets written to disk as the normalized output."""

    match: MatchInfo
    metadata: CDFMetadata
    players: list[PlayerRecord] = Field(default_factory=list)
    events: list[CDFEvent] = Field(default_factory=list)

    def confidence_summary(self) -> dict:
        """Quick sanity check used by Validator agent and the frontend dashboard."""
        if not self.events:
            return {"event_count": 0, "avg_confidence": None, "low_confidence_count": 0}
        confidences = [e.quality_confidence for e in self.events]
        return {
            "event_count": len(self.events),
            "avg_confidence": round(sum(confidences) / len(confidences), 4),
            "min_confidence": round(min(confidences), 4),
            "low_confidence_count": sum(1 for c in confidences if c < 0.6),
        }
