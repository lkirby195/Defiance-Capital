"""Every string the public borrower form shows, in English and Spanish.  # SPEC §4.2

The page is the one thing in this system a borrower reads, and GLENWOOD's borrowers are
contractors and small investors in Oklahoma and Colorado, a good share of whom would rather
read Spanish. So every label, hint, option, button, complaint and the thank-you line is here
twice, the page picks a column, and nothing it stores changes with the column: the deal
carries the same ``T2`` and ``3_5`` and ``12_PLUS`` whichever language the borrower chose.
The team's queue is English and is untouched.

The Spanish is written as plain, respectful Latin American Spanish in the ``usted`` register,
for the audience above - not a word-for-word rendering of the English, which would read like
a form that had been through a machine.

PENDING NATIVE-SPEAKER REVIEW: the ``es`` column was written by the author of this file and
has not been read by a native speaker. Treat every string in it as a draft until one has.

A key is looked up as ``t.key`` in the template; a missing key is a template bug and
``StrictUndefined`` says so. The choice of language comes from ``?lang=`` when the toggle
was pressed and from ``Accept-Language`` otherwise, and English is the default.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

Language = str

ENGLISH: Language = "en"
SPANISH: Language = "es"
LANGUAGES: tuple[Language, ...] = (ENGLISH, SPANISH)
DEFAULT_LANGUAGE: Language = ENGLISH

# (English, Spanish). The Spanish column is pending native-speaker review; see the module
# docstring.
STRINGS: Mapping[str, tuple[str, str]] = {
    # the page
    "page_title": ("Loan request", "Solicitud de préstamo"),
    "heading": ("Tell us about your deal", "Cuéntenos sobre su proyecto"),
    "intro": (
        "A few questions, about five minutes. We do the rest and a member of our team will "
        "be in touch.",
        "Son unas pocas preguntas, unos cinco minutos. Nosotros nos encargamos del resto y "
        "un miembro de nuestro equipo se comunicará con usted.",
    ),
    "toggle": ("Español", "English"),
    "toggle_aria": ("Leer en español", "Read in English"),
    "required": ("Required", "Obligatorio"),
    "optional": ("Optional", "Opcional"),
    "fix_marked": (
        "Please check the fields marked below.",
        "Por favor revise los campos marcados abajo.",
    ),
    "rate_limited": (
        "We have received several requests from your connection in the last hour. Please "
        "try again a little later.",
        "Hemos recibido varias solicitudes desde su conexión en la última hora. Por favor, "
        "inténtelo de nuevo un poco más tarde.",
    ),
    # 1. about you
    "about_you": ("About you", "Sobre usted"),
    "full_name": ("Full name", "Nombre completo"),
    "entity_name": ("Company or LLC, if you have one", "Empresa o LLC, si tiene una"),
    "phone": ("Phone", "Teléfono"),
    "phone_hint": ("10 digits, like 918-555-0142", "10 dígitos, por ejemplo 918-555-0142"),
    "email": ("Email", "Correo electrónico"),
    "credit_range": ("Your credit score", "Su puntaje de crédito"),
    "credit_hint": (
        "Your best guess is fine. We verify it later, with your permission.",
        "Una estimación está bien. Lo verificaremos más adelante, con su permiso.",
    ),
    "credit_top": ("{n} or higher", "{n} o más"),
    "credit_under": ("Under {n}", "Menos de {n}"),
    "pick_one": ("Choose one", "Elija una opción"),
    "experience": (
        "Deals you have completed in the last 3 years",
        "Proyectos que ha completado en los últimos 3 años",
    ),
    "repeat_borrower": (
        "Have you borrowed from us before?",
        "¿Ha pedido un préstamo con nosotros antes?",
    ),
    "yes": ("Yes", "Sí"),
    "no": ("No", "No"),
    # 2. the property
    "the_property": ("The property", "La propiedad"),
    "address": ("Street address", "Dirección (calle y número)"),
    "city": ("City", "Ciudad"),
    "state": ("State", "Estado"),
    "state_OK": ("Oklahoma", "Oklahoma"),
    "state_CO": ("Colorado", "Colorado"),
    "state_OTHER": ("Another state", "Otro estado"),
    "listing_url": (
        "Listing link (Zillow, Redfin, an auction site)",
        "Enlace del anuncio (Zillow, Redfin, un sitio de subastas)",
    ),
    "listing_hint": (
        "If you paste a link and leave the address blank, we read the address from the "
        "link. We never open the page.",
        "Si pega un enlace y deja la dirección en blanco, tomamos la dirección del enlace. "
        "Nunca abrimos la página.",
    ),
    # 3. the deal
    "the_deal": ("The deal", "El negocio"),
    "purchase_price": ("Purchase price", "Precio de compra"),
    "rehab_costs": ("Rehab budget", "Presupuesto de remodelación"),
    "rehab_hint": ("Enter 0 if there is no work to do.", "Ponga 0 si no hay trabajo por hacer."),
    "loan_requested": ("Loan amount you want", "Monto del préstamo que necesita"),
    "estimated_sale_price": (
        "What you expect it to sell for after the rehab",
        "Precio al que espera venderla después de la remodelación",
    ),
    "monthly_rent": (
        "Monthly rent, if you would keep it",
        "Renta mensual, si decide conservarla",
    ),
    # 4. timing
    "timing": ("Timing", "Plazos"),
    "term_bucket": ("How long do you need the loan?", "¿Por cuánto tiempo necesita el préstamo?"),
    "term_3": ("3 months", "3 meses"),
    "term_6": ("6 months", "6 meses"),
    "term_9": ("9 months", "9 meses"),
    "term_12": ("12 months", "12 meses"),
    "term_12_PLUS": ("12+ months", "12+ meses"),
    "closing_date": ("Closing date, if you have one", "Fecha de cierre, si ya la tiene"),
    # 5. how you heard
    "referral_heading": ("How you found us", "Cómo nos encontró"),
    "referral": ("How did you hear about us?", "¿Cómo supo de nosotros?"),
    "referral_hint": ("A few words is plenty.", "Con unas pocas palabras basta."),
    # 6. the consent line above the button: sending the form is the consent (SPEC §4.2)
    "consent": (
        "By submitting, you agree we may contact you about this request.",
        "Al enviar, usted acepta que podamos comunicarnos con usted sobre esta solicitud.",
    ),
    "business_purpose": (
        "GLENWOOD makes business-purpose loans to real estate investors, secured by property "
        "the borrower does not live in.",
        "GLENWOOD otorga préstamos con fines comerciales a inversionistas inmobiliarios, "
        "garantizados con propiedades en las que el prestatario no vive.",
    ),
    "not_commitment": (
        "Sending this form is not a loan commitment. A person reviews every request.",
        "Enviar este formulario no es un compromiso de préstamo. Una persona revisa cada "
        "solicitud.",
    ),
    "honeypot": ("Leave this field empty", "Deje este campo vacío"),
    "submit": ("Send my request", "Enviar mi solicitud"),
    "submit_hint": (
        "The button turns on once every required field is filled in.",
        "El botón se activa cuando todos los campos obligatorios están completos.",
    ),
    # complaints, one per box
    "err_required": ("This is required.", "Este campo es obligatorio."),
    "err_phone": (
        "Please enter a 10-digit phone number.",
        "Ingrese un número de teléfono de 10 dígitos.",
    ),
    "err_email": ("Please enter a valid email address.", "Ingrese un correo electrónico válido."),
    "err_amount": ("Please enter an amount in dollars.", "Ingrese un monto en dólares."),
    "err_amount_positive": (
        "Please enter an amount greater than zero.",
        "Ingrese un monto mayor que cero.",
    ),
    "err_choice": ("Please choose one of the options.", "Elija una de las opciones."),
    "err_date": ("Please enter a date.", "Ingrese una fecha válida."),
    "err_url": (
        "Please paste the whole web address, starting with http.",
        "Pegue la dirección web completa, comenzando con http.",
    ),
    "err_too_long": ("Please keep this shorter.", "Por favor, sea más breve."),
    # thanks
    "thanks_title": ("Thank you", "Gracias"),
    "thanks_body": (
        "We have your request. A member of our team will be in touch soon.",
        "Recibimos su solicitud. Un miembro de nuestro equipo se comunicará con usted pronto.",
    ),
    "thanks_again": ("Send another request", "Enviar otra solicitud"),
}

_COLUMN: dict[Language, int] = {ENGLISH: 0, SPANISH: 1}


def strings(language: Language) -> dict[str, str]:
    """The whole table in one language, keyed for ``t.key`` in a template."""
    column = _COLUMN[language]
    return {key: pair[column] for key, pair in STRINGS.items()}


def other_language(language: Language) -> Language:
    """The language the toggle offers: the one the page is not in."""
    return SPANISH if language == ENGLISH else ENGLISH


# ``es-MX;q=0.9``, ``en``, ``*;q=0.1`` - one entry per language range, a weight after a ``q=``.
_RANGE = re.compile(r"^\s*([A-Za-z*][A-Za-z0-9-]*)\s*(?:;\s*q\s*=\s*([0-9.]+))?\s*$")


def from_accept_language(header: str | None) -> Language:
    """The language the browser asks for first that the page has.  # RFC 9110 §12.5.4

    Weights are honoured and ties go to the earlier entry. ``es-MX`` and ``es-419`` are
    Spanish; anything that is neither Spanish nor English is skipped, and a header that asks
    for nothing the page has - or no header at all - gets English.
    """
    if not header:
        return DEFAULT_LANGUAGE
    ranked: list[tuple[float, int, Language]] = []
    for position, entry in enumerate(header.split(",")):
        match = _RANGE.match(entry)
        if match is None:
            continue
        tag, weight = match.group(1).lower(), match.group(2)
        try:
            quality = float(weight) if weight else 1.0
        except ValueError:
            continue
        if quality <= 0:
            continue
        primary = tag.split("-", 1)[0]
        if primary in LANGUAGES:
            ranked.append((-quality, position, primary))
    if not ranked:
        return DEFAULT_LANGUAGE
    ranked.sort()
    return ranked[0][2]


def choose_language(requested: str | None, accept_language: str | None) -> Language:
    """``?lang=`` when it names a language the page has, else the browser's preference."""
    if requested:
        lowered = requested.strip().lower()
        if lowered in LANGUAGES:
            return lowered
    return from_accept_language(accept_language)
