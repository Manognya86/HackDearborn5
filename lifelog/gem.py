"""All Gemini calls. Every function returns plain dicts / strings so the API layer stays thin."""
import json
from datetime import datetime

from google import genai
from google.genai import types

from . import config
from .models import StabilityModel, TripPlan, Verdict, VisualCheck, VoiceReport

_client: genai.Client | None = None


class GeminiUnavailable(RuntimeError):
    pass


def client() -> genai.Client:
    global _client
    if not config.GEMINI_API_KEY:
        raise GeminiUnavailable("GEMINI_API_KEY is not set in .env")
    if _client is None:
        _client = genai.Client(api_key=config.GEMINI_API_KEY)
    return _client


def _structured(contents, schema, system: str, temperature: float = 0.2):
    resp = client().models.generate_content(
        model=config.GEMINI_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=schema,
            temperature=temperature,
        ),
    )
    if resp.parsed is not None:
        return resp.parsed.model_dump()
    return schema.model_validate_json(resp.text).model_dump()


def _text(contents, system: str, tools=None, temperature: float = 0.4) -> str:
    resp = client().models.generate_content(
        model=config.GEMINI_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(system_instruction=system, tools=tools, temperature=temperature),
    )
    return resp.text or ""


SAFETY = ("You are not a doctor or pharmacist. Never invent label rules. When unsure, tell the person "
          "to ask their pharmacist. Be concise and concrete.")

# ------------------------------------------------------------------ label -> stability model
LABEL_SYSTEM = f"""You read medication packaging, package inserts and prescribing information and
extract the STORAGE and STABILITY rules into a machine-readable model.
Rules:
- Use Celsius. Convert Fahrenheit if needed.
- target range = the primary labeled storage condition (e.g. refrigerate 2-8C).
- bands = labeled allowances outside the target (e.g. 'may be kept at room temperature up to 30C for 28 days'
  -> min_c = target_max_c, max_c = 30, budget_hours = 672).
- Every quote field must be copied verbatim from the document. If there is nothing to quote, write 'Not stated on label'.
- If the label gives no tolerance above its highest limit, set above_limit_budget_hours = 8 and
  above_limit_is_assumption = true.
{SAFETY}"""


def extract_label(data: bytes, mime_type: str) -> dict:
    parts = [types.Part.from_bytes(data=data, mime_type=mime_type),
             "Extract the storage and stability model for this medication."]
    return _structured(parts, StabilityModel, LABEL_SYSTEM, temperature=0.0)


# ------------------------------------------------------------------ voice / free text reports
def parse_voice(now: datetime, items: list[dict], audio: bytes | None = None,
                mime_type: str = "audio/wav", text: str | None = None) -> dict:
    system = (f"The current time is {now.isoformat()}. A person is describing what happened to their "
              f"medicine. Their medicines: {json.dumps(items)}. Turn the report into exposure events with "
              "a best-estimate temperature (a parked car in summer sun reaches 45-60C, a winter car "
              "0 to -10C, a bag indoors ~22C, on the body ~31C). Transcribe the audio verbatim.")
    parts = []
    if audio:
        parts.append(types.Part.from_bytes(data=audio, mime_type=mime_type))
    parts.append(text or "Extract the exposure events from this voice report.")
    return _structured(parts, VoiceReport, system)


# ------------------------------------------------------------------ verdict
def advise(state: dict) -> dict:
    system = f"""You explain a medicine's remaining stability budget to a patient.
You get the label-derived model (with verbatim quotes), the computed life budget, the exposure
episodes that consumed it, data gaps, what-if options and similar historical exposure patterns.
Ground every claim in those numbers and quotes. Mention data gaps if any.
verdict: USE (budget healthy), USE_SOON (budget getting low or burning now), ASK_PHARMACIST (uncertain,
data gaps, or label-based assumption drives the result), DO_NOT_USE (frozen, or budget exhausted).
{SAFETY}"""
    return _structured(json.dumps(state, default=str), Verdict, system)


def visual_check(photo: bytes, mime_type: str, model: dict) -> dict:
    system = (f"Compare this photo of a medication against the label's visual warnings: "
              f"{json.dumps(model.get('visual_checks', []))}. Describe only what you can see. {SAFETY}")
    parts = [types.Part.from_bytes(data=photo, mime_type=mime_type), "Inspect this medication."]
    return _structured(parts, VisualCheck, system)


# ------------------------------------------------------------------ outage rescue
def rescue_plan(at_risk: list[dict], outage: dict) -> str:
    system = f"""You coordinate a neighborhood medicine rescue during a power outage.
For each at-risk person (already ranked by hours of medicine life left), write:
1. a priority line (name, medicine, hours left vs hours until power is restored),
2. which refuge to use (choose from the candidates given, nearest first unless a reason not to),
3. a short, calm SMS (<= 300 chars) to send them.
Finish with a one-paragraph summary for the emergency coordinator. Use markdown. {SAFETY}"""
    contents = json.dumps({"outage": outage, "at_risk": at_risk}, default=str)
    tools = None
    try:
        tools = [types.Tool(google_maps=types.GoogleMaps())]
        return _text(contents, system, tools=tools)
    except Exception:
        return _text(contents, system)


# ------------------------------------------------------------------ insurance / refill letter
def refill_letter(ctx: dict) -> str:
    system = f"""Write a concise early-refill / replacement request letter that a patient can hand to their
pharmacist or insurer, because their medication was exposed to temperatures outside its labeled
storage conditions. Include: patient placeholder fields in [brackets], the product, a dated exposure
timeline from the data, the exact label text that was exceeded (quoted), the outage reference if any,
and a polite request. Do not exaggerate. Plain text letter format. {SAFETY}"""
    return _text(json.dumps(ctx, default=str), system, temperature=0.3)


# ------------------------------------------------------------------ trip pre-check
def parse_trip(itinerary: str, now: datetime) -> dict:
    system = (f"Today is {now.date().isoformat()}. Split this travel itinerary into consecutive legs "
              "describing where the medicine physically is (e.g. on body at the airport, checked bag in "
              "the hold, parked car at the destination). Fill gaps so legs are continuous.")
    return _structured(itinerary, TripPlan, system)


def trip_advice(sim: dict) -> str:
    system = f"""Given a simulated trip exposure for a medicine (per-leg temperatures and budget use),
give 3-5 bullet points of practical advice: which legs are the risk, what to change, what to pack.
Reference the label quotes. {SAFETY}"""
    return _text(json.dumps(sim, default=str), system)


# ------------------------------------------------------------------ porch heat brief
def porch_brief(stats: list[dict]) -> str:
    system = f"""You are writing a one-page public-health brief for a mail-order pharmacy about heat exposure of
medication deliveries left in mailboxes/porches, using the aggregated data (mailbox temperature is
modeled from air temperature plus a daytime solar-gain assumption). Highlight the worst ZIP codes and
carriers and give 3 concrete recommendations. Markdown. {SAFETY}"""
    return _text(json.dumps(stats, default=str), system)
