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
from pydantic import BaseModel, ConfigDict, Field, confloat, conint


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

    model_config = ConfigDict(use_enum_values=True)


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

    model_config = ConfigDict(use_enum_values=True)


class LineupEntry(BaseModel):
    """One player's slot in a team sheet -- starter or bench, not an event."""

    player_id: str
    name: Optional[str] = None
    jersey_number: Optional[int] = None
    position: Optional[str] = None
    is_starter: bool = True
    is_captain: bool = False


class SubstitutionRecord(BaseModel):
    """A single in/out swap. Distinct from a CDFEvent of type SUBSTITUTION --
    this is the team-sheet's own bookkeeping (who replaced whom, and why,
    when the vendor reports it), used to render the sheet itself rather than
    the event timeline."""

    team: str
    player_out_id: str
    player_in_id: str
    minute: conint(ge=0, le=130)
    period: conint(ge=1, le=4) = 2
    reason: Optional[str] = None


class TeamSheet(BaseModel):
    """One side's lineup for the match."""

    team: str
    formation: Optional[str] = None
    starters: list[LineupEntry] = Field(default_factory=list)
    substitutes: list[LineupEntry] = Field(default_factory=list)
    substitutions: list[SubstitutionRecord] = Field(default_factory=list)
    coach: Optional[str] = None


class MatchOfficials(BaseModel):
    referee: Optional[str] = None
    assistant_referees: list[str] = Field(default_factory=list)
    fourth_official: Optional[str] = None
    var_official: Optional[str] = None


class MatchSheet(BaseModel):
    """The administrative record of the match -- who played, how they lined
    up, who officiated, where and in front of how many. A separate pillar
    from event data (what happened on the ball) and still discrete,
    pipeline-compatible data unlike tracking/video/physical feeds, which
    come from entirely different vendor systems and are out of scope here
    (see orchestrator.py's design note)."""

    home: TeamSheet
    away: TeamSheet
    officials: Optional[MatchOfficials] = None
    kickoff: Optional[datetime] = None
    attendance: Optional[conint(ge=0)] = None
    weather: Optional[str] = None


class CDFDocument(BaseModel):
    """Top-level object. This is what every agent passes down the pipeline
    and what gets written to disk as the normalized output."""

    match: MatchInfo
    metadata: CDFMetadata
    players: list[PlayerRecord] = Field(default_factory=list)
    events: list[CDFEvent] = Field(default_factory=list)
    match_sheet: Optional[MatchSheet] = None

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
