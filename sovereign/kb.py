"""Atlas knowledge base — first-response SOPs, retrieved at triage time.

Upgrade path: Superlinked (pip install superlinked) embeds these for semantic
top-k; v1 ships a deterministic keyword-overlap scorer so the pipeline runs
offline before any sponsor key exists.
"""
KB: list[dict[str, str]] = [
    {"title": "Motorbike / scooter crash", "body": (
        "Priority order: check breathing and severe bleeding first. Apply direct pressure to bleeding with any cloth. "
        "Do not move a suspected spinal/back injury — call local emergency number (EU 112, US 911, UK 999) and stay with them. "
        "Ask for the nearest hospital location and send it to the duty desk."
    )},
    {"title": "Lost or stolen passport", "body": (
        "Report the theft to local police and get a written report — it is required for the embassy. "
        "Find the nearest consulate or embassy and their business hours. "
        "A temporary emergency passport usually issues same business day; bring passport photo and the police report."
    )},
    {"title": "Theft or mugging, unhurt", "body": (
        "Get to a lit, populated place first. Cancel cards via bank app or hotline, then file a police report for insurance. "
        "Ask what was taken and whether emergency funds are needed; notify the duty desk."
    )},
    {"title": "Sudden illness / fever abroad", "body": (
        "Ask symptoms and duration. Fever over 39C for 2+ days, chest pain, trouble breathing, or confusion = in-person clinic now. "
        "Otherwise hydration, rest, and a pharmacy visit with the symptom list translated if possible."
    )},
    {"title": "Natural disaster / evacuation", "body": (
        "Move up and away from water for floods/tsunami; away from glass for quakes. "
        "Identify the nearest shelter or hotel on high ground, keep a charged phone and water, "
        "and push the alert to the duty desk with last known location."
    )},
    {"title": "Missing in contact, over 12h", "body": (
        "Confirm last contact and last known location. Alert duty desk immediately; they handle police and family notifications. "
        "Do not broadcast the missing person's name publicly before police are notified."
    )},
]
