"""All Gemini calls. Every function returns plain dicts / strings so the API layer stays thin."""
import json
import threading
import time
from datetime import datetime

import httpx
from google import genai
from google.genai import errors, types

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
        _client = genai.Client(api_key=config.GEMINI_API_KEY,
                               http_options=types.HttpOptions(timeout=int(config.GEMINI_TIMEOUT_S * 1000)))
    return _client


NOT_NEEDED = "Budgets, late-data repair, alerts, outage ranking and Under the hood don't need Gemini and keep working."
RETRY_STATUS = {500, 503, 504}      # transient on Google's side: retry once, then try the next model
NEXT_MODEL_STATUS = {404, 429}      # model retired / this model's quota used up: go straight to the next model
_last = threading.local()


def last_model() -> str | None:
    """The model that answered the most recent generate() call on this thread."""
    return getattr(_last, "model", None)


def _reason(e: "errors.APIError") -> str:
    det = (e.details or {}).get("error", {}).get("details", []) if isinstance(e.details, dict) else []
    quota = [v for d in det for v in d.get("violations", []) if v.get("quotaId")]
    retry = next((d.get("retryDelay") for d in det if d.get("retryDelay")), None)
    if quota:
        q = quota[0]
        per = "day" if "PerDay" in q["quotaId"] else "minute" if "PerMinute" in q["quotaId"] else "period"
        tier = " free-tier" if "FreeTier" in q["quotaId"] else ""
        wait = f", resets in ~{round(int(retry.rstrip('s')) / 60)} min" if retry and retry.rstrip("s").isdigit() else ""
        return f"429{tier} limit of {q.get('quotaValue', '?')} requests per {per} reached{wait}"
    msg = str(e.message or "").split(". ")[0].strip().rstrip(".")
    return f"{e.code} {e.status or ''}: {msg}".strip()


def generate(**kwargs):
    """Every Gemini call goes through here. Overloaded (503) or rate-limited (429) models are retried and then
    replaced by GEMINI_FALLBACK_MODELS; timeouts and remaining errors become GeminiUnavailable (HTTP 503 with
    Google's reason) instead of a hung button or a 500."""
    models = list(dict.fromkeys([kwargs.pop("model", config.GEMINI_MODEL)] + config.GEMINI_FALLBACK_MODELS))
    reasons = []
    for model in models:
        for attempt in range(2):
            try:
                resp = client().models.generate_content(model=model, **kwargs)
                _last.model = model
                return resp
            except GeminiUnavailable:
                raise
            except (httpx.TimeoutException, TimeoutError) as e:
                raise GeminiUnavailable(f"Gemini ({model}) didn't answer within {config.GEMINI_TIMEOUT_S:.0f} s. "
                                        f"Try again. {NOT_NEEDED}") from e
            except errors.APIError as e:
                if e.code in RETRY_STATUS and attempt == 0:
                    time.sleep(1.5)
                    continue
                reasons.append(f"{model} → {_reason(e)}")
                if e.code in RETRY_STATUS | NEXT_MODEL_STATUS:
                    break  # next model
                raise GeminiUnavailable(f"Gemini returned an error ({_reason(e)}). {NOT_NEEDED}") from e
            except httpx.HTTPError as e:
                raise GeminiUnavailable(f"Couldn't reach Gemini ({type(e).__name__}). {NOT_NEEDED}") from e
    head = ("Gemini is out of quota for this API key right now" if all("429" in r for r in reasons)
            else "Gemini is overloaded or rate-limited right now")
    hint = ("The free tier allows 20 requests per model per day: enable billing for the key's Google AI Studio "
            "project, or wait for the reset." if all("per day" in r for r in reasons) else "Try again in a minute.")
    raise GeminiUnavailable(f"{head} ({'; '.join(reasons)}). {hint} {NOT_NEEDED}")


def _structured(contents, schema, system: str, temperature: float = 0.2):
    resp = generate(
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
    resp = generate(
        model=config.GEMINI_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(system_instruction=system, tools=tools, temperature=temperature),
    )
    return resp.text or ""


SAFETY = ("You are not a doctor or pharmacist. Never invent label rules. When unsure, tell the person "
          "to ask their pharmacist. Be concise and concrete.")

LANGUAGES = {"en": "English", "ar": "Arabic", "es": "Spanish", "bn": "Bengali"}


def _lang(lang: str | None) -> str:
    """Instruction appended to prompts so answers come back in the reader's language
    (Dearborn has large Arabic-, Spanish- and Bengali-speaking communities)."""
    name = LANGUAGES.get((lang or "en").lower(), "English")
    if name == "English":
        return ""
    return (f" Write every human-readable sentence in {name}. Keep medicine names, numbers, units and any "
            f"verbatim label quotes exactly as given (quotes stay in their original language).")

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
- in_use_days is only for multi-use products with a limit after first opening. Single-dose pens and syringes get 0;
  their "total days at room temperature" belongs in bands.
{SAFETY}"""


def extract_label(data: bytes, mime_type: str) -> dict:
    parts = [types.Part.from_bytes(data=data, mime_type=mime_type),
             "Extract the storage and stability model for this medication."]
    return _structured(parts, StabilityModel, LABEL_SYSTEM, temperature=0.0)


def extract_label_text(product: str, storage_text: str) -> dict:
    """Same extraction, from the text of an official label section (e.g. openFDA 'storage_and_handling')."""
    prompt = (f"Medication: {product}\n\nOfficial label text (Section 16, storage and handling):\n{storage_text}\n\n"
              "Extract the storage and stability model. If the label lists several presentations, use the most common "
              "patient-held one (pen or vial) and say which in notes.")
    return _structured(prompt, StabilityModel, LABEL_SYSTEM, temperature=0.0)


def search_label(name: str) -> dict:
    """Grounded fallback: Google Search + URL Context find the official prescribing information and copy its
    storage section verbatim. Returns the text plus the web sources Gemini actually used."""
    system = ("Find the official FDA prescribing information for the medication (prefer dailymed.nlm.nih.gov, "
              "accessdata.fda.gov or the manufacturer). Copy the storage and handling section VERBATIM, then on the last "
              "line write SOURCE: <url>. If you cannot find an official source, say NOT FOUND.")
    resp = generate(
        model=config.GEMINI_MODEL,
        contents=f"Medication: {name}",
        config=types.GenerateContentConfig(
            system_instruction=system, temperature=0.0,
            tools=[types.Tool(google_search=types.GoogleSearch()), types.Tool(url_context=types.UrlContext())]),
    )
    sources = []
    for cand in resp.candidates or []:
        gm = getattr(cand, "grounding_metadata", None)
        for ch in (gm.grounding_chunks or []) if gm else []:
            if ch.web and ch.web.uri:
                sources.append({"title": ch.web.title, "url": ch.web.uri})
    return {"text": resp.text or "", "sources": sources}


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
def advise(state: dict, lang: str | None = None) -> dict:
    system = f"""You explain a medicine's remaining stability budget to a patient.
You get the label-derived model (with verbatim quotes), the computed life budget, the exposure
episodes that consumed it, data gaps, what-if options and similar historical exposure patterns.
Ground every claim in those numbers and quotes. Mention data gaps if any.
verdict: USE (budget healthy), USE_SOON (budget getting low or burning now), ASK_PHARMACIST (uncertain,
data gaps, or label-based assumption drives the result), DO_NOT_USE (frozen, or budget exhausted).
{SAFETY}""" + _lang(lang) + " The verdict field stays one of the four English codes."
    return _structured(json.dumps(state, default=str), Verdict, system)


def visual_check(photo: bytes, mime_type: str, model: dict, lang: str | None = None) -> dict:
    system = (f"Compare this photo of a medication against the label's visual warnings: "
              f"{json.dumps(model.get('visual_checks', []))}. Describe only what you can see. {SAFETY}" + _lang(lang))
    parts = [types.Part.from_bytes(data=photo, mime_type=mime_type), "Inspect this medication."]
    return _structured(parts, VisualCheck, system)


# ------------------------------------------------------------------ outage rescue
def rescue_plan(at_risk: list[dict], outage: dict, lang: str | None = None) -> str:
    system = f"""You coordinate a neighborhood medicine rescue during a power outage.
For each at-risk person (already ranked by hours of medicine life left), write:
1. a priority line (name, medicine, hours left vs hours until power is restored),
2. which refuge to use (choose from the candidates given, nearest first unless a reason not to),
3. a short, calm SMS (<= 300 chars) to send them.
Finish with a one-paragraph summary for the emergency coordinator. Use markdown. {SAFETY}""" + (
        _lang(lang).replace("every human-readable sentence", "each SMS") + " Keep the coordinator summary in English." if _lang(lang) else "")
    contents = json.dumps({"outage": outage, "at_risk": at_risk}, default=str)
    tools = None
    try:
        tools = [types.Tool(google_maps=types.GoogleMaps())]
        return _text(contents, system, tools=tools)
    except Exception:
        return _text(contents, system)


# ------------------------------------------------------------------ insurance / refill letter
def refill_letter(ctx: dict, lang: str | None = None) -> str:
    system = f"""Write a concise early-refill / replacement request letter that a patient can hand to their
pharmacist or insurer, because their medication was exposed to temperatures outside its labeled
storage conditions. Include: patient placeholder fields in [brackets], the product, a dated exposure
timeline from the data, the exact label text that was exceeded (quoted), the outage reference if any,
and a polite request. Do not exaggerate. Plain text letter format. {SAFETY}""" + (
        f" Write the letter in English (for the pharmacist), then add a short summary for the patient in "
        f"{LANGUAGES.get((lang or 'en').lower(), 'English')}." if _lang(lang) else "")
    return _text(json.dumps(ctx, default=str), system, temperature=0.3)


# ------------------------------------------------------------------ trip pre-check
def parse_trip(itinerary: str, now: datetime) -> dict:
    system = (f"Today is {now.date().isoformat()}. Split this travel itinerary into consecutive legs "
              "describing where the medicine physically is (e.g. on body at the airport, checked bag in "
              "the hold, parked car at the destination). Fill gaps so legs are continuous.")
    return _structured(itinerary, TripPlan, system)


def trip_advice(sim: dict, lang: str | None = None) -> str:
    system = f"""Given a simulated trip exposure for a medicine (per-leg temperatures and budget use),
give 3-5 bullet points of practical advice: which legs are the risk, what to change, what to pack.
Reference the label quotes. {SAFETY}""" + _lang(lang)
    return _text(json.dumps(sim, default=str), system)


# ------------------------------------------------------------------ porch heat brief
def porch_brief(stats: list[dict]) -> str:
    system = f"""You are writing a one-page public-health brief for a mail-order pharmacy about heat exposure of
medication deliveries left in mailboxes/porches, using the aggregated data (mailbox temperature is
modeled from air temperature plus a daytime solar-gain assumption). Highlight the worst ZIP codes and
carriers and give 3 concrete recommendations. Markdown. {SAFETY}"""
    return _text(json.dumps(stats, default=str), system)
