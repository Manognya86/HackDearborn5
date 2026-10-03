"""Pydantic schemas shared by Gemini structured output and the API."""
from pydantic import BaseModel, Field


class Band(BaseModel):
    label: str = Field(description="Short name, e.g. 'Room temperature allowance'")
    min_c: float
    max_c: float
    budget_hours: float = Field(description="Total hours allowed in this range, e.g. 28 days = 672")
    quote: str = Field(description="Verbatim label sentence this band comes from")


class StabilityModel(BaseModel):
    product_name: str
    form: str = Field(description="pen, vial, auto-injector, suspension, tablet ...")
    target_min_c: float = Field(description="Lower bound of the labeled storage range in Celsius")
    target_max_c: float = Field(description="Upper bound of the labeled storage range in Celsius")
    target_quote: str
    freeze_discard: bool = Field(description="True if the label says do not freeze / discard if frozen")
    freeze_c: float = Field(description="Temperature at or below which the product counts as frozen, usually 0")
    freeze_quote: str
    cold_ok: bool = Field(description="True if storage colder than the target range (but not frozen) is acceptable")
    bands: list[Band] = Field(description="Labeled temperature allowances outside the target range")
    above_limit_budget_hours: float = Field(
        description="Hours tolerated above the highest labeled limit. If the label gives no number use 8")
    above_limit_is_assumption: bool = Field(description="True if above_limit_budget_hours was not on the label")
    above_limit_quote: str
    in_use_days: float = Field(description="Days the medicine may be used after first opening or first use, regardless of "
                                           "temperature (e.g. 'discard 28 days after opening'). 0 if the label gives none")
    visual_checks: list[str] = Field(description="Visual signs the label says mean do not use (cloudy, particles ...)")
    discard_rules: list[str] = Field(description="Other discard rules, e.g. 'discard 28 days after opening'")
    notes: str


class VoiceEvent(BaseModel):
    minutes_ago_start: float = Field(description="How many minutes ago the exposure started")
    duration_minutes: float
    estimated_temp_c: float = Field(description="Best estimate of the temperature the medicine experienced")
    setting: str = Field(description="car, outdoors, indoors, bag, fridge, freezer, on body ...")
    summary: str


class VoiceReport(BaseModel):
    transcript: str
    events: list[VoiceEvent]
    item_hint: str = Field(description="Which medicine the person is talking about, if mentioned")


class Verdict(BaseModel):
    verdict: str = Field(description="One of: USE, USE_SOON, ASK_PHARMACIST, DO_NOT_USE")
    headline: str
    explanation: str
    actions: list[str]
    cited_quotes: list[str] = Field(description="Verbatim label quotes that support the verdict")


class TripLeg(BaseModel):
    place: str = Field(description="City or airport name usable for geocoding")
    start_iso: str = Field(description="ISO 8601 local start time")
    end_iso: str = Field(description="ISO 8601 local end time")
    setting: str = Field(description="One of: indoors, outdoors, parked_car, on_body, carry_on, checked_bag, cooler, fridge")
    typical_temp_c: float = Field(description="Fallback outdoor temperature estimate for that place and date")
    summary: str


class TripPlan(BaseModel):
    legs: list[TripLeg]


class VisualCheck(BaseModel):
    looks_ok: bool
    observations: list[str]
    matched_label_warnings: list[str]
    confidence: str = Field(description="low, medium or high")
